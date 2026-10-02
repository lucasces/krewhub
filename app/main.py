"""KrewHub central -- primeira fatia funcional (E1).

Substitui o modo manual das fatias anteriores (kubectl exec / editar YAML
na mão / flux reconcile manual) por: POST /devs/{owner_id}/provision faz
reconcile idempotente via API do k8s, espera o pod ficar Ready, registra a
rota no CHP, e persiste em SQLite.

Roda LOCAL nesta fatia (fora do cluster) -- decisão explícita pra essa
etapa: é mais rápido iterar sem precisar buildar/pushar imagem nem passar
pelo Flux a cada mudança de código. Nada aqui impede rodar como Deployment
depois; a única coisa que mudaria é o kubeconfig usado (in-cluster config
em vez de arquivo local) e o alcance de rede até o CHP (que já usa `exec`,
funciona local ou dentro do cluster igual)."""

from __future__ import annotations

import html
import logging
import sqlite3
import urllib.parse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, chp_client, k8s_manager, kiro_login, session_client, store
from app import extensions as ext_registry
from app import k8s_templates as tpl
from app.config import Settings, load_settings
from app.extensions import runtime as ext_runtime
from app.extensions import ui as ext_ui
from app.extensions.contributions import ContributionError
from app.oidc import OIDCConfigError, build_authorization_url, exchange_code

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("krewhub")

app = FastAPI(title="KrewHub central", version="0.1.0")

_settings: Settings = load_settings()

# state -> code_verifier, só durante a janela entre /login e /callback
# (minutos). Em memória de propósito -- o code_verifier NUNCA pode viajar
# de volta pelo browser (é exatamente o segredo que o PKCE protege; só
# `code`/`state` voltam no redirect do IdP, RFC 6749/7636). Não precisa
# sobreviver a um restart do processo: se o pod reciclar no meio de um
# login em andamento, o dev só clica em /login de novo -- mesmo trade-off
# já aceito pra `last_token_issued_at` (sessão é credencial, não
# persistida). Uma única réplica (`strategy: Recreate` no Deployment) --
# sem isso um segundo pod não veria o estado do primeiro.
_pending_logins: dict[str, str] = {}


# Nome do cookie de sessao propria do KrewHub (ver app/auth.py pra
# rationale de token interno assinado vs. access_token do Keycloak).
AUTH_COOKIE_NAME = "krewhub_session"


class AuthRedirect(Exception):
    """Levantada pela dependencia de auth quando quem bateu na rota foi
    claramente um browser navegando (Accept: text/html) sem credencial
    valida -- vira um 302 pro /login em vez de um 401 puro, mais util
    pra um humano que caiu ali sem sessao do que uma tela de erro JSON.
    Chamada programatica (curl/script, sem esse Accept) continua
    recebendo 401 JSON direto, nunca este redirect."""

    def __init__(self, to: str = "/login") -> None:
        self.to = to


@app.exception_handler(AuthRedirect)
def _auth_redirect_handler(request: Request, exc: AuthRedirect) -> RedirectResponse:
    return RedirectResponse(exc.to, status_code=302)


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[len("bearer "):].strip()
    return request.cookies.get(AUTH_COOKIE_NAME)


def _verify_session_checked(token: str) -> str:
    """Valida o token (assinatura + expiracao, via
    `auth.verify_session_payload`) E confere que a geracao embutida
    nele ainda bate com a geracao atual persistida pro owner_id
    (tabela `session_generations`, ver app/store.py) -- e o que da revogacao de
    verdade pro `krewhub_session` (issue #2): um token assinado ANTES
    de um `/logout` (que incrementa a geracao) passa a falhar aqui,
    mesmo com assinatura/expiracao ainda validas.

    Levanta `auth.AuthTokenError` pros dois casos (mesmo tipo de
    excecao de sempre) -- os tres call sites (`root`, `require_session`,
    `logout`) ja tratam esse tipo de erro do jeito certo pra cada um
    (401, 302 pro /login, ou melhor-esforco). Uma falha ao LER a geracao
    no SQLite propaga como `sqlite3.Error` -- nao e credencial invalida,
    entao cada call site trata a parte (503 em `require_session`; "nao
    verificado" em `root`/`logout`).

    Checagem de geracao deliberadamente FORA de app/auth.py -- mantem
    aquele modulo um validador puro de HMAC/expiracao, sem dependencia
    de SQLite (tests/test_auth_tokens.py continua rodando sem banco
    nenhum)."""
    payload = auth.verify_session_payload(token, secret=_settings.session_secret)
    owner_id = payload["owner_id"]
    with store.connect(_settings.db_path) as conn:
        current_gen = store.get_session_generation(conn, owner_id)
    if payload["gen"] != current_gen:
        raise auth.AuthTokenError(
            f"sessao revogada -- geracao do token ({payload['gen']}) nao bate "
            f"com a atual ({current_gen}) pro owner_id '{owner_id}', "
            "provavelmente por causa de um /logout depois deste token ter "
            "sido emitido"
        )
    return owner_id


def require_session(request: Request) -> str:
    """Dependencia FastAPI pros endpoints que agem sobre 'a sessao de
    quem chamou' SEM um owner_id na URL pra comparar (ex.: `/close`,
    diferente de `/devs/{owner_id}/...` -- ver `require_owner` abaixo,
    que REAPROVEITA esta funcao e so acrescenta a checagem de
    cross-owner). Aceita o cookie `krewhub_session` (setado pelo
    `/callback`) OU um header `Authorization: Bearer <token>` (mesma
    verificacao, pra dar pra chamar via curl/script sem depender de
    cookie de browser).

    Sem credencial nenhuma, ou credencial invalida/expirada -- 401 (ou
    302 pro /login se quem chamou parece ser um browser navegando, ver
    _wants_html). SQLite indisponivel ao ler a geracao de sessao -- 503
    (JSON, mesmo pra browser: mandar pro /login nao resolveria nada)."""
    token = _extract_token(request)
    if not token:
        if _wants_html(request):
            raise AuthRedirect("/login")
        raise HTTPException(
            status_code=401,
            detail=(
                "credencial ausente -- cookie 'krewhub_session' (via /login) ou "
                "header 'Authorization: Bearer <token>'"
            ),
        )
    try:
        return _verify_session_checked(token)
    except auth.AuthTokenError as exc:
        if _wants_html(request):
            raise AuthRedirect("/login") from exc
        raise HTTPException(status_code=401, detail=f"credencial invalida: {exc}") from exc
    except sqlite3.Error as exc:
        logger.error("falha ao ler a geracao de sessao no SQLite", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="verificacao de sessao indisponivel (falha no SQLite) -- tente de novo",
        ) from exc


def require_owner(owner_id: str, request: Request) -> str:
    """Dependencia FastAPI pros endpoints que agem sobre um owner_id
    especifico -- fecha o gap documentado em docs/ARCHITECTURE.md, secao
    "Authentication for KrewHub's own endpoints" ("qualquer um que
    alcance o CHP provisiona/reemite sessao pra qualquer owner_id").
    Reaproveita `require_session` pra credencial ausente/invalida (401
    ou 302 pro /login, mesma regra); acrescenta so a checagem de
    cross-owner: credencial valida mas de OUTRO owner_id que nao o da
    URL -- 403, e exatamente isso que fecha o buraco real (antes, dava
    pra chamar /devs/QUALQUER-EMAIL/provision sem ser aquele dev)."""
    token_owner_id = require_session(request)
    if token_owner_id != owner_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"credencial valida, mas pertence a '{token_owner_id}', nao a "
                f"'{owner_id}' -- cada dev so age sobre o proprio owner_id"
            ),
        )
    return token_owner_id


@app.on_event("startup")
def _self_register_route() -> None:
    """Reaproveita a mesma infra já usada pros pods de dev (CHP
    host-routing) pra expor o próprio KrewHub central -- em vez de uma
    Ingress nova (não há Ingress controller neste cluster hoje, ver
    docs/ARCHITECTURE.md, seção "Exposure without an Ingress
    controller"). Só roda se KREWHUB_SELF_HOST
    estiver configurado (vazio por padrão -- não faz sentido tentar isso
    rodando local, fora do cluster, sem o pod do CHP por perto). Falha
    aqui é só um warning, não derruba o boot -- o serviço continua
    acessível via port-forward direto no Service mesmo sem a rota."""
    if not _settings.self_host:
        logger.info("KREWHUB_SELF_HOST não configurado -- pulando auto-registro de rota no CHP")
        return
    try:
        c = k8s_manager.get_clients(_settings)
        target = f"http://krewhub-central.{_settings.chp_namespace}.svc.cluster.local:{_settings.self_port}"
        route = chp_client.register_route(c, _settings, host=_settings.self_host, target=target)
        logger.info("auto-registro de rota no CHP ok: %s", route)
    except Exception:
        logger.exception("auto-registro de rota no CHP falhou -- serviço segue de pé, só sem essa rota")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def root(request: Request) -> RedirectResponse:
    """Entrypoint de `krewhub.kiro.internal` (raiz, sem owner_id na URL
    -- diferente de /devs/{owner_id}/*). Reaproveita a MESMA verificacao
    de `_verify_session_checked` (+ `_extract_token`) ja usada por
    `require_owner` -- nao duplica logica de validacao HMAC/expiracao/
    geracao numa segunda implementacao. Diferenca deliberada em relacao a
    `require_owner`: aqui NUNCA deixa vazar 401/500 cru, nem pra chamada
    programatica sem `Accept: text/html` -- com sessao valida, 302 pro
    lobby do owner_id extraido do PROPRIO cookie/token (nunca de query
    param); sem token, token expirado, assinatura invalida ou payload
    malformado (qualquer `AuthTokenError`), ou falha do SQLite ao ler a
    geracao de sessao (logada como erro), 302 pro /login. `/login` e
    `/callback` nao verificam sessao nem olham pra `/` -- so redirecionam
    PRA FRENTE (IdP e lobby, respectivamente), entao nao ha como esta
    rota fechar um loop com nenhuma das duas. `Cache-Control: no-store`
    porque o destino do redirect depende da sessao de quem pediu -- nunca
    pode ficar cacheado no navegador nem no CHP."""
    token = _extract_token(request)
    owner_id: str | None = None
    if token:
        try:
            owner_id = _verify_session_checked(token)
        except auth.AuthTokenError:
            owner_id = None
        except sqlite3.Error:
            logger.error("falha ao ler a geracao de sessao no SQLite em /", exc_info=True)
            owner_id = None
    target = f"/devs/{urllib.parse.quote(owner_id, safe='')}/lobby" if owner_id else "/login"
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


@app.get("/login")
def login() -> RedirectResponse:
    """Inicia o fluxo OIDC (Authorization Code + PKCE) -- config 100%
    via env var (KREWHUB_OIDC_ISSUER/CLIENT_ID/REDIRECT_URI). Com config
    placeholder/vazia, retorna 501 com o motivo exato em vez de fingir
    sucesso.

    Testado ao vivo (Decisão #3, Keycloak da Somos): devolve um 302 DE
    VERDADE pro `authorization_endpoint` do IdP -- não só o JSON com a
    URL (era assim numa fatia anterior, quando o exchange não tinha sido
    provado ainda). O `code_verifier` (PKCE) é guardado em
    `_pending_logins`, indexado pelo `state`, porque é o único jeito de
    o `/callback` reencontrá-lo -- o browser, no redirect de volta, só
    traz `code`/`state` (nunca o verifier, isso quebraria o PKCE)."""
    try:
        result = build_authorization_url(_settings)
    except OIDCConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    _pending_logins[result["state"]] = result["code_verifier"]
    return RedirectResponse(result["authorization_url"], status_code=302)


@app.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Troca code por token -- testado ao vivo contra um IdP de terceiro
    (Keycloak). Recebe só o que o IdP de fato manda de volta no
    redirect (`code`+`state`, ou `error`+`error_description` se o dev
    cancelar/negar no provider) -- NUNCA o `code_verifier` na URL (ver
    `/login`); ele é resolvido aqui via `_pending_logins[state]`.

    Em caso de sucesso, REDIRECIONA pro lobby (`GET /devs/{owner_id}/lobby`)
    -- fecha o ciclo login -> lobby -> escolhas -> pod pronto. `owner_id`
    = claim `email` ou `sub` (resolvido em `oidc.exchange_code`, mesmo
    critério já usado no restante do fluxo)."""
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"IdP devolveu erro: {error} ({error_description or 'sem descrição'})",
        )
    if not code or not state:
        raise HTTPException(
            status_code=400, detail="callback sem 'code'/'state' -- redirect_uri chamada fora do fluxo /login"
        )

    code_verifier = _pending_logins.pop(state, None)
    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail=(
                "'state' desconhecido ou já usado -- login não começou por "
                "/login nesta instância do processo, ou o processo reiniciou "
                "entre /login e /callback (state é em memória, não persistido)"
            ),
        )

    try:
        result = exchange_code(_settings, code=code, code_verifier=code_verifier)
    except OIDCConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:  # troca de code real pode falhar de várias formas
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    owner_id = result.get("owner_id")
    if not owner_id:
        raise HTTPException(
            status_code=502,
            detail="exchange OIDC não retornou owner_id (claims sem 'email'/'sub')",
        )

    redirect = RedirectResponse(
        f"/devs/{urllib.parse.quote(owner_id, safe='')}/lobby", status_code=302
    )
    with store.connect(_settings.db_path) as conn:
        current_gen = store.get_session_generation(conn, owner_id)
    try:
        session_token = auth.sign_session(
            owner_id,
            secret=_settings.session_secret,
            ttl_seconds=_settings.auth_token_ttl_seconds,
            gen=current_gen,
        )
    except auth.AuthTokenError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    # Secure de verdade so quando a conexao ate aqui foi HTTPS -- hoje o
    # CHP fala HTTP puro com o krewhub-central (ver docs/ARCHITECTURE.md,
    # seção "Exposure without an Ingress controller"), setar Secure incondicional faria o browser
    # DESCARTAR o cookie em silencio numa conexao http:// e quebrar o
    # login inteiro sem nenhum erro visivel. Decisao documentada, nao
    # omitida: quando o CHP ganhar TLS (fora de escopo hoje), isso vira
    # Secure de verdade sem mudanca de codigo.
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    redirect.set_cookie(
        AUTH_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=_settings.auth_token_ttl_seconds,
        path="/",
    )
    return redirect


def _cleanup_extensions(owner_id: str, c, *, all_secrets: bool) -> None:
    """Parte de extensões do `/close`/`/logout`: remove o ConfigMap de
    arquivos e apaga chaves do Secret `krewhub-ext-<slug>` (merge patch
    com `null`; o RBAC não tem `delete` em secrets) -- TODAS no
    `/logout`, só as `generated` no `/close` (o que o dev digitou
    sobrevive). Melhor esforço: nunca levanta. Sem extensões habilitadas
    e sem estado salvo pro dev, não toca no cluster."""
    try:
        with store.connect(_settings.db_path) as conn:
            has_state = bool(store.list_extensions(conn, owner_id))
        if not has_state and not ext_registry.enabled_extensions(_settings):
            return
        k8s_manager.teardown_ext_resources(c, _settings.dev_namespace, tpl.slugify(owner_id))
        ext_runtime.wipe_secrets(_settings, owner_id, generated_only=not all_secrets, c=c)
    except Exception:
        logger.warning("limpeza das extensões falhou pra owner_id=%s", owner_id, exc_info=True)


def _close_dev_session(owner_id: str, *, wipe_all_ext_secrets: bool = False) -> dict:
    """Núcleo reaproveitado por `GET /close` (reporta erro pro chamador,
    via HTTP) e `GET /logout` (melhor esforço -- uma falha aqui NÃO pode
    impedir o logout do KrewHub em si, ver `logout` abaixo).

    Mudança de design (era só revogação de token, ver
    docs/ARCHITECTURE.md, seção "`/close` vs `/logout`"): agora faz DUAS coisas, nesta ordem
    deliberada --

    1. Revoga a sessão do dashboard `kirocrew`
       (`session_client.revoke_session` -- `kirocrew logout` via
       `kubectl exec`, bump de um contador de geração persistido em
       DISCO/PVC). MELHOR ESFORÇO aqui, sempre -- se o pod já não
       responder (ou qualquer outro `SessionError`), só loga e SEGUE pro
       teardown, nunca bloqueia. Motivo de ainda tentar revogar mesmo
       indo derrubar o Pod agora: o contador de geração é
       persistido no MESMO PVC que sobrevive ao teardown abaixo -- então
       um link/token já emitido antes deste `/close` continua rejeitado
       mesmo depois de um `/provision` futuro reconstruir o pod com o
       MESMO volume (a geração não é resetada, só lida de novo no boot).
       Sem essa chamada, o teardown por si só NÃO invalidaria um link
       antigo -- ele voltaria a funcionar no próximo provision.
    2. Derruba o workload em si (`k8s_manager.teardown_dev_workload`):
       Pod + Service + NetworkPolicy + ConfigMap deletados,
       PVC + Secret preservados de propósito. Esta parte NÃO é melhor
       esforço -- é a ação principal do endpoint agora, uma falha real
       (não-404) propaga como `k8s_manager.TeardownError`.

    Levanta `ValueError` se `owner_id` nunca foi provisionado (sem linha
    no SQLite -- não sabe em qual namespace/slug agir). Após o teardown,
    atualiza o registro no SQLite pra `status="closed"` -- reaproveita
    `store.upsert` (só sobrescreve namespace/host/status/detail, NUNCA
    `login_mode`/`login_identity_provider`/`login_region`), sem apagar a
    linha -- é o que deixa o próximo `/provision`/`/open`/`/lobby`
    reautenticar rápido, sem refazer OIDC nem o form do lobby.

    Extensões (`_cleanup_extensions`): roda DEPOIS do teardown, mas mesmo
    quando ele falha -- uma falha de teardown não pode deixar credenciais
    de extensão no Secret. `wipe_all_ext_secrets=True` (só `/logout`)
    apaga todas as chaves; `/close` apaga só as geradas."""
    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)
    if row is None:
        if wipe_all_ext_secrets:
            try:
                _cleanup_extensions(owner_id, k8s_manager.get_clients(_settings), all_secrets=True)
            except Exception:
                logger.warning("limpeza das extensões falhou pra owner_id=%s", owner_id, exc_info=True)
        raise ValueError(f"owner_id={owner_id!r} não provisionado")

    c = k8s_manager.get_clients(_settings)

    revoked = False
    try:
        session_client.revoke_session(c, namespace=row["namespace"], slug=row["slug"])
        revoked = True
    except session_client.SessionError as exc:
        logger.warning(
            "revogação do kirocrew falhou pra owner_id=%s -- seguindo com o teardown mesmo assim: %s",
            owner_id, exc,
        )

    try:
        teardown_result = k8s_manager.teardown_dev_workload(
            c, namespace=row["namespace"], slug=row["slug"]
        )
    finally:
        _cleanup_extensions(owner_id, c, all_secrets=wipe_all_ext_secrets)

    with store.connect(_settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id=owner_id,
            slug=row["slug"],
            namespace=row["namespace"],
            host=row["host"],
            status="closed",
            detail=str(teardown_result["steps"]),
        )

    return {"revoked": revoked, "teardown": teardown_result}


@app.get("/close", response_class=HTMLResponse)
def close_session(owner_id: str = Depends(require_session)) -> HTMLResponse:
    """Desliga o workload k8s deste dev -- Pod, Service,
    NetworkPolicy e ConfigMap deletados (workspace/histórico/login do
    kiro-cli sobrevivem no PVC, que NÃO é tocado, ver `_close_dev_session`).
    Mudança de design deliberada: antes só revogava um token/cookie do
    dashboard sem afetar nenhum recurso k8s -- confuso, "Fechar sessão"
    parecia só invalidar um link. Agora é realmente um culling manual,
    por-dev, sob demanda. Diferente de `/logout`: NÃO limpa o cookie
    `krewhub_session` (o dev continua "logado" no KrewHub) e NÃO chama o
    Keycloak. Exige sessão KrewHub válida (`require_session` --
    reaproveitada, não duplicada) pra saber QUAL owner_id fechar;
    `owner_id` vem sempre do PRÓPRIO cookie/token, nunca de query
    param/URL.

    Depois de fechada, o link de volta é pro lobby -- que, já tendo
    `login_mode` salvo (ver `GET /lobby`), reconcilia o workload do zero
    (idempotente, mesmo PVC) e reautentica rápido, sem precisar refazer
    o ciclo OIDC nem o `kiro-cli login`."""
    try:
        _close_dev_session(owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except k8s_manager.TeardownError as exc:
        raise HTTPException(status_code=502, detail=f"desligar o workload falhou: {exc}") from exc

    owner_id_html = html.escape(owner_id)
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>KrewHub -- sessão encerrada</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 2rem auto;">
  <h1>Workload encerrado</h1>
  <p>O pod do Kiro Crew de <code>{owner_id_html}</code> foi DESLIGADO --
  Pod, Service, NetworkPolicy e ConfigMap desse dev foram
  removidos do cluster (não é só um link/token invalidado como antes).
  Seu workspace, histórico e login do kiro-cli continuam intactos --
  ficam no volume persistente, que não foi tocado. Você continua logado
  no KrewHub.</p>
  <p><a href="/devs/{owner_id_html}/lobby">Voltar ao lobby</a> (reconstrói
  o pod do zero automaticamente, com o mesmo workspace).</p>
</body>
</html>""")


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    """Desloga de TUDO no KrewHub -- diferente de `/close`: além de
    limpar o cookie LOCAL do KrewHub (`krewhub_session`) e redirecionar
    pro `/login`, também faz TUDO que `/close` faz (mesmo núcleo,
    `_close_dev_session` -- não duplica a lógica): revoga a sessão do
    dashboard `kirocrew` (melhor esforço) e desliga o workload k8s
    (Pod/Service/NetworkPolicy/ConfigMap, preservando PVC/Secret).
    Não invalida nem revoga nada do lado do IdP (Keycloak): o próximo
    `/login` simplesmente começa um ciclo OIDC novo do zero, decisão
    deliberada.

    Antes do teardown, incrementa a geracao de sessao do owner_id
    (`store.bump_session_generation`) -- revoga TODO `krewhub_session`
    ja emitido pra ele, inclusive em outras abas/dispositivos, mesmo que
    o teardown falhe depois. `/close` NAO faz isso.

    TUDO que `_close_dev_session` faz é MELHOR ESFORÇO aqui -- diferente
    de `/close` (onde uma falha REAL de teardown vira 502), uma falha
    aqui (owner nunca provisionado, revogação ou teardown indisponível,
    cluster inacessível, etc.) NÃO pode impedir o logout do KrewHub em
    si (limpar cookie + redirect) -- mantém a garantia já testada de
    `/logout` nunca vazar erro.

    `GET` simples, não `POST`/form -- a ação só afeta a sessão/workload
    de QUEM chamou (não muda estado de outro owner_id, não expõe nada
    que um CSRF ganhasse lendo a resposta), risco de CSRF irrelevante
    aqui: na pior hipótese um 3rd-party força o próprio dev a deslogar
    (e desligar o próprio pod) a si mesmo, que só cai no /login de novo
    e reconstrói tudo no próximo /lobby.

    Idempotente -- mesmo `Set-Cookie` de limpeza e mesmo redirect com ou
    sem cookie presente, cookie expirado, ou assinatura inválida; nunca
    passa pelo `AuthTokenError`/`require_owner` (não precisa saber QUEM
    é a sessão pra limpar o cookie -- só precisa saber, quando dá, pra
    tentar a revogação/teardown também)."""
    token = _extract_token(request)
    owner_id: str | None = None
    if token:
        try:
            owner_id = _verify_session_checked(token)
        except auth.AuthTokenError:
            owner_id = None
        except sqlite3.Error:
            # Sem conseguir verificar, nao ha owner_id confiavel pra
            # bump/teardown -- so limpa o cookie e redireciona.
            logger.error("falha ao ler a geracao de sessao no SQLite em /logout", exc_info=True)
            owner_id = None
    if owner_id:
        # Incrementa a geracao de sessao PRIMEIRO (tabela propria
        # `session_generations`, nunca uma linha em `devs` -- ver
        # app/store.py) -- e o que da revogacao de verdade pro proprio
        # krewhub_session (issue #2), e nao pode depender do teardown
        # abaixo dar certo: justamente com a infra fora do ar, o token
        # antigo tem que deixar de valer. Uma falha aqui (SQLite
        # indisponivel) nao pode impedir o logout do KrewHub em si
        # (limpar cookie + redirect).
        try:
            with store.connect(_settings.db_path) as conn:
                store.bump_session_generation(conn, owner_id)
        except sqlite3.Error:
            logger.error(
                "falha ao incrementar a geracao de sessao no /logout pra owner_id=%s",
                owner_id,
                exc_info=True,
            )
        # Teardown melhor esforco -- QUALQUER excecao (nao so
        # TeardownError: get_clients pode levantar RuntimeError com
        # kubeconfig/cluster indisponivel) so e logada.
        try:
            _close_dev_session(owner_id, wipe_all_ext_secrets=True)
        except ValueError:
            logger.info("/logout pra owner_id=%s sem workload provisionado -- nada a desligar", owner_id)
        except Exception:
            logger.warning(
                "revogação/teardown falhou no /logout pra owner_id=%s", owner_id, exc_info=True
            )

    redirect = RedirectResponse("/login", status_code=302)
    # Mesmo critério de is_https já usado no /callback ao SETAR o cookie
    # -- pros atributos do Set-Cookie de limpeza combinarem com os do
    # cookie original (sem isso, um Secure incondicional aqui não
    # limparia o cookie setado sem Secure numa conexão http:// atual).
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    redirect.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
        secure=is_https,
        httponly=True,
        samesite="lax",
    )
    return redirect


def _do_provision(owner_id: str, *, wait: bool = True) -> dict:
    """Núcleo do reconcile -- extraído pra ser reaproveitado por
    `POST /devs/{owner_id}/provision` (chamada direta, uso de
    script/CI) e por `POST /devs/{owner_id}/lobby` (encadeado
    automaticamente depois que o dev escolhe as opções da sessão)."""
    if not owner_id.strip():
        raise HTTPException(status_code=400, detail="owner_id vazio")

    try:
        plans = ext_runtime.build_plans(_settings, owner_id)
        # Só passa os planos quando há extensões ativas (o reconcile sem
        # extensões segue exatamente como era).
        result = (
            k8s_manager.reconcile_dev(_settings, owner_id, plans)
            if plans
            else k8s_manager.reconcile_dev(_settings, owner_id)
        )
    except ContributionError as exc:
        raise HTTPException(status_code=422, detail=f"extensão inválida: {exc}") from exc
    namespace = result["namespace"]

    with store.connect(_settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id=owner_id,
            slug=result["slug"],
            namespace=namespace,
            host=result["host"],
            status="reconciled",
            detail=str(result["steps"]),
        )

    ready = True
    if wait:
        c = k8s_manager.get_clients(_settings)
        ready = k8s_manager.wait_for_ready(c, namespace, result["slug"])
        with store.connect(_settings.db_path) as conn:
            store.upsert(
                conn,
                owner_id=owner_id,
                slug=result["slug"],
                namespace=namespace,
                host=result["host"],
                status="ready" if ready else "timeout_waiting_ready",
                detail=str(result["steps"]),
            )
        if not ready:
            raise HTTPException(
                status_code=504,
                detail=f"pod em {namespace} não ficou Ready dentro do timeout",
            )
        if plans:
            ext_runtime.on_pod_ready_best_effort(_settings, owner_id, c)

    target = f"http://kirocrew-{result['slug']}.{namespace}.svc.cluster.local:5476"
    c = k8s_manager.get_clients(_settings)
    try:
        route = chp_client.register_route(c, _settings, host=result["host"], target=target)
    except chp_client.CHPError as exc:
        with store.connect(_settings.db_path) as conn:
            store.upsert(
                conn,
                owner_id=owner_id,
                slug=result["slug"],
                namespace=namespace,
                host=result["host"],
                status="route_registration_failed",
                detail=str(exc),
            )
        raise HTTPException(status_code=502, detail=f"registro de rota no CHP falhou: {exc}") from exc

    with store.connect(_settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id=owner_id,
            slug=result["slug"],
            namespace=namespace,
            host=result["host"],
            status="routed",
            detail=str(result["steps"]),
        )

    # Fecha o fluxo "provision -> já cai logado": emite o token de sessão
    # agora (automatiza o que era `kubectl exec ... kirocrew token` manual)
    # e devolve a URL pronta. Decisão de formato (raciocínio abaixo):
    # aqui é JSON (`dashboard_url_with_token`), não um 302 -- /provision é
    # uma chamada de infraestrutura (idempotente, pensada pra script/CI,
    # devolve o estado inteiro do reconcile), misturar um redirect nela
    # quebraria isso pra qualquer client HTTP não-browser. Quem quer a
    # "sensação" de login de verdade (redirect) usa GET /devs/{owner_id}/open.
    dashboard_url_with_token = None
    try:
        dashboard_url_with_token = session_client.issue_token_url(
            c,
            namespace=namespace,
            slug=result["slug"],
            host=result["host"],
            public_port=_settings.public_port,
            scheme=_settings.dev_pod_scheme,
            ttl=_settings.session_ttl,
        )
        with store.connect(_settings.db_path) as conn:
            store.mark_token_issued(conn, owner_id)
    except session_client.SessionError as exc:
        logger.warning("emissão de token falhou pra owner_id=%s: %s", owner_id, exc)

    return {
        **result,
        "ready": ready,
        "route": route,
        "dashboard_url_with_token": dashboard_url_with_token,
    }


@app.post("/devs/{owner_id}/provision")
def provision(owner_id: str, wait: bool = True, _owner: str = Depends(require_owner)) -> dict:
    """Reconcile idempotente do pod-por-dev + registro de rota no CHP +
    persistência. `owner_id` por enquanto é só uma string simulando o
    pós-login (achado de uma fatia anterior: não precisa ser um claim OIDC
    real -- kirocrew não valida essa identidade contra nada)."""
    return _do_provision(owner_id, wait=wait)


@app.post("/devs/{owner_id}/session")
def new_session(owner_id: str, _owner: str = Depends(require_owner)) -> dict:
    """Reemissão sob demanda -- útil quando a sessão anterior expirou (o
    LINK expira em ~5min se não clicado; a sessão criada após o clique
    dura o `--ttl` passado a `kirocrew token`, hoje `KREWHUB_SESSION_TTL`).
    Não requer que /provision tenha sido chamado antes NESTA execução do
    processo, só que o namespace já exista (senão 404)."""
    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="owner_id não provisionado")

    c = k8s_manager.get_clients(_settings)
    try:
        url = session_client.issue_token_url(
            c,
            namespace=row["namespace"],
            slug=row["slug"],
            host=row["host"],
            public_port=_settings.public_port,
            ttl=_settings.session_ttl,
        )
    except session_client.SessionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with store.connect(_settings.db_path) as conn:
        store.mark_token_issued(conn, owner_id)

    return {"owner_id": owner_id, "dashboard_url_with_token": url}


@app.get("/devs/{owner_id}/open")
def open_dashboard(owner_id: str, _owner: str = Depends(require_owner)) -> RedirectResponse:
    """Entrypoint pensado pra ser aberto direto no navegador: reconcilia
    o workload (idempotente, `_do_provision` -- rápido quando o pod já
    existe) e devolve HTTP 302 pra URL já autenticada -- simula "login
    real completou, caiu no dashboard" sem passo manual no meio.

    Reconciliar aqui (em vez de só emitir token contra o que já existir)
    passou a importar depois de `/close`/`/logout` desligarem o workload
    de verdade (ver docs/ARCHITECTURE.md, seção "`/close` vs `/logout`"):
    sem isso, um `/open` batido depois de um
    `/close` anterior (ex.: link salvo/favoritado) falharia com 502
    ("nenhum pod Running") em vez de reconstruir o pod do zero -- mesma
    garantia que `/provision`/`/lobby` já davam.

    Protegido por `require_owner`, mesmo guard de `/lobby`. Sem sessão
    (cookie ausente/expirado) e navegação de browser: 302 pro `/login`
    (fluxo completo termina no PRÓPRIO lobby do dev, não de volta aqui --
    não há passthrough de "voltar pra URL original" através do OIDC,
    trade-off aceito). Chamada programática sem sessão: 401 JSON. Sessão
    válida mas de OUTRO owner_id (ex.: link salvo/favoritado de outro dev):
    sempre 403 -- isso é falha de autorização, nunca vira um redirect
    silencioso pro /login."""
    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="owner_id não provisionado")

    result = _do_provision(owner_id, wait=True)
    url = result.get("dashboard_url_with_token")
    if not url:
        raise HTTPException(status_code=502, detail="emissão de token falhou -- ver logs do serviço")

    return RedirectResponse(url, status_code=302)


@app.post("/devs/{owner_id}/kiro-login")
def kiro_login_start(
    owner_id: str,
    mode: str | None = Query(
        None,
        description="'org' (Identity Center/SSO) ou 'personal' (Builder ID) -- obrigatório, sem default implícito",
    ),
    identity_provider: str | None = Query(
        None, description="Start URL do Identity Center -- só usado com mode=org"
    ),
    region: str | None = Query(None, description="Region -- só usado com mode=org"),
    _owner: str = Depends(require_owner),
) -> dict:
    """Automatiza o `kiro-cli login` (device-flow) que até esta fatia era
    feito à mão via `kubectl exec` + técnica pty+FIFO (documentada no
    README do GitOps). Fire-and-forget: dispara o device-flow dentro do
    pod e devolve a URL+código assim que aparecem -- NÃO espera o dev
    clicar (o processo continua fazendo polling dentro do pod,
    setsid-destacado, depois que respondemos).

    `mode` é obrigatório e não tem default implícito -- 400 se ausente ou
    inválido, igual à régua já seguida pro OIDC genérico (nenhum valor
    assumido silenciosamente). Pra `mode=org`, `identity_provider`/
    `region` vêm da query se informados, senão caem pro default de
    `KREWHUB_KIRO_IDENTITY_PROVIDER`/`KREWHUB_KIRO_REGION`; se nenhuma das
    duas fontes tiver valor, também é 400 (nenhuma organização default).
    `mode=personal` ignora os dois.

    Idempotente: se `kiro-cli whoami` já mostra sessão válida, não dispara
    um device-flow novo -- devolve `already_logged_in: true`."""
    if mode not in kiro_login.MODES:
        raise HTTPException(
            status_code=400,
            detail=f"parâmetro 'mode' obrigatório e deve ser um de {kiro_login.MODES} -- sem default implícito",
        )

    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="owner_id não provisionado")

    resolved_identity_provider = None
    resolved_region = None
    if mode == "org":
        resolved_identity_provider = identity_provider or _settings.kiro_identity_provider
        resolved_region = region or _settings.kiro_region
        if not resolved_identity_provider or not resolved_region:
            raise HTTPException(
                status_code=400,
                detail=(
                    "mode=org exige 'identity_provider' e 'region' -- via query param ou "
                    "KREWHUB_KIRO_IDENTITY_PROVIDER/KREWHUB_KIRO_REGION; nenhuma organização "
                    "é assumida como default"
                ),
            )

    c = k8s_manager.get_clients(_settings)
    try:
        result = kiro_login.start_device_flow(
            c,
            namespace=row["namespace"],
            slug=row["slug"],
            mode=mode,
            identity_provider=resolved_identity_provider,
            region=resolved_region,
        )
    except kiro_login.KiroLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"owner_id": owner_id, "mode": mode, **result}


async def _form_fields(request: Request) -> dict[str, str]:
    """Todos os campos string do form -- as extensões declaram campos
    dinâmicos (`ext.<id>.<chave>`) que o `Form(...)` do FastAPI não
    conhece de antemão."""
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


def _extensions_form_html(owner_id: str, errors: dict[str, list[str]] | None = None) -> str:
    """Seção "Extensões" do form do lobby: uma caixa por extensão
    habilitada pelo admin (vazio se nenhuma). Valores de campos `secret`
    nunca voltam preenchidos."""
    exts = ext_registry.enabled_extensions(_settings)
    if not exts:
        return ""
    with store.connect(_settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)
    return ext_ui.render_config_section(
        [(ext, rows.get(ext_id), (errors or {}).get(ext_id, [])) for ext_id, ext in exts.items()]
    )


def _lobby_form_html(owner_id: str, *, error: str | None = None, ext_errors: dict | None = None) -> str:
    """HTML puro (sem JS/framework) -- form de escolha da sessão, servido
    ANTES do provision rodar. `identity_provider`/`region` vêm
    pré-preenchidos com o default de KREWHUB_KIRO_IDENTITY_PROVIDER/
    KREWHUB_KIRO_REGION só pra poupar digitação -- o dev pode sobrescrever
    ou apagar, nenhuma organização é imposta."""
    owner_id_html = html.escape(owner_id)
    error_html = (
        f'<p style="color:#b00;font-weight:bold">{html.escape(error)}</p>' if error else ""
    )
    default_ip = html.escape(_settings.kiro_identity_provider)
    default_region = html.escape(_settings.kiro_region)
    extensions_html = _extensions_form_html(owner_id, ext_errors)
    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>KrewHub -- nova sessão</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 2rem auto;">
  <h1>Configurar sua sessão</h1>
  <p>Dev: <code>{owner_id_html}</code></p>
  {error_html}
  <form method="post" action="/devs/{owner_id_html}/lobby">
    <fieldset>
      <legend>Como você vai logar no <code>kiro-cli</code>?</legend>
      <label><input type="radio" name="login_mode" value="org" required> Pro (organização / Identity Center SSO)</label><br>
      <label><input type="radio" name="login_mode" value="personal"> Individual (Builder ID)</label>
    </fieldset>
    <fieldset>
      <legend>Só se "Pro" for selecionado acima</legend>
      <label>Identity provider (start URL):<br>
        <input type="text" name="identity_provider" value="{default_ip}" size="60"
               placeholder="https://sua-org.awsapps.com/start"></label><br><br>
      <label>Region:<br>
        <input type="text" name="region" value="{default_region}" size="20"
               placeholder="us-east-1"></label>
    </fieldset>
    {extensions_html}
    <br>
    <button type="submit">Iniciar sessão</button>
  </form>
</body>
</html>"""


def _lobby_result_html(
    owner_id: str,
    *,
    dashboard_url_with_token: str | None,
    kiro_result: dict | None,
    kiro_error: str | None,
    extensions_html: str = "",
) -> str:
    """Ordem deliberada: login do kiro-cli PRIMEIRO, dashboard DEPOIS --
    sem o login completo, o dashboard mostra a tela de "sandbox
    unavailable"/sign-in (achado de fatia anterior), então mandar o dev
    clicar no link errado primeiro só gera confusão. Os dois links abrem
    em aba nova (`target="_blank"`) pra não perder esta página de
    instruções ao clicar."""
    owner_id_html = html.escape(owner_id)

    if kiro_error:
        kiro_html = f'<p style="color:#b00">kiro-cli login falhou: {html.escape(kiro_error)}</p>'
    elif kiro_result and kiro_result.get("already_logged_in"):
        kiro_html = (
            "<p><strong>Você já está logado</strong> no kiro-cli nesse pod -- "
            "nenhum login novo foi disparado. Pode pular direto pro passo 2.</p>"
        )
    elif kiro_result:
        url = html.escape(kiro_result["verification_url"])
        code = html.escape(kiro_result["user_code"])
        kiro_html = (
            f'<p><strong>1. Faça login no kiro-cli:</strong> clique no link abaixo, '
            f'confirme que o código mostrado é <code>{code}</code> e aprove o acesso.</p>'
            f'<p><a href="{url}" target="_blank" rel="noopener">{url}</a></p>'
        )
    else:
        kiro_html = "<p><em>nenhum login de kiro-cli foi disparado</em></p>"

    if dashboard_url_with_token:
        url = html.escape(dashboard_url_with_token)
        dash_html = (
            '<p><strong>2. Depois de logado, acesse seu dashboard:</strong> '
            'clique no link abaixo pra abrir o Kiro Crew já autenticado.</p>'
            f'<p><a href="{url}" target="_blank" rel="noopener">{url}</a></p>'
        )
    else:
        dash_html = "<p><em>emissão do token do dashboard falhou -- ver logs do serviço</em></p>"

    # Links discretos: "Reconfigurar sessão" pra quem quer trocar org/modo
    # manualmente sem apagar o registro no banco (`?reconfigure=1` faz o
    # GET mostrar o form de novo, ver `lobby_form`); "Fechar sessão"
    # (`/close`) desliga o pod (workload k8s), mantendo o dev logado no
    # KrewHub; "Sair" (`/logout`) desloga de tudo (cookie do KrewHub +
    # workload) e ainda desliga o pod. Texto curto explicando a diferença
    # -- os dois nomes sozinhos ("fechar" vs "sair") não deixam óbvio que
    # os DOIS agora desligam infraestrutura de verdade (Pod/
    # Service/NetworkPolicy/ConfigMap), não só invalidam um link -- essa
    # é a mudança de design desta fatia (ver docs/ARCHITECTURE.md,
    # seção "`/close` vs `/logout`").
    session_links_html = (
        f'<p style="margin-top:2rem">'
        f'<a href="/devs/{owner_id_html}/lobby?reconfigure=1" style="font-size:0.85em;color:#666">'
        "Reconfigurar sessão</a>"
        ' &nbsp;|&nbsp; '
        '<a href="/close" style="font-size:0.85em;color:#666">Fechar sessão (desliga o pod)</a>'
        ' &nbsp;|&nbsp; '
        '<a href="/logout" style="font-size:0.85em;color:#666">Sair</a>'
        "</p>"
        '<p style="font-size:0.8em;color:#999">'
        '"Fechar sessão" DESLIGA o pod do Kiro Crew (Pod/Service/rede '
        'removidos do cluster) -- seu workspace e histórico continuam salvos, '
        'o próximo acesso reconstrói tudo automaticamente; você continua '
        'logado no KrewHub. "Sair" faz o mesmo e ainda desloga de tudo no '
        "KrewHub."
        "</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>KrewHub -- sessão pronta</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 2rem auto;">
  <h1>Sessão de {owner_id_html}</h1>
  <p>Sua sessão foi provisionada. Siga os dois passos abaixo, nesta ordem
  -- os links abrem em aba nova, esta página continua aberta.</p>
  {kiro_html}
  {dash_html}
  {extensions_html}
  {session_links_html}
</body>
</html>"""


def _run_lobby_session(
    owner_id: str,
    *,
    login_mode: str,
    identity_provider: str,
    region: str,
) -> HTMLResponse:
    """Núcleo reaproveitado por `POST /devs/{owner_id}/lobby` (form
    recém-submetido) E por `GET /devs/{owner_id}/lobby` quando já existe
    registro com `login_mode` salvo (pula o form -- ver `lobby_form`):
    reconcile idempotente (`_do_provision`, rápido quando o pod já
    existe) + `kiro-login` idempotente (`kiro_login.start_device_flow`,
    não dispara device-flow novo se já há sessão) + monta a MESMA página
    final de resultado. `login_mode`/`identity_provider`/`region` aqui já
    são os valores RESOLVIDOS (form ou default de env var já aplicado,
    nunca vazio pra `mode=org`) -- resolver a partir de input bruto é
    responsabilidade do chamador (`lobby_submit` faz isso a partir do
    form; `lobby_form` lê direto do que já foi persistido, já resolvido
    da vez anterior). Persistência da escolha (`store.set_login_choice`)
    também é responsabilidade do chamador -- não duplicada aqui."""
    provision_result = _do_provision(owner_id, wait=True)

    c = k8s_manager.get_clients(_settings)
    kiro_result = None
    kiro_error = None
    try:
        kiro_result = kiro_login.start_device_flow(
            c,
            namespace=provision_result["namespace"],
            slug=provision_result["slug"],
            mode=login_mode,
            identity_provider=identity_provider or None,
            region=region or None,
        )
    except kiro_login.KiroLoginError as exc:
        kiro_error = str(exc)
        logger.warning("kiro-login encadeado falhou pra owner_id=%s: %s", owner_id, exc)

    return HTMLResponse(
        _lobby_result_html(
            owner_id,
            dashboard_url_with_token=provision_result.get("dashboard_url_with_token"),
            kiro_result=kiro_result,
            kiro_error=kiro_error,
            # Cartões das extensões carregam à parte (iframe -> GET
            # /extensions/cards): hooks de status nunca bloqueiam o lobby.
            extensions_html=(
                ext_ui.render_cards_iframe(owner_id)
                if ext_runtime.active_extensions(_settings, owner_id)
                else ""
            ),
        )
    )


@app.get("/devs/{owner_id}/lobby", response_class=HTMLResponse)
def lobby_form(
    owner_id: str,
    reconfigure: bool = Query(
        False,
        description=(
            "Força mostrar o form de escolha de novo, mesmo com login_mode já "
            "salvo de uma execução anterior -- ver link 'Reconfigurar sessão'"
        ),
    ),
    _owner: str = Depends(require_owner),
) -> HTMLResponse:
    """Primeira vez desse owner_id (nenhum registro, ou registro sem
    `login_mode` salvo -- ex.: ficou só em `lobby_pending`) OU
    `?reconfigure=1`: mostra o form ANTES do pod subir, o dev escolhe
    mode (org/personal) e, se org, identity_provider/region -- sem
    nenhuma opção pré-selecionada nem organização sugerida como default
    (só pré-popula os CAMPOS de texto com o default de env var, que o dev
    pode sobrescrever).

    Owner_id JÁ provisionado antes (`login_mode` salvo no SQLite de um
    `POST /lobby` anterior) e sem `?reconfigure=1`: PULA o form -- vai
    direto pro reconcile idempotente + kiro-login idempotente com os
    MESMOS valores salvos da vez anterior (`_run_lobby_session`, mesma
    função que o POST usa -- não duplica a lógica aqui), e mostra a
    MESMA página final de resultado. Pensado pra "eu já configurei uma
    vez, só quero voltar pro dashboard" não pedir o form de novo."""
    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)

    if reconfigure or row is None or not row["login_mode"]:
        return HTMLResponse(_lobby_form_html(owner_id))

    return _run_lobby_session(
        owner_id,
        login_mode=row["login_mode"],
        identity_provider=row["login_identity_provider"] or "",
        region=row["login_region"] or "",
    )


@app.post("/devs/{owner_id}/lobby", response_class=HTMLResponse)
def lobby_submit(
    owner_id: str,
    login_mode: str = Form(...),
    identity_provider: str = Form(""),
    region: str = Form(""),
    form_fields: dict[str, str] = Depends(_form_fields),
    _owner: str = Depends(require_owner),
) -> HTMLResponse:
    """Recebe a escolha do form, PERSISTE (store.set_login_choice),
    encadeia o reconcile (_do_provision) e, assim que o pod fica Ready,
    dispara o /kiro-login equivalente com os MESMOS valores que o dev
    acabou de escolher -- fecha o ciclo sem uma segunda chamada manual.

    `login_mode` sem valor válido -- 400 (mesma régua de /kiro-login:
    nenhum modo assumido por default). Pra `login_mode=org`, os campos
    vazios caem pro default de env var; se nem o form nem a env var
    tiverem valor, também 400 (nenhuma organização default)."""
    if login_mode not in kiro_login.MODES:
        raise HTTPException(
            status_code=400,
            detail=f"'login_mode' deve ser um de {kiro_login.MODES} -- sem default implícito",
        )

    resolved_identity_provider = None
    resolved_region = None
    if login_mode == "org":
        resolved_identity_provider = identity_provider.strip() or _settings.kiro_identity_provider
        resolved_region = region.strip() or _settings.kiro_region
        if not resolved_identity_provider or not resolved_region:
            raise HTTPException(
                status_code=400,
                detail=(
                    "login_mode=org exige identity_provider e region -- via form ou "
                    "KREWHUB_KIRO_IDENTITY_PROVIDER/KREWHUB_KIRO_REGION; nenhuma organização "
                    "é assumida como default"
                ),
            )

    ext_changes, ext_errors = ext_runtime.parse_form(_settings, owner_id, form_fields)
    if ext_errors:
        return HTMLResponse(_lobby_form_html(owner_id, ext_errors=ext_errors), status_code=400)
    try:
        ext_runtime.save_form(_settings, owner_id, ext_changes)
    except Exception as exc:
        logger.error("falha ao salvar a config das extensões de owner_id=%s", owner_id, exc_info=True)
        raise HTTPException(status_code=502, detail="falha ao salvar a configuração das extensões") from exc

    with store.connect(_settings.db_path) as conn:
        store.set_login_choice(
            conn,
            owner_id=owner_id,
            mode=login_mode,
            identity_provider=resolved_identity_provider or "",
            region=resolved_region or "",
        )

    return _run_lobby_session(
        owner_id,
        login_mode=login_mode,
        identity_provider=resolved_identity_provider or "",
        region=resolved_region or "",
    )


@app.get("/devs/{owner_id}")
def get_dev(owner_id: str, _owner: str = Depends(require_owner)) -> dict:
    """Lookup do registro de um dev específico. Protegido por
    `require_owner` (mesmo guard de `/provision`/`/session`/`/lobby`)."""
    with store.connect(_settings.db_path) as conn:
        row = store.get(conn, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="owner_id não provisionado")
    return dict(row)


@app.get("/devs/{owner_id}/extensions")
def extensions_status(owner_id: str, _owner: str = Depends(require_owner)) -> dict:
    """Estado (JSON) das extensões habilitadas pelo admin: `state`,
    `conditions` e ações disponíveis de cada uma."""
    views = ext_runtime.evaluate(_settings, owner_id)
    return {"extensions": [v.to_json() for v in views]}


@app.get("/devs/{owner_id}/extensions/cards", response_class=HTMLResponse)
def extensions_cards(owner_id: str, _owner: str = Depends(require_owner)) -> HTMLResponse:
    """Cartões das extensões (HTML puro, sem JS). Embutido via `<iframe>`
    na página final do lobby; recarrega sozinho (`meta refresh`) conforme
    `ext_runtime.wants_refresh`."""
    views = ext_runtime.evaluate(_settings, owner_id)
    refresh = 5 if ext_runtime.wants_refresh(views) else None
    return HTMLResponse(
        ext_ui.render_cards_document(
            owner_id,
            [v.card_view() for v in views],
            lambda ext_id, action_id: ext_runtime.make_csrf(
                _settings.session_secret, owner_id, ext_id, action_id
            ),
            refresh_seconds=refresh,
        )
    )


@app.post("/devs/{owner_id}/extensions/{ext_id}/actions/{action_id}")
def extension_action(
    owner_id: str,
    ext_id: str,
    action_id: str,
    request: Request,
    form_fields: dict[str, str] = Depends(_form_fields),
    _owner: str = Depends(require_owner),
):
    """Executa uma ação declarada pela extensão. Browser (cookie de
    sessão): exige o token anti-CSRF do cartão (HMAC amarrado a
    owner+extensão+ação, com validade) e responde 303 de volta aos
    cartões. Chamada programática com `Authorization: Bearer`: sem CSRF
    (o header não é enviado automaticamente pelo browser) e resposta
    JSON."""
    bearer = (request.headers.get("authorization") or "").lower().startswith("bearer ")
    if not bearer and not ext_runtime.verify_csrf(
        _settings.session_secret, form_fields.get("csrf", ""), owner_id, ext_id, action_id
    ):
        raise HTTPException(status_code=403, detail="token anti-CSRF inválido ou expirado")
    try:
        result = ext_runtime.run_action(_settings, owner_id, ext_id, action_id, form_fields)
    except ext_runtime.ActionRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if bearer or not _wants_html(request):
        return {"ok": result.ok, "message": result.message}
    return RedirectResponse(f"/devs/{urllib.parse.quote(owner_id, safe='@')}/extensions/cards", status_code=303)

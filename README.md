# KrewHub central

Serviço que substitui o modo 100% manual usado nas fatias anteriores do
KrewHub (`kubectl exec` na mão, editar YAML no repo GitOps, `flux
reconcile` manual) por uma API que faz o reconcile do template
pod-por-dev via API do Kubernetes.

**Roda dentro do cluster desde a fatia de deploy** (namespace `<namespace-real>`,
mesmo onde o CHP já roda) — ver seção "Deploy no cluster" abaixo pra todo
o detalhe (imagem, RBAC, como acessar). Continua dando pra rodar local
também (fora do cluster, lendo `~/.kube/config-personal`) pra iteração
rápida de código sem precisar rebuildar imagem a cada mudança —
`app/k8s_manager.py` tenta `load_incluster_config()` primeiro e cai pro
kubeconfig local se isso falhar.

> **Nota de rebrand (KiroHub -> KrewHub):** o serviço, código, env vars
> (`KIROHUB_*` -> `KREWHUB_*`), FastAPI app, docstrings, logger names e o
> arquivo SQLite (`kirohub.db` -> `krewhub.db`) foram renomeados nesta
> fatia. `kiro.internal`, `kirocrew`, `kiro-cli`, `kiro-dev-*` continuam
> com o nome antigo de propósito -- são nomenclatura do **produto Kiro
> Crew que estamos hospedando**, não do nosso hub, então não fazem parte
> do rebrand por definição.
>
> O namespace k8s real onde o CHP e o `krewhub-central` rodam hoje, e o
> diretório GitOps correspondente, mantêm o nome antigo na
> infraestrutura viva (CHP rodando, rotas registradas, Kustomization do
> Flux apontando pra lá) -- renomear exigiria deletar/recriar namespace
> (alto blast-radius, exige aprovação explícita, não assumida aqui).
> Numa limpeza posterior, porém, o CÓDIGO deste repo deixou de ter esse
> nome real hardcoded como default (`KREWHUB_CHP_NAMESPACE`/
> `KREWHUB_K8S_CONTEXT` em `app/config.py` agora são genéricos/vazios --
> ver tabela de env vars abaixo) -- o valor real passa a vir só do
> manifest de deploy (fora deste repositório), nunca commitado aqui. Pela
> mesma razão, os valores de teste usados nas seções "Provado ao vivo"
> também foram generalizados nessa limpeza (ex.: `e2e-test@krewhub.local.test`
> em vez do domínio de teste antigo) -- deixaram de ser preservados como
> registro histórico literal.

## O que faz

- `POST /devs/{owner_id}/provision` — reconcile **idempotente**
  (create-se-não-existir / patch-se-já-existir) de: Namespace, Secret
  (`kiro-owner-id`), ConfigMap (`kiro-config`, incluindo
  `KIROCREW_CORS_ORIGINS` pro Host-header allowlist do dashboard), PVC
  (`kiro-workspace`, `rook-cephfs` RWO por default -- configurável via
  `KREWHUB_STORAGE_CLASS`), Service, NetworkPolicy (só CHP alcança a
  porta 5476), e o Deployment `kirocrew` — com TODO o hardening já
  validado ao vivo nas fatias anteriores (`seccompProfile: Unconfined` só
  no container pro sandbox `unshare(CLONE_NEWUSER)` funcionar, `fsGroup:
  1000`, `readOnlyRootFilesystem`, drop ALL caps) + qualquer overlay JSON
  Patch configurado por cima (ex.: o nodeAffinity pro control-plane deste
  cluster -- ver seção "Overlay JSON Patch por-cluster" abaixo). Depois
  espera o pod
  ficar Ready e registra a rota no CHP via `exec` no pod dele (a API de
  admin do CHP é loopback-only de propósito — ver
  `<repo-gitops>/clusters/<cluster>/<namespace-real>/chp/deployment.yaml`
  — então falamos com ela de dentro do pod, nunca abrindo rede nova).
  Persiste `owner_id -> {namespace, host, status}` em SQLite
  (`krewhub.db`, MVP — nada de operator/CRD).
- `GET /login` — monta a authorization URL OIDC (Authorization Code +
  PKCE), reaproveitando a mesma lógica já provada em
  `<repo-gitops>/clusters/<cluster>/<namespace-real>/oidc-client-poc/oidc_client.py`
  contra 3 issuers reais. 100% config-driven — sem `KREWHUB_OIDC_*`
  configurado, retorna 501 explícito. **Testado ao vivo contra o
  Keycloak de um IdP real de terceiro** (Decisão #3, ver seção dedicada abaixo) — devolve
  um 302 de verdade pro `authorization_endpoint` real.
- `GET /callback` — troca `code` por token, resolve `owner_id` do claim
  `email`/`sub` e, em caso de sucesso, seta o cookie de sessão própria
  do KrewHub (`krewhub_session`, ver seção "Autenticação dos próprios
  endpoints" abaixo) e redireciona (302) pro `GET /devs/{owner_id}/lobby`
  em vez de devolver os claims direto — fecha o ciclo
  login -> lobby -> escolhas -> pod pronto. Recepção real de
  `code`/`state` do IdP testada ao vivo; o exchange completo (troca do
  `code` por token) depende do Lucas terminar o login no navegador — ver
  seção dedicada abaixo pro status exato.
- `GET`/`POST /devs/{owner_id}/lobby` — tela HTML simples (form puro,
  sem JS) ANTES do pod subir: o dev escolhe `login_mode`
  (`org`/`personal`) e, se `org`, `identity_provider`/`region`. `POST`
  no mesmo path recebe a escolha, persiste, dispara o reconcile
  (`/provision`) e encadeia automaticamente o `/kiro-login` equivalente
  — ver seção dedicada abaixo. **Exige credencial válida do próprio
  `owner_id` da URL** (cookie ou `Authorization: Bearer`, ver seção
  "Autenticação dos próprios endpoints" abaixo).
- `POST /devs/{owner_id}/session` -- reemite o token de sessão do
  dashboard sob demanda (sessão expirou). **Exige credencial válida do
  próprio `owner_id`.** `GET /devs/{owner_id}/open` -- mesma emissão,
  mas com redirect 302 (ver seção dedicada abaixo) -- **não coberto por
  este endurecimento ainda, decisão registrada como gap remanescente na
  seção "Autenticação dos próprios endpoints"**.
- `POST /devs/{owner_id}/kiro-login?mode=org|personal` -- dispara o
  `kiro-cli login --use-device-flow` (Identity Center via `mode=org`, ou
  Builder ID via `mode=personal`) dentro do pod do dev, automatizando a
  técnica pty+FIFO que era feita à mão (ver seção dedicada abaixo).
  `mode` é obrigatório, sem default implícito. **Exige credencial válida
  do próprio `owner_id`.**
- `GET /healthz` -- sem auth (health check). `GET /devs` -- lista geral,
  **deliberadamente deixada aberta por enquanto** (decisão registrada na
  seção "Autenticação dos próprios endpoints"). `GET /devs/{owner_id}`
  -- consulta pontual, **também não coberta por este endurecimento
  ainda** (mesma seção).

## Por que `owner_id` não precisa ser um claim OIDC "de verdade"

Achado de arquitetura (investigação de código real do `kirocrew`,
documentado em detalhe no README do GitOps): `KIROCREW_OWNER_ID` é uma
credencial de integração do Slack (gate de DM), não uma identidade de
workspace — `kirocrew` nunca valida esse valor contra nada. Então
`owner_id` aqui só precisa ser um identificador ESTÁVEL o suficiente pra
nomear recursos; vira um slug DNS-1123-safe (`app/k8s_templates.py::slugify`).

## Rodando local

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 9100
```

Config (todas opcionais, têm default seguro pra este cluster — exceto as
`KREWHUB_OIDC_*`, que ficam vazias de propósito nesta fatia):

| Env var | Default |
|---|---|
| `KREWHUB_KUBECONFIG` | `~/.kube/config-personal` |
| `KREWHUB_K8S_CONTEXT` | vazio -- usa o `current-context` já ativo no kubeconfig; só importa no fallback local (o path in-cluster real nunca lê essa env var) |
| `KREWHUB_DEV_NAMESPACE` | `krewhub-devs` (namespace ÚNICO e compartilhado onde TODOS os pods de dev vivem -- ver seção "Namespace único compartilhado pra pods de dev" abaixo; criado declarativamente no GitOps, não pelo reconcile) |
| `KREWHUB_BASE_DOMAIN` | `kiro.internal` |
| `KREWHUB_PUBLIC_PORT` | `8080` (porta local do port-forward do CHP hoje) |
| `KREWHUB_DEV_POD_SCHEME` | `http` (setar `https` quando TLS termina na borda/Ingress/ALB e o backend interno é HTTP puro -- senão `KIROCREW_CORS_ORIGINS`/`dashboard_url_with_token` saem com scheme errado e o CSRF-origin check rejeita) |
| `KREWHUB_KIROCREW_IMAGE` | `ghcr.io/kirodotdev/kirocrew:0.6.0` |
| `KREWHUB_STORAGE_CLASS` | `rook-cephfs` |
| `KREWHUB_STORAGE_SIZE` | `10Gi` |
| `KREWHUB_CHP_NAMESPACE` | `krewhub` (placeholder genérico -- o deploy real sempre seta essa env var explicitamente, ver seção "Deploy no cluster") |
| `KREWHUB_CHP_ADMIN_PORT` | `8001` |
| `KREWHUB_DEV_POD_OVERLAY_PATH` | vazio -- path de um arquivo (YAML ou JSON) com overlay JSON Patch (RFC 6902) aplicado em cima do Pod/PVC genéricos de cada dev (ver seção "Overlay JSON Patch por-cluster" abaixo). Vazio = manifest 100% genérico, sem nenhuma restrição de nó/StorageClass fixa de cluster |\n| `KREWHUB_DEV_POD_OVERLAY_JSON` | vazio -- mesmo conteúdo do `_PATH` acima, mas inline (fallback pra dev local/smoke test); `_PATH` tem precedência se os dois vierem setados |\n| `KREWHUB_DB_PATH` | `./krewhub.db` |
| `KREWHUB_SESSION_TTL` | `24h` (passado a `kirocrew token --ttl`) |
| `KREWHUB_OIDC_ISSUER`/`_CLIENT_ID`/`_CLIENT_SECRET`/`_REDIRECT_URI`/`_SCOPES` | vazio (em cluster, vem do Secret `krewhub-oidc`, ver seção "Exchange OIDC real" abaixo) |
| `KREWHUB_SESSION_SECRET` | vazio -- segredo próprio do KrewHub pra assinar/validar o cookie/Bearer de sessão (ver seção "Autenticação dos próprios endpoints"); em cluster vem do Secret `krewhub-oidc`, chave `session-secret` |
| `KREWHUB_AUTH_TOKEN_TTL_SECONDS` | `86400` (24h) -- validade do cookie/token de sessão própria |
| `KREWHUB_KIRO_IDENTITY_PROVIDER` / `KREWHUB_KIRO_REGION` | vazio -- default só usado se a query (`mode=org`) não vier; sem nenhuma das duas fontes, 400 explícito |
| `KREWHUB_SELF_HOST` | vazio -- se setado, auto-registra a própria rota no CHP no startup (ver seção "Deploy no cluster") |
| `KREWHUB_SELF_PORT` | `8080` |

## Overlay JSON Patch por-cluster (nodeAffinity, tolerations, etc.)

**Achado de investigação anterior** (agente Kiro externo investigando
adoção do KrewHub num cluster EKS+Karpenter): `build_deployment` tinha um
`nodeAffinity` exigindo `node-role.kubernetes.io/control-plane`
**hardcoded direto no Python**, sem nenhuma env var nem via de
configuração -- decisão real deste homelab (o CSI do `rook-cephfs` só
roda nos nós `coruscant`/`tatooine`, ambos control-plane), mas impossível
de desligar/trocar sem editar `app/k8s_templates.py`. Diferente de
`KREWHUB_STORAGE_CLASS` (que já era configurável, só o *default* era
`rook-cephfs`), o `nodeAffinity` não tinha escape hatch nenhum.

**Mecanismo:** `app/overlay.py` aplica um overlay **JSON Patch (RFC
6902)**, via a lib `jsonpatch`, em cima do manifest genérico que
`build_pod`/`build_pvc` geram. Cada `build_*` monta o dict
genérico normalmente e devolve `apply_overlay(manifest,
load_overlay_ops(settings, "<recurso>"))` -- sem overlay configurado,
`apply_overlay` devolve o manifest intocado (100% genérico, roda em
qualquer cluster k8s, sem afinidade nem storageClass fixa nenhuma).

**BREAKING CHANGE (ver seção "Deployment vs Pod puro pro workload
por-dev"):** a chave de topo do workload por-dev era `deployment:` até
esta fatia (quando `build_deployment` gerava um `Deployment`) -- agora
é `pod:` (`build_pod` gera um `Pod` puro), e os paths RFC 6902 mudaram
de `/spec/template/spec/...` pra `/spec/...` (Pod não tem o wrapper
PodTemplateSpec que Deployment tinha). Um overlay antigo com
`deployment:` contra o código novo é **ignorado em silêncio** -- o Pod
sobe sem a afinidade/tolerations configuradas, sem erro nenhum. Quem
tiver um overlay próprio com a chave antiga precisa migrar pra `pod:` +
ajustar os paths ao atualizar pra esta versão.

**Por que JSON Patch e não strategic-merge-patch nem JSON Merge Patch
(RFC 7396):** não existe lib Python madura que replique client-side o
algoritmo de merge do k8s (que entende `containers`/`volumes` por
`name`, mas *não* tem merge-key nenhuma pra `tolerations` -- reimplementar
isso à mão é reinventar uma peça não-trivial do apimachinery). JSON Merge
Patch é simples mas substitui QUALQUER lista por inteiro (um overlay de
`volumes` apagaria o volume do workspace PVC se não o repetisse por
inteiro). JSON Patch é mais verboso mas cada operação é explícita
(`add`/`remove`/`replace` com `path`) e nunca apaga o que não foi pedido
-- ver docstring completa em `app/overlay.py`.

**Onde configurar:** `KREWHUB_DEV_POD_OVERLAY_PATH` (path de um arquivo
YAML/JSON, tipicamente montado via ConfigMap gerenciado fora do chart
Helm genérico -- mesmo padrão já usado pros outros recursos por-dev) ou
`KREWHUB_DEV_POD_OVERLAY_JSON` (conteúdo inline, fallback pra dev
local/smoke test). O arquivo/conteúdo é um dict `{recurso: [operações]}`,
uma chave por `build_*` que suporta overlay hoje (`pod`, `pvc`):

```yaml
# Equivalente exato ao nodeAffinity que antes estava hardcoded em
# build_deployment (hoje build_pod) -- é o overlay real usado no
# <cluster-homelab> (ver
# clusters/<cluster>/<namespace-real>/krewhub-central/dev-pod-overlay-configmap.yaml
# no repo GitOps). Path relativo a /spec direto -- Pod não tem o
# wrapper PodTemplateSpec que Deployment tinha.
pod:
  - op: add
    path: /spec/affinity
    value:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: node-role.kubernetes.io/control-plane
                  operator: Exists
pvc: []
```

**Limitação documentada pra `pvc`:** a spec de um PVC é majoritariamente
imutável após a criação (só `resources.requests.storage` pode crescer) --
um overlay que mude `storageClassName`/`accessModes` só pega em PVCs
criados DEPOIS da mudança de overlay; num PVC já existente o apiserver
rejeita o patch (422). Comportamento nativo do k8s, não deste mecanismo.

## Testes automatizados (suíte rápida, offline -- 135 testes, ~2.8s)

Suíte de **regressão pra rodar antes de cada deploy** -- diferente do
smoke-test manual documentado nas seções abaixo ("Provado ao vivo" e as
demais, sempre marcadas "testado ao vivo"), que segue existindo como
procedimento manual contra o cluster real com um owner descartável. Esta
suíte NUNCA toca o cluster real nem a rede real: todo `kubectl exec`
(`session_client`/`kiro_login`/`chp_client`) e todo client k8s
(`k8s_manager.get_clients`/`reconcile_dev`/etc.) são mockados
(`unittest.mock`/`monkeypatch`); OIDC (`urllib.request.urlopen`) também é
mockado. Só SQLite roda de verdade, sempre num arquivo `tmp_path` por
teste -- rápido e sem estado compartilhado entre testes.

```bash
uv run pytest              # roda tudo
uv run pytest -k lobby      # só os testes que batem "lobby" (pytest -k)
uv run pytest -x -v         # para no primeiro erro, verboso
```

Gerenciador de pacotes é `uv` (`pyproject.toml` + `uv.lock`, commitado).
`uv run` sincroniza o `.venv/` a partir do lock sozinho antes de rodar
qualquer comando -- sem script wrapper (não existe mais `test.sh`;
`uv run pytest` já resolve interpretador + deps + venv, idempotente/sem
custo real quando nada mudou, `python3` não precisa estar no PATH). Deps
de dev (`pytest`, `httpx2`) ficam no grupo `dev` de
`[dependency-groups]` -- só teste, não vão pra imagem (o `Dockerfile`
roda `uv sync --frozen --no-dev`).

**Cobertura, por módulo** (`tests/`):

| Arquivo | Cobre |
|---|---|
| `test_auth_tokens.py` | `sign_session`/`verify_session` -- válido, expirado, malformado, assinatura adulterada, secret errado, payload forjado |
| `test_oidc.py` | discovery (mock do `.well-known`), `build_authorization_url` (PKCE S256, state, client_id, redirect_uri), `exchange_code` (claim `email`/fallback `sub`) |
| `test_root.py` | `GET /` -- redirect pro lobby do PRÓPRIO owner (nunca de query), `Cache-Control: no-store`, sem loop com `/login`/`/callback` |
| `test_lobby.py` | form vs pular direto pro resultado (`login_mode` salvo), `?reconfigure=1`, validação de `POST` (`mode`/`identity_provider`/`region`, precedência form > env var), os 3 links da página de resultado |
| `test_provision_session_open.py` | `/provision` idempotente, `/session` 404-se-nunca-provisionado, `/open` redirect com token, `require_owner` (401/403) |
| `test_kiro_login.py` | validação de `mode`/`identity_provider`/`region` (precedência query > env var), idempotência (`already_logged_in`), + unit tests de `kiro_login.py` (guard clauses, parsing de código/URL, comando `org` vs `personal`) |
| `test_close_logout.py` | `/close` (revoga, mantém cookie do KrewHub) vs `/logout` (revoga + limpa cookie + redirect), melhor-esforço quando a revogação falha |
| `test_chp_client.py` | header `Authorization: token <valor>` (não `Bearer`), payload/host do registro de rota |
| `test_session_client.py` | `kirocrew token`/`kirocrew logout` via exec, filtro por slug (nunca vaza pro pod de outro dev) |
| `test_k8s_manager.py` | `reconcile_dev` idempotente (create na 1a chamada, patch na 2a, nomes deterministicos), namespace compartilhado |
| `test_k8s_templates.py` | `slugify` determinístico, nomeação de recursos por slug, `podSelector` da NetworkPolicy escopado ao dev certo |

**Achado ao escrever a suíte (não corrigido às cegas -- documentado
como é):** `POST /lobby` com `login_mode` OMITIDO por completo devolve
**422** (validação do próprio FastAPI, campo `Form(...)` obrigatório,
nunca chega no handler), não 400 -- diferente de `POST /kiro-login`,
onde `mode` é `Query(None, ...)` (opcional pro FastAPI, checado à mão no
handler), então lá "ausente" e "inválido" dão os dois 400. `login_mode`
PRESENTE com valor inválido (ex. `bogus`) continua 400 nos dois casos --
só o caso "campo inteiramente ausente" diverge entre os dois endpoints.
Comportamento pré-existente (não introduzido nesta fatia), documentado
e coberto por teste (`test_post_lobby_without_login_mode_is_422`), não
alterado -- mudar o tipo do parâmetro pra "consertar" isso é uma decisão
de API que cabe perguntar antes, não presumir.

## Provado ao vivo

```
POST /devs/e2e-test%40krewhub.local.test/provision?wait=true
→ cria kiro-dev-e2e-test-<namespace-real>-local-test do zero (7 recursos), pod Ready
→ registra e2e-test-<namespace-real>-local-test.kiro.internal no CHP
→ curl -H "Host: e2e-test-<namespace-real>-local-test.kiro.internal:8080" http://localhost:8080/api/health
  → {"ok": true}
```

Rodar de novo com o mesmo `owner_id`: os 7 passos voltam `"updated"` em
vez de duplicar — idempotência real, testada, não só declarada.

## Exchange OIDC real (Decisão #3 -- fechada)

Deliberadamente adiada em fatias anteriores ("fica pra depois, precisa
de client_id/secret reais"). Fechada nesta fatia contra um IdP real:
Keycloak de um IdP real de terceiro (`https://auth.devops.example.internal/auth/realms/master`),
client confidencial `krewhub` registrado pelo Lucas, `client_secret`
aplicado via `kubectl` direto num Secret `krewhub-oidc` no namespace
`<namespace-real>` (chaves `client-id`/`client-secret`/`issuer`/`redirect-uri` --
**nunca versionado em git**, mesma exceção já usada pro `imagePullSecret`
`ghcr-pull`; só a REFERÊNCIA ao Secret é git-tracked no
`deployment.yaml`). `KREWHUB_OIDC_SCOPES` fica de fora de propósito --
usa o default do `config.py` (`openid email profile`).

Testado ao vivo, nesta ordem:

1. **Discovery** -- `issuer/.well-known/openid-configuration` resolve os
   4 endpoints esperados (`authorization_endpoint`, `token_endpoint`,
   `jwks_uri`, `userinfo_endpoint`) contra o Keycloak real.
2. **`GET /login`** -- devolve 302 de verdade pro `authorization_endpoint`
   do Keycloak, com `client_id=krewhub`, o `redirect_uri` correto
   (`http://krewhub.kiro.internal:8080/callback`, dentro do wildcard
   `http://krewhub.kiro.internal:8080/*` cadastrado no client) e o PKCE
   challenge (`S256`). Confirmado via `python3 -c
   "urllib.request.urlopen(...)"` dentro do próprio pod -- a resposta do
   passo seguinte (`GET /devs/.../lobby` protegido, sem credencial,
   Accept: text/html) segue o 302 até o `/login`, que por sua vez segue
   até a tela de login real do Keycloak (`<html class="login-pf">`,
   `200`) -- cadeia completa confirmada ponta a ponta até o IdP.
3. **`GET /callback`** -- recebe `code`+`state` reais do IdP (não mais
   query params simulados de uma fatia anterior, que nunca funcionariam
   contra um IdP de verdade -- ver commit `9700b82`).

**Exchange completo confirmado ao vivo** -- o Lucas completou o login
de verdade no navegador (`http://krewhub.kiro.internal:8080/login`)
enquanto a proteção dos endpoints (seção seguinte) estava sendo
implementada em paralelo, e a cadeia inteira fechou com identidade real
de IdP pela primeira vez:

```
GET  /login                                          -> 302 pro Keycloak
GET  /callback?state=...&code=...&session_state=...  -> 302 pro lobby (exchange ok)
   owner_id resolvido do claim: lucas.ces@minha-org.com.br
GET  /devs/lucas.ces%40minha-org.com.br/lobby     -> 200 (cookie recem-setado ja validado)
POST /devs/lucas.ces%40minha-org.com.br/lobby     -> 200 (mesmo cookie, form submetido)
   reconcile real: namespace krewhub-devs, 6 recursos criados, pod kirocrew Ready
   rota registrada no CHP: lucas-ces-minha-org-com-br.kiro.internal
```

Ou seja: não foi só o exchange que funcionou -- o cookie setado pelo
`/callback` foi aceito de imediato pelos dois endpoints protegidos
seguintes (`GET`/`POST /lobby`) na mesma navegação, e o reconcile
disparado por eles proveu um pod `kirocrew` de verdade (`1/1 Running`)
pra essa identidade. Ciclo `login -> lobby -> escolhas -> pod pronto`
fechado ponta a ponta com IdP real, não simulado.

## Autenticação dos próprios endpoints (fecha o GAP REAL registrado antes)

Até esta fatia, qualquer requisição que alcançasse o CHP podia
reconciliar/reemitir sessão pra **qualquer** `owner_id`, sem nenhuma
verificação -- o `/callback` resolvia identidade, mas nada impedia
pular direto pro `/lobby`/`/provision` com um `owner_id` arbitrário via
curl. Fechado nesta fatia:

**Mecanismo -- token interno assinado, não o access_token do Keycloak**
(`app/auth.py`, HMAC-SHA256 sobre `{"owner_id", "exp"}`). Decisão
documentada (as duas eram válidas): validar o access_token do IdP
direto via JWKS (resolvido pelo discovery já implementado) também
funcionaria, mas um token próprio evita (a) depender da rede até o IdP
em toda request protegida -- não só login/callback -- e essa
dependência já mostrou ter custo real neste ambiente (erro de rede
batendo no proxy do CHP na mesma linha de trabalho desta fatia); e (b)
espalhar o access_token real do Keycloak, que carrega escopo/permissões
do IdP e não só identidade, por mais lugares (cookie de browser) do que
o necessário -- o KrewHub só precisa saber "quem é" o dev.

- `GET /callback`, em caso de sucesso, seta um cookie `HttpOnly`
  `krewhub_session` (`SameSite=Lax`, `max-age` = `KREWHUB_AUTH_TOKEN_TTL_SECONDS`,
  24h por default) antes do redirect pro lobby. `Secure` é setado
  **condicionalmente** (`True` só se a conexão até aqui foi `https://`
  ou veio `X-Forwarded-Proto: https`) -- hoje o CHP fala HTTP puro com o
  `krewhub-central` (ver "Deploy no cluster"); `Secure` incondicional
  faria o browser **descartar o cookie em silêncio** numa conexão
  `http://` e quebrar o login inteiro sem nenhum erro visível. Decisão
  documentada, não omitida: quando o CHP ganhar TLS (fora de escopo
  hoje), isso vira `Secure` de verdade sem mudança de código.
- Endpoints protegidos, todos exigindo credencial do **mesmo**
  `owner_id` da URL: `GET`/`POST /devs/{owner_id}/lobby`,
  `POST /devs/{owner_id}/provision`, `POST /devs/{owner_id}/session`,
  `POST /devs/{owner_id}/kiro-login`. Aceitam o cookie **OU** um header
  `Authorization: Bearer <token>` (mesma verificação) -- alternativa
  pensada pra chamada programática (curl/script) sem depender de cookie
  de browser.
  - Sem credencial: `401` JSON (`GET /healthz`-style chamada
    programática) ou `302` pro `/login` se a request tiver
    `Accept: text/html` (navegação de browser de verdade).
  - Credencial inválida/expirada/adulterada: mesma régua (`401`/`302`).
  - Credencial válida mas de **outro** `owner_id` que não o da URL:
    `403` explícito -- é isso que fecha o buraco real (antes dava pra
    chamar `/devs/QUALQUER-EMAIL/provision` sem ser aquele dev).
- **`GET /devs`** (lista geral, sem `owner_id` na URL) -- decisão
  registrada, não ambígua: **deixada aberta por enquanto**, sem exigir
  token de dev nenhum. Motivo: hoje só o Lucas opera o serviço (mesma
  premissa já usada alhures neste README), e é uma listagem, não uma
  ação sobre um dev específico. Vira bloqueio real assim que houver mais
  de um operador ou o serviço for exposto além da rede do operador --
  registrado aqui como follow-up, não como esquecimento.
- **`GET /devs/{owner_id}` (consulta pontual) e `GET /devs/{owner_id}/open`**
  (emite e redireciona com um token de dashboard) -- **NÃO cobertos por
  este endurecimento nesta fatia** (o pedido original listava só
  `lobby`/`provision`/`session`/`kiro-login`). Gap remanescente
  registrado explicitamente, não escondido: `/open` em particular é tão
  sensível quanto `/session` (emite token de dashboard pra qualquer
  `owner_id` informado) e deveria ganhar a mesma proteção numa próxima
  fatia.

**Testado ao vivo** (dentro do próprio pod em cluster, contra o
`KREWHUB_SESSION_SECRET` real, tokens assinados com `app.auth.sign_session`
pra dois `owner_id` de teste `dev-a@test.local`/`dev-b@test.local`):

```
POST /devs/dev-a/provision  sem credencial                          -> 401 JSON
GET  /devs/dev-a/lobby      sem credencial, Accept: text/html        -> segue 302 até a tela real de login do Keycloak
POST /devs/dev-a/provision  Bearer <token de dev-a adulterado>       -> 401 "assinatura invalida"
POST /devs/dev-b/provision  Bearer <token de dev-a> (cross-owner)    -> 403 "pertence a 'dev-a@test.local', nao a 'dev-b@test.local'"
GET  /devs/dev-a/lobby      cookie krewhub_session=<token de dev-a>  -> 200 (passa da auth, serve o form)
POST /devs/dev-b/kiro-login cookie krewhub_session=<token de dev-b>  -> 400 "mode obrigatorio" (passa da auth, cai na validação seguinte)
GET  /devs                  sem credencial                          -> 200 (continua aberto, decisão acima)
```

Os dois sentidos que importam (401 sem/errada credencial, 403
cross-owner, sucesso com credencial própria) foram confirmados de
verdade, não presumidos.

## `GET /` (raiz de `krewhub.kiro.internal`) -- entrypoint que decide sozinho login vs lobby

Antes desta fatia, `krewhub.kiro.internal:8080/` sem path nenhum não
tinha handler -- 404 puro, sem nenhuma pista de pra onde ir. Esta fatia
implementa a rota raiz como entrypoint natural: decide sozinha se quem
chegou já tem sessão (volta pro lobby, sem precisar clicar em `/login`
de novo) ou não (manda pro fluxo OIDC).

**Comportamento:**
- **Sessão válida** (cookie `krewhub_session` com assinatura HMAC e
  `exp` OK) -> `302` pro `/devs/{owner_id}/lobby` do **próprio** owner,
  `owner_id` extraído do cookie -- nunca de query param, nunca de outra
  fonte.
- **Sem sessão válida** (sem cookie, expirado, assinatura inválida ou
  payload malformado -- qualquer `AuthTokenError`) -> `302` pro
  `/login`. **Nunca** um 401/500 cru aqui, nem pra chamada programática
  sem `Accept: text/html` -- diferente de `require_owner` (que devolve
  401 JSON pra script/curl sem esse header), a raiz sempre redireciona,
  pros dois casos, porque não há um "owner_id da URL" pra comparar --
  só existe "tem sessão" ou "não tem".
- **Sem lógica de validação duplicada:** reaproveita `auth.verify_session`
  (mesma função que já protege `/provision`, `/lobby`, `/session`,
  `/kiro-login` via `require_owner`) e o mesmo `_extract_token` (cookie
  OU `Authorization: Bearer`). O handler da raiz só decide o `Response`
  (redirect) a partir do retorno dessa MESMA verificação -- nenhuma
  segunda implementação de HMAC/expiração.
- **`Cache-Control: no-store`** na resposta -- o destino do redirect
  depende de QUEM está pedindo (sessão própria), então não pode ficar
  cacheado nem no navegador nem no CHP (que faz proxy puro, sem cache
  próprio hoje, mas a garantia fica explícita no header mesmo assim).
- **Sem risco de loop:** `/login` sempre redireciona PRA FRENTE (pro
  `authorization_endpoint` do IdP) e `/callback` sempre redireciona PRA
  FRENTE (pro lobby) -- nenhum dos dois olha pra `/` nem verifica sessão
  antes de redirecionar, então não há caminho de volta pra raiz que
  fechasse um loop com ela.

**Testado ao vivo, através do mesmo túnel de sempre (port-forward no
Service do CHP + `Host:` header), depois de buildar/pushar
`ghcr.io/lucasces/krewhub-central:sha-7e58f53` e reconciliar via Flux
(`flux reconcile source git flux-system` + `flux reconcile kustomization
flux-system` -- ver "Deploy no cluster" pra explicação de por que
`krewhub-central/` reconcilia pela Kustomization raiz `flux-system`, não
por uma própria: os manifests desse diretório não têm uma
`<namespace-real>-krewhub-central.yaml` dedicada como `chp/`/`dev-testdev/` têm,
então quem aplica é o `flux-system` recursivo em `./clusters/<cluster>`
-- confirmado pelas labels `kustomize.toolkit.fluxcd.io/name: flux-system`
no Deployment já rodando antes desta fatia):**

```
curl -H 'Host: krewhub.kiro.internal' http://127.0.0.1:8080/
  (sem cookie)
  -> 302, Location: /login, Cache-Control: no-store

curl -H 'Host: krewhub.kiro.internal' -b 'krewhub_session=<token valido, owner root-route-test@krewhub.local.test>' http://127.0.0.1:8080/
  -> 302, Location: /devs/root-route-test%40krewhub.local.test/lobby, Cache-Control: no-store

curl -H 'Host: krewhub.kiro.internal' -b 'krewhub_session=garbage-not-a-token' http://127.0.0.1:8080/
  -> 302, Location: /login (malformado -- nao 401/500)

curl -H 'Host: krewhub.kiro.internal' -b 'krewhub_session=<payload real>.<assinatura adulterada>' http://127.0.0.1:8080/
  -> 302, Location: /login (assinatura invalida -- nao 401/500)
```

Token de teste gerado localmente com `app.auth.sign_session` usando o
`KREWHUB_SESSION_SECRET` real do cluster (lido do Secret `krewhub-oidc`,
nunca hardcoded) -- mesmo padrão de teste isolado já usado pros outros
endpoints protegidos (owner `*@krewhub.local.test`, nunca um owner real).

## Sessão automática do dashboard (token do `kirocrew`)

Antes desta fatia, "cair logado" no dashboard exigia um passo manual:
`kubectl exec ... kirocrew token` + colar a URL à mão. Isso foi
automatizado (`app/session_client.py`):

- `POST /devs/{owner_id}/provision` (depois de confirmar o pod Ready)
  já roda `kirocrew token --ttl {KREWHUB_SESSION_TTL}` dentro do pod via
  `exec` do client Python do k8s (não precisa de pty — `kirocrew token`
  não é interativo, diferente do `kiro-cli login`), extrai o `?token=...`
  da saída e devolve a URL pública já corrigida (host/porta do CHP, não
  o `localhost:5476` interno que o comando imprime por padrão) no campo
  `dashboard_url_with_token` da resposta JSON.
- `POST /devs/{owner_id}/session` — reemissão sob demanda (sessão
  expirou, precisa de link novo). Só exige que o `owner_id` já tenha
  sido provisionado antes (404 se não), não que `/provision` tenha
  rodado nesta execução do processo.
- `GET /devs/{owner_id}/open` — mesma emissão, mas devolve **HTTP 302**
  direto pra URL com token, pra abrir direto no navegador e sentir o
  "login completou, caiu no dashboard" sem nenhum JSON no meio.

**Decisão de formato (por que dois formatos em vez de escolher um só):**
`/provision` e `/session` devolvem o campo `dashboard_url_with_token`
em JSON, não um redirect — `/provision` é uma chamada de
infraestrutura idempotente pensada pra script/CI, que devolve o estado
inteiro do reconcile; misturar um 302 nela quebraria isso pra qualquer
client HTTP que não seja um navegador (curl, CI, etc. teriam que seguir
redirect só pra ler o corpo de status). Quem quer a *sensação* de login
de verdade (era o objetivo desta fatia) usa `GET /devs/{owner_id}/open`,
que existe só pra isso.

**Persistência:** o SQLite (`store.mark_token_issued`) guarda só
`last_token_issued_at` (timestamp) por `owner_id` — o token em si
**nunca** é persistido (é credencial de sessão; a única cópia que
existe fora do processo em memória é a URL devolvida na resposta HTTP,
uma vez).

**Testado ao vivo, através do CHP (não só direto no serviço local):**
```
POST /devs/e2e-test%40krewhub.local.test/session
→ {"dashboard_url_with_token": "http://e2e-test-<namespace-real>-local-test.kiro.internal:8080/?token=..."}

curl -i -H "Host: e2e-test-<namespace-real>-local-test.kiro.internal:8080" \
  "http://localhost:8080/?token=..."
→ HTTP/1.1 200 OK
→ Set-Cookie: mc_token_8080=...; HttpOnly; Max-Age=71955; Path=/; SameSite=Lax
→ Set-Cookie: mc_refresh_8080=...; HttpOnly; Path=/api/auth; SameSite=Lax
→ <!DOCTYPE html> ... (dashboard completo)
```
Fecha o fluxo "provision (ou reemissão) → já cai logado", sem nenhum
`kubectl exec` manual no meio, validado através do proxy de verdade
(porta 8080 local, Host header do jeito que o navegador manda), não só
chamando o k8s API direto.

## `kiro-cli login` automatizado (device-flow, org e personal)

Antes desta fatia, destravar a tela "Sandbox unavailable"/"Sign in to
Kiro" exigia `kubectl exec` manual rodando a técnica pty+FIFO documentada
no README do GitOps
(`clusters/<cluster>/<namespace-real>/README.md`, seção "kiro-cli login").
`app/kiro_login.py` automatiza exatamente essa técnica, cobrindo os dois
caminhos já vistos manualmente:

- **`mode=org`** -- `kiro-cli login --use-device-flow --license pro
  --identity-provider <start-url> --region <region>` (Identity Center
  corporativo). Pede confirmação (Enter) de dois prompts pré-preenchidos:
  "Enter Start URL", depois "Enter Region".
- **`mode=personal`** -- `kiro-cli login --use-device-flow` (sem
  `--license`/`--identity-provider`/`--region`). Mostra um menu de seleção
  (`? Select login method`: Builder ID / Google / GitHub / Your
  Organization) com "Use with Builder ID" já destacado por default --
  confirmado ao vivo que um único Enter aceita esse default. Não
  navegamos o menu pra Google/GitHub nesta fatia (exigiria mais
  Enters/setas -- fora de escopo).

Em ambos os casos a saída final é o mesmo formato (`Code: XXXX-XXXX` /
`Open this URL: <url>`), então o parsing (`_CODE_RE`/`_URL_RE`) é
compartilhado entre os dois modos.

- **`mode` é obrigatório, sem default implícito:** `POST
  /devs/{owner_id}/kiro-login` sem `mode`, ou com um valor que não seja
  `org`/`personal`, devolve **400** explicando o motivo -- mesma régua já
  seguida pro OIDC genérico (nenhum modo assumido silenciosamente).
- **`identity_provider`/`region` resolvidos em cascata, só pra
  `mode=org`:** 1) query param, se vier; 2) senão,
  `KREWHUB_KIRO_IDENTITY_PROVIDER`/`KREWHUB_KIRO_REGION`; 3) se nenhuma
  das duas fontes tiver valor, **400** explícito (nenhuma organização é
  assumida como default). `mode=personal` ignora os dois parâmetros por
  completo.
- **Por que precisa de pty (nos dois modos):** o wizard do `kiro-cli`
  espera confirmação interativa mesmo com as flags relevantes já
  preenchidas -- sem TTY real (`kubectl exec` comum), ele descarta o
  valor da flag (`region: must be a valid host label`) ou nem chega a
  mostrar o menu de seleção. A solução é um script Python rodado
  **dentro do pod** que cria seu próprio pty (`pty.spawn`) e lê stdin de
  uma FIFO (aberta em modo RDWR pra não bloquear o lançamento), tudo
  `setsid`-destacado da sessão do nosso `exec` -- sobrevive à desconexão
  enquanto o device-flow faz polling esperando o clique do dev.
- **Fire-and-forget:** `start_device_flow()` só dispara o processo,
  confirma os prompts do modo escolhido via poll no log (não sleeps
  fixos -- espera até 15s por estágio, checando a cada 0.5s) e devolve
  `verification_url`/`user_code` assim que aparecem. NÃO espera o
  clique -- o processo de polling continua rodando no pod depois que o
  endpoint responde.
- **Idempotência independe do modo:** antes de disparar qualquer coisa,
  roda `kiro-cli whoami` (não-interativo, não precisa de pty). Se já há
  sessão válida, devolve `{"already_logged_in": true, "whoami": "..."}`
  sem tocar em nada -- testado ao vivo nos dois modos contra um pod já
  logado.

**Testado ao vivo contra o pod `e2e-test`** (nunca tinha feito login --
confirmado antes via `kirocrew doctor`: `kiro login: not logged in`):

```
POST /devs/{owner_id}/kiro-login                          -> 400 (mode ausente)
POST /devs/{owner_id}/kiro-login?mode=bogus                -> 400 (mode inválido)
POST /devs/{owner_id}/kiro-login?mode=org                  -> 400 (sem identity_provider/region, sem env var)

POST /devs/e2e-test%40krewhub.local.test/kiro-login
     ?mode=org&identity_provider=https://minha-org.awsapps.com/start&region=us-east-1
→ (≈4.1s) {"mode": "org", "already_logged_in": false,
           "verification_url": "https://minha-org.awsapps.com/start/#/device?user_code=DSTF-VQPX",
           "user_code": "DSTF-VQPX"}

POST /devs/e2e-test%40krewhub.local.test/kiro-login?mode=personal
→ (≈2.9s) {"mode": "personal", "already_logged_in": false,
           "verification_url": "https://view.awsapps.com/start/#/device?user_code=WNZW-NSBR",
           "user_code": "WNZW-NSBR"}
```

Confirmei no pod, a cada chamada, um único processo de polling rodando
(sem duplicata) e `kiro-cli whoami` ainda `Not logged in` esperando o
clique. Testei idempotência (nos dois modos) inserindo um registro
temporário no store apontando pro pod `kiro-dev-testdev` (já logado de
uma fatia anterior) -- ambas as chamadas devolveram `already_logged_in:
true` com o `whoami` real, sem disparar nada; removi o registro temporário
depois.

**Observação operacional herdada da técnica manual (não é bug novo, é
comportamento pré-existente do `kiro-cli`, nos dois modos):** enquanto o
device-flow não é confirmado no navegador, o processo de polling fica
reimprimindo o spinner (`Logging in...`) no log
(`/tmp/kiro_login_out.log`, dentro do container, `emptyDir` local do nó)
indefinidamente -- em um teste manual isso gerou ~87KB em menos de um
minuto. Se um dev nunca completa o login, esse arquivo cresce sem limite
até o pod ser reciclado. Não é uma regressão desta automação -- só
documentando o fato pra não ser surpresa depois.

## Lobby de customização de sessão (login -> escolhas -> pod pronto)

Antes desta fatia, o fluxo pulava direto do login pro reconcile, e pra
completar o login do `kiro-cli` era preciso eu chamar `/kiro-login`
manualmente depois com `mode`/`identity_provider`/`region` certos.
`GET`/`POST /devs/{owner_id}/lobby` fecha esse ciclo:

- **`GET /devs/{owner_id}/lobby`** -- HTML puro (sem JS, sem
  Jinja2/framework -- string template em `_lobby_form_html`) com um
  radio `login_mode` (`org` = Pro/organização, `personal` = Individual/
  Builder ID -- nenhum pré-selecionado, forçando escolha explícita) e
  dois campos de texto (`identity_provider`, `region`) só relevantes se
  `org` for escolhido. Os campos vêm pré-populados com
  `KREWHUB_KIRO_IDENTITY_PROVIDER`/`KREWHUB_KIRO_REGION` só pra poupar
  digitação -- **totalmente sobrescrevíveis**, nenhuma organização é
  imposta (não há lista de "organizações conhecidas", é texto livre).
- **`POST /devs/{owner_id}/lobby`** (mesmo path, form
  `application/x-www-form-urlencoded`):
  1. Valida `login_mode` (400 se ausente/inválido -- mesma régua do
     `/kiro-login`: nenhum modo default).
  2. Se `login_mode=org`, resolve `identity_provider`/`region` (form >
     env var > 400 explícito se nenhuma fonte tiver valor).
  3. **Persiste a escolha** via `store.set_login_choice` (colunas novas
     `login_mode`/`login_identity_provider`/`login_region` na tabela
     `devs`) -- migração leve (`ALTER TABLE ADD COLUMN`), mesmo padrão já
     usado pra `last_token_issued_at`.
  4. Chama `_do_provision(owner_id)` (função extraída do endpoint
     `/provision` original -- mesmo reconcile idempotente, mesma emissão
     de token do dashboard, reaproveitado sem duplicar código).
  5. Assim que o pod está Ready, **encadeia automaticamente**
     `kiro_login.start_device_flow(...)` com os MESMOS valores que o dev
     escolheu no form -- sem uma segunda chamada manual minha.
  6. Devolve uma página HTML só, com os dois links prontos: URL do
     dashboard já autenticada (`dashboard_url_with_token`) e URL+código
     do device-flow do `kiro-cli` (ou aviso de "já estava logado", se
     for o caso).
- **`GET /callback` (OIDC) agora redireciona pro lobby** em vez de
  devolver os claims crus -- fecha o ciclo `login -> lobby -> escolhas ->
  pod pronto` completo (exchange real ainda não testado ao vivo, mas o
  redirect já está no lugar certo pra quando estiver).

**UX da página de resultado (`_lobby_result_html`) -- 3 ajustes:**
1. **Ordem invertida**: o passo do login do `kiro-cli` (device-flow) vem
   **antes** do link do dashboard, não depois. Sem o login completo, o
   dashboard mostra a tela de "sandbox unavailable"/sign-in (achado já
   documentado) -- mandar clicar no link errado primeiro só confundia.
2. **Instruções numeradas explícitas** em vez de só o link cru ("1. Faça
   login no kiro-cli... 2. Depois de logado, acesse seu dashboard...").
   O cenário `already_logged_in` tem texto próprio ("Você já está logado
   -- pode pular direto pro passo 2") em vez de reaproveitar o texto de
   fluxo novo.
3. Os dois links abrem em aba nova (`target="_blank" rel="noopener"`) --
   não perde a página de instruções ao clicar.

**Testado ao vivo, ciclo completo, contra um dev novo (`lobby-test`):**

```
GET  /devs/lobby-test%40krewhub.local.test/lobby                       -> 200, form HTML
POST /devs/lobby-test%40krewhub.local.test/lobby (sem login_mode)      -> 400
POST ...                              (login_mode=bogus)               -> 400
POST ...                              (login_mode=org, sem provider/region) -> 400

POST /devs/lobby-test%40krewhub.local.test/lobby   login_mode=personal
-> 200, página HTML com:
   1. Dashboard do Kiro Crew
      http://lobby-test-<namespace-real>-local-test.kiro.internal:8080/?token=...
   2. Login do kiro-cli
      Abra https://view.awsapps.com/start/#/device?user_code=HVGC-ZDJH ...
```

Confirmei cada peça de verdade, não só a resposta da API:
- `curl -H "Host: lobby-test-<namespace-real>-local-test.kiro.internal:8080" http://localhost:8080/?token=...`
  através do CHP -> **200 OK** + `Set-Cookie: mc_token_8080=...` (sessão
  do dashboard real).
- Um único processo `kiro-cli login --use-device-flow` rodando no pod
  (sem duplicata), `kiro-cli whoami` ainda `Not logged in` (aguardando o
  clique, como esperado de fire-and-forget).
- `GET /devs/lobby-test%40krewhub.local.test` mostra
  `login_mode: "personal"` persistido (e `login_identity_provider`/
  `login_region` vazios, corretos pra esse modo).

**Achado operacional durante o teste (não é bug da fatia, é fato do
cluster):** o namespace novo (`kiro-dev-lobby-test-...`) ficou com o pod
**Pending** por alguns minutos -- `0/3 nodes are available: ... 2
Insufficient cpu`. O cluster `<cluster-homelab>` já está com 2 dos 3
nós em ~88-91% de CPU *requested* (acumulado de fatias anteriores: 3 pods
de teste + o `kirocrew` de produção + outros workloads do homelab).
Precisei escalar o `e2e-test` (disposable) a 0 réplicas temporariamente
pra liberar capacidade, deixar o `lobby-test` ficar Ready, e escalar o
`e2e-test` de volta -- que por sua vez ficou `Pending` (esperado, mesma
causa). Não é um bug de lógica desta fatia -- é o primeiro sinal concreto
de que **culling por inatividade (já listado como próxima fatia) deixou
de ser só "economia elegante" e virou pré-requisito real** pra rodar mais
de ~2 pods de dev simultâneos neste cluster do jeito que ele está
dimensionado hoje.

### `GET /lobby` pula o form quando o dev já foi provisionado antes (`?reconfigure=1` força de novo)

Até esta fatia, `GET /devs/{owner_id}/lobby` **sempre** mostrava o form
de escolha, mesmo pra um dev que já tinha passado pelo lobby antes (já
tinha `login_mode` salvo no SQLite de uma execução anterior) -- ou seja,
"voltar pro dashboard" depois de já ter configurado a sessão uma vez
exigia preencher o form de novo (com os MESMOS valores) só pra chegar
nos mesmos links.

**Comportamento novo:**
- **Primeira vez desse `owner_id`** (nenhum registro no SQLite, ou
  registro existe mas ficou só em `lobby_pending` -- sem `login_mode`
  salvo) -> `GET /lobby` continua mostrando o form, comportamento
  idêntico ao de antes.
- **Owner_id já provisionado antes** (`login_mode` salvo de um `POST
  /lobby` anterior) -> `GET /lobby` **pula o form** e vai direto pro que
  o `POST` já fazia: reconcile idempotente (`_do_provision`, rápido
  porque o pod já existe) + `kiro-login` idempotente
  (`kiro_login.start_device_flow`, não dispara device-flow novo se já
  há sessão) + a MESMA página final de resultado (os dois links).
- **`?reconfigure=1`** -- força mostrar o form de novo mesmo com
  `login_mode` já salvo, pra quem quer trocar de org/modo manualmente
  sem precisar apagar o registro no banco. A página de resultado ganhou
  um link discreto **"Reconfigurar sessão"** (`/devs/{owner_id}/lobby?reconfigure=1`)
  exatamente pra isso.

**Sem lógica duplicada:** o núcleo (reconcile + kiro-login + montar a
página de resultado) foi extraído pra `_run_lobby_session(owner_id, *,
login_mode, identity_provider, region)` -- tanto o `GET` (quando pula o
form, lendo os valores JÁ RESOLVIDOS direto do SQLite) quanto o `POST`
(form recém-submetido, valores resolvidos ali mesmo) chamam essa MESMA
função. `POST` continua sendo o único responsável por persistir a
escolha (`store.set_login_choice`) -- o `GET` só lê o que já está lá.

**Testado ao vivo, através do mesmo túnel de sempre (port-forward no
Service do CHP + `Host:` header), depois de buildar/pushar
`ghcr.io/lucasces/krewhub-central:sha-5a3188d` e reconciliar via Flux
(`flux reconcile source git flux-system` + `flux reconcile kustomization
flux-system`):**

```
(a) owner novo, sem registro (lobby-flow-new-test@krewhub.local.test)
GET /devs/lobby-flow-new-test%40krewhub.local.test/lobby
  -> 200, form HTML (radio login_mode, campos identity_provider/region)

(b) owner com registro existente (lucas.ces@minha-org.com.br,
    login_mode=org salvo de um provision real anterior)
GET /devs/lucas.ces%40minha-org.com.br/lobby
  -> 200, PULA o form, direto pra pagina de resultado:
     "Voce ja esta logado no kiro-cli" (kiro-login idempotente, nenhum
     device-flow novo disparado) + link do dashboard com token novo +
     link "Reconfigurar sessao"

(c) mesmo owner de (b), forcando o form de novo
GET /devs/lucas.ces%40minha-org.com.br/lobby?reconfigure=1
  -> 200, form HTML de novo (mesmo com login_mode ja salvo)
```

Os três casos confirmados de verdade via `curl` com cookie de sessão
válido (`krewhub_session`, gerado localmente com `app.auth.sign_session`
usando o `KREWHUB_SESSION_SECRET` real do cluster), não presumidos.

### `GET /close` vs `GET /logout` -- dois conceitos diferentes, investigados ANTES de implementar

Primeira versão de `/logout` (fatia anterior) só limpava o cookie do
KrewHub. Mudança de escopo: distinguir **fechar a sessão de trabalho do
dashboard** (`kirocrew`, dentro do pod) de **deslogar do KrewHub**
(cookie próprio) -- são coisas diferentes, e a investigação abaixo (feita
ANTES de escrever qualquer código, sem presumir nada) confirma por quê.

**1) Existe revogação real de sessão no `kirocrew`?** Sim -- `kirocrew
--help` lista `logout: Revoke all active dashboard sessions`,
não-interativo (`kirocrew logout --help` só tem `[-h] [--port PORT]`,
sem wizard/pty). Rastreado até o código-fonte vendorizado:
`kiro_crew/cli_server.py::_logout` faz um `POST
http://127.0.0.1:<port>/api/logout` **local ao pod**, autenticado com o
mesmo `X-Local-Secret` de arquivo que `kirocrew token` já usa (mesmo
padrão de `exec` sem pty que `session_client.issue_token_url` já
executa). O handler chama `revoke_all_sessions()`
(`kiro_crew/dashboard/token_auth.py`): limpa estado em memória e faz
`bump_revocation_gen()` -- um contador de geração **persistido em
disco** que tanto `mc_token_*` quanto `mc_refresh_*` checam na
validação. Cookies já emitidos (mesmo os que o navegador ainda tiver
guardado) passam a ser rejeitados no próximo request -- sem tocar no
navegador, sem reiniciar o processo nem o pod.

**2) Os cookies do dashboard são cross-origin com o KrewHub?** Sim --
confirmado ao vivo (`curl` pelo CHP, `Host:
<slug>.kiro.internal`): `Set-Cookie: mc_token_5476=...` e
`mc_refresh_5476=...` **sem atributo `Domain`** (cookie host-only, RFC
6265 -- escopado exatamente a `<slug>.kiro.internal`, sem
compartilhamento com `.kiro.internal`). O `krewhub-central` responde de
`krewhub.kiro.internal` -- host **diferente** -- então uma resposta dele
é estruturalmente incapaz de setar/expirar esses cookies no navegador
(cross-origin, o navegador nunca aceitaria). A única forma de "matar"
essa sessão é a invalidação SERVER-SIDE do achado 1 acima, nunca um
`Set-Cookie` de limpeza vindo do KrewHub.

**3) Logout OIDC no Keycloak?** `end_session_endpoint` existe no
discovery document
(`https://auth.devops.example.internal/auth/realms/master/protocol/openid-connect/logout`),
mas fechar esse ciclo direito exigiria persistir o `id_token` do
Keycloak (hoje descartado em `/callback` -- só `owner_id` é extraído,
`result["tokens"]` nunca é salvo) pra poder mandar `id_token_hint` --
mudança de design real, não uma continuação óbvia do "sessão é
credencial, não persistida" já seguido no resto do projeto. **Decisão
confirmada:** fora de escopo por enquanto -- nem `/close` nem `/logout`
chamam o Keycloak.

**Desenho final, depois da decisão:**

- **`GET /close`** (protegido, `require_session` -- owner_id vem SEMPRE
  do cookie, nunca de query param): revoga a sessão real do dashboard
  (`session_client.revoke_session`, achado 1) e devolve uma página HTML
  simples de confirmação com link de volta pro lobby. **NÃO** limpa o
  cookie do KrewHub, **NÃO** chama o Keycloak -- o dev continua logado
  no KrewHub, só a sessão de trabalho do dashboard é encerrada. Voltar
  pro lobby reautentica rápido (já tem `login_mode` salvo -- ver `GET
  /lobby` -- gera um `kirocrew token` novo, sem refazer OIDC).
- **`GET /logout`**: faz a MESMA revogação que `/close`
  (`_revoke_kirocrew_session`, núcleo único, reaproveitado -- não
  duplicado) **e** limpa o cookie `krewhub_session` **e** redireciona
  pro `/login`. A revogação do `kirocrew` é MELHOR ESFORÇO aqui -- se
  falhar (owner nunca provisionado, pod indisponível), o logout do
  KrewHub em si segue acontecendo do mesmo jeito, preservando a garantia
  já testada de `/logout` nunca vazar erro/500. Continua idempotente com
  cookie ausente/malformado (nesse caso, sem owner_id pra revogar,
  simplesmente pula direto pra limpar o cookie).
- **`require_session`** extraído de `require_owner` (que agora só
  acrescenta a checagem de cross-owner por cima) -- endpoints sem
  `owner_id` na URL (como `/close`) reaproveitam a MESMA verificação de
  cookie/token, sem duplicar lógica de validação.
- Página de resultado do lobby (`_lobby_result_html`) ganhou dois links
  distintos, com texto curto explicando a diferença: **"Fechar sessão"**
  (`/close`) e **"Sair"** (`/logout`), ao lado do "Reconfigurar sessão"
  já existente.

**Testado ao vivo, contra um owner DESCARTÁVEL primeiro** (achado 1 nunca
foi executado contra o pod real do `lucas.ces@minha-org.com.br` --
só depois de confirmar no descartável), via CHP:

```
Provisionado close-logout-test@krewhub.local.test, kirocrew token --ttl 30m
  -> cookie mc_token_5476 (gen=0) confirmado funcionando (Set-Cookie ao
     re-exchangear o ?token=)

GET /close   (cookie krewhub_session válido)
  -> 200, "Sessão de trabalho encerrada" -- SEM Set-Cookie de krewhub_session

Re-exchange do MESMO ?token= (gen=0) depois do /close
  -> 200, mas SEM Set-Cookie nenhum -- a sessão foi revogada de verdade
     (comparado com um novo `kirocrew token` logo em seguida, que gera
     gen=1 e ESSE sim seta cookie -- confirma que é revogação real, não
     bloqueio geral de acesso)

krewhub_session (mesmo cookie de antes do /close) contra GET /
  -> 302 pro lobby -- ainda logado no KrewHub, como esperado

GET /logout   (cookie krewhub_session válido, sessão do kirocrew ainda
               ativa em gen=1)
  -> 302, Location: /login
     Set-Cookie: krewhub_session=""; expires=<no passado>; HttpOnly; Max-Age=0; Path=/; SameSite=lax

Re-exchange do token gen=1 depois do /logout
  -> 200, SEM Set-Cookie -- revogado também (gen bumpou de novo)
```

Confirmado também: `kubectl get pod` mostrou `RESTARTS: 0` o tempo
inteiro -- as duas revogações NUNCA reiniciaram o pod nem o processo.
Owner descartável limpo depois (Deployment/Service/PVC/Secret/
ConfigMap/NetworkPolicy no `krewhub-devs`, rota no CHP via `DELETE
/api/routes/...`, linha no SQLite) -- nenhum lixo deixado no cluster.

**Achado que segue valendo, herdado da versão anterior de `/logout`:**
como o cookie `krewhub_session` em si (`app/auth.py`) é um HMAC
**stateless**, `/logout` limpa esse cookie do NAVEGADOR mas não tem como
"revogar" o token em si -- reenviar de propósito o cookie antigo do
KrewHub (não o do kirocrew, que agora TEM revogação real) continua
autenticando até `exp` natural. Isso é sobre o cookie do KrewHub, não
sobre a sessão do `kirocrew` -- a distinção entre os dois é exatamente o
que esta fatia resolve.

### Mudança de design (posterior): `/close`/`/logout` passam a desligar o workload k8s, não só revogar sessão

A investigação e o desenho documentados na seção acima ("dois conceitos
diferentes") continuam válidos para a revogação de sessão do `kirocrew`
em si -- mas a decisão final de escopo MUDOU depois: além de revogar a
sessão, `/close` e `/logout` agora também derrubam o workload k8s do dev
(`k8s_manager.teardown_dev_workload`, ver docstring lá) -- um culling
manual, por-dev, sob demanda. Motivo: "Fechar sessão" só invalidando um
token/cookie, sem afetar nenhum recurso k8s, ficava confuso -- parecia
que devia liberar recursos do cluster e não liberava nada.

Ordem de execução em `_close_dev_session` (`app/main.py`): 1) revoga a
sessão do `kirocrew` (melhor esforço, ver seção acima); 2) deleta
Deployment + Service + NetworkPolicy + ConfigMap do dev
(`teardown_dev_workload`), preservando **explicitamente** o PVC
(`kiro-workspace-<slug>`) e o Secret (`kiro-owner-id-<slug>`) -- os dois
NUNCA aparecem no teardown, de propósito. É isso que garante que um
`/provision` seguinte reconstrua o workload do zero de forma idempotente
com o MESMO workspace/histórico/login do `kiro-cli`. `/close` propaga
falha real de teardown como 502; `/logout` trata a mesma falha como
melhor esforço (nunca impede o logout do KrewHub em si).

**Achado real ao validar contra o cluster homelab (`<cluster-homelab>`)
depois do deploy da imagem com essa mudança:** o `ClusterRole
krewhub-central` (`clusters/<cluster>/<namespace-real>/krewhub-central/clusterrole.yaml`,
GitOps) tinha sido escrito ANTES desta fatia e não tinha nenhum verbo
`delete` (decisão documentada como correta na época: "o reconcile hoje é
só create/patch, nunca remove nada"). Resultado: `/close`/`/logout`
davam 502 (403 Forbidden do apiserver, `cannot delete resource
"deployments"`) contra o cluster real, mesmo com a suíte pytest (que
mocka o client k8s) passando 100%. Corrigido adicionando `delete`
SOMENTE em Deployment/Service/NetworkPolicy/ConfigMap no `ClusterRole` --
Secret e PersistentVolumeClaim continuam de propósito SEM `delete`,
reforçando em profundidade (RBAC, não só código) a garantia de que o
teardown nunca apaga workspace ou credencial do dev.

**Testado ao vivo contra o cluster homelab depois da correção de RBAC**,
com um owner descartável (via port-forward direto no Service
`krewhub-central`, sessão assinada localmente com o `session-secret`
real, sem depender do fluxo OIDC completo):

```
POST /devs/<owner-descartável>/provision?wait=true
  -> 200, steps: secret/configmap/pvc/service/networkpolicy/deployment=created
     PVC uid=da1ed6a9-...  Secret uid=77703784-...

GET /close
  -> 200 "Workload encerrado"
  -> Deployment/Service/NetworkPolicy/ConfigMap: NotFound
  -> PVC/Secret: mesmos uids de antes (intactos)

POST /devs/<mesmo-owner>/provision?wait=true   (reprovision)
  -> 200, steps: pvc=updated, secret=updated (não "created")
     PVC/Secret: MESMOS uids de antes -- idempotência real confirmada
  -> pod 1/1 Running

GET /logout
  -> 302 Location: /login, Set-Cookie: krewhub_session=""; Max-Age=0
  -> Deployment/Service/NetworkPolicy/ConfigMap: NotFound de novo
  -> PVC/Secret: mesmos uids -- preservados também no /logout
```

Owner descartável limpo depois (PVC + Secret removidos manualmente com
credencial de cluster-admin, fora do RBAC do `krewhub-central` -- o
próprio teste confirma que o service account da aplicação não teria
conseguido apagar esses dois).

## Deploy no cluster (bloqueio de adoção fechado)

Até esta fatia, o serviço só rodava na máquina do operador
(`localhost:9100`), lendo `~/.kube/config-personal` -- ou seja, **só o
operador conseguia usar isso**. Isso foi o bloqueio real de adoção
priorizado nesta fatia (à frente de culling).

**Onde:** `clusters/<cluster>/<namespace-real>/krewhub-central/` -- decisão
deliberada de **não** criar um diretório `krewhub/` novo no GitOps (nem
renomear `<namespace-real>/` -> `krewhub/`): o CHP já roda em
`clusters/<cluster>/<namespace-real>/chp/`, os Kustomizations do Flux
descobrem cada subdiretório automaticamente (mesmo padrão de `chp/` e
`dev-testdev/`, sem kustomization.yaml agregador no nível acima), e
colocar o serviço junto do CHP no mesmo namespace (`<namespace-real>`) evita
qualquer necessidade de NetworkPolicy cross-namespace nova pra ele
alcançar o CHP. Renomear o diretório todo só por consistência de nome
não pagaria o churn (precisaria re-registrar o path em nada -- Flux
descobre por conteúdo, não por nome -- mas ainda seria um diff enorme e
sem ganho funcional).

**Imagem:** `ghcr.io/lucasces/krewhub-central:sha-f287446` (tag bump mais recente -- UX do lobby; a fatia original de deploy usou `sha-2a3ad7f`) -- `Dockerfile`
na raiz deste repo (`python:3.12-slim`, non-root uid 1000, `uv sync
--frozen --no-dev` a partir de `pyproject.toml`/`uv.lock` + `app/`).
Build/push feito com `podman` +
`gh auth token | podman login ghcr.io`, mesmo procedimento já usado pro
`omnigent-server` (ver CLAUDE.md do operador). **Achado no caminho:** o
pacote ficou `private` por padrão e a troca de visibilidade via API do
GitHub (`PATCH /user/packages/container/...`) devolveu 404 consistente
(limitação conhecida desse endpoint pra pacotes de conta pessoal) -- ao
contrário de `omnigent-server`/`omnigent-host`/`hermes-agent`, que já
eram públicos de antes. Resolvido com `imagePullSecret` (ver abaixo) em
vez de insistir na troca de visibilidade.

**RBAC (`serviceaccount.yaml`/`clusterrole.yaml`/`clusterrolebinding.yaml`):**
`ClusterRole` (não `Role`) -- histórico: originalmente porque o serviço
criava um **namespace novo por dev** sob demanda. **Isso mudou** (ver
seção "Namespace único compartilhado pra pods de dev" abaixo): hoje há
UM namespace fixo (`KREWHUB_DEV_NAMESPACE`, default `krewhub-devs`),
criado declarativamente no GitOps, não pelo reconcile -- o `ClusterRole`
continua sendo `ClusterRole` (não `Role`) só porque `KREWHUB_DEV_NAMESPACE`
é configurável e não queremos prender o RBAC a um nome fixo, não mais
por precisar de namespaces dinâmicos. Permissões são só o que
`app/k8s_manager.py` e `app/k8s_templates.py` de fato chamam (conferido
por grep, não uma lista aspiracional): `namespaces`
(**`get/list/watch` só -- SEM `create`/`patch`/`update`**, achado desta
fatia: sobrava, o reconcile só confirma que o namespace compartilhado já
existe), `secrets`/`persistentvolumeclaims` (`get/list/watch/create/
patch/update`, **sem `delete`** -- `teardown_dev_workload` nunca apaga
nenhum dos dois, de propósito), `configmaps`/`services`/`pods`
(`get/list/watch/create/patch/update/delete` -- `pods` migrou de
`deployments` nesta fatia, ver seção "Deployment vs Pod puro pro
workload por-dev": o workload por-dev virou um `Pod` puro em vez de
`Deployment`, então o RBAC de create/patch/update/delete foi junto) +
`pods/exec`(`get`,`create`), `networkpolicies`
(`get/list/watch/create/patch/update/delete`). **Achado ao vivo, não
óbvio:** o client
Python (`connect_get_namespaced_pod_exec`) emite a requisição de exec
como **HTTP GET** com upgrade pra websocket -- o apiserver valida RBAC
contra o verbo HTTP real da chamada, não contra a convenção usual
"`pods/exec` = verbo `create`" (essa convenção vale pro `kubectl exec`,
que usa POST/SPDY). Só com `create` no rule, o startup do serviço (que
faz exec no pod do CHP pra se auto-registrar) dava 403 explícito
("cannot **get** resource pods/exec") -- corrigido adicionando `get`.
Sem isso documentado, um RBAC copiado de exemplos genéricos da internet
teria o mesmo bug silencioso. Nenhuma permissão de `delete` nem
`cluster-admin` em lugar nenhum.

**Config in-cluster:** `app/k8s_manager.py::_load_config` tenta
`config.load_incluster_config()` primeiro (token do ServiceAccount
montado automaticamente pelo kubelet) e só cai pro kubeconfig local em
`ConfigException` -- confirmado ao vivo nos dois modos (local: log
`k8s config: kubeconfig local (...)`; in-cluster: log `k8s config:
in-cluster (ServiceAccount)`).

**Persistência:** `PersistentVolumeClaim` `krewhub-central-data` (1Gi,
`rook-cephfs`, mesma única StorageClass do cluster) montada em `/data`;
`KREWHUB_DB_PATH=/data/krewhub.db` no Deployment -- sem isso o SQLite
viveria no filesystem efêmero do container e se perderia a cada
restart/redeploy. Mesma exigência de `nodeAffinity` pro node
control-plane já documentada em `dev-testdev/deployment.yaml` (CSI do
`rook-cephfs` só roda em `coruscant`/`tatooine`).

**Exposição -- sem Ingress controller neste cluster (confirmado, não
suposto):** `kubectl get ingressclass` retorna vazio e não há
`ingress-nginx`/`traefik`/nenhum outro controller rodando
(`kubectl get pods -A` sem hits). Em vez de instalar um Ingress
controller novo (decisão de infra de alto impacto -- não assumida sem
perguntar, conforme combinado), **reaproveitei a infra que já existe**:
o próprio CHP, que já faz host-routing pros pods de dev. `main.py` tem um
hook de startup (`_self_register_route`) que, se `KREWHUB_SELF_HOST`
estiver configurado (setado no Deployment como
`krewhub.kiro.internal`), registra a própria rota no CHP
(`krewhub.kiro.internal -> krewhub-central.<namespace-real>.svc.cluster.local:8080`)
do mesmo jeito que registra rota pra cada pod de dev. Resultado prático:
acesso ao serviço segue exatamente o mesmo padrão já em uso (port-forward
no Service do CHP + `Host:` header, ou entrada em `/etc/hosts` apontando
`krewhub.kiro.internal` pro mesmo IP dos outros `*.kiro.internal`) --
**nenhuma infraestrutura nova precisou ser instalada**. Se um dia quiser
Ingress "de verdade" (TLS automático, LoadBalancer, etc.), instalar um
controller é uma decisão de infra separada, maior, que cabe perguntar
depois.

**`imagePullSecret` (`ghcr-pull`) -- aplicado direto via `kubectl`, NÃO
commitado no GitOps:**
```
kubectl -n <namespace-real> create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=lucasces \
  --docker-password="$(gh auth token)" --docker-email=lucas.ces@gmail.com
```
Motivo de não ir pro git: é uma credencial real (token do GitHub), exatamente
o caso que a regra "GitOps first" do operador já prevê como exceção
("reserve direct kubectl apply/edit/patch for changes you have a specific
reason not to put in git"). O `Deployment` referencia o secret só pelo
nome (`imagePullSecrets: [{name: ghcr-pull}]`) -- isso sim é git-tracked.
Se o token expirar ou for rotacionado, re-rodar o comando acima substitui
o Secret (não precisa tocar no Deployment).

**Testado ao vivo -- alguém sem o kubeconfig pessoal do operador
conseguindo usar o serviço:**
```
kubectl -n <namespace-real> rollout status deployment krewhub-central   -> 1/1 Ready
kubectl -n <namespace-real> logs deploy/krewhub-central:
  k8s config: in-cluster (ServiceAccount)
  rota registrada host=krewhub.kiro.internal target=http://krewhub-central.<namespace-real>.svc.cluster.local:8080 status=201

curl -H "Host: krewhub.kiro.internal:8080" http://localhost:8080/healthz
  -> 200 {"status": "ok"}

curl -H "Host: krewhub.kiro.internal:8080" http://localhost:8080/devs/rbac-test%40krewhub.local.test/lobby
  -> 200, form HTML servido pelo pod em cluster

POST .../lobby  login_mode=personal
  -> 200, página com dashboard_url_with_token + device-flow (user_code=SGLV-KDVW)
  -> reconcile via RBAC do ServiceAccount criou os 7 recursos
     (namespace/secret/configmap/pvc/service/networkpolicy/deployment)
     em kiro-dev-rbac-test-krewhub-local-test

curl -H "Host: rbac-test-krewhub-local-test.kiro.internal:8080" http://localhost:8080/?token=...
  -> 200 OK + Set-Cookie: mc_token_8080=... (sessão real do dashboard)
```
Nenhum desses comandos usou `~/.kube/config-personal` -- só HTTP através
do port-forward local no Service do CHP (o mesmo túnel que qualquer outra
pessoa com acesso à rede do cluster também pode abrir). Namespace de
teste (`kiro-dev-rbac-test-...`) removido depois de confirmado.

## Namespace único compartilhado pra pods de dev (arquitetura)

Até a fatia anterior, cada `POST /devs/{owner_id}/provision` criava um
`Namespace` k8s **novo** (`kiro-dev-<slug>`) sob demanda. Achado da
investigação de "spam de namespaces": isso gerava acúmulo sem processo
de limpeza (namespaces de teste ficavam pra trás -- `kiro-dev-e2e-test-*`,
`kiro-dev-lobby-test-*`, `kiro-dev-placeholder`, todos deletados numa
limpeza anterior a esta fatia, junto com um registro órfão
`rbac-test@krewhub.local.test` que sobrou no SQLite e o `krewhub.db`
local obsoleto). Mudança de arquitetura desta fatia: **todos os devs
passam a compartilhar UM único namespace**.

- **`KREWHUB_DEV_NAMESPACE`** (nova env var, default `krewhub-devs`) --
  namespace **dedicado só a pods de dev**, separado do namespace
  `<namespace-real>` onde vivem o CHP e o próprio `krewhub-central`. Decisão de
  manter separado (não reaproveitar `<namespace-real>`): blast-radius de RBAC do
  `ServiceAccount`/`ClusterRole` do `krewhub-central` fica
  conceitualmente isolado da infra do hub, não por precisar de fronteira
  de rede (isso é RBAC/NetworkPolicy, não namespace boundary).
- **Criado declarativamente no GitOps**
  (`clusters/<cluster>/<namespace-real>/krewhub-central/dev-namespace.yaml`),
  **não** pelo reconcile do app -- `ensure_dev_namespace` hoje só faz um
  `read_namespace` (GET) pra confirmar que existe, nunca cria nem edita.
  Isso permitiu **reduzir o RBAC**: o `ClusterRole` perdeu
  `create`/`patch`/`update` em `namespaces`, ficando só
  `get`/`list`/`watch` (ver seção RBAC acima) -- achado desta fatia,
  não era mais usado.
- **Nomes de recurso levam o slug do dev** pra coexistir no mesmo
  namespace sem colidir: `kiro-owner-id-<slug>` (Secret),
  `kiro-config-<slug>` (ConfigMap), `kiro-workspace-<slug>` (PVC),
  `kirocrew-<slug>` (Service + Pod),
  `allow-chp-to-dashboard-only-<slug>` (NetworkPolicy).
- **Ponto crítico de segurança -- isolamento de rede não depende mais da
  fronteira do namespace.** Pod/Service/NetworkPolicy de cada dev
  levam a label `krewhub.pespa.net/owner-slug: <slug>` nos pods; a
  `NetworkPolicy` de cada dev usa um `podSelector`
  (`app=kirocrew,krewhub.pespa.net/owner-slug=<slug>`) que seleciona **só
  o pod daquele dev** -- NetworkPolicy no Kubernetes é por-pod, então a
  regra do dev A nunca afeta o pod do dev B, e tráfego de A pro pod de B
  não bate em nenhuma allowlist de B (default-deny implícito de quem tem
  QUALQUER `NetworkPolicy` selecionando o pod).

**Testado ao vivo -- não presumido** (2 devs de teste reais provisionados
no MESMO namespace compartilhado, `net-test-a`/`net-test-b`, via
`/provision?wait=true`, cada um com sua Deployment/Service/NetworkPolicy
próprios; `net-test-b` ficou `Pending` por alguns minutos por contenção de
CPU nos nós control-plane -- mesmo achado operacional já documentado na
seção do lobby, contornado escalando `kiro-dev-testdev` (legado,
descartável) a 0 réplicas temporariamente e devolvendo a 1 depois):

```
A -> Service do B (porta 5476, DNS interno kirocrew-net-test-b-....svc.cluster.local)
  -> curl: Failed to connect ... Could not connect to server (exit 7)   BLOQUEADO

B -> Service do A
  -> curl: Failed to connect ... Could not connect to server (exit 7)   BLOQUEADO

CHP -> Service do A                     -> HTTP 403 (Host header não enviado,
CHP -> Service do B                     -> HTTP 403  mas TCP conectou -- alcançou
                                            a camada de aplicação, controle
                                            positivo de que A e B ESTÃO de pé e
                                            servindo, não é "porta fechada")
```

O contraste entre "connection failed" (A<->B, rejeitado na camada de
rede pela `NetworkPolicy`) e "403 do Host header" (CHP->A, CHP->B,
conectou e chegou na aplicação) confirma que o isolamento é real -- não
é só ausência de rota, é a allowlist da `NetworkPolicy` fazendo o
trabalho. Namespace e SQLite (`net-test-a@krewhub.local.test`,
`net-test-b@krewhub.local.test`) limpos depois de confirmado, mesmo
padrão dos testes anteriores.

**Legado -- `kiro-dev-testdev` e `kiro-dev-lucas-ces-gmail-com` NÃO
foram migrados:** esses dois continuam no modelo antigo (namespace
próprio por dev, sem a label `owner-slug`), decisão deliberada -- só o
comportamento **pra frente** (novos `/provision`) precisa do namespace
compartilhado. Consequência aceita: chamar `/session` ou `/kiro-login`
pra esses dois específicos falharia com "nenhum pod kirocrew Running"
sob o namespace novo (o código busca `kirocrew-<slug>` dentro de
`KREWHUB_DEV_NAMESPACE`, e esses dois pods vivem nos namespaces antigos)
-- não migrados nesta fatia, ficam documentados como legado até uma
decisão explícita de migrar ou aposentar.

## 404 no primeiro provision real via OIDC -- causa raiz e correção (não foi RBAC)

Logo depois da migração pra namespace único compartilhado + RBAC reduzido
(seção acima), o Lucas fez o primeiro `/provision` real via login OIDC de
verdade (`owner_id` = `lucas.ces@minha-org.com.br`, claim do Keycloak
de um IdP real de terceiro) e, ao abrir
`http://lucas-ces-minha-org-com-br.kiro.internal:8080/?token=...` no
navegador, recebeu **404**. Hipótese inicial (não confirmada de cara):
regressão do RBAC reduzido ou da normalização do slug, por ser o primeiro
provision real depois dessas mudanças.

**Evidência coletada, nessa ordem, antes de mexer em qualquer coisa:**

1. `kubectl get all -n krewhub-devs` -- `Deployment`/`Service`/`Pod`
   `kirocrew-lucas-ces-minha-org-com-br` existiam, `1/1 Running`, no
   namespace compartilhado certo. **Descarta** "recurso não foi criado".
2. `kubectl logs deploy/krewhub-central -n <namespace-real>` (filtrado por
   `minha-org`) mostrou o reconcile **completo e sem erro**:
   ```
   INFO:krewhub.k8s:reconcile owner_id=lucas.ces@minha-org.com.br namespace=krewhub-devs slug=lucas-ces-minha-org-com-br steps={'namespace': 'exists', 'secret': 'created', 'configmap': 'created', 'pvc': 'created', 'service': 'created', 'networkpolicy': 'created', 'deployment': 'created'}
   INFO:krewhub.chp:rota registrada host=lucas-ces-minha-org-com-br.kiro.internal target=http://kirocrew-lucas-ces-minha-org-com-br.krewhub-devs.svc.cluster.local:5476 status=201
   ```
   Nenhum 403/401 relacionado a esse `owner_id`, nenhum erro de RBAC nos
   sete steps do reconcile. **Descarta** "RBAC reduzido bloqueou alguma
   operação do provision".
3. Consulta direta na API admin do CHP (`kubectl exec` no pod do
   `configurable-http-proxy`, `GET /api/routes` com o token do Secret
   `chp-admin-token`) confirmou a rota registrada, host **exatamente**
   igual ao esperado, sem diferença de normalização (`.`/`@` -> `-`):
   ```
   "/lucas-ces-minha-org-com-br.kiro.internal": {
     "target": "http://kirocrew-lucas-ces-minha-org-com-br.krewhub-devs.svc.cluster.local:5476",
     "host": "lucas-ces-minha-org-com-br.kiro.internal"
   }
   ```
   **Descarta** "rota não registrada" e "bug de normalização de slug".
4. `ps aux | grep port-forward` revelou a causa real: os dois
   port-forwards locais ativos na porta 8080
   (`kubectl port-forward svc/krewhub-central 8080:8080` e um outro em
   `9300:8080`, ambos sobras de sessões anteriores testando os endpoints
   do próprio `krewhub-central`) apontavam **direto pro Service do
   `krewhub-central`**, não pro Service do CHP. `curl -H 'Host:
   lucas-ces-minha-org-com-br.kiro.internal' http://127.0.0.1:8080/`
   confirmou: **404**, vindo do `krewhub-central` (que só conhece suas
   próprias rotas -- `/login`, `/devs/...`, etc. -- e não faz dispatch por
   `Host` header pra outros pods), não do CHP.

**Causa raiz confirmada:** nenhuma regressão de código/RBAC/GitOps. O
túnel local usado pra acessar `*.kiro.internal:8080` estava amarrado no
Service errado -- `svc/krewhub-central` em vez de
`svc/configurable-http-proxy` (porta `8000`, o `--port` público do CHP
configurado em `--host-routing`). O CHP é quem faz o roteamento por
`Host` header pros pods de dev; o `krewhub-central` é só mais um alvo de
rota **dentro** do CHP (`krewhub.kiro.internal` -> Service do
`krewhub-central`, confirmado no mesmo `/api/routes`). Apontar o
port-forward pro `krewhub-central` direto pula o CHP inteiro, e qualquer
`Host` diferente das rotas nativas do `krewhub-central` cai em 404 --
exatamente o sintoma, e coincidentemente só apareceu agora porque foi o
primeiro teste após uma sessão anterior que deixou port-forwards
apontados pro alvo errado de pé.

**Correção aplicada (sem tocar em código nem manifest):**

```bash
kill <pid-dos-port-forwards-pro-krewhub-central>   # 8080:8080 e 9300:8080
kubectl port-forward svc/configurable-http-proxy 8080:8000 \
  --context <cluster-homelab> --namespace <namespace-real>
```

**Testado ponta a ponta depois da correção**, pelo mesmo túnel (porta
local `8080`), com `Host` header real:

```
curl -H 'Host: lucas-ces-minha-org-com-br.kiro.internal' http://127.0.0.1:8080/
  -> 200 OK, HTML real do Kiro Crew (aiohttp, dashboard), não mais 404

curl -H 'Host: krewhub.kiro.internal' http://127.0.0.1:8080/
  -> 404 esperado (krewhub-central não tem handler pra "/"; rotas
     próprias como /login e /devs/... continuam funcionando -- já
     confirmado nas seções de OIDC e autenticação acima)
```

Fluxo `login -> lobby -> provision -> dashboard` confirmado de pé de
novo pra `lucas.ces@minha-org.com.br`.

**Lição operacional pra não repetir:** pra testar `*.kiro.internal:8080`
localmente (qualquer host, dev ou `krewhub.kiro.internal`), o
port-forward tem que apontar SEMPRE pro Service do CHP
(`svc/configurable-http-proxy`, porta `8000`), nunca direto pro Service
de um app individual (`krewhub-central` ou `kirocrew-<slug>`) -- o CHP
que faz o dispatch por `Host` header, apontar pro alvo final pula essa
camada e produz 404 enganoso que parece bug de roteamento/RBAC mas é só
túnel local apontado errado. Antes de investigar qualquer 404 em
`*.kiro.internal`, primeiro passo é `ps aux | grep port-forward` +
conferir o alvo (`svc/configurable-http-proxy`, não outra coisa).

## "Sessão do dashboard sumiu" depois de um `kiro-cli login` reconhecido -- não é bug do KrewHub (achado do produto Kiro Crew)

Sintoma reportado: pro mesmo owner (`lucas.ces@minha-org.com.br`,
slug `lucas-ces-minha-org-com-br`, namespace compartilhado
`krewhub-devs`), o `kiro-cli login` foi reconhecido como já feito (cache
em `~/.local/share/kiro-cli/data.sqlite3` persistiu certo), **mas** a
sessão anterior do dashboard (tema, layout, etc.) sumiu, como se fosse
primeiro acesso -- mesmo supostamente sob o mesmo `$HOME` no mesmo PVC.
Investigado nesta ordem, sem presumir causa:

1. **`kubectl get pvc -n krewhub-devs`** -- só existe **UM** PVC pra esse
   owner: `kiro-workspace-lucas-ces-minha-org-com-br`, `Bound`, idade
   batendo com o provision original. **Descarta** "PVC órfão do namespace
   antigo" -- confirmado também que `kiro-dev-lucas-ces-minha-org-com-br`
   (o namespace que existiria no modelo pré-migração) nunca existiu: esse
   owner só foi provisionado depois da migração pra namespace
   compartilhado, não tem passado no modelo antigo.
2. **`describe pvc` + Deployment atual** -- o PVC `Used By` aponta pro
   pod atual (`kirocrew-lucas-ces-minha-org-com-br-9d5485d46-z9xjv`),
   montado em `/home/kirocrew` (volume `home`); só existe mais um volume,
   `tmp` (`emptyDir`, não persistente, esperado). Um único PVC, um único
   ponto de montagem, sem ambiguidade sobre "qual disco está de pé".
3. **Dentro do pod**, `HOME=/home/kirocrew` confirmado (`env`/`id`), e
   `data.sqlite3` do `kiro-cli` de fato mora sob esse `HOME`
   (`/home/kirocrew/.local/share/kiro-cli/data.sqlite3`) -- é por isso
   que o login persiste. Grep no código-fonte vendorizado do dashboard
   (`kiro_crew/dashboard/server.py`, comentário do próprio autor upstream
   junto da lógica de canonicalização de host) revelou onde tema/zoom/
   layout/notificações realmente vivem:
   > "Host canonicalization: converge loopback aliases (...) onto a
   > single origin so the SPA's **per-origin `localStorage`** (theme,
   > zoom, layout, notifications, ...) is never split across hostnames.
   > `localStorage` keys on `scheme://host:port` (...) **Disabled unless
   > `local_only`, so reverse-proxy / remote-host deployments are never
   > affected.**"

   Ou seja: tema/layout/preferências do dashboard **não são persistidos
   no servidor/PVC -- vivem inteiramente no `localStorage` do navegador**,
   por origem (`scheme://host:port`). É comportamento vendorizado do
   próprio Kiro Crew, não algo que o KrewHub grava ou deixa de gravar.
4. **`df`/`mount` dentro do pod** confirmam só dois pontos de montagem
   relevantes: CephFS (`rook-cephfs`) em `/home/kirocrew` (o PVC, onde o
   `kiro-cli` grava de verdade) e `ext4` local do node em `/tmp`
   (`emptyDir`, efêmero). **Não existe** nenhum path de preferências do
   dashboard fora do `HOME` pra checar -- porque, pelo achado acima, ele
   não grava preferências em arquivo nenhum no servidor, é 100%
   client-side. `KIROCREW_BIND=0.0.0.0` no `env` do pod confirma que essa
   instância roda com `local_only=False` (bind não-loopback, obrigatório
   pra ser alcançável via Service/CHP) -- exatamente o caso que o
   comentário do vendor rotula como "reverse-proxy / remote-host
   deployment", o cenário em que a proteção de canonicalização de origem
   fica desligada por design.
5. **Restarts do pod atual:** `RESTARTS=0`, mesmo pod (`-z9xjv`) de pé
   desde o provision original (idade do pod = idade do PVC, `describe
   pod` sem eventos recentes). **Descarta** "processo reiniciou e perdeu
   estado só-em-memória nunca gravado em disco" -- não houve restart
   nenhum entre os dois acessos.
6. **Idempotência do PVC no reconcile** (`app/k8s_manager.py` +
   `app/k8s_templates.py`): `ensure_pvc` chama `build_pvc`, que nomeia o
   `PersistentVolumeClaim` como `f"kiro-workspace-{slug}"` -- função pura
   do slug, sem timestamp/random, e o padrão `_ensure` é
   `read`-then-`create-or-patch` (get-or-create). O `namespace` também é
   fixo (`KREWHUB_DEV_NAMESPACE`) desde a migração. **Não existe** nenhum
   caminho de código em que dois `/provision` do mesmo owner_id,
   antes ou depois da migração de namespace-por-dev pra compartilhado,
   resultem em nomes de PVC diferentes -- confirmado lendo o código, não
   só observando ausência do namespace antigo.

**Causa raiz:** nenhuma perda de dado no KrewHub/PVC/RBAC -- os seis
pontos acima descartam, com evidência, PVC duplicado/órfão, montagem
errada, restart, e não-idempotência do reconcile. O que aconteceu é que
"sessão do dashboard" (tema/layout/preferências) **nunca foi, por design
do produto Kiro Crew, um dado persistido no servidor** -- é
`localStorage` do navegador, por origem. Ela "sumir" significa que a
visita em que ela pareceu resetada leu de um bucket de `localStorage`
diferente (navegador diferente, perfil diferente, aba anônima/privada,
ou storage limpo) do bucket da visita anterior -- nunca dependeu do PVC,
então não há nada no lado do servidor pra "corrigir" ali. O `KrewHub`
já serve um único host canônico e estável por owner
(`KIROCREW_CORS_ORIGINS=http://<slug>.kiro.internal:8080`, um valor só,
sem variação de porta/scheme entre visitas), então, do lado do servidor,
não há split de origem induzido pelo KrewHub -- qualquer split é
client-side, fora do que o `krewhub-central` controla.

**Sem correção aplicada nesta fatia -- decisão do Lucas antes de mexer:**
isso é comportamento estrutural do produto Kiro Crew vendorizado (tema
salvo só em `localStorage`, nunca no servidor), não um bug do KrewHub.
Uma correção "de verdade" (persistir preferências de dashboard no
servidor/PVC) seria uma mudança de produto no próprio `kirocrew`
(upstream, `ghcr.io/kirodotdev/kirocrew:0.6.0`) ou um proxy que injeta
estado -- escopo maior que este achado, não uma correção mínima seguro
de aplicar sem uma decisão explícita.

## Smoke-test em cluster efêmero (terceira camada de teste, engine plugável)

Duas camadas de teste já existiam: `uv run pytest` (offline, tudo
mockado) e o smoke-test MANUAL contra o cluster REAL (`<cluster-homelab>`,
seções "testado ao vivo" espalhadas por este README). Esta terceira
camada (`smoke/`) prova o mesmo fluxo ponta a ponta (provision -> rota no
CHP -> acesso ao dashboard -> close -> logout -> cleanup) contra um
cluster Kubernetes **descartável**, sem tocar no cluster real e sem
depender da imagem pesada/licenciada do kirocrew de verdade.

### Engine de cluster efêmero é plugável -- `smoke/engines/`

`smoke/run_smoke.py` nunca fala com kind/k3d/podman diretamente -- só com
a interface `ClusterEngine` (`smoke/engines/base.py`): `is_available()`,
`up()`, `down()`, e um método opcional `load_image()` (como uma imagem
Docker local chega no cluster é o ponto mais específico de cada engine,
não faz parte do contrato mínimo). Trocar de engine no futuro não exige
tocar em `run_smoke.py` nem em `smoke/fake_kirocrew/`.

Seleção via `KREWHUB_SMOKE_K8S_ENGINE`, **sem default silencioso**: sem
essa env var setada, o script lista as opções conhecidas com o motivo
exato de `is_available()` de cada uma e sai (código 2) -- rodar o engine
errado sem perceber (ex.: cair num fallback que aponta pro cluster REAL)
seria pior que exigir uma escolha explícita.

```bash
uv run python smoke/run_smoke.py --list-engines
KREWHUB_SMOKE_K8S_ENGINE=podman-machine uv run python smoke/run_smoke.py
```

### Qual engine funciona neste host hoje: só `podman-machine`

**`kind` e `k3d` NÃO funcionam neste host** (NixOS) -- não são só
"binário ausente", são duas paredes estruturais reais, confirmadas ao
tentar antes de escrever a abstração:

1. Ambos rodam "nodes" Kubernetes como containers Docker/Podman que
   montam `/lib/modules` do HOST por um path hardcoded
   (`/lib/modules:/lib/modules:ro`). Este host não tem `/lib/modules`
   clássico -- módulos vivem em
   `/run/current-system/kernel-modules/lib/modules/<versão>` (layout
   NixOS). O bind mount aponta pra um diretório vazio/inexistente.
2. Ambos esperam falar com o container runtime num socket de path fixo
   (`/var/run/docker.sock` ou equivalente). O socket do Podman aqui é
   rootless, em `$XDG_RUNTIME_DIR/podman/podman.sock` -- não no path que
   os node-containers de kind/k3d embutem.

Nenhuma das duas é contornável sem um workaround frágil (patch manual de
manifesto do kindnet, symlink fake de socket em `/var/run`, que exigiria
root e mascarar um caminho do sistema) -- o plano pediu pra reportar isso
em vez de aplicar. `smoke/engines/kind.py` e `k3d.py` ficam só como
stubs: interface pronta, `is_available()` documenta o motivo exato,
`up()`/`down()` levantam `EngineError` explícito, prontos pra implementar
de verdade se o host mudar.

**`podman-machine` funciona** porque sobe uma VM **real** (QEMU acelerado
por KVM -- `/dev/kvm` confirmado disponível) com um kernel Fedora CoreOS
de verdade: `/lib/modules` clássico existe lá dentro, e o container
runtime da VM não tem o problema de path do host. Dentro dessa VM
instalamos **k3s nativamente** (não mais um container aninhado) -- um
único binário com containerd embutido e o controlador de NetworkPolicy
do kube-router habilitado por padrão.

Pré-requisito descoberto ao vivo (não documentado antes de tentar): a
imagem do `podman machine` desta versão empacotada pelo Nix (5.8.6) não
traz `qemu-img`/`qemu-system-x86_64`, `gvproxy` nem `virtiofsd`
embutidos -- `podman machine start` falha com erro explícito pra cada um
(`could not find "gvproxy"...`, `failed to find virtiofsd`). Resolvido
via `nix build nixpkgs#<pkg> --no-link --print-out-paths` (rápido,
cacheado) escrevendo `~/.config/containers/containers.conf` com
`helper_binaries_dir` apontando pros três -- `smoke/engines/podman_machine.py`
faz isso sozinho a cada `up()` (idempotente).

Fluxo completo de `up()`: cria/inicia a VM -> instala k3s via SSH se
ainda não instalado (`get.k3s.io`, `--disable traefik --disable
servicelb`) -> espera o node ficar Ready -> busca o kubeconfig de dentro
da VM e reescreve `server:` pra um túnel SSH local
(`ssh -L 16443:127.0.0.1:6443 ...`) mantido em background -> devolve um
`ClusterHandle` usável IMEDIATAMENTE por `kubectl`/client Python
`kubernetes` rodando no host -- confirmado ao vivo que `exec` (usado por
`chp_client.py`/`session_client.py`) e `kubectl port-forward` (usado no
passo final do smoke-test) funcionam através desse túnel exatamente como
contra o cluster real.

`down()` por padrão remove a VM inteira (`podman machine rm -f`) --
efêmero de verdade, confirmado ao vivo (kubeconfig e VM somem depois).
`KREWHUB_SMOKE_KEEP_MACHINE=1` pula a remoção (só para a VM) pra iteração
rápida repetida sem pagar de novo o custo de instalar k3s (~2-3min) --
**cuidado**: rodar `up()` de novo rápido demais depois de um `down()`
sem essa flag pode colidir com namespaces ainda em `Terminating` da
rodada anterior (visto ao vivo uma vez) -- não é um bug de idempotência
do reconcile, é só o k3s ainda finalizando a exclusão; espera alguns
segundos ou usa `KREWHUB_SMOKE_KEEP_MACHINE=1` entre execuções rápidas.

### `smoke/engines/external.py` -- fallback manual

Não sobe nada -- aponta pra um kubeconfig/contexto já existente via
`KREWHUB_SMOKE_EXTERNAL_KUBECONFIG`/`KREWHUB_SMOKE_EXTERNAL_CONTEXT`
(sem default de contexto -- não assume qual usar). Útil pra apontar pra
um namespace descartável dentro de um cluster real (inclusive o próprio
`<cluster-homelab>`) se nenhum engine efêmero estiver disponível.
`down()` é sempre no-op -- este engine nunca destrói um cluster que não
criou.

### `smoke/fake_kirocrew/` -- por que não usar a imagem real

A imagem real (`ghcr.io/kirodotdev/kirocrew`) é pesada, licenciada, e
exige um humano completando um device-flow no navegador (`kiro-cli
login`) -- inviável pra um smoke-test automatizado. `fake_kirocrew/` é
um servidor HTTP mínimo (só stdlib Python) que serve `/api/health`,
`/api/ready`, `/api/live` e uma `/` reconhecível, mais um `kirocrew`
(CLI fake) que reproduz o formato de saída exato que
`session_client.py` já sabe parsear (`token --ttl` imprime uma URL com
`?token=`, `logout` imprime `✅`). Não reimplementa nada de
auth/sandbox real -- só o suficiente pra provar que o reconcile do
KrewHub, o roteamento do CHP e a emissão/revogação de sessão funcionam
de ponta a ponta.

`PodmanMachineEngine.load_image()` builda essa imagem DENTRO da VM (que
já tem podman, Fedora CoreOS) e importa o resultado no containerd
embutido do k3s via `ctr images import` -- sem precisar de nenhum
registry externo.

### Rodado ao vivo, com sucesso, ciclo completo

```
[1/8] engine.up() (podman-machine) ...
[2/8] aplicando manifests.yaml (namespaces + CHP) ...
[3/8] load_image(fake-kirocrew) ...
[4/8] subindo krewhub-central local, apontado pro cluster efêmero ...
[5/8] provision (reconcile + CHP route + token de sessão) ...
      provision ok: steps={'namespace': 'exists', 'secret': 'created',
      'configmap': 'created', 'pvc': 'created', 'service': 'created',
      'networkpolicy': 'created', 'deployment': 'created'}
      route={'host': 'smoke-test-krewhub-local-test.smoke.internal', ...,
      'status': 201}
[6/8] port-forward pro Service do CHP + acesso real ao dashboard fake ...
      200 OK através do CHP, HTML do fake-kirocrew confirmado
[7/8] close (revoga sessão do kirocrew) + logout (limpa cookie do KrewHub) ...

✅ SMOKE-TEST PASSOU -- todos os passos: ['engine_up', 'chp_ready',
'fake_image_loaded', 'krewhub_central_up', 'provision',
'dashboard_via_chp', 'close', 'logout']
[8/8] cleanup ...
```

Ciclo completo (VM do zero -> k3s instalado -> fluxo inteiro -> VM
removida) rodado ao vivo em ~4m30s. Confirmado ao final: `podman machine
list` vazio, `/tmp/krewhub-smoke-kubeconfig.yaml` removido -- nada ficou
pra trás.

### O que falta pros outros engines

`kind`/`k3d`: só voltam a ser viáveis se o host mudar (kernel com
`/lib/modules` clássico, ou as ferramentas ganharem suporte a path de
socket customizável) -- os stubs já estão prontos, só falta implementar
`up()`/`down()`/`load_image()` de verdade quando isso deixar de ser um
bloqueio. `external`: já funcional como fallback manual, mas nunca
testado ao vivo nesta fatia (não havia necessidade -- `podman-machine`
funcionou de primeira depois de resolvido o `helper_binaries_dir`).

## Empacotamento Helm (`charts/krewhub/`)

Chart Helm pro que hoje é aplicado manualmente/via Kustomization solta no
GitOps (`clusters/<cluster>/<namespace-real>/{krewhub-central,chp}/*.yaml`) --
**esta fatia só cria e valida o chart, NÃO troca o mecanismo de deploy em
produção** (segue rodando via GitOps/Flux normalmente até decisão
explícita em contrário).

### Onde o chart vive, e por quê

`charts/krewhub/` dentro **deste repo** (`~/personal/krewhub`), não no
repo GitOps (`<cluster-homelab>`) -- decisão, não default: este é o
repo de CÓDIGO-FONTE do app (Dockerfile, `app/`, testes), e o padrão mais
comum (e o que menos acopla os dois repos) é o chart viver junto do
código que ele empacota, com o GitOps só *consumindo* esse chart (via
`HelmRelease` apontando pra um `GitRepository`/`OCIRepository` deste
repo) -- o mesmo padrão de "app repo publica, GitOps repo referencia"
já usado pra imagem Docker (`ghcr.io/lucasces/krewhub-central`, buildada
aqui, referenciada lá só pela tag). Deixar o chart no GitOps faria mais
sentido se ele fosse só configuração de ambiente (values por cluster),
não definição de recursos -- não é o caso aqui, o chart É a definição
canônica dos recursos do app.

### Scaffold gerado com `helm create`, não escrito do zero

`helm create krewhub` gerou o boilerplate padrão (`Deployment`/
`Service`/`Ingress`/`HorizontalPodAutoscaler`/`HTTPRoute`/`tests/` de
demo, apontando pra uma imagem `nginx` de exemplo). Removidos por
completo: `templates/hpa.yaml`, `templates/httproute.yaml`,
`templates/ingress.yaml`, `templates/tests/` -- nada disso existe no
deploy real hoje (sem HPA, sem Ingress controller no cluster, sem Gateway
API). `deployment.yaml`/`service.yaml`/`serviceaccount.yaml`/
`_helpers.tpl`/`NOTES.txt` foram mantidos como arquivo mas o CONTEÚDO
inteiro foi reescrito pros recursos reais (ver abaixo) -- nada do
boilerplate original de demo sobrou.

### O que o chart cobre -- e o que DELIBERADAMENTE não cobre

Cobre exatamente os dois componentes ESTÁTICOS que já rodam em produção,
fonte de verdade = os manifests atuais em
`clusters/<cluster>/<namespace-real>/{krewhub-central,chp}/*.yaml`:

- **`krewhub-central`**: `Deployment`, `Service`, `ServiceAccount`,
  `ClusterRole`/`ClusterRoleBinding` (RBAC mínimo, idêntico ao já
  documentado), `PersistentVolumeClaim` (SQLite).
- **`configurable-http-proxy` (CHP)**: `Deployment`, `Service`.
- **`Namespace krewhub-devs`** (compartilhado, ver seção "Namespace
  único compartilhado" acima) -- é infra ESTÁTICA do hub (existe
  independente de qualquer dev logado), por isso faz parte do chart,
  mesmo não sendo "por-dev".

**NÃO cobre, de propósito, sem ambiguidade**: os recursos POR-DEV
(`Secret kiro-owner-id-<slug>`, `ConfigMap kiro-config-<slug>`, `Service
kirocrew-<slug>`, `PVC kiro-workspace-<slug>`, `NetworkPolicy
allow-chp-to-dashboard-only-<slug>`, `Deployment kirocrew-<slug>`, todos
dentro de `krewhub-devs`) -- esses continuam sendo criados/atualizados
dinamicamente pelo `k8s_manager.py`/`k8s_templates.py` do próprio
`krewhub-central` via client Python `kubernetes`, fora do lifecycle do
Helm. **Consequência explícita**: `helm uninstall`/`helm upgrade` NUNCA
tocam nesses recursos (nem cria, nem atualiza, nem remove) -- eles vivem
e morrem por ação da própria API do app, não do Helm. Documentado
também em `templates/NOTES.txt` (mostrado depois de todo
`install`/`upgrade`) pra não virar ambiguidade depois.

### Correção aplicada: chart genérico, desacoplado do homelab -- e gestão de Secret 100% do operador

Duas correções explícitas pedidas depois da primeira versão desta
fatia, já incorporadas no chart e revalidadas (`helm lint`/`helm
template`/comparação ao vivo repetidos depois da mudança, mesmo
resultado sem regressão):

1. **Nada de específico do homelab `<cluster-homelab>` como default
   implícito.** Removidos/generalizados:
   - `krewhubCentral.persistence.storageClassName` -- default agora é
     `""` (o campo `storageClassName` fica OMITIDO do manifest, não
     setado como string vazia -- diferença real: omitir = usa a
     StorageClass default do cluster; `storageClassName: ""` explícito
     SIGNIFICARIA "sem StorageClass nenhuma"). `rook-cephfs` (única
     StorageClass do cluster real) vira só um exemplo documentado.
   - A `nodeAffinity` fixa pro CSI do rook-cephfs (que só roda em nós
     control-plane) virou um **passthrough genérico**
     (`krewhubCentral.affinity`/`nodeSelector`/`tolerations`, todos
     `{}`/`[]` por default) -- o chart não assume NENHUMA topologia de
     nó de nenhum cluster. O valor real do homelab é só um exemplo
     comentado em `values.yaml` e no arquivo de override abaixo.
   - `krewhubCentral.selfHost` (domínio `krewhub.kiro.internal`) e
     `krewhubCentral.imagePullSecretName` (nome `ghcr-pull`) tinham
     valores default do homelab real -- agora `""` por default (recurso
     desligado/pulado até configurar explicitamente).
   - Removido o value `namespace: <namespace-real>` do topo (não era lido por
     nenhum template, só documentação solta -- o namespace de instalação
     é sempre o passado em `helm install -n <ns>`).
   - Novo arquivo `charts/krewhub/examples/values-family-cluster.yaml`:
     os valores REAIS do homelab (antigos defaults), agora como um
     override explícito de exemplo, usado só pra validar o chart contra
     o cluster real (`helm template ... -f examples/values-family-cluster.yaml`)
     -- nunca aplicado como default do chart em si.

2. **Gestão de Secret é 100% do operador, sem exceção -- e agora com
   duas posturas diferentes, deliberadas:**
   - **`chp.adminToken.existingSecretName`** (era
     `chp.adminTokenSecretName`): Secret INDISPENSÁVEL pro container do
     CHP sequer iniciar (sem ele, `CreateContainerConfigError`). Default
     `""` faz `helm template`/`helm install`/`helm lint --strict`
     **falharem explicitamente** via `required()` do Helm, com mensagem
     dizendo exatamente o que falta e por quê -- em vez de renderizar
     (ou pior, aplicar) um Deployment que nunca vai ficar Ready.
   - **`krewhubCentral.oidc.existingSecretName`**: diferente do CHP, o
     app TOLERA nativamente a ausência desta config (`/login` responde
     501 explicando o que falta; o resto do serviço sobe normal). Por
     isso o chart não força um `required()` aqui -- com `""` (default),
     o bloco inteiro de env vars OIDC/sessão é OMITIDO do Deployment
     (não um secretKeyRef quebrado apontando pra Secret vazio), e
     `templates/NOTES.txt` deixa explícito, no output do
     `install`/`upgrade`, que isso é um pré-requisito funcional
     pendente -- consistente com "documente que é pré-requisito" sendo
     a alternativa aceitável a "falhe" quando o próprio app já degrada
     bem sozinho.
   - **`krewhubCentral.imagePullSecretName`**: mesmo padrão de antes
     (opcional, `""` = nenhum), só que agora sem um nome de exemplo do
     homelab (`ghcr-pull`) como default.
   - Em nenhum dos três casos o chart cria, gera ou assume um mecanismo
     específico de gestão de segredo (nada de Bitwarden/sealed-secrets
     embutido) -- só referencia por nome/chave um Secret que o operador
     já trouxe pra existir.

### Confirmação explícita, com evidência: `values.yaml` NUNCA carrega o valor de um token/segredo -- só o NOME do `Secret`

Dúvida legítima levantada e verificada campo a campo, não de memória:
**todo campo `*.existingSecretName` deste chart guarda o NOME de um
objeto `Secret` do Kubernetes já existente no cluster (uma string curta
tipo `"krewhub-chp-admin"`), nunca o valor literal do token/segredo em
si.** O valor real do segredo nunca passa por `values.yaml`, por nenhum
template, nem pela saída de `helm template` -- ele mora exclusivamente
dentro do objeto `Secret` no cluster, que o Kubernetes resolve em tempo
de execução do pod via `secretKeyRef`, uma referência indireta.

Evidência 1 -- `values.yaml`, o campo em si:
```yaml
chp:
  adminToken:
    existingSecretName: ""   # NOME do Secret, não o token
    key: token                # NOME da chave dentro do Secret, não o valor dela
```

Evidência 2 -- o template (`templates/chp-deployment.yaml`) usa esse
valor só como `secretKeyRef.name`/`secretKeyRef.key` (campos de
REFERÊNCIA do próprio Kubernetes, nunca `value:` direto):
```yaml
- name: CONFIGPROXY_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ required "..." .Values.chp.adminToken.existingSecretName }}
      key: {{ .Values.chp.adminToken.key }}
```

Evidência 3 -- `helm template` renderizado de verdade
(`--set chp.adminToken.existingSecretName=krewhub-chp-admin`, rodado ao
vivo pra esta verificação), mostrando que SÓ o nome do Secret aparece no
manifest final, nunca um valor de token:
```yaml
            - name: CONFIGPROXY_AUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: krewhub-chp-admin
                  key: token
```

Evidência 4 -- varredura por qualquer campo que aceitasse o valor
LITERAL em vez da referência (`grep -rniE "token:|secret:|password:|credential"`
em `values.yaml`, `examples/`, `templates/`, `README.md`): os únicos
resultados são os nomes de CHAVE do próprio Helm (`adminToken:`, a
chave YAML que agrupa `existingSecretName`+`key`) -- nenhuma ocorrência
de um campo tipo `token: <valor>`/`password: <valor>` em lugar nenhum do
chart. O `required()` do Helm falha exclusivamente por falta do NOME
(string vazia) -- ele não sabe nem tem como saber se o segredo real
dentro daquele Secret é válido; só garante que ALGUM nome de Secret foi
apontado antes de gerar um Deployment que dependeria de um `valueFrom`
vazio.

Mesma garantia vale, com a mesma evidência de padrão, pro Secret OIDC
(`krewhubCentral.oidc.existingSecretName`) e pro `imagePullSecretName` --
os três seguem exatamente esta mesma forma (nome + chave(s), nunca
valor).

### Nomes de recurso são FIXOS, não gerados por `<release>-<chart>`

Diferente do padrão usual de chart Helm (`{{ include "chart.fullname" }}`
gerando nomes tipo `krewhub-krewhub-central`), os nomes aqui são fixos
(`krewhub-central`, `configurable-http-proxy`, `krewhub-central-data`,
...) ou vêm direto de `values.yaml` -- de propósito, pra bater
EXATAMENTE com o que já está rodando. Selector labels dos Deployments
(`app: krewhub-central`/`app: configurable-http-proxy`) também
preservados como estão hoje, não trocados por
`app.kubernetes.io/name` -- selector de Deployment é IMUTÁVEL; usar um
selector diferente forçaria `kubectl delete` + recriação (downtime) se
este chart algum dia substituir o deploy atual.

### Validação -- `helm lint` + `helm template` comparado ao vivo, sem regressão

```bash
cd charts/krewhub
helm lint .                                                 # values genéricos (default) -- OK
helm template krewhub . --namespace <namespace-real>                 # FALHA de propósito: chp.adminToken.existingSecretName obrigatório
helm template krewhub . --namespace <namespace-real> \
  -f examples/values-family-cluster.yaml                    # override real do homelab -- renderiza limpo
```

`helm lint .` com values default: 0 chart(s) failed (só um aviso
informativo de `icon` ausente, não é erro -- o `required()` do CHP só
aparece como `[INFO] Missing required value` no lint, que não falha por
padrão; `helm template`/`helm install` SIM falham, com exit code 1 e a
mensagem completa -- testado ao vivo dos dois jeitos).

Comparação real (com o override `examples/values-family-cluster.yaml`,
que reproduz os valores reais do homelab): rodei `kubectl get <cada um
dos 9 recursos> -o yaml`
contra o cluster `<cluster-homelab>` (produção) e comparei campo a
campo contra a saída do `helm template` (normalizando só o que o
apiserver preenche sozinho -- `status`, `resourceVersion`,
`managedFields`, defaults de probe/`Pod`/`Service`/`PVC`, anotações do
`kubectl`/Flux). **Resultado: a ÚNICA diferença real em todos os 9
recursos são os labels novos que o Helm adiciona** (`helm.sh/chart`,
`app.kubernetes.io/managed-by`, `app.kubernetes.io/part-of`,
`app.kubernetes.io/version`) -- puramente aditivos, não removem nem
mudam nenhum label/selector existente. Zero diferença de spec real
(imagem, env vars, volumes, probes, resources, RBAC rules -- tudo
idêntico).

Validação adicional: `kubectl apply --dry-run=server -f
<helm-template-output>` contra o cluster real (server-side dry-run --
valida contra o apiserver de verdade SEM persistir nada). Resultado: os
9 recursos retornaram `configured (server dry run)` -- nenhum
`created`/erro -- confirmando que o apiserver reconhece cada um como
correspondendo EXATAMENTE a um recurso já existente (mesmo
kind+namespace+nome), ou seja, se este chart fosse aplicado de verdade
hoje seria um update in-place, não uma recriação.

### Template do pod-por-dev configurável via `values.yaml` + overlay JSON Patch por-cluster (chart `0.1.1`)

**Achado que motivou esta fatia**: o chart só expunha, como env var
parametrizável, `KREWHUB_DEV_NAMESPACE`/`KREWHUB_DB_PATH`/
`KREWHUB_SELF_HOST`/`KREWHUB_SELF_PORT`/o bloco OIDC. TODAS as outras
settings do template pod-por-dev (`KREWHUB_STORAGE_CLASS`,
`KREWHUB_STORAGE_SIZE`, `KREWHUB_BASE_DOMAIN`, `KREWHUB_KIROCREW_IMAGE`,
`KREWHUB_CHP_NAMESPACE`, `KREWHUB_CHP_ADMIN_PORT`, `KREWHUB_PUBLIC_PORT`)
caiam sempre no default de `app/config.py` (valores do homelab --
`rook-cephfs`, `kiro.internal`, `<namespace-real>`, `kirocrew:0.6.0`), e o
mecanismo de overlay JSON Patch novo (`app/overlay.py`, seção acima)
não tinha via de configuração NENHUMA pelo chart -- só dava pra ligar
editando o Deployment à mão (como o GitOps faz hoje, fora do chart).
Mudança 100% ADITIVA: nenhum default mudou, nenhum manifest do homelab
regrediu (ver "Validação" abaixo).

**Novos campos, `krewhubCentral.devPodTemplate`** (todos `""` por
default = env var correspondente OMITIDA do Deployment, o app cai no
default de `app/config.py`, exatamente como antes desta fatia):

| Campo (`values.yaml`) | Env var | Default do CÓDIGO (`app/config.py`) |
|---|---|---|
| `storageClass` | `KREWHUB_STORAGE_CLASS` | `rook-cephfs` |
| `storageSize` | `KREWHUB_STORAGE_SIZE` | `10Gi` |
| `baseDomain` | `KREWHUB_BASE_DOMAIN` | `kiro.internal` |
| `kirocrewImage` | `KREWHUB_KIROCREW_IMAGE` | `ghcr.io/kirodotdev/kirocrew:0.6.0` |
| `publicPort` | `KREWHUB_PUBLIC_PORT` | `8080` |
| `scheme` | `KREWHUB_DEV_POD_SCHEME` | `http` |
| `chpAdminPort` | `KREWHUB_CHP_ADMIN_PORT` | `8001` |

**Exceção deliberada -- `chpNamespace` / `KREWHUB_CHP_NAMESPACE`**: o
default do CÓDIGO é um placeholder genérico (`"krewhub"`, sem relação
com nenhum cluster real -- generalizado numa limpeza posterior, ver
Nota de rebrand no topo deste README),
mas o CHP *deste chart* sobe sempre em `.Release.Namespace` (ver
`chp-deployment.yaml`) -- e `KREWHUB_CHP_NAMESPACE` é usado em três
pontos que dependem de bater com onde o CHP REALMENTE está:
`namespaceSelector` da `NetworkPolicy` por-dev
(`app/k8s_templates.py::build_networkpolicy`), self-register do próprio
`krewhub-central` no CHP (`app/main.py::_self_register_route`) e a busca
do pod do CHP via exec (`app/chp_client.py::_find_chp_pod`). Depender do
default do CÓDIGO quebraria os três sempre que a release deste chart
não for instalada num namespace chamado literalmente `krewhub` -- por
isso, DIFERENTE dos campos acima, o chart SEMPRE seta
`KREWHUB_CHP_NAMESPACE` (`devPodTemplate.chpNamespace | default
.Release.Namespace`), nunca omite, independente de qual seja o default
do código. Override explícito ainda funciona, pro caso do CHP viver fora
deste chart/namespace.

**Overlay JSON Patch por-cluster via chart -- `krewhubCentral.devPodOverlay`**:
`{}` (default) = nenhum `ConfigMap` criado, `KREWHUB_DEV_POD_OVERLAY_PATH`
NÃO setado (mesmo comportamento de antes desta fatia). Preenchido com o
mesmo formato de documento que `app/overlay.py` espera
(`{recurso: [operações JSON Patch]}`, chaves `deployment`/`pvc`), o novo
template `templates/dev-pod-overlay-configmap.yaml` cria um `ConfigMap`
(`krewhub-dev-pod-overlay`), `templates/deployment.yaml` monta ele em
`/etc/krewhub/dev-pod-overlay` (`readOnly`) e seta
`KREWHUB_DEV_POD_OVERLAY_PATH=/etc/krewhub/dev-pod-overlay/dev-pod-overlay.yaml`
-- assim o `nodeAffinity`/`tolerations`/o que for específico de CADA
cluster entra via `values.yaml` na hora do `helm install`/`upgrade`, sem
editar `app/` nem o Deployment à mão (o jeito como o GitOps faz hoje,
fora do chart -- ver `dev-pod-overlay-configmap.yaml` no repo GitOps).

**Imagem default bumpada `sha-e006f15` -> `sha-91a483d`**: confirmado
(`git show <sha>:app/overlay.py`) que a imagem default ANTERIOR do
chart (`sha-e006f15`) **não continha `app/overlay.py`** -- foi
adicionado só no commit `91a483d` (o mais recente em HEAD no momento
desta fatia). Ligar `krewhubCentral.devPodOverlay` contra a imagem
antiga faria o `krewhub-central` quebrar no boot (`ImportError`). Sem
necessidade de rebuild: `sha-91a483d` já estava publicado no GHCR e
rodando ao vivo no homelab (`<namespace-real>/krewhub-central`, pod `Running`,
confirmado via `kubectl`/`podman manifest inspect` contra o registry
real) -- só foi preciso apontar `krewhubCentral.image.tag`/`appVersion`
pra ela.

**Nenhum valor de outro ambiente (EKS, `example-stg`, etc.)
hardcoded** -- os únicos lugares onde esses nomes aparecem são um
exemplo comentado em `values.yaml` (`storageClass: gp3`, ilustrativo,
igual já era feito pro `rook-cephfs`/homelab) e um values fictício
temporário usado só pra validar `helm template` nesta fatia (removido,
nunca commitado -- ver "Validação" abaixo).

#### Validação desta fatia

```bash
cd charts/krewhub
helm lint .                                                      # 0 chart(s) failed
helm template krewhub . --namespace <namespace-real> \
  -f examples/values-family-cluster.yaml                         # homelab -- ver diff abaixo
helm template krewhub . --namespace example-stg \
  -f <values fictícios de EKS, não commitados>                   # EKS genérico -- novas env/ConfigMap
```

**Diff do `helm template` do homelab, ANTES vs. DEPOIS desta fatia**
(mesmo `examples/values-family-cluster.yaml`, sem nenhum campo novo
preenchido): a Única diferença de SPEC real é **uma env var nova**,
`KREWHUB_CHP_NAMESPACE: "<namespace-real>"` (o comportamento correto e
equivalente ao default do CÓDIGO no namespace `<namespace-real>`, ver exceção
acima -- não é opção, é uma correção deliberada, não uma regressão), +
os labels de versão (`helm.sh/chart: krewhub-0.1.1`,
`app.kubernetes.io/version: "sha-91a483d"`) e a tag de imagem
(`sha-91a483d`) atualizados -- esperado, mesmo bump documentado acima.
Nenhum outro campo/env/volume mudou; nenhum `ConfigMap` novo apareceu
(porque `devPodOverlay` continua `{}` no exemplo do homelab).

**`helm template` com values fictícios de EKS** (`storageClassName: gp3`,
`devPodTemplate` com `storageClass: gp3`/`storageSize: 20Gi`/
`baseDomain: s.example.internal`/`kirocrewImage: .../custom-gateway`/
`publicPort: "8080"`/`chpAdminPort: "8001"`, `devPodOverlay` com um
`nodeSelector` fictício, `--namespace example-stg` sem override
de `chpNamespace`) -- renderiza limpo, mostrando:
- As 6 env vars novas (`KREWHUB_STORAGE_CLASS`, `KREWHUB_STORAGE_SIZE`,
  `KREWHUB_BASE_DOMAIN`, `KREWHUB_KIROCREW_IMAGE`, `KREWHUB_PUBLIC_PORT`,
  `KREWHUB_CHP_ADMIN_PORT`) com os valores do EKS fictício.
- `KREWHUB_CHP_NAMESPACE: "example-stg"` -- confirma o default
  `.Release.Namespace` funcionando sem nenhum override explícito.
- Um `ConfigMap krewhub-dev-pod-overlay` novo, com o documento overlay
  exato passado em `devPodOverlay`.
- `KREWHUB_DEV_POD_OVERLAY_PATH` setado + o volume/volumeMount novos no
  Deployment, apontando pro `ConfigMap` acima.
- Nenhum `nodeAffinity`/`storageClass`/domínio do HOMELAB (`rook-cephfs`,
  `kiro.internal`, `coruscant`/`tatooine`) em lugar nenhum da saída.

### O que falta pra promover isto a mecanismo de deploy real -- decisão pendente, não tomada aqui

Duas formas de o Flux consumir este chart, nenhuma decidida:

1. **`HelmRelease` do Flux** apontando pra um `GitRepository` (ou
   `OCIRepository`, se o chart for publicado como artefato OCI no
   ghcr.io, mesmo registry já usado pra imagem) referenciando este repo
   -- substituiria as duas `Kustomization`s atuais relacionadas a
   `krewhub-central` (nota: hoje **não existe** uma `Kustomization` do
   Flux dedicada a `krewhub-central`; achado desta fatia, ver abaixo) e
   `<namespace-real>-chp`.
2. **Manter a `Kustomization` atual**, só trocando o CONTEÚDO versionado
   de manifests brutos pelo resultado de `helm template` commitado (ou
   um `helmCharts:` inline do próprio Kustomize) -- muda menos a
   operação do dia a dia (segue sendo só Flux Kustomization), mas perde
   parametrização via `values.yaml` em tempo de reconcile.

**Achado colateral desta fatia, relevante pra essa decisão**: hoje
`krewhub-central` **não tem uma Flux `Kustomization` dedicada** -- só
existem `<namespace-real>-chp` (path `./clusters/<cluster>/<namespace-real>/chp`) e
`<namespace-real>-dev-testdev`. Os manifests de `<namespace-real>/krewhub-central/*.yaml`
são aplicados pela `Kustomization` **raiz** `flux-system`
(`path: ./clusters/<cluster>`, sem `kustomization.yaml` própria
nesse path -- o kustomize-controller gera uma implícita, achando
TODO `.yaml` recursivamente) -- confirmado pelo label
`kustomize.toolkit.fluxcd.io/name: flux-system` no Deployment ao vivo,
não algo como `<namespace-real>-krewhub-central`. Isso não é um bug urgente (está
funcionando), mas é uma inconsistência preexistente que qualquer uma das
duas opções acima resolveria de propósito.

Sem decisão tomada aqui -- aguardando confirmação antes de qualquer
`helm install`/`helm upgrade` ou troca da `Kustomization` vigente.

### Publicado no GHCR como OCI artifact -- `oci://ghcr.io/lucasces/charts/krewhub`

O chart está publicado (`helm push`, não `helm install`/`upgrade` --
segue valendo a mesma regra: nada em produção foi tocado). Fonte de
verdade do CÓDIGO do chart continua sendo `charts/krewhub/` neste repo
-- o pacote no GHCR é só uma distribuição versionada e imutável dele
(cada `helm push` de uma versão nova exige um `version:` novo em
`Chart.yaml`; sobrescrever uma tag já publicada não é o fluxo normal do
OCI, e o GHCR trata cada tag como conteúdo imutável).

**Versão publicada**: `0.1.0` -- decisão: mantive o default do `helm
create` como primeira versão publicada (nenhuma mudança de conteúdo
entre "criar o chart" e "publicar", não havia motivo pra já nascer em
`0.2.0`+). Daqui pra frente, incrementar `version:` em `Chart.yaml` a
cada mudança de template/values antes de publicar de novo -- é
independente de `appVersion` (que segue a tag da imagem do
krewhub-central).

**Path escolhido**: `oci://ghcr.io/lucasces/charts/krewhub` -- mesma
conta/namespace (`lucasces`) já usada pra imagem
(`ghcr.io/lucasces/krewhub-central`), só com um segmento `charts/` a
mais pra não colidir no mesmo namespace de pacotes com as imagens de
container (`helm push <pacote>.tgz oci://ghcr.io/lucasces/charts` --
o nome final do pacote, `krewhub`, vem do `name:` em `Chart.yaml`, o
Helm anexa automaticamente).

**Login**: reaproveitado o MESMO mecanismo já usado pra imagem --
`gh auth token | helm registry login ghcr.io -u lucasces --password-stdin`
(equivalente ao `podman login` já documentado, só que é o próprio Helm
quem guarda a credencial OCI, em `~/.config/helm/registry/`, não o
Podman). Nenhuma credencial nova foi criada.

**Achado colateral corrigido no caminho, não relacionado ao Helm**: o
`~/.config/containers/containers.conf` escrito na fatia anterior (smoke
em cluster efêmero, pra resolver `qemu-img`/`gvproxy`/`virtiofsd`
ausentes do `podman machine`) tinha sobrescrito `helper_binaries_dir`
de um jeito que quebrou o `podman` normal (`netavark` -- o backend de
rede -- deixou de ser encontrado, `podman login`/qualquer comando que
inicializa rede parava com `could not find "netavark"`). Corrigido
adicionando os paths de `netavark`/`aardvark-dns` (resolvidos via `nix
build`, mesmo mecanismo já usado) à mesma lista, sem remover as entradas
da fatia anterior -- `podman` (login/build/push de imagem) e `podman
machine` (smoke-test) continuam funcionando os dois.

**Publicado privado por padrão** -- mesma limitação já documentada pra
`krewhub-central` (troca de visibilidade via API do GitHub pra pacotes
de conta pessoal): `gh api /user/packages?package_type=container`
mostra `charts/krewhub` como `private`, ao lado de `krewhub-central`
(também `private`). Não tentei nenhum workaround -- é o mesmo
comportamento padrão já aceito antes, não um bloqueio de permissão de
push (o push em si funcionou de primeira, sem erro de permissão
nenhum). Se precisar tornar público (ex.: alguém instalar o chart sem
usar a conta `lucasces`), isso é feito manualmente na UI do GitHub
(Settings do pacote `charts/krewhub`), igual já é feito/documentado pra
`krewhub-central`.

**Verificação real, não assumida**: `helm pull
oci://ghcr.io/lucasces/charts/krewhub --version 0.1.0` numa pasta
separada (`/tmp/helm-pull-verify`, descartada depois) devolveu o mesmo
digest do push (`sha256:eb384972c9...`). Comparei o conteúdo extraído
do pacote puxado contra o source deste repo:
- `values.yaml`, `templates/`, `examples/` -- **diff vazio, byte a
  byte idênticos**.
- `Chart.yaml` -- única diferença é cosmética (`helm package`
  reserializa o YAML, remove comentários, reordena campos) -- mesmo
  conteúdo semântico (`name`/`version`/`appVersion`/`description`
  idênticos).
- **`helm template` rodado a partir do pacote puxado do GHCR e a partir
  do source local, com os MESMOS values (`examples/values-family-cluster.yaml`),
  produziu saída IDÊNTICA** (`diff` vazio) -- a prova mais forte de
  integridade: o que está publicado é exatamente o que está no repo,
  não uma versão divergente.

### Correção aplicada: `examples/` (config específica do homelab) viajava dentro do `.tgz` publicado -- republicado

A verificação acima (`values.yaml`/`templates/`/`examples/` idênticos)
provou integridade de publicação, mas não pegou um problema
DIFERENTE: o `.helmignore` gerado pelo `helm create` (nunca editado até
agora) não excluía `examples/` -- `helm package` empacota TUDO dentro
da pasta do chart por padrão, então `examples/values-family-cluster.yaml`
(valores reais do homelab `<cluster-homelab>`: `rook-cephfs`,
`krewhub.kiro.internal`, `ghcr-pull`, nomes reais dos três Secrets)
**viajou dentro da versão `0.1.0` publicada originalmente**
(`sha256:eb384972c9...`). Confirmado com evidência antes de corrigir:

```
$ tar tzf krewhub-0.1.0.tgz   # ANTES da correção
krewhub/Chart.yaml
krewhub/values.yaml
...
krewhub/examples/values-family-cluster.yaml   # <- não deveria estar aqui
```

Nada nesse arquivo é segredo real (é só nomes de Secret/StorageClass/
domínio, não os valores dos segredos em si -- ver seção anterior sobre
nome-do-Secret-vs-valor-do-Secret), mas ainda assim é config específica
de UM ambiente vazando dentro de um artefato que deveria ser
100% genérico.

**Correção**: adicionada a linha `examples/` ao
`charts/krewhub/.helmignore` (arquivo do scaffold `helm create`, só
tinha os padrões default de VCS/IDE/backup -- nunca tinha uma entrada
pra isso). Reempacotado e **republicado na MESMA tag `0.1.0`**
(decisão: como a versão tinha acabado de ser publicada, minutos antes,
sem ninguém dependendo dela ainda, sobrescrever a tag é mais limpo que
inflar pra `0.1.1` por causa de um erro de empacotamento -- o GHCR
aceitou o overwrite sem exigir nada especial). Digest mudou de
`sha256:eb384972c9...` pra `sha256:80c9270c50...`, confirmando que o
conteúdo publicado agora é outro.

**Reverificado do zero, contra o pacote JÁ CORRIGIDO no GHCR** (não só
localmente):
```
$ helm pull oci://ghcr.io/lucasces/charts/krewhub --version 0.1.0
Pulled: ghcr.io/lucasces/charts/krewhub:0.1.0
Digest: sha256:80c9270c505a2324666babf2ca61f376c3d6f7441a1e1e663a29518e88d9573b

$ tar tzf krewhub-0.1.0.tgz
krewhub/Chart.yaml
krewhub/values.yaml
krewhub/templates/NOTES.txt
krewhub/templates/_helpers.tpl
krewhub/templates/chp-deployment.yaml
krewhub/templates/chp-service.yaml
krewhub/templates/clusterrole.yaml
krewhub/templates/clusterrolebinding.yaml
krewhub/templates/deployment.yaml
krewhub/templates/dev-namespace.yaml
krewhub/templates/pvc.yaml
krewhub/templates/service.yaml
krewhub/templates/serviceaccount.yaml
krewhub/.helmignore
# examples/ -- AUSENTE, confirmado
```

Grep por qualquer valor específico de homelab dentro do conteúdo real
do pacote republicado (`grep -rniE "kiro\.internal|rook-cephfs|<cluster-homelab>|ghcr-pull|krewhub-oidc|chp-admin-token|coruscant|tatooine" krewhub/`,
rodado no pacote extraído): as únicas ocorrências restantes são 7
linhas de COMENTÁRIO dentro de `values.yaml`, todas explicitamente
rotuladas `# Exemplo usado no homelab...` -- nenhuma delas é um valor
efetivamente SETADO. Confirmado também parseando o `values.yaml`
publicado com `yaml.safe_load`: `storageClassName`, `selfHost`,
`imagePullSecretName` são todos `""`, `affinity`/`nodeSelector` são
`{}`, `tolerations` é `[]` -- os defaults reais são genéricos de
verdade, só a documentação em comentário cita o homelab como exemplo.
**Deixei essas 7 linhas de comentário como estão** (são documentação
explicitamente rotulada como exemplo, não config vazando) -- se
preferir que nem isso apareça, é um ajuste rápido a pedir.

Confirmação final, direto do artefato corrigido no GHCR: `helm template`
sem nenhum override continua falhando com o mesmo erro de `required()`
de antes -- prova de que o pacote é o chart genérico de verdade, não
uma versão com atalho do homelab embutido.

### Segunda correção: mesmo os COMENTÁRIOS mencionando o homelab foram removidos -- zero referência, nem em texto

Decisão do Lucas sobre o ponto que eu tinha deixado em aberto acima: as
7 linhas de comentário em `values.yaml` (`# Exemplo usado no homelab
<cluster-homelab>...`) e uma linha adicional que eu não tinha
verificado (`templates/clusterrole.yaml`, um comentário citando o path
literal `clusters/<cluster>/<namespace-real>/krewhub-central/clusterrole.yaml`
do repo GitOps) também tinham que sumir -- zero referência ao homelab
no pacote publicado, nem em texto/documentação.

**Linha do tempo completa desta fatia** (as duas rodadas de correção,
nenhuma omitida):

1. Publicação original (`0.1.0`, digest `sha256:eb384972c9...`): o
   arquivo INTEIRO `examples/values-family-cluster.yaml` viajava dentro
   do `.tgz` (achado da 1ª correção, ver seção acima).
2. 1ª correção (`0.1.0`, digest `sha256:80c9270c50...`): adicionado
   `examples/` ao `.helmignore`, republicado -- arquivo inteiro
   removido, mas restaram 7 linhas de COMENTÁRIO em `values.yaml`
   citando o homelab como "exemplo" (não eram valores setados, só
   documentação).
3. 2ª correção (esta, `0.1.0`, digest `sha256:9aa8f011bd...`): reescrevi
   as 7 linhas de comentário em `values.yaml` pra genéricas (ex.
   `selfHost: krewhub.seu-dominio.example` em vez de
   `krewhub.kiro.internal`; `storageClassName: minha-storage-class` em
   vez de `rook-cephfs`) -- SEM citar nenhum nome real de ambiente. Achei
   e corrigi também um comentário em `templates/clusterrole.yaml` que
   citava o path literal do GitOps (`clusters/<cluster>/<namespace-real>/...`),
   não pego pelo grep da 1ª correção porque a lista de termos usada
   antes não incluía `<cluster>`/`<namespace-real>` sozinhos.

**Verificação exaustiva desta 2ª correção**, mesmo procedimento de
antes (local -> package -> push -> pull de volta -> grep no artefato
REALMENTE publicado, não só local), com a lista de termos ampliada
(`<cluster>`, `<namespace-real>` adicionados, além dos já usados):

```
$ helm pull oci://ghcr.io/lucasces/charts/krewhub --version 0.1.0
Pulled: ghcr.io/lucasces/charts/krewhub:0.1.0
Digest: sha256:9aa8f011bd8eed7bf88b8c18e4317a77475a7538f123dc62c4bc3a1ee3ca0810

$ grep -rniE "kiro\.internal|rook-cephfs|<cluster-homelab>|ghcr-pull|krewhub-oidc|chp-admin-token|coruscant|tatooine|<cluster>|<namespace-real>" krewhub/
ZERO ocorrências -- OK

$ helm template krewhub ./krewhub --namespace <namespace-real>    # sem override
Error: execution error at (krewhub/templates/chp-deployment.yaml:62:27):
  chp.adminToken.existingSecretName é obrigatório quando chp.enabled=true...
```

**Zero ocorrência confirmada de verdade no artefato publicado** -- nem
arquivo, nem valor, nem comentário/texto. O chart segue rodando/lintando
normal (`helm lint`/`helm template -f examples/values-family-cluster.yaml`
localmente, sem regressão) -- só a documentação em comentário deixou de
citar o ambiente real, os exemplos continuam existindo, só genéricos.

### Como instalar direto do GHCR

```bash
# Descobrir versões publicadas
helm show chart oci://ghcr.io/lucasces/charts/krewhub --version 0.1.0

# Instalar (exemplo -- sempre passe SEUS próprios values, o default do
# chart é genérico de propósito, ver seções acima)
helm install krewhub oci://ghcr.io/lucasces/charts/krewhub \
  --version 0.1.0 \
  --namespace <namespace-real> --create-namespace \
  -f seus-values.yaml

# Ou só renderizar/inspecionar sem instalar
helm template krewhub oci://ghcr.io/lucasces/charts/krewhub \
  --version 0.1.0 -f seus-values.yaml
```

Lembrete que já vale pro chart local também: os Secrets referenciados
em `seus-values.yaml` (`chp.adminToken.existingSecretName` --
obrigatório -- e `krewhubCentral.oidc.existingSecretName`, opcional)
precisam já existir no cluster/namespace ANTES do `helm install` -- ver
seção "Confirmação explícita... nome do Secret vs. valor do Secret"
acima.

## Deployment vs Pod puro pro workload por-dev (migração aplicada)

**Histórico:** uma investigação anterior levantou esta mesma pergunta --
já que cada dev tem só 1 réplica (`replicas: 1`) e `/close`/`/logout` já
fazem o culling manual (não depende de crash-loop pra "reiniciar"), o
`Deployment` (`kirocrew-<slug>`) ainda se justifica, ou um `Pod` puro
(`restartPolicy: Always`) resolveria com menos objeto no cluster? --
mas concluiu "não migrar", com a justificativa central de que a chave
`deployment:` do overlay JSON Patch "já está em produção em dois
clusters, trocar quebra os dois". Avaliado de novo com o Lucas: essa
justificativa era **fraca** -- um rename mecânico coordenado (trocar
`deployment:` -> `pod:` e o path do patch nos dois overlays, no MESMO
commit conceitual que o código novo) não é um bloqueio real de
arquitetura, só trabalho de coordenação. **Migrado de verdade nesta
fatia.**

**Por que migrar:** nenhum dos recursos k8s pró-réplica que um
`Deployment`/`ReplicaSet` existe pra suportar fazia sentido aqui -- 1
pod = 1 dev = 1 gateway, sem scaling, sem rolling deploy de verdade
(bump de imagem já era documentado como restart/recreate manual, nunca
um `kubectl set image` orquestrado). Usar `Deployment` desde o início
era estranho dado isso.

**O que mudou:**

- `app/k8s_templates.py::build_deployment` -> `build_pod` -- gera um
  `Pod` (`apiVersion: v1, kind: Pod`) em vez de `Deployment`
  (`apps/v1`). O `spec.template.spec` de antes virou `spec` direto
  (mesmo conteúdo bit-a-bit -- containers/volumes/securityContext --
  só o nivelamento do wrapper PodTemplateSpec mudou), com
  `restartPolicy: Always`.
- `app/k8s_manager.py`: `ensure_deployment` -> `ensure_pod` (usa
  `CoreV1Api.{read,create,patch}_namespaced_pod` em vez de
  `AppsV1Api.*_namespaced_deployment`); `wait_for_ready` migrado de ler
  `status.ready_replicas` (via `read_namespaced_deployment_status`) pra
  ler o Pod direto (`read_namespaced_pod` -- **de propósito, não**
  `read_namespaced_pod_status`, pra não precisar de uma regra de RBAC
  nova pro subrecurso `pods/status`) e conferir `status.phase ==
  "Running"` E todo `container_statuses[].ready == True`;
  `teardown_dev_workload` deleta o Pod (`delete_namespaced_pod`) em vez
  do Deployment. `Clients` perdeu o campo `apps`
  (`client.AppsV1Api()`) -- nada mais no código usa essa API depois da
  migração.
- `app/main.py`: comentários/HTML de `/close`, `/logout` e o lobby
  atualizados de "Deployment" pra "Pod" (texto visto pelo dev).
- **Overlay JSON Patch (`app/overlay.py`, `KREWHUB_DEV_POD_OVERLAY_*`)
  -- BREAKING CHANGE:** a chave de topo do documento de overlay mudou
  de `deployment:` pra `pod:`, e os paths RFC 6902 mudaram de
  `/spec/template/spec/...` pra `/spec/...` (Pod não tem o wrapper
  PodTemplateSpec que Deployment tinha). Um overlay antigo com
  `deployment:` contra o código novo é **ignorado em silêncio**
  (`load_overlay_ops` só lê a chave `pod`) -- o Pod sobe sem a
  afinidade/tolerations configuradas, sem erro nenhum. Migrado nos DOIS
  overlays reais em produção, no MESMO commit conceitual que este
  código:
  - homelab (`<cluster-homelab>`,
    `clusters/<cluster>/<namespace-real>/krewhub-central/dev-pod-overlay-configmap.yaml`):
    `deployment: [{op: add, path: /spec/template/spec/affinity, ...}]`
    -> `pod: [{op: add, path: /spec/affinity, ...}]`.
  - `example-stg` (`deploy/example-stg/values-example-stg.yaml`,
    chave `krewhubCentral.devPodOverlay`): `deployment: [tolerations,
    nodeSelector em /spec/template/spec/...]` -> `pod: [mesmas duas
    operações em /spec/...]`.
- **RBAC (`charts/krewhub/templates/clusterrole.yaml` e
  `clusters/<cluster>/<namespace-real>/krewhub-central/clusterrole.yaml`,
  homelab):** removida a regra `apps/deployments`(+`deployments/status`);
  o recurso `pods` (já existia só com `get/list/watch`, usado pra achar
  o pod do CHP por label selector) ganhou `create`, `patch`, `update`,
  `delete` -- mesmo tratamento que `deployments` tinha antes, agora
  aplicado ao Pod do workload por-dev. Confirmado por leitura do código
  (não suposto) que nenhum outro lugar usava `AppsV1Api`/`apps/*` --
  seguro remover a regra inteira, não só esvaziar verbos.
- **Service/NetworkPolicy:** confirmado no código (`build_service`,
  `build_networkpolicy`) que os dois selecionam por LABEL
  (`app=kirocrew`, `krewhub.pespa.net/owner-slug=<slug>`), nunca por
  owner reference nem por nome/kind do controlador -- **não precisaram
  de nenhuma mudança**, o Pod puro com os mesmos labels é selecionado
  exatamente igual a antes.

**Trade-off aceito conscientemente -- documentado, não escondido:** um
`Deployment`/`ReplicaSet` dá recriação automática se o **Pod inteiro**
morrer (crash do processo do kubelet, `kubectl delete pod` acidental,
node caíndo) -- o controller recria sozinho, inclusive em outro nó. Um
`Pod` puro com `restartPolicy: Always` só cobre reinicio de CONTAINER
dentro do MESMO Pod (o kubelet reinicia o processo que crashou); se o
Pod inteiro sumir, ele **não volta sozinho** -- precisa de
`reconcile_dev`/`/provision` rodar de novo (hoje: só manual, culling
automático por inatividade ainda não existe, ver "Fora de escopo"
abaixo). Essa "rede de segurança" do Deployment quase não era
aproveitada na prática mesmo antes da migração (não há nada
automatizado chamando `/provision` sozinho hoje) -- a perda é mais
teórica que prática agora, mas é real e fica registrada aqui.
**Validado ao vivo** (ver seção "Validação ao vivo da migração Pod"
abaixo): um `kubectl delete pod kirocrew-<slug>` manual, por fora do
KrewHub, derruba o pod e ele **fica** derrubado -- nenhum controller
trouxe ele de volta -- confirmando o trade-off na prática, não só na
teoria.

## Fora de escopo desta fatia (não são bloqueios, são a próxima fatia)

- Culling por inatividade (prioridade sobe -- ver achado de contenção de
  CPU na seção do lobby, e agora o serviço fica de pé o tempo todo como
  Deployment em vez de só quando o operador lembra de rodar local).
- Exchange OIDC real ponta a ponta -- **discovery + `/login` +
  recepção de `code`/`state` no `/callback` testados ao vivo contra o
  Keycloak de um IdP real de terceiro** (ver seção "Exchange OIDC real (Decisão #3)"
  acima); só falta o Lucas completar o login de verdade no navegador
  pra exercitar o exchange (`code` -> token) com um `code` real -- não
  simulável programaticamente por design.
- ~~Autenticação dos próprios endpoints~~ -- **fechada nesta fatia**
  pros quatro endpoints que importavam (`lobby`, `provision`, `session`,
  `kiro-login`) via cookie/Bearer + checagem de `owner_id` (403
  cross-owner) -- ver seção "Autenticação dos próprios endpoints" acima
  pro mecanismo, o teste ao vivo, e os dois gaps remanescentes
  registrados explicitamente (`GET /devs` deixado aberto por decisão,
  `GET /devs/{owner_id}` e `GET /devs/{owner_id}/open` ainda não
  cobertos).
- ~~Smoke-test em cluster efêmero~~ -- **fechada nesta fatia**: engine
  plugável (`smoke/engines/`), `podman-machine` funcional (único viável
  neste host -- kind/k3d documentados como bloqueados por paredes
  estruturais do host, não como bug), ciclo completo rodado ao vivo com
  sucesso (provision -> CHP -> dashboard fake -> close -> logout ->
  cleanup) -- ver seção "Smoke-test em cluster efêmero" acima.
- ~~Empacotamento Helm~~ -- **chart criado e validado nesta fatia**
  (`charts/krewhub/`), `helm lint`/`helm template` sem erro, comparado
  campo a campo contra os 9 recursos ao vivo em produção (só diff:
  labels novos, aditivos) + `kubectl apply --dry-run=server` confirmando
  update in-place, não recriação -- **deploy em produção NÃO foi
  trocado** (segue via GitOps/Flux normal); promover a chart real
  (`HelmRelease` vs. manter `Kustomization`) é decisão pendente, não
  tomada -- ver seção "Empacotamento Helm" acima.
- ~~Publicação do chart no GHCR~~ -- **publicado nesta fatia** como OCI
  artifact (`oci://ghcr.io/lucasces/charts/krewhub`, versão `0.1.0`,
  mesmo login já usado pra imagem) -- verificado com `helm pull` numa
  pasta separada + `helm template` do pacote puxado idêntico ao do
  source (diff vazio). Publicado privado por padrão (mesma limitação já
  aceita pro `krewhub-central`). Nenhum `helm install`/`upgrade` em
  produção -- ver seção "Publicado no GHCR" acima.
- ~~Settings do template pod-por-dev não configuráveis pelo chart
  (`storageClass`/`baseDomain`/`kirocrewImage`/CHP namespace-porta) +
  overlay JSON Patch sem via de configuração pelo chart~~ -- **fechado
  nesta fatia** (chart `0.1.1`): `krewhubCentral.devPodTemplate.*` +
  `krewhubCentral.devPodOverlay` (`ConfigMap` opcional), 100% aditivo,
  `helm lint`/`helm template` revalidados sem regressão no homelab (só
  diff: `KREWHUB_CHP_NAMESPACE` novo, correção deliberada, + bump de
  versão/imagem) -- ver seção "Template do pod-por-dev configurável via
  `values.yaml`" acima. Chart **NÃO republicado** no GHCR nesta fatia
  (só o source deste repo foi alterado/commitado) -- publicar uma nova
  versão `0.1.1` no OCI registry fica pra quando for de fato promovido a
  mecanismo de deploy (ver seção "O que falta pra promover..." acima).

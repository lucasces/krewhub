# KrewHub central

Serviço que substitui o modo 100% manual usado nas fatias anteriores do
KrewHub (`kubectl exec` na mão, editar YAML no repo GitOps, `flux
reconcile` manual) por uma API que faz o reconcile do template
pod-por-dev via API do Kubernetes.

**Roda dentro do cluster desde a fatia de deploy** (namespace `kirohub`,
mesmo onde o CHP já roda) — ver seção "Deploy no cluster" abaixo pra todo
o detalhe (imagem, RBAC, como acessar). Continua dando pra rodar local
também (fora do cluster, lendo `~/.kube/config-personal`) pra iteração
rápida de código sem precisar rebuildar imagem a cada mudança —
`app/k8s_manager.py` tenta `load_incluster_config()` primeiro e cai pro
kubeconfig local se isso falhar.

> **Nota de rebrand (KiroHub -> KrewHub):** o serviço, código, env vars
> (`KIROHUB_*` -> `KREWHUB_*`), FastAPI app, docstrings, logger names e o
> arquivo SQLite (`kirohub.db` -> `krewhub.db`) foram renomeados nesta
> fatia. **Decisão deliberada de NÃO renomear** (documentada aqui, não
> assumida): o namespace k8s `kirohub` (onde o CHP já roda hoje e onde
> este serviço passa a rodar -- ver seção de deploy), os namespaces
> `kiro-dev-*` por dev, o diretório GitOps
> `clusters/family-cluster/kirohub/`, e o domínio `kiro.internal` --
> todos esses continuam com o nome antigo porque (a) `kiro.internal`,
> `kirocrew`, `kiro-cli`, `kiro-dev-*` são nomenclatura do **produto Kiro
> Crew que estamos hospedando**, não do nosso hub, então não fazem parte
> do rebrand por definição; e (b) o namespace `kirohub`/diretório GitOps
> são infraestrutura viva (CHP rodando, rotas registradas, Kustomization
> do Flux apontando pra lá) -- renomear exigiria deletar/recriar
> namespace (alto blast-radius, exige aprovação explícita, não assumida
> aqui). As seções "Provado ao vivo" anteriores a esta fatia preservam os
> valores de teste originais (ex.: `e2e-test@kirohub.local.test`) como
> registro histórico exato do que foi executado -- não foram reescritas.

## O que faz

- `POST /devs/{owner_id}/provision` — reconcile **idempotente**
  (create-se-não-existir / patch-se-já-existir) de: Namespace, Secret
  (`kiro-owner-id`), ConfigMap (`kiro-config`, incluindo
  `KIROCREW_CORS_ORIGINS` pro Host-header allowlist do dashboard), PVC
  (`kiro-workspace`, `rook-cephfs` RWO), Service, NetworkPolicy (só CHP
  alcança a porta 5476), e o Deployment `kirocrew` — com TODO o
  hardening já validado ao vivo nas fatias anteriores (nodeAffinity
  control-plane pro CSI cephfs, `seccompProfile: Unconfined` só no
  container pro sandbox `unshare(CLONE_NEWUSER)` funcionar, `fsGroup:
  1000`, `readOnlyRootFilesystem`, drop ALL caps). Depois espera o pod
  ficar Ready e registra a rota no CHP via `exec` no pod dele (a API de
  admin do CHP é loopback-only de propósito — ver
  `galaxy-far-far-away/clusters/family-cluster/kirohub/chp/deployment.yaml`
  — então falamos com ela de dentro do pod, nunca abrindo rede nova).
  Persiste `owner_id -> {namespace, host, status}` em SQLite
  (`krewhub.db`, MVP — nada de operator/CRD).
- `GET /login` — monta a authorization URL OIDC (Authorization Code +
  PKCE), reaproveitando a mesma lógica já provada em
  `galaxy-far-far-away/clusters/family-cluster/kirohub/oidc-client-poc/oidc_client.py`
  contra 3 issuers reais. 100% config-driven — sem `KREWHUB_OIDC_*`
  configurado, retorna 501 explícito. **Testado ao vivo contra o
  Keycloak da Somos** (Decisão #3, ver seção dedicada abaixo) — devolve
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
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9100
```

Config (todas opcionais, têm default seguro pra este cluster — exceto as
`KREWHUB_OIDC_*`, que ficam vazias de propósito nesta fatia):

| Env var | Default |
|---|---|
| `KREWHUB_KUBECONFIG` | `~/.kube/config-personal` |
| `KREWHUB_K8S_CONTEXT` | `galaxy-far-far-away` |
| `KREWHUB_DEV_NAMESPACE` | `krewhub-devs` (namespace ÚNICO e compartilhado onde TODOS os pods de dev vivem -- ver seção "Namespace único compartilhado pra pods de dev" abaixo; criado declarativamente no GitOps, não pelo reconcile) |
| `KREWHUB_BASE_DOMAIN` | `kiro.internal` |
| `KREWHUB_PUBLIC_PORT` | `8080` (porta local do port-forward do CHP hoje) |
| `KREWHUB_KIROCREW_IMAGE` | `ghcr.io/kirodotdev/kirocrew:0.6.0` |
| `KREWHUB_STORAGE_CLASS` | `rook-cephfs` |
| `KREWHUB_STORAGE_SIZE` | `10Gi` |
| `KREWHUB_CHP_NAMESPACE` | `kirohub` (nome do namespace em si -- ver nota de rebrand abaixo) |
| `KREWHUB_CHP_ADMIN_PORT` | `8001` |
| `KREWHUB_DB_PATH` | `./krewhub.db` |
| `KREWHUB_SESSION_TTL` | `24h` (passado a `kirocrew token --ttl`) |
| `KREWHUB_OIDC_ISSUER`/`_CLIENT_ID`/`_CLIENT_SECRET`/`_REDIRECT_URI`/`_SCOPES` | vazio (em cluster, vem do Secret `krewhub-oidc`, ver seção "Exchange OIDC real" abaixo) |
| `KREWHUB_SESSION_SECRET` | vazio -- segredo próprio do KrewHub pra assinar/validar o cookie/Bearer de sessão (ver seção "Autenticação dos próprios endpoints"); em cluster vem do Secret `krewhub-oidc`, chave `session-secret` |
| `KREWHUB_AUTH_TOKEN_TTL_SECONDS` | `86400` (24h) -- validade do cookie/token de sessão própria |
| `KREWHUB_KIRO_IDENTITY_PROVIDER` / `KREWHUB_KIRO_REGION` | vazio -- default só usado se a query (`mode=org`) não vier; sem nenhuma das duas fontes, 400 explícito |
| `KREWHUB_SELF_HOST` | vazio -- se setado, auto-registra a própria rota no CHP no startup (ver seção "Deploy no cluster") |
| `KREWHUB_SELF_PORT` | `8080` |

## Testes automatizados (suíte rápida, offline -- 109 testes, ~1.4s)

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
./test.sh              # roda tudo
./test.sh -k lobby      # só os testes que batem "lobby" (pytest -k)
./test.sh -x -v         # para no primeiro erro, verboso
```

`python3` não está no PATH por padrão neste NixOS (ver skill
`nix-develop`) -- `test.sh` bootstrapa um `.venv/` (via `nix develop
~/personal/nixos#node-22`, que inclui `python3.13`, só na primeira vez)
e instala `requirements.txt` + `requirements-dev.txt` (`pytest`,
`httpx2` -- só teste, não vão pra imagem: o `Dockerfile` só copia
`requirements.txt`) nele antes de rodar. Chamadas seguintes usam
`.venv/bin/python` direto (auto-contido, não precisa mais de `nix
develop`).

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
POST /devs/e2e-test%40kirohub.local.test/provision?wait=true
→ cria kiro-dev-e2e-test-kirohub-local-test do zero (7 recursos), pod Ready
→ registra e2e-test-kirohub-local-test.kiro.internal no CHP
→ curl -H "Host: e2e-test-kirohub-local-test.kiro.internal:8080" http://localhost:8080/api/health
  → {"ok": true}
```

Rodar de novo com o mesmo `owner_id`: os 7 passos voltam `"updated"` em
vez de duplicar — idempotência real, testada, não só declarada.

## Exchange OIDC real (Decisão #3 -- fechada)

Deliberadamente adiada em fatias anteriores ("fica pra depois, precisa
de client_id/secret reais"). Fechada nesta fatia contra um IdP real:
Keycloak da Somos (`https://auth.devops.somosdigital.io/auth/realms/master`),
client confidencial `krewhub` registrado pelo Lucas, `client_secret`
aplicado via `kubectl` direto num Secret `krewhub-oidc` no namespace
`kirohub` (chaves `client-id`/`client-secret`/`issuer`/`redirect-uri` --
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
   owner_id resolvido do claim: lucas.ces@somoseducacao.com.br
GET  /devs/lucas.ces%40somoseducacao.com.br/lobby     -> 200 (cookie recem-setado ja validado)
POST /devs/lucas.ces%40somoseducacao.com.br/lobby     -> 200 (mesmo cookie, form submetido)
   reconcile real: namespace krewhub-devs, 6 recursos criados, pod kirocrew Ready
   rota registrada no CHP: lucas-ces-somoseducacao-com-br.kiro.internal
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
`kirohub-krewhub-central.yaml` dedicada como `chp/`/`dev-testdev/` têm,
então quem aplica é o `flux-system` recursivo em `./clusters/family-cluster`
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
POST /devs/e2e-test%40kirohub.local.test/session
→ {"dashboard_url_with_token": "http://e2e-test-kirohub-local-test.kiro.internal:8080/?token=..."}

curl -i -H "Host: e2e-test-kirohub-local-test.kiro.internal:8080" \
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
(`clusters/family-cluster/kirohub/README.md`, seção "kiro-cli login").
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

POST /devs/e2e-test%40kirohub.local.test/kiro-login
     ?mode=org&identity_provider=https://somosdigital.awsapps.com/start&region=us-east-1
→ (≈4.1s) {"mode": "org", "already_logged_in": false,
           "verification_url": "https://somosdigital.awsapps.com/start/#/device?user_code=DSTF-VQPX",
           "user_code": "DSTF-VQPX"}

POST /devs/e2e-test%40kirohub.local.test/kiro-login?mode=personal
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
GET  /devs/lobby-test%40kirohub.local.test/lobby                       -> 200, form HTML
POST /devs/lobby-test%40kirohub.local.test/lobby (sem login_mode)      -> 400
POST ...                              (login_mode=bogus)               -> 400
POST ...                              (login_mode=org, sem provider/region) -> 400

POST /devs/lobby-test%40kirohub.local.test/lobby   login_mode=personal
-> 200, página HTML com:
   1. Dashboard do Kiro Crew
      http://lobby-test-kirohub-local-test.kiro.internal:8080/?token=...
   2. Login do kiro-cli
      Abra https://view.awsapps.com/start/#/device?user_code=HVGC-ZDJH ...
```

Confirmei cada peça de verdade, não só a resposta da API:
- `curl -H "Host: lobby-test-kirohub-local-test.kiro.internal:8080" http://localhost:8080/?token=...`
  através do CHP -> **200 OK** + `Set-Cookie: mc_token_8080=...` (sessão
  do dashboard real).
- Um único processo `kiro-cli login --use-device-flow` rodando no pod
  (sem duplicata), `kiro-cli whoami` ainda `Not logged in` (aguardando o
  clique, como esperado de fire-and-forget).
- `GET /devs/lobby-test%40kirohub.local.test` mostra
  `login_mode: "personal"` persistido (e `login_identity_provider`/
  `login_region` vazios, corretos pra esse modo).

**Achado operacional durante o teste (não é bug da fatia, é fato do
cluster):** o namespace novo (`kiro-dev-lobby-test-...`) ficou com o pod
**Pending** por alguns minutos -- `0/3 nodes are available: ... 2
Insufficient cpu`. O cluster `galaxy-far-far-away` já está com 2 dos 3
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

(b) owner com registro existente (lucas.ces@somoseducacao.com.br,
    login_mode=org salvo de um provision real anterior)
GET /devs/lucas.ces%40somoseducacao.com.br/lobby
  -> 200, PULA o form, direto pra pagina de resultado:
     "Voce ja esta logado no kiro-cli" (kiro-login idempotente, nenhum
     device-flow novo disparado) + link do dashboard com token novo +
     link "Reconfigurar sessao"

(c) mesmo owner de (b), forcando o form de novo
GET /devs/lucas.ces%40somoseducacao.com.br/lobby?reconfigure=1
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
(`https://auth.devops.somosdigital.io/auth/realms/master/protocol/openid-connect/logout`),
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
foi executado contra o pod real do `lucas.ces@somoseducacao.com.br` --
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

## Deploy no cluster (bloqueio de adoção fechado)

Até esta fatia, o serviço só rodava na máquina do operador
(`localhost:9100`), lendo `~/.kube/config-personal` -- ou seja, **só o
operador conseguia usar isso**. Isso foi o bloqueio real de adoção
priorizado nesta fatia (à frente de culling).

**Onde:** `clusters/family-cluster/kirohub/krewhub-central/` -- decisão
deliberada de **não** criar um diretório `krewhub/` novo no GitOps (nem
renomear `kirohub/` -> `krewhub/`): o CHP já roda em
`clusters/family-cluster/kirohub/chp/`, os Kustomizations do Flux
descobrem cada subdiretório automaticamente (mesmo padrão de `chp/` e
`dev-testdev/`, sem kustomization.yaml agregador no nível acima), e
colocar o serviço junto do CHP no mesmo namespace (`kirohub`) evita
qualquer necessidade de NetworkPolicy cross-namespace nova pra ele
alcançar o CHP. Renomear o diretório todo só por consistência de nome
não pagaria o churn (precisaria re-registrar o path em nada -- Flux
descobre por conteúdo, não por nome -- mas ainda seria um diff enorme e
sem ganho funcional).

**Imagem:** `ghcr.io/lucasces/krewhub-central:sha-f287446` (tag bump mais recente -- UX do lobby; a fatia original de deploy usou `sha-2a3ad7f`) -- `Dockerfile`
na raiz deste repo (`python:3.12-slim`, non-root uid 1000, só copia
`requirements.txt` + `app/`). Build/push feito com `podman` +
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
existe), `secrets`, `configmaps`, `services`, `persistentvolumeclaims`,
`deployments`(+`status`), `networkpolicies`
(`get/list/watch/create/patch/update`, **sem `delete`** -- o reconcile é
só create-ou-patch hoje) e `pods`(`get/list/watch`) +
`pods/exec`(`get`,`create`). **Achado ao vivo, não óbvio:** o client
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
(`krewhub.kiro.internal -> krewhub-central.kirohub.svc.cluster.local:8080`)
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
kubectl -n kirohub create secret docker-registry ghcr-pull \
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
kubectl -n kirohub rollout status deployment krewhub-central   -> 1/1 Ready
kubectl -n kirohub logs deploy/krewhub-central:
  k8s config: in-cluster (ServiceAccount)
  rota registrada host=krewhub.kiro.internal target=http://krewhub-central.kirohub.svc.cluster.local:8080 status=201

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
  `kirohub` onde vivem o CHP e o próprio `krewhub-central`. Decisão de
  manter separado (não reaproveitar `kirohub`): blast-radius de RBAC do
  `ServiceAccount`/`ClusterRole` do `krewhub-central` fica
  conceitualmente isolado da infra do hub, não por precisar de fronteira
  de rede (isso é RBAC/NetworkPolicy, não namespace boundary).
- **Criado declarativamente no GitOps**
  (`clusters/family-cluster/kirohub/krewhub-central/dev-namespace.yaml`),
  **não** pelo reconcile do app -- `ensure_dev_namespace` hoje só faz um
  `read_namespace` (GET) pra confirmar que existe, nunca cria nem edita.
  Isso permitiu **reduzir o RBAC**: o `ClusterRole` perdeu
  `create`/`patch`/`update` em `namespaces`, ficando só
  `get`/`list`/`watch` (ver seção RBAC acima) -- achado desta fatia,
  não era mais usado.
- **Nomes de recurso levam o slug do dev** pra coexistir no mesmo
  namespace sem colidir: `kiro-owner-id-<slug>` (Secret),
  `kiro-config-<slug>` (ConfigMap), `kiro-workspace-<slug>` (PVC),
  `kirocrew-<slug>` (Service + Deployment),
  `allow-chp-to-dashboard-only-<slug>` (NetworkPolicy).
- **Ponto crítico de segurança -- isolamento de rede não depende mais da
  fronteira do namespace.** Deployment/Service/NetworkPolicy de cada dev
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
verdade (`owner_id` = `lucas.ces@somoseducacao.com.br`, claim do Keycloak
da Somos) e, ao abrir
`http://lucas-ces-somoseducacao-com-br.kiro.internal:8080/?token=...` no
navegador, recebeu **404**. Hipótese inicial (não confirmada de cara):
regressão do RBAC reduzido ou da normalização do slug, por ser o primeiro
provision real depois dessas mudanças.

**Evidência coletada, nessa ordem, antes de mexer em qualquer coisa:**

1. `kubectl get all -n krewhub-devs` -- `Deployment`/`Service`/`Pod`
   `kirocrew-lucas-ces-somoseducacao-com-br` existiam, `1/1 Running`, no
   namespace compartilhado certo. **Descarta** "recurso não foi criado".
2. `kubectl logs deploy/krewhub-central -n kirohub` (filtrado por
   `somoseducacao`) mostrou o reconcile **completo e sem erro**:
   ```
   INFO:krewhub.k8s:reconcile owner_id=lucas.ces@somoseducacao.com.br namespace=krewhub-devs slug=lucas-ces-somoseducacao-com-br steps={'namespace': 'exists', 'secret': 'created', 'configmap': 'created', 'pvc': 'created', 'service': 'created', 'networkpolicy': 'created', 'deployment': 'created'}
   INFO:krewhub.chp:rota registrada host=lucas-ces-somoseducacao-com-br.kiro.internal target=http://kirocrew-lucas-ces-somoseducacao-com-br.krewhub-devs.svc.cluster.local:5476 status=201
   ```
   Nenhum 403/401 relacionado a esse `owner_id`, nenhum erro de RBAC nos
   sete steps do reconcile. **Descarta** "RBAC reduzido bloqueou alguma
   operação do provision".
3. Consulta direta na API admin do CHP (`kubectl exec` no pod do
   `configurable-http-proxy`, `GET /api/routes` com o token do Secret
   `chp-admin-token`) confirmou a rota registrada, host **exatamente**
   igual ao esperado, sem diferença de normalização (`.`/`@` -> `-`):
   ```
   "/lucas-ces-somoseducacao-com-br.kiro.internal": {
     "target": "http://kirocrew-lucas-ces-somoseducacao-com-br.krewhub-devs.svc.cluster.local:5476",
     "host": "lucas-ces-somoseducacao-com-br.kiro.internal"
   }
   ```
   **Descarta** "rota não registrada" e "bug de normalização de slug".
4. `ps aux | grep port-forward` revelou a causa real: os dois
   port-forwards locais ativos na porta 8080
   (`kubectl port-forward svc/krewhub-central 8080:8080` e um outro em
   `9300:8080`, ambos sobras de sessões anteriores testando os endpoints
   do próprio `krewhub-central`) apontavam **direto pro Service do
   `krewhub-central`**, não pro Service do CHP. `curl -H 'Host:
   lucas-ces-somoseducacao-com-br.kiro.internal' http://127.0.0.1:8080/`
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
  --context galaxy-far-far-away --namespace kirohub
```

**Testado ponta a ponta depois da correção**, pelo mesmo túnel (porta
local `8080`), com `Host` header real:

```
curl -H 'Host: lucas-ces-somoseducacao-com-br.kiro.internal' http://127.0.0.1:8080/
  -> 200 OK, HTML real do Kiro Crew (aiohttp, dashboard), não mais 404

curl -H 'Host: krewhub.kiro.internal' http://127.0.0.1:8080/
  -> 404 esperado (krewhub-central não tem handler pra "/"; rotas
     próprias como /login e /devs/... continuam funcionando -- já
     confirmado nas seções de OIDC e autenticação acima)
```

Fluxo `login -> lobby -> provision -> dashboard` confirmado de pé de
novo pra `lucas.ces@somoseducacao.com.br`.

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

Sintoma reportado: pro mesmo owner (`lucas.ces@somoseducacao.com.br`,
slug `lucas-ces-somoseducacao-com-br`, namespace compartilhado
`krewhub-devs`), o `kiro-cli login` foi reconhecido como já feito (cache
em `~/.local/share/kiro-cli/data.sqlite3` persistiu certo), **mas** a
sessão anterior do dashboard (tema, layout, etc.) sumiu, como se fosse
primeiro acesso -- mesmo supostamente sob o mesmo `$HOME` no mesmo PVC.
Investigado nesta ordem, sem presumir causa:

1. **`kubectl get pvc -n krewhub-devs`** -- só existe **UM** PVC pra esse
   owner: `kiro-workspace-lucas-ces-somoseducacao-com-br`, `Bound`, idade
   batendo com o provision original. **Descarta** "PVC órfão do namespace
   antigo" -- confirmado também que `kiro-dev-lucas-ces-somoseducacao-com-br`
   (o namespace que existiria no modelo pré-migração) nunca existiu: esse
   owner só foi provisionado depois da migração pra namespace
   compartilhado, não tem passado no modelo antigo.
2. **`describe pvc` + Deployment atual** -- o PVC `Used By` aponta pro
   pod atual (`kirocrew-lucas-ces-somoseducacao-com-br-9d5485d46-z9xjv`),
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

Duas camadas de teste já existiam: `test.sh` (pytest offline, tudo
mockado) e o smoke-test MANUAL contra o cluster REAL (`galaxy-far-far-away`,
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
.venv/bin/python smoke/run_smoke.py --list-engines
KREWHUB_SMOKE_K8S_ENGINE=podman-machine .venv/bin/python smoke/run_smoke.py
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
`galaxy-far-far-away`) se nenhum engine efêmero estiver disponível.
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

## Fora de escopo desta fatia (não são bloqueios, são a próxima fatia)

- Culling por inatividade (prioridade sobe -- ver achado de contenção de
  CPU na seção do lobby, e agora o serviço fica de pé o tempo todo como
  Deployment em vez de só quando o operador lembra de rodar local).
- Exchange OIDC real ponta a ponta -- **discovery + `/login` +
  recepção de `code`/`state` no `/callback` testados ao vivo contra o
  Keycloak da Somos** (ver seção "Exchange OIDC real (Decisão #3)"
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

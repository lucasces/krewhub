# AGENTS.md

Resumo operacional pra qualquer agente (Claude ou outro) trabalhar neste
repo. Não substitui o `README.md`, que é a fonte de verdade detalhada
(histórico de decisões, investigações, testes ao vivo) — este arquivo só
reúne o que economiza uma investigação, com pointers pro README pra
detalhe.

Nota de nomenclatura: o projeto se chamava `KiroHub`, foi renomeado pra
`KrewHub` (código, env vars, FastAPI app). `kiro.internal`, `kirocrew`,
`kiro-cli`, `kiro-dev-*` continuam com o nome antigo de propósito — é
nomenclatura do produto **Kiro Crew** (hospedado aqui), não deste hub. O
namespace k8s real e o diretório GitOps onde o CHP/`krewhub-central`
rodam também mantêm o nome antigo na infra viva (alto blast-radius pra
renomear), mas não aparecem mais hardcoded como default no código
(`KREWHUB_K8S_CONTEXT`/`KREWHUB_CHP_NAMESPACE` em `app/config.py` são
genéricos/vazios — ver README, Nota de rebrand).

## Ambiente

- Gerenciador de pacotes/venv é `uv` (`pyproject.toml` + `uv.lock`,
  commitado — fonte de verdade das deps; sem mais `requirements*.txt`).
  Já disponível no PATH neste host (nix-profile pessoal); se não
  estiver, `nix shell nixpkgs#uv` avulso resolve. `python3`/`pip` em si
  continuam fora do PATH por padrão neste NixOS (ver skill
  `nix-develop`), mas o `uv` não depende disso — baixa/gerencia o
  interpretador sozinho (`.python-version` pina `3.13`).
- `uv run pytest` roda a suíte direto — sem script wrapper. `uv run`
  sincroniza o `.venv/` a partir do lock sozinho antes de rodar (testado
  com `.venv/` apagado: `uv run pytest` recria e passa igual; é
  idempotente/silencioso em chamadas seguintes onde nada mudou), e
  funciona de qualquer cwd dentro do repo (acha `pyproject.toml` subindo
  o diretório, igual `git -C`).
  - `uv run pytest` — roda tudo
  - `uv run pytest -k <termo>` — só os testes que batem `<termo>`
  - `uv run pytest -x -v` — para no primeiro erro, verboso
- Suíte é offline (k8s/`kubectl exec`/CHP/OIDC mockados, só SQLite roda
  de verdade em `tmp_path`), não toca o cluster real. Rodar antes de
  considerar qualquer mudança pronta.
- Deps de dev (pytest, `httpx2`) ficam no grupo `[dependency-groups].dev`
  do `pyproject.toml` — `uv sync --no-dev` (usado no `Dockerfile`) as
  deixa de fora da imagem.

## Arquitetura (resumo — ver README pra detalhe e histórico)

- `krewhub-central` (FastAPI, `app/`) expõe login OIDC + lobby, e faz o
  reconcile idempotente de um workload por-dev sob demanda: `POST
  /devs/{owner_id}/provision` cria/atualiza Secret, ConfigMap, PVC,
  Service, NetworkPolicy e um `Pod` puro (não `Deployment` — ver
  gotchas), depois registra a rota no `configurable-http-proxy` (CHP) via
  `exec` no pod dele. Roteamento é por Host header, não por path.
- Todos os devs compartilham um único namespace
  (`KREWHUB_DEV_NAMESPACE`, default `krewhub-devs`), criado
  declarativamente no GitOps, não pelo reconcile. Isolamento é via
  `NetworkPolicy` com `podSelector` por label
  `krewhub.pespa.net/owner-slug=<slug>`, não por fronteira de namespace
  (seção "Namespace único compartilhado" no README).
- O chart Helm (`charts/krewhub/`) cobre só a infra estática
  (`krewhub-central` + CHP + o Namespace compartilhado). Os recursos
  por-dev são geridos em runtime por `app/k8s_manager.py`/
  `app/k8s_templates.py`, fora do lifecycle do Helm — `helm
  uninstall`/`upgrade` nunca tocam neles (seção "Empacotamento Helm").
- O deploy em produção segue via GitOps/Flux normal (repositório
  separado, fora deste repo); o chart ainda não é o mecanismo de deploy
  real.

## Gotchas técnicos

- API do CHP usa header `Authorization: token <valor>` (não `Bearer`) —
  `app/chp_client.py`. `Bearer` é o formato do token de sessão próprio do
  KrewHub, endpoint diferente.
- O dashboard do Kiro Crew valida `Host`/`Origin` contra
  `KIROCREW_CORS_ORIGINS`. Quando TLS termina na borda (LB/Ingress) e o
  backend interno é HTTP puro, `KREWHUB_DEV_POD_SCHEME=https` gera a
  allowlist com o scheme certo (há um exemplo real desse ajuste numa
  config de ambiente de terceiro, fora deste repo); sem isso o
  CSRF-origin check rejeita com 403 mesmo com o resto configurado certo.
- `kiro-cli login` dentro do pod precisa de TTY real — via `kubectl exec`
  sem TTY, o wizard descarta as flags ou não mostra o menu. A técnica
  (`app/kiro_login.py`, exposta via `POST /devs/{owner_id}/kiro-login`)
  roda um script dentro do pod que cria seu próprio pty (`pty.spawn`) e
  lê stdin de uma FIFO.
- O sandbox do Kiro Crew (`unshare(CLONE_NEWUSER)`) precisa de
  `seccompProfile: Unconfined` no container — sem isso, `EPERM`. Em nós
  hardened (ex.: Bottlerocket em EKS) que zeram
  `user.max_user_namespaces` por padrão, o mesmo `unshare` falha com
  `ENOSPC` — é sysctl do node, não do chart/app (um ambiente de terceiro
  contorna isso com um nodepool dedicado com o sysctl corrigido).
- `kind`/`k3d` não funcionam pra cluster de teste efêmero nesse tipo de
  ambiente NixOS: montam `/lib/modules` do host num path que não existe
  nesse layout, e esperam o socket do Podman num path fixo diferente do
  socket rootless (`$XDG_RUNTIME_DIR/podman/podman.sock`). `podman
  machine` (VM QEMU/KVM efêmera, k3s nativo dentro) funciona —
  `smoke/engines/podman_machine.py` cuida do pré-requisito de
  `gvproxy`/`qemu-img`/`virtiofsd` que a imagem do Nix não traz embutido.
- Build de imagem multi-arch (amd64+arm64) com `podman`: a connection do
  `podman machine` precisa estar rootful (`--connection <nome>-root`) —
  rootless não propaga binfmt pro build. O build atual do
  `krewhub-central` é single-arch (`podman build` simples + `gh auth
  token | podman login ghcr.io`); isso só entra em jogo se um build
  multi-arch for necessário no futuro.
- Pod puro (não `Deployment`) pro workload por-dev: se o Pod inteiro
  morrer (`kubectl delete pod`, crash do kubelet, node caindo), não
  recria sozinho — sem ReplicaSet/controller por trás (seção "Deployment
  vs Pod puro" no README).
- Overlay JSON Patch (`KREWHUB_DEV_POD_OVERLAY_PATH`/`_JSON`) usa chave
  de topo `pod:` (não `deployment:`, desde a migração pra Pod puro), com
  paths RFC 6902 relativos a `/spec/...` direto (sem o wrapper
  `template.spec` que `Deployment` tinha). Um overlay antigo com
  `deployment:` é ignorado em silêncio contra o código atual — o Pod
  sobe sem a afinidade/tolerations esperadas, sem erro.

## Convenções do projeto

- Nunca commitar nome de cliente/empresa/domínio de terceiro — nem em
  código, mensagem de commit ou comentário de config (`.gitignore`
  incluso). Configs de ambientes de terceiros ficam untracked (ex.: sob
  `deploy/<nome-do-ambiente>/`), sem entrada explícita no `.gitignore` —
  simplesmente nunca recebem `git add`.
- Chart Helm é genérico por design — nenhum valor default em
  `charts/krewhub/values.yaml` assume um cluster específico
  (StorageClass, domínio, topologia de nó, mecanismo de secret).
  Exemplos de valores reais vivem só em `charts/krewhub/examples/`,
  nunca como default, e são excluídos do `.tgz`/OCI artifact publicado.
- Overlay JSON Patch é o mecanismo pra qualquer peculiaridade de cluster
  (afinidade de nó, tolerations, storage class fixo) — não hardcode isso
  em `app/k8s_templates.py` (seção "Overlay JSON Patch por-cluster" no
  README).
- Nomes de recurso do chart são fixos (não gerados por
  `<release>-<chart>`).

"""API pública estável das extensões do KrewHub.

Uma extensão é um pacote Python independente que publica uma subclasse de
`Extension` no entry point `krewhub.extensions`:

    [project.entry-points."krewhub.extensions"]
    aws-sso = "krewhub_ext_aws_sso:AwsSsoExtension"

Este módulo é o ÚNICO contrato entre o core e as extensões -- tudo o que
está em `__all__` segue `API_VERSION` (semver só do contrato: mudança
incompatível sobe o major e o loader recusa extensões que declaram outro
major). A extensão importa daqui (`from app.extensions.base import ...`);
o pacote dela NÃO declara `krewhub` como dependência, porque o KrewHub
roda de um diretório (`[tool.uv] package = false`) e o módulo `app`
já está no `sys.path` do processo que carrega os entry points.

Modelo: parte DECLARATIVA (campos, sidecars via `pod_contribution`, ações,
condições que levam a `ready`) + hooks IMPERATIVOS opcionais (`status`,
`handle_action`, `lobby_card`, `on_pod_ready`). O core cuida de
persistência, Secret, ConfigMap, merge no Pod, CSRF e renderização --
a extensão nunca escreve HTML nem fala direto com SQLite ou com o
apiserver."""

from __future__ import annotations

import re
import secrets as _secrets
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

API_VERSION = "1.5"

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

STATES = ("inactive", "pending", "needs_action", "ready", "degraded", "error")

FIELD_KINDS = ("text", "select", "bool", "secret", "generated", "multiselect")


class ExtensionError(Exception):
    """Erro de uso/validação com mensagem segura pra mostrar ao dev."""


# ---------------------------------------------------------------------------
# Declarativo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """Campo de configuração da extensão.

    - `text`/`select`/`bool`: config comum, guardada em SQLite.
    - `secret`: informado pelo dev, write-only; vai direto pro Secret do
      k8s, NUNCA pro SQLite (lá só `{set, updated_at}`).
    - `generated`: gerado pelo KrewHub na primeira vez (token aleatório),
      nunca mostrado nem digitado -- só o Pod consome.
    - `multiselect`: só em `ActionSpec.params`. Caixas de marcar cujas
      opções a extensão devolve a cada `status()` em `Status.choices`
      (`"<ação>.<chave>"`); `handle_action` recebe uma tupla com os valores
      marcados, todos garantidamente entre as opções oferecidas. `required`
      exige pelo menos um.
    """

    key: str
    label: str = ""
    kind: str = "text"
    required: bool = False
    default: str = ""
    help: str = ""
    options: tuple[str, ...] = ()
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not _KEY_RE.match(self.key):
            raise ValueError(f"FieldSpec.key inválida: {self.key!r}")
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"FieldSpec.kind inválido: {self.kind!r}")
        if self.kind == "select" and not self.options:
            raise ValueError("FieldSpec kind='select' exige options")
        if self.kind in ("secret", "generated") and self.default:
            raise ValueError("campos secret/generated não têm default")


@dataclass(frozen=True)
class Choice:
    """Opção de um parâmetro `multiselect`: `value` é o que volta ao
    `handle_action`; `label` é só exibição (escapado pelo renderer);
    `checked` pré-marca a caixa."""

    value: str
    label: str = ""
    checked: bool = False


#: valor de um parâmetro de ação: texto, ou tupla de valores se `multiselect`
ActionParam = str | tuple[str, ...]


@dataclass(frozen=True)
class ActionSpec:
    """Botão que o dev pode acionar. `requires` lista condições que
    precisam estar verdadeiras pro botão aparecer habilitado."""

    id: str
    label: str
    requires: tuple[str, ...] = ()
    params: tuple[FieldSpec, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not _KEY_RE.match(self.id):
            raise ValueError(f"ActionSpec.id inválido: {self.id!r}")


#: onde o initContainer das ferramentas encontra o volume (gravável) pra copiar
#: os binários -- ver `ToolsSpec`
TOOLS_POPULATE_DIR = "/tools"
#: raiz, no `kirocrew`, dos volumes de ferramentas (`<raiz>/<id da extensão>`)
TOOLS_MOUNT_ROOT = "/opt/krewhub-ext"
#: pasta de skills do Kiro Crew (lida a cada invocação do agente)
SKILLS_ROOT = "/home/kirocrew/.kiro/skills"


@dataclass(frozen=True)
class ToolsSpec:
    """Binários que a extensão disponibiliza no container `kirocrew`.

    O `kirocrew` roda com filesystem raiz somente leitura, sem root e sem
    capabilities, então não dá pra instalar nada nele em runtime. O core
    monta o caminho inteiro a partir disto:

    1. um volume `emptyDir` `<id>-tools`;
    2. um initContainer `<id>-tools` (mesmo endurecimento do `kirocrew`:
       não-root, raiz somente leitura, sem capabilities) que roda
       `image`+`command` com o volume gravável em `TOOLS_POPULATE_DIR`;
    3. o mesmo volume, SOMENTE LEITURA, em `<TOOLS_MOUNT_ROOT>/<id>` no
       `kirocrew`, e `<TOOLS_MOUNT_ROOT>/<id>/<bin_dir>` no começo do
       `PATH` dele.

    O initContainer termina antes de qualquer container do Pod subir, então
    o binário já existe quando o `kirocrew` inicia (um sidecar não dá essa
    garantia: sidecars e container principal sobem em paralelo).

    - `command` copia os arquivos pra `TOOLS_POPULATE_DIR`; precisa terminar
      com `<bin_dir>/` preenchido. Use `tools_copy_command("/opt/x")`: a raiz
      do emptyDir é do root e o initContainer não é dono dela nem tem
      `CAP_FOWNER`, então um `cp -a` (ou qualquer `--preserve`) falha ao
      acertar data/permissão da raiz e o Pod entra em CrashLoopBackOff.
    - `size_limit`: teto do emptyDir (cópia por Pod, descartada com ele).
    - `skills`: nomes de skills do agente que `command` deixa em
      `<skills_dir>/<nome>/SKILL.md` (o diretório pode ter outros arquivos,
      como scripts). O core monta cada `<skills_dir>/<nome>` do volume,
      somente leitura, em `~/.kiro/skills/<nome>` -- mesmas regras de nome de
      `PodContribution.skills`. O conteúdo vem da IMAGEM (sem o limite de 1 MiB
      do ConfigMap); em troca, o hash do spec acompanha a tag da imagem, não o
      texto da skill. O `name:` do frontmatter não é checado aqui (o core não
      vê a imagem): a extensão testa o arquivo que embute.
    """

    image: str
    command: tuple[str, ...]
    bin_dir: str = "bin"
    size_limit: str = "512Mi"
    skills: tuple[str, ...] = ()
    skills_dir: str = "skills"


@dataclass
class PodContribution:
    """O que a extensão acrescenta ao Pod do dev.

    - `containers`: sidecars (dicts de container k8s). Nome precisa
      começar com o id da extensão. Sem readinessProbe (a readiness do
      Pod é global) e nunca o container `kirocrew`.
    - `init_containers`, `volumes`: idem (nomes prefixados pelo id).
    - `main_env`/`main_volume_mounts`: acrescentados ao container
      `kirocrew` (índice 0).
    - `files`: `{nome_do_arquivo: conteúdo}` -- o core cria o ConfigMap
      `krewhub-ext-files-<slug>` e um volume `<id>-files` (somente
      leitura) com esses arquivos; a extensão monta esse volume nos seus
      containers (`files_volume_name(ext_id)`).
    - `tools`: binários expostos no `kirocrew` (ver `ToolsSpec`).
    - `skills`: `{nome: conteúdo do SKILL.md}` -- instruções pro agente do
      `kirocrew`, guardadas no ConfigMap compartilhado (1 MiB no total por dev,
      somando `files` de todas as extensões): serve a skills pequenas e a
      extensões sem imagem. Skills grandes ou com vários arquivos vão pela
      imagem (`ToolsSpec.skills`). O core as monta (somente leitura) em
      `~/.kiro/skills/<nome>/SKILL.md`, onde o Kiro Crew as descobre sozinho;
      `<nome>` precisa ser o id da extensão ou começar com `<id>-`, e o
      frontmatter precisa trazer `name: <nome>`.
    - `annotations`: anotações extras do Pod.
    """

    containers: list[dict] = field(default_factory=list)
    init_containers: list[dict] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)
    main_env: list[dict] = field(default_factory=list)
    main_volume_mounts: list[dict] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    tools: ToolsSpec | None = None
    skills: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


def files_volume_name(ext_id: str) -> str:
    return f"{ext_id}-files"


def tools_copy_command(source_dir: str) -> tuple[str, ...]:
    """`ToolsSpec.command` que copia `source_dir` (dentro da imagem) pra
    `TOOLS_POPULATE_DIR`, mantendo links simbólicos e o bit de execução.

    Nada de `-a`/`--preserve`: o `cp` tentaria ajustar data/permissão da
    própria raiz do emptyDir, que é do root e não é do usuário do
    initContainer (sem `CAP_FOWNER` isso dá EPERM e o `cp` sai com 1). Sem
    preserve, só os arquivos criados pelo `cp` (do próprio usuário) são
    tocados e o modo de cada um vem da origem, filtrado pelo umask."""
    return ("sh", "-c", f"cp -dR {shlex.quote(source_dir)}/. {TOOLS_POPULATE_DIR}/")


def tools_mount_path(ext_id: str) -> str:
    return f"{TOOLS_MOUNT_ROOT}/{ext_id}"


def secret_name(slug: str) -> str:
    return f"krewhub-ext-{slug}"


def secret_key(ext_id: str, key: str) -> str:
    return f"{ext_id}.{key}"


def secret_env(env_name: str, slug: str, ext_id: str, key: str, *, optional: bool = False) -> dict:
    """Entrada de `env` de container lendo uma chave do Secret da extensão.
    Campos `generated` sempre existem (o core os cria antes do Pod);
    campos `secret` informados pelo dev podem faltar -- use
    `optional=True` pra o container subir mesmo assim (a variável só
    aparece após o Pod ser recriado com o valor já salvo)."""
    ref = {"name": secret_name(slug), "key": secret_key(ext_id, key)}
    if optional:
        ref["optional"] = True
    return {"name": env_name, "valueFrom": {"secretKeyRef": ref}}


def generate_secret() -> str:
    return _secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# UI declarativa -- a extensão devolve dados, o core renderiza (html.escape
# num lugar só, sem JS)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class Card:
    """Cartão server-rendered. `code` é um texto de destaque (ex.: código
    de device-flow). `links` só aceitam http(s) (o renderer descarta o
    resto). Ações NÃO vêm aqui: o core desenha os botões de
    `Extension.actions` conforme as condições.

    `polling=True` diz que a extensão espera algo FORA do lobby (ex.: o
    dev autorizar um login no navegador) e que o cartão deve recarregar
    sozinho até isso mudar. Só ligue enquanto espera: o recarregamento
    descarta o que o dev estiver digitando nos formulários das ações."""

    title: str
    state: str = "pending"
    summary: str = ""
    rows: tuple[tuple[str, str], ...] = ()
    links: tuple[Link, ...] = ()
    code: str = ""
    messages: tuple[str, ...] = ()
    polling: bool = False


@dataclass
class Status:
    """Resultado do hook `status`: condições (booleanos) + cartão. O
    estado é derivado das condições (`derive_state`) quando o hook não o
    fixa explicitamente."""

    conditions: dict[str, bool] = field(default_factory=dict)
    card: Card | None = None
    state: str | None = None
    #: opções dos parâmetros `multiselect` das ações, por `"<ação>.<chave>"`
    choices: dict[str, tuple[Choice, ...]] = field(default_factory=dict)


@dataclass
class ActionResult:
    ok: bool = True
    message: str = ""
    card: Card | None = None
    state_updates: dict[str, Any] = field(default_factory=dict)


def derive_state(
    conditions: Mapping[str, bool],
    *,
    ready_when: Sequence[str],
    degraded_when: Sequence[str] = (),
    base: Sequence[str] = ("pod.ready",),
) -> str:
    """Regra padrão de estado:

    - alguma condição `base` falsa  -> `pending` (Pod/sidecar ainda subindo)
    - alguma `degraded_when` verdadeira -> `degraded`
    - todas `ready_when` verdadeiras -> `ready`
    - senão -> `needs_action` (o dev precisa fazer algo)
    """
    if not all(conditions.get(c, False) for c in base):
        return "pending"
    if any(conditions.get(c, False) for c in degraded_when):
        return "degraded"
    if all(conditions.get(c, False) for c in ready_when):
        return "ready"
    return "needs_action"


# ---------------------------------------------------------------------------
# Contextos entregues aos hooks
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    """Entrada de `pod_contribution` -- roda ao montar o spec do Pod."""

    owner_id: str
    slug: str
    namespace: str
    config: Mapping[str, Any]
    settings: Any


@dataclass
class ExtensionContext:
    """Entrada dos hooks em runtime (Pod já existe).

    `exec(script, container=...)` roda `sh -c` num container do Pod DO DEV
    (default: o sidecar da própria extensão, `container_default`).
    `get_secret(key)` lê um valor do Secret da extensão (nunca vem do
    SQLite). `state` é um dict persistido em `dev_extensions.runtime_state_json`
    -- mudanças feitas nele são salvas pelo core ao fim do hook.
    """

    owner_id: str
    slug: str
    namespace: str
    pod_name: str
    config: Mapping[str, Any]
    state: dict[str, Any]
    base_conditions: Mapping[str, bool]
    settings: Any
    container_default: str
    _exec: Callable[[str, str], str]
    _get_secret: Callable[[str], str | None]
    _run_detached: Callable[[str, tuple, str, tuple, tuple, float], str] | None = None

    def exec(self, script: str, *, container: str | None = None) -> str:
        return self._exec(container or self.container_default, script)

    def get_secret(self, key: str) -> str | None:
        return self._get_secret(key)

    def run_detached(
        self,
        command: Sequence[str],
        *,
        tag: str,
        script: Sequence[tuple[str, str]] = (),
        done_markers: Sequence[str] = (),
        timeout: float = 15.0,
        container: str | None = None,
    ) -> str:
        """Roda `command` destacado sob um pty (processo interativo que
        continua depois do hook, ex.: device-flow esperando o clique do
        dev). `script` é `[(esperar_por, enviar), ...]`; o retorno é o log
        assim que todos os `done_markers` aparecerem. O container precisa
        de `python3`, `base64` e um /tmp gravável. `tag` só aceita
        `[a-z0-9_]`."""
        if self._run_detached is None:
            raise ExtensionError("run_detached indisponível neste contexto")
        return self._run_detached(
            container or self.container_default,
            tuple(command),
            tag,
            tuple(script),
            tuple(done_markers),
            timeout,
        )


# ---------------------------------------------------------------------------
# Classe base
# ---------------------------------------------------------------------------


class Extension:
    """Subclasse concreta define `id`, `name`, `fields`, `actions` e o
    que precisar dos hooks. Instâncias não guardam estado (uma por
    processo, compartilhada entre devs)."""

    api_version: str = API_VERSION
    id: str = ""
    name: str = ""
    description: str = ""
    fields: tuple[FieldSpec, ...] = ()
    actions: tuple[ActionSpec, ...] = ()
    #: nome do container sidecar principal (alvo default de `ctx.exec`) --
    #: por padrão o próprio id da extensão
    sidecar: str = ""

    # --- declarativo -----------------------------------------------------

    def pod_contribution(self, ctx: BuildContext) -> PodContribution:
        return PodContribution()

    def validate(self, config: Mapping[str, Any]) -> list[str]:
        """Erros (texto seguro) da configuração. O default só checa
        `required`/`select`/`pattern`; extensões acrescentam regras."""
        errors: list[str] = []
        for f in self.fields:
            if f.kind in ("secret", "generated"):
                continue
            value = config.get(f.key, f.default)
            if f.kind == "bool":
                continue
            value = "" if value is None else str(value).strip()
            label = f.label or f.key
            if f.required and not value:
                errors.append(f"{label}: obrigatório")
            elif value and f.kind == "select" and value not in f.options:
                errors.append(f"{label}: valor inválido")
            elif value and f.pattern and not re.fullmatch(f.pattern, value):
                errors.append(f"{label}: formato inválido")
        return errors

    # --- hooks imperativos ----------------------------------------------

    def status(self, ctx: ExtensionContext) -> Status:
        """Condições + cartão. Chamado só com a extensão habilitada e o
        Pod pronto (o core devolve `pending`/`inactive` antes disso)."""
        return Status(conditions={}, card=Card(title=self.name, state="ready"), state="ready")

    def handle_action(self, ctx: ExtensionContext, action_id: str, params: Mapping[str, ActionParam]) -> ActionResult:
        raise ExtensionError(f"ação desconhecida: {action_id}")

    def lobby_card(self, ctx: ExtensionContext) -> Card | None:
        """Cartão do lobby, quando difere do cartão do `status`. `None`
        (default) = o core usa o cartão devolvido por `status`."""
        return None

    def on_pod_ready(self, ctx: ExtensionContext) -> None:
        """Chamado uma vez por provisionamento, depois que o Pod fica
        pronto. Melhor esforço: exceção só é logada."""

    # --- helpers do core -------------------------------------------------

    @property
    def sidecar_name(self) -> str:
        return self.sidecar or self.id

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}

    def action_map(self) -> dict[str, ActionSpec]:
        return {a.id: a for a in self.actions}

    def check_definition(self) -> None:
        """Valida a definição da própria classe (id, campos, ações)."""
        if not _ID_RE.match(self.id or ""):
            raise ValueError(f"id de extensão inválido: {self.id!r}")
        keys = [f.key for f in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError(f"extensão {self.id}: chaves de campo duplicadas")
        for f in self.fields:
            if f.kind == "multiselect":
                raise ValueError(f"extensão {self.id}: multiselect só vale em parâmetros de ação ({f.key})")
        ids = [a.id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"extensão {self.id}: ids de ação duplicados")


__all__ = [
    "API_VERSION",
    "STATES",
    "ActionParam",
    "ActionResult",
    "ActionSpec",
    "BuildContext",
    "Card",
    "Choice",
    "Extension",
    "ExtensionContext",
    "ExtensionError",
    "FieldSpec",
    "Link",
    "PodContribution",
    "SKILLS_ROOT",
    "Status",
    "TOOLS_MOUNT_ROOT",
    "TOOLS_POPULATE_DIR",
    "ToolsSpec",
    "derive_state",
    "files_volume_name",
    "generate_secret",
    "secret_env",
    "secret_key",
    "secret_name",
    "tools_copy_command",
    "tools_mount_path",
]

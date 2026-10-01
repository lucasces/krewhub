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
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

API_VERSION = "1.0"

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

STATES = ("inactive", "pending", "needs_action", "ready", "degraded", "error")

FIELD_KINDS = ("text", "select", "bool", "secret", "generated")


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
    - `annotations`: anotações extras do Pod.
    """

    containers: list[dict] = field(default_factory=list)
    init_containers: list[dict] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)
    main_env: list[dict] = field(default_factory=list)
    main_volume_mounts: list[dict] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


def files_volume_name(ext_id: str) -> str:
    return f"{ext_id}-files"


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
    `Extension.actions` conforme as condições."""

    title: str
    state: str = "pending"
    summary: str = ""
    rows: tuple[tuple[str, str], ...] = ()
    links: tuple[Link, ...] = ()
    code: str = ""
    messages: tuple[str, ...] = ()


@dataclass
class Status:
    """Resultado do hook `status`: condições (booleanos) + cartão. O
    estado é derivado das condições (`derive_state`) quando o hook não o
    fixa explicitamente."""

    conditions: dict[str, bool] = field(default_factory=dict)
    card: Card | None = None
    state: str | None = None


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
    _run_detached: Callable[..., str] | None = None

    def exec(self, script: str, *, container: str | None = None) -> str:
        return self._exec(container or self.container_default, script)

    def get_secret(self, key: str) -> str | None:
        return self._get_secret(key)

    def run_detached(self, flow: Any) -> str:
        """Executa um `pod_exec.DetachedFlow` (pty+FIFO) no Pod do dev."""
        if self._run_detached is None:
            raise ExtensionError("run_detached indisponível neste contexto")
        return self._run_detached(flow)


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

    def handle_action(self, ctx: ExtensionContext, action_id: str, params: Mapping[str, str]) -> ActionResult:
        raise ExtensionError(f"ação desconhecida: {action_id}")

    def lobby_card(self, ctx: ExtensionContext) -> Card | None:
        """Cartão extra do lobby; default: o cartão do `status`."""
        return self.status(ctx).card

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
        ids = [a.id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"extensão {self.id}: ids de ação duplicados")


__all__ = [
    "API_VERSION",
    "STATES",
    "ActionResult",
    "ActionSpec",
    "BuildContext",
    "Card",
    "Extension",
    "ExtensionContext",
    "ExtensionError",
    "FieldSpec",
    "Link",
    "PodContribution",
    "Status",
    "derive_state",
    "files_volume_name",
    "generate_secret",
    "secret_env",
    "secret_key",
    "secret_name",
]

"""Extensão AWS SSO: credenciais temporárias do IAM Identity Center no Pod
do dev, via `aws-sso-cli` rodando como sidecar.

O `kirocrew` também recebe o `aws` CLI v2 (initContainer + volume somente
leitura, ver `ToolsSpec`) e a skill `aws-sso` em `~/.kiro/skills`, pra o agente
saber usar as credenciais sem configuração manual.

Vários papéis ao mesmo tempo: o dev marca um ou mais papéis no cartão. O
primeiro é o PADRÃO (endpoint `/`, sem perfil: é o que `aws` usa sozinho); cada
papel ativo ganha também um perfil nomeado `<conta>:<papel>` num arquivo
gerenciado (`AWS_CONFIG_FILE`, num emptyDir que o sidecar escreve e o
`kirocrew` lê), cujo `credential_process` busca o slot do papel no mesmo
servidor. Com `AWS_CONFIG_FILE` apontando pra esse arquivo, o `~/.aws/config`
pessoal do dev NÃO é lido pelo CLI (sem mesclagem).

Fluxo: o sidecar `aws-sso` sobe o `aws-sso ecs server` em 127.0.0.1:4144
(protegido por bearer token gerado pelo KrewHub). O container `kirocrew`
recebe `AWS_CONTAINER_CREDENTIALS_FULL_URI` + `AWS_CONTAINER_AUTHORIZATION_TOKEN`,
então qualquer SDK/CLI da AWS busca as credenciais ali -- nada de chave
estática em disco. O token SSO fica num emptyDir PRIVADO do sidecar
(`aws-sso-state`), que o `kirocrew` não monta; some junto com o Pod e o
login é refeito a cada recriação.

Protocolo com o supervisor (`extensions/aws-sso/supervisor.py`): o
supervisor é o ÚNICO escritor de `/state/status.json`; as ações só
escrevem arquivos de pedido em `/state/req/`."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from typing import Any, Mapping

from app.extensions.base import (
    ActionParam,
    ActionResult,
    ActionSpec,
    BuildContext,
    Card,
    Choice,
    Extension,
    ExtensionContext,
    ExtensionError,
    FieldSpec,
    Link,
    PodContribution,
    Status,
    ToolsSpec,
    derive_state,
    files_volume_name,
    secret_env,
    tools_copy_command,
    tools_mount_path,
)


logger = logging.getLogger("krewhub.ext.aws-sso")

EXT_ID = "aws-sso"
SIDECAR = "aws-sso"
PORT = 4144
STATE_DIR = "/state"
#: AWS CLI v2 embutido na imagem do sidecar; o initContainer `aws-sso-tools`
#: copia isto pro volume que o `kirocrew` monta (ver `ToolsSpec`)
TOOLS_SOURCE_DIR = "/opt/krewhub-tools"
#: skill do agente, entregue pela imagem (extensions/aws-sso/skills/<nome>/SKILL.md)
SKILL_NAME = "aws-sso"
#: `credential_process` dos perfis (extensions/aws-sso/credential_process.py), em `<tools>/bin`
HELPER_BIN = "krewhub-aws-sso-creds"
CONFIG_DIR = "/etc/aws-sso"
#: emptyDir dos perfis: o sidecar grava em PROFILES_DIR, o kirocrew lê (RO) em PROFILES_MOUNT
PROFILES_VOLUME = "aws-sso-profiles"
PROFILES_DIR = "/profiles"
PROFILES_MOUNT = "/etc/krewhub/aws-sso"
#: papéis ativos ao mesmo tempo (o mesmo limite vale no supervisor)
MAX_ROLES = 10
#: imagem do sidecar e do initContainer; sem valor padrão de propósito --
#: uma tag assumida que não existe no registro deixaria o Pod em
#: ImagePullBackOff, então a extensão se recusa a ficar ativa sem ela
IMAGE_ENV = "KREWHUB_EXT_AWS_SSO_IMAGE"
IMAGE_MISSING_ERROR = (
    f"a imagem do sidecar não está configurada: defina {IMAGE_ENV} no ambiente do KrewHub "
    "(Helm: krewhubCentral.extensions.env) com a imagem krewhub-ext-aws-sso publicada"
)


def _image() -> str:
    return os.environ.get(IMAGE_ENV, "").strip()

LOGIN_TAG = "awssso_login"
LOGIN_TIMEOUT = 20.0
LOGIN_URL_STALE_SECONDS = 15 * 60

_START_URL_RE = r"https://[A-Za-z0-9.-]+\.(awsapps\.com|amazonaws\.com)(/[^\s]*)?"
_REGION_RE = r"[a-z]{2}(-[a-z]+)+-\d"
PROFILE_FORMAT = "{{ .AccountIdPad }}:{{ .RoleName }}"
_PROFILE_RE = re.compile(r"^\d{12}:[A-Za-z0-9_+=,.@-]{1,64}$")
_URL_IN_LOG = re.compile(r"https://[^\s\"'<>]+")
_CODE_IN_LOG = re.compile(r"(?:user_code=|[Cc]ode[: ]+)([A-Z0-9]{4}-[A-Z0-9]{4})")


def render_config(config: Mapping[str, Any]) -> str:
    """`config.yaml` do aws-sso-cli. `AuthWorkflow` fica no nível global
    (dentro de `SSOConfig` o aws-sso ignora a chave e cai no PKCE, que
    exige um navegador na máquina do dev); `UrlAction: print` imprime a
    URL em vez de abrir um navegador inexistente.

    `ProfileFormat` fixa o nome do perfil como `<id da conta com 12
    dígitos>:<papel>`. O default do aws-sso-cli usa o NOME da conta, que
    pode ter parênteses, acentos e o que mais o admin da organização
    digitou; o id (`AccountIdPad`, zero-preenchido) e o
    nome do papel IAM (`[\\w+=,.@-]`, até 64) têm alfabeto fechado, então o
    perfil é validável por regex estrita e seguro de passar adiante."""
    q = json.dumps
    return (
        "SSOConfig:\n"
        "  default:\n"
        f"    StartUrl: {q(str(config['start_url']).strip())}\n"
        f"    SSORegion: {q(str(config['sso_region']).strip())}\n"
        f"    DefaultRegion: {q(_default_region(config))}\n"
        "DefaultSSO: default\n"
        "SecureStore: json\n"
        "UrlAction: print\n"
        "AuthWorkflow: device_code\n"
        f"ProfileFormat: {q(PROFILE_FORMAT)}\n"
    )


def _default_region(config: Mapping[str, Any]) -> str:
    return str(config.get("default_region") or config["sso_region"]).strip()


def _quote_file(path: str, content: str) -> str:
    return f"mkdir -p {shlex.quote(os.path.dirname(path))} && printf %s {shlex.quote(content)} > {shlex.quote(path)}"


class AwsSsoExtension(Extension):
    id = EXT_ID
    name = "AWS SSO"
    description = (
        "Credenciais temporárias da AWS (IAM Identity Center) para o ambiente, "
        "sem chaves estáticas. O login é refeito a cada recriação do Pod."
    )
    sidecar = SIDECAR
    fields = (
        FieldSpec(
            "start_url",
            "URL do portal AWS SSO",
            required=True,
            pattern=_START_URL_RE,
            help="Ex.: https://d-xxxxxxxxxx.awsapps.com/start",
        ),
        FieldSpec("sso_region", "Região do IAM Identity Center", required=True, pattern=_REGION_RE),
        FieldSpec(
            "default_region",
            "Região padrão dos comandos AWS",
            pattern=_REGION_RE,
            help="Se vazio, usa a região do Identity Center.",
        ),
        FieldSpec("bearer", kind="generated"),
    )
    actions = (
        ActionSpec(
            "start_login",
            "Iniciar login",
            requires=("sidecar.aws-sso.running",),
            description="Gera um código de dispositivo; autorize no portal AWS.",
        ),
        ActionSpec(
            "refresh_roles",
            "Atualizar contas e papéis",
            requires=("sso.logged_in",),
        ),
        ActionSpec(
            "apply_roles",
            "Usar estes papéis",
            requires=("sso.logged_in",),
            description=(
                f"Marque até {MAX_ROLES} papéis. O primeiro da lista é o padrão do AWS CLI; "
                "os demais ficam disponíveis como perfis nomeados."
            ),
            params=(FieldSpec("profiles", "Papéis (conta:papel)", kind="multiselect", required=True),),
        ),
        ActionSpec(
            "reload_creds",
            "Recarregar credenciais",
            requires=("sso.role_selected",),
        ),
    )

    def validate(self, config: Mapping[str, Any]) -> list[str]:
        errors = super().validate(config)
        if not _image():
            errors.append(IMAGE_MISSING_ERROR)
        return errors

    # --- Pod -------------------------------------------------------------

    def pod_contribution(self, ctx: BuildContext) -> PodContribution:
        cfg = ctx.config
        image = _image()
        if not image:
            raise ExtensionError(IMAGE_MISSING_ERROR)
        files_vol = files_volume_name(EXT_ID)
        sidecar = {
            "name": SIDECAR,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "env": [
                {"name": "HOME", "value": f"{STATE_DIR}/home"},
                {"name": "AWS_SSO_HELPER", "value": f"{tools_mount_path(EXT_ID)}/bin/{HELPER_BIN}"},
                secret_env("KREWHUB_AWS_SSO_TOKEN", ctx.slug, EXT_ID, "bearer"),
            ],
            "volumeMounts": [
                {"name": "aws-sso-state", "mountPath": STATE_DIR},
                {"name": "aws-sso-tmp", "mountPath": "/tmp"},
                {"name": files_vol, "mountPath": CONFIG_DIR, "readOnly": True},
                {"name": PROFILES_VOLUME, "mountPath": PROFILES_DIR},
            ],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "10m", "memory": "48Mi"},
                "limits": {"memory": "192Mi"},
            },
        }
        region = _default_region(cfg)
        return PodContribution(
            containers=[sidecar],
            volumes=[
                {"name": "aws-sso-state", "emptyDir": {}},
                {"name": "aws-sso-tmp", "emptyDir": {}},
                {"name": PROFILES_VOLUME, "emptyDir": {}},
            ],
            main_volume_mounts=[{"name": PROFILES_VOLUME, "mountPath": PROFILES_MOUNT, "readOnly": True}],
            main_env=[
                secret_env("KREWHUB_AWS_SSO_TOKEN", ctx.slug, EXT_ID, "bearer"),
                {"name": "AWS_CONTAINER_AUTHORIZATION_TOKEN", "value": "Bearer $(KREWHUB_AWS_SSO_TOKEN)"},
                {"name": "AWS_CONTAINER_CREDENTIALS_FULL_URI", "value": f"http://127.0.0.1:{PORT}/"},
                {"name": "AWS_CONFIG_FILE", "value": f"{PROFILES_MOUNT}/config"},
                {"name": "AWS_REGION", "value": region},
                {"name": "AWS_DEFAULT_REGION", "value": region},
            ],
            files={"config.yaml": render_config(cfg)},
            tools=ToolsSpec(
                image=image,
                command=tools_copy_command(TOOLS_SOURCE_DIR),
                skills=(SKILL_NAME,),
            ),
        )

    # --- status ----------------------------------------------------------

    def _read_status(self, ctx: ExtensionContext) -> dict[str, Any]:
        raw = ctx.exec(f"cat {STATE_DIR}/status.json 2>/dev/null || true").strip()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError as exc:
            logger.warning("status.json ilegível no sidecar (%s); assumindo estado vazio", exc)
            data = {}
        return data if isinstance(data, dict) else {}

    def status(self, ctx: ExtensionContext) -> Status:
        st = self._read_status(ctx)
        selected = _str_list(st.get("profiles"))
        loaded = set(_str_list(st.get("loaded_profiles")))
        labels = st.get("role_labels") if isinstance(st.get("role_labels"), dict) else {}
        conditions = dict(ctx.base_conditions)
        conditions["sso.server"] = bool(st.get("server"))
        conditions["sso.logged_in"] = bool(st.get("logged_in"))
        conditions["sso.role_selected"] = bool(selected)
        conditions["sso.creds_loaded"] = bool(st.get("loaded"))
        state = derive_state(
            conditions,
            ready_when=("sso.server", "sso.logged_in", "sso.creds_loaded"),
        )

        rows: list[tuple[str, str]] = []
        for i, profile in enumerate(selected):
            tags = []
            if len(selected) > 1 and i == 0:
                tags.append("padrão")
            if profile not in loaded and state != "pending":
                tags.append("sem credenciais")
            rows.append(("Papel", f"{profile} ({', '.join(tags)})" if tags else profile))
            if labels.get(profile):
                rows.append(("Conta", str(labels[profile])))
        if st.get("roles"):
            rows.append(("Papéis disponíveis", str(st["roles"])))
        roles = _str_list(st.get("role_names"))
        links: tuple[Link, ...] = ()
        code = ""
        polling = False
        messages: list[str] = []

        if state == "ready":
            summary = "Credenciais carregadas. SDKs e CLI da AWS já as usam automaticamente."
            if len(selected) > 1:
                messages.append(
                    f"O padrão é {selected[0]}. Para os demais, use `--profile <conta:papel>` ou AWS_PROFILE."
                )
        elif state == "pending":
            summary = "Aguardando o ambiente subir."
        elif not conditions["sso.logged_in"]:
            summary = "Faça login para liberar as credenciais."
            login = ctx.state.get("login") or {}
            if login.get("url") and time.time() - float(login.get("at", 0)) < LOGIN_URL_STALE_SECONDS:
                links = (Link("Autorizar no portal AWS", str(login["url"])),)
                code = str(login.get("code") or "")
                polling = True
                messages.append("Depois de autorizar, a página atualiza sozinha.")
        elif not selected:
            summary = "Login feito. Marque os papéis que o ambiente deve assumir."
            # com um único papel o supervisor o seleciona sozinho
            polling = int(st.get("roles") or 0) == 1
        else:
            summary = "Papéis escolhidos, credenciais ainda não carregadas."
            polling = not st.get("error")
        if st.get("error"):
            messages.append(str(st["error"]))

        card = Card(
            title=self.name,
            state=state,
            summary=summary,
            rows=tuple(rows),
            links=links,
            code=code,
            messages=tuple(messages),
            polling=polling,
        )
        # ativos primeiro, na ordem em que valem (o primeiro é o padrão); o formulário
        # devolve os marcados na ordem das opções
        ordered = [*selected, *(r for r in roles if r not in selected)]
        choices = {
            "apply_roles.profiles": tuple(
                Choice(r, _describe_role(r, labels), checked=r in selected) for r in ordered
            )
        }
        return Status(conditions=conditions, card=card, state=state, choices=choices)

    # --- ações -----------------------------------------------------------

    def handle_action(self, ctx: ExtensionContext, action_id: str, params: Mapping[str, ActionParam]) -> ActionResult:
        if action_id == "start_login":
            return self._start_login(ctx)
        if action_id == "refresh_roles":
            ctx.exec(_quote_file(f"{STATE_DIR}/req/refresh", str(time.time())))
            return ActionResult(message="Atualização de papéis solicitada.")
        if action_id == "apply_roles":
            raw = params.get("profiles", ())
            profiles = list(dict.fromkeys(p.strip() for p in ([raw] if isinstance(raw, str) else raw)))
            if not profiles or not all(_PROFILE_RE.match(p) for p in profiles):
                raise ExtensionError("Perfil inválido.")
            if len(profiles) > MAX_ROLES:
                raise ExtensionError(f"Escolha no máximo {MAX_ROLES} papéis.")
            ctx.exec(_quote_file(f"{STATE_DIR}/req/profiles", "\n".join(profiles)))
            return ActionResult(message=f"{len(profiles)} papel(is) solicitado(s); o primeiro é o padrão.")
        if action_id == "reload_creds":
            ctx.exec(_quote_file(f"{STATE_DIR}/req/reload", str(time.time())))
            return ActionResult(message="Recarga de credenciais solicitada.")
        return super().handle_action(ctx, action_id, params)

    def _start_login(self, ctx: ExtensionContext) -> ActionResult:
        cmd = (
            f"rm -f {STATE_DIR}/login_ok; "
            "aws-sso login --url-action print "
            f"&& touch {STATE_DIR}/login_ok"
        )
        log = ctx.run_detached(
            ("sh", "-c", cmd),
            tag=LOGIN_TAG,
            done_markers=("https://",),
            timeout=LOGIN_TIMEOUT,
        )
        url = _first_url(log)
        if not url:
            raise ExtensionError("Não consegui obter a URL de login do aws-sso.")
        match = _CODE_IN_LOG.search(log)
        code = match.group(1) if match else ""
        ctx.state["login"] = {"url": url, "code": code, "at": time.time()}
        card = Card(
            title=self.name,
            state="needs_action",
            summary="Autorize o acesso no portal AWS.",
            links=(Link("Autorizar no portal AWS", url),),
            code=code,
        )
        return ActionResult(message="Login iniciado.", card=card)


def _str_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _describe_role(profile: Any, labels: Mapping[str, Any]) -> str:
    """`id:Papel (Nome da conta)`: o valor a digitar é só o `id:Papel`; o
    nome da conta é rótulo e aparece apenas escapado pelo renderer."""
    name = str(labels.get(profile) or "").strip()
    return f"{profile} ({name})" if name else str(profile)


def _first_url(log: str) -> str:
    for m in _URL_IN_LOG.finditer(log):
        url = m.group(0).rstrip(".,)")
        if "amazonaws.com" in url or "awsapps.com" in url:
            return url
    return ""

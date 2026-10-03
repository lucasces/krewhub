"""Extensão GitHub: `git clone`/`git push` por HTTPS no Pod do dev com um
token (fine-grained PAT), sem sidecar e sem imagem própria.

O token fica no Secret `krewhub-ext-<slug>` e é montado como ARQUIVO somente
leitura no `kirocrew`; o credential helper do `/etc/gitconfig` lê esse arquivo
a cada operação git. Não há variável de ambiente com o token: o kubelet
atualiza o arquivo quando o Secret muda (~1 min), então trocar o token vale
sem recriar o Pod. O `/etc/gitconfig` não carrega o token, só o caminho."""

from __future__ import annotations

from typing import Any, Mapping

from app.extensions.base import (
    BuildContext,
    Card,
    Extension,
    ExtensionContext,
    FieldSpec,
    PodContribution,
    Status,
    files_volume_name,
    secret_key,
    secret_name,
)

EXT_ID = "github"
DEFAULT_HOST = "github.com"
TOKEN_VOLUME = "github-token"
TOKEN_DIR = "/etc/krewhub/github"
TOKEN_FILE = f"{TOKEN_DIR}/token"
GITCONFIG_FILE = "gitconfig"
GITCONFIG_PATH = "/etc/gitconfig"

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
#: hostname (com porta opcional): vai pra dentro do gitconfig, então o alfabeto é fechado
HOST_RE = rf"{_LABEL}(?:\.{_LABEL})*(?::[0-9]{{1,5}})?"

# `$1` é a operação que o git passa ao helper; só `get` devolve credencial.
# Sem arquivo/token vazio o helper sai calado e o git cai no prompt normal.
HELPER = (
    'f() { test "$1" = get || exit 0; '
    f"t=$(cat {TOKEN_FILE} 2>/dev/null); "
    'test -n "$t" || exit 0; '
    'echo username=x-access-token; echo "password=$t"; }; f'
)


def _host(config: Mapping[str, Any]) -> str:
    return (str(config.get("host") or "").strip() or DEFAULT_HOST).lower()


def render_gitconfig(host: str) -> str:
    helper = HELPER.replace("\\", "\\\\").replace('"', '\\"')
    return f'[credential "https://{host}"]\n\thelper = "!{helper}"\n'


class GithubExtension(Extension):
    id = EXT_ID
    name = "GitHub"
    description = (
        "git clone/push por HTTPS no ambiente com um token do GitHub (fine-grained). "
        "Trocar o token vale em cerca de um minuto, sem recriar o Pod."
    )
    fields = (
        FieldSpec(
            "token",
            "Token do GitHub",
            kind="secret",
            required=True,
            help="Fine-grained personal access token, só com os repositórios e permissões necessários.",
        ),
        FieldSpec(
            "host",
            "Servidor",
            default=DEFAULT_HOST,
            pattern=HOST_RE,
            help="github.com, ou o host do GitHub Enterprise Server.",
        ),
    )

    def pod_contribution(self, ctx: BuildContext) -> PodContribution:
        host = _host(ctx.config)
        return PodContribution(
            volumes=[
                {
                    "name": TOKEN_VOLUME,
                    "secret": {
                        "secretName": secret_name(ctx.slug),
                        "items": [{"key": secret_key(EXT_ID, "token"), "path": "token"}],
                        # o Secret pode ainda não ter a chave: o volume sobe vazio e o kubelet o preenche depois
                        "optional": True,
                    },
                }
            ],
            main_volume_mounts=[
                {"name": TOKEN_VOLUME, "mountPath": TOKEN_DIR, "readOnly": True},
                {
                    "name": files_volume_name(EXT_ID),
                    "mountPath": GITCONFIG_PATH,
                    "subPath": GITCONFIG_FILE,
                    "readOnly": True,
                },
            ],
            files={GITCONFIG_FILE: render_gitconfig(host)},
        )

    def status(self, ctx: ExtensionContext) -> Status:
        host = _host(ctx.config)
        return Status(
            conditions={"github.configured": True},
            card=Card(title=self.name, state="ready", summary=f"git por HTTPS em {host}"),
            state="ready",
        )

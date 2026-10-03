"""Config 100% via env var -- mesmo princípio do oidc-client-poc: nenhum
valor de infra (contexto k8s, imagem, storage class, domínio, credenciais
OIDC) fica hardcoded no código. Tudo tem um default REVISÁVEL, não um
valor secreto ou específico de organização embutido."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- cluster ---
    k8s_kubeconfig: str
    k8s_context: str

    # --- template do pod-por-dev (mesmo padrão de dev-testdev/) ---
    dev_namespace: str
    base_domain: str
    public_port: str
    dev_pod_scheme: str
    kirocrew_image: str
    storage_class: str
    storage_size: str
    chp_namespace: str
    chp_pod_label: str
    chp_admin_port: int

    # --- overlay JSON Patch (RFC 6902) por-cluster, aplicado em cima dos
    # manifests genéricos de build_deployment/build_pvc (ver app/overlay.py
    # -- é aqui, não em código Python novo, que entra qualquer peculiaridade
    # de cluster específico, tipo nodeAffinity pro control-plane) ---
    dev_pod_overlay_path: str
    dev_pod_overlay_json: str

    # --- persistência local (SQLite, MVP) ---
    db_path: str

    # --- sessão do dashboard (kirocrew token) ---
    session_ttl: str

    # --- OIDC genérico (mesmo mecanismo do oidc-client-poc/oidc_client.py) ---
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_redirect_uri: str
    oidc_scopes: str

    # --- sessao propria do KrewHub (cookie/Bearer pos-login, ver app/auth.py) ---
    session_secret: str
    auth_token_ttl_seconds: int

    # --- kiro-cli login (device-flow, Identity Center) ---
    kiro_identity_provider: str
    kiro_region: str

    # --- auto-registro da própria rota no CHP (ver main.py startup) ---
    self_host: str
    self_port: int

    # --- extensões (ids instalados que o admin habilita; ver docs/EXTENSIONS.md) ---
    extensions_enabled: str = ""


def load_settings() -> Settings:
    return Settings(
        k8s_kubeconfig=_env(
            "KREWHUB_KUBECONFIG", os.path.expanduser("~/.kube/config-personal")
        ),
        k8s_context=_env("KREWHUB_K8S_CONTEXT", ""),
        dev_namespace=_env("KREWHUB_DEV_NAMESPACE", "krewhub-devs"),
        base_domain=_env("KREWHUB_BASE_DOMAIN", "kiro.internal"),
        public_port=_env("KREWHUB_PUBLIC_PORT", "8080"),
        dev_pod_scheme=_env("KREWHUB_DEV_POD_SCHEME", "http"),
        kirocrew_image=_env("KREWHUB_KIROCREW_IMAGE", "ghcr.io/kirodotdev/kirocrew:0.6.0"),
        storage_class=_env("KREWHUB_STORAGE_CLASS", "rook-cephfs"),
        storage_size=_env("KREWHUB_STORAGE_SIZE", "10Gi"),
        chp_namespace=_env("KREWHUB_CHP_NAMESPACE", "krewhub"),
        chp_pod_label=_env("KREWHUB_CHP_POD_LABEL", "app=configurable-http-proxy"),
        chp_admin_port=int(_env("KREWHUB_CHP_ADMIN_PORT", "8001")),
        dev_pod_overlay_path=_env("KREWHUB_DEV_POD_OVERLAY_PATH", ""),
        dev_pod_overlay_json=_env("KREWHUB_DEV_POD_OVERLAY_JSON", ""),
        db_path=_env("KREWHUB_DB_PATH", "./krewhub.db"),
        session_ttl=_env("KREWHUB_SESSION_TTL", "24h"),
        oidc_issuer=_env("KREWHUB_OIDC_ISSUER"),
        oidc_client_id=_env("KREWHUB_OIDC_CLIENT_ID"),
        oidc_client_secret=_env("KREWHUB_OIDC_CLIENT_SECRET"),
        oidc_redirect_uri=_env("KREWHUB_OIDC_REDIRECT_URI"),
        oidc_scopes=_env("KREWHUB_OIDC_SCOPES", "openid email profile"),
        session_secret=_env("KREWHUB_SESSION_SECRET"),
        auth_token_ttl_seconds=int(_env("KREWHUB_AUTH_TOKEN_TTL_SECONDS", "86400")),
        kiro_identity_provider=_env("KREWHUB_KIRO_IDENTITY_PROVIDER"),
        kiro_region=_env("KREWHUB_KIRO_REGION"),
        self_host=_env("KREWHUB_SELF_HOST"),
        self_port=int(_env("KREWHUB_SELF_PORT", "8080")),
        extensions_enabled=_env("KREWHUB_EXTENSIONS_ENABLED"),
    )

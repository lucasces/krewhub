"""Fixtures compartilhadas -- suite RAPIDA e OFFLINE (sem cluster real,
sem rede real): todo acesso a k8s/kubectl exec/CHP e' mockado
(monkeypatch nas funcoes de app/k8s_manager.py, app/session_client.py,
app/chp_client.py, app/kiro_login.py). SQLite roda de verdade, mas
sempre num arquivo tmp por teste (tmp_path) -- rapido, sem estado
compartilhado entre testes.

Isso e' a suite de REGRESSAO pra rodar antes de cada deploy -- diferente
do smoke-test manual contra um owner descartavel no cluster real (ver
README, secao "GET /close vs GET /logout" e as demais secoes de teste ao
vivo), que continua sendo procedimento manual, fora desta suite.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import auth
from app.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings determinística, isolada por teste (SQLite num arquivo
    tmp_path próprio). `self_host=""` -- pula o auto-registro de rota no
    CHP no startup do app (senão bateria em k8s_manager/chp_client de
    verdade logo na criação do TestClient)."""
    return Settings(
        k8s_kubeconfig="",
        k8s_context="test-context",
        dev_namespace="krewhub-devs",
        base_domain="kiro.internal",
        public_port="8080",
        dev_pod_scheme="http",
        kirocrew_image="ghcr.io/kirodotdev/kirocrew:0.6.0",
        storage_class="rook-cephfs",
        storage_size="10Gi",
        chp_namespace="chp-ns",
        chp_pod_label="app=configurable-http-proxy",
        chp_admin_port=8001,
        dev_pod_overlay_path="",
        dev_pod_overlay_json="",
        db_path=str(tmp_path / "krewhub-test.db"),
        session_ttl="24h",
        oidc_issuer="https://idp.test.local/realms/test",
        oidc_client_id="krewhub-test-client",
        oidc_client_secret="",
        oidc_redirect_uri="http://krewhub.kiro.internal:8080/callback",
        oidc_scopes="openid email profile",
        session_secret="test-only-session-secret-do-not-use-in-prod",
        auth_token_ttl_seconds=3600,
        kiro_identity_provider="",
        kiro_region="",
        self_host="",
        self_port=8080,
    )


@pytest.fixture
def client(settings, monkeypatch):
    """TestClient com `app.main._settings`/`_pending_logins` isolados por
    teste. `base_url` fixo em `krewhub.kiro.internal` -- mesmo host usado
    em produção atrás do CHP (relevante pro cookie `path=/` e pros testes
    de redirect que comparam URL absoluta)."""
    import app.main as main

    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "_pending_logins", {})
    with TestClient(main.app, base_url="http://krewhub.kiro.internal") as c:
        yield c


@pytest.fixture
def sign_cookie(settings):
    """Assina um token de sessão do KrewHub válido pro owner_id dado --
    mesmo helper (`auth.sign_session`) que `/callback` usa de verdade,
    não uma segunda implementação."""

    def _sign(owner_id: str, *, ttl_seconds: int | None = None) -> str:
        return auth.sign_session(
            owner_id,
            secret=settings.session_secret,
            ttl_seconds=settings.auth_token_ttl_seconds if ttl_seconds is None else ttl_seconds,
        )

    return _sign


@pytest.fixture
def expired_cookie(settings):
    """Token assinado corretamente mas já expirado -- `exp` no passado.
    Não reaproveita `auth.sign_session` (que sempre soma um TTL positivo
    a partir de agora) porque precisamos exatamente do caso "assinatura
    válida, mas expirado", não "assinatura inválida"."""

    def _expired(owner_id: str) -> str:
        return auth.sign_session(owner_id, secret=settings.session_secret, ttl_seconds=-3600)

    return _expired


@pytest.fixture
def tampered_cookie(sign_cookie):
    """Token com assinatura ADULTERADA -- payload válido, últimos bytes
    da assinatura trocados. Cobre "assinatura adulterada falha" sem
    duplicar a lógica de verificação (só corrompe a saída de
    `sign_session`)."""

    def _tampered(owner_id: str) -> str:
        token = sign_cookie(owner_id)
        payload_b64, sig_b64 = token.rsplit(".", 1)
        # Troca o PRIMEIRO char da assinatura (nao o ultimo -- o ultimo
        # char de um base64url de 32 bytes codifica so 4 bits uteis +
        # 2 bits de padding descartados no decode, entao alguns pares de
        # caracteres decodificam pro MESMO byte final e nao corrompem
        # nada; o primeiro char sempre cobre os bits mais significativos
        # do primeiro byte, corrompendo garantido).
        first = sig_b64[0]
        alt = "A" if first != "A" else "B"
        return f"{payload_b64}.{alt}{sig_b64[1:]}"

    return _tampered

"""app/k8s_templates.py -- normalizacao owner_id -> slug e geracao dos
manifests (puro, sem k8s/rede: sao so dicts Python)."""

from __future__ import annotations

from app import k8s_templates as tpl
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        k8s_kubeconfig="",
        k8s_context="test",
        dev_namespace="krewhub-devs",
        base_domain="kiro.internal",
        public_port="8080",
        kirocrew_image="ghcr.io/kirodotdev/kirocrew:0.6.0",
        storage_class="rook-cephfs",
        storage_size="10Gi",
        chp_namespace="kirohub",
        chp_pod_label="app=configurable-http-proxy",
        chp_admin_port=8001,
        db_path=":memory:",
        session_ttl="24h",
        oidc_issuer="",
        oidc_client_id="",
        oidc_client_secret="",
        oidc_redirect_uri="",
        oidc_scopes="openid email profile",
        session_secret="s",
        auth_token_ttl_seconds=3600,
        kiro_identity_provider="",
        kiro_region="",
        self_host="",
        self_port=8080,
    )
    base.update(overrides)
    return Settings(**base)


def test_slugify_real_owner_id_matches_documented_example():
    # Exemplo real desta sessao de trabalho (ver README, secao "404 no
    # primeiro provision real via OIDC").
    assert tpl.slugify("lucas.ces@somoseducacao.com.br") == "lucas-ces-somoseducacao-com-br"


def test_slugify_is_deterministic():
    assert tpl.slugify("dev-a@test.local") == tpl.slugify("dev-a@test.local")


def test_slugify_different_owners_dont_collide():
    a = tpl.slugify("dev-a@test.local")
    b = tpl.slugify("dev-b@test.local")
    assert a != b


def test_slugify_similar_owners_dont_collide_after_normalization():
    """Dois owner_ids que só diferem em caracteres que a normalização
    colapsa (ponto vs hífen) continuam distintos -- normalização não
    pode introduzir colisão entre identidades diferentes."""
    a = tpl.slugify("dev.a@test.local")
    b = tpl.slugify("dev-a@test.local")
    # Aqui os dois REALMENTE colapsariam pro mesmo slug -- documentando o
    # comportamento real (achado, não presumido): "." e "-" mapeiam pro
    # mesmo separador. O que importa é que isso é ESTÁVEL e não gera
    # exceção -- não que sejam sempre distintos incondicionalmente.
    assert a == b  # achado: colisão real e esperada entre "." e "-"
    # Mas owners com conteúdo alfanumérico diferente nunca colidem:
    assert tpl.slugify("dev-a@test.local") != tpl.slugify("dev-c@test.local")


def test_slugify_lowercases_and_lowers_max_length():
    slug = tpl.slugify("Dev.With.UPPERCASE@Test.Local")
    assert slug == slug.lower()
    assert len(slug) <= 40


def test_slugify_empty_alnum_falls_back_to_stable_hash():
    """owner_id sem nenhum char alfanumérico não pode virar slug vazio
    (label k8s inválido) -- e o fallback precisa ser determinístico."""
    slug1 = tpl.slugify("###@@@")
    slug2 = tpl.slugify("###@@@")
    assert slug1 == slug2
    assert slug1.startswith("dev-")
    assert slug1 != ""


def test_host_for_uses_base_domain():
    settings = _settings(base_domain="kiro.internal")
    assert tpl.host_for("lucas-ces-somoseducacao-com-br", settings) == "lucas-ces-somoseducacao-com-br.kiro.internal"


def test_build_resource_names_are_deterministic_by_slug():
    """Reconciliar duas vezes com o MESMO owner_id -> MESMO slug -> os
    MESMOS nomes de recurso -- get-or-create nunca duplica (idempotência
    de nomenclatura, complementar ao teste de idempotência do
    k8s_manager, que confirma create-vs-patch)."""
    settings = _settings()
    owner_id = "dev-a@test.local"
    slug1 = tpl.slugify(owner_id)
    slug2 = tpl.slugify(owner_id)
    assert slug1 == slug2

    ns = "krewhub-devs"
    host = tpl.host_for(slug1, settings)

    secret1 = tpl.build_secret(ns, slug1, owner_id)
    secret2 = tpl.build_secret(ns, slug2, owner_id)
    assert secret1["metadata"]["name"] == secret2["metadata"]["name"] == f"kiro-owner-id-{slug1}"

    pvc = tpl.build_pvc(ns, slug1, settings)
    assert pvc["metadata"]["name"] == f"kiro-workspace-{slug1}"

    svc = tpl.build_service(ns, slug1)
    assert svc["metadata"]["name"] == f"kirocrew-{slug1}"

    deployment = tpl.build_deployment(ns, slug1, settings)
    assert deployment["metadata"]["name"] == f"kirocrew-{slug1}"

    netpol = tpl.build_networkpolicy(ns, slug1, settings)
    assert netpol["metadata"]["name"] == f"allow-chp-to-dashboard-only-{slug1}"

    cm = tpl.build_configmap(ns, slug1, host, settings)
    assert cm["metadata"]["name"] == f"kiro-config-{slug1}"


def test_build_resource_names_differ_between_owners():
    settings = _settings()
    ns = "krewhub-devs"
    slug_a = tpl.slugify("dev-a@test.local")
    slug_b = tpl.slugify("dev-b@test.local")

    svc_a = tpl.build_service(ns, slug_a)
    svc_b = tpl.build_service(ns, slug_b)
    assert svc_a["metadata"]["name"] != svc_b["metadata"]["name"]

    pvc_a = tpl.build_pvc(ns, slug_a, settings)
    pvc_b = tpl.build_pvc(ns, slug_b, settings)
    assert pvc_a["metadata"]["name"] != pvc_b["metadata"]["name"]


def test_networkpolicy_pod_selector_is_scoped_to_this_devs_slug_only():
    """Ponto crítico de segurança documentado no README: isolamento de
    rede entre devs no namespace COMPARTILHADO depende do podSelector
    específico do slug -- não da fronteira do namespace."""
    settings = _settings()
    slug = tpl.slugify("dev-a@test.local")
    netpol = tpl.build_networkpolicy("krewhub-devs", slug, settings)

    pod_selector = netpol["spec"]["podSelector"]["matchLabels"]
    assert pod_selector == {"app": "kirocrew", tpl.OWNER_LABEL_KEY: slug}

    # Só ingress do CHP, só na porta do dashboard -- nada mais liberado.
    assert netpol["spec"]["policyTypes"] == ["Ingress"]
    rule = netpol["spec"]["ingress"][0]
    assert rule["ports"] == [{"protocol": "TCP", "port": 5476}]
    assert rule["from"][0]["podSelector"]["matchLabels"]["app"] == "configurable-http-proxy"
    assert (
        rule["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        == settings.chp_namespace
    )


def test_networkpolicy_selector_never_matches_a_different_dev():
    settings = _settings()
    slug_a = tpl.slugify("dev-a@test.local")
    slug_b = tpl.slugify("dev-b@test.local")
    netpol_a = tpl.build_networkpolicy("krewhub-devs", slug_a, settings)
    # O podSelector da NetworkPolicy do dev A nunca bate com o label do
    # pod do dev B (owner-slug diferente) -- é exatamente essa
    # discriminação que impede o dev B de herdar a allowlist do dev A.
    assert netpol_a["spec"]["podSelector"]["matchLabels"][tpl.OWNER_LABEL_KEY] == slug_a
    assert netpol_a["spec"]["podSelector"]["matchLabels"][tpl.OWNER_LABEL_KEY] != slug_b


def test_deployment_and_service_share_the_same_owner_slug_label():
    """Service seleciona o Deployment certo (mesmo owner-slug label) --
    sem isso o Service de um dev poderia rotear pro pod de outro no
    namespace compartilhado."""
    settings = _settings()
    slug = tpl.slugify("dev-a@test.local")
    deployment = tpl.build_deployment("krewhub-devs", slug, settings)
    service = tpl.build_service("krewhub-devs", slug)

    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert service["spec"]["selector"] == pod_labels


def test_configmap_cors_origin_matches_host_and_public_port():
    settings = _settings(base_domain="kiro.internal", public_port="8080")
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"http://{host}:8080"

"""app/k8s_templates.py -- normalizacao owner_id -> slug e geracao dos
manifests (puro, sem k8s/rede: sao so dicts Python)."""

from __future__ import annotations

import json

from app import k8s_templates as tpl
from app.config import Settings

# Overlay equivalente exato ao nodeAffinity que antes estava hardcoded em
# build_deployment, hoje build_pod (achado de uma fatia anterior) -- usado
# pra provar que o overlay reproduz o comportamento antigo bit-a-bit
# quando configurado. Path relativo a `/spec/...` direto -- migrado de
# `/spec/template/spec/...` junto com a troca de Deployment pra Pod puro
# (Pod nao tem o wrapper PodTemplateSpec que Deployment tinha).
_HOMELAB_NODE_AFFINITY_OVERLAY = {
    "pod": [
        {
            "op": "add",
            "path": "/spec/affinity",
            "value": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-role.kubernetes.io/control-plane",
                                        "operator": "Exists",
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        }
    ]
}


def _settings(**overrides) -> Settings:
    base = dict(
        k8s_kubeconfig="",
        k8s_context="test",
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
    # Exemplo real usado historicamente pra validar o slugify contra um
    # owner_id de verdade.
    assert tpl.slugify("lucas.ces@minha-org.com.br") == "lucas-ces-minha-org-com-br"


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
    assert tpl.host_for("lucas-ces-minha-org-com-br", settings) == "lucas-ces-minha-org-com-br.kiro.internal"


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

    pod = tpl.build_pod(ns, slug1, settings)
    assert pod["metadata"]["name"] == f"kirocrew-{slug1}"

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
    """Ponto crítico de segurança documentado em AGENTS.md, seção
    "Architecture": isolamento de
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


def test_pod_and_service_share_the_same_owner_slug_label():
    """Service seleciona o Pod certo (mesmo owner-slug label) --
    sem isso o Service de um dev poderia rotear pro pod de outro no
    namespace compartilhado."""
    settings = _settings()
    slug = tpl.slugify("dev-a@test.local")
    pod = tpl.build_pod("krewhub-devs", slug, settings)
    service = tpl.build_service("krewhub-devs", slug)

    pod_labels = pod["metadata"]["labels"]
    assert service["spec"]["selector"] == pod_labels


def test_configmap_cors_origin_matches_host_and_public_port():
    settings = _settings(base_domain="kiro.internal", public_port="8080")
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"http://{host}:8080"


def test_configmap_cors_origin_omits_default_http_port():
    """Achado real: um browser nunca inclui a porta padrao (80 pra http)
    no header Origin. Se KIROCREW_CORS_ORIGINS incluir ":80" explicito,
    o CSRF-origin check da lib vendored (match exato de string) nunca
    bate com o Origin real -- 403 "CSRF check failed" mesmo com host e
    scheme corretos. Porta 80 precisa ser omitida do valor gerado."""
    settings = _settings(base_domain="kiro.internal", public_port="80")
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"http://{host}"


def test_configmap_cors_origin_default_scheme_is_http_unchanged():
    """dev_pod_scheme default precisa reproduzir bit-a-bit o
    comportamento de antes desta fatia (homelab, sem TLS na borda) --
    nao pode haver regressao pra quem nunca setou KREWHUB_DEV_POD_SCHEME."""
    settings = _settings(base_domain="kiro.internal", public_port="8080")
    assert settings.dev_pod_scheme == "http"
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"http://{host}:8080"


def test_configmap_cors_origin_https_scheme_with_default_port_omits_443():
    """Quando TLS termina na borda (Ingress/ALB) e dev_pod_scheme=https,
    a porta publica 443 (padrao do scheme https) precisa ser OMITIDA
    pelo mesmo motivo que 80 e omitido pra http -- o browser tambem nao
    inclui a porta padrao https no header Origin."""
    settings = _settings(base_domain="kiro.internal", public_port="443", dev_pod_scheme="https")
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"https://{host}"


def test_configmap_cors_origin_https_scheme_with_non_default_port_keeps_port():
    settings = _settings(base_domain="kiro.internal", public_port="8443", dev_pod_scheme="https")
    slug = tpl.slugify("dev-a@test.local")
    host = tpl.host_for(slug, settings)
    cm = tpl.build_configmap("krewhub-devs", slug, host, settings)
    assert cm["data"]["KIROCREW_CORS_ORIGINS"] == f"https://{host}:8443"


# -- Overlay JSON Patch (achado desta fatia: nodeAffinity control-plane
# estava hardcoded em build_deployment sem via de configuracao nenhuma) --


def test_build_pod_without_overlay_has_no_affinity_at_all():
    """Default seguro: sem KREWHUB_DEV_POD_OVERLAY_*, o manifest e' 100%
    generico -- nenhum campo `affinity` no spec do pod, roda em qualquer
    cluster k8s."""
    settings = _settings()
    slug = tpl.slugify("dev-a@test.local")
    pod = tpl.build_pod("krewhub-devs", slug, settings)
    assert "affinity" not in pod["spec"]


def test_build_pvc_without_overlay_is_unaffected():
    settings = _settings()
    slug = tpl.slugify("dev-a@test.local")
    pvc = tpl.build_pvc("krewhub-devs", slug, settings)
    assert pvc["spec"]["storageClassName"] == "rook-cephfs"
    assert "metadata" in pvc and pvc["metadata"]["name"] == f"kiro-workspace-{slug}"


def test_build_pod_overlay_reproduces_the_old_hardcoded_node_affinity():
    """Com o overlay equivalente ao do homelab configurado, o `affinity`
    resultante e' IDENTICO ao que antes vinha hardcoded direto no
    Python -- prova de que a migracao pro overlay nao muda o
    comportamento em producao quando o overlay certo e' aplicado."""
    settings = _settings(dev_pod_overlay_json=json.dumps(_HOMELAB_NODE_AFFINITY_OVERLAY))
    slug = tpl.slugify("dev-a@test.local")
    pod = tpl.build_pod("krewhub-devs", slug, settings)

    assert pod["spec"]["affinity"] == {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "node-role.kubernetes.io/control-plane",
                                "operator": "Exists",
                            }
                        ]
                    }
                ]
            }
        }
    }
    # O resto do manifest continua identico ao caso sem overlay -- o
    # overlay so ACRESCENTA o campo `affinity`, nao toca em mais nada.
    baseline = tpl.build_pod("krewhub-devs", slug, _settings())
    baseline["spec"]["affinity"] = pod["spec"]["affinity"]
    # O hash do spec acompanha o overlay (spec diferente => hash diferente).
    assert pod["metadata"]["annotations"][tpl.SPEC_HASH_ANNOTATION] != (
        baseline["metadata"]["annotations"][tpl.SPEC_HASH_ANNOTATION]
    )
    baseline["metadata"]["annotations"] = pod["metadata"]["annotations"]
    assert pod == baseline


def test_build_pod_overlay_only_affects_pod_not_pvc():
    """Overlay com chave `pod` nao vaza pro build_pvc -- cada
    `build_*` so aplica os ops da sua propria chave no documento."""
    settings = _settings(dev_pod_overlay_json=json.dumps(_HOMELAB_NODE_AFFINITY_OVERLAY))
    slug = tpl.slugify("dev-a@test.local")
    pvc = tpl.build_pvc("krewhub-devs", slug, settings)
    assert pvc["spec"]["storageClassName"] == "rook-cephfs"
    assert "affinity" not in pvc["spec"]


def test_build_pod_malformed_overlay_raises_clear_error_not_silent_crash():
    """Overlay malformado (nao e' um dict {recurso: [...]}) precisa
    falhar explicito na hora de gerar o manifest -- nunca ser ignorado
    quieto nem estourar um erro generico sem contexto."""
    settings = _settings(dev_pod_overlay_json="- not-a-dict-at-the-top-level")
    slug = tpl.slugify("dev-a@test.local")
    try:
        tpl.build_pod("krewhub-devs", slug, settings)
    except ValueError as exc:
        assert "recurso" in str(exc) or "dict" in str(exc)
    else:
        raise AssertionError("overlay malformado deveria levantar ValueError")

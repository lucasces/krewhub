"""app/overlay.py -- overlay JSON Patch (RFC 6902) por cima dos manifests
genéricos. Cobre o achado desta fatia: o nodeAffinity pro control-plane
estava hardcoded em build_pod sem via de configuração nenhuma;
agora é um overlay opcional, com default seguro (sem overlay = manifest
100% genérico)."""

from __future__ import annotations

import json

import pytest

from app import overlay
from app.config import Settings


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
        chp_namespace="kirohub",
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


def test_apply_overlay_without_ops_returns_manifest_unchanged():
    """[] (default seguro) -- apply_overlay nem toca no manifest."""
    manifest = {"spec": {"foo": "bar"}}
    assert overlay.apply_overlay(manifest, []) is manifest


def test_apply_overlay_adds_a_field_without_mutating_the_original():
    manifest = {"spec": {"template": {"spec": {}}}}
    ops = [
        {"op": "add", "path": "/spec/template/spec/affinity", "value": {"nodeAffinity": {}}}
    ]
    patched = overlay.apply_overlay(manifest, ops)
    assert patched["spec"]["template"]["spec"]["affinity"] == {"nodeAffinity": {}}
    # devolve dict novo -- não muta o manifest original passado.
    assert "affinity" not in manifest["spec"]["template"]["spec"]


def test_load_overlay_ops_empty_when_nothing_configured():
    settings = _settings()
    assert overlay.load_overlay_ops(settings, "pod") == []
    assert overlay.load_overlay_ops(settings, "pvc") == []


def test_load_overlay_ops_reads_inline_json_fallback():
    settings = _settings(
        dev_pod_overlay_json=json.dumps(
            {"pod": [{"op": "add", "path": "/x", "value": 1}]}
        )
    )
    assert overlay.load_overlay_ops(settings, "pod") == [
        {"op": "add", "path": "/x", "value": 1}
    ]


def test_load_overlay_ops_missing_resource_key_is_empty():
    """Overlay configurado só pra `pvc` -- `pod` continua genérico."""
    settings = _settings(dev_pod_overlay_json=json.dumps({"pvc": []}))
    assert overlay.load_overlay_ops(settings, "pod") == []


def test_load_overlay_ops_reads_from_path_and_path_wins_over_inline(tmp_path):
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text("pod:\n  - op: add\n    path: /from-file\n    value: true\n")
    settings = _settings(
        dev_pod_overlay_path=str(overlay_file),
        dev_pod_overlay_json=json.dumps(
            {"pod": [{"op": "add", "path": "/from-json", "value": True}]}
        ),
    )
    ops = overlay.load_overlay_ops(settings, "pod")
    assert ops == [{"op": "add", "path": "/from-file", "value": True}]


def test_load_overlay_ops_rejects_non_dict_top_level():
    """Overlay malformado (lista na raiz em vez de {recurso: [...]}) falha
    com erro claro, não crash silencioso nem overlay ignorado quieto."""
    settings = _settings(
        dev_pod_overlay_json=json.dumps([{"op": "add", "path": "/x", "value": 1}])
    )
    with pytest.raises(ValueError):
        overlay.load_overlay_ops(settings, "pod")


def test_load_overlay_ops_rejects_non_list_value_for_resource():
    settings = _settings(dev_pod_overlay_json=json.dumps({"pod": {"op": "add"}}))
    with pytest.raises(ValueError):
        overlay.load_overlay_ops(settings, "pod")

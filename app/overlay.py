"""Overlay client-side (JSON Patch, RFC 6902) aplicado por cima dos
manifests genéricos gerados em k8s_templates.py -- é o único lugar onde
uma peculiaridade de CLUSTER específico (nodeAffinity pro control-plane
do galaxy-far-far-away, tolerations, volumes extras, o que vier) pode
entrar, sem precisar de env var nova nem redeploy de código a cada
peculiaridade nova (achado da investigação anterior: nodeAffinity
control-plane estava hardcoded direto em build_deployment, sem via de
configuração -- ver git log/README pra essa investigação).

Por que JSON Patch (RFC 6902) e não strategic-merge-patch nem JSON Merge
Patch (RFC 7396):

- Strategic merge patch é a semântica "certa" pro k8s (mescla listas por
  chave -- `containers`/`volumes` por `name`), mas a implementação de
  referência vive em Go (`k8s.io/apimachinery/pkg/util/strategicpatch`),
  consumida pelo apiserver quando o PATCH chega com
  `Content-Type: application/strategic-merge-patch+json`. Não existe lib
  Python madura e amplamente adotada que replique esse algoritmo
  client-side (ela precisaria saber, campo a campo, quais listas têm
  merge-key e qual é -- e quais NÃO têm nenhuma, como `tolerations`, que
  cai pra "substitui a lista inteira" mesmo dentro do SMP de verdade).
  Reimplementar esse mapeamento à mão é reinventar uma peça não-trivial
  do apimachinery -- risco desproporcional pro tamanho deste serviço.
- JSON Merge Patch (RFC 7396) é simples (um dict que se funde
  recursivamente), mas qualquer lista é SUBSTITUÍDA por inteiro. Um
  overlay que só quisesse acrescentar 1 toleration apagaria as outras; um
  overlay de `volumes` apagaria o volume do workspace PVC que
  build_deployment já declara, a menos que o overlay o repetisse por
  inteiro -- regressão silenciosa que só apareceria em runtime (pod sem
  workspace montado), não em teste.
- JSON Patch (RFC 6902), via a lib madura `jsonpatch` (PyPI), é mais
  verboso (cada mudança é uma operação add/remove/replace com path
  explícito) mas nunca apaga o que não foi pedido -- `add
  .../tolerations/-` ACRESCENTA sem tocar no resto. Trade-off aceito:
  paths são posicionais em listas (`containers/0/...` assume o container
  `kirocrew` no índice 0 -- verdade hoje, único container do pod; overlay
  precisa ser revisto se isso mudar).
"""

from __future__ import annotations

import jsonpatch
import yaml

from app.config import Settings


def _load_overlay_doc(settings: Settings) -> dict:
    """{} (documento vazio) se nada configurado. Precedência:
    `KREWHUB_DEV_POD_OVERLAY_PATH` (arquivo, tipicamente montado via
    ConfigMap gerenciado fora do chart genérico -- ver README, seção
    "Overlay JSON Patch por-cluster") primeiro; `KREWHUB_DEV_POD_OVERLAY_JSON`
    (conteúdo inline, útil pra dev local/smoke test sem precisar montar
    arquivo) como fallback; nenhum dos dois setado = documento vazio."""
    if settings.dev_pod_overlay_path:
        with open(settings.dev_pod_overlay_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    elif settings.dev_pod_overlay_json:
        raw = settings.dev_pod_overlay_json
    else:
        return {}
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(
            "overlay precisa ser um dict {recurso: [operações JSON Patch]} "
            f"(ex.: {{'pod': [...]}}) -- veio {type(doc).__name__}"
        )
    return doc


def load_overlay_ops(settings: Settings, resource: str) -> list[dict]:
    """Lista de operações JSON Patch (RFC 6902) configuradas pro
    `resource` dado (ex. "pod", "pvc"). [] (default seguro) se
    nada configurado, ou se o overlay configurado não menciona esse
    `resource` -- nesse caso o `build_*` correspondente gera o manifest
    100% genérico, sem nada de cluster nenhum."""
    ops = _load_overlay_doc(settings).get(resource, [])
    if not ops:
        return []
    if not isinstance(ops, list):
        raise ValueError(
            f"overlay['{resource}'] precisa ser uma lista de operações JSON "
            f"Patch (RFC 6902) -- veio {type(ops).__name__}"
        )
    return ops


def apply_overlay(manifest: dict, ops: list[dict]) -> dict:
    """Aplica `ops` sobre `manifest`, sem mutar o original -- devolve um
    dict novo. `ops` vazio devolve `manifest` como veio, intacto (mesmo
    objeto, nem cópia -- não há nada pra aplicar)."""
    if not ops:
        return manifest
    return jsonpatch.apply_patch(manifest, ops, in_place=False)

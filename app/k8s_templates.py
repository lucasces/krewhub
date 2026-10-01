"""Gera os manifests do template pod-por-dev, parametrizados por owner_id
-- em vez de copiar o diretório e editar à mão, isso vira um dict Python
por recurso, aplicado via API do k8s (k8s_manager.py).

Mudança de arquitetura desta fatia: TODOS os pods de dev agora vivem num
namespace ÚNICO E COMPARTILHADO (`Settings.dev_namespace`), não mais um
namespace novo por dev. Motivo: um namespace por dev criava um dev
namespace-list sem controle nenhum (achado da investigação de "spam de
namespaces") -- consolidar reduz a área de RBAC dinâmica e o número de
objetos Namespace que o serviço precisa criar (idealmente zero: o
namespace compartilhado é criado via GitOps, não pelo reconcile -- ver
AGENTS.md, seção "Architecture").

Como todos os recursos (Secret/ConfigMap/PVC/Service/Deployment/
NetworkPolicy) agora coexistem no MESMO namespace, cada um leva o `slug`
do dev no nome pra não colidir -- e o Deployment/Service/NetworkPolicy
levam a label `OWNER_LABEL_KEY: <slug>` pra que a NetworkPolicy consiga
selecionar SÓ o pod daquele dev (isolamento de rede entre devs agora
depende dessa label, não mais da fronteira do namespace -- ver
build_networkpolicy).

Toda decisão já validada ao vivo nas fatias anteriores está preservada
aqui: seccompProfile Unconfined só no container kirocrew (sandbox
unshare), fsGroup 1000 (PVC root-owned), NetworkPolicy só liberando o CHP
pra porta 5476, KIROCREW_CORS_ORIGINS apontando pro host do dev
(Host-header allowlist do dashboard).

O que NÃO está mais fixo aqui (achado de investigação anterior: estava
hardcoded sem via de configuração nenhuma) é o nodeAffinity pro node
control-plane que o CSI do rook-cephfs deste cluster exige -- isso agora
é responsabilidade do overlay JSON Patch (ver app/overlay.py e
KREWHUB_DEV_POD_OVERLAY_PATH/_JSON em docs/ARCHITECTURE.md, seção
"Per-cluster JSON Patch overlay"): sem overlay configurado,
build_pod/build_pvc geram manifest 100% genérico, sem nada
específico de cluster nenhum -- rodam em qualquer cluster k8s.

Mudança de arquitetura desta fatia (migração real, não só investigada --
ver docs/ARCHITECTURE.md, seção "Pure Pod instead of Deployment for the
per-dev workload"): o
workload por-dev deixou de ser um `Deployment` (1 réplica,
`ReplicaSet` de tabelinha) e virou um `Pod` puro (`restartPolicy:
Always`). Nenhum dos recursos k8s pró-réplica fazia sentido aqui -- 1
pod = 1 dev = 1 gateway, sem scaling, sem rolling deploy de verdade
(bump de imagem já era documentado como restart/recreate manual, nunca
um `kubectl set image` orquestrado). `restartPolicy: Always` cobre
exatamente o mesmo caso que o ReplicaSet cobria na prática (container
morre, kubelet reinicia) -- a única coisa que se perde é recriação
automática se o Pod INTEIRO for removido (delete acidental, node
morrer): nesse caso um `Pod` puro fica removido até alguém chamar
`reconcile_dev`/`/provision` de novo -- hoje isso já é sempre manual
(não há nada automatizado chamando `/provision` sozinho, culling
automático ainda não existe), então a perda é mais teórica que prática
agora -- trade-off aceito conscientemente, documentado em
docs/ARCHITECTURE.md, não escondido."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence

from app.config import Settings
from app.extensions.base import PodContribution
from app.extensions.contributions import collect_files, merge_contributions
from app.overlay import apply_overlay, load_overlay_ops

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Label que discrimina o pod de CADA dev dentro do namespace compartilhado
# -- é o que garante que a NetworkPolicy de um dev não vaze pro pod de
# outro dev no mesmo namespace (ver build_networkpolicy/build_pod).
OWNER_LABEL_KEY = "krewhub.pespa.net/owner-slug"

# Porta padrao de cada scheme -- usado pra decidir quando OMITIR a porta
# de KIROCREW_CORS_ORIGINS (ver build_configmap). O browser nunca inclui
# a porta padrao do scheme atual no header Origin.
_DEFAULT_PORT_FOR_SCHEME = {"http": "80", "https": "443"}


def slugify(owner_id: str, *, max_len: int = 40) -> str:
    """owner_id (email, claim, o que for) -> label DNS-1123 seguro.

    Não é o owner_id "de verdade" -- achado de fatia anterior: kirocrew
    não valida essa identidade contra nada (é só credencial de Slack), a
    única exigência real é um identificador ESTÁVEL o suficiente pra
    nomear recursos. Colapsar owner_id inteiro (incluindo domínio de
    email) no slug mantém 1 owner_id -> 1 slug determinístico.

    max_len=40 é deliberado: o slug entra em nomes de recurso (ex.
    `kirocrew-<slug>`, que precisa ser um nome de Service válido -- limite
    RFC 1035 de 63 chars) e em valor de label (limite k8s de 63 chars) --
    40 deixa folga suficiente pros prefixos mais longos já usados aqui."""
    slug = _SLUG_RE.sub("-", owner_id.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    if not slug:
        # owner_id sem nenhum char alfanumérico -- não fica vazio (label
        # inválido), usa hash curto e estável em vez de falhar.
        import hashlib

        slug = "dev-" + hashlib.sha256(owner_id.encode()).hexdigest()[:8]
    return slug


def host_for(slug: str, settings: Settings) -> str:
    return f"{slug}.{settings.base_domain}"


def build_secret(namespace: str, slug: str, owner_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"kiro-owner-id-{slug}", "namespace": namespace},
        "type": "Opaque",
        "stringData": {"KIROCREW_OWNER_ID": owner_id},
    }


def build_configmap(namespace: str, slug: str, host: str, settings: Settings) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"kiro-config-{slug}", "namespace": namespace},
        "data": {
            "TZ": "America/Sao_Paulo",
            "KIROCREW_PORT": "5476",
            # KIROCREW_BIND deliberadamente não setado -- default 0.0.0.0
            # da imagem já é o que o Service precisa (ver dev-testdev/).
            #
            # Host-header allowlist do dashboard (achado da fatia de
            # login): sem isso, todo Host != localhost/127.0.0.1 recebe
            # 403 "Host header not allowed." em qualquer rota fora dos
            # health probes.
            #
            # Porta padrao do scheme (80 pra http, unico scheme usado
            # aqui) precisa ser OMITIDA -- o browser nunca inclui a
            # porta padrao no header Origin, e o CSRF-origin check da
            # lib vendored (kiro_crew/dashboard/origin.py::check_origin)
            # faz match EXATO de string "scheme://host:port" contra esse
            # valor (diferente do Host-header check, que so compara
            # hostname e por isso nao pegou esse bug antes). Um valor
            # com ":80" explicito nunca bate com o Origin real do
            # browser -- achado real, 403 "CSRF check failed" no fluxo
            # de import do Kiro Crew.
            # Scheme tambem precisa ser configuravel (KREWHUB_DEV_POD_SCHEME,
            # settings.dev_pod_scheme): quando TLS termina na borda
            # (Ingress/ALB) e o backend interno e HTTP puro, o browser manda
            # Origin com "https://" mesmo que o Service seja HTTP -- um
            # scheme "http://" hardcoded aqui nunca bateria com esse Origin
            # real, mesmo com a porta certa. A porta padrao omitida tambem
            # depende do scheme (80 pra http, 443 pra https).
            "KIROCREW_CORS_ORIGINS": (
                f"{settings.dev_pod_scheme}://{host}"
                if settings.public_port == _DEFAULT_PORT_FOR_SCHEME.get(settings.dev_pod_scheme, "80")
                else f"{settings.dev_pod_scheme}://{host}:{settings.public_port}"
            ),
        },
    }


def build_pvc(namespace: str, slug: str, settings: Settings) -> dict:
    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"kiro-workspace-{slug}", "namespace": namespace},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": settings.storage_class,
            "resources": {"requests": {"storage": settings.storage_size}},
        },
    }
    # PVC spec é majoritariamente imutável após a criação (só
    # resources.requests.storage pode crescer) -- um overlay que mude
    # storageClassName/accessModes só pega em PVCs criados DEPOIS da
    # mudança; num PVC já existente o apiserver rejeita o patch (422),
    # comportamento nativo do k8s, não deste mecanismo.
    return apply_overlay(manifest, load_overlay_ops(settings, "pvc"))


def build_service(namespace: str, slug: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"kirocrew-{slug}", "namespace": namespace},
        "spec": {
            "type": "ClusterIP",
            # Os dois labels -- não só "app" -- pra selecionar
            # exclusivamente o pod DESTE dev, nunca o de outro no mesmo
            # namespace compartilhado.
            "selector": {"app": "kirocrew", OWNER_LABEL_KEY: slug},
            "ports": [
                {"name": "dashboard", "port": 5476, "targetPort": 5476, "protocol": "TCP"}
            ],
        },
    }


def build_networkpolicy(namespace: str, slug: str, settings: Settings) -> dict:
    """Ponto crítico de segurança desta fatia: com todos os devs no MESMO
    namespace, o isolamento não vem mais da fronteira do namespace -- vem
    do `podSelector` abaixo, que seleciona SÓ o pod deste dev (via
    OWNER_LABEL_KEY). Kubernetes NetworkPolicy é por-pod: um pod só fica
    sob "default-deny-ingress-exceto-o-permitido" para as policies cujo
    podSelector o seleciona. Como cada dev tem sua PRÓPRIA NetworkPolicy
    aqui (nome único `allow-chp-to-dashboard-only-<slug>`, podSelector
    único), o pod do dev A nunca é afetado pela regra do dev B, e
    tráfego de A pro pod de B não bate em nenhuma allowlist de B -> é
    rejeitado. Mecanismo coberto por teste (ver AGENTS.md, seção
    "Architecture")."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"allow-chp-to-dashboard-only-{slug}",
            "namespace": namespace,
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": "kirocrew", OWNER_LABEL_KEY: slug}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": settings.chp_namespace
                                }
                            },
                            "podSelector": {
                                "matchLabels": {"app": "configurable-http-proxy"}
                            },
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 5476}],
                }
            ],
        },
    }


SPEC_HASH_ANNOTATION = "krewhub.pespa.net/spec-hash"


def spec_hash(spec: dict, files: dict[str, str] | None = None) -> str:
    """Hash estável do `spec` final do Pod (já com overlay e extensões).
    O spec de um Pod é imutável no apiserver -- mudar imagem, sidecar ou
    volume exige recriar. Comparar esse hash com a anotação do Pod
    existente é o que diz se precisa.

    `files` é o conteúdo dos arquivos das extensões (ConfigMap): o spec só
    referencia as chaves, mas um sidecar que lê o arquivo no start precisa
    ser recriado quando o conteúdo muda. Sem arquivos, o hash é o do spec
    puro (Pods sem extensões não são recriados por isso)."""
    payload = {"spec": spec, "files": files} if files else spec
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_pod(
    namespace: str,
    slug: str,
    settings: Settings,
    contributions: Sequence[tuple[str, PodContribution]] = (),
) -> dict:
    """Antes desta fatia, este era `build_deployment` (gerava um
    `Deployment` de 1 réplica com `strategy: Recreate`, ver
    docs/ARCHITECTURE.md, seção "Pure Pod instead of Deployment for the
    per-dev workload" pro histórico da investigação e da migração
    real). Migrado pra `Pod` puro:
    `spec.template.spec` do Deployment de antes virou `spec` direto (o
    conteúdo -- containers/volumes/securityContext -- é idêntico,
    bit-a-bit, só o nivelamento do wrapper mudou), com `restartPolicy:
    Always` cobrindo o mesmo caso de restart de container que o
    ReplicaSet cobria na prática pra 1 réplica sem rolling deploy.

    Trade-off aceito conscientemente (documentado em
    docs/ARCHITECTURE.md, não escondido): um `Pod` puro NÃO se
    auto-recria se o objeto Pod
    INTEIRO for removido (crash do node, delete acidental) -- só o
    kubelet reiniciando o CONTAINER dentro dele continua automático.
    Recriação nesse caso exige `reconcile_dev`/`/provision` de novo
    (hoje sempre manual -- não há culling/chamada automatizada).
    """
    pod_labels = {"app": "kirocrew", OWNER_LABEL_KEY: slug}
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"kirocrew-{slug}",
            "namespace": namespace,
            "labels": pod_labels,
        },
        "spec": {
            # Equivalente do restart automatico que o ReplicaSet do
            # Deployment dava pra 1 replica -- kubelet reinicia o
            # CONTAINER (crash, OOM, probe falhando) sozinho. O que ISSO
            # nao cobre (perdido conscientemente, ver docstring da
            # funcao e docs/ARCHITECTURE.md): o Pod inteiro sumir (delete manual, node
            # cair) -- nesse caso ninguem recria sozinho.
            "restartPolicy": "Always",
            "securityContext": {
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            # Sem "affinity" aqui de propósito -- nenhuma restrição
            # de nó genérica faz sentido pra QUALQUER cluster.
            # Se o cluster precisar de uma (ex.: nodeAffinity pro
            # control-plane, exigido pelo CSI do rook-cephfs no
            # homelab), isso entra via overlay JSON
            # Patch (ver app/overlay.py, aplicado no fim desta
            # função) -- nunca hardcoded aqui. Path do overlay agora é
            # relativo a `/spec/...` direto (nao mais
            # `/spec/template/spec/...` -- esse nivel so existia porque
            # Deployment tem um PodTemplateSpec por baixo; Pod puro nao
            # tem esse wrapper).
            "containers": [
                {
                    "name": "kirocrew",
                    "image": settings.kirocrew_image,
                    "ports": [{"containerPort": 5476, "name": "dashboard"}],
                    "envFrom": [
                        {"configMapRef": {"name": f"kiro-config-{slug}"}}
                    ],
                    "env": [
                        {
                            "name": "KIROCREW_OWNER_ID",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": f"kiro-owner-id-{slug}",
                                    "key": "KIROCREW_OWNER_ID",
                                }
                            },
                        },
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    ],
                    "volumeMounts": [
                        {"name": "home", "mountPath": "/home/kirocrew"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                        # Unconfined SO aqui (pod-level continua
                        # RuntimeDefault acima) -- sandbox do Kiro
                        # Crew precisa de unshare(CLONE_NEWUSER),
                        # RuntimeDefault bloqueia isso.
                        "seccompProfile": {"type": "Unconfined"},
                    },
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "2Gi"},
                        "limits": {"cpu": "2", "memory": "8Gi"},
                    },
                    "startupProbe": {
                        "httpGet": {"path": "/api/health", "port": 5476},
                        "failureThreshold": 30,
                        "periodSeconds": 10,
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/api/ready", "port": 5476},
                        "periodSeconds": 10,
                    },
                    "livenessProbe": {
                        "httpGet": {"path": "/api/live", "port": 5476},
                        "periodSeconds": 20,
                    },
                }
            ],
            "volumes": [
                {
                    "name": "home",
                    "persistentVolumeClaim": {
                        "claimName": f"kiro-workspace-{slug}"
                    },
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    # Extensões entram ANTES do overlay (o overlay do admin tem a última
    # palavra) e antes do hash (mudou a contribuição -> recria o Pod).
    extra_annotations = merge_contributions(manifest["spec"], slug, contributions)
    manifest = apply_overlay(manifest, load_overlay_ops(settings, "pod"))
    manifest["metadata"].setdefault("annotations", {}).update(extra_annotations)
    manifest["metadata"]["annotations"][SPEC_HASH_ANNOTATION] = spec_hash(
        manifest["spec"], collect_files(contributions)
    )
    return manifest

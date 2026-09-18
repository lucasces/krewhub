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
README "Namespace compartilhado pra pods de dev").

Como todos os recursos (Secret/ConfigMap/PVC/Service/Deployment/
NetworkPolicy) agora coexistem no MESMO namespace, cada um leva o `slug`
do dev no nome pra não colidir -- e o Deployment/Service/NetworkPolicy
levam a label `OWNER_LABEL_KEY: <slug>` pra que a NetworkPolicy consiga
selecionar SÓ o pod daquele dev (isolamento de rede entre devs agora
depende dessa label, não mais da fronteira do namespace -- ver
build_networkpolicy).

Toda decisão já validada ao vivo nas fatias anteriores está preservada
aqui: nodeAffinity control-plane (CSI cephfs só roda lá), seccompProfile
Unconfined só no container kirocrew (sandbox unshare), fsGroup 1000 (PVC
root-owned), NetworkPolicy só liberando o CHP pra porta 5476, KIROCREW_CORS_ORIGINS
apontando pro host do dev (Host-header allowlist do dashboard)."""

from __future__ import annotations

import re

from app.config import Settings

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Label que discrimina o pod de CADA dev dentro do namespace compartilhado
# -- é o que garante que a NetworkPolicy de um dev não vaze pro pod de
# outro dev no mesmo namespace (ver build_networkpolicy/build_deployment).
OWNER_LABEL_KEY = "krewhub.pespa.net/owner-slug"


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
            "KIROCREW_CORS_ORIGINS": f"http://{host}:{settings.public_port}",
        },
    }


def build_pvc(namespace: str, slug: str, settings: Settings) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"kiro-workspace-{slug}", "namespace": namespace},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": settings.storage_class,
            "resources": {"requests": {"storage": settings.storage_size}},
        },
    }


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
    rejeitado. Testado ao vivo (ver README)."""
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


def build_deployment(namespace: str, slug: str, settings: Settings) -> dict:
    pod_labels = {"app": "kirocrew", OWNER_LABEL_KEY: slug}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"kirocrew-{slug}",
            "namespace": namespace,
            "labels": pod_labels,
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": pod_labels},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "securityContext": {
                        "fsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "affinity": {
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
            },
        },
    }

# KrewHub

Self-service provisioning of **Kiro Crew** development environments on
Kubernetes — one isolated pod per dev, on demand, no manual operator
intervention.

## What it is

KrewHub is a central service (`krewhub-central`) that gives any
authenticated dev their own **Kiro Crew** gateway pod, provisioned on
demand and reachable via a dedicated subdomain — the same model
[JupyterHub](https://jupyterhub.readthedocs.io/) popularized for
notebooks (one pod per user, host-based routing, idempotent
provisioning), applied here to Kiro Crew instead of Jupyter.

A dev opens KrewHub, logs in via OIDC, picks how they want `kiro-cli`
to authenticate (Identity Center or Builder ID), and within seconds has
a pod running with a persistent workspace and its own URL — no cluster
access, no `kubectl`, no waiting on an operator to provision anything
by hand.

## Features

- **OIDC login** (Authorization Code + PKCE) against any compliant IdP
  — 100% config-driven, no hardcoded client.
- **Session configuration lobby**: the dev picks `kiro-cli`'s login
  mode (`org`/Identity Center or `personal`/Builder ID) before the pod
  comes up.
- **Idempotent provisioning**: `POST /devs/{owner_id}/provision`
  creates/updates all of a dev's k8s resources (Secret, ConfigMap, PVC,
  Service, NetworkPolicy, Pod) without duplicating anything on repeated
  calls.
- **Automated `kiro-cli login`** (device flow), no manual TTY from the
  operator required.
- **`/close` and `/logout`** with real workload teardown — always
  preserve the dev's PVC (workspace) and Secret (credential), so a
  subsequent provision rebuilds from scratch with the same history.
- **Per-dev network isolation**: all devs share a single namespace,
  isolated from each other via `NetworkPolicy` (not by namespace
  boundary).
- **Extensions**: optional, admin-enabled plugins that add sidecars,
  files and credentials to each workspace, with a status card in the
  lobby. The first one, `aws-sso`, provides AWS credentials from IAM
  Identity Center — see [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md).
- **Generic Helm chart**: no default assumes a specific cluster's
  StorageClass, domain, or node topology.
- **Per-cluster JSON Patch overlay**: infrastructure peculiarities
  (node affinity, tolerations, storage class) go in through
  configuration, never through a code fork.

## Architecture

`krewhub-central` (FastAPI) exposes login/lobby and does the idempotent
reconcile of the per-dev workload via the Kubernetes API; it then
registers the route with
[`configurable-http-proxy`](#credits) (CHP), which routes by `Host`
header to each pod. All devs live in a single shared namespace —
isolation is via `NetworkPolicy`, not a namespace per dev. The Helm
chart covers only the static infrastructure (`krewhub-central` + CHP);
per-dev resources are managed at runtime, outside the Helm lifecycle.

Operational detail (commands, gotchas) lives in
[`AGENTS.md`](AGENTS.md); the reasoning behind non-obvious design
decisions lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Credits

- **[JupyterHub](https://jupyterhub.readthedocs.io/)** — design
  reference for self-service pod-per-user provisioning
  (`KubeSpawner`/Zero-to-JupyterHub were read as a reference for how to
  do pod+PVC/culling/RBAC). No code was reused, and Jupyter's
  single-user contract was deliberately dropped — KrewHub solves a
  simpler problem (one gateway per dev, not a full Jupyter kernel).
- **[`configurable-http-proxy`](https://github.com/jupyterhub/configurable-http-proxy)**
  — reused as the official binary (the same project JupyterHub uses for
  host-based routing), not reimplemented. It's a real KrewHub
  dependency, running inside the cluster alongside `krewhub-central`.

## Requirements

- A Kubernetes cluster (any distribution — no dependency on a specific
  provider).
- Helm 3.x.
- An existing OIDC/OAuth2 IdP (Keycloak, Google, Microsoft, GitLab,
  etc. — any one with a standard discovery document).
- Access to the Kiro Crew gateway image (`kirocrewImage` in the chart).

> **Bottlerocket nodes (common on EKS/Karpenter nodepools):** the
> hardened AMI ships with `user.max_user_namespaces=0` by default,
> which breaks the Kiro Crew gateway's sandbox (`unshare(CLONE_NEWUSER)`
> fails with `ENOSPC`, shown as "Sandbox unavailable" in the UI). Raise
> that sysctl (`settings.kernel.sysctl` in the node's bootstrap/userdata)
> or schedule dev pods on a nodepool with a different AMI before
> running workloads there. See
> [`charts/krewhub/examples/ec2nodeclass-bottlerocket-sysctl.yaml`](charts/krewhub/examples/ec2nodeclass-bottlerocket-sysctl.yaml)
> for a reference Karpenter `EC2NodeClass` with the sysctl fix applied.

## Installation

The only supported path is via Helm chart, published as an OCI
artifact:

```bash
# Discover published versions
helm show chart oci://ghcr.io/lucasces/charts/krewhub --version <version>

# Install (always with your own values -- the chart default is
# deliberately generic, doesn't assume any specific cluster)
helm install krewhub oci://ghcr.io/lucasces/charts/krewhub \
  --version <version> \
  --namespace krewhub --create-namespace \
  -f your-values.yaml

# Or just render/inspect without installing
helm template krewhub oci://ghcr.io/lucasces/charts/krewhub \
  --version <version> -f your-values.yaml
```

The Secrets referenced in `your-values.yaml`
(`chp.adminToken.existingSecretName`, required; and
`krewhubCentral.oidc.existingSecretName`, optional) must already exist
in the install namespace **before** `helm install` — this chart never
creates any Secret, it only references one by name.

Every field has an example/comment right in
[`charts/krewhub/values.yaml`](charts/krewhub/values.yaml) itself. A
real override example (not generic, so not versioned in git) lives
locally in `charts/krewhub/examples/` once created — useful as a
reference for "what a real filled-in values.yaml looks like", but it
isn't distributed with the chart.

## Configuration

The main `values.yaml` groups to know:

- **`krewhubCentral.devPodTemplate`** — parameters for the per-dev pod
  template (storage class, size, base domain, gateway image, public
  scheme).
- **`krewhubCentral.devPodOverlay`** — JSON Patch overlay (RFC 6902)
  for any cluster peculiarity that doesn't fit a dedicated field
  (nodeAffinity, tolerations, etc.) — see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#per-cluster-json-patch-overlay).
- **`krewhubCentral.extensions`** — `enabled` (comma-separated ids of
  installed extensions to turn on) and `env` (extension-specific
  settings); see [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md).
- **`chp.affinity`/`chp.nodeSelector`/`chp.tolerations`** — proxy
  scheduling, for clusters with dedicated nodepools/taints.
- **`krewhubCentral.oidc`** — reference to the Secret holding the IdP
  credentials. Without this configured, the service still starts up
  fine and `/login` just responds `501` until it's filled in.

Each field has a comment explaining its default and format directly in
the file — this README doesn't duplicate that.

## Usage

Standard dev flow: `/login` (OIDC) → `/lobby` (pick `kiro-cli`'s login
mode) → automatic pod provisioning → Kiro Crew dashboard ready, session
already authenticated.

To end a session: `GET /close` ends the dashboard's work session and
tears down the dev's k8s workload (keeping workspace and credential
intact, so a subsequent provision picks up where it left off); `GET
/logout` does the same and also logs out of KrewHub. Logout revokes
every KrewHub session of that dev, so it ends the session on all their
browsers and devices, not only the one it was called from.

## Known limitations

- **No automatic idle culling** — pods stay up until someone calls
  `/close`/`/logout` manually, or an operator tears them down by hand.
- **The Helm chart isn't yet validated as the sole deployment
  mechanism** in production across every environment — it works
  (`helm lint`/`helm template` without errors), but depends on correct
  RBAC for the target cluster (see the `pods/exec` requirements in
  `docs/ARCHITECTURE.md`).
- **MVP persistence**: state (`owner_id -> pod/host/status`) lives in
  SQLite local to `krewhub-central`, no HA, no operator/CRD.

## Contributing

Changes are made directly by the maintainer; there's no external
contributor process yet. See [`RELEASING.md`](RELEASING.md) for how a
tagged release (image + Helm chart) gets published via GitHub Actions.

## License

[MIT](LICENSE).

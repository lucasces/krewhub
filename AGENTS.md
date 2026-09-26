# AGENTS.md

Operational summary for any agent (Claude or other) working in this
repo. `README.md` is the entry point (what the project does, how to
install/use it, in open-source project style); `docs/ARCHITECTURE.md`
covers the "why" behind non-obvious design decisions. This file is the
third angle: what saves an investigation when touching the code —
environment, technical gotchas, conventions.

Naming note: the project used to be called `KiroHub`, renamed to
`KrewHub` (code, env vars, FastAPI app). `kiro.internal`, `kirocrew`,
`kiro-cli`, `kiro-dev-*` deliberately keep the old name — it's naming
for the **Kiro Crew** product (hosted here), not this hub. The real k8s
namespace and the GitOps directory where CHP/`krewhub-central` run also
keep the old name in live infra (high blast-radius to rename), but no
longer show up hardcoded as a default in the code
(`KREWHUB_K8S_CONTEXT`/`KREWHUB_CHP_NAMESPACE` in `app/config.py` are
generic/empty).

## Environment

- Package/venv manager is `uv` (`pyproject.toml` + `uv.lock`, committed
  — source of truth for deps; no more `requirements*.txt`). Already
  available on this host's PATH (personal nix-profile); if not, a
  standalone `nix shell nixpkgs#uv` works. `python3`/`pip` themselves
  are still off the PATH by default on this NixOS (see the
  `nix-develop` skill), but `uv` doesn't depend on that — it
  downloads/manages the interpreter itself (`.python-version` pins
  `3.13`).
- `uv run pytest` runs the suite directly — no wrapper script. `uv run`
  syncs `.venv/` from the lock on its own before running (tested with
  `.venv/` deleted: `uv run pytest` recreates it and passes the same;
  idempotent/silent on subsequent calls where nothing changed), and
  works from any cwd inside the repo (finds `pyproject.toml` walking
  up, same as `git -C`).
  - `uv run pytest` — runs everything
  - `uv run pytest -k <term>` — only tests matching `<term>`
  - `uv run pytest -x -v` — stop at first failure, verbose
- The suite is offline (k8s/`kubectl exec`/CHP/OIDC are mocked, only
  SQLite runs for real, in `tmp_path`), doesn't touch the real cluster.
  Run it before considering any change done.
- Dev deps (pytest, `httpx2`) live in the `[dependency-groups].dev`
  group of `pyproject.toml` — `uv sync --no-dev` (used in the
  `Dockerfile`) leaves them out of the image.

## Architecture (summary — see `docs/ARCHITECTURE.md` for the why)

- `krewhub-central` (FastAPI, `app/`) exposes OIDC login + lobby, and
  does the idempotent reconcile of a per-dev workload on demand: `POST
  /devs/{owner_id}/provision` creates/updates a Secret, ConfigMap, PVC,
  Service, NetworkPolicy, and a plain `Pod` (not a `Deployment` — see
  gotchas), then registers the route with `configurable-http-proxy`
  (CHP) via `exec` into its pod. Routing is by Host header, not by
  path.
- All devs share a single namespace (`KREWHUB_DEV_NAMESPACE`, default
  `krewhub-devs`), created declaratively in GitOps, not by the
  reconcile. Isolation is via `NetworkPolicy` with a `podSelector` on
  the `krewhub.pespa.net/owner-slug=<slug>` label, not by namespace
  boundary.
- The Helm chart (`charts/krewhub/`) covers only the static infra
  (`krewhub-central` + CHP + the shared Namespace). Per-dev resources
  are managed at runtime by `app/k8s_manager.py`/`app/k8s_templates.py`,
  outside the Helm lifecycle — `helm uninstall`/`upgrade` never touch
  them.
- Production deployment follows normal GitOps/Flux (a separate
  repository, outside this one); the chart still isn't the real
  deployment mechanism.

## Technical gotchas

- The CHP API uses the header `Authorization: token <value>` (not
  `Bearer`) — `app/chp_client.py`. `Bearer` is the format for KrewHub's
  own session token, a different endpoint.
- The Kiro Crew dashboard validates `Host`/`Origin` against
  `KIROCREW_CORS_ORIGINS`. When TLS terminates at the edge
  (LB/Ingress) and the internal backend is plain HTTP,
  `KREWHUB_DEV_POD_SCHEME=https` generates the allowlist with the
  right scheme (there's a real example of this adjustment in a
  third-party environment config, outside this repo); without it the
  CSRF-origin check rejects with 403 even with everything else
  configured correctly.
- `kiro-cli login` inside the pod needs a real TTY — via `kubectl exec`
  without a TTY, the wizard drops the flags or doesn't show the menu.
  The technique (`app/kiro_login.py`, exposed via `POST
  /devs/{owner_id}/kiro-login`) runs a script inside the pod that
  creates its own pty (`pty.spawn`) and reads stdin from a FIFO.
- The Kiro Crew sandbox (`unshare(CLONE_NEWUSER)`) needs
  `seccompProfile: Unconfined` on the container — without it, `EPERM`.
  On hardened nodes (e.g. Bottlerocket on EKS) that zero out
  `user.max_user_namespaces` by default, the same `unshare` fails with
  `ENOSPC` instead — that's a node sysctl, not the chart/app (a
  third-party environment works around this with a dedicated nodepool
  with the sysctl fixed).
- `kind`/`k3d` don't work for an ephemeral test cluster on **this
  NixOS host specifically**: they mount the host's `/lib/modules` at a
  path that doesn't exist in this layout, and expect the Podman socket
  at a fixed path different from the rootless one
  (`$XDG_RUNTIME_DIR/podman/podman.sock`). `podman machine` (an
  ephemeral QEMU/KVM VM, k3s built in) works locally here —
  `integration/engines/podman_machine.py` handles the
  `gvproxy`/`qemu-img`/`virtiofsd` prerequisite that the Nix image
  doesn't bundle. This is a local-host limitation, not a `kind`
  limitation in general: on GitHub-hosted `ubuntu-latest` runners
  (real, non-rootless Docker preinstalled, classic `/lib/modules`
  present), `kind` works cleanly with no workaround needed —
  `release.yml`'s `integration-test` job uses the `kind` engine
  (`integration/engines/kind.py`) precisely because that's true there.
- Multi-arch (amd64+arm64) image builds with `podman`: the `podman
  machine` connection needs to be rootful (`--connection
  <name>-root`) — rootless doesn't propagate binfmt to the build. The
  current `krewhub-central` build is single-arch (plain `podman build`
  + `gh auth token | podman login ghcr.io`); this only comes into play
  if a multi-arch build is ever needed.
- Plain Pod (not `Deployment`) for the per-dev workload: if the whole
  Pod dies (`kubectl delete pod`, kubelet crash, node going down), it
  doesn't recreate itself — no ReplicaSet/controller behind it
  (rationale in `docs/ARCHITECTURE.md`).
- The JSON Patch overlay (`KREWHUB_DEV_POD_OVERLAY_PATH`/`_JSON`) uses
  the top-level key `pod:` (not `deployment:`, since the migration to a
  plain Pod), with RFC 6902 paths relative to `/spec/...` directly (no
  `template.spec` wrapper, which `Deployment` had). An old overlay with
  `deployment:` is silently ignored by the current code — the Pod comes
  up without the expected affinity/tolerations, no error (more
  detail/rationale in `docs/ARCHITECTURE.md`).
- `pods/exec` RBAC: the Python client
  (`connect_get_namespaced_pod_exec`, used to register the route with
  CHP) issues the call as HTTP GET with a websocket upgrade — the
  apiserver validates against the real HTTP verb, not against the
  usual convention "`pods/exec` = verb `create`" (which only holds for
  `kubectl exec`, via POST/SPDY). A `ClusterRole` with only `create` on
  `pods/exec` fails with a 403 "cannot **get** resource pods/exec" —
  it needs `get` too.
- Testing `*.kiro.internal` via local port-forward: always point at the
  CHP Service (`svc/configurable-http-proxy`, the public port from
  `--host-routing`), never directly at an individual app's Service
  (`krewhub-central` or `kirocrew-<slug>`) — CHP is what dispatches by
  `Host` header. Pointing at the final target skips that layer and
  produces a misleading 404 that looks like a routing/RBAC bug.

## Project conventions

- Never commit anything identifying a third-party client, company,
  domain, or person — not in code, commit messages, or as literal
  text in `.gitignore` itself (an entry like `deploy/acme-corp/`
  would leak the name into version control even as an ignore rule).
  The one `.gitignore` entry that exists for this class of file,
  `values-*.yaml`, is a generic filename pattern that names no third
  party — it's what keeps a real, filled-in cluster values file (e.g.
  `charts/krewhub/examples/values-<your-cluster>.yaml`) out of git.
- The Helm chart is generic by design — no default value in
  `charts/krewhub/values.yaml` assumes a specific cluster
  (StorageClass, domain, node topology, secret mechanism). Examples of
  real values live only in `charts/krewhub/examples/`, never as a
  default, and are excluded from the published `.tgz`/OCI artifact.
- The JSON Patch overlay is the mechanism for any cluster peculiarity
  (node affinity, tolerations, fixed storage class) — don't hardcode
  it in `app/k8s_templates.py` (see `docs/ARCHITECTURE.md`).
- Chart resource names are fixed (not generated from
  `<release>-<chart>`).
- All project markdown documentation (`README.md`, `AGENTS.md`,
  `CLAUDE.md`, `docs/*.md`) is written in English — permanent
  convention, not a one-off. Inline code comments/docstrings stay in
  Portuguese for now, unaffected by this.
- Commit messages are always in English, no exceptions, same as the
  markdown convention above.

## Editing third-party-facing docs

Every `*.md` file in this repo except `AGENTS.md`/`CLAUDE.md`
themselves (`README.md`, `RELEASING.md`, `docs/ARCHITECTURE.md`, and
any future doc file) is third-party/end-user facing. It must read as
clean, objective, standalone documentation — zero trace of how or why
an AI assistant arrived at the content. Checklist:

- **No process/session narration.** No "the agent", "this session",
  "we decided", "was evaluated", "at this point", "previously", no
  turn-by-turn reasoning, no hedging filler ("it's worth noting
  that", "it should be mentioned"). State facts directly.
- **No circular/empty statements.** Every sentence must convey a
  concrete, non-obvious fact. Reject anything that just restates its
  own heading or something already implied.
- **No dangling contrast.** Don't leave a sentence that only makes
  sense in reference to something else that has since been
  removed/changed elsewhere in the docs or the code. If the thing it
  contrasts against goes away, re-check the sentence.
- **No cross-doc/cross-section redundancy** — with one legitimate
  exception: deliberately repeating a fact for the skimmability of a
  runbook-style section (e.g. `RELEASING.md`'s "Cutting a release
  candidate" section restating that there's no approval gate, so it
  reads standalone). Call this out explicitly when it's the reason;
  otherwise, redundancy is a bug.
- **"Known limitations" (or similarly named) sections**: every bullet
  must describe an actual gap or weakness — something concretely
  missing, or gated by a specific condition. A bullet describing
  correct/expected behavior (e.g. "endpoint X requires auth") doesn't
  belong there.
- **When editing a doc because code/architecture changed**, don't
  just append new text — re-read the surrounding section for now-
  dangling references, redundant restatements, or context that no
  longer applies, and clean those up in the same change.

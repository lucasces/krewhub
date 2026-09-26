# Architecture

This document covers the "why" behind design decisions that aren't
obvious just from reading the code or the chart's `values.yaml` —
things that, without this explanation, someone would probably
rediscover the hard way (a silent bug, RBAC copied wrong from a generic
example, etc.).

For day-to-day operation (commands, technical gotchas, conventions)
see [`AGENTS.md`](../AGENTS.md). This file is about design, not
workflow.

## Provisioning flow

`POST /devs/{owner_id}/provision` is an idempotent reconcile
(create-if-missing / patch-if-exists) that creates a Secret, ConfigMap,
PVC, Service, NetworkPolicy, and a plain `Pod` for the dev's workload,
waits for it to become `Ready`, and registers the route with
`configurable-http-proxy` (CHP) via `exec` into the pod itself — CHP's
admin API is loopback-only by design, so `krewhub-central` talks to it
from inside the pod instead of opening new network access. `owner_id ->
{namespace, host, status}` lives in SQLite (MVP, no operator/CRD).

`owner_id` doesn't need to be a "real" OIDC claim: in Kiro Crew,
`KIROCREW_OWNER_ID` is just a Slack integration credential (DM gate),
not a workspace identity — `kirocrew` never validates that value
against anything. So `owner_id` here only needs to be stable enough to
name resources with; it becomes a DNS-1123-safe slug
(`app/k8s_templates.py::slugify`).

## Authentication for KrewHub's own endpoints

Endpoints that act on an `owner_id` (`lobby`, `provision`, `session`,
`kiro-login`) require a credential — the `krewhub_session` cookie
(`HttpOnly`, `SameSite=Lax`; `Secure` only when the connection up to
this point was `https://` or came with `X-Forwarded-Proto: https`, so
the browser doesn't silently drop the cookie while CHP speaks plain
HTTP internally) **or** an equivalent `Authorization: Bearer <token>`
header, for programmatic calls that don't rely on a browser cookie.

The token is signed locally (`app/auth.py`, HMAC-SHA256 over
`{owner_id, exp}`), not the IdP's `access_token` — a deliberate
decision: validating the IdP's `access_token` via JWKS would also
work, but a dedicated token avoids depending on network access to the
IdP on every protected request, and avoids spreading a token that
carries IdP scope/permissions (not just identity) further than
necessary. Consequence: since it's stateless HMAC, there's no
server-side revocation of that token — `/logout` clears the browser
cookie, but deliberately resending an old token still authenticates
until its natural `exp`.

A valid credential for an `owner_id` different from the one in the URL
→ explicit `403` (this is what stops one dev from calling
`/devs/ANY-EMAIL/provision` as someone else). No credential → `401`
(programmatic call) or a `302` to `/login` (browser navigation, via
`Accept: text/html`).

`GET /devs/{owner_id}/open` is reachable as a plain browser link (a
dashboard bookmark, for instance), so it follows the same convention as
`GET /`: a missing or expired session on browser navigation
(`Accept: text/html`) yields a `302` to `/login` instead of a raw `401`
— the dev ends up back at their own lobby, not necessarily back at the
original `/open` link, since there is no passthrough of the originally
requested URL through the OIDC flow. A programmatic call without a
session still gets a plain `401`. A valid session for a *different*
`owner_id` (e.g. a link bookmarked by another dev) always gets `403`,
never a silent redirect — that case is a real authorization failure,
not a missing-login case.

## `/close` vs `/logout`

Two different concepts: **`/close`** ends only the dashboard's
(`kirocrew`) work session — real, server-side revocation, via a `POST
/api/logout` local to the pod (`kirocrew` bumps a generation counter
persisted to disk; already-issued `mc_token_*`/`mc_refresh_*` cookies
get rejected on the next request). The dev stays logged into KrewHub.
**`/logout`** does the same revocation **and** clears the
`krewhub_session` cookie **and** redirects to `/login`.

The dashboard's cookies (`mc_token_*`/`mc_refresh_*`) are host-only (no
`Domain` attribute), and `krewhub-central` responds from a different
host than the dev's pod — so a response from KrewHub is structurally
incapable of setting/expiring those cookies in the browser
(cross-origin). The only way to end that session is the server-side
revocation above, never a cleanup `Set-Cookie` coming from KrewHub.

Besides revoking the session, both endpoints also tear down the dev's
k8s workload (Deployment/Service/NetworkPolicy/ConfigMap) — manual,
per-dev, on-demand culling. **PVC (`kiro-workspace-<slug>`) and Secret
(`kiro-owner-id-<slug>`) are never touched**, either by the app or by
RBAC (the `ClusterRole` has no `delete` on
`secrets`/`persistentvolumeclaims` — defense in depth, not just code
discipline). This is what guarantees a subsequent `/provision`
rebuilds the workload from scratch with the same `kiro-cli`
workspace/history.

## RBAC: a non-obvious verb

The Kubernetes Python client (`connect_get_namespaced_pod_exec`, used
to register the route with CHP) issues the exec call as an **HTTP
GET** with a websocket upgrade — the apiserver validates RBAC against
the call's real HTTP verb, not against the usual convention
"`pods/exec` = verb `create`" (which holds for `kubectl exec`, which
uses POST/SPDY). An RBAC copied from generic examples online (just
`create` on `pods/exec`) fails with a 403 "cannot **get** resource
pods/exec" — it needs `get` too.

## Exposure without an Ingress controller

No Ingress controller is assumed. `krewhub-central` registers its own
route with CHP at startup (`KREWHUB_SELF_HOST` configured), reusing the
same host-routing already used for each dev pod, instead of requiring
new Ingress infrastructure.

## Per-cluster JSON Patch overlay

Mechanism (`app/overlay.py`) for any cluster peculiarity (nodeAffinity,
tolerations, fixed storage class) without hardcoding it in Python or
adding a new env var per peculiarity — RFC 6902 (JSON Patch), not
strategic-merge or JSON Merge Patch (RFC 7396): there's no mature
Python library that replicates k8s's by-`name` merge
(containers/volumes), and JSON Merge Patch replaces whole lists (a
`volumes` overlay would wipe out the workspace PVC unless repeated in
full). Each JSON Patch operation is explicit (`add`/`remove`/`replace`
+ `path`) and never deletes what wasn't asked for.

The top-level key is `pod:` (not `deployment:`) since the per-dev
workload's migration to a plain `Pod` — paths are relative to
`/spec/...` directly (no `template.spec` wrapper, which `Deployment`
had). An overlay using the old key is silently ignored by the current
code.

The PVC has a limitation native to k8s, not to this mechanism: the
spec is mostly immutable after creation (only
`resources.requests.storage` can grow) — an overlay that changes
`storageClassName`/`accessModes` only takes effect on PVCs created
AFTER the change; on an existing PVC the apiserver rejects the patch
with 422.

## Pure Pod instead of Deployment for the per-dev workload

The per-dev workload (`kirocrew-<slug>`) is a plain `Pod`, not a
`Deployment`/`ReplicaSet`. Reason: 1 pod = 1 dev = 1 gateway, no
scaling, no real rolling deploy (an image bump was already handled as
a manual restart/recreate) — none of what a `Deployment` exists to
support made sense here. Consciously accepted trade-off: a
`Deployment` automatically recreates if the **whole Pod** dies (kubelet
crash, accidental `kubectl delete pod`, node going down); a plain
`Pod` doesn't — `/provision` needs to run again manually (automatic
idle culling doesn't exist yet, see Limitations in the README).

# Extensions

An extension adds an optional capability to every developer workspace —
extra containers in the dev Pod, configuration files, environment
variables, credentials the developer provides, and a status card in the
lobby with actions. Two extensions ship in this repository: `aws-sso`
gives the workspace AWS credentials obtained through IAM Identity Center
(sidecar, tools image, interactive login), and `github` gives `git` access
to GitHub over HTTPS with a token (no sidecar, no image). The model is
generic: KrewHub core has no knowledge of any specific extension.

## Model

An extension is a Python class derived from `app.extensions.base.Extension`
and has two parts.

**Declarative.** Class attributes describe what it needs:

- `fields` — configuration the developer fills in the lobby.
  `kind` is one of `text`, `select`, `bool`, `secret` (entered by the
  developer, stored only in a Kubernetes Secret) or `generated`
  (created by KrewHub on first use, never shown).
- `actions` — buttons on the card, each with `requires` (condition names
  that must be true for the button to be enabled) and optional `params`.
  A parameter of kind `multiselect` renders a group of checkboxes; see
  [Cards and forms](#cards-and-forms).
- `pod_contribution(ctx)` — a `PodContribution` merged into the Pod:
  sidecar containers, volumes, mounts, env for the main container, files
  rendered into a ConfigMap, [binaries](#tools-and-skills-in-the-main-container)
  exposed in the main container and agent skills.

**Imperative (all optional).**

- `validate(config)` — extra validation beyond the field specs.
- `status(ctx)` — returns the extension's conditions (booleans), its
  state and the card to render. Runs on every card refresh.
- `handle_action(ctx, action_id, params)` — runs when a button is
  pressed.
- `lobby_card(ctx)` — a card for the lobby when it should differ from
  the one returned by `status`.
- `on_pod_ready(ctx)` — runs once after each provision, best effort.

The context passed to hooks offers `exec` (run a shell script in a
container of the dev Pod and get its output exactly as printed),
`run_detached` (run a long-lived command in a
pseudo-terminal and read its output until a marker appears, for flows
like device-code logins), `get_secret` and a persisted `state` dict.

The dataclasses and base classes in `app/extensions/base.py` listed in
`__all__` are the public API. `API_VERSION` follows semver for that
contract only.

### States and conditions

Conditions are named booleans. KrewHub always provides `pod.ready` and
`sidecar.<name>.running`; an extension adds its own. The card state is:

| State | Meaning |
|---|---|
| `inactive` | The developer has not enabled the extension. |
| `pending` | The Pod is not ready yet. |
| `needs_action` | The developer must do something (usually press a button), or the extension is waiting for something outside the lobby. |
| `ready` | Working. |
| `degraded` | Working partially. |
| `error` | Invalid configuration, or a hook raised an exception. The card shows a generic message; the exception goes to the log only. |

### Cards and forms

The lobby is server-rendered without JavaScript. Cards are served by
`GET /devs/{owner_id}/extensions/cards` inside an iframe and refresh
themselves with a `<meta refresh>` while any extension is `pending` or
sets `Card.polling`. An extension sets `polling` only while it waits for
something outside the lobby, such as the developer authorizing a login in
another tab: a refresh discards what is typed into an action form, so
`needs_action` alone does not trigger it.
All extension text is HTML-escaped by a single renderer, and links are
accepted only with `http`/`https`. Actions are `POST
/devs/{owner_id}/extensions/{ext_id}/actions/{action_id}`: browsers send
an HMAC anti-CSRF token (derived from the session secret, valid for one
hour) and receive a redirect back to the cards; clients that authenticate
with `Authorization: Bearer` get JSON and need no token. The lobby form
(`POST /devs/{owner_id}/lobby`, which enables extensions and stores their
secrets) follows the same rule: it embeds a token bound to the owner and
answers 403 without a valid one.
`GET /devs/{owner_id}/extensions` returns the state of all extensions as
JSON.

**Multi-select parameters.** An action parameter of kind `multiselect`
(valid only in `ActionSpec.params`) is rendered as checkboxes. The options
are dynamic, so the extension returns them from `status()` in
`Status.choices`, keyed `"<action id>.<param key>"`, as `Choice(value,
label, checked)` tuples; `checked` pre-marks a box, so a form can show the
current selection. `handle_action` receives the checked values as a tuple
in `params`. The core rejects the request (HTTP 400) when a value is not
among the options offered by the last `status()` call or, for a `required`
parameter, when none is checked. Labels are HTML-escaped like any other
extension text.

## Installing and enabling

Two separate steps with two different owners:

1. **Installed** — the extension's pip package is present in the
   `krewhub-central` image and publishes an entry point in the group
   `krewhub.extensions`:

   ```toml
   [project.entry-points."krewhub.extensions"]
   aws-sso = "krewhub_ext_aws_sso:AwsSsoExtension"
   ```

   The published `krewhub-central` image of a stable release does **not**
   include any extension: the release workflow builds it without
   `KREWHUB_EXTENSIONS`. To use extensions, build your own image with that
   build argument (see below), or try the test image that release
   candidate tags (`-rc.`) publish as `krewhub-central:aws-sso-github-test-<version>`,
   which has `aws-sso` and `github` installed.

2. **Enabled** — the administrator lists the extension ids in
   `KREWHUB_EXTENSIONS_ENABLED` (comma-separated; Helm value
   `krewhubCentral.extensions.enabled`). Every installed extension is
   imported and instantiated when the registry loads, listed or not. One
   that is not listed has no form fields, no endpoints, receives no actions
   and contributes nothing to Pods.

Developers then opt in per workspace in the lobby.

### Installation in the image

`krewhub-central` is not itself an installable package
(`[tool.uv] package = false`), so a plugin cannot declare it as a
dependency; it imports `app.extensions.base` from the host's module
path and must not depend on `krewhub`. The image installs plugins into
the application's virtual environment through a build argument:

```sh
# from a checkout of this repository
docker build \
  --build-arg KREWHUB_EXTENSIONS="./extensions/aws-sso ./extensions/github" .
```

The argument is a space-separated list of pip requirements, here local
paths into the checkout. The in-tree extensions are not published to PyPI
or to any other public index, so never install them by package name: a
name resolved from a public index is not the code in this repository. If
you publish an extension package to a private index, install it with an
exact version from a requirements file with hashes and `--require-hashes`
(the Dockerfile's install step does not enable hash checking by itself).
For local development the repository lists `krewhub-ext-aws-sso` and
`krewhub-ext-github` as editable path dependencies in the `dev`
dependency group, so `uv run pytest` exercises the real entry points.

### Supply-chain risk

Extension code runs inside `krewhub-central`, whose service account has
`pods/exec` on every developer Pod and access to the per-developer
Secrets. A malicious or compromised extension package can therefore read
and run anything in every workspace. The mitigation is administrative:
install only code you have reviewed, build from a pinned commit of the
paths in `KREWHUB_EXTENSIONS` (an exact version with hashes for any
indexed package), and rebuild deliberately when upgrading. Enabling
is a second gate, but it does not make an installed package safe.

## Secrets lifecycle

Per-developer values live in the Kubernetes Secret `krewhub-ext-<slug>`
with keys `<ext_id>.<field>`. KrewHub's SQLite database stores only
whether a secret is set and when, never its value.

- `secret` fields are written to the Secret when the lobby form is
  submitted; leaving the field blank keeps the existing value.
- `generated` fields are created on first use and never rotated.
- Disabling an extension removes its keys.
- **Rotation does not recreate the Pod.** The spec hash covers the spec
  and the contributed files, never secret values. A secret exposed as an
  environment variable (`secret_env`, i.e. `secretKeyRef`) is read only
  when the container starts, so a changed value reaches the Pod at its
  next restart. A secret mounted as a file through an extension-owned
  `secret` volume is refreshed in place by the kubelet (about a minute),
  so a program that reads the file on demand sees the new value without a
  restart. Prefer the file when the credential can change while the Pod
  runs (the `github` extension does).
- `/close` removes the `generated` keys of every extension.
- `/logout` removes **all** extension keys.

Keys are removed with a JSON merge patch that sets them to `null`; the
Secret object itself is never deleted, matching the RBAC rule that
`krewhub-central` cannot delete Secrets. Files contributed by extensions
go into the ConfigMap `krewhub-ext-files-<slug>`, which is deleted on
teardown. Because the Pod spec is immutable, KrewHub stores a hash of the
spec — including the content of those files — in an annotation and
recreates the Pod when it changes.

**When the Pod is recreated.** Recreation interrupts whatever runs in the
Pod (the workspace stays on the PVC), so only an explicit `POST` does it:
saving the lobby form, `POST /devs/{owner_id}/provision`, or the "Apply
update" button. A `GET` (`/open`, the lobby) never deletes a running Pod.
When the hash diverges, the lobby shows a "pending update" notice with a
button (`POST /devs/{owner_id}/lobby/apply-update`, protected by the same
kind of anti-CSRF token) and `/open` redirects (303) to the lobby instead
of the dashboard. If the old Pod does not disappear within 120 seconds or
another request creates the new one first (409), the request fails with
`503` and `Retry-After`, and trying again completes it.

## Tools and skills in the main container

The main container (`kirocrew`) has a read-only root filesystem, runs as
a non-root user and drops all capabilities, so an extension cannot install
anything into it at run time. Two contribution types cover what a
workspace needs from an extension beyond environment variables.

**`tools`: binaries on the main container's `PATH`.** A `ToolsSpec`
(`image`, `command`, optional `bin_dir`, `size_limit`, `skills` and
`skills_dir`) makes KrewHub add:

1. an `emptyDir` volume `<id>-tools`;
2. an init container `<id>-tools` that runs `command` in `image` with the
   volume writable at `/tools`; it has the same hardening as the main
   container (non-root, read-only root filesystem, no capabilities);
3. the same volume, read-only, at `/opt/krewhub-ext/<id>` in the main
   container, with `/opt/krewhub-ext/<id>/<bin_dir>` prepended to its
   `PATH`.

`command` copies the files into `/tools` and must leave `<bin_dir>/` filled
in; `tools_copy_command(source_dir)` returns a ready-made one. The result
is a per-Pod copy that disappears with the Pod; changing `image` or
`command` changes the spec hash and recreates the Pod.

Do not use `cp -a` or any `--preserve` option in `command`. The root of
the `emptyDir` belongs to root and the init container has neither that
ownership nor `CAP_FOWNER`, so setting its timestamps or mode fails with
`Operation not permitted` and the init container crash-loops.
`tools_copy_command` runs `cp -dR`, which keeps symlinks and the
executable bit.

Alternatives considered:

- *A sidecar that populates a shared volume.* Sidecars and the main
  container start in parallel, so the binary may not exist when the main
  container starts. An init container finishes first.
- *A derived `kirocrew` image.* It would tie every cluster to one
  extension set and break the administrator's choice of `kirocrew` image
  (`KREWHUB_KIROCREW_IMAGE`).

An environment variable replaces the image's own, and Kubernetes does not
expand `$(PATH)` with a value that comes from the image, so the `PATH`
KrewHub sets is the tool directories followed by the Debian default
`/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin`. An
extension cannot define `PATH` itself when it contributes tools; the
administrator's overlay can still change it.

**Skills: instructions for the agent.** The agent in `kirocrew` (Kiro
Crew) discovers skills by scanning `~/.kiro/skills/<name>/SKILL.md` at
every invocation and indexes each one by its `name` and `description`
frontmatter. A skill appears when the extension is enabled and disappears
when it is not, with no configuration by the developer. In both delivery
paths `<name>` must be the extension id or start with `<id>-`, the
content must start with a frontmatter block containing `name: <name>`,
and the skill is mounted read-only. Two ways to deliver one:

| | `PodContribution.skills` | `ToolsSpec.skills` |
|---|---|---|
| Content comes from | the extension's Python code, as `{name: SKILL.md text}` | the extension image, at `<skills_dir>/<name>/` (default `skills/`), copied by the `tools` init container |
| Stored in | the ConfigMap `krewhub-ext-files-<slug>`, mounted from `SKILL.md` | the `<id>-tools` volume, mounted from the `<skills_dir>/<name>` sub-path |
| Size | shares the 1 MiB ConfigMap limit with every extension's `files` for that developer | no practical limit; a skill can include several files, such as scripts |
| Needs an image | no | yes (the `tools` image) |
| Spec hash follows | the skill text | the image tag |
| Editing a skill | change the text and release the central image | change the file, rebuild the extension image and bump its tag |

Use `PodContribution.skills` for a short skill, or when the extension has
no image of its own. Use `ToolsSpec.skills` when the extension already
ships a `tools` image and the skill is long, has several files or should
be versioned with the tools it describes. Because KrewHub does not see
the image, it does not check that `<skills_dir>/<name>/SKILL.md` exists
or that its frontmatter matches `<name>`; the extension tests the file it
ships. A name used in both paths is rejected, since both would mount at
the same path.

Neither path updates a running Pod: a change to a ConfigMap skill or to
the image tag changes the spec hash and recreates the Pod.

## Writing an extension

- Keep the package free of a `krewhub` dependency; import only from
  `app.extensions.base`.
- Name sidecar containers after the extension id. The main container is
  always `containers[0]`; sidecars are appended after it. Pod readiness
  looks at the main container only, so sidecars must not define a
  readiness probe.
- The root filesystem is read-only; mount an `emptyDir` wherever the
  sidecar writes.
- Pick how a secret reaches the container: `secret_env` for values read
  once at start-up, an own `secret` volume (with `items` and
  `optional: true`) for values that must follow rotation. Mounts of the
  `files` ConfigMap use `subPath` and are static: changing their content
  changes the hash and recreates the Pod.
- Never put a secret in a card, a log line or an exception message that
  can reach `ActionResult.message`.
- Test the extension with the same fixtures the repository uses: fake
  Kubernetes clients, `pod_exec.exec_sh` and `pod_exec.run_detached`
  patched, and `extensions.reset_for_tests()` to control discovery (see
  `tests/test_ext_aws_sso_e2e.py`).

## AWS SSO (`aws-sso`)

Gives the workspace AWS credentials from IAM Identity Center, for one or
more roles at the same time. Any AWS SDK or CLI in the main container
picks them up automatically, with no login inside the workspace.

**Lobby fields.** `start_url` (the `*.awsapps.com` Identity Center
start URL, required), `sso_region` (required) and `default_region`
(defaults to `sso_region`).

**How it works.**

- The sidecar container runs [`aws-sso-cli`](https://github.com/synfinatic/aws-sso-cli)
  and a small supervisor. `aws-sso ecs server` listens on
  `127.0.0.1:4144` inside the Pod and serves credentials in the
  container-credentials format; the main container reaches it through
  `AWS_CONTAINER_CREDENTIALS_FULL_URI` and
  `AWS_CONTAINER_AUTHORIZATION_TOKEN`.
- The server only answers requests carrying a random bearer token that
  KrewHub generates, stores in the Secret and injects into both
  containers.
- The SSO token and cached roles live in a private `emptyDir` of the
  sidecar, not shared with the main container. Login is therefore redone
  every time the Pod is recreated.
- The `start_login` action starts a device-code login in the sidecar
  and shows the verification link and code on the card. Once the
  developer authorizes in the browser, the supervisor lists the roles;
  with a single role it is selected automatically, otherwise the
  developer ticks the roles to use in the `apply_roles` form (a
  [multi-select parameter](#multi-select-parameters)).
  `refresh_roles` re-reads the available roles and `reload_creds`
  refreshes the credentials of every active role.
- Roles are identified by `<12-digit account id>:<role name>` (for
  example `000123456789:AdministratorAccess`). The generated
  `config.yaml` sets `ProfileFormat` to `{{ .AccountIdPad }}:{{ .RoleName }}`
  because the `aws-sso-cli` default embeds the account name, which can
  contain parentheses, accents and other characters. `apply_roles`
  accepts only that format. The card shows each account name next to its
  profile as a label, so the developer still sees which account is which,
  but the name is never used as a value.

**Multiple roles.** `apply_roles` takes the *desired set* of active roles
(at most 10): each submission replaces the previous set instead of adding
to it, and the form shows the current roles first, ticked.

- The first role of the set is the **default**: the supervisor loads it
  without a slot, so it is served on the endpoint the SDKs already use
  (`/`) and plain `aws` commands run as that role. The card marks it
  "(padrão)" when more than one role is active.
- Every active role also gets a named profile `<account id>:<role name>`
  in a managed AWS config file. The supervisor writes it into an
  `emptyDir` that only the sidecar can write and the main container
  mounts read-only (`/etc/krewhub/aws-sso/config`, exported as
  `AWS_CONFIG_FILE`). Each profile uses a `credential_process`, a small
  standard-library Python helper (`krewhub-aws-sso-creds`, shipped in the
  tools volume) that reads the role's slot (`/slot/<profile>`) from the
  local credential server with the bearer token. `aws configure
  list-profiles` lists the active roles; `--profile <name>` or
  `AWS_PROFILE` selects one.
- The supervisor keeps one slot per non-default role, reloads all of
  them in its regular refresh cycle (and on `reload_creds`), retries a
  failed load every 30 seconds, and removes the slots of roles that were
  unticked. The card flags a role whose credentials are not loaded.
- The selection is kept in the sidecar's private state, so it survives a
  supervisor restart and a new login.

**`~/.aws/config` is not read while the extension is active.** The AWS
CLI reads one config file, the one `AWS_CONFIG_FILE` names; it does not
merge it with `~/.aws/config`. To use a personal config in the
workspace, export another `AWS_CONFIG_FILE` in the shell. The managed
profiles then disappear from that shell, but the default role still
works, because it does not depend on the config file.

- The main container gets the [AWS CLI v2](https://docs.aws.amazon.com/cli/)
  through the `tools` contribution (`aws` on `PATH`; the init container
  copies it out of the extension image) and the skill `aws-sso`, which
  ships in the same image (`extensions/aws-sso/skills/aws-sso/SKILL.md`)
  and is delivered by the same init container. The skill
  tells the agent how the default role and the named profiles work, to
  list them with `aws configure list-profiles`, to confirm the role with
  `aws sts get-caller-identity`, to ask before changing resources, never
  to run `aws sso login` or `aws configure`, and to send the developer to
  the lobby card when credentials are missing or expired.

**Image.** The extension image is built from `extensions/aws-sso/` with
`aws-sso-cli` and the AWS CLI v2 pinned by version and SHA-256 (the CLI per
architecture), runs as a non-root user and drops all capabilities. Pods use
it both for the sidecar and for the `aws-sso-tools` init container. The
release workflow publishes it as
`ghcr.io/<owner>/krewhub-ext-aws-sso`. The image used by Pods must be set
explicitly with the environment variable `KREWHUB_EXT_AWS_SSO_IMAGE` (Helm:
`krewhubCentral.extensions.env`); there is no default tag. While it is
unset or blank the extension fails validation with an error naming the
variable: the lobby form refuses to enable it, an already enabled one shows
an `error` card and contributes nothing to the Pod, so Pods never wait on an
image that cannot be pulled.

**Helm example.**

```yaml
krewhubCentral:
  extensions:
    enabled: "aws-sso"
    env:
      KREWHUB_EXT_AWS_SSO_IMAGE: "ghcr.io/example/krewhub-ext-aws-sso:<version>"
```

`enabled` only takes effect if the `krewhub-central` image you deploy has
the extension installed: the stable image does not (see "Installing and
enabling"), so point `krewhubCentral.image.repository` and `krewhubCentral.image.tag`
at your own build or at an `aws-sso-github-test-<version>` image.

## GitHub (`github`)

Gives `git` access to GitHub (or GitHub Enterprise Server) over HTTPS
with a personal access token. It is the minimal shape of an extension:
declarative only, with no sidecar, no tools and no image of its own.

**Fields.** `token` (secret, required; use a fine-grained token limited to
the repositories the developer needs) and `host` (default `github.com`; a
host name with an optional port, validated by a strict pattern because it
is written into a configuration file).

**What the Pod gets.**

- An extension-owned `secret` volume exposes the token as the read-only
  file `/etc/krewhub/github/token`. It is `optional`, so the Pod starts
  even if the token has not been saved yet.
- `/etc/gitconfig`, mounted from the extension's files ConfigMap, holds a
  `credential` section for `https://<host>` whose helper reads that file at
  every git operation and answers only `get` requests. If the file is
  missing or empty the helper prints nothing and git falls back to its own
  prompt. The token is never written to the configuration.
- No environment variable carries the token, so it does not show up in
  `env`, `/proc/<pid>/environ` of other processes or `kubectl describe`.

**Rotation.** Saving a new token in the lobby patches the Secret; the
kubelet refreshes the mounted file within about a minute and the next git
operation uses it. The Pod is not recreated. Changing `host` changes the
rendered `/etc/gitconfig`, hence the spec hash, and recreates the Pod.

The card is `ready` whenever the extension is enabled and has no actions;
whether the token is valid is only known to GitHub when git uses it.

### Comparison

| | `aws-sso` | `github` |
|---|---|---|
| Containers | sidecar + init container | none |
| Image | `krewhub-ext-aws-sso` | none |
| Credential source | interactive login in the lobby | token typed in the lobby |
| Secret delivery | `generated` bearer token as an environment variable | `secret` field as a mounted file |
| Pod files | skill and a managed AWS config | `/etc/gitconfig` |
| Actions | login, role selection, reload | none |
| Rotation | not applicable (short-lived, refreshed by the sidecar) | kubelet refresh, no Pod restart |

## Known limitations

- **The tool directories are prepended to a fixed default `PATH`.** If a
  `kirocrew` image changes its own `PATH`, the one KrewHub sets replaces
  it; adjust it with the Pod overlay.
- **Tools are copied per Pod.** The AWS CLI occupies about 275 MB of node
  ephemeral storage per workspace and is copied at every Pod start.
- **Changing the default role means unticking it first.** The form
  returns the ticked roles in the order of the options, with the active
  roles first, so a newly ticked role never precedes the current default.
  Untick the default (the next active role becomes the default) or
  untick everything and tick the roles again in the wanted order.
- **Only the first 300 roles are offered.** The supervisor publishes at
  most 300 role names to the card; roles beyond that are not selectable.
- **The default role's credentials stay served until replaced.** Slots of
  unticked roles are deleted from the credential server, but
  `aws-sso-cli` 2.3.2 cannot clear the default endpoint (deleting it
  makes the server fail on the next read), so it is only replaced by the
  next default.
- **The bearer token is visible inside the Pod.** It is passed to
  `aws-sso setup ecs auth --bearer-token` and so appears in the
  sidecar's process arguments, and it is an environment variable of the
  main container. Anything running in the workspace can read it and call
  the loopback credential server.
- **The credential server speaks plain HTTP on loopback.** `aws-sso`'s
  TLS mode is disabled; traffic never leaves the Pod network namespace.
- **`github`: a rotated token takes up to about a minute to arrive.** The
  kubelet refreshes mounted Secret volumes periodically; git operations
  started before that still use the old token.
- **`github`: only HTTPS remotes are covered.** SSH remotes are not
  configured, and one token serves one host.
- **`github`: the developer's own `~/.gitconfig` still applies.** Git
  consults credential helpers in configuration order, system file first,
  so this helper answers before one set in the user's configuration; a
  `helper =` line with an empty value in `~/.gitconfig` resets the list
  and disables it.
- **`github`: `/etc/gitconfig` is replaced, not merged.** It is mounted
  with `subPath`, so a file of the same name in the base image would be
  hidden. The current `kirocrew` image does not ship one.
- **The token is readable inside the Pod.** Anything running in the
  workspace as the same user can read `/etc/krewhub/github/token`; that is
  inherent to letting `git` use it.

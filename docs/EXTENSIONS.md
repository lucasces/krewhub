# Extensions

An extension adds an optional capability to every developer workspace —
extra containers in the dev Pod, configuration files, environment
variables, credentials the developer provides, and a status card in the
lobby with actions. The first extension, `aws-sso`, gives the workspace
AWS credentials obtained through IAM Identity Center. The model is
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
with `Authorization: Bearer` get JSON and need no token.
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

2. **Enabled** — the administrator lists the extension ids in
   `KREWHUB_EXTENSIONS_ENABLED` (comma-separated; Helm value
   `krewhubCentral.extensions.enabled`). An installed extension that is
   not listed is never loaded into the request path: it has no form
   fields, no endpoints and contributes nothing to Pods.

Developers then opt in per workspace in the lobby.

### Installation in the image

`krewhub-central` is not itself an installable package
(`[tool.uv] package = false`), so a plugin cannot declare it as a
dependency; it imports `app.extensions.base` from the host's module
path and must not depend on `krewhub`. The image installs plugins into
the application's virtual environment through a build argument:

```sh
docker build \
  --build-arg KREWHUB_EXTENSIONS="krewhub-ext-aws-sso==0.1.0" .
# or, from the monorepo checkout:
docker build --build-arg KREWHUB_EXTENSIONS="./extensions/aws-sso" .
```

The argument is a space-separated list of pip requirements. For local
development the repository lists `krewhub-ext-aws-sso` as an editable
path dependency in the `dev` dependency group, so `uv run pytest`
exercises the real entry point.

### Supply-chain risk

Extension code runs inside `krewhub-central`, whose service account has
`pods/exec` on every developer Pod and access to the per-developer
Secrets. A malicious or compromised extension package can therefore read
and run anything in every workspace. The mitigation is administrative:
install only packages you have reviewed, pin an exact version in
`KREWHUB_EXTENSIONS`, and rebuild deliberately when upgrading. Enabling
is a second gate, but it does not make an installed package safe.

## Secrets lifecycle

Per-developer values live in the Kubernetes Secret `krewhub-ext-<slug>`
with keys `<ext_id>.<field>`. KrewHub's SQLite database stores only
whether a secret is set and when, never its value.

- `secret` fields are written to the Secret when the lobby form is
  submitted; leaving the field blank keeps the existing value.
- `generated` fields are created on first use and never rotated.
- Disabling an extension removes its keys.
- `/close` removes the `generated` keys of every extension.
- `/logout` removes **all** extension keys.

Keys are removed with a JSON merge patch that sets them to `null`; the
Secret object itself is never deleted, matching the RBAC rule that
`krewhub-central` cannot delete Secrets. Files contributed by extensions
go into the ConfigMap `krewhub-ext-files-<slug>`, which is deleted on
teardown. Because the Pod spec is immutable, KrewHub stores a hash of the
spec — including the content of those files — in an annotation and
recreates the Pod when it changes.

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
- Never put a secret in a card, a log line or an exception message that
  can reach `ActionResult.message`.
- Test the extension with the same fixtures the repository uses: fake
  Kubernetes clients, `pod_exec.exec_sh` and `pod_exec.run_detached`
  patched, and `extensions.reset_for_tests()` to control discovery (see
  `tests/test_ext_aws_sso_e2e.py`).

## AWS SSO (`aws-sso`)

Gives the workspace AWS credentials from IAM Identity Center. Any AWS
SDK or CLI in the main container picks them up automatically, with no
profile or login inside the workspace.

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
  developer picks one with `apply_roles`. `refresh_roles` re-reads the
  available roles and `reload_creds` refreshes the credentials.
- Roles are identified by `<12-digit account id>:<role name>` (for
  example `000123456789:AdministratorAccess`). The generated
  `config.yaml` sets `ProfileFormat` to `{{ .AccountIdPad }}:{{ .RoleName }}`
  because the `aws-sso-cli` default embeds the account name, which can
  contain parentheses, accents and other characters. `apply_roles`
  accepts only that format. The card shows each account name next to its
  profile as a label, so the developer still sees which account is which,
  but the name is never used as a value.

- The main container gets the [AWS CLI v2](https://docs.aws.amazon.com/cli/)
  through the `tools` contribution (`aws` on `PATH`; the init container
  copies it out of the extension image) and the skill `aws-sso`, which
  ships in the same image (`extensions/aws-sso/skills/aws-sso/SKILL.md`)
  and is delivered by the same init container. The skill
  tells the agent that credentials come from the container-credentials
  endpoint with no profiles, to confirm the active role with
  `aws sts get-caller-identity`, to ask before changing resources, and to
  send the developer to the lobby card when credentials are missing or
  expired. One role is active at a time, so the skill does not list roles.

**Image.** The extension image is built from `extensions/aws-sso/` with
`aws-sso-cli` and the AWS CLI v2 pinned by version and SHA-256 (the CLI per
architecture), runs as a non-root user and drops all capabilities. Pods use
it both for the sidecar and for the `aws-sso-tools` init container. The
release workflow publishes it as
`ghcr.io/<owner>/krewhub-ext-aws-sso`. Set the image used by Pods with
the environment variable `KREWHUB_EXT_AWS_SSO_IMAGE` (Helm:
`krewhubCentral.extensions.env`).

**Helm example.**

```yaml
krewhubCentral:
  extensions:
    enabled: "aws-sso"
    env:
      KREWHUB_EXT_AWS_SSO_IMAGE: "ghcr.io/example/krewhub-ext-aws-sso:0.1.0"
```

## Known limitations

- **The tool directories are prepended to a fixed default `PATH`.** If a
  `kirocrew` image changes its own `PATH`, the one KrewHub sets replaces
  it; adjust it with the Pod overlay.
- **Tools are copied per Pod.** The AWS CLI occupies about 275 MB of node
  ephemeral storage per workspace and is copied at every Pod start.
- **The `aws-sso` device-code login is not validated on a real cluster.**
  The code assumes `aws-sso login --url-action print` runs through the
  same pseudo-terminal driver as the Kiro login and prints the
  verification URL and a `XXXX-XXXX` code on its output. The parsing is
  tested against a representative log, not against the real binary's
  output without a TTY.
- **The bearer token is visible inside the Pod.** It is passed to
  `aws-sso setup ecs auth --bearer-token` and so appears in the
  sidecar's process arguments, and it is an environment variable of the
  main container. Anything running in the workspace can read it and call
  the loopback credential server.
- **The credential server speaks plain HTTP on loopback.** `aws-sso`'s
  TLS mode is disabled; traffic never leaves the Pod network namespace.

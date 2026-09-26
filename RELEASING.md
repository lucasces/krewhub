# Releasing

This project publishes two artifacts on every release: the
`krewhub-central` container image (multi-arch, `linux/amd64` +
`linux/arm64`) and the `charts/krewhub` Helm chart, both to GHCR
(`ghcr.io/<owner>`). Both are built and pushed by
[`.github/workflows/release.yml`](.github/workflows/release.yml),
triggered by pushing a tag matching `v*`.

Pushing the tag *is* the release action; there is no manual approval
gate in front of publishing. `release.yml` runs the CI checks first
(`needs:` on the same test/lint job used for every push and PR), then
builds and pushes the image and the chart automatically.

Confirm, in **Settings → Actions → General → Workflow permissions**,
that "Read and write permissions" is selected (or that `packages: write`
is otherwise granted to the default `GITHUB_TOKEN`) — see "Auth" below.

## Cutting a release candidate

```bash
git tag v1.2.3-rc.1
git push origin v1.2.3-rc.1
```

Any tag whose version part (after stripping the leading `v`) is **not**
a clean `MAJOR.MINOR.PATCH` (e.g. `1.2.3-rc.1`, `1.2.3-beta.2`) is
treated as a release candidate. `release.yml`:

- Runs the same tests/lint as `ci.yml` first (`needs:` on that job).
- Derives the version (`1.2.3-rc.1`) and builds+pushes the image tagged
  `1.2.3-rc.1` (no `latest` tag — RCs never move `latest`).
- Packages the chart with `version`/`appVersion` set to `1.2.3-rc.1` and
  pushes it to `oci://ghcr.io/<owner>/charts`.
- Publishing starts immediately once CI passes — no approval step.

## Promoting to a stable release

Once an RC has been validated, cut the stable tag on the same (or a
later) commit:

```bash
git tag v1.2.3
git push origin v1.2.3
```

This is a clean `MAJOR.MINOR.PATCH` tag, so `release.yml` treats it as
stable: image tagged `1.2.3` **and** `latest`, chart packaged/pushed as
`1.2.3`.

There's no dedicated "promote" command — a stable tag is just a normal
`v*` tag that happens to be pre-release-suffix-free. Nothing carries
over automatically from the RC tag; the stable build is produced fresh
from whatever commit is tagged.

## What CI actually checks before anything ships

`ci.yml` (also reused by `release.yml` as its first job) runs, on every
push to `main` and every pull request:

- `uv run pytest` — the full test suite.
- `helm lint charts/krewhub`.
- `helm template` against a throwaway, fully generic/fictitious values
  file (built inline in the workflow — never
  `charts/krewhub/examples/values-<your-cluster>.yaml`, which
  documents one specific real cluster, is gitignored, and is never
  available to CI or the published chart).

The chart-publish job additionally re-verifies, on the packaged `.tgz`
itself, that `examples/` and any `values-*.yaml` never leak into the
artifact.

## Auth

Both the image and chart publish jobs log in to `ghcr.io` using
`${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}` (no PAT). This
works out of the box for a **new** package that doesn't exist in GHCR
yet — the first authenticated push creates it, linked to this
repository, inheriting the repository's visibility.

The image (`krewhub-central`) and the chart (under
`ghcr.io/<owner>/charts`) were previously pushed **by hand**, under the
maintainer's personal account/token, before this workflow existed. If
those packages still only grant access to that personal account (rather
than being linked to this repository), the first automated push from
`GITHUB_TOKEN` can fail with a permissions error even with "Read and
write permissions" enabled at the repository level — GHCR packages have
their own, separate "Manage Actions access" setting per package. If that
happens: open the existing package's settings on GHCR and add this
repository with **Write** role, or delete/re-push the package once so
it gets (re)created and linked automatically by the workflow. No PAT
secret should be needed for the normal case; only reach for one if the
above doesn't resolve it.

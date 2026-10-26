# Releasing Chudware

Browser containers and native installers have independent publication paths.
The browser path is [`.github/workflows/self-host.yml`](../.github/workflows/self-host.yml);
the native path is [`.github/workflows/release.yml`](../.github/workflows/release.yml).
The public MIT source is at https://github.com/Hunter-124/Chudware.
The browser image is public at `ghcr.io/hunter-124/chudware-web`; the default
quickstart downloads its `edge` channel. Existing native releases are
prereleases/runtime assets, not a public stable desktop installer.

Native installers use signed update and payload manifests. Signing keys and
optional platform certificates remain described in [SIGNING.md](SIGNING.md).
The complete workflow inventory is in [reference/CI.md](reference/CI.md).

## Credential checks before publication

Preserving development history also preserves old credential-bearing blobs. Revoke
or rotate exposed project credentials **before** publishing source, tags or assets;
deleting a file from the current checkout is not revocation.

- Audit every reachable ref and release source/binary, plus available public issue,
  pull-request and Actions-log content. Record unavailable evidence explicitly.
- Require evidence for the exact credential: an authoritative provider revocation,
  exact-resource deletion, or a supported verification endpoint's explicit rejection.
  A missing token name, scanner `verified=false`, an unsupported token type, or a
  failed network request is not proof. Classify public native OAuth client
  configuration separately from user access/refresh tokens and service credentials.
- Keep raw candidates and provider responses out of logs, commits and reports.
  Use domain-separated fingerprints: a bare hash can itself be a credential
  (Cloudflare R2 secret access keys are derived from API tokens).
- Give replacement deployment credentials only the required permissions, install
  them through the secret store, and exercise the real deployment before claiming
  recovery. Keep GitHub secret scanning and push protection enabled.

## Browser container publication

To build a candidate from the repository root without overwriting a public image tag:

```bash
export CHUDWARE_IMAGE=chudware-selfhost:local
docker compose -f web/compose.yaml up --build -d --wait
```

This builds the complete browser client, native engine, gateway and non-AI
Python workers from the whole repository context using
`web/deploy/container/Dockerfile`. It does not depend on desktop release jobs,
native signing secrets, or downloading the optional agent runtime/SDK. Browser
online Copilot remains unsupported; the image is not a desktop installer.
[The deployment guide](../web/README.md) covers storage, authentication/TLS,
container isolation requirements and upgrade/rollback.

`self-host.yml` builds on pull requests, `master`, `v*` tags and manual runs.
Before any publication it starts an isolated Compose project, exercises the
complete image, recreates the container with its volumes retained, and reopens
the saved model. A separate `packages: write` job loads that exact tested image;
PR jobs have read-only repository permission and never log in to GHCR.
No additional private checkout or private-repository token is needed.

The public destination is `ghcr.io/hunter-124/chudware-web` (Linux x86-64).
Trusted runs in the canonical repository on `master` or a `v*` tag publish
`sha-<full-commit>`. These tags are
immutable by workflow policy: an existing tag is refused rather than replaced,
including on reruns. Keep a deployed digest or commit tag for rollback.
Only pushes to `master`, or a manual run on `master` with `move_edge` explicitly
selected, advance the moving `edge` channel. Tag runs do not advance it; there
is no configured `latest` or `stable` browser channel.

After publication, verify an anonymous pull of the emitted commit reference/digest.
For a new package or fork, its owner must also set the GHCR package's visibility.
A public source checkout or successful authenticated CI push alone does not establish
public image access. Do not advertise a pull command until the anonymous check passes.
Optional owner-operated hosting is arranged through the existing contact
route as project support, not a required license or an implemented checkout.

### Operational and persistence acceptance

The consumer-visible command targets an already-running isolated deployment;
it never starts/stops a daemon or touches unrelated documents, recents or chats:

```bash
python3 -m venv .venv-smoke
.venv-smoke/bin/python -m pip install httpx==0.28.1 websockets==16.1
.venv-smoke/bin/python web/deploy/smoke.py http://127.0.0.1:8300
```

It verifies health, nonempty HTML/JS/WASM/data with MIME/isolation/cache/CSP
headers, hostile Host and HTTP/WebSocket Origin refusal, a real native 10 mm box
build, authorized AICM download, native save/download/reopen and the non-AI
`parts_list_categories` Python worker. Loading states and asynchronous events
are handled explicitly; a health response or `hello` alone cannot pass it.
It uses one fresh `chudware-smoke-<uuid>.step+` name and closes only its own
documents. By default it deletes only that exact unchanged file.

For restart acceptance, explicitly keep its file and a new local receipt:

```bash
.venv-smoke/bin/python web/deploy/smoke.py http://127.0.0.1:8300 \
  --keep --receipt /tmp/chudware-smoke.json
docker compose -f web/compose.yaml down
docker compose -f web/compose.yaml up --no-build -d --force-recreate
.venv-smoke/bin/python web/deploy/smoke.py http://127.0.0.1:8300 \
  --resume /tmp/chudware-smoke.json
```

Do **not** use `down --volumes`: this acceptance checks the retained data,
workspace and engine-state volumes. Resume verifies the saved bytes against
the receipt before reopening and removes the unchanged smoke file on success;
the local receipt can then be removed. `--resume ... --keep` retains it for
another restart. Use a new receipt path for each fresh run. A changed or
unverified file is left untouched and fails acceptance. This operational
command is not a browser-rendering, multi-user security, or complete feature
qualification suite.

## Cutting a native release

Native public release remains blocked by the unpinned `macos-arm64` runtime lock
entry. The SDK source archive and pinned Linux, Windows and macOS x64 runtime
binaries are hosted in this repository's `chudcode-runtime-v0.4.4` release; no
private companion repository is required. All platform jobs must succeed, so
browser-image publication must not be routed through this pipeline. The following
is the configured maintainer procedure, not a claim that public installers are
currently available.

1. Make sure `master` is green: `fast-gate`, `linux-gate` and `native-build`.
2. Pick the version. Tags are `v<major>.<minor>.<patch>` with an optional
   prerelease suffix (`v0.3.0-beta.1`). A suffix publishes a GitHub
   prerelease, which the updater never offers to users.
3. Tag the commit and push the tag:

   ```bash
   git tag -a v0.3.0 -m "Chudware 0.3.0"
   git push origin v0.3.0
   ```

4. Follow the `release` run under Actions. Nothing is published unless all
   three platform builds succeed; the publish job then creates the release in
   one step, with download and code-signing notes followed by the generated
   changelog.

To rebuild a tag that already exists (for example after adding a signing
secret), run the `release` workflow manually and enter the tag. The publish
job refuses to overwrite an existing release, so delete it first with
`gh release delete v0.3.0`; the tag itself stays.

## What gets published

| Job | Runner | Assets |
|---|---|---|
| `linux` | `ubuntu-24.04`, `fedora:44` container | `Chudware-<v>-linux-x86_64.AppImage`, `chudware_<v>_amd64.deb`, `chudware-<v>-1.x86_64.rpm` |
| `windows` | `windows-2025` | `Chudware-Setup-<v>-x86_64.exe` (Inno Setup), `Chudware-<v>-x86_64.msi` (WiX) |
| `macos` | `macos-15` (Apple silicon) | `Chudware-<v>-macos-arm64.zip` |
| `publish` | `ubuntu-24.04` | `update-linux-x86_64.json`, `update-windows-x86_64.json`, `update-macos-arm64.json`, `install.sh` (copied from `packaging/linux/install.sh`), `SHA256SUMS` |

Package names for prereleases follow each format's rules; `packaging/artifact_names.py`
is the single source of those names. Each platform job:

1. builds the core and host with `CHUDWARE_DEVELOPER_BUILD=OFF`, link-time
   optimization and `CHUDWARE_VERSION_OVERRIDE=<v>`, against the pinned CEF SDK;
2. runs the core and host test suites through `scripts/run_release_ctest.py`;
3. stages the payload (host, engine, `chudware-hostd`, the Nuitka-compiled
   `chudware-worker` and `chudware-mcp` sidecars and the pinned `chud` runtime),
   writes `payload-manifest.v1.json` and signs it with the release key;
4. seals the payload into the platform package and verifies the result: every
   library resolves inside the bundle, `chudware-hostd --status` reports the
   release version, and the signed payload manifest matches the extracted bytes.

`chudware-hostd` verifies the same payload manifest at startup, so a modified
installation is detected even outside the updater.

## Update manifests

The updater fetches

```text
https://github.com/Hunter-124/Chudware/releases/latest/download/update-<platform>-<arch>.json
```

where `<platform>` is `linux`, `windows` or `macos` and `<arch>` is `x86_64` or
`arm64`. GitHub resolves `latest` to the newest published release that is not a
draft or prerelease and redirects to the asset; the updater follows the
redirect over HTTPS. `CHUDWARE_UPDATE_MANIFEST_URL` overrides the URL, which is
how a prerelease is tested before it is promoted.

Each manifest is written by `packaging/github_release_manifest.py`:

```json
{
  "version": "0.3.0",
  "platform": "linux",
  "arch": "x86_64",
  "key_id": "release-ci-v1",
  "sha256": "<64 lowercase hex characters>",
  "size": 412345678,
  "signature": "<base64 Ed25519 signature>",
  "notes_url": "https://github.com/Hunter-124/Chudware/releases/tag/v0.3.0",
  "url": "https://github.com/Hunter-124/Chudware/releases/download/v0.3.0/Chudware-0.3.0-linux-x86_64.AppImage"
}
```

The signature covers the NUL-separated preimage

```text
chudware-artifact-v1 \0 key_id \0 version \0 platform \0 arch \0 sha256
```

The updater accepts a manifest only if `key_id` names a key compiled into
`core/security/release_keys.h`, the signature verifies, and `platform` and
`arch` match the running build. It then downloads `url`, checks `size` and
`sha256`, verifies the payload manifest inside the downloaded package, and only
then stages the update. Updates remain optional: an old or missing manifest and
offline operation do not disable the application. Published manifests contain
no minimum supported version, and the release workflow has no enforced upgrade
floor. Current clients also ignore any `min_supported` field in historical
manifests. Updates are not a license check or support deadline.

### Checking a published release

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

To exercise the real update path against a published release from a host
build tree:

```bash
cmake --build host/build-linux --target test_updater
host/build-linux/bin/test_updater --live \
  https://github.com/Hunter-124/Chudware/releases/download/v0.3.0/update-linux-x86_64.json \
  0.2.0 /tmp/live-updater
```

It downloads the artifact, runs every verification gate and stages the update
without touching the running application.

## Release key handling

Payload and update manifests are signed with the Ed25519 key `release-ci-v1`.
Its private seed lives only in the `CHUDWARE_RELEASE_SIGNING_KEY` repository
secret; its public half is compiled into `core/security/release_keys.h` and
listed in `RELEASE_PUBLIC_KEYS` in `packaging/github_release_manifest.py`. The signing tools check the seed against
that public key before they sign, so a wrong or rotated secret fails the
release instead of producing manifests no client can verify. Never commit the
seed or pass it on a command line.

Rotating the key is a client migration: add the new public key to
`core/security/release_keys.h` and `packaging/github_release_manifest.py`, ship a release
signed with the old key, and switch the secret and `CHUDWARE_RELEASE_KEY_ID` in
`release.yml` only after that release has been out long enough for users to
update. [SIGNING.md](SIGNING.md) covers generating a key.

## Versioning

`core/cmake/ChudwareVersion.cmake` derives the version from
`git describe --tags`. Release builds override it:

```bash
cmake --preset linux-core -DCHUDWARE_VERSION_OVERRIDE=0.3.0
# -- Chudware version 0.3.0 (<commit>, <date>, dirty=0)
```

The value must match `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$` or configure
fails. The full string, prerelease included, is used for artifact names and
manifests; CMake's `project(VERSION ...)` receives only the numeric triple.

## Native agent runtime prerequisites

Native bundles include the agent runtime `chud`; this is an internal packaging
dependency, not a separate product or a prerequisite for browser self-hosting.
The pinned `chudcode-rpc` SDK source archive and supported native binaries are
published in this repository's runtime release, requiring no companion repository access.
`vendor/chudcode/CHUDCODE_ARTIFACT.lock` records each target
(`linux-x86_64`, `macos-arm64`, `macos-x86_64`, `windows-x86_64`) by URL and
SHA-256, and release jobs fetch pinned targets with:

```bash
python3 scripts/pin_chudcode.py ensure --target linux-x86_64 --cache-dir /tmp/chudcode
```

The download is verified against the lock before packaging; a target whose
lock row is unpinned fails the release. The `macos-arm64` row is currently
unpinned, which blocks the all-platform native publisher independently of
signing credentials. Publish reviewed source and binary assets in this repository's
runtime release before repinning every supported target:

```bash
base=https://github.com/Hunter-124/Chudware/releases/download/chudcode-runtime-vX.Y.Z
python3 scripts/pin_chudcode.py pin \
  --version X.Y.Z --rpc-protocol 2 --repository-commit <commit> \
  --artifact linux-x86_64=chudcode-linux-x64 --url linux-x86_64=$base/chudcode-linux-x64 \
  --artifact macos-arm64=chudcode-darwin-arm64 --url macos-arm64=$base/chudcode-darwin-arm64 \
  --artifact macos-x86_64=chudcode-darwin-x64 --url macos-x86_64=$base/chudcode-darwin-x64 \
  --artifact windows-x86_64=chudcode-windows-x64.exe --url windows-x86_64=$base/chudcode-windows-x64.exe
python3 scripts/pin_chudcode.py check-lock
```

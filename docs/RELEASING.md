# Releasing Chudware

Two pipelines, one product. Know which one you are in.

| | `nightly-release.yml` | `release.yml` |
|---|---|---|
| Trigger | 09:17 UTC nightly + manual dispatch | `v*` tag push + manual dispatch |
| Channel | **beta only** (the gateway rejects anything else) | beta or stable |
| Signing key | `release-ci-v1`, in CI, from a GitHub secret | `release-v1`, offline, age-encrypted, owner workstation |
| Human work | none, then one click to promote | full offline ceremony |

The nightly pipeline is the normal path. `release.yml` exists for a release
signed by the owner's offline key; nothing in this document replaces it, and no
change here gives CI the ability to publish a stable release: `POST
/api/releases/ci/publish` is beta-only, and only `POST
/api/admin/releases/promote` (an admin session in `/admin`) creates a stable row.

## The automatic flow

Every night, and on demand from the Actions tab:

Nightly runs share one concurrency group with cancellation disabled. A new scheduled
or manual run queues rather than cancelling the platform legs of an in-flight release.


1. **`plan`** (gateway host, `cc-linux-livingroom`; metadata and API calls
   only, it compiles nothing) reads `GET https://app.chudware.com/api/releases/base-version`
   for the stable base (highest stable patch + 1, floor `0.1.0`) and builds the
   nightly version `<base>-beta.<run number>` — e.g. `0.1.0-beta.42`. On a
   *scheduled* run it asks the gateway over loopback whether `ci_builds` already
   holds a `published` row for `HEAD`; if so the night is skipped. A manual
   dispatch always builds. Every Chudcode-backed build leg is recorded as
   `queued`: `linux/x86_64`, `windows/x86_64` and `macos/x86_64`.
2. **`linux-beta`** (`cc-linux-build`, the private NVMe build guest; never the
   serving host) configures core and host with
   `-DCHUDWARE_VERSION_OVERRIDE=<version> -DCHUDWARE_DEVELOPER_BUILD=OFF`, builds,
   runs `ctest` for core and host, takes a headless `CHUDWARE_SHOT=m1` screenshot
   under Xvfb, stages the immutable AppDir via
   `packaging/linux/build_appimage.sh --prepare-signing`, signs the payload
   manifest with `packaging/ci_sign.py`, and lets `packaging/finalize_release.py`
   embed the signature, build the AppImage and generate the SBOM, SLSA provenance
   and platform-test evidence. The finished AppImage is then extracted and
   verified — zero `not found` in `ldd`, `chudware-hostd --status` loads and
   reports this exact version, and the signed payload manifest matches the
   extracted bytes — before anything is offered to a customer. It then packages
   that same verified tree as `chudware_<version>_amd64.deb` and
   `chudware-<version>-<release>.x86_64.rpm` via
   `packaging/linux/build_linux_packages.sh`, signs each package's own digest
   under both the beta and the stable version, and publishes them with
   `format` `deb` / `rpm`. Those two formats are best-effort: a packaging
   failure is a warning naming the missing formats, and never costs the nightly
   its AppImage. They need `rpm-build` and `xz` on `cc-linux-build`.
3. **`windows-beta`** (`cc-win-package`) does the same for Windows: junctions,
   protected assets, `windows-occt` preset plus the host GUI at the pinned
   version, `ctest`, compiled sidecars, and the **Inno Setup** installer
   `Chudware-Setup-<version>-x86_64.exe`. `ISCC.exe` is a hard requirement: if
   Inno Setup is missing the job fails at the capability check. It never
   substitutes a zip.
4. **`macos-beta`** (`cc-mac-intel`, x86_64) configures the `macos-x86_64`
   core preset and `host/build-macos-x86_64` at the pinned version, runs
   `ctest` for core and host, then packages with
   `packaging/macos/build_app.sh`: `--prepare-signing` retains the immutable
   `Chudware.app` and emits the payload preimage, `packaging/ci_sign.py` signs
   it, and `--finalize-signing` seals that exact bundle and archives it with
   `ditto` as `Chudware-<version>-macos-x86_64.zip`. The finished zip is then
   expanded and verified before anything is offered to a customer:
   `Chudware.app` is present with its `payload-manifest.v1.json` + `.sig`
   verifying against `release-ci-v1`, `otool -L` shows no shipped Mach-O file
   loading an absolute path outside `/System` or `/usr/lib`, and
   `chudware-hostd --status` loads and reports this exact version. That is
   the same shape `host/src/update/apply_macos.cpp` requires of an update: it
   expands the zip with `ditto -x -k`, refuses an archive whose root is not
   `Chudware.app`, and re-verifies the in-bundle payload manifest before
   swapping the app.

   The Intel Mac has Command Line Tools only, so no step here uses
   `xcodebuild`, and the nightly beta is signed but unnotarized: it makes no
   Gatekeeper claim. `macbook-air-m2` can still run the CAD core-only
   `macos-arm64` preset, but no Mac ARM Chudcode builder or release artifact
   exists; it cannot package a Chudcode-backed release.
5. **Publish.** Every build job gets its bytes into `<releases_dir>/incoming/`
   on the gateway host through the authenticated HTTP API and then calls
   `POST /api/releases/ci/publish`. No builder reads `gateway.env`, holds a
   production SSH credential or writes the gateway's release directory; only
   `plan`, which runs on the gateway host and builds nothing, reads the token
   from `gateway.env` and talks to **`http://127.0.0.1:8300`**.
   * **`linux-beta`, `windows-beta` and `macos-beta`** hold the same token as
     the `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret and reach the gateway over TLS
     on `https://app.chudware.com`: the artifact and each payload-manifest,
     signature and evidence sidecar is streamed in under its canonical name with
     `PUT /api/releases/ci/upload/<name>` (curl `--upload-file`, so a 300 MB
     installer is never buffered in memory), and the job compares the sha256
     the endpoint reports against the local digest before moving on. A missing
     local file or a failed upload fails that publish. Then the ordinary
     `ci/publish` and `ci/status` calls follow over the same origin, with the
     body's digests computed from the same local `RUNNER_TEMP` files that were
     uploaded. Those runners need nothing but outbound 443. The Linux and macOS
     legs share `packaging/ci_gateway.sh`; Windows uses
     `packaging/windows/publish_release.ps1`.

     Anything larger than 64 MiB goes up in 64 MiB chunks
     (`?offset=<n>`, and `&complete=1` on the last one), because the proxy in
     front of the public origin answers **413** for bodies over 100 MB and the
     Windows installer is ~230 MB. The offset must equal the bytes already
     staged, so a retried or interleaved chunk is rejected instead of corrupting
     an artifact, and only the finalizing call reports a digest.

   On every host the token reaches curl through a `0600` `--config` file, never
   on a command line. The gateway recomputes the digest and size, verifies both
   signatures, and moves everything into `<releases_dir>/store/beta/<version>/`.
6. **Status.** Every transition (`queued`, `building`, `testing`, `packaging`,
   `publishing`, `published`, `failed`) is posted to `POST
   /api/releases/ci/status` and shows up in `/admin` under Dashboard and
   Releases -> CI builds. A failed job reports `failed` from an `if: always()`
   step, so a red night is visible in the admin UI, not only in Actions.

### Two signatures per build

Each build signs the same artifact bytes twice: once as `<base>-beta.<run>` and
once as `<base>`. The beta signature publishes the beta row; the stable signature
rides along on that row (`releases.stable_version` / `stable_signature`) so
promotion needs no rebuild and no new signature. Both use key id
`release-ci-v1`, public key
`d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6`.

### Evidence

SBOM, provenance and platform-test documents are generated by
`packaging/generate_release_evidence.py`, signed with the same key, and their
digests are sent in the publish body. The platform-test document must list the
suites that really ran (`ctest core`, `ctest host`); the workflow asserts that.

If evidence generation fails, the job emits a `::warning::`, publishes the beta
**without** an evidence bundle, and says so in the log. Such a build can be
installed and tested, but it **cannot be promoted to stable** — promotion
requires a complete evidence bundle. The next successful night supersedes it.

## Promoting beta to stable in /admin

1. Open `https://app.chudware.com/admin` and go to **Releases**.
2. Find the beta row for the version you want. Rows carry two badges: a stable
   pair badge (the build signed a stable version too) and an evidence badge.
   **Promote to stable** only appears when both are present.
3. Click **Promote to stable**. The gateway re-verifies the stable signature
   against the row's key id, version, platform, arch and sha256, copies the
   artifact and every sidecar to `store/stable/<stable version>/`, and inserts a
   stable row with `version = stable_version`. The bytes customers download are
   the exact bytes that were tested — no rebuild.
4. Promote each platform you intend to ship. `GET /api/releases/latest` starts
   serving the new stable per platform immediately; the in-app updater picks it
   up on its next poll.
5. A second promote of the same version returns 409. To withdraw, delete the
   stable row from the same table (installed clients stop being offered it).

Promotion never moves the supported-version floor. Nightly publishes send
`min_supported = 0.1.0` on purpose: the gateway refuses to move the floor
backward, so a nightly that raised it would strand installed clients. Raising the
floor is a deliberate act through `packaging/publish.py register --min-supported`.

## Verifying a published release end to end

`GET /api/releases/latest` names the target identity and, for an entitled
caller, a download URL. When no CDN credential is configured the URL is this
gateway's own `/api/releases/artifact/<key>` route, and **that route is
entitlement-gated**: the updater sends its entitlement with the artifact GET
too (same origin only), so an unentitled client sees metadata and no URL.

To prove a published build actually installs, run the production check-and-stage
path against the live endpoint from a host build:

```sh
ninja -C host/build-linux test_updater
CHUDWARE_UPDATE_CHANNEL=beta \
CHUDWARE_UPDATE_ENTITLEMENT="<compact entitlement token>" \
  host/build-linux/bin/test_updater --live \
  https://app.chudware.com/api/releases/latest 0.1.0-beta.0 /tmp/live-updater
```

It fetches the manifest, downloads the artifact, and verifies size, SHA-256,
the Ed25519 release signature and the *inner* payload manifest of the extracted
tree before the atomic stage rename; it prints every status transition and
asserts the running app was untouched. A token comes from the same flow the app
uses — `POST /desktop/authorize` with an admin session cookie and a
base64url Ed25519 device key returns a handoff URL carrying `token=`.

Note that while `min_supported` is `0.1.0` every `0.1.0-beta.N` client is
*below* the floor, so the status reads **Mandatory** the whole way through:
`setStatus` pins that state and only the message tracks progress.

The install half — replacing the running AppImage with the staged one — ends in
`execv`, which would launch the GUI. Run it with the install directory on a
noexec mount so the relaunch is blocked *after* the replacement lands:

```sh
unshare -Ur -m sh -c 'mkdir -p /tmp/nx && mount -t tmpfs -o noexec,size=600M tmpfs /tmp/nx &&
  host/build-linux/bin/test_updater --apply-once /tmp/live-updater /tmp/nx/Chudware.AppImage'
```

`result=Failed` with `relaunch Failed` is the expected outcome there: it means
the staged artifact was re-verified, installed byte for byte over the old file
and left executable, and only the (deliberately blocked) relaunch failed. On a
normal mount the same call returns `Relaunching` and the new build takes over.

## Required GitHub secrets

| Secret | Used by | Content |
|---|---|---|
| `CHUDWARE_RELEASE_CI_KEY` | all three build jobs | base64 32-byte Ed25519 **seed** for `release-ci-v1` (single line, no PEM). Offline copy: `~/.chudware/release-ci-v1.seed.b64` on the owner workstation. |
| `CHUDWARE_GATEWAY_RELEASE_TOKEN` | `linux-beta`, `windows-beta`, `macos-beta` | the gateway's release publishing token, the same value as `CHUDWARE_GATEWAY_RELEASE_TOKEN` in `gateway.env`. Every build leg authenticates to `https://app.chudware.com` with this Bearer token over TLS; no builder reads `gateway.env`. Only `plan` does **not** use it: it runs on the gateway host, builds nothing, and reads the token from `gateway.env` at the point of use. |

There are no SSH secrets any more. `CHUDWARE_LIVINGROOM_SSH_HOST`,
`CHUDWARE_LIVINGROOM_SSH_KEY` and `CHUDWARE_LIVINGROOM_HOST_KEY` were removed
when the transport moved from `scp` + `ssh` to the gateway's upload endpoint:
`192.168.1.x` and `192.168.0.x` do not route to each other, so the SSH path
never worked from those builders. The off-LAN runners now need **outbound 443
only** - no LAN reachability, no host key, no shell on the gateway box.

Nothing else is secret: the CI public key, the public base URL, the loopback URL
and the gateway env path are in the workflow in the clear on purpose, so a
rotated key fails loudly at `ci_sign.py --expect-public-key` instead of silently
signing with the wrong key.
On every host the token is handed to curl through a `0600` `--config` file, never
on a command line: a shared runner's process list is readable, and GitHub's log
masking does not help there.

Rotating `release-ci-v1` means: generate a new seed, add the new public key to
all three keyrings (`core/security/release_keys.h`,
`Webservice/gateway/releases.py`, `packaging/publish.py`), ship a host build that
trusts it, then replace the secret. Keep the old key in the keyrings until no
installed client still verifies against it.

## Runner prerequisites

**`cc-linux-build`, the private NVMe build guest, never the gateway host** (it
publishes over TLS with the `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret and needs no
production file):

* `cmake`, `ninja`, `ctest`, `python3`, `curl`, `tar`, `file`, `ldd`,
  `sha256sum`, `strip`, `rsync`
* `xvfb-run` for the headless screenshot
* the pinned Linux x86_64 `chud` artifact in the CAD runtime release mirror,
  or an explicit verified `CHUDWARE_CHUD_BIN` (see
  `docs/reference/CI_RUNNERS.md`, "Pinned Chudcode artifact")
* Nuitka and the rest of `packaging/requirements-release.txt` in the release
  Python (the AppImage compiles the Python sidecars)
* outbound HTTPS to `https://app.chudware.com`

**`cc-win-package`:**

* Visual Studio with the x64 C++ toolset (`vcvars64.bat` or the DevShell module)
* `cmake`, `ninja`, `ctest`, `git`, `python`, `curl` on `PATH`
  (`%USERPROFILE%\chudware-ci-tools\{bin,python}` is added automatically, as in
  `native-build.yml`)
* **Inno Setup 6** — `ISCC.exe` in `%USERPROFILE%\chudware-ci-tools\innosetup`
  (portable unpack; the runner service account cannot run Inno's installer),
  `%ProgramFiles(x86)%\Inno Setup 6`, on `PATH`, or named by the `ISCC`
  environment variable. `runner-provision.yml` (Actions -> run) installs Inno
  into exactly these locations.
* Nuitka and `packaging/requirements-release.txt` for the compiled sidecars.
  Nuitka's onefile mode on Windows needs Dependency Walker and asks for it
  interactively, which a CI shell answers "no"; `build_sidecars.ps1` passes
  `--assume-yes-for-downloads`, so the first release build fetches it into
  `%LOCALAPPDATA%\Nuitka` and later builds reuse the cache. That runner
  therefore needs outbound HTTPS beyond app.chudware.com on its first build.
* a pinned Windows x86_64 `chud.exe` in the CAD runtime release mirror, or an
  explicit verified `CHUDWARE_CHUD_BIN`. The nightly job fetches and verifies
  its digest-keyed cache with the CAD job token; no runner service environment
  change is required. Build missing Windows bytes with
  `chudcode-runtime-windows-build.yml`, then pin their real SHA-256.
* `CHUDWARE_SIGN_IDENTITY` if you want Authenticode on the installer; without it
  the installer is unsigned and SmartScreen will warn (see `docs/SIGNING.md`)
* outbound HTTPS to `app.chudware.com`. This runner cannot reach the gateway's
  LAN address and does not need to: it uploads and publishes over TLS, and the
  job's first channel step fails loudly with the unreachable origin if 443 is
  blocked.

**`cc-mac-intel` (imac-intel, x86_64):**

* Xcode **Command Line Tools** only (`xcode-select -p`, `clang`, `xcrun`). No
  step uses `xcodebuild`, and the capability check asserts the compiler rather
  than the IDE.
* `cmake`, `ninja`, `ctest`, `python3`, `git`, `curl`, `tar`, `zip`, `rsync`,
  plus the Command Line Tools' `ditto`, `otool`, `install_name_tool`, `plutil`
  and `codesign`
* Homebrew, with `opencascade` installed: off Windows `ProvisionOCCT.cmake`
  downloads nothing and the `macos-x86_64` preset `find_package`s OCCT
  through `/usr/local`.
* a release Python 3.11+ carrying `packaging/requirements-release.txt` (Nuitka
  for the compiled `chudware-worker`/`chudware-mcp` sidecars, `cryptography` for
  every signature). macOS ships 3.9.6, so the job picks its interpreter by
  capability: `~/chudware-ci-tools/release-python/bin/python3` first, then
  `python3.14`..`python3` from `PATH`, and the first one that imports both
  `cryptography` and `nuitka` wins.
* the pinned macOS x86_64 `chud` artifact in the CAD runtime release mirror,
  or an explicit verified `CHUDWARE_CHUD_BIN`. The nightly capability step
  downloads and verifies it before packaging, without a private-runtime PAT
  or manual Mac installation. Packaging itself stays offline; see
  `docs/reference/CI_RUNNERS.md`, "Pinned Chudcode artifact".
* outbound HTTPS to `app.chudware.com`, for the same reason as the Windows
  builder: the Intel Mac cannot route to `192.168.0.183`.

`runner-provision.yml` can prepare the Intel builder: dispatch it with
`runner_label` set to `cc-mac-intel` and it installs the brew formulae
(`cmake ninja python@3.13 opencascade`) and the release-Python venv at
`~/chudware-ci-tools/release-python`, then prints a capability report that exits
non-zero on any `MISSING`. Homebrew itself is the one thing it will not install:
that installer needs an interactive sudo, so it fails with an explicit
instruction instead. The ARM Mac's CAD core-only environment may also be
provisioned, but it is not a Chudcode-backed release builder. macOS steps
are bash 3.2 (Apple's `/bin/bash`): no
associative arrays, no `mapfile`, no `${var^^}`, and string accumulators instead
of arrays, because expanding an empty array under `set -u` is an
unbound-variable error before bash 4.4.

Windows steps are Windows PowerShell 5.1 with ASCII-only bodies. `bash` on those
hosts is an empty WSL shim that exits 0 without running anything, and PowerShell
5.1 misreads non-ASCII in the runner's BOM-less script; `scripts/verify-ci-workflows.py`
enforces both rules.

`packaging/requirements-release.txt` holds two kinds of pin: the build tools
(Nuitka, cryptography, PyYAML, patchelf) and the runtime packages the compiled
sidecars must bundle. Nuitka can bundle only what the release Python imports, so
each bundled package is also force-included (`--include-package`) by the three
packaging scripts under the same platform condition as its marker, and a release
Python without it fails the build instead of shipping without the feature. Today
that is `pycolmap` (plus its dependency `numpy`) for Gaussian/sparse reverse
engineering on Linux x86_64 and Windows x86_64 release legs. The dependency
also supports macOS arm64 core-only builds, but macOS ARM is not a
Chudcode-backed release leg. `pycolmap` 3.13.0 publishes no Intel macOS
wheel, so the x86_64 `.app` ships without it and `reveng_status` reports
sparse SfM unavailable there. Each active `nightly-release` leg `pip install`s
the file into its release Python before building, so a new pin needs no
re-provision; `runner-provision` syncs it into an existing venv too.

## Chudcode artifact pin

`vendor/chudcode/CHUDCODE_ARTIFACT.lock` is the release authority for the
compiled RPC service (`chud`) that runs every online agent turn. A lock with
`null` artifact URL/hash fields declares unpublished required inputs and
deliberately makes package/deploy paths fail closed. The supported targets are
exactly Linux x86_64, macOS x86_64 and Windows x86_64. A future repin pins all
three atomically from one published commit:

```bash
python3 scripts/pin_chudcode.py pin \
  --version X.Y.Z --rpc-protocol 2 --repository-commit COMMIT \
  --artifact linux-x86_64=/secure/chud-linux-x86_64 --url linux-x86_64=https://... \
  --artifact macos-x86_64=/secure/chud-macos-x86_64 --url macos-x86_64=https://... \
  --artifact windows-x86_64=/secure/chud-windows-x86_64.exe --url windows-x86_64=https://...
```

The lock's `version` is the runtime release. `engine_version` is optional
source provenance only; it never changes executable identity. Every artifact
must report exactly `Chudcode vX.Y.Z` from `--version` (the executable's own
identity string); engine provenance is never accepted as or appended to it.

### Current pin status (2026-10-11)

The lock pins `chudcode-v0.4.3` (the thin fork on upstream oh-my-pi 18.4.4,
`engine_version` 18.4.4). Linux x86_64 and macOS x86_64 are the upstream
release assets. Windows x86_64 (`6627829b…`) is the native build from
`chudcode-runtime-windows-build.yml`, published to the CAD mirror, because the
Linux cross-compiled Windows CLI crashes inside Bun 1.4.0 at startup; Chudcode
releases no longer ship it. Between a version bump and the native Windows
build, the Windows row is `null` and Windows installer legs fail closed. Locks
written before the x64-only matrix also list
`linux-aarch64`/`macos-arm64`; `load_lock` still accepts those so rollback can
verify retained releases, but `pin` and `check-lock` require the exact
supported set. ARM targets (Linux ARM, macOS ARM) are not release targets. A
macOS ARM build happens only by hand, when the hardware is available; CI never
blocks a release on it.

The runtime repository is private. Nightly uses the CAD repository's
`chudcode-runtime-v<lock version>` release mirror and its own ephemeral
`github.token` (`contents: read`), not a broad OAuth token or a new PAT secret.
`pin_chudcode.py ensure` verifies an explicit input or a digest-keyed cache,
and authenticates a cold download through the GitHub API without forwarding
Authorization to its signed release CDN. The locked digest remains
authoritative. The global `check-lock` and `pin` require all three targets.

The `stage` command used by packaging is offline and accepts only a local
`CHUDWARE_CHUD_BIN`; it verifies the executable format/architecture, SHA-256,
and `--version` product identity before copying it as canonical `chud` or
`chud.exe`. Network retrieval is available only through explicit `fetch` or
`ensure` invocations before packaging.

### Artifact-lock operating rules

The lock is the complete identity of the embedded online runtime, not a source-vendoring
hint. It records schema `chudcode-artifact-lock-v1`, the Chudcode product version, repository
commit, RPC protocol 2, and exactly three immutable release-platform rows. Each row pairs
the canonical filename with an unauthenticated HTTPS URL and SHA-256.

`pin` validates every executable format, executes the host-target binary for product-version
identity, writes canonical JSON to a temporary file, and replaces the lock only after those
checks pass. All three `--artifact` and `--url` pairs are mandatory, so a partial platform
update is intentionally impossible. Useful focused checks are:

```bash
python3 scripts/pin_chudcode.py check-lock
python3 scripts/pin_chudcode.py verify --target linux-x86_64 --input /secure/chud-linux-x86_64
python3 scripts/pin_chudcode.py stage --target linux-x86_64 \
  --input /secure/chud-linux-x86_64 --output /tmp/chud
```

`stage` re-verifies before and after copying and atomically publishes only canonical
`chud`/`chud.exe`. It never downloads. Only explicit `fetch` or `ensure` steps
perform network I/O. Anonymous redirects remain refused; authenticated
GitHub release downloads allow only GitHub's HTTPS release CDN and never
forward credentials there. Mirror bytes must match the committed lock.

The packaged services worker resolves the signed-payload-adjacent canonical binary. Developer
runs may override it with `CHUDWARE_CHUDCODE_BIN`; release jobs use `CHUDWARE_CHUD_BIN` only
as the local staging input. Do not add a package-manager install, source checkout, or second
provider runtime to a release image.

The release also carries CAD's checked-in `assets/ui-shared-manifest.json` from the same
Chudcode release line. Its bytes must match Chudcode's file and its schema must be
`chud-ui-shared-v1`; packaging must copy the checked-in bytes rather than regenerate them.
This manifest is a shared palette/tone/status/usage/font/icon vocabulary, not executable
widget code or runtime state.

## Versioning

`core/cmake/ChudwareVersion.cmake` derives the product version from
`git describe --tags`. The release pipeline overrides it:

```sh
cmake -S . -B build/x -DCHUDWARE_VERSION_OVERRIDE=0.1.0-beta.42
# -- Chudware version 0.1.0-beta.42 (f62b2da7, 2026-08-19, dirty=0)
```

The value must match `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$` or the
configure fails. `CHUDWARE_VERSION` carries the full string (prerelease
included), which is what every artifact name, payload manifest and release
manifest uses; `project(VERSION ...)` gets the numeric triple only, because CMake
rejects a prerelease suffix there. An override is never marked dirty — the
pipeline pinned the commit it built.

## Manual dispatch and debugging

* **Run one now:** Actions -> `nightly-release` -> Run workflow. Dispatch skips
  the "already published" check, so it always builds.
* **Watch progress without Actions:** `/admin` -> Releases -> CI builds, or
  `GET /api/admin/ci/builds?limit=20`.
* **A macOS release row stuck on `queued`:** check `imac-intel` /
  `cc-mac-intel` availability and re-dispatch when the runner is online.
  The ARM Mac may run core-only CI but has no Chudcode release artifact,
  so waiting on `cc-mac-arm` cannot publish a Chudcode-backed release.
* **A rejected publish:** the gateway recomputes the digest and size, and a
  mismatch (422) **deletes the staged artifact** from `incoming/` while leaving
  the sidecars. Re-run the job: it rebuilds and re-uploads everything before
  publishing. Every other 422 leaves `incoming/` untouched.
* **No Promote button on a row:** its evidence bundle is incomplete. `POST
  /api/admin/releases/promote` answers 422 and names every missing column, and a
  publish that omitted evidence also stores no `commit_sha` (the bundle is
  all-or-none). The row still installs and serves; the next successful nightly
  supersedes it.
* **Signing a request by hand** (same code path CI uses):

  ```sh
  CHUDWARE_RELEASE_CI_KEY="$(cat ~/.chudware/release-ci-v1.seed.b64)" \
    python3 packaging/ci_sign.py \
      --expect-public-key d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6 \
      artifact --key-id release-ci-v1 --version 0.1.0-beta.42 \
      --platform linux --arch x86_64 --artifact dist/Chudware-0.1.0-beta.42-linux-x86_64.AppImage
  ```

* **A failed upload:** `PUT /api/releases/ci/upload/<name>` streams to
  `incoming/<name>.part` and renames only on success, so a dropped connection
  leaves no half-file for `ci/publish` to trip over. Every call carries
  `--retry 5 --retry-delay 15 --retry-max-time 600`, and the job compares the
  sha256 the endpoint returns against the local digest, so transport corruption
  is named at the file that caused it instead of as a 422 an hour later. A 413
  means the artifact passed the endpoint's 2 GiB ceiling.
* **Transport rules.** Only `plan` runs on the gateway host: it must use
  `http://127.0.0.1:8300` and read the token from `gateway.env`, never the
  public URL, and it must never compile. `linux-beta`, `windows-beta` and
  `macos-beta` are builders and must use `https://app.chudware.com` with the
  `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret; no builder gets `gateway.env`, a NAS
  release path or an SSH path, and adding one back would put production
  secrets on a build guest. On both transports the token goes to curl in a
  `0600` `--config` file, never in argv.

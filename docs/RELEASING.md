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

1. **`plan`** (gateway host) reads `GET https://app.chudware.com/api/releases/base-version`
   for the stable base (highest stable patch + 1, floor `0.1.0`) and builds the
   nightly version `<base>-beta.<run number>` — e.g. `0.1.0-beta.42`. On a
   *scheduled* run it asks the gateway over loopback whether `ci_builds` already
   holds a `published` row for `HEAD`; if so the night is skipped. A manual
   dispatch always builds. Every build leg is recorded as `queued`:
   `linux/x86_64`, `windows/x86_64`, `macos/arm64` and `macos/x86_64`.
2. **`linux-beta`** (gateway host, `cc-linux-build`) configures core and host with
   `-DCHUDWARE_VERSION_OVERRIDE=<version> -DCHUDWARE_DEVELOPER_BUILD=OFF`, builds,
   runs `ctest` for core and host, takes a headless `CHUDWARE_SHOT=m1` screenshot
   under Xvfb, stages the immutable AppDir via
   `packaging/linux/build_appimage.sh --prepare-signing`, signs the payload
   manifest with `packaging/ci_sign.py`, and lets `packaging/finalize_release.py`
   embed the signature, build the AppImage and generate the SBOM, SLSA provenance
   and platform-test evidence. The finished AppImage is then extracted and
   verified — zero `not found` in `ldd`, `chudware-hostd --status` loads and
   reports this exact version, and the signed payload manifest matches the
   extracted bytes — before anything is offered to a customer.
3. **`windows-beta`** (`cc-win-package`) does the same for Windows: junctions,
   protected assets, `windows-occt` preset plus the host GUI at the pinned
   version, `ctest`, compiled sidecars, and the **Inno Setup** installer
   `Chudware-Setup-<version>-x86_64.exe`. `ISCC.exe` is a hard requirement: if
   Inno Setup is missing the job fails at the capability check. It never
   substitutes a zip.
4. **`macos-beta`** (`cc-mac-intel` and `cc-mac-arm`, one matrix row each,
   `fail-fast: false`) configures the `macos-x86_64` / `macos-arm64` core preset
   and `host/build-macos-<arch>` at the pinned version, runs `ctest` for core and
   host, then packages with `packaging/macos/build_app.sh`: `--prepare-signing`
   retains the immutable `Chudware.app` and emits the payload preimage,
   `packaging/ci_sign.py` signs it, and `--finalize-signing` seals that exact
   bundle and archives it with `ditto` as
   `Chudware-<version>-macos-<arch>.zip`. The finished zip is then expanded and
   verified before anything is offered to a customer: `Chudware.app` is present
   with its `payload-manifest.v1.json` + `.sig` verifying against
   `release-ci-v1`, `otool -L` shows no shipped Mach-O file loading an absolute
   path outside `/System` or `/usr/lib`, and `chudware-hostd --status` loads and
   reports this exact version. That is the same shape
   `host/src/update/apply_macos.cpp` requires of an update: it expands the zip
   with `ditto -x -k`, refuses an archive whose root is not `Chudware.app`, and
   re-verifies the in-bundle payload manifest before swapping the app.

   Both Macs are Command Line Tools only, so no step here uses `xcodebuild`, and
   both Macs sign unnotarized: a nightly beta makes no Gatekeeper claim.
   `macbook-air-m2` (`cc-mac-arm`) is a laptop and is often asleep. Its row then
   sits **queued** until the runner returns, and GitHub fails it after 24 hours
   queued. That is accepted: the rows are independent, so `imac-intel` publishes
   regardless, and a row that never starts runs no steps, so it posts no
   `failed` transition — it stays `queued` in `/admin` until the next night's
   run supersedes it.
5. **Publish.** Each job stages the artifact and its sidecars in
   `<releases_dir>/incoming/` and calls `POST /api/releases/ci/publish` on
   **`http://127.0.0.1:8300`**, on the gateway host. The Windows and macOS
   builders get there over `scp` + `ssh`, through the same
   `remote_gateway.sh` helper each job writes to the box; the release token
   never leaves the gateway box and is never passed on a command line.
   The gateway recomputes the digest and size,
   verifies both signatures, and moves everything into
   `<releases_dir>/store/beta/<version>/`.
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

## Required GitHub secrets

| Secret | Used by | Content |
|---|---|---|
| `CHUDWARE_RELEASE_CI_KEY` | all three build jobs | base64 32-byte Ed25519 **seed** for `release-ci-v1` (single line, no PEM). Offline copy: `~/.chudware/release-ci-v1.seed.b64` on the owner workstation. |
| `CHUDWARE_LIVINGROOM_SSH_HOST` | `windows-beta`, `macos-beta` | gateway host address, e.g. `192.168.0.183` |
| `CHUDWARE_LIVINGROOM_SSH_KEY` | `windows-beta`, `macos-beta` | OpenSSH **private** key authorized for `liveuser` on the gateway host |
| `CHUDWARE_LIVINGROOM_HOST_KEY` | `windows-beta`, `macos-beta` | the gateway host's `known_hosts` line (ed25519). Host-key checking is strict; without this the upload fails closed. |

Nothing else is secret: the CI public key, loopback URL and gateway env path are
in the workflow in the clear on purpose, so a rotated key fails loudly at
`ci_sign.py --expect-public-key` instead of silently signing with the wrong key.

Rotating `release-ci-v1` means: generate a new seed, add the new public key to
all three keyrings (`core/security/release_keys.h`,
`Webservice/gateway/releases.py`, `packaging/publish.py`), ship a host build that
trusts it, then replace the secret. Keep the old key in the keyrings until no
installed client still verifies against it.

## Runner prerequisites

**`cc-linux-build`, and it must be the gateway host** (it reads
`/home/liveuser/chudware-web-data/gateway.env` and posts to loopback; the job
fails at its capability check with an explicit message otherwise):

* `cmake`, `ninja`, `ctest`, `python3`, `curl`, `tar`, `file`, `ldd`,
  `sha256sum`, `strip`, `rsync`
* `xvfb-run` for the headless screenshot
* `bun` for the compiled OMP bridge sidecar
* Nuitka and the rest of `packaging/requirements-release.txt` in the release
  Python (the AppImage compiles the Python sidecars)
* write access to `<releases_dir>/incoming`

**`cc-win-package`:**

* Visual Studio with the x64 C++ toolset (`vcvars64.bat` or the DevShell module)
* `cmake`, `ninja`, `ctest`, `git`, `python`, `bun`, `ssh`, `scp` on `PATH`
  (`%USERPROFILE%\chudware-ci-tools\{bin,python}` is added automatically, as in
  `native-build.yml`)
* **Inno Setup 6** — `ISCC.exe` in `%USERPROFILE%\chudware-ci-tools\innosetup`
  (portable unpack; the runner service account cannot run Inno's installer),
  `%ProgramFiles(x86)%\Inno Setup 6`, on `PATH`, or named by the `ISCC`
  environment variable. `runner-provision.yml` (Actions -> run) installs bun
  and Inno into exactly these locations.
* Nuitka and `packaging/requirements-release.txt` for the compiled sidecars
* `CHUDWARE_SIGN_IDENTITY` if you want Authenticode on the installer; without it
  the installer is unsigned and SmartScreen will warn (see `docs/SIGNING.md`)

**`cc-mac-intel` (imac-intel, x86_64) and `cc-mac-arm` (macbook-air-m2, arm64):**

* Xcode **Command Line Tools** only (`xcode-select -p`, `clang`, `xcrun`). No
  step uses `xcodebuild`, and the capability check asserts the compiler rather
  than the IDE.
* `cmake`, `ninja`, `ctest`, `python3`, `git`, `curl`, `tar`, `zip`, `rsync`,
  `ssh`, `scp`, `bun`, plus the Command Line Tools' `ditto`, `otool`,
  `install_name_tool`, `plutil` and `codesign`
* Homebrew, with `opencascade` installed: off Windows `ProvisionOCCT.cmake`
  downloads nothing and the `macos-*` presets `find_package` OCCT through the
  brew prefix (`/opt/homebrew` on arm64, `/usr/local` on Intel)
* a release Python 3.11+ carrying `packaging/requirements-release.txt` (Nuitka
  for the compiled `chudware-worker`/`chudware-mcp` sidecars, `cryptography` for
  every signature). macOS ships 3.9.6, so the job picks its interpreter by
  capability: `~/chudware-ci-tools/release-python/bin/python3` first, then
  `python3.14`..`python3` from `PATH`, and the first one that imports both
  `cryptography` and `nuitka` wins.
* `bun` for the compiled OMP bridge sidecar. `~/.bun/bin`, `/opt/homebrew/bin`
  and `/usr/local/bin` are prepended to `PATH` by the job itself, since the
  runner service starts with a minimal `PATH`.

`runner-provision.yml` covers both Macs: dispatch it with `runner_label` set to
`cc-mac-intel` or `cc-mac-arm` and it installs bun, the brew formulae
(`cmake ninja python@3.13 opencascade`), and the release-Python venv at
`~/chudware-ci-tools/release-python`, then prints a capability report that exits
non-zero on any `MISSING`. Homebrew itself is the one thing it will not install:
that installer needs an interactive sudo, so it fails with an explicit
instruction instead. macOS steps are bash 3.2 (Apple's `/bin/bash`): no
associative arrays, no `mapfile`, no `${var^^}`, and string accumulators instead
of arrays, because expanding an empty array under `set -u` is an
unbound-variable error before bash 4.4.

Windows steps are Windows PowerShell 5.1 with ASCII-only bodies. `bash` on those
hosts is an empty WSL shim that exits 0 without running anything, and PowerShell
5.1 misreads non-ASCII in the runner's BOM-less script; `scripts/verify-ci-workflows.py`
enforces both rules.

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
* **A macOS row stuck on `queued`:** its Mac is asleep or offline (usually
  `macbook-air-m2` / `cc-mac-arm`). GitHub keeps the row queued and fails it
  after 24 hours; the job never runs a step, so it never posts `failed` and the
  `/admin` row stays `queued` for that `run_id`. Wake the Mac and re-dispatch,
  or ignore it: the next night's run publishes the arch again. The Intel row is
  unaffected (`fail-fast: false`).
* **A rejected publish:** the gateway recomputes the digest and size, and a
  mismatch (422) **deletes the staged artifact** from `incoming/` while leaving
  the sidecars. Re-run the job: it rebuilds and re-stages everything before
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

* **Never** post a publish or status call to `https://app.chudware.com` from a
  runner. The release token lives on the gateway host; loopback is the only
  publish path, and that is what makes the token non-exportable.

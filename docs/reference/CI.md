# Continuous integration

All workflows live in `.github/workflows/` and run on GitHub-hosted runners.
Heavy native Linux jobs run in a `fedora:44` container because the core needs
OCCT 7.9.x, which Fedora packages as `opencascade-devel`;
`.github/scripts/install-fedora-deps.sh` lists their system packages. The
independent self-host workflow uses the Docker-capable Ubuntu runner to build
the complete application image through its own Fedora-based Dockerfile.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `fast-gate.yml` | Every pull request, push to `master` | Runs `scripts/check.py` (source-level audits) on Linux, Windows and macOS with the audit packages pinned in `requirements.txt`. |
| `linux-gate.yml` | Every pull request, push to `master` | Runs `scripts/gate.sh` with `CHUDWARE_GATE_STRICT=1`: check.py, core and host builds, the WASM build smoke, core ctest, hostd smoke, the Python service suites and the headless screenshot smoke. Then runs the host GUI ctest suite and the `web/gateway` and `web/deploy` pytest suites. |
| `native-build.yml` | Push to `master` touching native code, manual | Configures, builds and tests through the CMake presets: `linux-core` plus the host GUI on Linux, `windows-occt` plus the host GUI on Windows, `macos-arm64` (core only) on macOS. Uploads nothing. |
| `site-build.yml` | Changes under `website/` | Builds the static site. On pushes to `master` it also deploys `website/dist` to the `chudware-site` Cloudflare Pages project; pull requests only build. |
| `release.yml` | Push of a `v*` tag, manual for an existing tag | Builds, signs and publishes a GitHub Release. See [RELEASING.md](../RELEASING.md). |
| `self-host.yml` | Every pull request, push to `master` or a `v*` tag, manual | Builds the complete Linux x86-64 browser/native-engine image from the repository root via `web/compose.yaml`, runs the operational smoke including a non-AI Python worker, recreates with retained volumes and verifies the saved file. Trusted canonical-repository runs may publish that exact tested image to GHCR. Independent of native signing/all-platform jobs and optional agent downloads. |

Strict mode makes `gate.sh` fail instead of skipping when a toolchain is
missing, so a broken CI image can never pass by testing nothing. Locally the
same script skips unavailable stages.

Native builds ask Ninja to finish independent targets after a compiler failure
(`-k 0`), so one clean-runner log reports the independent failures instead of
revealing them one at a time. The build still exits unsuccessfully and tests
are not run against incomplete output.

Linux gate, native-build and release containers use an init process to reap
orphaned subprocesses after timeout/cancellation checks. The shared
`.github/scripts/prepare-linux-ci.sh` gives them private `HOME`, `XDG_DATA_HOME`
and `TMPDIR` directories beneath root-owned `/tmp`, not the runner's shared
mounts: firmware storage validates ownership and permissions of every ancestor.
Git trusts only the exact checkout through the disposable container's system
configuration, established before version metadata is generated. That trust
survives pytest's deliberate `HOME` isolation; checkout's temporary global
configuration does not. All Fedora profiles install the OpenSSL command-line
tool required by the updater HTTPS regression fixture; its libraries alone
are insufficient. The `gate` profile also installs the RPM and Debian inspection
tools exercised by the packaging suite.

## Secrets and variables

Pull-request workflows use no secrets. Pull requests from forks run with a
read-only token and cannot reach any of the values below.

| Name | Kind | Used by | Purpose |
|---|---|---|---|
| `CHUDWARE_RELEASE_SIGNING_KEY` | Secret (required for releases) | `release.yml` | Base64 32-byte Ed25519 seed of release key `release-ci-v1`. Signs payload manifests and update manifests. |
| `CHUDWARE_WINDOWS_CERTIFICATE`, `CHUDWARE_WINDOWS_CERTIFICATE_PASSWORD` | Secrets (optional) | `release.yml` | Base64 PFX and its password for Authenticode signing of the installer and MSI. |
| `CHUDWARE_MACOS_CERTIFICATE`, `CHUDWARE_MACOS_CERTIFICATE_PASSWORD` | Secrets (optional) | `release.yml` | Base64 `.p12` with a Developer ID Application identity, and its password. |
| `CHUDWARE_NOTARY_APPLE_ID`, `CHUDWARE_NOTARY_TEAM_ID`, `CHUDWARE_NOTARY_PASSWORD` | Secrets (optional) | `release.yml` | Apple ID, team ID and app-specific password for notarization. |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | Secrets | `site-build.yml` | Cloudflare Pages deployment of chudware.com. |

[SIGNING.md](../SIGNING.md) explains how to create each signing value.

### Browser image permissions and publication

`self-host.yml` needs no additional secrets, private-runtime checkout or private
dependency token. Its build job has only `contents: read`; checkout credentials
are not persisted into the build context. Pull requests build and smoke only.
The complete image is transferred as a short-lived Actions artifact after
HTTP/assets/security/native CAD/Python-worker and recreation-persistence smoke
passes. The publication job does not rebuild or execute repository scripts.
Only that job has `packages: write`, uses the run's `GITHUB_TOKEN` for GHCR, and
logs out afterward. It runs only in `Hunter-124/Chudware` on `master` or `v*`
tags; manual runs on other branches cannot publish.

The configured package destination is `ghcr.io/hunter-124/chudware-web`, not an
assertion that a public image already exists. Publication emits immutable
`sha-<full-commit>` tags and refuses to overwrite an existing commit tag, even
on reruns. Only `master` pushes or explicit manual `move_edge` runs on `master`
advance the supported moving `edge` channel. Tags never advance it; no
`latest`/`stable` channel is configured. No public-release, desktop-signing or
native-runtime job is a prerequisite.

Package visibility and anonymous pulls must be enabled and verified by the owner
after a real publication. Public MIT source at https://github.com/Hunter-124/Chudware
or an authenticated push does not establish public image access. See
[RELEASING.md](../RELEASING.md) for publication prerequisites and the separate
unpinned macOS ARM64 runtime blocker. Native payload/update signing remains unchanged.

## Reproducing a job locally

Source audits (any OS, Python 3.13):

```bash
python3 -m pip install -r requirements.txt
python3 scripts/check.py
```

Full Linux gate, inside the same container CI uses:

```bash
podman run --rm -it --init -v "$PWD:/src:Z" -w /src fedora:44 bash
bash .github/scripts/install-fedora-deps.sh gate
git config --system --add safe.directory /src
VENV_DIR=/tmp/gate-venv PYTHON=python3.13 bash scripts/provision_venv.sh pytest==9.1.1
CHUDWARE_GATE_PYTHON=/tmp/gate-venv/bin/python CHECK_PYTHON=/tmp/gate-venv/bin/python bash scripts/gate.sh
```

Without `CHUDWARE_GATE_STRICT=1` the gate skips stages whose toolchain is
missing. To match CI exactly, also install emsdk 6.0.3 (export `EMSDK`), fetch
the CEF SDK with `scripts/fetch_cef_posix.sh` (export the printed path as
`CEF_ROOT`) and set `CHUDWARE_GATE_STRICT=1`.

The live contact-FEA checks wait for terminal job results before asserting convergence,
material identity, and load transfer. A `wait_job` timeout ends only that wait, not the
solve, so these checks continue waiting instead of treating a still-running job as a
failed physical result. Interrupting a check cancels only its own unfinished job.

Native builds use the same presets as `native-build.yml`:

```bash
cmake --preset linux-core && cmake --build --preset linux-core -- -k 0
ctest --test-dir build/linux-core/core --output-on-failure
```

On Windows, run the same commands with `windows-occt` from a Visual Studio x64
developer prompt after `scripts\setup_windows_links.ps1`; on Apple silicon,
`brew install ninja opencascade` and use `macos-arm64`.

The Windows job allows six hours for the full Release/LTO build, core tests and
host GUI tests. The former four-hour limit canceled a progressing linker before
tests began; production optimization and test coverage remain enabled.

Eigen is consumed as pinned headers through `Eigen3::Eigen`, without configuring
its unused BLAS/LAPACK implementations. This avoids probing an unrelated MinGW
Fortran compiler in an MSVC build and does not change the application's system
BLAS/CHOLMOD acceleration.

Apple SDKs without floating-point `std::from_chars` use the existing
locale-independent numeric reader for SAT imports, with a bounded input view
rather than a token-string copy. Native parser tests cover decimal points,
exponents, partial input and exact token boundaries. Geometry identifiers avoid
the Windows SDK's `near` macro; the public JSON `near` selection hint is unchanged.
Constraint diagnostic closures explicitly capture their inputs for MSVC. Native
test variables must not shadow C functions used by assertion macros: on Darwin,
doctest calls `raise(SIGTRAP)`.

The native regressions also cover canonical private temporary roots on Darwin
without relaxing document-controlled symlink rejection, explicit singleton JSON
arrays, and one initialization owner for policy state shared by C++17/C++20 code.
Worker checks retain fast-child completion, crash and timeout containment; TCP
checks retain final bytes across expired deadlines and per-poll byte limits.
Rendering checks require opaque faces to cover shared triangle edges without
revealing an opposite face's finish.

Managed PolyMesh sources receive an authenticated, idempotent constant-sizing
grid correction during configure. Hashes authenticate the pinned original or exact
corrected source after normalizing Git's LF/CRLF checkout line endings; explicit
developer source overrides are never modified.
The meshing regressions require positive-quality two-body geometry for interface
sizes equal to or coarser than the background, not a particular error message.

### Complete self-host image

From the repository root, using a fresh isolated Compose project:

```bash
export COMPOSE_PROJECT_NAME=chudware-acceptance
export CHUDWARE_IMAGE=chudware-selfhost:acceptance
docker compose -f web/compose.yaml up --build -d
python3 -m venv .venv-smoke
.venv-smoke/bin/python -m pip install httpx==0.28.1 websockets==16.1
.venv-smoke/bin/python web/deploy/smoke.py http://127.0.0.1:8300 \
  --keep --receipt /tmp/chudware-smoke.json
docker compose -f web/compose.yaml down
docker compose -f web/compose.yaml up --no-build -d --force-recreate
.venv-smoke/bin/python web/deploy/smoke.py http://127.0.0.1:8300 \
  --resume /tmp/chudware-smoke.json
```

Choose an unused receipt path and port/deployment; do not run against an open
user session. The smoke creates one UUID-named `.step+`, verifies its hash
after recreation, reopens native geometry/mesh and checks the general Python
worker. It closes only its own documents and deletes only its exact unchanged
saved file on the resume run; no recents/chat rewrites or workspace-wide
deletions occur. The receipt remains local. Do not delete the volumes between
the two runs. For routine smoke with immediate cleanup omit `--keep`,
`--receipt` and `--resume`. The command's checks are operational acceptance,
not a substitute for the existing gateway/isolation suites or a real browser
rendering check.

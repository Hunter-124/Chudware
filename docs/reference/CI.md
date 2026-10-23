# Continuous integration

All workflows live in `.github/workflows/` and run on GitHub-hosted runners.
Linux jobs run in a `fedora:44` container because the core needs OCCT 7.9.x,
which Fedora packages as `opencascade-devel`;
`.github/scripts/install-fedora-deps.sh` is the single list of system packages.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `fast-gate.yml` | Every pull request, push to `master` | Runs `scripts/check.py` (source-level audits) on Linux, Windows and macOS with the audit packages pinned in `requirements.txt`. |
| `linux-gate.yml` | Every pull request, push to `master` | Runs `scripts/gate.sh` with `CHUDWARE_GATE_STRICT=1`: check.py, core and host builds, the WASM build smoke, core ctest, hostd smoke, the Python service suites and the headless screenshot smoke. Then runs the host GUI ctest suite and the `web/gateway` and `web/deploy` pytest suites. |
| `native-build.yml` | Push to `master` touching native code, manual | Configures, builds and tests through the CMake presets: `linux-core` plus the host GUI on Linux, `windows-occt` plus the host GUI on Windows, `macos-arm64` (core only) on macOS. Uploads nothing. |
| `site-build.yml` | Changes under `website/` | Builds the static site. On pushes to `master` it also deploys `website/dist` to the `chudware-site` Cloudflare Pages project; pull requests only build. |
| `release.yml` | Push of a `v*` tag, manual for an existing tag | Builds, signs and publishes a GitHub Release. See [RELEASING.md](../RELEASING.md). |

Strict mode makes `gate.sh` fail instead of skipping when a toolchain is
missing, so a broken CI image can never pass by testing nothing. Locally the
same script skips unavailable stages.

## Secrets and variables

Pull-request workflows use no secrets. Pull requests from forks run with a
read-only token and cannot reach any of the values below.

| Name | Kind | Used by | Purpose |
|---|---|---|---|
| `CHUDWARE_RELEASE_SIGNING_KEY` | Secret (required for releases) | `release.yml` | Base64 32-byte Ed25519 seed of release key `release-ci-v1`. Signs payload manifests and update manifests. |
| `CHUDWARE_WINDOWS_CERTIFICATE`, `CHUDWARE_WINDOWS_CERTIFICATE_PASSWORD` | Secrets (optional) | `release.yml` | Base64 PFX and its password for Authenticode signing of the installer and MSI. |
| `CHUDWARE_MACOS_CERTIFICATE`, `CHUDWARE_MACOS_CERTIFICATE_PASSWORD` | Secrets (optional) | `release.yml` | Base64 `.p12` with a Developer ID Application identity, and its password. |
| `CHUDWARE_NOTARY_APPLE_ID`, `CHUDWARE_NOTARY_TEAM_ID`, `CHUDWARE_NOTARY_PASSWORD` | Secrets (optional) | `release.yml` | Apple ID, team ID and app-specific password for notarization. |
| `CHUDWARE_MIN_SUPPORTED_VERSION` | Variable (optional) | `release.yml` | Oldest version that may keep running without a mandatory update. Defaults to `0.0.0`. |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | Secrets | `site-build.yml` | Cloudflare Pages deployment of chudware.com. |

[SIGNING.md](../SIGNING.md) explains how to create each signing value.

## Reproducing a job locally

Source audits (any OS, Python 3.13):

```bash
python3 -m pip install -r requirements.txt
python3 scripts/check.py
```

Full Linux gate, inside the same container CI uses:

```bash
podman run --rm -it -v "$PWD:/src:Z" -w /src fedora:44 bash
bash .github/scripts/install-fedora-deps.sh gate
VENV_DIR=/tmp/gate-venv PYTHON=python3.13 bash scripts/provision_venv.sh pytest==9.1.1 dvc==3.67.1
CHUDWARE_GATE_PYTHON=/tmp/gate-venv/bin/python CHECK_PYTHON=/tmp/gate-venv/bin/python bash scripts/gate.sh
```

Without `CHUDWARE_GATE_STRICT=1` the gate skips stages whose toolchain is
missing. To match CI exactly, also install emsdk 6.0.3 (export `EMSDK`), fetch
the CEF SDK with `scripts/fetch_cef_posix.sh` (export the printed path as
`CEF_ROOT`) and set `CHUDWARE_GATE_STRICT=1`.

Native builds use the same presets as `native-build.yml`:

```bash
cmake --preset linux-core && cmake --build --preset linux-core
ctest --test-dir build/linux-core/core --output-on-failure
```

On Windows, run the same commands with `windows-occt` from a Visual Studio x64
developer prompt after `scripts\setup_windows_links.ps1`; on Apple silicon,
`brew install ninja opencascade` and use `macos-arm64`.

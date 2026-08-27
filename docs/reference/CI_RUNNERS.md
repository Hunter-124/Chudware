# CI runners — the self-hosted fleet, and why CI costs $0

Chudware's CI runs entirely on self-hosted runners owned by the project. That is
not an aesthetic preference; it is the mechanism by which GitHub Actions costs
nothing on this **private** repository.

## The spend rule

Verified against
<https://docs.github.com/en/billing/concepts/product-billing/github-actions>:

| Thing | Billed on a private repo? |
|---|---|
| Self-hosted runner minutes | **No.** Verbatim: *"GitHub Actions usage is free for self-hosted runners."* |
| GitHub-hosted runner minutes | **Yes.** 2,000 free/month on GitHub Free, then per-minute (macOS is $0.062/min). |
| Larger runners | **Yes, always** — even on public repos. |
| Actions artifact storage | **Yes.** 500 MB free, *shared with GitHub Packages*, then $0.25/GB-month, accrued hourly. |
| Actions cache | Only if the repo cache limit is raised above the included 10 GB. Never raise it. |
| Job logs and `$GITHUB_STEP_SUMMARY` | **No** — explicitly not counted. |
| GitHub Release assets | **No** — release assets are not Actions storage. |

Two consequences that every workflow in this repo obeys:

1. **Every job is self-hosted.** No `ubuntu-latest`, no `windows-latest`, no
   `macos-latest`, not even for a one-line lint job.
2. **Build outputs never become Actions artifacts.** Installers are ~500 MB
   each; four of them would consume the entire allowance in a single tagged run,
   and deleting them afterwards does not refund the GB-hours already accrued.
   Artifacts go to Cloudflare R2. The one sanctioned `actions/upload-artifact`
   use is the cross-platform parity dumps, which are a few KB of JSON at
   `retention-days: 1`.

`scripts/verify-ci-workflows.py` enforces both, and `ci-workflow-policy.yml`
runs it on every change to `.github/workflows/**`. The verifier carries
negative-control fixtures that prove each rule still fires; a rule that has
never fired is not a rule.

## The fleet

Runners register to a *repository*, an organization, or an enterprise. `Hunter-124`
is a personal account with no organization tier, so the CloudCastle fleet cannot
serve this repository as registered. Each machine therefore runs **two** runner
instances: the original CloudCastle one, and a second CAD-scoped one in its own
directory with its own service name. The GitHub runner explicitly supports this.

| Runner | OS / Arch | Labels | CAD install dir |
|---|---|---|---|
| `livingroom-pc` | Linux X64 | `cloudcastle-ci`, `cc-linux-build`, `cc-linux-livingroom` | `/opt/actions-runner-cad` |
| `hunter-workstation` | Linux X64 | `cloudcastle-ci`, `cc-linux-hunter` | `/opt/actions-runner-cad` |
| `cloudcastle-server` | Linux X64 | `cloudcastle-ci`, `cc-prod-control`, `cc-artifact-host`, `cc-infra-webservice` | `/opt/actions-runner-cad` |
| `ryzen-7900x3d` | Windows X64 | `cloudcastle-ci`, `cc-win-primary`, `cc-win-package`, `cc-win-ryzen` | `C:\actions-runner-cad` |
| `imac-intel` | macOS X64 | `cloudcastle-ci`, `cc-mac-intel` | `~/actions-runner-cad` |
| `macbook-air-m2` | macOS ARM64 | `cloudcastle-ci`, `cc-mac-arm` | `~/actions-runner-cad` |

Labels deliberately match the CloudCastle install on the same machine, so
workflow routing syntax is portable between the two repositories.

### Roles: builder vs control

Endpoints are not interchangeable, and pretending they are made `runner-smoke`
permanently red. Every runner carries exactly one role, and each workflow's
expectations follow from it:

| Role | Labels | Must have |
|---|---|---|
| **builder** | `cc-linux-build`, `cc-linux-livingroom`, `cc-linux-hunter`, `cc-win-primary`, `cc-win-package`, `cc-win-ryzen`, `cc-mac-arm`, `cc-mac-intel` | git, Python >= 3.11, cmake, ninja, ctest, a C++ toolchain, >= 20 GiB free |
| **control** | `cc-prod-control`, `cc-artifact-host`, `cc-infra-webservice` | git and python, nothing else |

`cloudcastle-server` is control-only: it publishes artifacts and operates
production. It has **no `cmake` and no `ninja`**, deliberately, and demanding
them there describes a machine doing its job, not a fault. Never route a build
leg at the generic `cloudcastle-ci` pool without also pinning a builder label.

### Capability notes that workflows depend on

- **`livingroom-pc` is also the production host.** Keep heavy matrix legs on
  `hunter-workstation`; a release build that starves the box also starves
  paying customers.
- **Builders need Python >= 3.11**, not merely "a python3". `fast-gate` runs
  `scripts/check.py`, whose `advertised_tool_audit` imports the real MCP tool
  catalog and therefore PyYAML, the MCP SDK, NumPy and SciPy at the pins in the
  root `requirements.txt` — and the pinned NumPy and SciPy publish no wheel for
  anything older than 3.11. macOS ships 3.9.6, so both Macs carry a Homebrew
  `python@3.13`; `fast-gate` picks the newest suitable interpreter explicitly
  instead of trusting whatever `python3` resolves to.
- **The Windows endpoints have Windows PowerShell 5.1, not PowerShell 7.** Every
  Windows step must declare `shell: powershell`. `shell: pwsh` fails with
  `pwsh: command not found`, and `shell: bash` is worse: it resolves to an empty
  WSL shim that exits 0 without running the script, so the step passes having
  done nothing. A step that runs on more than one OS is therefore always split
  into an `if: runner.os != 'Windows'` half and an `if: runner.os == 'Windows'`
  half — `shell:` must name a literal interpreter and actionlint rejects an
  expression there anyway.
- **Both Macs have only the Xcode Command Line Tools**, not full Xcode. Nothing
  may invoke `xcodebuild`; `clang`, `cmake` and `ninja` only.
- Homebrew prefixes are `/opt/homebrew` on `macbook-air-m2` (arm64) and
  `/usr/local` on `imac-intel` (x86_64), which is what the `macos-arm64` and
  `macos-x86_64` CMake presets set `CMAKE_PREFIX_PATH` to. Homebrew is not on
  CMake's default search path.
- `CMakePresets.json` requires **CMake >= 3.24** on any runner that runs
  `cmake --preset`; `native-build.yml` checks this explicitly and fails with
  that message rather than a confusing generator error.
- **`native-build.yml` deliberately configures every host GUI build with
  `CHUDWARE_ENABLE_CEF=OFF`.** The per-push compile-and-test gate therefore uses
  `NullWebView` and does not compile the CEF backend; a CEF-only break surfaces
  at release time. Its Windows row additionally launches the built GUI with a
  deliberately missing managed worker, loads the same row's
  `build/windows-core/bin/libchudware.dll` through the in-process fallback, and
  validates a real PNG headless capture. Tagged releases remain CEF-enabled, so
  `cc-linux-build` and `cc-win-package` need the platform CEF SDK provisioned
  before the first tagged release.

### Provisioning: `runner-smoke.yml`'s dispatch-only `provision` job

`runner-smoke.yml` does not only report gaps, it can close the ones that can be
closed unattended. Dispatching it manually runs a `provision` job over
`cc-win-ryzen`, `cc-mac-intel` and `cc-mac-arm` before the probe, so a single
dispatch repairs the fleet and proves the repair in its own log. The scheduled
run never provisions: a nightly cron that silently mutates six machines is not a
probe.

On macOS it is `brew install` for any of `cmake`, `ninja`, `python@3.13` and
`opencascade` that is missing, and a no-op for any that is present — it repairs
a gap, it never upgrades a working builder underneath a release.

On Windows it is harder, and the shape is worth remembering. `ryzen-7900x3d`'s
runner service is `NT AUTHORITY\NETWORK SERVICE` (its `$env:USERNAME` reads
`GARRISONGAMINGP$`). That account cannot write `C:\Program Files`, cannot edit
the machine `PATH`, and cannot restart its own service — and the service takes
its environment as a snapshot at start, so even a successful `PATH` edit would
be invisible until someone reboots the box. So:

- Python is the **`python-build-standalone` relocatable tarball**, not the
  python.org installer. The installer was tried first and returned
  `0x80070005` (access denied) even with `InstallAllUsers=0`: Windows Installer
  cannot run a per-user install for `NETWORK SERVICE`. The tarball needs no
  msiexec, no registry and no elevation, is the same distribution
  `actions/setup-python` uses, and carries `pip` and `venv`, which is all
  `fast-gate` asks of it. It is unpacked with `System32\tar.exe` (bsdtar, in
  Windows since 10 1803).
- Python and ninja (the official `ninja-win.zip`) land **unelevated in
  `%USERPROFILE%\chudware-ci-tools`** — `\python` and `\bin`. Both downloads are
  version-pinned and SHA-256-verified in the workflow, the same discipline
  `ci-workflow-policy.yml` uses for its actionlint asset.
- Every Windows job puts that directory on `PATH` **itself**, in its first step:
  `$env:PATH` for the current step and `$GITHUB_PATH` for the ones after it.
  `fast-gate.yml`, `native-build.yml` and `runner-smoke.yml` all carry the same
  six-line block. This is the whole mechanism; there is no machine `PATH` entry
  to find and nothing survives outside that directory.

### PowerShell step bodies are ASCII-only

The runner writes a step's script to disk as UTF-8 with no BOM, and Windows
PowerShell 5.1 decodes a BOM-less file as the ANSI code page. A UTF-8 em dash
therefore arrives as three CP1252 characters ending in `”` — a curly closing
quote, which PowerShell accepts as a string delimiter. One em dash in a
*comment* silently terminates a string several lines above it and the step dies
at parse time with `missing closing '}'`, pointing at the wrong line, before a
single statement runs. This cost two provisioning attempts on `ryzen-7900x3d`.
`scripts/verify-ci-workflows.py` now enforces it as `powershell-non-ascii`,
with a seeded fixture in `ci-workflow-policy.yml` proving the rule fires.

### `dell-optiplex-7040` is deliberately absent

Its CloudCastle runner service runs as `NT AUTHORITY\NETWORK SERVICE`,
unelevated. That account cannot install a Windows service, and `schtasks`
refuses to create even a per-user logon task for it (*"No mapping between
account names and security IDs was done"*). A CAD runner can be *registered*
there but can never be *started*, and a permanently-offline runner in the fleet
list is worse than no entry: it looks like capacity that does not exist, and any
job routed at `cc-win-dell` hangs until it times out.

No workflow targets `cc-win-dell`, so nothing is lost. To add it properly,
someone with local Administrator on that machine runs, from an **elevated**
prompt:

```powershell
cd C:\actions-runner-cad
# token from: gh api -X POST /repos/Hunter-124/CAD/actions/runners/registration-token --jq .token
./config.cmd --unattended --url https://github.com/Hunter-124/CAD --token <TOKEN> `
  --name dell-optiplex-7040 --labels cloudcastle-ci,cc-win-dell --work _work --replace --runasservice
```

## Registering or re-registering a runner

Mint a registration token (valid one hour, reusable for several machines):

```bash
gh api -X POST /repos/Hunter-124/CAD/actions/runners/registration-token --jq .token
```

**Linux**, on a machine that already has a CloudCastle runner — clone that tree
rather than re-downloading, but exclude its job state:

```bash
sudo rsync -a --exclude '_work/' --exclude '_diag/' --exclude '.runner' \
  --exclude '.runner_migrated' --exclude '.credentials*' --exclude '.service' \
  --exclude '.path' --exclude '.env' /opt/actions-runner/ /opt/actions-runner-cad/
sudo chown -R "$USER:$USER" /opt/actions-runner-cad
cd /opt/actions-runner-cad
./config.sh --unattended --url https://github.com/Hunter-124/CAD --token <TOKEN> \
  --name "$(hostname -s)" --labels cloudcastle-ci,<capacity-label> --work _work --replace
sudo ./svc.sh install "$USER" && sudo ./svc.sh start
```

Copying `_work` is the mistake to avoid: it holds gigabytes of root-owned CI
checkouts and the copy fails partway on permissions.

Where the CloudCastle service has a `ci.slice` / `TMPDIR` drop-in, copy it to
`/etc/systemd/system/actions.runner.Hunter-124-CAD.<name>.service.d/` too — on
`hunter-workstation` that drop-in is what keeps CI scratch off the RAM-backed
`/tmp` that once OOM-killed the box.

**macOS and Windows** endpoints are not reachable over SSH from the workstation;
they only reach GitHub outbound. They were bootstrapped by dispatching a
workflow onto their *existing* CloudCastle runner, which is the one channel that
can execute a command on them. If that is needed again, the procedure is: add a
dispatch-only workflow to the CloudCastle repository, put a CAD registration
token in a repository secret (never a workflow input — inputs are logged),
dispatch it, then delete both the workflow and the secret.

On macOS, `./svc.sh install` creates a per-user LaunchAgent and needs no
elevation.

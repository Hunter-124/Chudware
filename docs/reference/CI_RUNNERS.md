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

### Capability notes that workflows depend on

- **`livingroom-pc` is also the production host.** Keep heavy matrix legs on
  `hunter-workstation` and `cloudcastle-server`; a release build that starves
  the box also starves paying customers.
- **`cloudcastle-server` has no `cmake` and no `ninja`.** It is a publish and
  control endpoint (`cc-artifact-host`), not a builder. Never route a build leg
  at the generic `cloudcastle-ci` pool without also pinning a builder label, or
  it can land here and fail on a missing toolchain.
- **The Windows endpoints have Windows PowerShell 5.1, not PowerShell 7.** Every
  Windows step must declare `shell: powershell`. `shell: pwsh` fails with
  `pwsh: command not found`.
- **Both Macs have only the Xcode Command Line Tools**, not full Xcode. Nothing
  may invoke `xcodebuild`; `clang`, `cmake` and `ninja` only.
- Homebrew prefixes are `/opt/homebrew` on `macbook-air-m2` (arm64) and
  `/usr/local` on `imac-intel` (x86_64), which is what the `macos-arm64` and
  `macos-x86_64` CMake presets set `CMAKE_PREFIX_PATH` to. Homebrew is not on
  CMake's default search path.
- `CMakePresets.json` requires **CMake >= 3.24** on any runner that runs
  `cmake --preset`; `native-build.yml` checks this explicitly and fails with
  that message rather than a confusing generator error.

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

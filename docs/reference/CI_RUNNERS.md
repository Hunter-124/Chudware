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

## Execution trust boundary (SupplyChain RS-2)

**Signing is blocked operationally until the evidence below is supplied.**
These workflows specify the required routing; they do not prove that runner
groups, disposable images, protected environments, or clean signing machines
have been provisioned. No live runner/configuration/credential check was made
for this cutover. The historical fleet inventory below is not approval to reuse
that fleet's existing writable state.

All minutes remain self-hosted: there is no paid GitHub-hosted fallback. A
`runs-on: {group: ..., labels: [self-hosted, ...]}` selects a self-hosted group
and narrows its membership by labels; it does **not** request a larger runner.
Missing groups/images intentionally leave jobs unschedulable, rather than
silently running branch code on the legacy signing account.

| Execution class | Group | Workflows/jobs | Mutable state and secrets |
|---|---|---|---|
| Branch/PR execution | `chudware-untrusted-ephemeral` | `fast-gate`, `ci-workflow-policy`, `linux-gate`, every `native-build` row (including tagged and manually selected branch builds) | One job per fresh OS/VM; no protected environments, signing keys, release transport credentials, production access, or write-capable repository token |
| Protected release/control/maintenance | `chudware-trusted-protected` | `nightly-release`, reusable `release-prepare`, `chudcode-runtime-windows-build`, `runner-provision`, `runner-smoke`, `deploy-prod`, reusable/direct `stripe-sandbox`, `site-build`, `prod-external-watch` | Separately administered, freshly provisioned or externally integrity-attested machines; environment-scoped credentials and protected source refs only |

The groups are disjoint. A runner belongs to one group; provisioning and smoke
jobs operate on the protected fleet, not on the branch workers. Within the
protected group, existing endpoint labels still distinguish builders from
production/control hosts. Its three separate environments are
`chudware-release-protected`, `chudware-control-protected`, and
`chudware-maintenance-protected`; they scope approval and credential delivery,
not OS isolation.

Disposable labels are `cc-linux-disposable`, `cc-win-disposable`,
`cc-mac-disposable-gate-intel` and `cc-mac-disposable-intel`. Mac images retain
the gate/native account identities and resource caps checked by the workflows,
but those accounts and their tool directories live **inside the disposable
guest**, not beside the signer on its OS. Native CPU architecture is still
required; a disposable Mac VM is supported, Rosetta is not. The complete
native/ARM configure/build/test matrix and the release packaging legs remain.

### Scheduling authorization cannot live in PR-editable YAML

Repository-scoped runners and labels alone cannot enforce this boundary: a PR
can edit its workflow to request a signing label or remove a job guard.
The personal-account repository configuration recorded below does not provide
the required organization/enterprise runner-group authorization. Before
enabling release execution, migrate the registration/repository ownership to
an organization/enterprise arrangement that actually supports **restricted
workflow access**, and provide the authenticated configuration export. GitHub
documents additional organization groups on Team and workflow restrictions in
Enterprise Cloud; verify the selected account's capability rather than assume
a free personal account supports them. This is an infrastructure/plan
prerequisite, not a change to the zero-paid-hosted-minutes convention.

Configure selected-repository access (this private repository only) for both
groups. On `chudware-trusted-protected`, set `restricted_to_workflows: true`
and allow only the defining workflow paths in the protected-class row above,
each fully qualified as `ORG/REPO/.github/workflows/NAME.yml@refs/heads/master`.
Do **not** authorize `fast-gate`, `ci-workflow-policy`, `linux-gate`,
`native-build`, a wildcard ref, arbitrary branches, or all workflows. Remove
legacy repository/default-group registrations capable of servicing those
labels; group-less or alternate-workflow requests must have no protected
capacity. Protect master and version tags against unreviewed creation/update,
and include workflow/script/lock/image changes in the protected review policy.

GitHub authorizes only jobs **directly defined** in an allowed workflow.
The tagged `release.yml` entry point therefore calls `release-prepare.yml`;
allowlist the reusable builder and its nested `stripe-sandbox.yml`, not just
the caller. For each approved `vX.Y.Z` tag, explicitly authorize both defining
reusable workflows at `@refs/tags/vX.Y.Z` (or an approved immutable SHA when
using a pinned call). Non-reusable protected workflows are authorized on
master only. Do not pretend `@refs/tags/v*` is an exact ref. A missing approved
tag entry must deny/queue the release, not fall back to a persistent PR lane.
Revoke obsolete tag authorizations when they are no longer needed.

Every persistent job also checks `github.ref_protected`, permits only master
or version-tag refs and push/dispatch/schedule events, and selects a protected
environment. Reusable callers pass only named sandbox credentials, never
`secrets: inherit`; the callee independently checks the original caller's
context and obtains its environment approval. These guards are defense in
depth, not a substitute for the server-side group restrictions.

Configure each protected environment with exact allowed protected refs,
required authorized reviewers, prevention of self-review/bypass as supported,
and least-privilege credentials. **Remove release/signing/publication/provider
and runner-administration secrets from repository/organization scopes visible
to branch workflows**; otherwise a modified branch can request them on its
disposable runner. Put release keys/identities/R2/gateway transport credentials
in the release environment, control/site/Stripe/alert credentials in the
control environment, and only required maintenance credentials in maintenance.
Keep the offline owner key out of Actions entirely.

### Required image and host evidence

An operator must provide dated, authenticated evidence for all of:

1. Group IDs, memberships, selected repository IDs, restricted workflow/ref
   entries (including nested reusable definitions), protected master/tag rules
   and environment protections. Record account/plan capabilities and show
   that a modified branch cannot schedule a protected member or read a dummy
   environment secret. Never include actual secret values in the packet.
2. Image IDs/digests and the external controller configuration registering
   workers with `--ephemeral` (one job), deleting their complete VM/OS disk,
   deregistering them after success/failure/cancellation, and quarantining
   failed destruction. Runner deregistration alone does not erase state.
3. No shared writable HOME/USERPROFILE, interpreter/venv/site-packages
   (including `.pth` hooks), PATH tools, Homebrew prefix, cargo/rustup,
   npm/pip caches, SDK/tool cache, workspace/temp, mount, keychain, certificate
   store or signing-agent socket between groups. A read-only, digest-qualified
   image seed is acceptable; mutable PR caches and PR-built images are not.
   Containers or separate users sharing the signer's OS are not the boundary.
   Guests must not reach host admin sockets, credentials or production networks.
4. Rebuild/quarantine evidence for every previously shared signing host before
   credentials are re-enabled. Merely relabeling the Windows primary/package
   service or Linux account preserves the audited persistence route. Attest
   the OS/image, interpreter startup inputs and native tool/SDK identities
   **before** job dispatch/secret release, from outside the writable checkout.
   `runner-provision`/`runner-smoke` run approved source; they do not themselves
   attest that a formerly compromised persistent interpreter is safe.
5. Exact protected commit/locked build inputs for every signed output. Protected
   jobs rebuild from the guarded event commit; they do not download Actions
   artifacts/caches produced by PR jobs or checkout a PR-supplied ref. Verify
   that release Python, native SDKs and mirrored runtime inputs match reviewed
   locks. The Linux gate uses `CHUDWARE_WHEELHOUSE` from its isolated image and
   `RUNNER_TEMP/gate-runtime`; a missing reviewed `RUNTIME_WHEELS.lock`/wheel
   set is a hard prerequisite, never permission to download private source.
   Its target snapshot must also include reviewed pytest, DVC 3.67.1 and
   their complete wheel dependency closure. They are additional authenticated
   lock entries, not direct product requirements. The workflow never performs
   a second pip install into the identity-checked runtime.
   Protected image services must export `CHUDWARE_CI_BASE_PYTHON` (an absolute
   executable path whose base interpreter/startup payload is externally
   qualified) and `CHUDWARE_WHEELHOUSE` (the approved complete offline wheel
   directory). Nightly and reusable release builders create their authenticated
   interpreter in `RUNNER_TEMP/release-python`. Only the separately trusted
   base runs `services.installer.provision --check-inputs --venv-dir ...`
   to inspect executable/launcher/pyvenv.cfg and installed wheel bytes offline
   before the candidate interpreter is ever launched. Bootstrap clears
   inherited `PYTHON*`, `LD_*` and `DYLD_*` injection variables first.
   The selected target must also cover every applicable declaration in
   `packaging/requirements-release.txt` and its full wheel closure. There is no
   public pip sync, mutable-home interpreter fallback, private source download,
   or build-backend execution. Protected `runner-provision` may prepare its
   persistent HOME-local release runtime through the same lock checks; an
   existing unverified runtime is refused rather than silently trusted or
   replaced. This wheel identity check supplements, not replaces, the external
   clean-machine/OS/native-tool evidence.
6. Real native test/evidence retention. Both CTest suites must execute tests,
   not only register disabled cases; `--no-tests=error` and JUnit counts/log
   digests are retained in canonical `test-results.json`. Prepared metadata
   hash-binds all five result/JUnit/log files. Finalization independently
   verifies counts and bytes; command strings and a passed flag are not proof.
   Every native format gets separate artifact/commit-bound signed evidence.
   Missing evidence or failed native commands stop signing/publication even
   when a builder leaves installer/AppImage bytes behind.

### Offline validation and routing smoke

After all source slices land, the integrator can run, without CI credentials:

```bash
python3 scripts/verify-ci-workflows.py --scope-all --no-annotations
python3 scripts/test_verify_ci_workflows.py
python3 scripts/test_run_release_ctest.py
python3 scripts/test_release_workflow_behavior.py
python3 -m pytest scripts/test_native_input_pins.py services/installer/test_wheelhouse.py
actionlint -shellcheck= -pyflakes= -ignore 'label .+ is unknown'
```

The dependency-free regression suite invokes the real policy CLI on disposable
fixture repositories: PR/branch positive cases, legacy/computed-group denial,
guard bypasses, secret/write-token denial, protected artifact/cache/ref denial,
and reusable caller/callee/secret checks, including incompatible Windows shell
routing. Windows jobs execute genuine PowerShell, never the no-op Bash shim.
Passing this is source-policy evidence
only; it cannot attest GitHub scheduling or machine destruction.
The native-input and wheelhouse fixture suites additionally cover missing image
inputs, refusal to execute a stale interpreter, offline wheel identity/closure,
and authenticated compiler prerequisites; they still do not exercise real CI.

For an offline machine smoke, use two local disposable OS images with no real
credentials: an untrusted fixture writes bounded sentinels into its home,
Python startup-hook directory and tool cache. Destroy that guest. Start a fresh
untrusted guest and a separately provisioned protected fixture guest and prove
that none of the sentinels exists or executes, including on abnormal shutdown.
Use only a dummy key/secret in the protected fixture and verify that it is
unreachable from the untrusted guest. Match a frozen, sanitized group-policy
export against normal PR/branch, edited-PR-to-protected-label, manual branch,
approved master/tag and nested-reusable scenarios: unauthorized cases must
have no eligible protected member; approved cases must retain all platform
rows. Later authenticated scheduling evidence is still required; never run
these smoke scenarios on customer sessions or real signing infrastructure.

References: [self-hosted runner groups and account prerequisites](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access),
[exact workflow/ref authorization](https://docs.github.com/enterprise-cloud@latest/actions/how-tos/manage-runners/self-hosted-runners/manage-access),
[runner-group API](https://docs.github.com/en/rest/actions/self-hosted-runner-groups).

## Historical fleet inventory (not RS-2 qualification)

The following inventory records the former personal-account/repository-scoped
registrations and capability notes. It is retained for operator migration,
not evidence that these machines are isolated or authorized for signing.
Do not attach disposable labels to these persistent services. Protected
registrations must be rebuilt/attested and moved to the enforced protected
group before use; branch workers are separate disposable guests.

Legacy labels and install directories:

| Runner | OS / Arch | Labels | CAD install dir |
|---|---|---|---|
| `livingroom-pc` | Linux X64 | `cloudcastle-ci`, `cc-linux-livingroom` | `/opt/actions-runner-cad` (service runs as `liveuser`) |
| `hunter-workstation` | Linux X64 | retired; service and GitHub registration removed | `/opt/actions-runner-cad` (retained source/SDK, not build capacity) |
| `cloudcastle-server` | Linux X64 | `cloudcastle-ci`, `cc-prod-control`, `cc-artifact-host`, `cc-infra-webservice` | `/opt/actions-runner-cad` |
| `chudware-ryzen-linux` | Linux X64 | `cc-linux-chudware`; `cloudcastle-ci` and `cc-linux-build` only after qualification | `/opt/actions-runner-chudware` inside the `Chudware-CI` WSL2 guest |
| `ryzen-7900x3d` | Windows X64 | `cloudcastle-ci`, `cc-win-primary`, `cc-win-package`, `cc-win-ryzen`, `cc-win-chudware` | `C:\actions-runner-cad` (work folder `D:\ChudwareCI\Windows\w`) |
| `imac-intel` | macOS X64 | `cloudcastle-ci`, `cc-mac-intel` | `~/actions-runner-cad` (operator/signer lane: GUI account, login keychain, signing identities, Homebrew owner) |
| `imac-intel-chudware-test` | macOS X64 | `cloudcastle-ci`, `cc-mac-chudware-intel-bootstrap`, `cc-mac-chudware-intel` | `/Library/CloudCastleCI/ccci-chudware/actions-runner` (system LaunchDaemon `com.cloudcastle.ci.runner.chudware` as the hidden `ccci-chudware` account) |
| `imac-intel-chudware-gate-test` | macOS X64 | `cc-mac-chudware-gate-intel-bootstrap`; `cloudcastle-ci` and `cc-mac-chudware-gate-intel` after qualification | `/Library/CloudCastleCI/ccci-chudware-gate/actions-runner-gate` (system LaunchDaemon `com.cloudcastle.ci.runner.chudware.gate` as the hidden UID 423 `ccci-chudware-gate` account) |
| `macbook-air-m2` | macOS ARM64 | offline, not currently registered; ordinary checks do not route here | `~/actions-runner-cad` (operator lane: GUI account, Homebrew owner) |
| `macbook-air-m2-chudware-test` | macOS ARM64 | **not registered yet** — will register with only `cc-mac-chudware-arm-bootstrap`; `cloudcastle-ci` and `cc-mac-chudware-arm` only after qualification. No ordinary workflow routes at it (see [ARM coverage without ARM hardware](#arm-coverage-without-arm-hardware)) | `/Library/CloudCastleCI/ccci-chudware/actions-runner` on the M2 (system LaunchDaemon `com.cloudcastle.ci.runner.chudware` as the hidden `ccci-chudware` account) |

Native builds now require disposable capacity labels in the untrusted group.
`cc-linux-build`, `cc-win-package`/`cc-win-ryzen` and `cc-mac-intel` are
protected capability labels only; they no longer service fast-gate,
ci-workflow-policy, linux-gate or native-build. A formerly qualified private
account/WSL guest is not automatically a qualified RS-2 signing machine.

**nightly-release splits serving from building.** `plan` is metadata/API work
only and routes at `cc-linux-livingroom`: it reads the release token from
`/home/liveuser/chudware-web-data/gateway.env` (custody stays with `liveuser`
on the serving host) to ask loopback `http://127.0.0.1:8300` whether `HEAD`
already published and to record the queued rows; it compiles nothing.
`linux-beta` builds on the protected `cc-linux-build` endpoint and needs no
production file, NAS release path or production SSH credential: like the
Windows and macOS legs it obtains `CHUDWARE_GATEWAY_RELEASE_TOKEN` from its
protected release environment, keeps it in a run-scoped 0600 curl config and
publishes over TLS to `https://app.chudware.com`.
Every signed byte stays in its `RUNNER_TEMP`; the artifact and each payload
manifest/signature/evidence sidecar go up under its canonical name through
`PUT /api/releases/ci/upload/<name>` (streamed up to 64 MiB, otherwise 64 MiB
offset chunks with `complete=1`; each reply's sha256 must match the local
digest), and only then does `ci/publish` run, where the gateway re-digests
every staged file against the signed body. The bash client is
`packaging/ci_gateway.sh` (`gw_open`, `gw_post`, `gw_upload`,
`gw_upload_verified`, `gw_upload_release`, `ci_status`), shared with
`macos-beta`; Windows uses `packaging/windows/publish_release.ps1`.
Neither gateway.env nor production service configuration is copied into a build
guest; approved release credentials are delivered separately by its protected
environment.

### Roles: builder vs control

Endpoints are not interchangeable, and pretending they are made `runner-smoke`
permanently red. Every runner carries exactly one role, and each workflow's
expectations follow from it:

| Role | Labels | Must have |
|---|---|---|
| **builder** | `cc-linux-build`, `cc-linux-chudware`, `cc-linux-hunter`, `cc-win-primary`, `cc-win-package`, `cc-win-ryzen`, `cc-mac-arm`, `cc-mac-intel`, `cc-mac-chudware-intel`, `cc-mac-chudware-gate-intel`, `cc-mac-chudware-arm` | git, Python >= 3.11, cmake, ninja, ctest, a C++ toolchain that compiles, links and runs, >= 20 GiB free |
| **control** | `cc-linux-livingroom`, `cc-prod-control`, `cc-artifact-host`, `cc-infra-webservice` | git and python, nothing else |

`cloudcastle-server` is control-only: it publishes artifacts and operates
production. It has **no `cmake` and no `ninja`**, deliberately, and demanding
them there describes a machine doing its job, not a fault. Never route a build
leg at the generic `cloudcastle-ci` pool without also pinning a builder label.

### Capability notes that workflows depend on

- **`livingroom-pc` is the production host, not build capacity.** Its CAD
  runner runs as `liveuser` and carries only `cc-linux-livingroom`, which
  exists for `deploy-prod.yml`'s updater, nightly-release's metadata-only
  `plan` job (above), and runner-smoke's inventory and probe. The livingroom
  smoke row is control-only too: it must not compile or require a builder's
  20 GiB free-space threshold on the serving SSD.
  The updater builds there because it must, under `liveuser`, against the canonical
  checkout; nothing else may be routed at that label, because a build that
  starves the box also starves paying customers.
- **`livingroom-pc` cannot be woken remotely as currently cabled.** Measured
  2026-09-03 from its own CloudCastle RMM inventory (`devices.inventory.wake`)
  while it was offline: `wired_wol` is armed in the OS (`ethtool` reports
  `wol g` on `eno1`) but blocked by `no_wired_link` — the machine runs on Wi-Fi
  (`wlp2s0`, DHCP `192.168.1.78`), and a magic packet has nowhere to arrive on a
  NIC with no carrier. `wifi_wol` is blocked by `driver_no_wowlan`, and the
  `cloudcastle-wake.timer` RTC schedule reports `powers_on_from_off: false`, so
  it resumes from sleep only. MACs: `eno1` `e4:54:e8:80:f2:57`, `wlp2s0`
  `fa:bc:db:88:50:2d` (randomized by NetworkManager, so it is not a stable WoL
  target either). **Connect `eno1` to the LAN** and the wake path becomes real —
  `cloudcastle-wol.service` re-arms `wol g` on every boot — which is the only
  change that makes production recoverable without hands
  on the box.
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
  CMake's default search path. The prefix is owned by the operator account;
  the private test account reads it and never writes it.
- **Ordinary Mac tests run inside disposable OS images.** The gate image uses
  `ccci-chudware-gate`; native/ARM images use `ccci-chudware`. The account name,
  0700 home, `CC_CI_PRODUCT=Chudware`, `CC_CI_LANE=test`, native architecture
  and compiler/test concurrency caps remain checked. They do not establish
  isolation on their own. The whole guest, including image-local Homebrew,
  Python, home and caches, must be fresh and destroyed after one job.
  Signed packaging (`release-prepare.yml`, nightly-release's `macos-beta`)
  and `runner-provision.yml` run only on the separate protected fleet.
- **The ARM private test account does not exist yet.** It is created only by
  running CloudCastle's `provision-macos-test-runner.sh` (product `Chudware`)
  natively on the physical M2, which is offline; there is no runtime proof of
  it. No ordinary workflow waits for it: native-build covers arm64 macOS by
  cross-compiling on the Intel test account, which proves compilation and
  linking only. Native Apple-silicon runtime stays unqualified until that
  account is registered, and neither the `cc-mac-arm` operator account nor an
  Intel lane is ever its substitute.
- `CMakePresets.json` requires **CMake >= 3.24** on any runner that runs
  `cmake --preset`; `native-build.yml` checks this explicitly and fails with
  that message rather than a confusing generator error.
- **`native-build.yml` is release-only** (`v*` tags plus manual dispatch), and
  deliberately configures every host GUI build with `CHUDWARE_ENABLE_CEF=OFF`.
  Ordinary pushes run fast-gate + linux-gate without claiming Windows/macOS
  hardware. The native release gate uses `NullWebView` and therefore does not
  compile the CEF backend; CEF is covered by the separate packaging workflow.
  The Windows row additionally launches the built GUI with a deliberately
  missing managed worker and the engine daemon off: the engine-dependent `m1`
  scenario must load that row's `build/windows-occt/bin/libchudware.dll`
  through the in-process fallback, exit 0, write a real PNG and save its part;
  a copy of the host `bin` directory outside the checkout, with no library
  anywhere, must fail `m1` with exit 1 and no capture while the static `chat`
  shot still exits 0 with a PNG. Release packaging remains CEF-enabled,
  so `cc-linux-build` and `cc-win-package` need the platform CEF SDK provisioned.
- **Windows builds are resource-bounded.** The Windows legs of `native-build.yml`
  and `nightly-release.yml` derive `CMAKE_BUILD_PARALLEL_LEVEL` from half the
  logical processors, capped by currently free physical memory less an 8 GiB
  reserve at 2 GiB per job; run build and packaging steps at below-normal
  priority (inherited by cmake, ninja, cl, link and Nuitka); keep ctest serial;
  and give each run its own `_MSPDBSRV_ENDPOINT_`, so no build shares an
  account-wide `mspdbsrv`. The headless GUI capture sets `CHUDWARE_HIDDEN=1` and
  nested PowerShell runs `-NonInteractive -WindowStyle Hidden`: nothing a
  runner launches may reach anyone's desktop.

### ARM coverage without ARM hardware

Native-build completes its three native baseline rows before either ARM row
starts. Per-run workflow concurrency prevents a delayed ARM row from locking
the next native build out; no row is cancelled to achieve this. Both ARM targets
use x86_64 builders, no paid runner and no M2. Each row's `test_mode` states
exactly what it proves:

| Row | Builder | Preset | `test_mode` | Proves |
|---|---|---|---|---|
| `Linux aarch64 (QEMU-emulated tests)` | `cc-linux-chudware` (`chudware-ryzen-linux` guest) | `linux-aarch64-qemu` | `qemu` | all registered portable OCCT core tests as aarch64 ELF under qemu-user, including the AICM cross-process shm/file-fallback handoff, whose child the x86_64 `/bin/sh` starts through binfmt_misc |
| `macOS arm64 (cross-compiled, compile-only)` | `cc-mac-chudware-intel` (`ccci-chudware`) | `macos-arm64-cross` | `compile-only` | the OCCT core compiles and links as arm64 Mach-O; nothing is executed |

Neither proves ARM hardware: emulation is not ARM timing, memory ordering or
kernel behaviour, and the Intel iMac has no Rosetta and cannot run arm64 code
at all. Native Darwin arm64 runtime qualification remains with the physical M2
test account above; the signed `cc-mac-arm`/`cc-mac-intel` release lanes are
unchanged. Both rows check every binary in `build/<preset>/bin` (readelf
`AArch64` / `lipo -archs` `arm64`) after the build, and the emulated row
refuses to run ctest unless the generated CTest file launches tests through
`cmake/toolchains/qemu-aarch64-run.sh`. That wrapper sets `QEMU_LD_PREFIX`
and gives every test its own `TMPDIR`, because the emulated suite runs ctest
in parallel across the guest's CPUs and several tests write fixed file names
into the temp directory.

QEMU runs the FP-heavy mesh/FEA code far slower than native. Every registered
portable core test executes; a slow test is a failure, not an exclusion or
native-only claim. Emulated tests are capped at 1,800 seconds; shorter explicit
allowances are retained. Numerical tests that exceeded 600 seconds are not
excluded, and native test allowances are unchanged.
The row's summary distinguishes registered/executed tests from ARM hardware
qualification.

Every ARM command is hard-bounded by `scripts/run_bounded.py` (stdlib Python;
macOS has no GNU `timeout`), which runs the command in its own process group
and on overrun sends that group SIGTERM, then SIGKILL 15 s later, and exits
124; a failing command's own status passes through. Every SDK fetch gets
10 min, configure 15 min and install 10 min; core configure 15 min; the
emulated ctest run 180 min with 7,200 s per test. Build budgets differ by row
because the Intel Mac test account compiles with one process
(`CMAKE_BUILD_PARALLEL_LEVEL=1`):

| Row | OCCT build (other SDK builds) | Whole SDK step | Core build | Job (deadline) |
|---|---|---|---|---|
| `Linux aarch64 (QEMU-emulated tests)` | 30 min (30 min) | 60 min | 30 min | 240 min (237) |
| `macOS arm64 (cross-compiled, compile-only)` | 200 min (30 min) | 230 min | 120 min | 330 min (327) |

The Mac budgets are measured, not guessed: a cold darwin-arm64 OCCT build
reached 1,241 of its 5,476 Ninja actions in 30 minutes (about 1.45 s each,
slowly rising), so it needs about 133 minutes; 200 minutes allow 2.2 s per
action. The cold native `macos-x86_64` core build on the same account took
69 minutes (with LTO), so the cross core build gets 120. A cold SDK plus the
core build is about 215 minutes against a 327-minute deadline; a cached SDK
reruns only its consumer check before the core build. The job-wide deadline,
three minutes short of the job budget, caps every bounded command, so a cold
SDK that leaves too little time for the rest fails that step with 124 rather
than running into Actions' hard stop. Changing `build_arm_sdk.sh` (budgets
included) changes the recipe id, so the next run of each row rebuilds its SDK
cold. The cross presets build with `CHUDWARE_LTO=OFF`: nothing they build ships.

The complete QEMU run measured 3,203 seconds for `mesh` and 5,667 for
`mesher_advanced`; the 180-minute suite cap then stopped the remaining serial
fixtures. The suite now has a 210-minute bound, still at most 120 per test
and clipped by the unchanged 237-minute job deadline. Existing explicit native
test timeouts receive an eightfold allowance capped at 7,200 seconds only for
Linux aarch64 cross-tests. That row also sets the existing
`CHUDWARE_TEST_WALL_SCALE=8`: per-part assertions retain finite 720-second
ceilings (2,160 for the bore), while native ceilings remain unchanged. No test,
meshing assertion or existing serialization is removed.

The same run exposed an ARM FMA shared-edge parity defect, not an emulation
tolerance issue. The pinned Polyhedral-FEA classifier now canonically orders
edge endpoints so neighboring triangles receive bitwise-opposite signs. Its
retained public-classifier regression failed before and passed all 48 assertions
after on actual aarch64 binaries under QEMU; the STEP-bar consumer passed all
47 original assertions without relaxing requested-size or element-count bounds.

The ARM dependencies come from `scripts/build_arm_sdk.sh <linux-aarch64|darwin-arm64>`,
which builds pinned sources into the runner account's own
`RUNNER_TOOL_CACHE/chudware-arm-sdk/<target>/<recipe id>` (no Homebrew, shared
prefix or other product's account). Cache identity includes the build recipe,
target toolchain, QEMU wrapper and compiler/SDK identity. Completed SDKs are
atomically published under an owned lock; incomplete or invalid SDKs rebuild.
OCCT `V7_9_3` at `a016080b` includes visualization, application framework and
data exchange, alongside FreeType, RapidJSON and, on Linux, fontconfig and
static OpenSSL inside shared libcurl. Both cold and cached SDKs must configure
and link a consumer of all installed OCCT exports. The Linux consumer runs
real box-volume, mesh, CAF, glTF, FreeType, curl and fontconfig checks under
QEMU; Darwin validates thin arm64 links without execution. A timeout remains
exit 124, never a successful cache hit.
Both ARM presets set `CHUDWARE_WITH_CHOLMOD=OFF` because the SDK carries no
SuiteSparse, so FEA uses the Eigen solver. The Polyhedral-FEA learned advisor
is off on aarch64 because only x86-64 ONNX Runtime archives are pinned.

Root-owned prerequisites in the Chudware Linux guest (installed by
CloudCastle's `provision-nvme-linux-ci.sh`, never by the runner account):
`g++-aarch64-linux-gnu` (cross libraries in `/usr/aarch64-linux-gnu`),
`qemu-user-static` with an enabled `qemu-aarch64` binfmt_misc entry in that
guest's own kernel, `perl`, `make`, `curl`, `pkg-config` and `gperf` (the
OpenSSL and fontconfig builds), plus the `python3` every row already needs
(it runs the bounds). Ubuntu's arm64 multiarch packages are
deliberately not used: `libcurl4-openssl-dev:arm64` cannot be co-installed with
the amd64 package the native build needs. The Mac row needs only the Command
Line Tools (SDK and `/usr/bin/python3`).

To reproduce on an x86_64 Linux host with those packages (each command
bounded as in CI):

```bash
export CHUDWARE_ARM_SDK="$(python3 scripts/run_bounded.py 3600 scripts/build_arm_sdk.sh linux-aarch64)"
python3 scripts/run_bounded.py 900 cmake --preset linux-aarch64-qemu
python3 scripts/run_bounded.py 1800 cmake --build --preset linux-aarch64-qemu
CHUDWARE_TEST_WALL_SCALE=8 python3 scripts/run_bounded.py 12600 \
  ctest --test-dir build/linux-aarch64-qemu/core --output-on-failure --parallel "$(nproc)" --timeout 7200
```

and the compile-only arm64 build on an Intel Mac with the Command Line Tools:

```bash
export CHUDWARE_ARM_SDK="$(python3 scripts/run_bounded.py 13800 scripts/build_arm_sdk.sh darwin-arm64)"
python3 scripts/run_bounded.py 900 cmake --preset macos-arm64-cross
python3 scripts/run_bounded.py 7200 cmake --build --preset macos-arm64-cross
```

### Provisioning and qualification: `runner-smoke.yml`

`runner-smoke.yml` does not only report gaps, it can close the ones that can be
closed unattended. Dispatching it manually runs a `provision` job over whichever
of `cc-win-ryzen`, `cc-mac-intel` and `cc-mac-arm` are online before the probe,
so a single dispatch repairs the fleet and proves the repair in its own log. The
scheduled run never provisions: a nightly cron that silently mutates six
machines is not a probe.

On a builder the probe compiles, links, runs and ctests a one-file CMake/Ninja
C++ project in `RUNNER_TEMP` (on Windows through the same vswhere-then-
`vcvars64.bat` discovery native-build uses), because a toolchain that only
answers `--version` proves nothing about headers, the linker or the loader.
Fleet rows that share hardware with another product's CI also carry an
isolation contract the probe asserts against the live job (next section).

Its `runner_label` dispatch input selects one endpoint instead of `all`:

```bash
gh workflow run runner-smoke.yml -R Hunter-124/Chudware -f runner_label=cc-win-ryzen
gh workflow run runner-smoke.yml -R Hunter-124/Chudware -f runner_label=cc-linux-chudware
gh workflow run runner-smoke.yml -R Hunter-124/Chudware -f runner_label=cc-mac-chudware-intel-bootstrap
```

Only that endpoint is provisioned (when it is provisionable) and probed, so
qualifying a new runner never queues rows for unrelated or offline hardware. A
targeted row is routed by `self-hosted`, OS, architecture and the unique runner
label only, **without `cloudcastle-ci`**: a runner joins that pool after this
run qualifies it, so demanding the pool label would make qualification
impossible. The cron and `all` dispatches keep routing every row through
`cloudcastle-ci`. Without `CI_RUNNER_ADMIN_TOKEN` (absent today) those runs
cannot see which runners are online, so they queue the registered fleet minus
`macbook-air-m2` (`may_be_offline`): a queued row for the offline M2 never
starts, so its timeout never applies, and it would hold every daily run open
for GitHub's 24-hour queue limit. `-f runner_label=cc-mac-arm` still probes
it on demand.

On macOS provisioning is `brew install` for any of `cmake`, `ninja`,
`python@3.13` and `opencascade` that is missing, and a no-op for any that is
present — it repairs a gap, it never upgrades a working builder underneath a
release. It runs only on the operator lanes (`cc-mac-intel`, `cc-mac-arm`),
which own Homebrew.

The private Mac test accounts `imac-intel-chudware-test`,
`imac-intel-chudware-gate-test` and (once it exists)
`macbook-air-m2-chudware-test` are never provisioned (`provision:false`; the
provision step also refuses any `CC_CI_LANE=test` account). Their probe only
CHECKS the same formulae, Python >= 3.11 and a real compile/link/ctest, and
asserts the isolation contract: the row's `ccci-chudware` or
`ccci-chudware-gate` account in the hidden 420-499 uid range, running natively
on the Mac's own architecture (no Rosetta, no VM), the row's private `HOME`
at mode 0700 and workspace under its declared runner root, `TMPDIR` and the
XDG directories inside that home, `CC_CI_PRODUCT=Chudware`, `CC_CI_LANE=test`,
numeric `CMAKE_BUILD_PARALLEL_LEVEL`/`CC_CI_BUILD_JOBS`, serial ctest, and a
Homebrew prefix it does not own. A missing formula is fixed from that Mac's
operator lane (`runner-provision.yml` with `cc-mac-intel` or `cc-mac-arm`).
Promotion order: the runner registers with only its bootstrap label
(`cc-mac-chudware-<intel|arm>-bootstrap` for builders,
`cc-mac-chudware-gate-intel-bootstrap` for the source gate); dispatch that
label (the row keeps `self-hosted`, `macOS` and its architecture), and only
after it is green add `cloudcastle-ci` and the matching product routing label.
CloudCastle's manager creates the gate with `--product Chudware --gate`;
that mode cannot purge accounts, run legacy cleanup or install Rust.

`macbook-air-m2-chudware-test` carries `registered:false` in runner-smoke's
fleet list: a cron or `all` run reports it under *Pending registration* rather
than queueing a row nothing can claim, and only a targeted dispatch of
`cc-mac-chudware-arm-bootstrap` (or `cc-mac-chudware-arm`) probes it. Drop the
marker, and update the table above, in the change that records its
registration.

On Windows `ryzen-7900x3d`'s runner service is its own non-administrator
virtual account (next section). That account cannot write `C:\Program Files`,
cannot edit the machine `PATH`, and cannot restart its own service — and the
service takes its environment as a snapshot at start, so even a successful
`PATH` edit would be invisible until the service restarts. So:

- Python is the **`python-build-standalone` relocatable tarball**, not the
  python.org installer. The installer was tried first and returned
  `0x80070005` (access denied) even with `InstallAllUsers=0`: Windows Installer
  cannot run a per-user install for a service account. The tarball needs no
  msiexec, no registry and no elevation, is the same distribution
  `actions/setup-python` uses, and carries `pip` and `venv`, which is all
  `fast-gate` asks of it. It is unpacked with `System32\tar.exe` (bsdtar, in
  Windows since 10 1803).
- Python and ninja (the official `ninja-win.zip`) land **unelevated in
  `%USERPROFILE%\chudware-ci-tools`** — `\python` and `\bin`, inside the
  account's private `D:\ChudwareCI\Windows\profile`. Both downloads are
  version-pinned and SHA-256-verified in the workflow, the same discipline
  `ci-workflow-policy.yml` uses for its actionlint asset.
- Console-script launchers in `python\Scripts` embed the absolute interpreter
  path they were generated for. The tools tree moved with the account from the
  old `NETWORK SERVICE` profile, so provisioning reinstalls any distribution
  whose launchers name another interpreter, and the probe fails on any stale
  launcher that remains. Workflows still call `python -m pip`, never a launcher.
- Windows jobs prepend their **own image/account's** tools directory to PATH.
  Identical path strings in two separate VM disks are not shared state;
  mounting the persistent release profile into a disposable job is forbidden.
  `runner-smoke` provisioning targets only the approved protected fleet.
  Untrusted images are prepared offline and independently qualified.

### Per-product isolation on GarrisonGamingPC

GarrisonGamingPC (`GARRISONGAMINGP`) hosts CloudCastle, Chudware and Chudcode
CI. Their tests must never collide, and persistent build caches live on the
4TB NVMe (`D:`), never on the serving SSD. The boundary is collision safety
between products, not a malicious-tenant security boundary: the CloudCastle
runner keeps the elevated capability its own jobs require.

**Linux** runs in one WSL2 guest per product, each launched by its own
non-administrator, batch-only Windows account, so each product gets its own
WSL utility VM (kernel) and an ext4 root on its own VHDX under `D:\<Product>CI`.
No guest mounts another product's storage, and no guest holds production
secrets.

| Product | Windows account | Guest | Runner / account | Root on D: | Labels |
|---|---|---|---|---|---|
| Chudware | `ChudwareLinuxCI` | `Chudware-CI` (`chudware-ryzen-linux`, 24 GB / 8 CPU) | `chudware-ryzen-linux` as `ccci-chudware` in `/opt/actions-runner-chudware` | `D:\ChudwareCI` | `cc-linux-chudware`, then `cloudcastle-ci` + `cc-linux-build` once qualified |
| CloudCastle | `CloudCastleLinuxCI` | `CloudCastle-CI` | its own repository | `D:\CloudCastleCI` | not this repository |
| Chudcode | `ChudcodeLinuxCI` | `Chudcode-CI` | its own repository | `D:\ChudcodeCI` | not this repository |

The guest's HOME (pip/uv/venv/emsdk caches), runner work tree and temp all sit
on that ext4 root, so they persist on the NVMe and are private to the guest.
runner-smoke's `chudware-ryzen-linux` row asserts the account, a non-root
runner, an owned HOME, a workspace under `/opt/actions-runner-chudware/_work`,
the `/etc/cloudcastle-ci/product` binding `Chudware`, a WSL2 kernel, and ext4
for `/`, HOME, `RUNNER_TEMP` and the workspace (never a host share).
Promotion order: dispatch `runner_label=cc-linux-chudware`; only after it is
green does `cloudcastle-ci`/`cc-linux-build` get added to the runner.

**Native Windows** keeps the single CAD runner `ryzen-7900x3d`
(`C:\actions-runner-cad`, service
`actions.runner.Hunter-124-CAD.ryzen-7900x3d`), which runs as the
non-administrator virtual account
`NT SERVICE\actions.runner.Hunter-124-CAD.ryzen-7900x3d`
(`S-1-5-80-1193026161-3971065153-2732555818-2173988348-3783229323`) with a
private, ACL-protected namespace:

| Path | Use |
|---|---|
| `D:\ChudwareCI\Windows\profile` | `USERPROFILE`, `HOME`, `APPDATA`, `LOCALAPPDATA`; tools in `profile\chudware-ci-tools`; `CARGO_HOME` `profile\.cargo`; `RUSTUP_HOME` `profile\.rustup` |
| `D:\ChudwareCI\Windows\tmp` | `TEMP`, `TMP` |
| `D:\ChudwareCI\Windows\w` | runner work folder (`RUNNER_TEMP` `w\_temp`, `RUNNER_TOOL_CACHE` `w\_tool`) |
The copied work tree is owned by the virtual service SID. Administrative
copy ownership is not accepted through a global Git `safe.directory` exception:
it is repaired with the runner idle/stopped, without following link targets.

CloudCastle's native runner keeps its own elevated virtual account and
`D:\CloudCastleCI\Windows\cc`; Chudcode's dedicated native runner has its own
non-administrator virtual account and `D:\ChudcodeCI\Windows`. runner-smoke's
`ryzen-7900x3d` row asserts the account name and SID, a token without an
enabled Administrators group, the exact profile/home/cache/temp values,
APPDATA/LOCALAPPDATA/workspace/runner temp/tool cache under the namespace, a
fixed D: volume with no reparse point on any of those paths, that neither
sibling namespace is even listable, and a real MSVC compile/link/run. The
machine-level Visual Studio Build Tools, Windows SDK and CMake are shared
read-only; nothing mutable is shared.
Native fixture files use `std::filesystem::temp_directory_path()` and therefore
the account's `TMPDIR`/`TEMP`, not literal shared `/tmp` names. This also keeps
operator-lane release tests from leaving sticky-directory files that break
the private Mac account's next test.

Inside the code, named OS objects are per process: the AICM shared-memory
handoff (`core/util/aicm_handoff.cpp`) scopes every `Local\chudware_*` mapping
and fallback file by the publishing process id and refuses a mapping that
already exists instead of writing into it. Session 0's `Local\` namespace is
shared by every service, so a bare per-process counter name would let two
runners' test processes alias each other's meshes.
Python consumers use `services.aicm_handoff.read_handoff` as borrowed readers.
Linux and macOS open the existing POSIX object read-only with `shm_open` and
never register, unregister or unlink it through Python's resource tracker.
Windows holds an existing named-mapping handle before calling `mmap`, so a
missing publication cannot create a fake empty object. Only shared-memory URI
names are percent-decoded; fallback filenames are used literally. The native
publisher keeps its object alive across independent reader exits and releases
it through its own document/process lifetime.

The provisioning and identity cutover are operated from the CloudCastle
repository (`CloudCastle-RMM/deploy/selfhost/provision-nvme-linux-ci.{ps1,sh}`
and `relocate-windows-ci-cache.ps1`), not from a workflow on this repository:
the old `chudcode-runner-windows-provision.yml`, which cloned this runner into
a second `NETWORK SERVICE` service through an interactive scheduled task, is
deleted.

### Pinned Chudcode artifact (`CHUDWARE_CHUD_BIN`)

Desktop releases target Linux x86_64, macOS x86_64 and Windows x86_64. Every
release embeds the compiled `chud` runtime. The nightly capability step uses
`scripts/pin_chudcode.py ensure`: an explicit `CHUDWARE_CHUD_BIN` is verified
offline; otherwise a digest-keyed cache under the runner account's
`~/chudware-ci-tools/chudcode/<sha256>/chud` (`chud.exe` on Windows) is verified,
or populated from this repository's release mirror. Packaging still uses the
strictly offline `stage` command.

The mirror tag is `chudcode-runtime-v<lock version>` in `Hunter-124/Chudware`.
Its assets keep the filenames from `vendor/chudcode/CHUDCODE_ARTIFACT.lock`:

| Runner | Lock target | Mirror asset |
|---|---|---|
| the `cc-linux-build` runner | `linux-x86_64` | `chudcode-linux-x64` |
| `imac-intel` | `macos-x86_64` | `chudcode-darwin-x64` |
| `ryzen-7900x3d` | `windows-x86_64` | `chudcode-windows-x64.exe` (must be built and pinned before the Windows nightly can pass) |

Only the capability step receives the CAD repository's `github.token`
(`contents: read`). No private-runtime PAT, runner environment change, Mac SSH
connection or runner service restart is needed. The downloader resolves the
asset through the authenticated GitHub API and follows its signed HTTPS release
CDN URL **without** forwarding Authorization. The checked-in lock remains
authoritative: format/architecture and SHA-256 are checked before execution,
then native `--version` must report exactly `Chudcode v<lock version>`. A wrong
mirror asset, corrupt cache, wrong explicit installation or unpinned target
fails closed; none can bypass verification. Remove a damaged cache entry
deliberately before trying again.

For a new runtime pin, mirror the verified bytes in the private CAD release
before dispatching nightly. `ensure` derives the tag from the locked version,
so builders fetch the new digest without manual installation. Do not commit
binaries or use metered Actions artifacts.

Upstream Chudcode releases contain no Windows CLI: the Linux cross-compiled
`chudcode-windows-x64.exe` segfaults at startup in Bun 1.4.0. Build the exact locked
commit on the existing Ryzen runner using
`.github/workflows/chudcode-runtime-windows-build.yml`. Upload an authenticated source tarball
to the same CAD mirror as
`chudcode-source-<full locked repository_commit>.tar.gz`, with the archive root
`Hunter-124-chudcode-<full locked repository_commit>/`, and supply its SHA-256
as the builder's `source_archive_sha256` dispatch input. The builder verifies
the archive, builds the Windows native addon and compiled CLI, smoke-checks
the runtime, and retains the resulting bytes outside `_work`. Its explicit
`publish` input defaults to false; when enabled, the separate upload job receives
`contents: write` and publishes the Windows binary to the CAD runtime mirror.
After obtaining and verifying those bytes, pin the Windows lock row's URL and
SHA-256 and re-dispatch nightly. Never invent a digest or substitute a Linux
binary for the Windows runtime.

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

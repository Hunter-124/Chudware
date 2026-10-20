# Windows packaging

Windows release legs run on the `cc-win-package` self-hosted runner
(`.github/workflows/release.yml`, `.github/workflows/nightly-release.yml`). The customer
payload is **source-free and signed**: `services.installer.build_release` stages compiled
binaries only, writes `payload-manifest.v1.json`, verifies an offline Ed25519 payload
signature, then invokes Inno Setup and the optional Authenticode hook. The installer
filename and the binary's `--version` come from the same generated version header.

## Components

- **Core** — `windows-occt` preset → `build\windows-occt\bin\` (`libchudware.dll`,
  `chudware-hostd.exe`); see `docs/reference/BUILD_WINDOWS.md`.
- **Host GUI** — `host\build-win\bin\chudware.exe` plus the CMake-staged runtime beside it
  (CEF files, `assets\`, `locales\`, `chudware-uninstall-helper.exe`). `release.yml` uses
  `host\build-windows` and passes its paths explicitly.
- **Compiled sidecars** — `packaging/windows/build_sidecars.ps1` Nuitka-compiles
  `chudware-worker.exe` and `chudware-mcp.exe` from `services/compiled_entry.py`, stages the
  pinned Chudcode `chud.exe`, and generates the protected prompt/role assets. It needs a
  release Python with `packaging/requirements-release.txt` installed (Nuitka, PyYAML,
  cryptography, and the runtime packages the sidecars must bundle, e.g. pycolmap/numpy) and
  host/hostd trees configured with `-DCHUDWARE_DEVELOPER_BUILD=OFF`.
- **`services/installer/build_release.py`** — copies the GUI runtime tree (rejecting source,
  `.pdb`, test binaries and symlinks), the required `chudware-hostd.exe`,
  `chudware-worker.exe`, `chudware-mcp.exe`, `chud.exe`, `chudware-uninstall-helper.exe` and
  `install.ps1` into `<out>\Chudware\`, then writes and verifies the payload manifest.
  `--prepare-signing --payload-only` emits the signing preimage; `--finalize-signing <sig>`
  (or `--payload-signature`) verifies the signature and, unless `--payload-only`, compiles
  `dist\Chudware-Setup-<version>-x86_64.exe`. `--zip` additionally keeps
  `Chudware-release.zip`.
- **`packaging/windows/chudware.iss`** — presents `LICENSE-EULA.txt`, installs per-user under
  `%LOCALAPPDATA%\Programs\Chudware` as a byte-for-byte copy of the verified payload, and on
  uninstall runs the hash-checked native `chudware-uninstall-helper.exe`. It does **not** run
  `install.ps1` or any Python provisioning.
- **Developer path (not used by the packaged installer)** — `services/installer/install.ps1`
  provisions a managed Python 3.11 venv only from a complete offline wheelhouse and
  independently reviewed `vendor/python/RUNTIME_WHEELS.lock`, then registers MCP
  clients. Missing identities fail closed; see [runtime prerequisites](BUILD_WINDOWS.md#reviewed-runtime-identity-prerequisite).
  `services/installer/uninstall.ps1` only launches the registered OS uninstaller.

## Prerequisites on the Windows build host

1. Visual Studio Build Tools with the MSVC x64 toolchain, CMake ≥ 3.24 and Ninja.
2. A release Python (`CHUDWARE_RELEASE_PYTHON`, else `.venv\Scripts\python.exe`, else
   `python.exe` on `PATH`) with `packaging/requirements-release.txt` installed.
3. The Chudcode artifact (`CHUDWARE_CHUD_BIN`) matching `vendor/chudcode/CHUDCODE_ARTIFACT.lock`.
4. [Inno Setup 6](https://jrsoftware.org/isinfo.php), with `ISCC.exe` on `PATH` or its full
   path in `ISCC`.
5. The release key id/public key for payload signing, and for Authenticode the certificate and
   Windows SDK `signtool.exe` described in `docs/SIGNING.md`.

## Build

From an x64 Native Tools PowerShell (mirrors the nightly Windows leg):

```powershell
cmake --preset windows-occt -DCHUDWARE_DEVELOPER_BUILD=OFF
cmake --build --preset windows-occt
ctest --test-dir build\windows-occt\core --output-on-failure
cmake -S host -B host\build-win -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_LTO=ON -DCHUDWARE_DEVELOPER_BUILD=OFF
cmake --build host\build-win
ctest --test-dir host\build-win --output-on-failure
$sidecars = Join-Path $env:TEMP "chudware-release-sidecars"

powershell -ExecutionPolicy Bypass -File packaging\windows\build_sidecars.ps1 `
  -OutputDir $sidecars -HostBinary host\build-win\bin\chudware.exe `
  -HostdBinary build\windows-occt\bin\chudware-hostd.exe

python -m services.installer.build_release --prepare-signing --payload-only --out dist `
  --worker-bin $sidecars\chudware-worker.exe --mcp-bin $sidecars\chudware-mcp.exe `
  --chud-bin $sidecars\chud.exe
# sign dist\Chudware\payload-manifest.v1.preimage offline, then:
python -m services.installer.build_release --finalize-signing .\payload.sig `
  --release-public-key .\release-public.pem --out dist
# -> dist\Chudware-Setup-<CHUDWARE_VERSION>-x86_64.exe
```

`--host-bin`/`--hostd-bin` default to `host\build-win\bin\chudware.exe` and the first existing
`build\windows-occt\bin\chudware-hostd.exe` (then `windows-core` and legacy layouts).
Do not pass `/DAppVersion` manually. The Inno script has no placeholder default and refuses
to compile unless `build_release.py` supplies the version read from the generated header.
The finalization example assumes the offline signer supplied `payload.sig` and
the matching public-key PEM at `release-public.pem`; these are operator-provided inputs,
not files generated by the build. The commands show the packaging tree, not evidence of
a successful Windows build or install.

## Verification checklist (run once on a real Windows box)

- [ ] `dist\Chudware-Setup-<version>-x86_64.exe` runs without error and completes; the
      installed tree under `%LOCALAPPDATA%\Programs\Chudware` matches
      `payload-manifest.v1.json` (no source, no `.pdb`).
- [ ] `%LOCALAPPDATA%\Programs\Chudware\chudware.exe` launches with no Python installed on the
      machine (the worker, MCP server and engine are compiled payload files).
- [ ] The Start Menu has "Chudware" and "Uninstall Chudware" entries.
- [ ] Uninstalling via "Apps & Features" runs `chudware-uninstall-helper.exe`, removes the
      manifest-owned files, and refuses (removing nothing) if the helper was modified.
- [ ] Developer path only: `install.ps1` from an unpacked payload provisions
      `%APPDATA%\Chudware\runtime\venv` and `python -m services.installer.provision --check`
      reports `"ready": true`.

## Engine-host daemon (chudware-hostd) — Windows validation checklist (Phase 6)

The Windows backend uses named pipes, Job Objects and an owner-private exclusive
file handle for single-instance admission; the retired public named mutex is not
authority. It is a required release payload file. Isolated native Windows 11/MSVC
developer execution and security/failure-path results are recorded in
`reports/security/2026-10-12/release-hardening-remediation-evidence.json` and its
`windows-native-evidence.zip` artifact. These results are not signed-installer,
approved-private-runtime or GPU/desktop qualification. CI's daemon-disabled GUI
smoke (`CHUDWARE_ENGINE_DAEMON=0`) does not supply that missing evidence.

The platform sources include `services/ipc/ipc_transport_win.cpp`,
`services/ipc/ipc_discovery_win.cpp`, `core/hostd/child_supervisor_win.cpp`,
`core/hostd/worker_bridge.cpp` and `services/mcp/daemon_bridge.py`. The checklist
below remains the installed-runtime acceptance gate, not a claim that every
manual scenario has passed.
Use an isolated instance key for this checklist and only terminate the daemon and
children started for that validation. A running user session is not a test fixture.

### Build

- [ ] `cmake --build --preset windows-occt --target chudware-hostd` succeeds with MSVC and the
      `_win.cpp` files compile without signed/unsigned or handle-cast warnings (treat any as a
      review miss, not noise).
- [ ] `chudware-hostd.exe --version` prints the build id. `--status` with no daemon prints
      "no running instance" and exits 1.

### Daemon lifecycle

- [ ] Run `chudware-hostd.exe --foreground` in one terminal. Second terminal:
      `chudware-hostd.exe --status` returns JSON with `state`, `pid`, `children`.
      Task Manager shows exactly ONE chudware-hostd.exe.
- [ ] Start a second `chudware-hostd.exe --foreground`: it must exit 0 immediately
      (private file-lock claim lost), leaving the first serving.
- [ ] `chudware-hostd.exe --stop` drains and exits the daemon; the pipe name disappears
      (`Test-Path \\.\pipe\chudware-hostd-<SID>-<key>` in PowerShell goes $false).
- [ ] Idle exit: start the daemon with `CHUDWARE_HOSTD_LINGER_SECS=5`, connect nothing —
      it exits on its own within ~6 s and the log (`%APPDATA%\Chudware\logs\hostd.log`)
      says "idle ... exiting".

### MCP client (daemon_bridge.py pipe transport)

- [ ] `set CHUDWARE_ENGINE_DAEMON=1`, start a Claude Code session with the Chudware MCP
      server; a `new_part` + `add_features` round trip works, and Task Manager shows the
      engine (OCCT memory) inside chudware-hostd.exe, NOT the python MCP process.
- [ ] Dual-client race: launch two MCP sessions simultaneously from a state with no
      daemon — exactly one chudware-hostd.exe results; both sessions' calls succeed.
- [ ] Kill chudware-hostd.exe in Task Manager mid-session; the next MCP call reconnects
      (respawn + one retry) and succeeds after the engine reloads.

### Job containment (KILL_ON_JOB_CLOSE + breakaway-denied fallback, plan Top-risk 1)

- [ ] agent child containment: `agent_spawn` a `powershell -c sleep 600`, then kill the
      DAEMON with Task Manager (hard kill, not --stop). The powershell child must die
      immediately (its Job Object's last handle closed -> KILL_ON_JOB_CLOSE). This is the
      PDEATHSIG-analog acceptance test.
- [ ] `agent_stop` (non-forced) terminates the child ~5 s later (grace timer -> job
      termination; there is no SIGTERM analog on Windows — expected).
- [ ] Breakaway-denied recipe: wrap the spawning client in a deny-breakaway
      KILL_ON_JOB_CLOSE job and confirm the documented degraded mode. PowerShell wrapper
      (run as admin not required):

      ```powershell
      # jobwrap.ps1 — approximates Claude Code's job containment:
      # create a job with KILL_ON_JOB_CLOSE and WITHOUT LimitBreakawayOk,
      # assign a child cmd.exe to it, and from THAT cmd run the MCP client.
      $cs = @"
      using System; using System.Runtime.InteropServices;
      public static class J {
        [DllImport("kernel32")] public static extern IntPtr CreateJobObject(IntPtr a, string n);
        [DllImport("kernel32")] public static extern bool SetInformationJobObject(IntPtr j, int c, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION i, int l);
        [DllImport("kernel32")] public static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
        [StructLayout(LayoutKind.Sequential)] public struct JOBOBJECT_BASIC_LIMIT_INFORMATION { public long a,b; public uint LimitFlags; public UIntPtr c,d; public uint e,f,g,h; }
        [StructLayout(LayoutKind.Sequential)] public struct IO_COUNTERS { public ulong a,b,c,d,e,f; }
        [StructLayout(LayoutKind.Sequential)] public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION { public JOBOBJECT_BASIC_LIMIT_INFORMATION Basic; public IO_COUNTERS Io; public UIntPtr p1,p2,p3,p4; }
      }
      "@
      Add-Type -TypeDefinition $cs
      $job = [J]::CreateJobObject([IntPtr]::Zero, $null)
      $info = New-Object J+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
      $info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no BREAKAWAY_OK (0x800)
      [J]::SetInformationJobObject($job, 9, [ref]$info, [Runtime.InteropServices.Marshal]::SizeOf($info)) | Out-Null
      $p = Start-Process cmd -PassThru -ArgumentList '/k','title jobbed-shell'
      [J]::AssignProcessToJobObject($job, $p.Handle) | Out-Null
      Write-Host "jobbed shell pid $($p.Id) — run the MCP client from that window; close THIS PowerShell to kill the job"
      while ($true) { Start-Sleep 5 }   # keep $job handle alive
      ```

      Inside the jobbed cmd, trigger a daemon spawn (python -c "from services.mcp import
      daemon_bridge; daemon_bridge.client().call('ping')"). Expect: the loud
      "job breakaway denied" warning on stderr, the daemon still comes up and serves,
      and closing the wrapper PowerShell (job handle drop) kills daemon + children.
      Then verify the next client cleanly respawns a daemon.
- [ ] GUI-first durability: start the GUI (never jobbed) with CHUDWARE_ENGINE_DAEMON=1
      first, then agent sessions; killing the agent sessions leaves the daemon alive.

### Pipe security (plan Top-risk 2)

- [ ] Squat resistance: pre-create the pipe name from another (same-user) process
      (`[System.IO.Pipes.NamedPipeServerStream]::new("chudware-hostd-<SID>-<key>")`) and start
      the daemon — it must FAIL loudly at listen() (FILE_FLAG_FIRST_PIPE_INSTANCE), not
      silently coexist.
- [ ] Spoof resistance: with the squatter serving instead of a real daemon, a client
      connect must be REFUSED ("pipe server identity mismatch" requires a different-user
      squatter; same-user squatters pass the SID check by design — confirm the error path
      with a second Windows account or a service-hosted squatter).
- [ ] DACL: `[System.IO.Pipes.NamedPipeClientStream]` connect from a DIFFERENT user
      account fails with access denied.

### Uninstall hygiene

- [ ] With no children active, `chudware-hostd.exe --stop` drains the validation daemon.
      With a validation child active, bare `--stop` refuses; `--stop --force` stops that
      child and the validation daemon. Do not force-stop unrelated/user instances.

## Installer integration — verification checklist

Current wiring (untested on a real Windows box): `build_release.py` requires and stages
`chudware-hostd.exe` (never its `.pdb`); the `.iss` sets `CloseApplications=yes`; the developer
`install.ps1` stops install-dir daemons (`--stop --force`, then an image-path-scoped
`taskkill`) and `register_mcp.py --hostd-bin` pins `CHUDWARE_HOSTD_BIN` in the MCP server env.
See `docs/reference/HOSTD.md` for the daemon reference.

- [ ] `build_release` staging: `dist\Chudware\` contains `chudware-hostd.exe` and
      `payload-manifest.v1.json` lists it; staging fails outright when the daemon binary is
      missing.
- [ ] Fresh install: `dist\Chudware-Setup-<ver>-x86_64.exe /VERYSILENT` completes;
      `%LOCALAPPDATA%\Programs\Chudware\chudware-hostd.exe --version` prints the build id.
- [ ] Developer `install.ps1` path: the `chudware` entry in `~\.claude.json` (and Claude
      Desktop config) has `env.CHUDWARE_HOSTD_BIN` = the installed exe path.
- [ ] Shared daemon: launch the GUI with `CHUDWARE_ENGINE_DAEMON=1`, then an MCP session —
      Task Manager shows ONE `chudware-hostd.exe`, image path under the install dir.
- [ ] Upgrade-over-running-daemon: with the installed daemon serving, re-run the installer —
      Restart Manager / the daemon stop path releases it, the payload copy succeeds (no
      locked-file errors), and a dev-tree `chudware-hostd.exe` running simultaneously from the
      repo is NOT killed.
- [ ] `/VERYSILENT` install → `--status` OK → silent uninstall
      (`"%LOCALAPPDATA%\Programs\Chudware\unins000.exe" /VERYSILENT`): no
      `chudware-hostd.exe` remains and the install dir is removed.
- [ ] `CloseApplications=yes`: run the installer while `chudware.exe` is open — Restart
      Manager prompts/handles it instead of failing the file copy.

## Known constraints

- **The packaged installer does not provision Python.** Customer installs run the compiled
  payload; only the developer `install.ps1` path needs Python 3.11 and `provision.py`.
- **The EULA is mandatory.** Inno Setup reads the repository-root `LICENSE-EULA.txt` and
  presents it before installation.
- **GPL subprocess tools are never bundled**, per `contracts/licenses.md`: CalculiX, esptool,
  LibreDWG `dwg2dxf` and similar are subprocess-only and discovered at runtime (the
  pattern `services/installer/provision.py:converters_status()` uses for pandoc/LibreOffice;
  LibreDWG is also found in the per-user install the user builds with
  `python3 scripts/provision_libredwg.py`) — the Windows installer must not add a step that
  downloads or bundles any of those.

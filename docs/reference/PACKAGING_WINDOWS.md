# Windows packaging (T-Q1)

**This document was written on a Linux box that cannot build, run, or verify any of the steps
below** — no MSVC, no Inno Setup, no Windows `Chudware.exe`. Everything here is derived from
reading `services/installer/{build_release.py,install.ps1,uninstall.ps1}` (which already exist
and already work for a manual/dev install) plus `host/src/bootstrap.cpp`'s POSIX branch,
which the Windows branch mirrors exactly (same `%APPDATA%/Chudware` layout, same
`installer.provision` invocation). A Windows host must run through this once and report back
before T-Q1's Windows box can be marked more than `[~]`.

## What already exists (don't rebuild it)

- `services/installer/install.ps1` / `uninstall.ps1` — a complete, working installer/uninstaller
  already: copies the payload, provisions the managed runtime venv, registers the MCP server,
  sets up file associations, creates a Start Menu shortcut. This has presumably been run and
  used manually already (it predates T-Q1); it just isn't wrapped in a normal installer `.exe`.
- `services/installer/build_release.py` — assembles the payload these scripts consume
  (`Chudware.exe`, `assets/`, `app/`, `wheelhouse/`, `requirements.txt`) into `dist/Chudware/`.
- `packaging/windows/Chudware.iss` (this task) — a thin Inno Setup wrapper around the above two:
  stages the payload, runs `install.ps1` as a silent post-install step, registers
  `uninstall.ps1` as the uninstall action. **Not verified on Windows** — see the checklist below.

## Prerequisites on the Windows build host

1. Visual Studio Build Tools (or full VS) with the MSVC toolchain — for `host/build` (the current
   Windows GUI target — `host/` is the single GUI on both OSes).
2. Python 3.11 on PATH (or the `py` launcher with a 3.11 install) — `install.ps1` and
   `installer/provision.py` both resolve this the same way `_find_base_python()` does.
3. [Inno Setup 6](https://jrsoftware.org/isinfo.php) — installs `ISCC.exe` (the command-line
   compiler); add its install dir to PATH or invoke it by full path below.

## Build steps

```powershell
# 1. Build the host GUI (`host/` is the single GUI target on both OSes).
cmake --build host\build --config Release

# 2. Assemble the release payload (app/ + wheelhouse/ + the exe + assets).
python -m installer.build_release --out dist --zip
#   -> dist\Chudware\  (payload folder)  and  dist\Chudware-release.zip

# 3. Compile the installer.
ISCC packaging\windows\Chudware.iss /DPayloadDir=dist\Chudware /DAppVersion=<version>
#   -> dist\Chudware-Setup-<version>.exe
```

`<version>` should come from `git describe --tags --always` (matching the Linux AppImage's
version stamping in `packaging/linux/build_appimage.sh`) — e.g. in PowerShell:
`$env:AppVersion = (git describe --tags --always).Trim()`.

## Verification checklist (run once on a real Windows box)

- [ ] `dist\Chudware-Setup-<version>.exe` runs without error and completes (watch for the
      "Provisioning the Chudware runtime" step — this is a real `pip install` of build123d/OCP/VTK
      and can take several minutes; the installer should not appear to hang without a status
      message during this).
- [ ] After install, `%LOCALAPPDATA%\Programs\Chudware\Chudware.exe` launches and the worker starts
      (no "Python runtime not installed" banner — the first-run dialog /
      Settings ▸ Advanced ▸ Runtime runs the same check the GUI does).
- [ ] `%APPDATA%\Chudware\runtime\venv\Scripts\python.exe` exists and
      `%APPDATA%\Chudware\runtime\.ready.json` has a `req_hash` matching the shipped
      `requirements.txt` (`installer.provision.is_ready()`'s own check).
- [ ] The Start Menu has an "Chudware" entry (note: currently duplicated — see the `.iss` file's
      comment; `install.ps1` also makes its own shortcut).
- [ ] Uninstalling via "Apps & Features" removes the Start Menu entries and `%APPDATA%\Chudware`
      (via `uninstall.ps1`'s `[UninstallRun]` step), leaving no orphaned MCP client registration
      (`installer.register_mcp --unregister` runs as part of `uninstall.ps1`).
- [ ] `python -m installer.provision --check` inside the installed `app\` reports `"ready": true`.

## Engine-host daemon (chudware-hostd) — Windows validation checklist (Phase 6)

The Windows backend of the shared engine daemon (named pipes, Job Objects, named-mutex
single instance) was **written and code-reviewed on Linux, never compiled or executed on
Windows**. The sources are `services/ipc/ipc_transport_win.cpp`, `services/ipc/
ipc_discovery_win.cpp`, `core/hostd/child_supervisor_win.cpp`, plus small `#ifdef _WIN32`
branches in `core/hostd/main.cpp`, `services/worker/engine_backend.cpp` and
`services/mcp/daemon_bridge.py`. Every item below must pass on a real Windows 10/11 box
before Windows daemon mode is considered shipped. Installer integration (`/VERYSILENT`
install staging `chudware-hostd.exe`, install.ps1/uninstall.ps1 calling `--stop`) is Phase 7 —
noted here so nobody hunts for it in this phase.

### Build

- [ ] `cmake --build core\build --config Release --target chudware-hostd` succeeds with MSVC
      (the target is no longer POSIX-gated). Expect zero warnings from the `_win.cpp` files;
      treat any signed/unsigned or handle-cast warning as a review miss, not noise.
- [ ] `chudware-hostd.exe --version` prints the build id. `--status` with no daemon prints
      "no running instance" and exits 1.

### Daemon lifecycle

- [ ] Run `chudware-hostd.exe --foreground` in one terminal. Second terminal:
      `chudware-hostd.exe --status` returns JSON with `state`, `pid`, `children`.
      Task Manager shows exactly ONE chudware-hostd.exe.
- [ ] Start a second `chudware-hostd.exe --foreground`: it must exit 0 immediately
      (named-mutex race lost), leaving the first serving.
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

### Uninstall hygiene (dry-run of the Phase 7 wiring)

- [ ] After `chudware-hostd.exe --stop`, no chudware-hostd.exe remains in Task Manager and no
      agent children survive. (Phase 7 will call this from uninstall.ps1; verify the
      primitive works now.)

## Installer integration (Phase 7) — verification checklist

The Phase-7 wiring (written and reviewed on Linux, untested on Windows): `build_release.py`
stages `chudware-hostd.exe`/`.pdb`, `install.ps1`/`uninstall.ps1` stop the install-dir daemon
first, `register_mcp.py --hostd-bin` pins `CHUDWARE_HOSTD_BIN` in the MCP server env, the
`.iss` gained `CloseApplications=yes`. See `docs/reference/HOSTD.md` for the daemon reference.

- [ ] `python -m installer.build_release --zip` after
      `cmake --build core\build --config Release --target chudware-hostd`: `dist\Chudware\`
      contains `chudware-hostd.exe` (and `.pdb`), `manifest.json` says `"hostd": true`.
      Without the core build it prints the warning and stages the rest (`"hostd": false`).
- [ ] Fresh install: `dist\Chudware-Setup-<ver>.exe /VERYSILENT` completes;
      `%LOCALAPPDATA%\Programs\Chudware\chudware-hostd.exe --version` prints the build id.
- [ ] MCP registration env: `~\.claude.json` (and Claude Desktop config) `chudware`
      entry has `env.CHUDWARE_HOSTD_BIN` = the installed exe path.
- [ ] Shared daemon: launch the GUI with `CHUDWARE_ENGINE_DAEMON=1`, then an MCP session —
      Task Manager shows ONE `chudware-hostd.exe`, image path under the install dir.
- [ ] Upgrade-over-running-daemon: with the installed daemon serving, re-run the
      installer — install.ps1's `--stop` (plus scoped taskkill) stops it, the payload
      copy succeeds (no locked-file errors), and a dev-tree `chudware-hostd.exe` running
      simultaneously from the repo is NOT killed.
- [ ] `/VERYSILENT` install → `--status` OK → silent uninstall
      (`"%LOCALAPPDATA%\Programs\Chudware\unins000.exe" /VERYSILENT`): no
      `chudware-hostd.exe` remains, install dir removed, MCP entry unregistered.
- [ ] `CloseApplications=yes`: run the installer while Chudware.exe is open — Restart
      Manager prompts/handles it instead of failing the file copy.

## Known gaps (not fixed here — flagging so they aren't rediscovered)

- **No bundled Python.** `build_release.py --with-python` is a documented `TODO` (drop a
  `python-build-standalone` CPython into `python-embed/`) — today's installer depends on the
  target machine already having Python 3.11 on PATH. Not fully "offline-friendly" until that
  lands.
- **Duplicate Start Menu shortcut** (`install.ps1` + the `.iss`'s own `[Icons]` entry) — cosmetic,
  needs an `install.ps1 -NoShortcut` flag to fix cleanly; not changed here since it can't be
  tested without a Windows box.
- **No EULA/license page** in the `.iss` — fine for internal dogfooding, needed before any
  external release (product is commercial closed-source per `CONTRIBUTING.md`).
- **GPL subprocess tools are never bundled**, per the license guardrails table
  (`CONTRIBUTING.md` ▸ License guardrails): CalculiX/OpenFOAM/esptool/etc. are discovered on
  PATH at runtime (same pattern `services/installer/provision.py:converters_status()` already
  uses for pandoc/LibreOffice) — the Windows installer must not add a step that downloads or
  bundles any of those.

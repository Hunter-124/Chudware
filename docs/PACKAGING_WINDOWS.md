# Windows packaging (T-Q1)

**This document was written on a Linux box that cannot build, run, or verify any of the steps
below** — no MSVC, no Inno Setup, no Windows `AI-CAD.exe`. Everything here is derived from
reading `services/installer/{build_release.py,install.ps1,uninstall.ps1}` (which already exist
and already work for a manual/dev install) plus `services/worker/bootstrap.cpp`'s POSIX branch,
which the Windows branch mirrors exactly (same `%APPDATA%/AI-CAD` layout, same
`installer.provision` invocation). A Windows host must run through this once and report back
before T-Q1's Windows box can be marked more than `[~]`.

## What already exists (don't rebuild it)

- `services/installer/install.ps1` / `uninstall.ps1` — a complete, working installer/uninstaller
  already: copies the payload, provisions the managed runtime venv, registers the MCP server,
  sets up file associations, creates a Start Menu shortcut. This has presumably been run and
  used manually already (it predates T-Q1); it just isn't wrapped in a normal installer `.exe`.
- `services/installer/build_release.py` — assembles the payload these scripts consume
  (`AI-CAD.exe`, `assets/`, `app/`, `wheelhouse/`, `requirements.txt`) into `dist/AI-CAD/`.
- `packaging/windows/AI-CAD.iss` (this task) — a thin Inno Setup wrapper around the above two:
  stages the payload, runs `install.ps1` as a silent post-install step, registers
  `uninstall.ps1` as the uninstall action. **Not verified on Windows** — see the checklist below.

## Prerequisites on the Windows build host

1. Visual Studio Build Tools (or full VS) with the MSVC toolchain — for `host/build` (the current
   Windows GUI target; `ui/imgui` has no Windows CMake preset yet, see note below).
2. Python 3.11 on PATH (or the `py` launcher with a 3.11 install) — `install.ps1` and
   `installer/provision.py` both resolve this the same way `_find_base_python()` does.
3. [Inno Setup 6](https://jrsoftware.org/isinfo.php) — installs `ISCC.exe` (the command-line
   compiler); add its install dir to PATH or invoke it by full path below.

## Build steps

```powershell
# 1. Build the host GUI (the Windows target — ui/imgui has no Windows CMake preset as of this
#    writing; T-G11 "Windows build verify, then delete host/" tracks migrating this).
cmake --build host\build --config Release

# 2. Assemble the release payload (app/ + wheelhouse/ + the exe + assets).
python -m installer.build_release --out dist --zip
#   -> dist\AI-CAD\  (payload folder)  and  dist\AI-CAD-release.zip

# 3. Compile the installer.
ISCC packaging\windows\AI-CAD.iss /DPayloadDir=dist\AI-CAD /DAppVersion=<version>
#   -> dist\AI-CAD-Setup-<version>.exe
```

`<version>` should come from `git describe --tags --always` (matching the Linux AppImage's
version stamping in `packaging/linux/build_appimage.sh`) — e.g. in PowerShell:
`$env:AppVersion = (git describe --tags --always).Trim()`.

## Verification checklist (run once on a real Windows box)

- [ ] `dist\AI-CAD-Setup-<version>.exe` runs without error and completes (watch for the
      "Provisioning the AI-CAD runtime" step — this is a real `pip install` of build123d/OCP/VTK
      and can take several minutes; the installer should not appear to hang without a status
      message during this).
- [ ] After install, `%LOCALAPPDATA%\Programs\AI-CAD\AI-CAD.exe` launches and the worker starts
      (no "Python runtime not installed" banner — see `ui/imgui/src/ui/first_run.cpp`'s
      first-run dialog / Settings ▸ Advanced ▸ Runtime for the same check the GUI does).
- [ ] `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe` exists and
      `%APPDATA%\AI-CAD\runtime\.ready.json` has a `req_hash` matching the shipped
      `requirements.txt` (`installer.provision.is_ready()`'s own check).
- [ ] The Start Menu has an "AI-CAD" entry (note: currently duplicated — see the `.iss` file's
      comment; `install.ps1` also makes its own shortcut).
- [ ] Uninstalling via "Apps & Features" removes the Start Menu entries and `%APPDATA%\AI-CAD`
      (via `uninstall.ps1`'s `[UninstallRun]` step), leaving no orphaned MCP client registration
      (`installer.register_mcp --unregister` runs as part of `uninstall.ps1`).
- [ ] `python -m installer.provision --check` inside the installed `app\` reports `"ready": true`.

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

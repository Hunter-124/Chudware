# Chudware installer. Lays down the payload, provisions the runtime into %APPDATA%\Chudware,
# and registers the MCP server with Claude. Run from the unpacked release folder.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-InstallDir <dir>] [-NoRegister]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Chudware",
    [switch]$NoRegister
)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
Write-Host "==> Installing Chudware to $InstallDir"

# ---------------------------------------------------------------------------
# Pre-copy cleanup helpers. Shared shape with uninstall.ps1 (deliberately
# duplicated: uninstall.ps1 is staged on its own by chudware.iss and must keep
# working from {app} with no sibling module to dot-source).
# ---------------------------------------------------------------------------

# Every directory a Chudware install has ever defaulted to, current name and both
# pre-rename names. This matters because instance identity hashes the REALPATH of
# chudware-hostd.exe (services/ipc/ipc_discovery.cpp instanceKey): a daemon left
# running from any other directory is a wholly separate daemon — own pipe, own
# mutex, own tray icon — that this install neither replaces nor stops.
function Get-ChudwareInstallDirs {
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "Programs") }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    if (${env:ProgramFiles(x86)}) { $roots += ${env:ProgramFiles(x86)} }
    $out = @()
    foreach ($r in $roots) {
        foreach ($n in @("Chudware", "Billet", "AI-CAD")) { $out += (Join-Path $r $n) }
    }
    $out | Select-Object -Unique
}

# Best-effort: drain then kill any engine-host daemon whose image lives under $dir.
# --stop --force drains cleanly (and, when the pipe is wedged, escalates to
# TerminateProcess via its pidfile — core/hostd/main.cpp stopViaPidfile); the
# taskkill sweep is the backstop and is scoped by image path, so a dev-tree daemon
# on the same machine is never touched. Never throws: a daemon we cannot stop must
# degrade to a warning, not a failed install.
function Stop-ChudwareDaemon([string]$dir) {
    if (-not $dir) { return }
    $stopped = $false
    foreach ($exe in @("chudware-hostd.exe", "billet-hostd.exe")) {
        $p = Join-Path $dir $exe
        if (-not (Test-Path $p)) { continue }
        Write-Host "==> Stopping engine daemon in $dir ..."
        try { & $p --stop --force 2>$null | Out-Null } catch {}
        $stopped = $true
    }
    if (-not $stopped) { return }
    Start-Sleep -Seconds 2
    foreach ($name in @("chudware-hostd", "billet-hostd")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($dir, [System.StringComparison]::OrdinalIgnoreCase)
        } | ForEach-Object {
            Write-Warning "force-killing lingering daemon pid $($_.Id) ($($_.Path))"
            taskkill /PID $_.Id /F /T 2>$null | Out-Null
        }
    }
}

# Retire pre-rename Start Menu entries and install trees (the Windows counterpart of
# packaging/linux/install_desktop.sh's Billet.desktop/AI-CAD.desktop sweep), so an
# upgraded machine shows exactly one launcher and hosts exactly one daemon binary.
# NOTE: %APPDATA%\Billet and %APPDATA%\AI-CAD are DATA roots, not install dirs, and
# are deliberately left alone — host/src/bootstrap.cpp appdataRoot() still reads them
# as a back-compat fallback and they hold the user's config/chat history.
function Remove-LegacyBranding([string]$keepDir) {
    $programs = [Environment]::GetFolderPath('Programs')
    foreach ($n in @("Billet", "AI-CAD")) {
        if ($programs) {
            Remove-Item (Join-Path $programs "$n.lnk") -Force -ErrorAction SilentlyContinue
            # Inno DefaultGroupName folder from a pre-rename setup.exe.
            Remove-Item (Join-Path $programs $n) -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($d in (Get-ChudwareInstallDirs)) {
        if ($d -eq $keepDir) { continue }
        if (-not (Test-Path $d)) { continue }
        if ((Split-Path $d -Leaf) -eq "Chudware") {
            # A second CURRENT-branded install (e.g. an old machine-wide
            # {autopf}\Chudware from before chudware.iss moved to {localappdata}).
            # Its daemon is already stopped; deleting someone's deliberate
            # machine-wide deployment is not ours to do, so just say so.
            Write-Warning "another Chudware install exists at $d - remove it to avoid two independent daemons"
            continue
        }
        Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $d) { Write-Warning "could not remove legacy install dir $d (in use or needs admin)" }
        else { Write-Host "==> Removed legacy install dir $d" }
    }
}

# Stop daemons from THIS install dir (their binaries are about to be overwritten and
# a live chudware-hostd.exe holds file locks on them) AND from every other known
# install location, since those keep running and re-serving clients otherwise.
foreach ($d in ((@($InstallDir) + @(Get-ChudwareInstallDirs)) | Select-Object -Unique)) {
    Stop-ChudwareDaemon $d
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($item in @("chudware.exe", "chudware-hostd.exe", "chudware-hostd.pdb", "assets", "app", "wheelhouse", "python-embed", "requirements.txt", "llamacpp", "models")) {
    $p = Join-Path $src $item
    if (Test-Path $p) { Copy-Item $p -Destination $InstallDir -Recurse -Force }
}

# Resolve a base interpreter for creating the venv: bundled portable python preferred.
$embed = Join-Path $InstallDir "python-embed\python.exe"
if (Test-Path $embed) {
    $basePy = $embed
} else {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) { $basePy = (& py -3.11 -c "import sys;print(sys.executable)") }
    else { $basePy = (Get-Command python).Source }
}
Write-Host "==> Base python: $basePy"

$app = Join-Path $InstallDir "app"
$wh = Join-Path $InstallDir "wheelhouse"
Push-Location $app
try {
    $whArgs = @()
    if (Test-Path $wh) { $whArgs = @("--wheelhouse", $wh) }
    Write-Host "==> Provisioning runtime into %APPDATA%\Chudware ..."
    $provisionOut = & $basePy -m installer.provision @whArgs --base-python $basePy --with-app-code
    if ($LASTEXITCODE -ne 0) { $provisionOut | ForEach-Object { Write-Host $_ }; throw "provisioning failed" }
    $provisionOut | ForEach-Object { Write-Host $_ }
    # provision.py may legitimately reuse a pre-Chudware-rename venv (services/paths.py's
    # active_root() back-compat) -- read the actual python path it reports rather than
    # assuming the new %APPDATA%\Chudware name, or registration below targets a python.exe
    # that was never created.
    $provisionJson = ($provisionOut -join "`n") | ConvertFrom-Json
    $venvPy = $provisionJson.python
    if (-not $venvPy -or -not (Test-Path $venvPy)) {
        throw "provisioning reported no usable python (got '$venvPy')"
    }
    if (-not $NoRegister) {
        Write-Host "==> Registering Chudware MCP server with Claude ..."
        # Pin CHUDWARE_HOSTD_BIN in the registered server env so MCP sessions and the GUI
        # resolve the SAME installed engine daemon (shared instance key = shared daemon).
        $regArgs = @("--python", $venvPy, "--cwd", $app)
        if (Test-Path $hostdExe) { $regArgs += @("--hostd-bin", $hostdExe) }
        & $venvPy -m installer.register_mcp @regArgs
        # File associations (.step+ default + Open-with for the formats Chudware loads). Per-user
        # (HKCU), idempotent, reversed on uninstall. Stdlib-only, so the base python is fine too.
        Write-Host "==> Registering Chudware file associations ..."
        & $venvPy -m installer.setup_file_assoc --exe (Join-Path $InstallDir "chudware.exe")
    }

    # Optional sim/firmware backends (Renode portable zip, openEMS MSVC note).
    # Non-fatal: a failure here must never abort the install.
    try {
        Write-Host "==> Optional: provisioning Renode + openEMS (best-effort) ..."
        $env:CHUDWARE_PROVISION_OPTIONAL_SIM = "1"
        & $venvPy -c @"
import sys
try:
    from installer.provision import ensure_optional_sim_tools
    st = ensure_optional_sim_tools(install=True)
    for k, v in st.items():
        print(f'  {k}: installed={v.get(\"installed\")} path={v.get(\"path\") or v.get(\"python\")}')
except Exception as e:
    print(f'  optional sim tools skipped: {e}')
    sys.exit(0)
"@
    } catch {
        Write-Warning "optional Renode/openEMS provisioning skipped: $_"
    }
} finally {
    Pop-Location
}

# Start Menu shortcut
try {
    $ws = New-Object -ComObject WScript.Shell
    $programs = [Environment]::GetFolderPath('Programs')
    $lnk = $ws.CreateShortcut((Join-Path $programs "Chudware.lnk"))
    $lnk.TargetPath = Join-Path $InstallDir "chudware.exe"
    $lnk.WorkingDirectory = $InstallDir
    $lnk.Save()
} catch { Write-Warning "shortcut creation skipped: $_" }

# Last, so a failure here can never cost us a working install.
try { Remove-LegacyBranding $InstallDir } catch { Write-Warning "legacy cleanup skipped: $_" }

Write-Host "==> Chudware installed. Launch from the Start Menu or $InstallDir\chudware.exe"

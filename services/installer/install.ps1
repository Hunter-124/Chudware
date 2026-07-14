# AI-CAD installer. Lays down the payload, provisions the runtime into %APPDATA%\AI-CAD,
# and registers the MCP server with Claude. Run from the unpacked release folder.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-InstallDir <dir>] [-NoRegister]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\AI-CAD",
    [switch]$NoRegister
)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
Write-Host "==> Installing AI-CAD to $InstallDir"

# Stop any engine-host daemon that was spawned from THIS install dir before we copy
# over its binaries (a running aicad-hostd.exe would hold locks on the payload files).
# --stop drains cleanly; the scoped taskkill only touches processes whose image path is
# under $InstallDir, so a dev-tree daemon on the same machine is never killed.
$hostdExe = Join-Path $InstallDir "aicad-hostd.exe"
if (Test-Path $hostdExe) {
    Write-Host "==> Stopping engine daemon from previous install ..."
    try { & $hostdExe --stop 2>$null | Out-Null } catch {}
    Start-Sleep -Seconds 2
    Get-Process -Name "aicad-hostd" -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)
    } | ForEach-Object {
        Write-Warning "force-killing lingering daemon pid $($_.Id) ($($_.Path))"
        taskkill /PID $_.Id /F /T 2>$null | Out-Null
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($item in @("AI-CAD.exe", "aicad-hostd.exe", "aicad-hostd.pdb", "assets", "app", "wheelhouse", "python-embed", "requirements.txt", "llamacpp", "models")) {
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
    Write-Host "==> Provisioning runtime into %APPDATA%\AI-CAD ..."
    & $basePy -m installer.provision @whArgs --base-python $basePy --with-app-code
    if ($LASTEXITCODE -ne 0) { throw "provisioning failed" }

    $venvPy = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
    if (-not $NoRegister) {
        Write-Host "==> Registering AI-CAD MCP server with Claude ..."
        # Pin AICAD_HOSTD_BIN in the registered server env so MCP sessions and the GUI
        # resolve the SAME installed engine daemon (shared instance key = shared daemon).
        $regArgs = @("--python", $venvPy, "--cwd", $app)
        if (Test-Path $hostdExe) { $regArgs += @("--hostd-bin", $hostdExe) }
        & $venvPy -m installer.register_mcp @regArgs
        # File associations (.step+ default + Open-with for the formats AI-CAD loads). Per-user
        # (HKCU), idempotent, reversed on uninstall. Stdlib-only, so the base python is fine too.
        Write-Host "==> Registering AI-CAD file associations ..."
        & $venvPy -m installer.setup_file_assoc --exe (Join-Path $InstallDir "AI-CAD.exe")
    }

    # Optional sim/firmware backends (Renode portable zip, openEMS MSVC note).
    # Non-fatal: a failure here must never abort the install.
    try {
        Write-Host "==> Optional: provisioning Renode + openEMS (best-effort) ..."
        $env:AICAD_PROVISION_OPTIONAL_SIM = "1"
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
    $lnk = $ws.CreateShortcut((Join-Path $programs "AI-CAD.lnk"))
    $lnk.TargetPath = Join-Path $InstallDir "AI-CAD.exe"
    $lnk.WorkingDirectory = $InstallDir
    $lnk.Save()
} catch { Write-Warning "shortcut creation skipped: $_" }

Write-Host "==> AI-CAD installed. Launch from the Start Menu or $InstallDir\AI-CAD.exe"

# Chudware uninstaller. Unregisters the MCP server and removes app + runtime.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-InstallDir <dir>] [-KeepRuntime]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Chudware",
    [switch]$KeepRuntime
)
$ErrorActionPreference = "Continue"

# Every directory a Chudware install has ever defaulted to, current name and both
# pre-rename names. Instance identity hashes the REALPATH of chudware-hostd.exe
# (services/ipc/ipc_discovery.cpp instanceKey), so a daemon running from any other
# directory is a wholly separate daemon that this uninstall would otherwise leave
# behind, tray icon and all. Mirrors install.ps1 (duplicated on purpose: this script
# is staged alone into {app} by chudware.iss and has no sibling module to load).
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

# Best-effort drain + kill of any daemon whose image lives under $dir; a live
# chudware-hostd.exe keeps its install dir undeletable. Scoped by image path, so a
# dev-tree daemon is never touched. Never throws.
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
        } | ForEach-Object { taskkill /PID $_.Id /F /T 2>$null | Out-Null }
    }
}

# This install dir FIRST (it must become deletable), then every other known
# location — including the pre-rename ones and the old machine-wide
# {autopf}\Chudware — so no orphan daemon outlives the uninstall.
foreach ($d in ((@($InstallDir) + @(Get-ChudwareInstallDirs)) | Select-Object -Unique)) {
    Stop-ChudwareDaemon $d
}

$venvPy = "$env:APPDATA\Chudware\runtime\venv\Scripts\python.exe"
$app = Join-Path $InstallDir "app"
if ((Test-Path $venvPy) -and (Test-Path $app)) {
    Write-Host "==> Unregistering MCP server ..."
    Push-Location $app
    & $venvPy -m services.installer.register_mcp --unregister
    Write-Host "==> Unregistering file associations ..."
    & $venvPy -m services.installer.setup_file_assoc --unregister
    Pop-Location
}

$programs = [Environment]::GetFolderPath('Programs')
Remove-Item (Join-Path $programs "Chudware.lnk") -Force -ErrorAction SilentlyContinue
# Inno's own [Icons] group folder (DefaultGroupName=Chudware) plus the pre-rename
# Billet/AI-CAD launcher entries — the Windows counterpart of the stale-.desktop
# sweep in packaging/linux/install_desktop.sh.
foreach ($n in @("Chudware", "Billet", "AI-CAD")) {
    Remove-Item (Join-Path $programs "$n.lnk") -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $programs $n) -Recurse -Force -ErrorAction SilentlyContinue
}
# Pre-rename install TREES. Their data roots (%APPDATA%\Billet, %APPDATA%\AI-CAD)
# are NOT touched even with -KeepRuntime off: those hold user config/chat history
# that host/src/bootstrap.cpp appdataRoot() still reads as a back-compat fallback,
# and this uninstaller never installed them.
foreach ($d in (Get-ChudwareInstallDirs)) {
    if ((Split-Path $d -Leaf) -eq "Chudware") { continue }
    Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
if (-not $KeepRuntime) {
    Remove-Item "$env:APPDATA\Chudware" -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "==> Chudware removed."

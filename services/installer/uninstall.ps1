# AI-CAD uninstaller. Unregisters the MCP server and removes app + runtime.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-InstallDir <dir>] [-KeepRuntime]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\AI-CAD",
    [switch]$KeepRuntime
)
$ErrorActionPreference = "Continue"

# Stop the engine-host daemon from THIS install dir FIRST — a running aicad-hostd.exe
# would keep $InstallDir undeletable. Scoped: only kills processes whose image path is
# under $InstallDir (never a dev-tree daemon).
$hostdExe = Join-Path $InstallDir "aicad-hostd.exe"
if (Test-Path $hostdExe) {
    Write-Host "==> Stopping engine daemon ..."
    try { & $hostdExe --stop --force 2>$null | Out-Null } catch {}
    Start-Sleep -Seconds 2
    Get-Process -Name "aicad-hostd" -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)
    } | ForEach-Object { taskkill /PID $_.Id /F /T 2>$null | Out-Null }
}

$venvPy = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
$app = Join-Path $InstallDir "app"
if ((Test-Path $venvPy) -and (Test-Path $app)) {
    Write-Host "==> Unregistering MCP server ..."
    Push-Location $app
    & $venvPy -m installer.register_mcp --unregister
    Write-Host "==> Unregistering file associations ..."
    & $venvPy -m installer.setup_file_assoc --unregister
    Pop-Location
}

$programs = [Environment]::GetFolderPath('Programs')
Remove-Item (Join-Path $programs "AI-CAD.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
if (-not $KeepRuntime) {
    Remove-Item "$env:APPDATA\AI-CAD" -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "==> AI-CAD removed."

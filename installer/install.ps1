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
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($item in @("AI-CAD.exe", "assets", "app", "wheelhouse", "python-embed", "requirements.txt", "llamacpp", "models")) {
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
        & $venvPy -m installer.register_mcp --python $venvPy --cwd $app
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

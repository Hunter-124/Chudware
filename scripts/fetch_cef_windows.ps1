# Download + extract the CEF minimal binary distribution for Windows x64
# (Chromium 149 — matches services/browser/stealth.py cookie interop).
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_cef_windows.ps1
#
# Result: extern\cef\cef_binary_*_windows64_minimal\  (gitignored)
# Then rebuild the host with CEF ON:
#   build.bat host

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dest = Join-Path $Root "extern\cef"
$Version = "149.0.6+g0d0eeb6+chromium-149.0.7827.201"
$FileName = "cef_binary_${Version}_windows64_minimal.tar.bz2"
# Spotify CDN encodes + as %2B
$UrlName = $FileName -replace '\+', '%2B'
$Url = "https://cef-builds.spotifycdn.com/$UrlName"
$Archive = Join-Path $Dest "cef_binary_149.0.6_windows64_minimal.tar.bz2"
$Extracted = Join-Path $Dest "cef_binary_${Version}_windows64_minimal"

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if (Test-Path (Join-Path $Extracted "Release\libcef.dll")) {
    Write-Host "CEF already present: $Extracted"
    exit 0
}

Write-Host "Downloading $Url ..."
curl.exe -L --retry 3 --retry-delay 2 -o $Archive $Url
if (-not (Test-Path $Archive) -or (Get-Item $Archive).Length -lt 1MB) {
    throw "Download failed or too small: $Archive"
}
Write-Host "Extracting ($([math]::Round((Get-Item $Archive).Length/1MB)) MB archive) ..."
python -c @"
import tarfile, pathlib
archive = pathlib.Path(r'$($Archive -replace '\\','/')')
dest = pathlib.Path(r'$($Dest -replace '\\','/')')
with tarfile.open(archive, 'r:bz2') as tf:
    tf.extractall(dest)
print('extracted ok')
"@
if (-not (Test-Path (Join-Path $Extracted "Release\libcef.dll"))) {
    throw "Extract finished but libcef.dll missing under $Extracted"
}
Write-Host "Done. CEF_ROOT=$Extracted"
Write-Host "Rebuild host:  build.bat host"

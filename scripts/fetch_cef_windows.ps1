# Download + extract the CEF minimal binary distribution for Windows x64
# (Chromium 149 — matches services/browser/stealth.py cookie interop).
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_cef_windows.ps1
#
# Result: extern\cef\cef_binary_*_windows64_minimal\  (gitignored)
# Then rebuild the host with CEF ON:
#   build.bat host

param([string]$DestinationRoot = "", [switch]$Offline)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dest = if ($DestinationRoot) { $DestinationRoot } else { Join-Path $Root "extern\cef" }
$Version = "149.0.6+g0d0eeb6+chromium-149.0.7827.201"
$FileName = "cef_binary_${Version}_windows64_minimal.tar.bz2"
# Spotify CDN encodes + as %2B
$UrlName = $FileName -replace '\+', '%2B'
$Url = "https://cef-builds.spotifycdn.com/$UrlName"
$Archive = Join-Path $Dest "cef_binary_149.0.6_windows64_minimal.tar.bz2"
$Extracted = Join-Path $Dest "cef_binary_${Version}_windows64_minimal"
# SHA256 bootstrapped from inert primary HTTPS bytes, matched against the
# primary https://cef-builds.spotifycdn.com/index.json SHA1 before extraction:
# fe8f461b743f03dc640e998ae08264407d8bc2c9. Ongoing verification is SHA256.
$Sha256 = "a8942099a69e46c1d7243068303d21df5cab3bcdea82f55ef1aa07ee99d70ac0"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if (-not (Test-Path $Archive)) {
    if ($Offline) { throw "Authenticated CEF archive missing (offline): $Archive" }
    Write-Host "Downloading $Url ..."
    curl.exe --proto '=https' --proto-redir '=https' -fL --retry 3 --retry-delay 2 -o $Archive $Url
    if ($LASTEXITCODE -ne 0) { throw "CEF download failed ($LASTEXITCODE)" }
}
if ((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant() -ne $Sha256) {
    throw "CEF archive SHA-256 mismatch; refusing extraction: $Archive"
}
# A cached extracted DLL is not provenance. Clear only this managed SDK tree
# and reconstruct it from the authenticated archive, including on repeat runs.
if (Test-Path $Extracted) { Remove-Item -Recurse -Force $Extracted }
Write-Host "Extracting authenticated CEF archive ..."
python -c @"
import tarfile, pathlib
archive = pathlib.Path(r'$($Archive -replace '\\','/')')
dest = pathlib.Path(r'$($Dest -replace '\\','/')')
with tarfile.open(archive, 'r:bz2') as tf:
    tf.extractall(dest)
print('extracted ok')
"@
if ($LASTEXITCODE -ne 0) { throw "Authenticated CEF extraction failed ($LASTEXITCODE)" }
if (-not (Test-Path (Join-Path $Extracted "Release\libcef.dll"))) {
    throw "Extract finished but libcef.dll missing under $Extracted"
}
Write-Host "Done. CEF_ROOT=$Extracted"
Write-Host "Rebuild host:  build.bat host"

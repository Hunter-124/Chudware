# Stage Windows engine + host into dist\Chudware
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stage_windows.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Dist = Join-Path $Root "dist\Chudware"
$CoreCandidates = @(
    (Join-Path $Root "build\windows-occt\bin"),
    (Join-Path $Root "core\build-occt\bin\Release")
)
$HostCandidates = @(
    (Join-Path $Root "host\build-win\bin"),
    (Join-Path $Root "host\build\bin\Release"),
    (Join-Path $Root "host\build\bin")
)

$CoreBin = $null
foreach ($c in $CoreCandidates) {
    if (Test-Path (Join-Path $c "libchudware.dll")) {
        $CoreBin = $c
        break
    }
}
if (-not $CoreBin) {
    throw "libchudware.dll not found. Build windows-occt first."
}

$HostBin = $null
foreach ($h in $HostCandidates) {
    if (Test-Path (Join-Path $h "chudware.exe")) {
        $HostBin = $h
        break
    }
}

New-Item -ItemType Directory -Force -Path $Dist | Out-Null
Write-Host "[stage] core from $CoreBin -> $Dist"
& robocopy $CoreBin $Dist *.dll chudware-hostd.exe chudware-hostd.pdb /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy core failed ($LASTEXITCODE). Close Chudware/MCP and retry."
}

if ($null -ne $HostBin) {
    foreach ($name in @("chudware.exe", "chudware.pdb")) {
        $p = Join-Path $HostBin $name
        if (Test-Path $p) {
            Copy-Item -Force $p $Dist
            Write-Host "[stage] $name"
        }
    }
    Write-Host "[stage] also next to host bin: $HostBin"
    & robocopy $CoreBin $HostBin *.dll chudware-hostd.exe /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
} else {
    Write-Warning "chudware.exe not found - staging engine only"
}

foreach ($pkg in @("engine", "agent", "mcp_server", "installer")) {
    $src = Join-Path $Root $pkg
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $Dist ("app\" + $pkg)
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    & robocopy $src $dst /MIR /XD __pycache__ build samples .git /XF *.pyc /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
}

$req = Join-Path $Root "requirements.txt"
if (Test-Path $req) {
    Copy-Item -Force $req (Join-Path $Dist "requirements.txt")
}
$assets = Join-Path $Root "assets"
if (Test-Path $assets) {
    & robocopy $assets (Join-Path $Dist "assets") /MIR /XD __pycache__ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
}

Write-Host ""
Write-Host "[stage] DONE"
$dll = Join-Path $Dist "libchudware.dll"
$hostd = Join-Path $Dist "chudware-hostd.exe"
$chudware = Join-Path $Dist "chudware.exe"
if (Test-Path $dll) {
    Write-Host ("  libchudware : OK " + (Get-Item $dll).Length)
} else {
    Write-Host "  libchudware : MISSING"
}
if (Test-Path $hostd) { Write-Host "  hostd    : OK" } else { Write-Host "  hostd    : MISSING" }
if (Test-Path $chudware) { Write-Host "  chudware   : OK" } else { Write-Host "  chudware   : MISSING" }
Write-Host ("  Run      : " + $chudware)

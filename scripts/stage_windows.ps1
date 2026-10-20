# Stage Windows engine + host into dist\Chudware.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stage_windows.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Invoke-Robocopy {
    & robocopy @args | Out-Null
    $exitCode = $LASTEXITCODE
    # Robocopy's 0..7 statuses are successful; 8+ (or an abnormal signed exit) fail.
    if ($exitCode -lt 0 -or $exitCode -ge 8) {
        throw "robocopy failed ($exitCode): $($args[0]) -> $($args[1])"
    }
}

Push-Location -LiteralPath $Root
try {
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
        if (Test-Path -LiteralPath (Join-Path $c "libchudware.dll") -PathType Leaf) {
            $CoreBin = $c
            break
        }
    }
    if (-not $CoreBin) { throw "libchudware.dll not found. Build windows-occt first." }
    if (-not (Test-Path -LiteralPath (Join-Path $CoreBin "chudware-hostd.exe") -PathType Leaf)) {
        throw "chudware-hostd.exe not found beside libchudware.dll. Build windows-occt first."
    }

    $HostBin = $null
    foreach ($h in $HostCandidates) {
        if (Test-Path -LiteralPath (Join-Path $h "chudware.exe") -PathType Leaf) {
            $HostBin = $h
            break
        }
    }
    if (-not $HostBin) { throw "chudware.exe not found. Build the host GUI first." }

    $req = Join-Path $Root "requirements.txt"
    if (-not (Test-Path -LiteralPath $req -PathType Leaf)) { throw "runtime declarations missing: $req" }
    $identities = @("vendor/python/RUNTIME_WHEELS.lock", "vendor/chudcode/CHUDCODE_ARTIFACT.lock")
    foreach ($identity in $identities) {
        $source = Join-Path $Root $identity
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "reviewed runtime identity missing: $source" }
    }
    $wheelhouse = Join-Path $Root "wheelhouse"
    if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) { throw "reviewed offline runtime wheelhouse missing: $wheelhouse" }
    $servicesSrc = Join-Path $Root "services"
    if (-not (Test-Path -LiteralPath $servicesSrc -PathType Container)) { throw "runtime services missing: $servicesSrc" }

    New-Item -ItemType Directory -Force -Path $Dist | Out-Null
    Write-Host "[stage] core from $CoreBin -> $Dist"
    Invoke-Robocopy $CoreBin $Dist *.dll chudware-hostd.exe chudware-hostd.pdb /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
    foreach ($name in @("chudware.exe", "chudware.pdb")) {
        $p = Join-Path $HostBin $name
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            Copy-Item -Force -LiteralPath $p -Destination $Dist
            Write-Host "[stage] $name"
        }
    }
    Write-Host "[stage] also next to host bin: $HostBin"
    Invoke-Robocopy $CoreBin $HostBin *.dll chudware-hostd.exe /R:2 /W:1 /NFL /NDL /NJH /NJS /NP

    $App = Join-Path $Dist "app"
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    $engineSrc = Join-Path $Root "engine"
    if (Test-Path -LiteralPath $engineSrc -PathType Container) {
        Invoke-Robocopy $engineSrc (Join-Path $App "engine") /MIR /XD __pycache__ build samples .git /XF *.pyc /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    }
    $servicesDst = Join-Path $App "services"
    New-Item -ItemType Directory -Force -Path $servicesDst | Out-Null
    $servicesInit = Join-Path $servicesSrc "__init__.py"
    if (Test-Path -LiteralPath $servicesInit -PathType Leaf) {
        Copy-Item -Force -LiteralPath $servicesInit -Destination (Join-Path $servicesDst "__init__.py")
    } else {
        New-Item -ItemType File -Force -Path (Join-Path $servicesDst "__init__.py") | Out-Null
    }
    foreach ($pkg in @("agent", "mcp", "installer", "plm", "dag", "browser", "deliverables", "ipc", "serving", "verify", "parts", "reveng", "sim", "vision")) {
        $src = Join-Path $servicesSrc $pkg
        if (-not (Test-Path -LiteralPath $src -PathType Container)) { continue }
        $dst = Join-Path $servicesDst $pkg
        Invoke-Robocopy $src $dst /MIR /XD __pycache__ build samples .git /XF *.pyc test_*.py /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    }
    Get-ChildItem -LiteralPath $servicesSrc -Filter "*.py" -File | Where-Object {
        $_.Name -ne "__init__.py" -and -not $_.Name.StartsWith("test_")
    } | ForEach-Object {
        Copy-Item -Force -LiteralPath $_.FullName -Destination (Join-Path $servicesDst $_.Name)
    }

    Copy-Item -Force -LiteralPath $req -Destination (Join-Path $Dist "requirements.txt")
    Copy-Item -Force -LiteralPath $req -Destination (Join-Path $App "requirements.txt")
    foreach ($identity in $identities) {
        $source = Join-Path $Root $identity
        $destination = Join-Path $App $identity
        New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
        Copy-Item -Force -LiteralPath $source -Destination $destination
    }
    Invoke-Robocopy $wheelhouse (Join-Path $Dist "wheelhouse") /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    $assets = Join-Path $Root "assets"
    if (Test-Path -LiteralPath $assets -PathType Container) {
        Invoke-Robocopy $assets (Join-Path $Dist "assets") /MIR /XD __pycache__ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    }

    foreach ($name in @("libchudware.dll", "chudware-hostd.exe", "chudware.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dist $name) -PathType Leaf)) {
            throw "staged runtime file missing: $name"
        }
    }
    Write-Host ""
    Write-Host "[stage] DONE"
    Write-Host ("  Run : " + (Join-Path $Dist "chudware.exe"))
} finally {
    Pop-Location
}

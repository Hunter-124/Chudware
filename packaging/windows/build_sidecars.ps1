# Build protected production sidecars into a builder-owned output directory. Configure host and
# hostd with -DCHUDWARE_DEVELOPER_BUILD=OFF first, and generate protected assets under
# host/build-win/generated/protected-assets. Pass outputs via --worker-bin/--mcp-bin/
# --omp-bridge-bin before hashing the immutable Windows payload.
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,
    [string]$Python = $env:CHUDWARE_RELEASE_PYTHON,
    [string]$HostBinary = "",
    [string]$HostdBinary = ""
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
$Entry = Join-Path $Root "services\compiled_entry.py"
$ReleaseRequirements = Join-Path $Root "packaging\requirements-release.txt"
$ProtectedAssetsOut = Join-Path $Root "host\build-win\generated\protected-assets"
if (-not $HostBinary) { $HostBinary = Join-Path $Root "host\build-win\bin\chudware.exe" }
if (-not $HostdBinary) { $HostdBinary = Join-Path $Root "build\windows-occt\bin\chudware-hostd.exe" }

if (-not (Test-Path -LiteralPath $Entry -PathType Leaf)) {
    throw "services/compiled_entry.py is required for release sidecars"
}
if (-not (Test-Path -LiteralPath $OutputDir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
}
if (-not $Python) {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $Python = $venvPython
    } else {
        $Python = (Get-Command python.exe -ErrorAction Stop).Source
    }
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

function Assert-ProductionCache([string]$BuildDir, [string]$Label) {
    $resolved = [IO.Path]::GetFullPath($BuildDir)
    $cache = Join-Path $resolved "CMakeCache.txt"
    if (-not (Test-Path -LiteralPath $cache -PathType Leaf)) {
        throw "$Label has no CMakeCache.txt: $cache"
    }
    $matches = @(Select-String -LiteralPath $cache -SimpleMatch "CHUDWARE_DEVELOPER_BUILD:BOOL=OFF")
    if ($matches.Count -ne 1 -or $matches[0].Line -ne "CHUDWARE_DEVELOPER_BUILD:BOOL=OFF") {
        throw "$Label was not explicitly configured with CHUDWARE_DEVELOPER_BUILD=OFF"
    }
}

function Get-VerifiedBinaryBuildDir([string]$Binary, [string]$Label) {
    $resolved = [IO.Path]::GetFullPath($Binary)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label binary is missing: $resolved"
    }
    $buildDir = [IO.Path]::GetFullPath((Join-Path (Split-Path $resolved -Parent) ".."))
    Assert-ProductionCache $buildDir $Label
    return $buildDir
}

function Build-OmpBridge([string]$Destination) {
    $bun = (Get-Command bun.exe -ErrorAction Stop).Source
    $workspace = Join-Path $Root "vendor\omp"
    Push-Location $workspace
    try {
        Write-Host "==> installing locked vendored OMP workspace"
        Invoke-Checked $bun @("install", "--frozen-lockfile")
        Write-Host "==> compiling chudware-omp-bridge.exe"
        Invoke-Checked $bun @(
            "build", "--compile", "--sourcemap=none", "--packages=bundle", "--reject-unresolved",
            "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
            "--no-compile-autoload-package-json", "--windows-hide-console",
            "bridge/src/bridge.ts", "--outfile", $Destination
        )
    } finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "Bun did not produce $Destination"
    }
}

function Generate-ProtectedAssets {
    Write-Host "==> generating protected prompt and role tables"
    Invoke-Checked $Python @(
        (Join-Path $Root "packaging\embed_protected_assets.py"),
        "--source-root", $Root,
        "--output-dir", $ProtectedAssetsOut
    )
    foreach ($required in @(
        (Join-Path $ProtectedAssetsOut "python\chudware_protected_assets.py"),
        (Join-Path $ProtectedAssetsOut "include\chudware\protected_assets.generated.h")
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "protected asset generator did not produce $required"
        }
    }
}

function Build-PythonSidecar([string]$Module, [string]$Name) {
    Invoke-Checked $Python @("-c", "import nuitka")
    $work = Join-Path $Root "build\release-sidecars\windows\$Name"
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    $includes = @("--include-module=chudware_protected_assets", "--include-module=services.$Module")
    if ($Module -eq "worker_main") {
        $includes += @(
            "--include-module=services.parts.vendor_parts",
            "--include-module=services.deliverables.worker_rpc",
            "--include-module=services.parts.worker_rpc",
            "--include-module=services.agent.worker_rpc",
            "--include-module=services.reveng.worker_rpc",
            "--include-module=services.dag.worker_rpc",
            "--include-module=services.agent.subagent_rpc",
            "--include-module=services.plm.worker_rpc",
            "--include-module=services.sim.em.worker_rpc",
            "--include-module=services.sim.multiphysics_rpc",
            "--include-module=services.manufacturing.printers.worker_rpc"
        )
    }

    $arguments = @(
        "-m", "nuitka", "--mode=onefile", "--remove-output",
        "--output-dir=$work", "--output-filename=$Name.exe",
        "--nofollow-import-to=*.tests", "--nofollow-import-to=*.test_*"
    ) + $includes + @($Entry)
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = (Join-Path $ProtectedAssetsOut "python") + $(if ($oldPythonPath) { ";$oldPythonPath" } else { "" })
    Push-Location $Root
    try {
        Write-Host "==> compiling $Name.exe"
        Invoke-Checked $Python $arguments
    } finally {
        Pop-Location
        $env:PYTHONPATH = $oldPythonPath
    }

    $built = Join-Path $work "$Name.exe"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
        throw "Nuitka did not produce $built"
    }
    Copy-Item -LiteralPath $built -Destination (Join-Path $OutputDir "$Name.exe") -Force
    Remove-Item -LiteralPath $work -Recurse -Force
}

function Assert-SourceFreePayload([string]$RootPath) {
    $forbiddenExtensions = @(".py", ".pyc", ".pyo", ".ts", ".tsx", ".js", ".jsx", ".map", ".pdb")
    $forbiddenDirectories = @("__pycache__", "node_modules", ".git", ".github", ".svn")
    foreach ($item in (Get-ChildItem -LiteralPath $RootPath -Recurse -Force)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "reparse point survived production staging: $($item.FullName)"
        }
        if ($item.PSIsContainer) {
            if ($forbiddenDirectories -contains $item.Name -or $item.Name.EndsWith(".dSYM", [StringComparison]::OrdinalIgnoreCase)) {
                throw "repository/debug directory survived production staging: $($item.FullName)"
            }
            continue
        }
        if (($forbiddenExtensions -contains $item.Extension.ToLowerInvariant()) -or
            $item.Name.StartsWith("test_", [StringComparison]::OrdinalIgnoreCase) -or
            $item.Name -match "_test\.[^.]+$") {
            throw "source/test/debug artifact survived production staging: $($item.FullName)"
        }
    }
}

$HostBuildDir = Get-VerifiedBinaryBuildDir $HostBinary "host GUI"
$HostdBuildDir = Get-VerifiedBinaryBuildDir $HostdBinary "chudware-hostd"

Generate-ProtectedAssets
Build-PythonSidecar "worker_main" "chudware-worker"
Build-PythonSidecar "mcp.server" "chudware-mcp"
Build-OmpBridge (Join-Path $OutputDir "chudware-omp-bridge.exe")
Assert-SourceFreePayload $OutputDir

Write-Host "==> built compiled release sidecars in $OutputDir"
Write-Host "==> verified host input: $HostBinary ($HostBuildDir)"
Write-Host "==> verified hostd input: $HostdBinary ($HostdBuildDir)"
Write-Host "==> release compiler pins: $ReleaseRequirements"

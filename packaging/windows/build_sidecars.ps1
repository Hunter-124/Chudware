# Build protected production sidecars into a builder-owned output directory. Configure host and
# hostd with -DCHUDWARE_DEVELOPER_BUILD=OFF first, and generate protected assets under
# host/build-win/generated/protected-assets. Pass outputs via --worker-bin/--mcp-bin/
# --chud-bin before hashing the immutable Windows payload.
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,
    [string]$Python = $env:CHUDWARE_RELEASE_PYTHON,
    [string]$HostBinary = "",
    [string]$HostdBinary = "",
    # Engine library whose advisor closure is carried into the GUI runtime root. Defaults to
    # the first libchudware.dll found beside the host/hostd binaries or in the core build trees.
    [string]$EngineBinary = "",
    [string]$ChudBinary = $env:CHUDWARE_CHUD_BIN
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

function Stage-Chudcode([string]$Destination) {
    if (-not $ChudBinary) {
        throw "CHUDWARE_CHUD_BIN or -ChudBinary is required"
    }
    Invoke-Checked $Python @(
        (Join-Path $Root "scripts\pin_chudcode.py"),
        "stage",
        "--target", "windows-x86_64",
        "--input", $ChudBinary,
        "--output", $Destination
    )
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

    # --assume-yes-for-downloads: on Windows Nuitka refuses standalone/onefile
    # mode without Dependency Walker and asks interactively for permission to
    # fetch it. A CI shell answers "no (default non-interactive)" and the
    # compile dies. The tool is cached under LOCALAPPDATA after the first run.
    $arguments = @(
        "-m", "nuitka", "--mode=onefile", "--remove-output",
        "--assume-yes-for-downloads",
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

# Learned mesh-advisor runtime (the polymesh ONNX advisor).
#
# core/sim/mesher.cpp resolves the package relative to libchudware.dll. The flat Windows
# payload therefore keeps the four runtime artifacts beside the engine.
#
# The engine's own import table decides whether anything ships. An engine configured
# without the advisor imports no onnxruntime.dll and needs no package, so such a build
# stages nothing and stays valid. An advisor-linked engine whose package is incomplete would
# ship a permanently dead optimizer that silently falls back on every study, so that case
# fails here — before the manifest is hashed — instead of reaching a customer.
$AdvisorPackageDir = "polymesh-advisor"
$AdvisorRequiredFiles = @("model.onnx", "normalization.json", "clamps.json", "ood.json")

function Test-ImportsOnnxRuntime([string]$Binary) {
    # The import directory stores the DLL name as plain ASCII, so a byte scan answers "was
    # the advisor compiled in?" without dumpbin, which would drag a Visual Studio
    # environment into a script that otherwise needs none. ISO-8859-1 maps every byte to one
    # char, so IndexOf over the decoded image is an exact byte search.
    $image = [IO.File]::ReadAllText($Binary, [Text.Encoding]::GetEncoding(28591))
    return $image.IndexOf("onnxruntime.dll", [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Resolve-EngineBinary {
    if ($EngineBinary) {
        if (-not (Test-Path -LiteralPath $EngineBinary -PathType Leaf)) {
            throw "engine library not found: $EngineBinary"
        }
        return [IO.Path]::GetFullPath($EngineBinary)
    }
    $candidates = @(
        (Join-Path (Split-Path -Parent $HostBinary) "libchudware.dll"),
        (Join-Path (Split-Path -Parent $HostdBinary) "libchudware.dll"),
        (Join-Path $Root "build\windows-occt\bin\libchudware.dll"),
        (Join-Path $Root "build\windows-core\bin\libchudware.dll"),
        (Join-Path $Root "core\build\bin\Release\libchudware.dll")
    )
    $found = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $found) {
        throw ("libchudware.dll not found; the Windows release payload would have no engine. Searched: " +
            ($candidates -join "; ") + ". Build the windows-occt preset, or pass -EngineBinary.")
    }
    return [IO.Path]::GetFullPath($found)
}

function Stage-AdvisorRuntime([string]$Engine, [string]$RuntimeRoot) {
    $engineDir = Split-Path -Parent $Engine
    if (-not (Test-ImportsOnnxRuntime $Engine)) {
        Write-Host "==> engine imports no onnxruntime.dll: mesh advisor disabled, no advisor payload to stage"
        return
    }
    $ortSource = Join-Path $engineDir "onnxruntime.dll"
    if (-not (Test-Path -LiteralPath $ortSource -PathType Leaf)) {
        throw ("$Engine imports onnxruntime.dll but none is staged beside it in $engineDir. " +
            "Rebuild the engine so CMake stages the ONNX Runtime; the advisor cannot load without it.")
    }
    $sourcePackage = Join-Path $engineDir $AdvisorPackageDir
    foreach ($artifact in $AdvisorRequiredFiles) {
        $path = Join-Path $sourcePackage $artifact
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw ("$Engine imports onnxruntime.dll but its advisor package is incomplete: missing $path. " +
                "Rebuild the engine so CMake stages the advisor package; refusing to hash a payload whose optimizer is dead.")
        }
    }
    $runtimeRootFull = [IO.Path]::GetFullPath($RuntimeRoot)
    $destinationPackage = Join-Path $runtimeRootFull $AdvisorPackageDir
    New-Item -ItemType Directory -Force -Path $destinationPackage | Out-Null
    foreach ($artifact in $AdvisorRequiredFiles) {
        $source = Join-Path $sourcePackage $artifact
        $destination = Join-Path $destinationPackage $artifact
        # The engine build tree and the GUI runtime root are the same directory in some
        # layouts; Copy-Item onto itself is an error, and the file is already in place.
        if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($destination)) {
            Copy-Item -LiteralPath $source -Destination $destination -Force
        }
    }
    $ortDestination = Join-Path $runtimeRootFull "onnxruntime.dll"
    if ([IO.Path]::GetFullPath($ortSource) -ne [IO.Path]::GetFullPath($ortDestination)) {
        Copy-Item -LiteralPath $ortSource -Destination $ortDestination -Force
    }
    Write-Host "==> staged advisor package ($($AdvisorRequiredFiles.Count) artifacts) + onnxruntime.dll into $runtimeRootFull"
}

$HostBuildDir = Get-VerifiedBinaryBuildDir $HostBinary "host GUI"
$HostdBuildDir = Get-VerifiedBinaryBuildDir $HostdBinary "chudware-hostd"

# Advisor closure first: it is a cheap check that fails packaging before the multi-minute
# Nuitka sidecar builds, and its output must be in the runtime root before
# services/installer/build_release.py copies that tree into the hashed payload.
$Engine = Resolve-EngineBinary
Stage-AdvisorRuntime $Engine (Split-Path -Parent ([IO.Path]::GetFullPath($HostBinary)))

Generate-ProtectedAssets
Build-PythonSidecar "worker_main" "chudware-worker"
Build-PythonSidecar "mcp.server" "chudware-mcp"
Stage-Chudcode (Join-Path $OutputDir "chud.exe")
Assert-SourceFreePayload $OutputDir

Write-Host "==> built compiled release sidecars in $OutputDir"
Write-Host "==> verified host input: $HostBinary ($HostBuildDir)"
Write-Host "==> verified hostd input: $HostdBinary ($HostdBuildDir)"
Write-Host "==> release compiler pins: $ReleaseRequirements"
Write-Host "==> verified engine input: $Engine"

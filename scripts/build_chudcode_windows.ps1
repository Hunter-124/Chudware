# Windows PowerShell 5.1, ASCII only. Build the CAD-locked source archive;
# no publish or runner-service changes. See workflow_dispatch for acquisition.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceArchive,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SourceArchiveSha256
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$cad = Split-Path $PSScriptRoot -Parent
$lockPath = Join-Path $cad 'vendor\chudcode\CHUDCODE_ARTIFACT.lock'
$lock = Get-Content -Raw $lockPath | ConvertFrom-Json
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) { throw 'Windows x64 is required' }
if ($lock.repository -ne 'https://github.com/Hunter-124/chudcode.git' -or
    $lock.repository_commit -notmatch '^[0-9a-f]{40}$') { throw 'Unexpected locked source identity' }
$tools = Join-Path $env:USERPROFILE 'chudware-ci-tools'
foreach ($dir in @((Join-Path $tools 'bin'), (Join-Path $tools 'python'),
                   (Join-Path $tools 'python\Scripts'))) {
    if (Test-Path $dir) { $env:PATH = "$dir;$env:PATH" }
}
if ($env:GITHUB_ACTIONS -eq 'true' -and
    (-not $env:CHUDWARE_BOOTSTRAP_BIN -or -not $env:CARGO_HOME -or -not $env:RUSTUP_HOME)) {
    throw 'CI requires authenticated workflow Bun and isolated Rust homes'
}
if ($env:CARGO_HOME) { $env:PATH = "$(Join-Path $env:CARGO_HOME 'bin');$env:PATH" }
if ($env:CHUDWARE_BOOTSTRAP_BIN) { $env:PATH = "$env:CHUDWARE_BOOTSTRAP_BIN;$env:PATH" }
foreach ($tool in @('bun', 'rustup', 'cargo', 'python', 'tar.exe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required build dependency missing: $tool" }
}
if ($env:GITHUB_ACTIONS -eq 'true') {
    if ($env:CHUDWARE_BUN_BINARY_SHA256 -notmatch '^[0-9a-f]{64}$' -or
        (Get-FileHash -Algorithm SHA256 (Get-Command bun).Source).Hash.ToLowerInvariant() -ne $env:CHUDWARE_BUN_BINARY_SHA256) {
        throw 'Bun binary changed after authenticated bootstrap'
    }
    foreach ($proxy in @('rustup', 'cargo')) {
        if ((Get-FileHash -Algorithm SHA256 (Get-Command $proxy).Source).Hash.ToLowerInvariant() -ne
            '88d8258dcf6ae4f7a80c7d1088e1f36fa7025a1cfd1343731b4ee6f385121fc0') {
            throw "Rust bootstrap proxy changed after authentication: $proxy"
        }
    }
}
& python -c 'import sys; assert sys.version_info >= (3, 11)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
$bunVersion = ((& bun --version) | Select-Object -First 1).ToString().Trim()
if ($bunVersion -ne '1.4.0') {
    throw "Build requires Bun 1.4.0, matching pinned runtime CI (found '$bunVersion' at $((Get-Command bun).Source))"
}
$actualSourceSha = (Get-FileHash -Algorithm SHA256 $SourceArchive).Hash.ToLowerInvariant()
if ($actualSourceSha -ne $SourceArchiveSha256) { throw 'Source archive checksum mismatch' }
$archiveRoot = "Hunter-124-chudcode-$($lock.repository_commit)"
$entries = @(& tar.exe -tzf $SourceArchive)
if ($LASTEXITCODE -ne 0 -or $entries.Count -eq 0) { throw 'Cannot list source archive' }
foreach ($entry in $entries) {
    if (-not $entry.StartsWith("$archiveRoot/", [StringComparison]::Ordinal) -or
        $entry -match '(^|/)\.\.(/|$)' -or $entry.Contains('\')) {
        throw "Archive entry does not match exact locked commit: $entry"
    }
}
$run = "$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"
if ($run -eq '-') { $run = [Guid]::NewGuid().ToString('N') }
$sourceParent = Join-Path $env:RUNNER_TEMP "chudcode-source-$run"
New-Item -ItemType Directory -Path $sourceParent | Out-Null
& tar.exe -xzf $SourceArchive -C $sourceParent
if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed' }
$source = Join-Path $sourceParent $archiveRoot
$package = Get-Content -Raw (Join-Path $source 'packages\coding-agent\package.json') | ConvertFrom-Json
if ($package.version -ne $lock.engine_version) { throw 'Locked engine version differs from source package' }

# Initialize MSVC and Windows SDK for Cargo and CMake without touching services.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) { throw 'Visual Studio Build Tools with Desktop C++ and Windows SDK are required' }
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'MSVC x64 build tools were not found' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
$command = '"' + $vcvars + '" >nul && set'
$developerEnvironment = & $env:COMSPEC /d /s /c $command
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize MSVC environment' }
foreach ($line in $developerEnvironment) {
    $split = $line.IndexOf('=')
    if ($split -gt 0) { [Environment]::SetEnvironmentVariable($line.Substring(0, $split), $line.Substring($split + 1), 'Process') }
}
$cmakeRoot = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake'
foreach ($dir in @((Join-Path $cmakeRoot 'CMake\bin'), (Join-Path $cmakeRoot 'Ninja'))) {
    if (Test-Path $dir) { $env:PATH = "$dir;$env:PATH" }
}
foreach ($tool in @('cl.exe', 'link.exe', 'cmake', 'ninja')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required native dependency missing: $tool" }
}
$toolchainText = Get-Content -Raw (Join-Path $source 'rust-toolchain.toml')
if ($toolchainText -notmatch 'channel\s*=\s*"(nightly-\d{4}-\d{2}-\d{2})"') { throw 'Source has no pinned Rust nightly' }
$toolchain = $Matches[1]
& rustup run $toolchain rustc --version
if ($LASTEXITCODE -ne 0) { throw 'Pinned Rust toolchain is missing; run the workflow Rust setup step first' }
$env:RUSTFLAGS = '-C target-feature=+crt-static -C target-cpu=x86-64-v2'
$env:PCRE2_SYS_STATIC = '1'
$env:CARGO_TARGET_DIR = Join-Path $tools 'chudcode-build-target'
Push-Location $source
try {
    & bun install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'Locked Bun dependency installation failed' }
    # Windows PowerShell 5.1 strips embedded double quotes from native-command
    # arguments, so `bun -e '...import "..."...'` arrives unparsable; run a file.
    $probe = Join-Path $source 'chudware-version-probe.ts'
    Set-Content -Path $probe -Encoding ascii -Value 'import { PRODUCT_VERSION } from "./packages/coding-agent/src/brand.ts"; process.stdout.write(PRODUCT_VERSION);'
    $version = & bun $probe
    $probeExit = $LASTEXITCODE
    Remove-Item $probe
    if ($probeExit -ne 0 -or $version -ne "Chudcode v$($lock.version)") { throw "Locked product version differs from source brand (got '$version')" }
    & cargo "+$toolchain" build --locked --profile ci --target x86_64-pc-windows-msvc -p pi-natives
    if ($LASTEXITCODE -ne 0) { throw 'Windows native addon build failed' }
    $addon = Join-Path $source 'packages\natives\native\pi_natives.win32-x64-baseline.node'
    New-Item -ItemType Directory -Path (Split-Path $addon -Parent) -Force | Out-Null
    Copy-Item (Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc\ci\pi_natives.dll') $addon
    & bun scripts/stamp-native-version.ts $addon
    if ($LASTEXITCODE -ne 0) { throw 'Native release stamping failed' }
    # The upstream Bun.build compile target is bun-windows-x64-baseline. This
    # is the packed equivalent of --compile --target=bun-windows-x64, preserving
    # bytecode, docs, compatibility/JSON plugins, native tarball and stats assets.
    & bun scripts/ci-release-build-binaries.ts --targets win32-x64
    if ($LASTEXITCODE -ne 0) { throw 'Packed Windows CLI compilation failed' }
} finally {
    Pop-Location
}
$retention = $env:CHUDCODE_RETENTION_DIRECTORY
if (-not $retention) { $retention = Join-Path $tools 'chudcode-builds' }
$retained = Join-Path $retention "$($lock.repository_commit)-$run"
New-Item -ItemType Directory -Path $retained -Force | Out-Null
$binary = Join-Path $retained 'chud.exe'
Copy-Item (Join-Path $source 'packages\coding-agent\binaries\chudcode-windows-x64.exe') $binary
$smoke = & python (Join-Path $PSScriptRoot 'smoke_chudcode_runtime.py') --binary $binary --version $lock.version
if ($LASTEXITCODE -ne 0) { throw 'Native Windows runtime/assets/RPC smoke failed' }
$evidence = $smoke | ConvertFrom-Json
$digest = (Get-FileHash -Algorithm SHA256 $binary).Hash.ToLowerInvariant()
if ($digest -ne $evidence.sha256) { throw 'Runtime digest changed after smoke' }
# Exercise CAD's exact verify contract against a prospective pin, never alter
# the tracked lock or invent a digest before the binary exists.
# pin_chudcode accepts only canonical JSON (sorted keys, compact, trailing LF); a
# ConvertTo-Json rendering is rejected, so the prospective lock is written by Python.
$prospectiveLock = Join-Path $retained 'CHUDCODE_ARTIFACT.lock'
$windowsUrl = "https://github.com/$env:GITHUB_REPOSITORY/releases/download/chudcode-runtime-v$($lock.version)/chudcode-windows-x64.exe"
$writer = Join-Path $retained 'write_prospective_lock.py'
Set-Content -Path $writer -Encoding ascii -Value @'
import json, sys
src, dst, sha, url = sys.argv[1:5]
with open(src, encoding="ascii") as f:
    lock = json.load(f)
lock["artifacts"]["windows-x86_64"].update(sha256=sha, url=url)
data = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
with open(dst, "w", encoding="ascii", newline="\n") as f:
    f.write(data)
'@
& python $writer $lockPath $prospectiveLock $digest $windowsUrl
if ($LASTEXITCODE -ne 0) { throw 'Cannot write prospective CAD pin' }
Remove-Item $writer
& python (Join-Path $PSScriptRoot 'pin_chudcode.py') --lock $prospectiveLock verify --target windows-x86_64 --input $binary
if ($LASTEXITCODE -ne 0) { throw 'CAD prospective Windows pin verification failed' }
$cache = Join-Path (Join-Path $tools 'chudcode') $digest
New-Item -ItemType Directory -Path $cache -Force | Out-Null
Copy-Item $binary (Join-Path $cache 'chud.exe') -Force
Copy-Item $binary (Join-Path $retained 'chudcode-windows-x64.exe')
"$digest  chudcode-windows-x64.exe" | Set-Content -Encoding Ascii (Join-Path $retained 'SHA256SUMS.txt')
$evidence | Add-Member -NotePropertyName repository_commit -NotePropertyValue $lock.repository_commit
$evidence | Add-Member -NotePropertyName source_archive_sha256 -NotePropertyValue $SourceArchiveSha256
$evidence | ConvertTo-Json -Depth 10 | Set-Content -Encoding Ascii (Join-Path $retained 'build-evidence.json')
Write-Host "version=$($evidence.version_output) sha256=$digest bytes=$($evidence.bytes) retained=$retained cache=$cache"
if ($env:GITHUB_OUTPUT) {
    "directory=$retained", "sha256=$digest", "version=$($lock.version)" | Out-File $env:GITHUB_OUTPUT -Encoding utf8 -Append
}
if ($env:GITHUB_STEP_SUMMARY) {
    @('## Locked Windows Chudcode build', '', "Version: $($evidence.version_output)",
      "Commit: $($lock.repository_commit)", "Source SHA-256: $SourceArchiveSha256", "Binary SHA-256: $digest",
      "Bytes: $($evidence.bytes)", "Retained directory: $retained", "Digest cache: $cache\chud.exe",
      'Native assets/workers smoke, RPC v2 negotiation/discovery/host-tool registration, and prospective CAD pin verification passed.',
      'No provider turn. No publication from this build job.') | Out-File $env:GITHUB_STEP_SUMMARY -Encoding utf8 -Append
}

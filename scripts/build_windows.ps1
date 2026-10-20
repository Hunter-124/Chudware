# One-shot Windows full build: OCCT core + host GUI + stage + lite MCP.
# Requires VS 2022/2026 C++ tools (vcvars64) + Ninja on PATH (or WinGet Links).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$originalEnvironment = [Environment]::GetEnvironmentVariables('Process')
$originalOutputEncoding = [Console]::OutputEncoding

function Invoke-Native {
    $program, $nativeArgs = $args
    & $program @nativeArgs
    if ($LASTEXITCODE -ne 0) { throw "$program failed with exit code $LASTEXITCODE" }
}

Push-Location -LiteralPath $Root
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
    Write-Host "=== junctions ==="
    Invoke-Native (Join-Path $PSHOME "powershell.exe") -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\setup_windows_links.ps1")

    # Reuse the native runtime builder's vswhere/environment-import pattern.
    # No shared temporary batch file, ASCII path conversion or cmd-expanded repo path.
    $vs = $null
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath -utf8
        if ($LASTEXITCODE -ne 0) { throw "Visual Studio discovery failed ($LASTEXITCODE)" }
    }
    if (-not $vs) {
        $vsCandidates = @(
            "C:\Program Files\Microsoft Visual Studio\18\Community",
            "C:\Program Files\Microsoft Visual Studio\2022\Community",
            "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
        )
        $vs = $vsCandidates | Where-Object {
            Test-Path -LiteralPath (Join-Path $_ "VC\Auxiliary\Build\vcvars64.bat") -PathType Leaf
        } | Select-Object -First 1
    }
    if (-not $vs) { throw "Visual Studio C++ workload not found" }
    $vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path -LiteralPath $vcvars -PathType Leaf)) { throw "vcvars64.bat not found: $vcvars" }
    $command = '"' + $vcvars + '" >nul && set'
    # cmd /u emits the environment in UTF-16 even for Unicode user/install paths.
    # Decode its redirected output explicitly instead of relying on the console code page.
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $env:COMSPEC
    $start.Arguments = '/u /d /s /c "' + $command + '"'
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.StandardOutputEncoding = [Text.Encoding]::Unicode
    $developerProcess = [Diagnostics.Process]::Start($start)
    try {
        $developerEnvironment = $developerProcess.StandardOutput.ReadToEnd()
        $developerProcess.WaitForExit()
        if ($developerProcess.ExitCode -ne 0) { throw "Could not initialize MSVC environment ($($developerProcess.ExitCode))" }
    } finally {
        $developerProcess.Dispose()
    }
    foreach ($line in ($developerEnvironment -split '\r?\n')) {
        $split = $line.IndexOf('=')
        if ($split -gt 0) {
            [Environment]::SetEnvironmentVariable($line.Substring(0, $split), $line.Substring($split + 1), 'Process')
        }
    }
    $cmakeRoot = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake"
    foreach ($dir in @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"),
        "C:\Program Files\CMake\bin",
        (Join-Path $cmakeRoot "CMake\bin"),
        (Join-Path $cmakeRoot "Ninja")
    )) {
        if (Test-Path -LiteralPath $dir -PathType Container) { $env:PATH = "$dir;$env:PATH" }
    }
    foreach ($tool in @("cl.exe", "link.exe", "cmake", "ninja")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required native dependency missing: $tool" }
    }
    $cmake = (Get-Command cmake).Source

    Write-Host "=== windows-occt ==="
    Invoke-Native $cmake --preset windows-occt
    Invoke-Native $cmake --build --preset windows-occt --parallel
    Write-Host "=== host GUI ==="
    Invoke-Native $cmake -S host -B host\build-win -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_LTO=ON -DCHUDWARE_ENABLE_CEF=OFF
    Invoke-Native $cmake --build host\build-win --parallel

    Write-Host "=== stage ==="
    Invoke-Native (Join-Path $PSHOME "powershell.exe") -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stage_windows.ps1")

    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $py -PathType Leaf) {
        Write-Host "=== lite MCP re-register ==="
        Invoke-Native $py (Join-Path $Root "services\installer\setup_mcp_clients.py") --python $py --repo $Root --profile lite
    }

    Write-Host ""
    Write-Host "DONE. Run: dist\Chudware\chudware.exe"
    Write-Host "  or:    host\build-win\bin\chudware.exe"
    if (Test-Path -LiteralPath $py -PathType Leaf) {
        Write-Host "MCP: restart Grok / Claude Code / OpenCode to pick up chudware (lite)."
    }
} finally {
    Pop-Location
    [Console]::OutputEncoding = $originalOutputEncoding
    foreach ($name in [Environment]::GetEnvironmentVariables('Process').Keys) {
        if (-not $originalEnvironment.Contains($name)) {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
    }
    foreach ($name in $originalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
}

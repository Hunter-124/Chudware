# One-shot Windows full build: OCCT core + host GUI + stage + lite MCP.
# Requires VS 2022/2026 C++ tools (vcvars64) + Ninja on PATH (or WinGet Links).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$vcvarsCandidates = @(
    "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
)
$vcvars = $vcvarsCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $vcvars) { throw "vcvars64.bat not found — install VS C++ workload" }

$cmakeCandidates = @(
    "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
    "C:\Program Files\CMake\bin\cmake.exe"
)
$cmake = $cmakeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cmake) {
    $cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
}
if (-not $cmake) { throw "cmake not found" }

Write-Host "=== junctions ==="
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\setup_windows_links.ps1")

$bat = @"
@echo off
call "$vcvars" || exit /b 1
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
cd /d "$Root"
echo === windows-occt ===
"$cmake" --preset windows-occt || exit /b 1
"$cmake" --build --preset windows-occt --parallel || exit /b 1
echo === host GUI ===
"$cmake" -S host -B host\build-win -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_ENABLE_CEF=OFF || exit /b 1
"$cmake" --build host\build-win --parallel || exit /b 1
exit /b 0
"@
$tmp = Join-Path $env:TEMP "chudware_build_windows.bat"
Set-Content -Path $tmp -Value $bat -Encoding ASCII
Write-Host "=== build (core+host) ==="
& cmd /c $tmp
if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }

Write-Host "=== stage ==="
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stage_windows.ps1")

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $py) {
    Write-Host "=== lite MCP re-register ==="
    & $py (Join-Path $Root "installer\setup_mcp_clients.py") --python $py --repo $Root --profile lite
}

Write-Host ""
Write-Host "DONE. Run: dist\Chudware\chudware.exe"
Write-Host "  or:    host\build-win\bin\chudware.exe"
Write-Host "MCP: restart Grok / Claude Code / OpenCode to pick up chudware (lite)."

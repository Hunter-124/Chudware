@echo off
setlocal
rem ===========================================================================
rem  AI-CAD build script  --  rebuild the native host and refresh dist\AI-CAD\
rem ---------------------------------------------------------------------------
rem  Usage:
rem    build.bat            Default: compile the C++ host (Release) and sync the
rem                         exe + app code + assets into dist\AI-CAD\, KEEPING the
rem                         existing offline wheelhouse. Use after editing host
rem                         C++ or the engine / agent / mcp_server Python.
rem    build.bat host       Only compile the C++ host exe (does not touch dist\).
rem    build.bat app        Only sync the Python app code into dist\ (no compile).
rem                         Fastest path when you only changed .py files.
rem    build.bat full       Full clean release via installer.build_release --
rem                         re-downloads the wheelhouse (slow, needs network).
rem    build.bat clean      Wipe host\build and dist\, then do a default build.
rem ---------------------------------------------------------------------------
rem  Close any running AI-CAD.exe first, or the exe copy will fail (file lock).
rem ===========================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=default"

set "BUILD=%ROOT%\host\build"
set "EXE=%BUILD%\bin\Release\AI-CAD.exe"
set "DIST=%ROOT%\dist\AI-CAD"

rem --- locate a Python for the staging / release steps -----------------------
set "PY="
set "VENVPY=%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe"
if exist "%VENVPY%" set "PY=%VENVPY%"
if not defined PY for /f "delims=" %%p in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"

if /i "%MODE%"=="clean" (
  echo [build] cleaning host\build and dist ...
  if exist "%BUILD%" rmdir /s /q "%BUILD%"
  if exist "%ROOT%\dist" rmdir /s /q "%ROOT%\dist"
  set "MODE=default"
)

rem --- configure + compile the native host (skipped for 'app') ---------------
if /i not "%MODE%"=="app" (
  if not exist "%BUILD%\AI_CAD_Host.sln" (
    echo [build] configuring CMake ...
    cmake -S "%ROOT%\host" -B "%BUILD%" -G "Visual Studio 17 2022" -A x64 || goto :fail
  )
  echo [build] compiling AI-CAD.exe ^(Release^) ...
  cmake --build "%BUILD%" --config Release --target AI-CAD || goto :fail
  if not exist "%EXE%" (
    echo [build] ERROR: exe was not produced
    goto :fail
  )
)

if /i "%MODE%"=="host" goto :done

rem --- full clean release (re-stages everything, incl. the wheelhouse) -------
if /i "%MODE%"=="full" (
  if not defined PY (
    echo [build] ERROR: no Python found for installer.build_release
    goto :fail
  )
  echo [build] assembling full release ^(re-downloads the wheelhouse^) ...
  pushd "%ROOT%"
  "%PY%" -m installer.build_release --out "%ROOT%\dist" || ( popd & goto :fail )
  popd
  goto :done
)

rem --- default / app: refresh the dist payload in place (keeps wheelhouse) ----
echo [build] syncing dist\AI-CAD\ ...
if not exist "%DIST%" mkdir "%DIST%"

if /i not "%MODE%"=="app" (
  copy /y "%EXE%" "%DIST%\AI-CAD.exe" >nul || goto :fail
  robocopy "%ROOT%\host\assets" "%DIST%\assets" /MIR /XF Rubik-VF.ttf make_logo.py /XD __pycache__ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 goto :fail
  rem --- stage the C++ core (libaicad.dll + its OCCT/Netgen sibling DLLs) next to
  rem     the exe so the host loads the in-process C++ engine with NO env override
  rem     (AI_CAD_LIBAICAD). This is the production engine for parts / sim / agent.
  set "CORE_BIN=%ROOT%\core\build-occt\bin\Release"
  if exist "%ROOT%\core\build-occt\bin\Release\libaicad.dll" (
    echo [build] staging C++ core DLLs from core\build-occt\bin\Release ...
    rem /R:2 /W:1 so a DLL locked by a RUNNING app/MCP fails fast (≈2s) instead of robocopy's
    rem default million-retry (which hangs the build for ~30 min). If this fails, close the
    rem desktop app AND any running MCP server (they hold libaicad.dll + the OCCT TK*.dll), then rerun.
    robocopy "%ROOT%\core\build-occt\bin\Release" "%DIST%" *.dll /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 (
      echo [build] ERROR: could not stage core DLLs -- a DLL is locked. Close AI-CAD.exe AND any
      echo [build]        running AI-CAD MCP server ^(both load libaicad.dll / OCCT TK*.dll^), then rerun.
      goto :fail
    )
  ) else (
    echo [build] WARNING: core\build-occt\bin\Release\libaicad.dll not found -- C++ core NOT staged.
    echo [build]          The app will fall back to the Python worker. Build the core with:
    echo [build]          cmake --build core\build-occt --config Release
  )
)

for %%P in (engine agent mcp_server installer) do (
  robocopy "%ROOT%\%%P" "%DIST%\app\%%P" /MIR /XD __pycache__ build samples /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 goto :fail
)
for %%P in (llamacpp models) do (
  if exist "%ROOT%\%%P" (
    robocopy "%ROOT%\%%P" "%DIST%\%%P" /MIR /XD __pycache__ /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )
)
copy /y "%ROOT%\requirements.txt" "%DIST%\app\requirements.txt" >nul || goto :fail
copy /y "%ROOT%\requirements.txt" "%DIST%\requirements.txt" >nul

:done
echo.
echo [build] DONE (%MODE%).  Run: "%DIST%\AI-CAD.exe"
endlocal & exit /b 0

:fail
echo.
echo [build] FAILED (%MODE%).  See the messages above.
endlocal & exit /b 1

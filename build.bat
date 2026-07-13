@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  Billet (AI-CAD) Windows build  --  rebuild host + stage dist\AI-CAD\
rem ---------------------------------------------------------------------------
rem  Usage:
rem    build.bat            Default: compile Billet (Release) and sync exe +
rem                         core DLLs + app code + assets into dist\AI-CAD\.
rem    build.bat host       Only compile the C++ host (does not touch dist\).
rem    build.bat core       Only rebuild libaicad + aicad-hostd (windows-occt).
rem    build.bat app        Only sync Python app code into dist\ (no compile).
rem    build.bat full       Full clean release via installer.build_release
rem                         (re-downloads the wheelhouse; needs network).
rem    build.bat clean      Wipe host\build-win and dist\, then default build.
rem    build.bat shortcut   Install/refresh the user Start Menu shortcut only.
rem ---------------------------------------------------------------------------
rem  Close any running Billet.exe first, or the exe/DLL copy will fail (lock).
rem  Requires VS 2022/2025 Build Tools or Community with C++ workload.
rem  Run from a normal shell; this script loads vcvars64 when needed.
rem ===========================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=default"

set "BUILD=%ROOT%\host\build-win"
set "EXE=%BUILD%\bin\Billet.exe"
set "DIST=%ROOT%\dist\AI-CAD"
set "CORE_BUILD=%ROOT%\build\windows-occt"
set "CORE_BIN=%CORE_BUILD%\bin"

rem --- locate cmake (PATH, then VS-bundled) -----------------------------------
set "CMAKE=cmake"
where cmake >nul 2>&1
if errorlevel 1 (
  set "CMAKE="
  for %%C in (
    "%ProgramFiles%\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    "%ProgramFiles%\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    "%ProgramFiles%\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    "%ProgramFiles%\CMake\bin\cmake.exe"
  ) do if not defined CMAKE if exist %%~C set "CMAKE=%%~C"
)
if not defined CMAKE (
  echo [build] ERROR: cmake not found. Install VS C++ workload or Kitware CMake.
  goto :fail
)

rem --- locate a Python for the staging / release steps -----------------------
set "PY="
set "VENVPY=%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe"
if exist "%VENVPY%" set "PY=%VENVPY%"
if not defined PY for /f "delims=" %%p in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"

if /i "%MODE%"=="shortcut" goto :shortcut

if /i "%MODE%"=="clean" (
  echo [build] cleaning host\build-win and dist ...
  if exist "%BUILD%" rmdir /s /q "%BUILD%"
  if exist "%ROOT%\dist" rmdir /s /q "%ROOT%\dist"
  set "MODE=default"
)

rem --- ensure MSVC is on PATH when compiling (app/shortcut skip the toolchain)
if /i not "%MODE%"=="app" if /i not "%MODE%"=="shortcut" (
  where cl >nul 2>&1
  if errorlevel 1 call :load_vcvars
  where cl >nul 2>&1
  if errorlevel 1 (
    echo [build] ERROR: MSVC cl.exe not on PATH. Install VS C++ tools, or open
    echo [build]        "x64 Native Tools Command Prompt" and re-run build.bat.
    goto :fail
  )
)

rem --- core only -------------------------------------------------------------
if /i "%MODE%"=="core" (
  call :build_core || goto :fail
  goto :done
)

rem --- configure + compile the native host (skipped for 'app') ---------------
if /i not "%MODE%"=="app" (
  rem Prefer a staged core so modeling works out of the box.
  if not exist "%CORE_BIN%\libaicad.dll" (
    echo [build] libaicad.dll missing — building windows-occt core first ...
    call :build_core || goto :fail
  )

  if not exist "%BUILD%\build.ninja" if not exist "%BUILD%\CMakeCache.txt" (
    echo [build] configuring host ^(Ninja + MSVC, Release^) ...
    rem Auto-enable CEF when the Chromium 149 Windows SDK is under extern\cef\
    rem (scripts\fetch_cef_windows.ps1). Forward-slash CEF_ROOT so CEF cmake
    rem macros don't choke on Windows \U path escapes.
    set "CEF_ROOT_CFG="
    for /d %%D in ("%ROOT%\extern\cef\cef_binary_*") do (
      if exist "%%~D\Release\libcef.dll" set "CEF_ROOT_CFG=%%~D"
    )
    if defined CEF_ROOT_CFG (
      set "CEF_ROOT_CFG=%CEF_ROOT_CFG:\=/%"
      echo [build] CEF SDK found — enabling AI_CAD_ENABLE_CEF
      "%CMAKE%" -S "%ROOT%\host" -B "%BUILD%" -G Ninja -DCMAKE_BUILD_TYPE=Release -DAI_CAD_ENABLE_CEF=ON -DCEF_ROOT=%CEF_ROOT_CFG% || goto :fail
    ) else (
      echo [build] no CEF SDK under extern\cef — building without browser backend
      echo [build]          ^(run scripts\fetch_cef_windows.ps1 to enable CEF^)
      "%CMAKE%" -S "%ROOT%\host" -B "%BUILD%" -G Ninja -DCMAKE_BUILD_TYPE=Release -DAI_CAD_ENABLE_CEF=OFF || goto :fail
    )
  )
  echo [build] compiling Billet.exe ^(Release^) ...
  "%CMAKE%" --build "%BUILD%" --config Release --target Billet || goto :fail
  if not exist "%EXE%" (
    echo [build] ERROR: exe was not produced at %EXE%
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
  call :install_shortcut
  goto :done
)

rem --- default / app: refresh the dist payload in place ----------------------
echo [build] syncing dist\AI-CAD\ ...
if not exist "%DIST%" mkdir "%DIST%"

if /i not "%MODE%"=="app" (
  copy /y "%EXE%" "%DIST%\Billet.exe" >nul || goto :fail
  if exist "%BUILD%\bin\Billet.pdb" copy /y "%BUILD%\bin\Billet.pdb" "%DIST%\Billet.pdb" >nul
  rem Compat name for older scripts that still look for AI-CAD.exe
  copy /y "%EXE%" "%DIST%\AI-CAD.exe" >nul 2>nul
  rem CEF helper + runtime (libcef.dll, *.pak, locales/) when built with AI_CAD_ENABLE_CEF
  if exist "%BUILD%\bin\ai_cad_cef_helper.exe" (
    copy /y "%BUILD%\bin\ai_cad_cef_helper.exe" "%DIST%\ai_cad_cef_helper.exe" >nul
  )
  if exist "%BUILD%\Release\ai_cad_cef_helper.exe" if not exist "%DIST%\ai_cad_cef_helper.exe" (
    copy /y "%BUILD%\Release\ai_cad_cef_helper.exe" "%DIST%\ai_cad_cef_helper.exe" >nul
  )
  if exist "%BUILD%\bin\libcef.dll" (
    echo [build] staging CEF runtime from %BUILD%\bin ...
    robocopy "%BUILD%\bin" "%DIST%" libcef.dll chrome_elf.dll libEGL.dll libGLESv2.dll d3dcompiler_47.dll dxcompiler.dll dxil.dll vk_swiftshader.dll vulkan-1.dll icudtl.dat v8_context_snapshot.bin resources.pak chrome_100_percent.pak chrome_200_percent.pak vk_swiftshader_icd.json /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
    if exist "%BUILD%\bin\locales" (
      robocopy "%BUILD%\bin\locales" "%DIST%\locales" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
      if errorlevel 8 goto :fail
    )
  )

  if exist "%ROOT%\host\assets" (
    robocopy "%ROOT%\host\assets" "%DIST%\assets" /MIR /XF Rubik-VF.ttf make_logo.py /XD __pycache__ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )
  if exist "%BUILD%\bin\assets" (
    robocopy "%BUILD%\bin\assets" "%DIST%\assets" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )

  rem Stage C++ core DLLs + hostd next to the exe (in-process engine).
  set "CORE_BIN_STAGE="
  if exist "%ROOT%\build\windows-occt\bin\libaicad.dll" set "CORE_BIN_STAGE=%ROOT%\build\windows-occt\bin"
  if not defined CORE_BIN_STAGE if exist "%ROOT%\core\build-occt\bin\Release\libaicad.dll" set "CORE_BIN_STAGE=%ROOT%\core\build-occt\bin\Release"
  if defined CORE_BIN_STAGE (
    echo [build] staging C++ core DLLs from %CORE_BIN_STAGE% ...
    robocopy "%CORE_BIN_STAGE%" "%DIST%" *.dll /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 (
      echo [build] ERROR: could not stage core DLLs -- a DLL is locked. Close Billet.exe AND any
      echo [build]        running Billet/AI-CAD MCP server, then rerun.
      goto :fail
    )
    if exist "%CORE_BIN_STAGE%\aicad-hostd.exe" (
      robocopy "%CORE_BIN_STAGE%" "%DIST%" aicad-hostd.exe aicad-hostd.pdb /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
      if errorlevel 8 (
        echo [build] ERROR: could not stage aicad-hostd.exe -- it is locked ^(daemon running^).
        echo [build]        Stop it first:  "%DIST%\aicad-hostd.exe" --stop   then rerun.
        goto :fail
      )
    ) else (
      echo [build] WARNING: aicad-hostd.exe not found in %CORE_BIN_STAGE% -- engine daemon NOT staged.
      echo [build]          Build it:  build.bat core
    )
  ) else (
    echo [build] WARNING: libaicad.dll not found under build\windows-occt\bin
    echo [build]          -- C++ core NOT staged. App opens but cannot create geometry.
    echo [build]          Build the core:  build.bat core
  )
)

rem Python app packages (junction names engine/agent/mcp_server/installer when present)
for %%P in (engine agent mcp_server installer) do (
  if exist "%ROOT%\%%P\" (
    robocopy "%ROOT%\%%P" "%DIST%\app\%%P" /MIR /XD __pycache__ build samples tests /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )
)
rem services.X packages (PLM hub, dag, browser, …) — engine.worker imports these as
rem `from services.plm.worker_rpc …`. Without them plm_*/git_* never register.
if not exist "%DIST%\app\services" mkdir "%DIST%\app\services"
if exist "%ROOT%\services\__init__.py" copy /y "%ROOT%\services\__init__.py" "%DIST%\app\services\__init__.py" >nul
if not exist "%DIST%\app\services\__init__.py" type nul > "%DIST%\app\services\__init__.py"
for %%P in (plm dag browser deliverables ipc serving verify) do (
  if exist "%ROOT%\services\%%P\" (
    robocopy "%ROOT%\services\%%P" "%DIST%\app\services\%%P" /MIR /XD __pycache__ build tests /XF *.pyc test_*.py /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )
)
if exist "%ROOT%\agent\" (
  robocopy "%ROOT%\agent" "%DIST%\app\services\agent" /MIR /XD __pycache__ build tests /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 goto :fail
)
if exist "%ROOT%\mcp_server\" (
  robocopy "%ROOT%\mcp_server" "%DIST%\app\services\mcp" /MIR /XD __pycache__ build tests /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 goto :fail
)
for %%P in (llamacpp models) do (
  if exist "%ROOT%\%%P\" (
    robocopy "%ROOT%\%%P" "%DIST%\%%P" /MIR /XD __pycache__ /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :fail
  )
)
if exist "%ROOT%\requirements.txt" (
  copy /y "%ROOT%\requirements.txt" "%DIST%\app\requirements.txt" >nul 2>nul
  copy /y "%ROOT%\requirements.txt" "%DIST%\requirements.txt" >nul 2>nul
)

call :install_shortcut

:done
echo.
echo [build] DONE (%MODE%).  Run: "%DIST%\Billet.exe"
echo [build]        Start Menu: Billet
endlocal & exit /b 0

:fail
echo.
echo [build] FAILED (%MODE%).  See the messages above.
endlocal & exit /b 1

rem ===========================================================================
:load_vcvars
for %%V in (
  "%ProgramFiles%\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
  "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
) do if exist "%%~V" (
  echo [build] loading MSVC env from %%~V
  call "%%~V" >nul
  exit /b 0
)
exit /b 1

:build_core
echo [build] building C++ core ^(windows-occt: aicad + aicad-hostd^) ...
if not exist "%CORE_BUILD%\build.ninja" if not exist "%CORE_BUILD%\CMakeCache.txt" (
  "%CMAKE%" --preset windows-occt || exit /b 1
)
"%CMAKE%" --build "%CORE_BUILD%" --config Release --target aicad aicad-hostd || exit /b 1
if not exist "%CORE_BIN%\libaicad.dll" (
  echo [build] ERROR: libaicad.dll not produced
  exit /b 1
)
exit /b 0

:shortcut
call :install_shortcut
goto :done

:install_shortcut
set "PROGRAMS=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "LNK=%PROGRAMS%\Billet.lnk"
set "TARGET=%DIST%\Billet.exe"
if not exist "%TARGET%" set "TARGET=%EXE%"
if not exist "%TARGET%" (
  echo [build] WARNING: no Billet.exe to shortcut yet
  exit /b 0
)
set "WORKDIR=%DIST%"
if not exist "%WORKDIR%\Billet.exe" set "WORKDIR=%BUILD%\bin"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut('%LNK%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%WORKDIR%'; $s.Description='Billet parametric CAD'; $s.IconLocation='%TARGET%,0'; $s.Save()"
if exist "%LNK%" (
  echo [build] Start Menu shortcut: %LNK%
) else (
  echo [build] WARNING: could not create Start Menu shortcut
)
exit /b 0

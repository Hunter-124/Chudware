@echo off
setlocal EnableDelayedExpansion
REM ===========================================================================
REM  Chudware MCP setup  --  portable one-click installer
REM
REM  Wires the Chudware MCP server (id "chudware"; legacy "ai-cad" entries are
REM  preserved) into every MCP client you have installed:
REM      Claude Code, Cursor, VS Code, opencode, Antigravity.
REM
REM  What it does:
REM    1. Finds a Python that already has the engine deps (the Chudware runtime,
REM       or a repo-local .venv). If none is found, it builds a .venv here and
REM       installs requirements.txt.
REM    2. Verifies the MCP server actually starts.
REM    3. Writes/merges the right config file for each client (non-destructive).
REM
REM  Usage:   mcp_setup.bat              install / re-register (full profile)
REM           mcp_setup.bat lite         install lite profile (capable models)
REM           mcp_setup.bat uninstall    remove the chudware (and legacy ai-cad) entries
REM ===========================================================================

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
cd /d "%REPO%"

set "MODE=register"
set "PROFILE=full"
if /i "%~1"=="uninstall"   set "MODE=unregister"
if /i "%~1"=="--unregister" set "MODE=unregister"
if /i "%~1"=="remove"      set "MODE=unregister"
if /i "%~1"=="lite"        set "PROFILE=lite"
if /i "%~1"=="--lite"      set "PROFILE=lite"
if /i "%~1"=="full"        set "PROFILE=full"
if /i "%~2"=="lite"        set "PROFILE=lite"
if /i "%~2"=="--lite"      set "PROFILE=lite"

echo(
echo ===========================================================
echo   Chudware MCP setup  (%MODE%, profile=%PROFILE%)
echo   repo: %REPO%
echo ===========================================================
echo(

REM ---- 1. locate a usable Python -------------------------------------------
set "PYEXE="

call :try_python "%APPDATA%\Chudware\runtime\venv\Scripts\python.exe"
if not defined PYEXE call :try_python "%APPDATA%\Chudware\runtime\venv\Scripts\python.exe"
if not defined PYEXE call :try_python "%REPO%\.venv\Scripts\python.exe"

if not defined PYEXE (
    echo No ready-made Python found. Building a local .venv ...
    set "BASEPY="
    where py >nul 2>nul   && set "BASEPY=py -3"
    if not defined BASEPY ( where python >nul 2>nul && set "BASEPY=python" )
    if not defined BASEPY (
        echo(
        echo  ERROR: no Python launcher on PATH. Install Python 3.11+ from python.org
        echo         (tick "Add python.exe to PATH"^) and re-run this script.
        goto :fail
    )
    !BASEPY! -m venv "%REPO%\.venv"
    if errorlevel 1 ( echo  ERROR: venv creation failed. & goto :fail )
    set "PYEXE=%REPO%\.venv\Scripts\python.exe"
    echo Installing dependencies (this can take a few minutes the first time^) ...
    "!PYEXE!" -m pip install --upgrade pip
    "!PYEXE!" -m pip install -r "%REPO%\requirements.txt"
    if errorlevel 1 ( echo  ERROR: dependency install failed. & goto :fail )
)

echo Using Python: !PYEXE!
echo(

REM ---- 2. verify the server can start --------------------------------------
echo Verifying the MCP server imports ...
"!PYEXE!" -c "import services.mcp.server" 2>nul
if errorlevel 1 (
    echo  ERROR: the MCP server failed to import under this Python.
    echo         Try deleting .venv and re-running, or check requirements.txt.
    goto :fail
)
echo   ok
echo(

REM ---- 3. write the client configs -----------------------------------------
set "FLAG="
if /i "%MODE%"=="unregister" set "FLAG=--unregister"
"!PYEXE!" "%REPO%\installer\setup_mcp_clients.py" --python "!PYEXE!" --repo "%REPO%" --profile %PROFILE% !FLAG!
if errorlevel 1 ( echo  ERROR: writing client configs failed. & goto :fail )

echo(
echo ===========================================================
if /i "%MODE%"=="unregister" (
    echo   Done. The chudware (and legacy ai-cad) MCP server was removed from all clients.
) else (
    echo   Done. Restart any open client to pick up the server:
    echo     - Claude Code : reopen this folder; approve "chudware" when prompted
    echo     - Cursor      : Settings ^> MCP  (or just reopen the project^)
    echo     - VS Code     : reopen the folder; Start the server in .vscode/mcp.json
    echo     - opencode    : run it from this folder
    echo     - Antigravity : restart the app
)
echo ===========================================================
endlocal
exit /b 0

REM ---- helper: set PYEXE if %1 exists and has mcp + build123d --------------
:try_python
if defined PYEXE goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import mcp, build123d" >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:fail
echo(
echo Setup did not complete.
endlocal
exit /b 1

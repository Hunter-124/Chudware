@echo off
rem One-shot team sync: refresh remote board/issues/presence caches, print digest.
rem Usage: work\sync.bat [agent-name]
pushd "%~dp0.."
where py >nul 2>nul && (set "PYCMD=py -3") || (set "PYCMD=python")
if "%~1"=="" (
  %PYCMD% work\tools\teamsync.py sync
) else (
  %PYCMD% work\tools\teamsync.py sync --agent %1
)
popd

@echo off
rem Team sync daemon: continuously pulls remote task/issue/presence state, pushes
rem your heartbeat, and prints a change digest each cycle. Leave running while working.
rem Usage: work\workd.bat <agent-name> [interval-seconds]
if "%~1"=="" ( echo usage: work\workd.bat ^<agent-name^> [interval-seconds] & exit /b 2 )
pushd "%~dp0.."
where py >nul 2>nul && (set "PYCMD=py -3") || (set "PYCMD=python")
set "INTERVAL=%~2"
if "%INTERVAL%"=="" set "INTERVAL=60"
%PYCMD% work\tools\teamsync.py daemon --agent %1 --interval %INTERVAL%
popd

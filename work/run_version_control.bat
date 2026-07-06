@echo off
rem Launch the team version-control dashboard:
rem   Hot issues / Human roadblocks / Master DAG / Lane ownership (assign lanes to
rem   people + their working branch) / Codebase (graphify) preview.
rem Safe to run more than once - if the dashboard is already up it just opens a
rem fresh browser tab instead of failing to bind the port.
pushd "%~dp0.."
where py >nul 2>nul && (set "PYCMD=py -3") || (set "PYCMD=python")
start "" %PYCMD% work\tools\dashboard.py %*
popd

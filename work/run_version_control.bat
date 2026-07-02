@echo off
rem Launch the team-progress dashboard (Human Roadblocks / DAG / Graphify Preview / Verify).
pushd "%~dp0.."
where py >nul 2>nul && (set "PYCMD=py -3") || (set "PYCMD=python")
start "" %PYCMD% work\tools\dashboard.py
popd

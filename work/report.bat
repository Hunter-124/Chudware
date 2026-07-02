@echo off
rem Launch the AI-guided issue-reporter GUI (bug/idea -> GitHub issue + backlog task).
pushd "%~dp0.."
where py >nul 2>nul && (set "PYCMD=py -3") || (set "PYCMD=python")
start "" %PYCMD% work\tools\issue_reporter.py
popd

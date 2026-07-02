#!/usr/bin/env bash
# Launch the AI-guided issue-reporter GUI (bug/idea -> GitHub issue + backlog task).
cd "$(dirname "$0")/.." || exit 1
exec python3 work/tools/issue_reporter.py

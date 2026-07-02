---
description: Launch the AI-guided issue-reporter GUI (bug/idea -> GitHub issue + backlog task)
---

!`(nohup python3 work/tools/issue_reporter.py >/tmp/aicad-issue-reporter.log 2>&1 & disown; sleep 1; echo "Issue reporter launched.") 2>&1 || (start "" py -3 work\tools\issue_reporter.py 2>nul && echo "Issue reporter launched (Windows).")`

Tell the user: the issue-reporter GUI window should now be open on their screen (title
"AI-CAD — Report an Issue / Idea"). It walks them through drafting a bug/idea report,
optionally asks the local `claude` CLI to research the repo and elaborate it into a
well-scoped report, then files it as a linked GitHub issue + `work/backlog/` task in one
step. If no window appeared within a few seconds: on Linux/macOS check
`/tmp/aicad-issue-reporter.log` for errors (common causes: no `python3-tk` package
installed, or no display/headless session — it needs a real desktop session, not SSH);
on Windows check that `py` is on PATH. $ARGUMENTS

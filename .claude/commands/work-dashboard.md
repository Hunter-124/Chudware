---
description: Launch the team-progress dashboard (Human Roadblocks / DAG / Graphify Preview / Verify)
---

!`(nohup python3 work/tools/dashboard.py >/tmp/aicad-dashboard.log 2>&1 & disown; sleep 1; echo "Dashboard launched.") 2>&1 || (start "" py -3 work\tools\dashboard.py 2>nul && echo "Dashboard launched (Windows).")`

Tell the user: the dashboard window should now be open on their screen (title
"AI-CAD — Team Progress Dashboard"). Four tabs: **Human Roadblocks** (what's blocked on
a human right now — check this first), **DAG** (the master task graph, click a node to
claim/note/change status/release, or edit its dependencies/workstream), **Graphify
Preview** (live query/explain with a staleness banner), and **Verify** (a task's
Definition-of-done checklist + `scripts/gate.sh` runner + progress log). Set an identity
(top-left "Set identity…") before using any claim/note/status/relink action — it's how
actions get attributed. If no window appeared within a few seconds: on Linux/macOS check
`/tmp/aicad-dashboard.log` for errors (no `python3-tk` package, or no display/headless
session — needs a real desktop session, not SSH); on Windows check that `py` is on PATH.
$ARGUMENTS

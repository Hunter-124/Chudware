---
description: Launch the read-only team-progress dashboard (Human Roadblocks / DAG / Graphify Preview)
---

!`(nohup python3 work/tools/dashboard.py >/tmp/aicad-dashboard.log 2>&1 & disown; sleep 1; echo "Dashboard launched.") 2>&1 || (start "" py -3 work\tools\dashboard.py 2>nul && echo "Dashboard launched (Windows).")`

Tell the user: the dashboard window should now be open on their screen (title
"AI-CAD — Team Progress (read-only)"). Three tabs, all display-only — it never claims,
edits, or writes anything: **Human Roadblocks** (what's blocked on a human right now —
check this first), **DAG** (the master task graph, color-coded, greyed where locked on
unmet deps or awaiting human review — click a node to see its details), and **Graphify
Preview** (live query/explain with a staleness banner). Auto-refreshes every 30s. To
actually change the plan — claim a task, add a note, change status, edit dependencies —
do it here in chat (the `aicad-team` MCP tools: `claim`, `note`, `set_status`,
`work_relink`, ...) or let autopilot decide on its own (`work/prompts/autopilot.md`). If
no window appeared within a few seconds: on Linux/macOS check `/tmp/aicad-dashboard.log`
for errors (no `python3-tk` package, or no display/headless session — needs a real
desktop session, not SSH); on Windows check that `py` is on PATH. $ARGUMENTS

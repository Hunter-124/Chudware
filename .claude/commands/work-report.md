---
description: Help the user report a bug or idea as a GitHub issue plus a work/backlog card (user-run reporter or gh CLI)
---
Help the user report a bug or idea. Do not launch anything yourself. The user runs one of
these two paths.

**Issue-reporter GUI (desktop session only).**
- Start it with `work/report.sh`, or `python3 work/tools/issue_reporter.py` (Windows:
  `work\report.bat`).
- Requirements: Tk (`python3-tk`) and a real display. It does not work over SSH or headless.
- Optional **Preview** box: runs `graphify query`.
- Optional **Ask AI to elaborate** button: runs the user's local `claude -p` against the repo.
- **Submit** does three things:
  - Runs `gh issue create` with a `severity:<level>` label. It is skipped when `gh` is
    missing, and the failure is reported when the label or authentication is missing.
  - Writes the next `work/backlog/T-BL<NN>.md` card.
  - Regenerates `work/DAG.md` via `python3 work/tools/work.py dag`.

**CLI (no GUI).**
- Issue: `gh issue create --title "<title>" --body-file <draft.md>`.
- Board card: a `work/backlog/` card following work/WORKFLOW.md, then
  `python3 work/tools/work.py validate`.
- Use the host issue interface (`read issue://…`, `xd://github` search) to check for
  duplicates before reporting; `/work-issues` describes lookup and honest failures.

Your part:
1. Draft the report from what the user gave below: title, description, repro steps,
   expected vs actual behaviour, severity and suspected files. Ground the suspected files
   with the current graph/source evidence before naming them.
2. Hand the draft back together with the exact command for the path the user picks.
3. Write the backlog card or body file only if the user asks.

$ARGUMENTS

---
name: chudware-ai-tab-command-center
description: Update the AI workspace command center and its shared settings controls.
---

# chudware-ai-tab-command-center

- In `host/src/ui/ai/ai_workspace.cpp`, current navigation is Current Run (0), Sub-agents (6), Plan (1), Activity (2), Sessions (3), Insights (4), and Options (5).
- Keep session knobs shared through `drawHarnessSessionKnobs`; trace the selected conversation and event ownership when changing the views.
- Re-read files that changed before editing rather than bypassing an edit staleness guard. Use an isolated visual probe.

Verify: `scripts/testbox.py --suite agent`

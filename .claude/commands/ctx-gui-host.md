---
description: Work on the gui-host section — loads its maintained context pack (source map, entry points, debugging playbook)
---
You are working on the **gui-host** section of Chudware. Its maintained context pack (tracked in git, kept current by agents):

!`cat work/context/gui-host.md 2>/dev/null || echo "(context pack missing — run /work-ctx-refresh or explore with graphify)"`

Use the entry points and graphify starters to orient before exploring raw files. The pack is a map; code is the authority. GUI ownership and verification: `.claude/skills/gui-orchestrator/SKILL.md`; task policy: `work/WORKFLOW.md`. Refresh stale references in work/context/gui-host.md within your write-set. Debugging commands are a verification handoff: leaf agents do not run builds, gates, tests, formatters, generated-output refreshes or git commands; the driver verifies the combined change once.

Task: $ARGUMENTS

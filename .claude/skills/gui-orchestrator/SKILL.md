---
name: gui-orchestrator
description: Master orchestrator for AI-CAD GUI change sessions. Use whenever the user hands over one or more GUI/UI changes to make in this repo — visual tweaks, panel behavior, ribbon buttons, icons, layout, theming, any "change the UI so that…" request — even a single small one. Sets up a master-branch session, clarifies ambiguous targets with the user, batches changes into waves of parallel subagents dispatched straight to the right files (no searching), gates each wave on a compiling build, pushes to master as it goes, and hands the user a manual verification checklist instead of screenshots.
---

# GUI Orchestrator

You are the **integrator** for a GUI-changes session. You do not edit UI code yourself except
trivial one-liners — you organize the user's change list, dispatch subagents to exact files,
gate their output on a build, commit+push, and tell the user precisely what to eyeball in the
running app. Your value is that subagents never burn tokens searching: this skill and the
specialist skills below already contain the map.

## Session setup (do this first, every session)

```bash
cd <repo root>   # the directory containing this .claude/skills/
git checkout master && git pull origin master
git status -sb   # must be clean; if dirty or a stray branch has unmerged work, STOP and ask the user
./build.sh       # baseline build — never dispatch onto a broken master
```

Master-only workflow: all commits land on `master` and are pushed to `origin master`
immediately after each wave. No feature branches, no PRs. If master doesn't build at session
start, fixing that is wave 0.

## Intake — clarify before you dispatch

Take the user's full list of changes. For each one, resolve it to **subsystem + files** using
the dispatch table below. If a change is ambiguous — which panel, which element, which of two
similar widgets, desired look/behavior — **ask the user now** (AskUserQuestion, batch all
ambiguities into one round). A wrong guess costs a whole wave; a question costs seconds.
Never dispatch a change you can't name the target file for.

For anything the table doesn't cover, locate it with graphify before falling back to grep:
`graphify query "<question>"`, `graphify path "<fileA>" "<fileB>"`, or read
`graphify-out/GRAPH_REPORT.md`. See `.claude/skills/graphify/SKILL.md`. This covers the whole
program (engine, kernel, MCP server, contracts), not just the GUI — use it when a GUI change
has non-GUI tentacles (e.g. a new command needs a worker RPC).

## Dispatch table

All production GUI code lives in `host/src/` — per `build.sh`, "the ui/imgui / ui/abstract
(AI-CAD2) app is retired; host/ is the single cross-platform GUI target". Ignore AGENTS.md's
claim that host/ was deleted (stale, T-G11 was reverted); do not send agents to `ui/imgui`. Each subsystem has a
specialist skill with the deep map; subagent prompts must say **"Read <skill path> first"**
so workers load only their own subsystem's knowledge.

| Change concerns | Files (primary) | Specialist skill |
|---|---|---|
| Icons, atlas, icon generation from modeling scripts | `assets/make_icons.py`, `scripts/icon_pipeline.py`, `host/src/ui/icons*.cpp` | `.claude/skills/gui-icons/SKILL.md` |
| 3D viewport, rendering, picking, camera, overlays/HUD | `host/src/ui/viewport_panel.cpp`, `host/src/renderer.cpp`, `host/src/render/` | `.claude/skills/gui-viewport/SKILL.md` |
| Ribbon tabs/buttons, feature tree, PropertyManager forms, command routing | `host/src/ui/ribbon_def.cpp`, `ui/widgets/ribbon.cpp`, `ui/feature_tree.cpp`, `ui/property_manager.cpp`, `ui/pm_forms.cpp` | `.claude/skills/gui-ribbon-commands/SKILL.md` |
| 2D sketcher, 3D sketcher, constraints UI, dimensions | `host/src/ui/sketch_mode.cpp` (8.9k LOC), `ui/sketch3d_mode.cpp` | `.claude/skills/gui-sketch/SKILL.md` |
| Workspace tabs: Parts Library, Assembly, CAM, ECAD, PLM, RevEng, AI/Activity, Drawing, Simulation | `host/src/ui/parts/`, `ui/asm_panels.cpp`, `ui/cam/`, `ui/ecad/`, `ui/plm/`, `ui/reveng/`, `ui/ai/`, `ui/drawing_panel.cpp`, `ui/sim_manager.cpp` | `.claude/skills/gui-workspaces/SKILL.md` |
| Theme/colors/glass styling, dock layout, DPI/metrics, dialogs/modals, status bar, title bar, toasts | `host/src/ui_theme.cpp`, `ui/layout.cpp`, `ui/metrics.h`, `ui/settings_modal.cpp` | `.claude/skills/gui-theme-layout/SKILL.md` |

**Shared/hot files — integrator-owned.** `host/src/app.cpp` (frame loop / panel dispatch),
`host/src/core/app_state.h` (central data model), `host/src/ui/ribbon_def.cpp` (append-only
command table), `ui_theme.cpp/.h`, and anything in `contracts/`. At most ONE agent per wave
may touch a given shared file; if several changes need the same one, either merge them into
one agent or apply the shared-file edit yourself between waves. `contracts/` is append-only.

## Waves

1. Group the resolved changes into waves of parallel subagents with **disjoint write-sets**
   (per AGENTS.md swarm rules). Two changes to the same file = same agent or different waves.
2. Order waves by dependency (e.g. new icon must exist before the ribbon button that uses it).
3. Spawn each wave's agents in a single message so they run concurrently.

Each subagent prompt must contain, so the worker never searches or guesses:
- Read `<repo>/.claude/skills/<specialist>/SKILL.md` FIRST — it has the subsystem map.
- The exact change, in the user's words plus your clarifications.
- The exact files to edit (from the table + specialist skill).
- Match surrounding style; colors only via `ui_theme` (lint enforces this); icons only from
  `contracts/icons.md` inventory.
- Verify with a compile: `./build.sh` from repo root (2–3 min, Ninja). Fix until green.
- **Never run git** (leaf agents don't commit — you do).
- Return: files changed, one-line summary per change, and one manual verification step
  ("open X panel, do Y, expect Z").

## Gate, commit, push — per wave

After a wave's agents return:

```bash
./build.sh                                   # must compile clean
python3 scripts/gui_lint.py --rule theme     # if any wave touched colors/styling
python3 scripts/gui_lint.py --rule icons     # if any wave touched icons/ribbon
git add -A && git commit -m "gui: <wave summary>"   # one commit per wave
git push origin master                       # push as you go, every wave
```

If the build breaks and two agents' edits interact, fix the seam yourself rather than
re-dispatching. Optional cheap smoke test (no user needed):
`AI_CAD_SHOT=m1 ./host/build-linux/bin/AI-CAD --ui-screenshot scratch/smoke.png` — only to
confirm the app boots, not to visually judge the change.

## User verification — no screenshot self-review

Do NOT spend tokens screenshotting and judging visuals; the user verifies in the running app.
After each wave (or at session end for small waves), give a checklist, one line per change:

```
Run: ./host/build-linux/bin/AI-CAD
1. Ribbon > Sketch tab — new "Conic" flyout under Spline; icon renders in both themes.
2. Parts Library > search "bearing" — results stream in without freezing the filter pane.
```

Collect the user's pass/fail per item; failed items become the next wave.

## Keep the map alive

These skills are version-controlled so contributors share them. When a wave adds/renames/moves
panels or files, the same wave must update the affected specialist SKILL.md (and this table if
routing changed) — include it in the wave's commit. Also refresh the knowledge graph when
structure changed meaningfully: `graphify update .` (or per the graphify skill). A stale map
is worse than no map: agents trust it blindly.

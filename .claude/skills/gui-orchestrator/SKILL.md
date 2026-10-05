---
name: gui-orchestrator
description: Master orchestrator for Chudware GUI change sessions. Use whenever the user hands over one or more GUI/UI changes to make in this repo — visual tweaks, panel behavior, ribbon buttons, icons, layout, theming, any "change the UI so that…" request — even a single small one. Resolves each change to exact files, batches them into waves of parallel subagents with disjoint write-sets dispatched straight to those files (no searching), builds and exercises each wave centrally, and hands the user a visual verification checklist.
---

# GUI Orchestrator

You are the **integrator** for a GUI-changes session. You do not edit UI code yourself except
trivial one-liners and shared-file seams — you organize the user's change list, dispatch
subagents to exact files, build and exercise their output centrally, and tell the user precisely
what to eyeball in the running app. Your value is that subagents never burn tokens searching:
this skill and the specialist skills below already contain the map. This skill is the single
home for shared-file ownership, wave rules and verification; specialist skills only carry their
subsystem's map and gotchas.

## Session setup

```bash
git status -sb   # record once: which paths already carry user edits
./build.sh       # baseline — never dispatch onto a tree that does not build
```

A dirty tree is normal: leave unrelated paths and hunks untouched and plan write-sets around
them. Stop and ask only if a file a change needs carries conflicting user edits. Work on
`master`: no feature branches, no worktrees, no PRs; never pull, checkout or stash over
uncommitted work. If the baseline does not build, fixing that is wave 0.

## Intake — resolve before you dispatch

Take the user's full list of changes and resolve each to **subsystem + files** with the dispatch
table below. Settle what repo evidence can settle; if a change is still materially ambiguous —
which panel, which of two similar widgets, desired look/behavior — ask the user in ONE batched
question-tool round. A wrong guess costs a whole wave. Never dispatch a change you can't name the
target file for. For anything the table doesn't cover (including non-GUI tentacles such as a new
worker RPC), locate it with graphify (`graphify query|path|explain`, AGENTS.md workflow item 1)
before falling back to grep.

## Dispatch table

All production GUI code lives in `host/src/` — `host/` is the sole GUI source tree; there is no
`ui/imgui` / `ui/abstract` shell to send agents to. Each subsystem has a specialist skill with
the deep map; subagent prompts must say **"Read <skill path> first"** so workers load only their
own subsystem's knowledge.

| Change concerns | Files (primary) | Specialist skill |
|---|---|---|
| Icons, atlas, icon generation from modeling scripts | `assets/make_icons.py`, `scripts/icon_pipeline.py`, `host/src/ui/icons*.cpp` | `.claude/skills/gui-icons/SKILL.md` |
| 3D viewport, rendering, picking, camera, overlays/HUD | `host/src/ui/viewport_panel.cpp`, `host/src/renderer.cpp`, `host/src/render/` | `.claude/skills/gui-viewport/SKILL.md` |
| Ribbon tabs/buttons, feature tree, PropertyManager forms, command routing | `host/src/ui/ribbon_def.cpp`, `host/src/app_commands.cpp`, `ui/widgets/ribbon.cpp`, `ui/feature_tree.cpp`, `ui/property_manager.cpp`, `ui/pm_forms.cpp` | `.claude/skills/gui-ribbon-commands/SKILL.md` |
| 2D sketcher, 3D sketcher, constraints UI, dimensions | `host/src/ui/sketch_mode.cpp`, `ui/sketch3d_mode.cpp` | `.claude/skills/gui-sketch/SKILL.md` |
| Workspace tabs: Parts Library, Assembly, CAM, ECAD, PLM, RevEng, AI/Activity, Drawing, Simulation | `host/src/ui/parts/`, `ui/asm_panels.cpp`, `ui/cam/`, `ui/ecad/`, `ui/plm/`, `ui/reveng/` (RevEng also reaches `host/src/platform/camera*.cpp` for capture work), `ui/ai/`, `ui/drawing_panel.cpp`, `ui/sim_manager.cpp` | `.claude/skills/gui-workspaces/SKILL.md` |
| Theme/colors/glass styling, dock layout, DPI/metrics, dialogs/modals, status bar, title bar, toasts | `host/src/ui_theme.cpp`, `ui/layout.cpp`, `ui/metrics.h`, `ui/settings_modal.cpp` | `.claude/skills/gui-theme-layout/SKILL.md` |
| Shared widget/motion vocabulary — a new kit primitive, a new density token, a new easing | `host/src/ui/widgets/kit.h/.cpp`, `host/src/ui/anim.h/.cpp` | `.claude/skills/gui-theme-layout/SKILL.md` |

**Shared/hot files — integrator-owned.** `host/src/app.cpp` (frame loop / panel dispatch, app-level
command hooks), `host/src/app_commands.cpp` (ordered command router — registration order is
semantics), `host/src/core/app_state.h` (central data model), `host/src/ui/ribbon_def.cpp`
(append-only command table), `host/src/ui/metrics.h` (append-only sizing tokens, including the
`re*()` block the Reverse Engineering workspace sizes itself from; RE panel agents CALL
`metrics::re*()` and never write pixel literals, but adding a token is an integrator edit),
`ui_theme.cpp/.h`, `host/src/ui/widgets/kit.h/.cpp` and `host/src/ui/anim.h/.cpp` (the app-wide
widget and motion vocabularies — panel agents CONSUME them; a new primitive is a dedicated
earlier-wave agent), and anything in `contracts/` (append-only). At most ONE agent per wave may
touch a given shared file; if several changes need the same one, merge them into one agent or
apply the shared-file edit yourself between waves.

**Per-workspace kits are thin wrappers over `ui::kit`** (`ui/reveng/reveng_widgets.h`,
`ui/plm/plm_hub.h`, `ui/parts/parts_widgets.h`, `ui/copilot/ui_common.cpp`): panel agents consume
them and never edit them; a genuinely new widget is one dedicated agent owning `kit.h/.cpp` in an
earlier wave, exactly like the icon-before-ribbon-button ordering. The rule, its rationale and the
`gui_lint.py --rule dupe` enforcement live in `.claude/skills/gui-theme-layout/SKILL.md`.

## Waves

1. Group the resolved changes into waves of parallel subagents with **disjoint write-sets**.
   Two changes to the same file = same agent or different waves.
2. Order waves by dependency (e.g. new icon must exist before the ribbon button that uses it).
3. Spawn each wave's agents in a single message so they run concurrently.

Each subagent prompt must contain, so the worker never searches or guesses:
- Read `.claude/skills/<specialist>/SKILL.md` FIRST — it has the subsystem map.
- The exact change, in the user's words plus your clarifications.
- The exact files it may edit (from the table + specialist skill); nothing outside them.
- Match surrounding style; widgets from `ui::kit` and motion from `ui::anim` (never a hand-rolled
  card/chip/empty state or a bare duration literal); colors only via `ui_theme`; icons only from
  the `contracts/icons.md` inventory (details: gui-theme-layout).
- **"Implement, do not narrate."** Put those words in the prompt. This cost a wave: agents given a
  large multi-item brief spent their budget on design commentary and ran out mid-refactor,
  leaving call sites without definitions and a non-compiling tree. Scope ONE feature cluster per
  agent, and have it land each item complete (call site + definition) before starting the next.
- **Workers do not build, lint, run the app, format or touch git.** Concurrent `./build.sh` /
  `ninja` runs race on the Ninja lock; you build once per wave.
- Return: files changed, one-line summary per change, the `CHUDWARE_SHOT` scenario that exercises
  it (if any), and one user check ("open X panel, do Y, expect Z").

## Per-wave gate (integrator only)

```bash
./build.sh                                   # or: ninja -C host/build-linux Chudware
python3 scripts/gui_lint.py --rule theme     # applyTheme literal scale / FontGlobalScale
python3 scripts/gui_lint.py --rule color     # IM_COL32/ImVec4 literals, GetStyleColorVec4 bypass
python3 scripts/gui_lint.py --rule glyph     # codepoints the font has no cell for (render as '?')
python3 scripts/gui_lint.py --rule dupe      # a second implementation of a ui::kit primitive
python3 scripts/gui_lint.py --rule icons     # if the wave touched icons/ribbon
python3 scripts/gui_lint.py --rule scale     # unscaled pixel literals in ImGui geometry
CHUDWARE_SHOT=<scenario> xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/<scenario>.png
```

The ninja target is `Chudware`; the binary it produces is lowercase `chudware`. Run the scenario(s)
that exercise the surface the wave changed (`host/src/ui/scenarios.cpp`): `m1` (part build, edit,
undo — viewport/tree/PropertyManager), `sketch`, `sketch3d`, `appearance` (theme/glass),
`workspaces`, `manufacture`, `topology`, `iconpipe`, `reveng_splat` (real-GL splat proof, see
gui-viewport). Drop `xvfb-run -a` only with `CHUDWARE_SHOT_HEADED=1` for a visible window. The
scenario is the functional gate: it drives the real engine through the changed path.
Inspect the actual changed surface and its capture; a screenshot's existence is not
visual proof. Re-dispatch substantive broken artifacts to scoped corrective editors.

## User verification

After verifying the surface, provide a user acceptance checklist at the end of each
wave or session, one line per change:

```
Run: ./host/build-linux/bin/chudware
1. Ribbon > Sketch tab — new "Conic" flyout under Spline; icon renders in both themes.
2. Parts Library > search "bearing" — results stream in without freezing the filter pane.
```

Collect the user's pass/fail per item; failed items become the next wave.

## Commits

Commit only when the user asked for it or the session's workflow expects it, and only after a
green wave. You (never a worker) commit, staging the wave's write-set by explicit path — never
`git add -A` / `git add .`, which sweeps in untracked user files and unrelated edits — and only
your own hunks where a file also carries user changes. Push only when asked.

## Keep the map alive

When a wave adds/renames/moves panels or files, the same wave updates the affected specialist
SKILL.md (and this table if routing changed). Refresh the knowledge graph when structure changed
meaningfully: `graphify update .`. A stale map is worse than no map: agents trust it blindly.

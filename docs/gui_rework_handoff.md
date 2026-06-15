# GUI Rework — Handoff (5-wave plan COMPLETE)

> Session 2026-06-14/15. Plan: `docs/GUI_REWORK_PLAN.md`. Baseline before this session: `dd54040`.
> Driven by a parallel-subagent integrator pattern (engine ⟂ host streams; integrator commits).

## What landed (all on master)

| Commit | Wave | Summary |
|---|---|---|
| `8b997d5` | 4A/4B | Engine copy/paste ops: `copy_feature`/`paste_feature`/`paste_feature_as_derived`/`sketch_paste_entities`; `aicad.feature_bundle/1`, id→placeholder remap, cross-doc external-ref resolution per `selectors.md`. |
| `b9df13d` | 1C/1D | `buildDefaultLayout` extracted to `ui/layout.cpp`; single `"LeftPane"` dock with a vertical icon-tab strip (Explorer / FeatureManager / PropertyManager / Configurations). FeatureTree + PM split into no-Begin/End `*Body()` helpers; PM tab auto-selects on form open. |
| `9a633c8` | — | engine/sim reds fixed (were a misdiagnosis — `main()`-runner wiring, not gmsh/env) → 60 passed. |
| `4039d36` | 2 | ECAD Schematic/PCB as first-class doc tabs: `TabKind += Schematic,PCB`; per-`DocTab` `ecadState` (off the file-static singleton, multi-doc capable); New-Doc chooser on `+`; contextual ribbon on switch. |
| `2bb7869` | 4C/4D | Host copy/paste: `AppState::Clipboard`; FeatureManager Copy/Cut/Paste; sketch follow-cursor paste-placement; global Ctrl+C/X/V routing; `edit_copy/cut/paste` dispatcher ids. |
| `9afb9cc` | 5A/5B | SolidWorks 7-menu bar (File/Edit/View/Insert/Tools/Window/Help) wired via `ribbonDispatch` string ids + `st` flags; ribbon label/icon audit. |
| `1694aa9` | — | Closeout regression notes refreshed. |
| `2debbd2` | — | Engine read-only fast-path: `copy_feature` no longer bumps revision / pushes undo / dirties. |
| `4304246` | 3 | File Explorer panel + Save/Open/Recent (Ctrl+O/S/Shift+S), workspace tree, filesystem ops clamped to workspace root, recent files persist to config.json. |

**Regression at handoff:** host `build.bat host` exit 0; `--ui-screenshot` exit 0 (default / `AI_CAD_SHOT=tree|m1|ecad|explorer`). engine/doc **473**, engine/sim **60**, agent **40** — all green.

## Deferred (honest)
- **Wave 5C** theme/spacing DPI pass — intentionally skipped this round (touches too many files to run parallel-safe); a focused later pass.
- File **Export** menu (STEP/STL/3MF/DXF) + **Save All** + **Recent** beyond config — greyed/minimal; need exporter dispatch ids / multi-doc save.
- Explorer **drag-drop into viewport** (plan 3A.5).
- Copy/paste **cross-doc derived live-relink** (`paste_feature_as_derived` is one-time resolve + `derived_from` metadata; live propagation is a host follow-up).
- Wave 5 suggested AppState request flags for `edit_delete`/`select_all`/export — un-grey those Edit/Tools items later.

## Process notes for the next session (IMPORTANT)
- **Agent `isolation: 'worktree'` branches from a STALE base here** — all worktrees came up at `3b78d79` (the merge-base with `codex/gui-overhual`), **92 commits behind master**. Two agents "succeeded" against it; their diffs would have reverted master. **Do NOT use worktree isolation in this repo** — run agents NON-isolated in the main tree.
- **Safe parallelism without worktrees:** engine (Python, `pytest`) ⟂ host (C++, `build.bat`) run concurrently fine (disjoint files + disjoint build/test). For two HOST agents: only safe if neither self-builds (a shared build compiles the other's half-edited files) — give them **disjoint write-sets** and have the **integrator build once**. String-keyed `ribbonDispatch("cmd_id")` decouples menu↔command waves at compile time.
- `app_state.h` / `CMakeLists.txt` are append-only shared — one owner per round.
- `cmd.exe /c "build.bat host"` sometimes prints only the shell banner via the Bash tool; use the **PowerShell tool** (`& cmd.exe /c "<abs path>\build.bat host"`) to capture real output + exit code.
- `--ui-screenshot` REQUIRES an output-path arg, else it falls through to windowed mode and never exits headlessly.

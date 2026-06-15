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

## Deferred items — follow-up round (DONE 2026-06-15)
The deferred items below were tackled after the 5-wave plan landed (engine⟂host parallel streams):
- `6dbdc6b` **cross-doc derived live-relink** — `session.refresh_derived(id?)` re-pulls derived-paste
  features from their live source doc; worker RPC `doc_refresh_derived`. (479 passed.)
- `0d78843` **File > Export** (STEP/IGES/STL/3MF/BREP via `DocMirror::exportFile`→`export_file` RPC) +
  **Save All** + **Edit > Delete / Select All** (un-greyed; `featureDeleteSelection`/`featureSelectAll`).
- `bf9f760` **drag-drop to open** — OS file drop (chained `glfwSetDropCallback`) + Explorer→viewport
  ImGui drag/drop, both via the `requestOpenPath` pump.

### Still deferred (honest)
- **Wave 5C** theme/spacing DPI pass — **deferred to post-merge**: the sizing design-system
  (`scripts/gui_lint.py` + `host/src/ui/metrics.h`) lives on the `codex/gui-overhual` lane, NOT master.
  A freehand pass now would be lower-quality and duplicate/conflict; do it after branch reconciliation.
- **Host trigger for `refresh_derived`** — the engine mechanism + RPC exist; a FeatureManager
  "Rebuild Derived" context-menu item (and a "Paste as Derived" entry point) is a small host follow-up.
- File **Export for Drawing/ECAD tabs** (those keep their own DXF/PDF/Gerber paths); multi-untitled **Save All**.

## Process notes for the next session (IMPORTANT)
- **Agent `isolation: 'worktree'` branches from a STALE base here** — all worktrees came up at `3b78d79` (the merge-base with `codex/gui-overhual`), **92 commits behind master**. Two agents "succeeded" against it; their diffs would have reverted master. **Do NOT use worktree isolation in this repo** — run agents NON-isolated in the main tree.
- **Safe parallelism without worktrees:** engine (Python, `pytest`) ⟂ host (C++, `build.bat`) run concurrently fine (disjoint files + disjoint build/test). For two HOST agents: only safe if neither self-builds (a shared build compiles the other's half-edited files) — give them **disjoint write-sets** and have the **integrator build once**. String-keyed `ribbonDispatch("cmd_id")` decouples menu↔command waves at compile time.
- `app_state.h` / `CMakeLists.txt` are append-only shared — one owner per round.
- `cmd.exe /c "build.bat host"` sometimes prints only the shell banner via the Bash tool; use the **PowerShell tool** (`& cmd.exe /c "<abs path>\build.bat host"`) to capture real output + exit code.
- `--ui-screenshot` REQUIRES an output-path arg, else it falls through to windowed mode and never exits headlessly.
# GUI Rework Handoff

## 2026-06-14 — Codex left-manager pass

### Done
- Added a docked `FeatureManager##LeftPane` shell with SolidWorks-style vertical tabs:
  Explorer, FeatureManager, PropertyManager, ConfigurationManager.
- Refactored FeatureManager, PropertyManager, and Configurations to expose embedded body renderers
  while keeping their old standalone wrappers available.
- Wired PropertyManager forms to auto-select the PM tab and return to FeatureManager after close.
- Moved Configurations into the left manager instead of drawing it as a separate task-pane window.
- Replaced the three-item title menu with CAD-standard File / Edit / View / Insert / Tools / Window /
  Help menus, with enabled items routed through existing AppState or ribbon-dispatch paths.
- Changed status-bar height from a bare `30*scale` to `ImGui::GetFrameHeight()`.
- Documented the new left-pane and menu sizing/behavior contracts in `docs/GUI_STYLE_GUIDE.md`.

### Verification
- `.\build.bat host` passed.
- Headless smoke passed:
  `AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=tree AI-CAD.exe --ui-screenshot scratch/codex-left-pane-smoke.png`
- Manual screenshot inspection confirmed the left manager, menu bar, ribbon, dockspace, status bar,
  AI panel, and central editor all rendered without blank panels or obvious overlap.

### Deferred
- Explorer has a working workspace list and Open entry point, but not rename/copy/paste/delete yet.
- File > Save / Save As still points users to the existing FeatureManager save path rather than
  owning a unified top-level save command.
- The old FeatureManager body still contains its compact internal toolbar; it is functional but can
  be visually simplified now that the outer left-tab rail exists.
- Runtime DPI-change handling remains open from the audit backlog.

### Gotchas
- `FeatureManager##LeftPane` is the docked shell name; plain `FeatureManager` is still used by the
  legacy standalone wrapper and should not be docked by the default layout.
- New menu commands should use `st.ribbonDispatch` wherever possible so ribbon and menu behavior stay
  identical.

## 2026-06-14 — Codex feature/runtime/render hardening pass

### Done
- Added `scripts/feature_surface_audit.py`, a deterministic coverage gate for visible ribbon
  commands.
- Fixed PM angular fields for Extrude draft and Revolve angle so they use angle semantics instead of
  length dimensions.
- Added managed `llamacpp` provider support with optional AppData-installed `llama-server` and GGUF
  model discovery. See `docs/LOCAL_AI_RUNTIME.md`.
- Hardened offline mode so remote managed `llamacpp` endpoints are refused before server probing.
- Routed managed `llamacpp` through the micro-orchestration path for small local models.
- Fixed release/app staging for optional `llamacpp` and `models` payload folders.
- Fixed the center dock default/focus rule so part workflows surface the 3D Viewport, not the ECAD
  launchpad.
- Fixed renderer issues from audit: FBO completeness checks now bind the real offscreen FBO,
  Hidden Lines Removed uses a depth-only face pass, Section View presets match Z-up datum
  conventions, and Zoom-to-Selection / Normal-To pan to off-origin selections correctly.

### Verification
- `.\build.bat host` passed.
- `.\build.bat app` passed.
- `py -3 -m agent.test_offline_mode` passed.
- `py -3 -m py_compile installer\llamacpp.py agent\providers\llamacpp_provider.py agent\config.py agent\orchestrator\micro.py installer\build_release.py` passed.
- `python scripts\feature_surface_audit.py` passed.
- `python scripts\gui_lint.py --rule icons` passed.
- `python scripts\gui_lint.py --rule theme` passed.
- Headless tree smoke passed and was manually inspected:
  `AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=tree AI-CAD.exe --ui-screenshot scratch/codex-final-smoke-viewport.png`.

### Remaining High-Value Backlog
- `python scripts\gui_lint.py --rule scale` still reports hundreds of legacy fixed-pixel findings,
  mostly sketch/ECAD/drawing overlays and older panels.
- PropertyManager previews need a transactional pass: replacing an active PM should cancel or
  explicitly accept previews, preview changes should coalesce undo history, and OK should stay open
  on failed validation/rebuild.
- Large assembly pick IDs still need a remap/table path before body IDs exceed the current encoding.

## 2026-06-14 — Codex continuation pass

### Done
- Section View clipping now propagates into ID-buffer picking and hover/selection/feature overlays,
  so hidden clipped-away topology is not pickable/highlighted.
- `sch_annotate` is wired from the PCB ribbon to the existing ECAD `sch_annotate` op through
  `ecad_apply_ops`.
- PropertyManager replacement now rolls back an active preview before opening a different PM. If
  rollback fails, the original PM stays open.
- PropertyManager OK now blocks on missing refs/invalid dimensions/failed preview instead of closing
  as if the feature had committed.
- PM live-preview undo history now coalesces after the first successful preview, with separate
  fallback-cancel bookkeeping.
- View Selector menu actions now call `ViewSelector::open()` directly instead of injecting Space,
  so the menu works even when the cursor is not hovering the viewport image.

### Verification
- `.\build.bat host` passed.
- `.\build.bat app` passed.
- `python scripts\feature_surface_audit.py` passed with 128 explicit handlers discovered.
- `python scripts\gui_lint.py --rule icons` passed.
- `python scripts\gui_lint.py --rule theme` passed.
- `py -3 -m agent.test_offline_mode` passed.
- Headless tree smoke passed:
  `AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=tree AI-CAD.exe --ui-screenshot scratch/codex-continue-tree.png`.
- Headless section smoke passed:
  `AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=tree AI_CAD_SECTION=1 AI-CAD.exe --ui-screenshot scratch/codex-continue-section.png`.

### Remaining High-Value Backlog
- Large assembly pick IDs still need a remap/table path before body IDs exceed the current encoding.
- Viewport navigation should keep orbit/pan/MMB capture after the cursor leaves the image rect.
- ECAD still needs interactive placement for Via, Net Label, Power Port, Copper Zone, and
  cross-document source/board plumbing for Update Board.
- Evaluate dialogs still use hard-coded analysis inputs instead of PM-style selection forms.

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

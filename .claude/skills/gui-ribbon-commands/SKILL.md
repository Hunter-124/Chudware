---
name: gui-ribbon-commands
description: Chudware ribbon, feature tree, PropertyManager and command-routing specialist — adding/changing ribbon tabs, buttons, flyouts, feature-tree rows and context menus, PropertyManager parameter forms, and how a button click becomes an engine job. Use for any task about toolbar buttons, commands, the FeatureManager tree, or feature parameter forms. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Ribbon / Tree / PropertyManager Specialist

Scope: the command surface — ribbon, feature tree, property forms, and routing from click to
engine. Icons themselves (drawing/generating them) are `gui-icons`; the sketcher's in-canvas
tools are `gui-sketch`.

## File map

| File | Role |
|---|---|
| `host/src/ui/ribbon_def.cpp/.h` | **Append-only** static command table (`RibbonCmd` rows). `ribbon.cpp` and `app.cpp` call `getRibbonCmds()` |
| `host/src/ui/widgets/ribbon.cpp/.h` | Ribbon widget: tabs, groups, big/small buttons, flyouts, layout & drawing |
| `host/src/ui/feature_tree.cpp` (~3.9k LOC) | FeatureManager tree: hierarchy, suppress/reorder, rollback bar, badges, context menus |
| `host/src/ui/property_manager.cpp` | PropertyManager panel shell (per-doc form state) |
| `host/src/ui/pm_forms.cpp` | Form schemas per feature type — field definitions the PropertyManager renders |
| `host/src/ui/command_search.cpp` | Command palette / search over the ribbon table |
| `host/src/app.cpp` (~3.2k LOC, **integrator-owned**) | Frame loop; dispatches ribbon commands via `onCommand(kCmd_Xxx, args)` |

## RibbonCmd schema (ribbon_def.h)

```cpp
struct RibbonCmd {
  const char* cmd_id;  // snake_case unique id
  const char* label;   // ≤12 chars fits a big button
  const char* icon;    // icon name WITHOUT @32/@20 suffix — must exist in contracts/icons.md
  const char* tab;     // "Features" | "Sketch" | "Evaluate" | "Simulation" | ...
  const char* group;   // group label within the tab
  bool big;            // 32px icon over label vs 20px + label right
  const char* sub_of;  // parent cmd_id for flyout/dropdown sub-commands, else nullptr
  bool stretch;        // reserved-but-unwired (renders disabled)
};
```

**Append rows, never edit existing ones** (header says so; other cards depend on row order).

## Command flow

Ribbon click → `onCommand(kCmd_..., args)` in `app.cpp` → `AppState::runJob()` enqueues →
`WorkerClient::sendRpc()` (`host/src/worker_client.h`, format in `contracts/worker_rpc.md`) →
Python kernel executes → result drained per-frame by `host/src/core/engine_events.cpp` →
`AppState`/`DocTab` updated → tree & viewport repaint.

## Adding a command, end to end

1. Icon exists? Check `contracts/icons.md`; if not, that's a `gui-icons` task first.
2. Append a `RibbonCmd` row in `ribbon_def.cpp` (right tab/group, `sub_of` for flyouts).
3. Handle the command id in `app.cpp`'s dispatch (coordinate with orchestrator — shared file).
4. If it opens a parameter form: add the schema to `pm_forms.cpp`; op fields must match
   `contracts/feature_params.md` / `contracts/doc_ops.md`.
5. If it creates a tree node: `feature_tree.cpp` renders from the doc's feature list — usually
   automatic once the engine op lands in the document.

## Conventions & gotchas

- Feature ops and their parameters are contract-frozen: `contracts/doc_ops.md`,
  `contracts/feature_params.md` (append-only).
- Per-doc UI state (active feature, form state) lives on `AppState::DocTab` in
  `host/src/core/app_state.h` (integrator-owned — request seam edits, don't sprawl).
- Colors from `ui_theme` only; icon names must pass `scripts/gui_lint.py --rule icons`.
- Interaction/gesture language: `contracts/interaction.md`.

## Assembly component tree (feature_tree.cpp `drawAssemblyTree`)

- Each component expands into a **nested read-only** mini feature-tree mirroring the part env:
  a "Reference" folder (planes/axes/points) + history features with **sketches nested under
  their consuming feature** (Extrude1 ▸ Sketch). Rendered by file-static helpers
  `drawComponentFeatureTree` / `drawComponentFeatureRow` / `componentScanSketchRefs` (they
  borrow the part-env consumption analysis but do NOT reuse `drawOneRow`, which is welded to
  live part state). Data comes from `ui::componentFeatures()` (`component_features.*`), whose
  `ComponentFeature` carries `folder` + full `raw` json. Non-Reference folders
  (Dress-up/Patterns) aren't nested yet; only native `.step+` components populate (dumb STEP
  imports have no feature list).
- Component right-click menu: **"Edit"** (placement/mates → sets
  `AppState::requestEditComponentMates`, consumed by `asm_panels.cpp` to focus the mate form)
  and **"Edit Model"** (in-context model edit → `AppState::enterComponentEdit(cid, absPath)`);
  double-click a component row also enters in-context edit. Ghost/edit-mode machinery is in
  gui-viewport.
- **Viewport → tree sync** (`ui::featureTreeRevealPick`, called from app.cpp's selection
  observer the moment a pick is added — never gated on the `doc_resolve_pick` reply): part docs
  (and in-context edit) select the owner-map feature; assembly component rows light from the live
  `st.selection` (body id = ordinal among non-suppressed components, `featureTreeAssemblyComponent`;
  pattern instances via the resolve reply → `featureTreeNoteAssemblyPick`). A pending
  `treeScrollToSelection` force-opens the Features section / folder / instance group and scrolls.
  Assemblies have an empty engine `owner_map`, so nested component feature rows are not lit.
  Selected rows (every kind) paint via `fmRowSelectedPaint` (accentSoft fill + accent glow).
- The AI "Name & group" tree-header button was removed (sidelined); `nameAndGroupFeatures`
  is kept `[[maybe_unused]]` for later re-wiring.

## Build & verify

`./build.sh` from repo root must compile; run `scripts/gui_lint.py --rule icons` if you
referenced any icon. Never run git. Return files changed + a one-line user check, e.g.
"Features tab > new 'Wrap' button in the Modify group opens its PropertyManager form."

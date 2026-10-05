---
name: gui-ribbon-commands
description: Chudware ribbon, feature tree, PropertyManager and command-routing specialist — adding/changing ribbon tabs, buttons, flyouts, feature-tree rows and context menus, PropertyManager parameter forms, and how a button click becomes an engine job. Use for any task about toolbar buttons, commands, the FeatureManager tree, or feature parameter forms. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Ribbon / Tree / PropertyManager Specialist

Scope: the command surface — ribbon, feature tree, property forms, and routing from click to
engine. Icons themselves (drawing/generating them) are `gui-icons`; the sketcher's in-canvas
tools are `gui-sketch`. Shared-file ownership, waves and verification: `gui-orchestrator`.

## File map

| File | Role |
|---|---|
| `host/src/ui/ribbon_def.cpp/.h` | **Append-only** static command table (`RibbonCmd` rows) + `ribbonCommandCapability()`. `ribbon.cpp` and `app.cpp` call `getRibbonCmds()` |
| `host/src/ui/widgets/ribbon.cpp/.h` | Ribbon widget: tabs, groups, big/small buttons, flyouts, layout & drawing |
| `host/src/ui/feature_tree.cpp/.h` | FeatureManager tree: hierarchy, suppress/reorder, rollback bar, badges, context menus, viewport→tree sync |
| `host/src/ui/property_manager.cpp` | PropertyManager panel shell (per-doc form state); `PropertyManager::openForCommand` |
| `host/src/ui/pm_forms.cpp` | Form schemas per feature type — field definitions the PropertyManager renders |
| `host/src/ui/command_search.cpp` | Command palette / search over the ribbon table |
| `host/src/app_commands.cpp/.h` (**integrator-owned**) | `buildRibbonCommandRouter`: ordered `CommandRouter` rules mapping command ids/prefixes to the owning module; `CommandRouter::authorize` capability gate |
| `host/src/app.cpp` (**integrator-owned**) | Frame loop; `dispatchCmd` (pre/post policy around the router) and the app-level command hooks (`RibbonCommandHooks`: Edit clipboard verbs, Make Drawing, Display style, Appearance, Sheet Metal) |

## RibbonCmd schema (ribbon_def.h)

```cpp
enum class CommandCapability { Mutate, Inspect, Export };

struct RibbonCmd {
  const char* cmd_id;  // snake_case unique id
  const char* label;   // ≤12 chars fits a big button
  const char* icon;    // icon name WITHOUT @32/@20 suffix — must exist in contracts/icons.md
  const char* tab;     // "Features" | "Sketch" | "Evaluate" | "Simulation" | ...
  const char* group;   // group label within the tab
  bool big;            // 32px icon over label vs 20px + label right
  const char* sub_of;  // parent cmd_id for flyout/dropdown sub-commands, else nullptr
  bool stretch;        // reserved-but-unwired (renders disabled)
  CommandCapability capability = CommandCapability::Mutate;
};
```

**Append rows, never edit existing ones** (header says so; other cards depend on row order).
`capability` is fail-closed: unclassified and unknown ids are `Mutate`, which read-only /
entitlement mode blocks. Mark a row `Inspect` (view/display/measure only) or `Export` (writes a
copy, never mutates the open document) only when that is strictly true — a mutating command
marked Inspect/Export silently unlocks editing in read-only mode.

## Command flow

Ribbon click (or shortcut bar / gesture wheel via `st.ribbonDispatch`) → `dispatchCmd` in
`app.cpp` → `CommandRouter::authorize` (Mutate commands refused in read-only mode) →
`CommandRouter::dispatch` walks rules in registration order; the first handler that returns true
consumes the command — a workspace module entry point (`SketchMode::onRibbonCommand`,
`PropertyManager::openForCommand`, `AsmPanels::handleCommand`, `camHandleCommand`, …) or an
app-level hook → engine RPC over `st.worker` (`WorkerClient`, `host/src/worker_client.h`; method
contracts in `contracts/worker_rpc.md`) → `engine::call` (`services/worker/engine_backend.cpp`)
→ `chudware-hostd` executes in libchudware → `doc.changed` and other events drained per frame by
`host/src/core/engine_events.cpp` → `AppState`/`DocTab` updated → tree & viewport repaint.

## Adding a command, end to end

1. Icon exists? Check `contracts/icons.md`; if not, that's a `gui-icons` task first.
2. Append a `RibbonCmd` row in `ribbon_def.cpp` (right tab/group, `sub_of` for flyouts,
   `capability` only if it is genuinely Inspect/Export).
3. Route it: most ids already reach their module through an existing prefix rule (`sk_`, `dwg_`,
   `asm_`, `cam_`, `reveng_`, `sim_`, `eval_`, PM forms) — handle the id in that module's entry
   point. A new family needs a rule in `buildRibbonCommandRouter` (`app_commands.cpp`; order is
   semantics, audit the fall-through cases) — integrator-owned, request it.
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
- The Features section header hosts only the Add Folder button (`folder_new` icon).

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
  Assemblies have an empty engine `owner_map`; instead the assembly `doc_resolve_pick` reply's
  `owner_feature` (feature inside the component's part) goes through
  `featureTreeNoteAssemblyPick(st, hit, component, owner)` → the nested component feature row
  lights while that pick stays selected, and its component row / Dress-up folder open once + scroll.
  Selected rows (every kind) paint via `fmRowSelectedPaint` (accentSoft fill + accent glow).

Scenario that exercises this surface: `CHUDWARE_SHOT=m1` (see gui-orchestrator).

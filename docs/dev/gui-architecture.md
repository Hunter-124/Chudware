# GUI architecture

The desktop GUI is a native C++ Dear ImGui application. All production GUI code lives in
`host/src/`, which is the sole GUI source tree (the same tree compiles to WASM for the
browser build). This document maps the subsystems, the shared files that need care, the
headless scenario harness, and the conventions for the command surface, viewport, sketcher
and icons. Visual-system rules (colors, metrics, `ui::kit`, lint) are in
[gui-theme-layout.md](gui-theme-layout.md); workspace tabs are in
[gui-workspaces.md](gui-workspaces.md); the Evaluate tools are in
[evaluate-tools.md](evaluate-tools.md).

## Subsystem map

| Change concerns | Primary files |
|---|---|
| Icons, atlas, icon generation | `assets/make_icons.py`, `scripts/icon_pipeline.py`, `host/src/ui/icons*.cpp` |
| 3D viewport, rendering, picking, camera, HUD | `host/src/ui/viewport_panel.cpp`, `host/src/renderer.cpp`, `host/src/render/` |
| Ribbon, feature tree, PropertyManager, command routing | `host/src/ui/ribbon_def.cpp`, `host/src/app_commands.cpp`, `host/src/ui/widgets/ribbon.cpp`, `host/src/ui/feature_tree.cpp`, `host/src/ui/property_manager.cpp`, `host/src/ui/pm_forms.cpp` |
| 2D and 3D sketcher | `host/src/ui/sketch_mode.cpp`, `host/src/ui/sketch3d_mode.cpp` |
| Workspace tabs (Parts, Assembly, CAM, ECAD, PLM, RevEng, AI, Drawing, Simulation, Firmware, Manufacture) | see [gui-workspaces.md](gui-workspaces.md) |
| Theme, layout, DPI, modals, status/title bar, toasts | `host/src/ui_theme.cpp`, `host/src/ui/layout.cpp`, `host/src/ui/metrics.h`, `host/src/ui/settings_modal.cpp` |
| Shared widget/motion vocabulary | `host/src/ui/widgets/kit.h/.cpp`, `host/src/ui/anim.h/.cpp` |

## Shared files

These files are shared by many features; change them deliberately and keep edits small:

- `host/src/app.cpp`: frame loop, panel dispatch, app-level command hooks.
- `host/src/app_commands.cpp`: the ordered command router; registration order is semantics.
- `host/src/core/app_state.h`: the central data model. Per-document UI state belongs on
  `AppState::DocTab`; add narrow seams rather than sprawling members.
- `host/src/ui/ribbon_def.cpp`: append-only command table.
- `host/src/ui/metrics.h`: append-only sizing tokens.
- `host/src/ui_theme.cpp/.h`, `host/src/ui/widgets/kit.h/.cpp`, `host/src/ui/anim.h/.cpp`: the
  app-wide color, widget and motion vocabularies. Panels consume them; a new primitive is its
  own change, landed before the panels that use it (the same ordering as a new icon before
  the ribbon button that uses it).
- `contracts/`: append-only.

## Shared panel patterns

- Panel shape: `class XxxPanel { void draw(AppState&); void drawBody(AppState&); }`; global
  dialogs are `void drawXxxDialog(AppState&)`.
- Engine data arrives through the per-frame drain in `host/src/core/engine_events.cpp`; panels
  render from `AppState` and never call the worker synchronously. RPCs go through
  `WorkerClient` (`host/src/worker_client.h`; methods in `contracts/worker_rpc.md`).
- Streaming logs stick to the bottom via `ui::chatLogStickToBottom("<region id>")` with a
  unique id per surface (two surfaces sharing an id fight over the flag).

## Verification: lint and the scenario harness

```bash
./build.sh                                   # or: ninja -C host/build-linux Chudware
python3 scripts/gui_lint.py                  # all rules; see gui-theme-layout.md
CHUDWARE_SHOT=<scenario> xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/<scenario>.png
```

The ninja target is `Chudware`; the binary is lowercase `chudware`. `CHUDWARE_SHOT` runs a
scripted scenario from `host/src/ui/scenarios.cpp` headlessly through the real engine and
writes a capture; drop `xvfb-run -a` and set `CHUDWARE_SHOT_HEADED=1` for a visible window.
Common scenarios: `m1` (part build, edit, undo: viewport, tree, PropertyManager), `sketch`,
`sketch3d`, `appearance` (theme/glass), `workspaces`, `manufacture`, `topology`, `iconpipe`,
`reveng_splat`. `CHUDWARE_IDAUDIT=1` adds the ImGui duplicate-ID/clipping audit to any
scenario.

A scenario is the functional gate: it drives the changed path end to end. A capture's
existence is not visual proof; inspect the changed surface. Self-checking scenarios (for
example `topology`, `pipedraw`, `simaudit`) write reports and exit non-zero on failure. When
a workspace gains a major new flow, add a scenario for it.

Run build commands one at a time; parallel `./build.sh`/`ninja` runs race on the Ninja lock.

## Ribbon, feature tree and command routing

| File | Role |
|---|---|
| `host/src/ui/ribbon_def.cpp/.h` | Append-only `RibbonCmd` table plus `ribbonCommandCapability()` |
| `host/src/ui/widgets/ribbon.cpp/.h` | Ribbon widget: tabs, groups, big/small buttons, flyouts |
| `host/src/ui/feature_tree.cpp/.h` | FeatureManager tree: hierarchy, suppress/reorder, rollback bar, badges, context menus, viewport-to-tree sync |
| `host/src/ui/property_manager.cpp` | PropertyManager shell (per-doc form state); `PropertyManager::openForCommand` |
| `host/src/ui/pm_forms.cpp` | Per-feature form schemas |
| `host/src/ui/command_search.cpp` | Command palette over the ribbon table |
| `host/src/app_commands.cpp/.h` | `buildRibbonCommandRouter`: ordered rules mapping ids/prefixes to modules; `CommandRouter::authorize` |

`RibbonCmd` fields: `cmd_id` (unique snake_case), `label` (12 characters fit a big button),
`icon` (bare name from `contracts/icons.md`), `tab`, `group`, `big` (32 px icon over label vs
20 px beside it), `sub_of` (parent id for flyouts), `stretch` (reserved, renders disabled), and
`capability` (`Mutate`, `Inspect`, `Export`). Append rows; never edit or reorder existing
ones. `capability` is fail-closed: unclassified ids are `Mutate`, which read-only mode blocks.
Mark a row `Inspect` or `Export` only when it strictly never mutates the open document.

**Command flow.** Ribbon click (or shortcut bar / gesture wheel) → `dispatchCmd` in `app.cpp`
→ `CommandRouter::authorize` → `CommandRouter::dispatch` walks rules in order until a handler
consumes the id (a module entry point such as `SketchMode::onRibbonCommand`,
`PropertyManager::openForCommand`, `AsmPanels::handleCommand`, or an app-level hook) → engine
RPC over `WorkerClient` → `chudware-hostd` executes in libchudware → `doc.changed` events
drained per frame → `AppState`/`DocTab` update → tree and viewport repaint.

**Adding a command.**

1. Ensure the icon exists in `contracts/icons.md` (otherwise add it first; see Icons).
2. Append a `RibbonCmd` row (tab, group, `sub_of` for flyouts, capability only if genuinely
   Inspect/Export).
3. Route it. Most ids reach their module through an existing prefix rule (`sk_`, `dwg_`, `asm_`,
   `cam_`, `reveng_`, `sim_`, `eval_`); a new family needs a rule in `buildRibbonCommandRouter`
   with the fall-through cases audited.
4. A parameter form goes in `pm_forms.cpp`; op fields must match `contracts/feature_params.md`
   and `contracts/doc_ops.md` (both append-only).
5. Tree nodes render from the document's feature list automatically once the op lands.

**Assembly component tree** (`feature_tree.cpp` `drawAssemblyTree`). Each component expands
into a nested, read-only feature tree (a Reference folder plus history features with sketches
nested under their consuming feature), fed by `ui::componentFeatures()`
(`component_features.*`). Only native `.step+` components populate it. Component context menu:
**Edit** (placement/mates) and **Edit Model** (in-context edit via
`AppState::enterComponentEdit`).

**Viewport-to-tree sync.** `ui::featureTreeRevealPick` is called from the selection observer
as soon as a pick is added, never gated on the `doc_resolve_pick` reply. Part documents select
the owner-map feature; assemblies (which have no engine owner map) use the resolve reply's
`owner_feature` through `featureTreeNoteAssemblyPick`.

## Viewport

| File | Role |
|---|---|
| `host/src/ui/viewport_panel.cpp/.h` | Input, gizmo, selection UX, per-frame 3D draw |
| `host/src/renderer.cpp/.h` | OpenGL renderer: orbit camera, mesh render, shaders, transparent thumbnail path |
| `host/src/render/id_pass.cpp/.h` | ID-pass picking: face/edge IDs to an offscreen buffer, read back under the cursor |
| `host/src/render/overlays.cpp/.h` | Native GL overlays: selection washes, simulation indicators, CAM path batches |
| `host/src/ui/sim/sim_scene.h/.cpp` | Viewport-to-simulation scene dispatcher (`ui::simscene`) |
| `host/src/render/splat_renderer.cpp/.h`, `re_points.cpp` | Gaussian splat and point-cloud rendering |
| `host/src/render/aicm_loader.cpp`, `host/src/mesh.cpp` | `.aicm` mesh loading and host mesh data |

**Data flow.** libchudware tessellates the document into an AICM file → `doc.changed` →
`AppState::loadDocMesh`/`loadDocMeshAsync` feed the renderer, ID pass and overlays → a click
reads back an ID → a `render::PickHit` joins `AppState::selection` → PropertyManager and
feature ops consume it. Background tabs keep their parsed mesh on `DocSession::cachedDocMesh`.

- **Picking is ID-based** (topology IDs from libchudware's tessellation), never a raycast
  against triangles. Anything pickable needs an ID-pass representation.
- The transparent thumbnail path in `renderer.cpp` is used by the icon pipeline; changing its
  background/alpha behavior breaks icon generation.
- **Framing.** The 3D image is full-bleed behind the glass panes. `viewport_panel.cpp` computes
  the clear region between the overlay columns each frame and calls
  `Renderer::setFramingRegion(shiftX, shiftY, scale)` (a lens shift; picking and overlays
  follow). Wheel/area zoom pivot on that region's centre.
- Pane controls that stand for a face call `ui::claimViewportHoverFace(body, face)` every
  hovered frame; UI code never writes `AppState::hover` directly.
- **CAM overlays** are drawn by `viewport_panel.cpp` through `Overlays` inside the renderer's
  colour FBO: Manufacture-only, excluded from the ID pass, section-clipped and depth-aware.
- **Simulation scene seam.** `ui::simscene` dispatches to the open panel's piping or hydraulics
  scene; the viewport never reaches into those panels. Collection and drawing run without a
  document mesh (empty parts are valid network canvases). Armed tools consume only clicks below
  ImGui's drag threshold; drags, MMB and wheel remain navigation. Scene pointer hover uses
  `ImGuiHoveredFlags_NoNavOverride`. Auxiliary scene bounds enlarge framing without moving the
  orbit centre; zoom clamps use `maxSceneZoom`, not a hard-coded limit.
- **Bottom-anchored HUD cards** publish their rectangle with `publishViewportHudRect()` so the
  orientation triad and dimensions readout lift above them.
- **In-context component edit.** Edit Model makes the component's part document active and draws
  the assembly as a translucent ghost (`captureCurrentAsGhost()` reuses the uploaded GL
  buffers; `setGhostTransform` receives the inverse component placement). The ghost is
  excluded from picking. Exit does an asynchronous save-then-rebuild of the assembly.
- **Gaussian splats.** The loader accepts validated PLY (log scales, wxyz quaternions, opacity
  logits, SH degree 0-3), projects full anisotropic covariance, and caps at 2 000 000 Gaussians
  and 2 GiB input. Camera motion updates only sorted indices. Image-plane NDC is clamped to
  ±1.3 before differentiating, matching Brush. Worker threads parse files but never touch GL.
  Real-GL smoke: `CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=reveng_splat CHUDWARE_REPO="$PWD"
  ./host/build-linux/bin/chudware --ui-screenshot scratch/re.png`; CPU contract:
  `host/tests/test_splat_renderer.cpp`.
- Headless render modes for testing: `chudware --render <in.stl> <out.png>` and
  `--contact <in.stl> <out.png>`.

## Sketcher

| File | Role |
|---|---|
| `host/src/ui/sketch_mode.cpp/.h` | The 2D sketcher: tool state machine, gestures, entity drawing, relation glyphs, smart dimensions, trim/extend/offset/convert/mirror/pattern |
| `host/src/ui/sketch3d_mode.cpp/.h` | 3D sketcher: free-space curves in world coordinates |
| `host/src/ui/sketch_relations_panel.cpp/.h` | Display/Delete Relations panel over a plain `RelationRow` model |
| [`contracts/interaction.md`](../../contracts/interaction.md) | The gesture language (click/drag/hover semantics) |
| [`contracts/sketch3d_gui_contract.md`](../../contracts/sketch3d_gui_contract.md) | Frozen 3D-sketcher UX spec |

- The sketcher sends ops to `chudware-hostd`, where libchudware owns the constraint solver;
  solved geometry returns through the event drain. Touching endpoints are auto-coincident on
  every edit, so UI flows do not add redundant coincident constraints.
- Each tool is a state-machine branch keyed by its ribbon command id; the tool set mirrors the
  Sketch sections of `contracts/icons.md`. New gestures need an appended entry in
  `contracts/interaction.md`.
- Relations to model geometry read resolved selections from `st.selection` (reuse
  `item.selector`), project them with one associative `sketch_convert` (construction, fixed),
  and add the relation in the same batch. World axes parallel to the sketch u/v map to the
  solver's virtual `axis.h`/`axis.v`.
- Performance: batch picked refs into one op, never block on `resolvePick` when the selection
  already carries a selector, and keep per-frame work dirty-flagged.
- Inactive sketches ghost in the viewport unless a built feature consumes them. Consumption has
  one definition, `core::consumedSketches` (`host/src/core/sketch_consumers.h`), shared with the
  FeatureManager's sketch nesting. Ghost frames come from the engine's `doc_sketch_overview`,
  which already applies the sketch's `params.offset`; never add it again in the host.
- `CHUDWARE_SHOT=sketch` prints solved coordinates; headless captures frame sketches too
  closely, so judge overlays from the printed numbers plus popup/panel captures.

## Icons

**Procedural line-art icons** come from `assets/make_icons.py` (PIL, deterministic, so
regeneration is diffable): drawn at 8x supersample into `assets/icons/atlas_dark.png`,
`assets/icons/atlas_light.png` and `assets/icons/atlas.json`, at 32 px (big buttons) and 20 px
(small buttons, tree rows), named `name@32`/`name@20`. **Both atlases share one pixel layout**,
so a theme switch swaps the texture while UVs stay constant. Helpers: `assets/pack_icons.py`,
`assets/test_icons.py`, `assets/test_pack_icons.py`.

**Screenshot icons** come from `scripts/icon_pipeline.py`: a recipe drives the real engine
headlessly (`CHUDWARE_SHOT=iconpipe` with `CHUDWARE_ICON_RECIPES=<json>`), renders through the
transparent thumbnail path, and fits each render into its cells in both atlases, leaving every
other cell byte-identical. Run `python3 scripts/icon_pipeline.py --render` (`--only a,b`,
`--list`, `--inject-only`). Icons carry no baked background. CAM tiles
(`scripts/cam_toolpath_icons.py`) render real `cam_generate` toolpaths gated by `cam_simulate`;
an icon is never faked. A failed build step yields an empty tile on
`scratch/icon_contact_sheet.png`, which should be regenerated for review.

**Runtime.** `host/src/ui/icons.cpp` loads the atlas (`getIcon(name, size)`), `syncIconTheme()`
swaps it on theme toggle, and `icons_sdf.cpp` rasterizes line art crisply at any DPI. Consumers
use bare names; size is chosen by context. The build copies the atlas to
`host/build-linux/bin/assets/icons/`.

**Adding an icon.** Append the name to [`contracts/icons.md`](../../contracts/icons.md) (names
never change or disappear), add a draw function or recipe, regenerate, confirm both `@32` and
`@20` cells exist in both atlases, reference it by name, and run
`python3 scripts/gui_lint.py --rule icons`.

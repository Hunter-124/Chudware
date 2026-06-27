# AI-CAD — GUI Rewrite Design Spec

> Clean-room rebuild of the desktop GUI. Branch `feat/gui-rewrite`. New code lives under `app/`.
> Stack: **Dear ImGui (docking) + GLFW + OpenGL** (MIT/zlib — no copyleft). Reuses the existing
> engine bridge + OpenGL viewport from `host/`; rebuilds the *shell, IA, command taxonomy, theme,
> chrome, and agent panel* from scratch. Windows-first, structured for Linux/macOS.

This document is the single source of truth for the new UI. It supersedes
`docs/GUI_REWORK_PLAN.md` and `docs/GUI_STYLE_GUIDE.md` (those describe the SolidWorks-clone layout
we are replacing).

---

## 0. Why a rewrite

The current GUI is a faithful **SolidWorks clone**: a CommandManager ribbon with 11 contextual tabs,
a "FeatureManager" design tree, a "PropertyManager" that hijacks the left pane, and SW's 7-menu bar.
This is (a) a trade-dress / IP liability and (b) genuinely scattered — 150 commands spread across 11
tabs, the same operation split into 3 buttons (Extrude / Extrude Cut / Extruded Surface), two
competing chat UIs (`legacy_chat` + `activity_panel`), and doc-tab logic duplicated in two places.

Goals: **distinct modern identity · one coherent command taxonomy · zero capability loss · the AI
agent as a first-class, transparent collaborator.**

---

## 1. Design principles

1. **Verb-first, not noun-first.** Tools are grouped by what you're *doing* (Create / Modify /
   Pattern / Construct / Inspect), not by SolidWorks' nouns ("Features", "Dress-up"). Verb grouping
   is how a human reasons about the next action and is legally + visually distinct from SW.
2. **One operation, one tool.** Add/Cut/Intersect/Surface are *options inside* a tool, not separate
   buttons. Extrude is **one** button with an operation selector. This collapses ~30 buttons.
3. **Workspaces, not tab soup.** A single doc-type-aware workspace switcher replaces 11 ribbon tabs.
   You see only the tools relevant to what you're building.
4. **Nothing is ever buried.** A command palette (`Ctrl/⌘+K`) reaches *every* command by fuzzy search,
   so simplifying the visible surface never hides capability.
5. **The agent is a panel, not a popup.** A persistent, transparent agent surface shows thoughts,
   tool calls, and results inline — like Claude Code / the VS Code agent.
6. **Quiet, modern, dark-first.** Neutral desaturated UI, one vivid accent, generous spacing,
   rounded corners, subtle elevation, smooth motion. Not SW's light blue-grey.
7. **DPI-correct by construction.** One scale value, fonts pre-rasterized at scale, style scaled
   once. (Carry over the one sound idea from the old `GUI_STYLE_GUIDE.md` §1.)

---

## 2. Information architecture (shell regions)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  TITLE BAR:  ☰  logo   [ Part1 ×][ Bracket ×][ + ]      ⌕ search ⌘K     – ▢ ✕      │  ← custom chrome, portable
├───┬──────────────────────────────────────────────────────────────────┬───────────┤
│ A │  WORKSPACE BAR:  ◉ Model  Sheet Metal  Weldment  Surface  Simulate │           │
│ C │  CONTEXT TOOLBAR:  [Sketch] [Create▾] [Modify▾] [Pattern▾] [Construct▾]  …Inspect│           │
│ T ├──────────────────────────────────────────────────────────────────┤   AGENT    │
│ I │                                                                    │   PANEL    │
│ V │                                                                    │           │
│ I │                      VIEWPORT  (3D / 2D / schematic / board)        │ transparent│
│ T │                      ┌─ in-canvas nav gizmo (corner)                │  thoughts  │
│ Y │   LEFT DOCK          │  in-canvas mini-toolbar (context)            │  + actions │
│   │   (rail-driven:      │  selection HUD                               │  + chat    │
│ R │    Tree / Bodies /   │                                              │           │
│ A │    Files / Configs)  │                                              │  ┌───────┐ │
│ I │                      │                                              │  │composer│ │
│ L │                      │                                              │  └───────┘ │
├───┴──────────────────────────────────────────────────────────────────┴───────────┤
│  STATUS BAR:   ● ready   units: mm   sel: 2 edges   snap: ⊿   ░ rebuild 100%        │
└──────────────────────────────────────────────────────────────────────────────────┘
```

| Region | Replaces | Purpose |
|---|---|---|
| **Title bar** | `win_chrome` + `title_bar` + SW menu | App menu (☰), logo, **single** doc-tab strip, global search/palette, window buttons. Portable custom chrome. |
| **Activity rail** (left, ~48px) | the SW "left task pane tabs" | Switches the left dock content: **Tree** (history), **Bodies/Layers**, **Files**, **Configurations**, **Search-in-doc**. VS-Code-style, not a SW vertical tab strip. |
| **Left dock** | FeatureManager/PropertyManager pane | Content driven by the rail. The model **Tree** (renamed from "FeatureManager"). |
| **Workspace bar** | 11 ribbon tabs | Doc-type-aware mode switcher (§3). One row, ≤6 chips. |
| **Context toolbar** | the ribbon body | Flat, grouped tool buttons for the active workspace. Split-buttons open verb-group flyouts. |
| **Viewport** | `viewport_panel` + renderer | The reused, optimized OpenGL canvas (§6). Floating in-canvas toolbar + nav gizmo + selection HUD. |
| **Inspector** | "PropertyManager" | Tool/feature editing form. Appears as an **overlay card docked to the left dock top** (or floating near the viewport) when a tool is active — *not* a pane that replaces the tree. Renamed "Inspector". |
| **Agent panel** (right) | `legacy_chat` + `activity_panel` (both deleted) | The single transparent AI surface (§5). Dockable, collapsible. |
| **Status bar** | `status_bar` | Units, selection summary, snap state, async progress, rebuild state. |

**Docking:** ImGui docking branch. Default layout = left dock (tree) · center (viewport) · right
dock (agent). Every panel is dockable/floatable/closable; layout persists per user. **No duplicate
doc-tab system** — one `DocTabBar` in the title bar drives everything.

---

## 3. The new command taxonomy (the core of the redesign)

Two orthogonal axes replace the flat 11-tab ribbon:

**Axis 1 — Workspace** (set by doc type + user choice). The workspace bar shows only these:

| Doc type | Workspaces offered |
|---|---|
| **Part** | **Model** · *Sheet Metal* · *Weldment* · *Surface* · **Simulate** |
| **Assembly** | **Assemble** · **Simulate** |
| **Drawing** | **Drafting** |
| **Schematic** | **Schematic** |
| **PCB** | **Board** |

(Italic = specialized workbench: still one chip, but visually secondary. `Sheet Metal / Weldment /
Surface` swap the **Create/Modify** groups to their specialized tools while keeping the same verb
structure. `Inspect` tools are a persistent right-aligned cluster in every 3D workspace, not a mode.)

**Axis 2 — Verb groups** (consistent across every 3D workspace):

`Sketch · Create · Modify · Pattern · Construct · Inspect · Output`

Each group is a split-button on the context toolbar; clicking the arrow opens a flyout of its tools.
This is **6 buttons** where SW has 11 tabs × ~6 groups.

### 3.1 Full old→new mapping (zero capability loss)

> Every one of the ~150 current `cmd_id`s maps to a home. "Op variants" means several old buttons
> collapse into one tool whose operation is an option in the Inspector.

#### Workspace: **Model** (Part)

| Group | Tool (new) | Absorbs (old cmd_ids) |
|---|---|---|
| Sketch | **New Sketch** | `sketch_new` (sketch tools become the contextual Sketch workspace, §3.2) |
| Create | **Extrude** | `extrude_boss`, `extrude_cut`, `surf_extrude` *(op: New/Add/Cut/Intersect/Surface)* |
| Create | **Revolve** | `revolve_boss`, `revolve_cut`, `surf_revolve` *(op variants)* |
| Create | **Sweep** | `sweep_boss`, `sweep_cut`, `surf_sweep` *(op variants)* |
| Create | **Loft** | `loft_boss`, `loft_cut`, `surf_loft` *(op variants)* |
| Create | **Rib** | `rib` |
| Create | **Hole** | `hole_wizard` |
| Create | **Coil/Helix** | `curve_helix` |
| Create | **Planar/Boundary Surface** | `surf_planar`, `surf_boundary`, `surf_fill`, `surf_ruled` *(Surface workspace promotes these)* |
| Modify | **Fillet** | `fillet` |
| Modify | **Chamfer** | `chamfer` |
| Modify | **Shell** | `shell` |
| Modify | **Draft** | `draft` |
| Modify | **Split** | `split` |
| Modify | **Combine** (boolean) | `boolean_combine`, `boolean_subtract`, `boolean_intersect` *(op: Add/Subtract/Intersect)* |
| Modify | **Offset / Thicken / Knit** | `surf_offset`, `surf_thicken`, `surf_knit` *(Surface ws)* |
| Modify | **Face tools** | `surf_trim`, `surf_untrim`, `surf_delete_face`, `surf_replace_face` *(Surface ws)* |
| Pattern | **Rectangular Pattern** | `linear_pattern` |
| Pattern | **Circular Pattern** | `circular_pattern` |
| Pattern | **Mirror** | `mirror_feature` |
| Construct | **Plane / Axis / Point / Coord Sys** | `ref_plane`, `ref_axis`, `ref_point`, `ref_csys` |
| Inspect | **Measure** | `eval_measure` |
| Inspect | **Properties** | `eval_mass_props`, `eval_section_props` |
| Inspect | **Analyze** | `eval_draft`, `eval_thickness`, `eval_curvature`, `eval_zebra`, `eval_symmetry` |
| Inspect | **Check / Interference** | `eval_check`, `eval_interference`, `eval_geometry` |
| Inspect | **Profiler** | `prof_toggle` |
| Output | **Make Drawing** | `make_drawing` |
| Output | **Export** | (STEP/IGES/STL/3MF/BREP — File menu + here) |

#### Workspace: **Sheet Metal** (Part, swaps Create/Modify)

| Group | Tool | Absorbs |
|---|---|---|
| Create | **Base Flange** | `sm_base_flange` |
| Create | **Edge Flange** | `sm_edge_flange` |
| Create | **Hem** | `sm_hem` |
| Create | **Jog** | `sm_jog` |
| Modify | **Flatten** | `sm_flat_pattern` |
| Output | **Export Flat DXF** | `sm_export_flat_dxf` |

#### Workspace: **Weldment** (Part, swaps Create/Modify)

| Group | Tool | Absorbs |
|---|---|---|
| Create | **Structural Member** | `weld_member` |
| Modify | **Gusset** | `weld_gusset` |
| Modify | **End Cap** | `weld_end_cap` |

#### Workspace: **Surface** (Part, promotes the surface tools to primary)
All `surf_*` commands listed above, organized Create / Modify / Edit. (They *also* remain reachable
from Model so users never have to mode-switch for a one-off surface.)

#### Workspace: **Simulate** (Part + Assembly)

| Group | Tool | Absorbs |
|---|---|---|
| Study | **New Study** | `sim_new_static`, `sim_new_modal`, `sim_new_flow`, `sim_new_thermal`, `sim_new_fatigue` *(type chosen in form)* |
| Setup | **Material** | `sim_material` |
| Setup | **Fixtures** | `sim_fixture` |
| Setup | **Loads** | `sim_load_force`, `sim_load_pressure` *(load type in form)* |
| Setup | **Mesh** | `sim_mesh` |
| Run | **Run / Results** | `sim_run`, `sim_results` |

#### Workspace: **Assemble** (Assembly)

| Group | Tool | Absorbs |
|---|---|---|
| Insert | **Insert Component / Standard Parts** | `asm_insert`, `asm_parts_library` |
| Position | **Move** | `asm_move` |
| Position | **Mate** | `asm_mate` |
| Pattern | **Component Pattern** | `asm_pattern` |
| Inspect | **Interference / BOM** | `asm_interference`, `asm_bom` |
| Display | **Exploded View** | `asm_explode` |

#### Workspace: **Drafting** (Drawing) — merges old "View Layout" + "Annotation" tabs

| Group | Tool | Absorbs |
|---|---|---|
| Views | **Model / Projected / Standard 3 / Section / Detail** | `dwg_view_model`, `dwg_view_proj`, `dwg_view_std3`, `dwg_view_section`, `dwg_view_detail`, `dwg_section_half`, `dwg_section_reverse` |
| Dimension | **Smart Dim / Model Items / Hole Callout** | `dwg_smart_dim`, `dwg_model_items`, `dwg_hole_callout` |
| Annotate | **GD&T / Datum / Surface Finish / Weld** | `dwg_gdt`, `dwg_datum`, `dwg_datum_target`, `dwg_surface_finish`, `dwg_weld` |
| Annotate | **Center Mark / Center Line / Note / Balloon** | `dwg_center_mark`, `dwg_center_line`, `dwg_note`, `dwg_balloon`, `dwg_auto_balloon` |
| Tables | **Title Block / BOM** | `dwg_title_block`, `dwg_bom_table` |
| Sheet | **Sheet Props / Rebuild / Fit / Scale** | `dwg_sheet_props`, `dwg_rebuild`, `dwg_fit`, `dwg_scale_*` |
| Output | **Export DXF / PDF** | `dwg_export_dxf`, `dwg_export_pdf` |

#### Workspace: **Schematic** (ECAD) & **Board** (ECAD)

| Workspace | Group | Tool | Absorbs |
|---|---|---|---|
| Schematic | New | New Sch / Open / Save | `ecad_new_sch`, `ecad_open`, `ecad_save` |
| Schematic | Draw | Place Symbol / Wire / Net Label / Power Port | `sch_add_symbol`, `sch_add_wire`, `sch_add_label`, `sch_add_power` |
| Schematic | Verify | Annotate / Run ERC | `sch_annotate`, `ecad_run_erc` |
| Board | New | New PCB / Update from Sch | `ecad_new_pcb`, `pcb_update_from_sch` |
| Board | Place | Footprint / Route / Via / Copper Zone | `pcb_add_footprint`, `pcb_add_track`, `pcb_add_via`, `pcb_add_zone` |
| Board | Auto | Autoroute / Fill Zones / Run DRC | `ecad_autoroute`, `ecad_fill_zones`, `ecad_run_drc` |
| Board | Analyze | Simulate | `ecad_simulate` |
| Board | Output | Board to 3D / Gerbers / Drill / BOM | `ecad_to_3d`, `ecad_export_gerber`, `ecad_export_drill`, `ecad_export_bom` |
| (both) | AI | **AI Design** | `ecad_ai_design` → routes into the unified Agent panel |

### 3.2 Sketch as a contextual mode

`sketch_new` enters a **Sketch workspace** (the whole context toolbar swaps). Groups:
`Draw · Dimension · Modify · Relations · Finish`.

- **Draw**: Line, Rectangle▾(corner/center/3pt/3pt-center/parallelogram), Circle▾(center/perimeter),
  Arc▾(3pt/center/tangent), Polygon, Slot▾(straight/ctr-straight/arc 3pt/arc ctr), Spline▾(spline/
  style/equation/airfoil/on-surface), Ellipse▾(ellipse/partial/parabola/conic), Point, Text.
  *(Absorbs all `sk_*` draw + flyout cmd_ids and the `sk_ellipse_big`/`sk_rect_*`/`sk_slot_*`/
  `sk_spline_*` variants — flyouts, not separate buttons.)*
- **Dimension**: Smart Dimension (`smart_dim`).
- **Modify**: Trim, Extend, Offset, Fillet, Chamfer, Convert, Mirror, Pattern▾(linear/circular),
  Move/Copy. *(`sk_trim`, `sk_extend`, `sk_offset_ent`, `sk_fillet`, `sk_chamfer`, `sk_convert_ent`,
  `sk_mirror`, `sk_lin_pattern`, `sk_circ_pattern`, `sk_move`.)*
- **Relations**: Add Relation, Fix, Construction toggle. *(`sk_relations`, `sk_fix`,
  `sk_construction`.)*
- **Finish**: ✔ Exit Sketch (`sketch_exit`).

### 3.3 The command registry (implementation)

All of the above is **data**, not hardcoded layout. One table:

```cpp
struct Command {
    const char* id;            // stable dispatch id (engine contract — unchanged)
    const char* label;         // display
    IconId      icon;
    Workspace   workspace;     // Model, Sketch, Assemble, Drafting, Schematic, Board, Simulate, …
    Group       group;         // Sketch, Create, Modify, Pattern, Construct, Inspect, Output, …
    const char* parent;        // flyout parent id, or null (top-level)
    const char* keywords;      // extra search terms for the ⌘K palette
    OpVariants  variants;      // e.g. {New, Add, Cut, Intersect, Surface} — populates the Inspector
    Predicate   enabledWhen;   // context gate (selection / doc state)
};
```

- The context toolbar, the `⌘K` palette, the app (☰) menu, and right-click context menus are all
  **generated from this one registry** — never duplicated. (Old code had the ribbon defined twice.)
- `cmd_id`s stay byte-for-byte identical to today so the engine dispatch + agent tool names don't
  change. We only re-home and re-group them.
- Unifying boss/cut/surface is a *UI* concern: the tool emits the existing `extrude_boss` vs
  `extrude_cut` cmd based on the chosen op variant. No engine change.

---

## 4. The app (☰) menu — replaces SolidWorks' 7-menu bar

A single compact menu button (top-left), not a persistent menubar. Sections:
**File** (New▾, Open, Save, Save As, Save All, Export▾, Recent, Close) ·
**Edit** (Undo, Redo, Cut, Copy, Paste, Delete, Select All) ·
**View** (panels toggles, themes, reset layout, viewport styles) ·
**Insert** (= the Create/Construct groups, generated from the registry) ·
**Tools** (Inspect/Simulate/specialized workbench entry points) ·
**Help** (Docs, Shortcuts, About). Every entry dispatches the same `cmd_id` as its toolbar/palette
twin — one code path.

---

## 5. The transparent agent panel (headline feature)

Replaces **both** `legacy_chat.cpp` and `activity_panel.cpp` with one surface modeled on Claude
Code / the VS Code agent. The agent's run already emits a structured event stream (the engine writes
`%APPDATA%/AI-CAD/cache/runs/<run>/events.jsonl`; RPCs `agent_run_v2` / `agent_mission_v1`). The new
panel renders that stream as a **live timeline of typed blocks** instead of an opaque "thinking…".

### Block types (rendered top-to-bottom in the transcript)

| Block | Source event | Rendering |
|---|---|---|
| **User message** | user input | right-aligned bubble, markdown |
| **Thinking** | `thinking` / reasoning delta | collapsible, dimmed, italic, streams token-by-token; auto-collapses when the next action starts |
| **Action** (tool call) | `tool_call` | inline card: tool icon + human label ("Extrude Boss 25 mm"), live status spinner → ✓/✗, expandable to show the raw op args (JSON) and the result; CAD ops show a thumbnail when the engine returns a preview render |
| **Result / observation** | `tool_result` | folded into the Action card (success summary or error with a "retry"/"explain" affordance) |
| **Plan / todo** | `plan` / mission steps | a checklist that ticks off as steps complete (mission orchestrator) |
| **Assistant message** | `message` delta | normal markdown bubble, streams |
| **Diff / change** | feature add/edit | a compact "what changed in the model" chip linking to the tree node it created |
| **Usage footer** | `usage` | tokens + wall-time per turn, dim, end of turn |
| **Error** | `error` | red card with the real reason (read from `events.jsonl`, never the "budget exhausted" mask) |

### Behaviors
- **Streaming with motion**: blocks fade/slide in; the active action card pulses while running.
- **Transparency toggle**: a "Show thinking" switch (default on) like Claude Code's verbose mode.
- **Jump-to-geometry**: clicking an Action card selects/zooms the geometry it created.
- **Stop / steer**: a stop button on the composer; mid-run the user can send a steering message.
- **Composer**: multiline input, model picker (Claude / Gemini / local — reuses existing provider
  config), attach (image/file for vision review), and a mode hint (Ask vs Agent/Build).
- **Per-doc threads**: the agent thread follows the active doc tab (existing behavior, kept).

This is the single biggest UX differentiator and the one panel we build fully in the first slice.

---

## 6. Viewport (reuse + optimize)

Keep the existing OpenGL pipeline (it's the valuable, already-optimized, non-IP code). From the
recon: `aicm` mesh format, per-body instancing (`rigid_instance`/`TopLoc`), a batched **id-pass**
for picking (one int-id attribute per vertex, ~3 draws/body), deduped overlay VBOs, a compile cache
that made picks 188→13 ms. **Preserve all of it.** The render core moves under `app/src/viewport/`
largely intact (`renderer`, `mesh`, `render/aicm_loader`, `render/id_pass`, `render/overlays`,
`camera_anim`).

Modernization (additive, perf-positive):
- Render the scene to an **offscreen FBO texture** and composite as an ImGui image — lets the
  viewport live in a dockable ImGui window cleanly and enables post-fx (FXAA/SMAA, SSAO-lite,
  outline) without touching geometry.
- In-canvas **nav gizmo** (generic orientation cube — distinct from SW's ViewCube), floating
  context mini-toolbar, and a selection HUD, all drawn as ImGui overlays on the image.
- Keep id-pass picking; only re-render the id buffer on camera/scene change (already batched).
- Frame pacing: redraw on demand (dirty-flag) + vsync; the viewport should idle at ~0% GPU when
  nothing moves. Animations use a fixed-timestep easer (§7.4).

---

## 7. Visual design system (theme tokens)

Dark-first, with a light variant. Tokens live in `app/src/theme/tokens.h` and feed both ImGui style
and custom draw-list rendering.

### 7.1 Color (dark) — neutral grays + one accent
```
bg/0   #0E1116   app background (deepest)
bg/1   #151A21   panels
bg/2   #1C232C   raised cards / toolbar
bg/3   #232C37   hover
line   #2C3744   borders / dividers (1px)
txt/0  #E6Eaf0   primary text
txt/1  #9AA7B5   secondary text
txt/2  #5E6B7A   disabled / hints
accent #4C8DFF   primary accent (selection, focus, active tool)   ← NOT SW blue-grey
good   #46C08D   success / confirm
warn   #E2B33E   warning
bad    #E5604D   error / cut ops
```
Light variant mirrors with inverted bg ramp + same accent. Themes are full palette swaps, not
hardcoded per-widget colors (the old code had this bug).

### 7.2 Typography (keep the OFL fonts — clean license)
| Role | Font | px @1.0× |
|---|---|---|
| body | Rubik Regular | 15 |
| label | Rubik Medium | 15 |
| h2 / section | Rubik SemiBold | 17 |
| h1 / title | Rubik Bold | 26 |
| mono / code / agent JSON | JetBrains Mono | 13.5 |

(Slightly tighter than the old 17.5 body for a denser, more pro feel.) Icon font: a vector icon set
(§8) merged into the atlas at load.

### 7.3 Spacing & shape
- 4px base grid. WindowPadding 14/12, FramePadding 10/7, ItemSpacing 8/7.
- Rounding: controls 6, cards/popups 8, windows 10. Borders 1px `line`.
- One scale value `app::uiScale`; fonts pre-rasterized at `px*scale`; `ScaleAllSizes(scale)` once.
  (Carry over the *only* good part of the old style guide — its §1 scaling model.)

### 7.4 Motion
- Durations: micro 90ms (hover), standard 160ms (panel open, tab switch), entrance 220ms.
- Easing: `easeOutCubic` default; springs for the nav gizmo snap.
- All animation is value-lerp driven by `ImGui::GetIO().DeltaTime` (immediate-mode friendly);
  nothing blocks the frame. Respect a "reduce motion" setting.

---

## 8. Icons & assets (placeholder system + guide)

The old atlas had a fragile name-contract (magenta "?" bugs from typos) and heavy icon *reuse*
(`extrude_boss` stood in for sheet-metal, weldment, surface tools). The rewrite:

- Adopts a **vetted open-source vector icon set** (e.g. Lucide / Tabler — MIT) as the base, rendered
  to a multi-size atlas at build time, **plus** a set of **custom CAD-operation glyphs** (extrude,
  revolve, fillet, …) that don't exist in general icon sets.
- A **validated registry**: every `IconId` is an enum; a startup assert proves each has an atlas
  cell — a typo fails the build, never ships magenta.
- **Placeholders + guide**: where a custom CAD glyph isn't drawn yet, a neutral monochrome
  placeholder renders (not magenta), and `app/resources/icons/ICONS.md` lists every needed icon with
  a description + suggested reference, so an image-gen model (or a cropped in-app screenshot) can
  fill it in. See that file for the full enumerated list and the generation workflow.

---

## 9. Reuse map (host/ → app/)

| host/ module | Disposition | Notes |
|---|---|---|
| `worker_client`, `core/cpp_core`, `core/async_rpc`, `core/doc_mirror`, `core/sim_client`, `core/jobs`, `core/selection`, `core/app_state` (data parts) | **KEEP / light refactor** | The engine bridge + doc model. The contract the new UI binds to. |
| `renderer`, `mesh`, `render/*`, `ui/camera_anim`, `ui/viewport_panel` (GL parts) | **KEEP** → `app/src/viewport/` | The optimized OpenGL pipeline. Re-wrap in a dockable FBO window. |
| `ui/sketch_mode`, `ui/sim_manager`, `ui/drawing_panel`, `ui/ecad/*`, `ui/asm_panels`, `ui/property_manager` (form logic), `ui/feature_tree` (tree logic), `ui/airfoil`, `ui/section_tool`, `ui/design_table_panel`, `ui/cutlist_panel`, `ui/config_panel`, `ui/equations_dialog`, `ui/evaluate_dialogs` | **REHOST / restyle** | Working, engine-integrated editors. Re-skinned to the new theme + re-homed under the new IA. Rewritten incrementally, not up front. |
| `app.cpp`, `ui/layout`, `ui/doc_tabs` + `core/doc_tabs` (dup), `ui/ribbon_def`, `ui/widgets/ribbon`, `ui/title_bar`, `ui/status_bar`, `ui/shortcut_bar`, `ui/view_selector`, `ui/settings_modal`, `ui_theme`, `win_chrome`, `ui/icons` | **DELETE & REWRITE** | The SW-clone shell, chrome, ribbon, theme, dup doc-tabs, icon system. |
| `ui/legacy_chat`, `ui/activity_panel` | **DELETE & REWRITE** | → the single transparent Agent panel (§5). |

---

## 10. Cross-platform (Windows-first, portable by construction)

| Windows-only today | Portable replacement |
|---|---|
| `win_chrome.cpp` (DWM custom titlebar, `WM_NCHITTEST`) | GLFW borderless window + ImGui custom titlebar + a small per-OS hit-test shim (`GLFW_RESIZE_*` cursors; native drag via `glfwSetWindowPos` on title drag). macOS keeps native traffic-lights. |
| `app.rc` / `app.ico` | CMake per-platform resource: `.ico` (win), `.desktop`+png (linux), `.icns`+bundle (mac). |
| `%APPDATA%\AI-CAD` paths | one `app::userDataDir()` → `%APPDATA%` / `$XDG_CONFIG_HOME` / `~/Library/Application Support`. |
| native file dialogs | `nfd` (Native File Dialog Extended, zlib) — cross-platform. |
| Visual Studio generator in `build.bat` | CMake presets (Ninja on Linux/mac, VS on Windows). Keep `build.bat` for Windows convenience. |

We write portable from day one (no raw Win32 in new code) but only *build/verify* Windows now.

---

## 11. Phased build (vertical slice first)

1. **Scaffold** — `app/` CMake, vendored deps (imgui-docking, glfw, glad, json, stb, nfd, fonts),
   `main.cpp`, window + GL context, links the reused `host/` bridge + render sources.
2. **Shell + theme** — title bar/chrome, docking layout, theme tokens, fonts, status bar, activity
   rail. Empty panels, real look.
3. **Viewport** — re-wrap the reused renderer in the FBO/dockable window; nav gizmo; picking.
4. **Tree + one tool end-to-end** — model Tree (reused logic, new skin) + the command registry +
   context toolbar + the Extrude tool with Inspector → a real feature created via the engine.
5. **Agent panel** — the full transparent timeline (§5) wired to `agent_run_v2` + `events.jsonl`.
6. **Command palette** (`⌘K`) over the registry.
7. **Re-home remaining workspaces** — Sketch, Assemble, Drafting, Simulate, ECAD — rehosting the
   existing editors under the new IA, restyling as we go.

Each phase builds + runs. Slice = phases 1–6 (a coherent, runnable app with one full modeling
workflow + the agent).

---

## 12. Open follow-ups
- Decide Lucide vs Tabler vs custom-only for the base icon set (tracked in ICONS.md).
- Linux/macOS build verification (deferred per Windows-first decision).
- Migration of persisted layout/config from the old host (best-effort; new schema).
</content>
</invoke>

# AI-ECAD GUI Integration Guide (Wave D)

> The four new files under `host/src/ui/ecad/` are an **isolated scaffold** — they compile against the
> existing host headers (`ui_theme.h`, `imgui.h`, `core/app_state.h`, `nlohmann/json.hpp`) but are
> **not yet wired** into the build, the dockspace, the draw loop, or the ribbon. This was deliberate:
> another agent is editing the shared host files, so every shared-file edit lives here as a fenced
> snippet for a human/agent to apply deliberately at build time. Nothing in this guide has been
> build-verified (ImGui/GLFW come from CMake FetchContent that isn't downloaded in the scaffold env).
>
> New files (all carry the header `// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified …`):
> - `host/src/ui/ecad/ecad_mirror.{h,cpp}` — read-only parse of `ecad_tree` JSON → typed world geometry
> - `host/src/ui/ecad/ecad_canvas.{h,cpp}` — 2D pan/zoom canvas; renders schematic AND board
> - `host/src/ui/ecad/ecad_panel.{h,cpp}` — dockable editor panel (`ui::drawEcadEditor(AppState&)`)
>
> Apply the steps below in order. After (a)–(c) the panel renders an empty-state hint; after (d)–(e)
> it shows live documents; (f) adds property forms for the ops.

---

## (a) Add the 4 `.cpp` files to `host/CMakeLists.txt`

In `add_executable(AI-CAD …)` (around the C20b/C22b block, ~line 89), insert a new section. The two
headers don't need listing (header-only inclusion), only the four translation units:

```cmake
  # --- Wave D: AI-ECAD GUI (2D schematic / PCB editor) ---
  src/ui/ecad/ecad_mirror.cpp
  src/ui/ecad/ecad_canvas.cpp
  src/ui/ecad/ecad_panel.cpp
  # --- end Wave D ---
```

(There are three `.cpp` files; `ecad_panel.cpp` includes `ecad_canvas.cpp`/`ecad_mirror.cpp` only via
their headers, so all three must be listed.) No new include directories are required — the files use
relative includes (`../../ui_theme.h`, `../../core/app_state.h`) and the already-present `imgui` /
`nlohmann_json` targets.

---

## (b) Register the dock window in `host/src/ui/title_bar.cpp` → `buildDefaultLayout`

The panel's window title is `"ECAD"`. Dock it where the central content lives so it sits beside the
Viewport (ECAD is a full-canvas editor like the 3D viewport, not a side tool). Add one line inside
`buildDefaultLayout`, next to the existing `DockBuilderDockWindow("Viewport", main)` (~line 91):

```cpp
    ImGui::DockBuilderDockWindow("Viewport", main);
    // Wave D: the ECAD 2D editor shares the central node with the Viewport (tabbed). The user
    // switches between mechanical (Viewport) and electrical (ECAD) with the central tab bar.
    ImGui::DockBuilderDockWindow("ECAD", main);
```

If you prefer ECAD to open only on demand, omit this and let it float on first show; docking it here
just gives it a sensible home tab.

---

## (c) Call `ui::drawEcadEditor(st)` in `host/src/app.cpp`'s draw loop

Include the header near the other panel includes at the top of `app.cpp`:

```cpp
#include "ui/ecad/ecad_panel.h"
```

Then call it in the per-frame panel block, right after `ui::drawViewportPanel(st, io, busy);`
(~line 1062):

```cpp
        ui::drawViewportPanel(st, io, busy);
        ui::drawEcadEditor(st);          // Wave D: AI-ECAD 2D editor (no-ops until a doc loads)
```

`drawEcadEditor` opens/closes its own `ImGui::Begin("ECAD")` and shows an empty-state hint when no
ECAD document is loaded, so this is safe to call unconditionally every frame.

---

## (d) Add a "PCB" ribbon tab + command entries to `host/src/ui/ribbon_def.cpp`

`ribbon_def.cpp` is **append-only** (its header comment is explicit: "later cards APPEND … never
reorder or delete existing rows"). Append a new tab block at the END of the `kCmds[]` array (before
the closing `};`). `RibbonCmd` fields are `{cmd_id, label, icon, tab, group, big, sub_of, stretch}`
(see `ribbon_def.h`). Reuse existing icon names where possible; missing icons render a placeholder.

```cpp
    // =========================================================================
    // Tab: PCB  (Wave D — AI-ECAD electrical design)
    // =========================================================================

    // --- Group: New ---
    { "ecad_new_sch",      "New Schematic",  "sketch",          "PCB",      "New",             true,  nullptr,          false },
    { "ecad_new_pcb",      "New PCB",         "extrude_boss",    "PCB",      "New",             true,  nullptr,          false },
    { "ecad_open",         "Open ECAD",       "ref_plane",       "PCB",      "New",             false, nullptr,          false },

    // --- Group: Schematic ---
    { "sch_add_symbol",    "Place Symbol",    "point_ent",       "PCB",      "Schematic",       true,  nullptr,          false },
    { "sch_add_wire",      "Wire",            "line",            "PCB",      "Schematic",       true,  nullptr,          false },
    { "sch_add_label",     "Net Label",       "text_ent",        "PCB",      "Schematic",       false, nullptr,          false },
    { "sch_add_power",     "Power Port",      "construction",    "PCB",      "Schematic",       false, nullptr,          false },
    { "sch_annotate",      "Annotate",        "ok_check",        "PCB",      "Schematic",       false, nullptr,          false },

    // --- Group: Board ---
    { "pcb_set_outline",   "Board Outline",   "rect_corner",     "PCB",      "Board",           true,  nullptr,          false },
    { "pcb_add_footprint", "Place Footprint", "point_ent",       "PCB",      "Board",           true,  nullptr,          false },
    { "pcb_add_track",     "Route Track",     "line",            "PCB",      "Board",           true,  nullptr,          false },
    { "pcb_add_via",       "Via",             "circle",          "PCB",      "Board",           false, nullptr,          false },
    { "pcb_add_zone",      "Copper Zone",     "rect_center",     "PCB",      "Board",           false, nullptr,          false },
    { "pcb_update_from_sch","Update from Sch","ok_check",        "PCB",      "Board",           false, nullptr,          false },

    // --- Group: Outputs ---
    { "ecad_to_3d",        "Board to 3D",     "extrude_boss",    "PCB",      "Outputs",         true,  nullptr,          false },
    { "ecad_export_gerber","Gerbers",         "ref_plane",       "PCB",      "Outputs",         false, nullptr,          false },
    { "ecad_export_bom",   "BOM",             "text_ent",        "PCB",      "Outputs",         false, nullptr,          false },
```

The ribbon widget builds its tab bar from the distinct `tab` strings in the table, so the new "PCB"
tab appears automatically once these rows exist. `st.ribbonTab` is an index into the tab order; if you
want a shot/scenario to jump to it, set `st.ribbonTab` to the PCB tab's index (it will be the last
tab unless others are appended after it).

### Wiring the PCB commands into the dispatcher (`host/src/app.cpp`)

The dispatcher is set twice — once in `ui::setRibbonCallback(...)` (~line 207) and again in
`st.ribbonDispatch = ...` (~line 230); add the SAME branch to both. Insert it before the final
`std::printf("[ribbon] … (no PM form)")` fallthrough. This is also where you create/route the ECAD
document via the worker RPC (see §(e) for the helpers it calls):

```cpp
        // Wave D: AI-ECAD commands.
        {
            std::string cmd(cmd_id);
            if (cmd.rfind("ecad_", 0) == 0 || cmd.rfind("sch_", 0) == 0 ||
                cmd.rfind("pcb_", 0) == 0) {
                if (cmd == "ecad_new_sch") { ecadNewDoc(st, "sch"); return; }
                if (cmd == "ecad_new_pcb") { ecadNewDoc(st, "pcb"); return; }
                // Geometry-placing ops (sch_add_symbol, pcb_add_track, …) open a PM form whose OK
                // emits the op via ecadApplyOps (§(e)/(f)). Until those forms exist, log + return so
                // the command doesn't fall through to the CAD "no PM form" path.
                st.addLog(std::string("[ecad] ") + cmd_id + " (form pending)");
                return;
            }
        }
```

`sch_*` / `pcb_*` here are the schematic/board ops shadowing the same names; the `ecad_` prefix guard
plus the explicit handlers keep them from colliding with any future CAD command (none of the existing
CAD `cmd_id`s start with `sch_`/`pcb_`/`ecad_`).

---

## (e) Worker-RPC call pattern — `ecad_apply_ops` / `ecad_tree` → the mirror

The ECAD RPC surface (`contracts/ecad/ecad_rpc.md`, registered from `engine/ecad/rpc.py`) is the
exact analogue of the CAD `doc_*` methods that `core/doc_mirror.cpp` already drives via
`WorkerClient::call(...)`. The scaffold's panel keeps its mirror behind `ui::ecadEditorMirror()` and a
`ui::ecadEditorLoadResponse(const nlohmann::json&)` feeder; you call the worker and hand the response
to the feeder.

**Recommended permanent state** (small AppState additions, mirroring the CAD `core::DocMirror docMirror;`
field): add to `struct AppState` in `host/src/core/app_state.h`:

```cpp
    // ---- Wave D: AI-ECAD document ----
    std::string ecadDocId;     // open ECAD doc id (empty = none); analogue of DocMirror::docId()
    int         ecadRevision = -1;  // last known ECAD revision (optimistic-concurrency base)
```

Then the editor mirror can read `st.ecadDocId`. (The scaffold panel currently stores the doc id inside
its own state via `EcadMirror::setDocId`, so this AppState field is optional until you move the mirror
onto AppState — see the note at the top of `ecad_panel.h`.)

**Free helper functions** (put them in `app.cpp` near the dispatcher, or a new
`host/src/ui/ecad/ecad_rpc_client.cpp` you also add to CMake):

```cpp
#include "ui/ecad/ecad_panel.h"   // ecadEditorLoadResponse, ecadEditorMirror

// Create a new sch/pcb document on the worker and load it into the editor mirror.
static void ecadNewDoc(AppState& st, const char* kind) {
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_new", {{"kind", kind}}, res, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD new failed: " + err);
        return;
    }
    if (res.contains("code")) {                      // error envelope {code,message,detail}
        st.toast(ui::ToastLevel::Error, "ECAD new: " + jstr(res, "message"));
        return;
    }
    st.ecadDocId = jstr(res, "doc_id");
    st.ecadRevision = jint(res, "revision");
    ui::ecadEditorMirror().setDocId(st.ecadDocId);
    ui::ecadEditorLoadResponse(res);                 // res already carries {revision,kind,tree,compile}
    st.showFeatureTree = true;                       // (or focus the ECAD dock tab)
    st.addLog("[ecad] new " + std::string(kind) + " doc " + st.ecadDocId);
}

// Apply an op batch (the same closed vocabulary as contracts/ecad/ecad_ops.md). On success the
// response's {revision,kind,tree,compile} flow straight into the mirror; on stale_revision we resync
// via ecad_tree (same discipline as core::DocMirror::applyOps).
static void ecadApplyOps(AppState& st, const nlohmann::json& ops) {
    if (st.ecadDocId.empty()) return;
    nlohmann::json res; std::string err;
    nlohmann::json params = {
        {"doc_id", st.ecadDocId},
        {"base_revision", st.ecadRevision},
        {"ops", ops},
    };
    if (!st.worker.call("ecad_apply_ops", params, res, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD apply failed: " + err);
        return;
    }
    if (res.contains("code")) {
        if (jstr(res, "code") == "stale_revision") {
            nlohmann::json tr; std::string e2;
            if (st.worker.call("ecad_tree", {{"doc_id", st.ecadDocId}}, tr, e2) &&
                !tr.contains("code")) {
                st.ecadRevision = jint(tr, "revision");
                ui::ecadEditorLoadResponse(tr);
            }
            st.toast(ui::ToastLevel::Warn, "ECAD edit dropped (stale); resynced");
        } else {
            st.toast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
        }
        return;
    }
    st.ecadRevision = jint(res, "revision");
    ui::ecadEditorLoadResponse(res);
}
```

`jstr` / `jint` are the tolerant accessors already declared in `app_state.h`. `st.worker` is the
existing `WorkerClient`; `call(method, params, result, err)` is its only entry point (see
`worker_client.h`). Both `ecad_new` and `ecad_apply_ops` return the full ops-response object, so the
mirror's `loadFromTree` can consume them directly — no shape adaptation needed.

**Placeholder ids in one batch** (`ecad_ops.md §3`): an op that creates an object can be referenced
later in the SAME batch as `"$<op-index>"`, exactly like the CAD ops. E.g. add a symbol in op 0, wire
to it in op 1 with `{"from":{"sym":"$0","pin":"1"}, …}`. The mirror doesn't need to know about
placeholders — the worker resolves them and returns concrete ids in `id_map`.

**Undo/redo**: call `ecad_undo` / `ecad_redo` `{doc_id}` and feed the response through
`ecadEditorLoadResponse` the same way (they return ops-responses). Route Ctrl+Z/Ctrl+Y to these when
the ECAD tab is focused (mirror the existing `AppState::docUndo/docRedo` gating).

**Canvas pick → ref** (future): `ecad_resolve_pick {doc_id, layer?, xy, kinds?}` turns a canvas click
into a typed selector (pin/pad/track/via/net). The canvas already returns the world-mm cursor
(`EcadCanvasIO::worldMouse`); pass it as `xy` and feed the resulting selector into a PM SelectorWell.

---

## (f) Suggested `PMFormDef` entries (`host/src/ui/pm_forms.cpp`) for the key ECAD ops

The PropertyManager form system (`property_manager.h`) is data-driven: each feature/op type maps to a
`PMFormDef { type, title, icon, fields[] }`, and `pmFindForm(type)` / `pmFormForCommand(cmdId, mode)`
resolve it. The existing `PMFieldKind` enum is `{ Dim, SelectorWell, Combo, Check, Int, DatumRef,
FeatureWell }`. ECAD ops fit most of these directly; two would benefit from **append-only** new kinds
(noted below — append at the end of the enum so existing ordinals are untouched).

The cleanest integration is to reuse the PM unchanged and have `commit()` emit an ECAD op instead of a
CAD feature when `type` starts with `sch_`/`pcb_`; but `pm_forms.cpp` only owns the TABLE — wiring the
ECAD emit path is a `property_manager.cpp` change out of scope here. The table entries below are ready
to append to `pm_forms.cpp`'s form list:

```cpp
// --- Wave D: AI-ECAD op forms (emit ECAD ops via the §(e) ecadApplyOps path) ---

// sch_add_symbol — place a library symbol. `libid`/`ref`/`value` are text; pos comes from the canvas
// click (not a form field). Until a NetRef/LibRef kind exists, libid is a Combo seeded from
// lib_cache keys (or a free-text Dim-like field).
{ "sch_add_symbol", "Place Symbol", "point_ent", {
    // NOTE: libid is best a new PMFieldKind::LibRef (a searchable library combo). As a stopgap a
    // Combo seeded from the doc's lib_cache works; comboOptions would be filled at openForCommand.
    { PMFieldKind::Combo, "libid", "Library Symbol", "Symbol",  0.0, nullptr, {}, false,
      "Device:R|Device:C|Device:L|Device:D", "R|C|L|Diode" },
    { PMFieldKind::Int,   "angle", "Rotation",       "Symbol",  0.0, "[deg]" },
    { PMFieldKind::Combo, "mirror","Mirror",         "Symbol",  0.0, nullptr, {}, false, "none|x|y" },
    { PMFieldKind::Combo, "ref_auto","Reference",    "Symbol",  0.0, nullptr, {}, false, "auto|manual" },
}},

// pcb_add_track — route a copper segment. p1/p2 from canvas clicks; width + layer + net here.
{ "pcb_add_track", "Route Track", "line", {
    // layer: a new PMFieldKind::LayerCombo (copper-layer picker fed from the stackup) reads best;
    // a plain Combo with the canonical copper ids is the stopgap.
    { PMFieldKind::Combo, "layer", "Layer", "Track", 0.0, nullptr, {}, false, "F.Cu|In1.Cu|B.Cu" },
    { PMFieldKind::Dim,   "width", "Width", "Track", 0.25, "[mm]" },
    // net: a new PMFieldKind::NetRef (combo fed from compile.netlist / doc.nets) reads best; a Combo
    // seeded at openForCommand from the mirror's nets() is the stopgap. Optional (defaults net-class).
    { PMFieldKind::Combo, "net",   "Net",   "Track", 0.0, nullptr, {}, false, "(auto)|GND|VCC" },
}},

// pcb_add_via — a through/blind via at a canvas point.
{ "pcb_add_via", "Via", "circle", {
    { PMFieldKind::Dim,   "diameter", "Diameter", "Via", 0.8, "[mm]" },
    { PMFieldKind::Dim,   "drill",    "Drill",    "Via", 0.4, "[mm]" },
    { PMFieldKind::Combo, "type",     "Type",     "Via", 0.0, nullptr, {}, false, "through|blind|buried|micro" },
    { PMFieldKind::Combo, "net",      "Net",      "Via", 0.0, nullptr, {}, false, "(auto)|GND|VCC" },
}},

// pcb_add_footprint — place a footprint (fpid + ref/value + side).
{ "pcb_add_footprint", "Place Footprint", "point_ent", {
    { PMFieldKind::Combo, "fpid", "Footprint", "Footprint", 0.0, nullptr, {}, false,
      "Resistor_SMD:R_0603|Capacitor_SMD:C_0603", "R_0603|C_0603" },
    { PMFieldKind::Int,   "angle","Rotation",  "Footprint", 0.0, "[deg]" },
    { PMFieldKind::Combo, "side", "Side",      "Footprint", 0.0, nullptr, {}, false, "top|bottom" },
}},

// sch_add_label — a net label (text + scope) at a canvas point.
{ "sch_add_label", "Net Label", "text_ent", {
    // text would be a free-text field; the PM has no String kind yet — a new PMFieldKind::Text is the
    // natural append-only addition. Until then, reuse a Combo of common net names or extend the enum.
    { PMFieldKind::Combo, "scope", "Scope", "Label", 0.0, nullptr, {}, false, "local|global|hier" },
}},
```

### Recommended append-only `PMFieldKind` additions

Add these to the END of `enum class PMFieldKind` in `property_manager.h` (after `FeatureWell`) so no
existing ordinal shifts:

| new kind     | purpose                                                                 |
|--------------|-------------------------------------------------------------------------|
| `NetRef`     | A net combo fed live from `EcadMirror::nets()` (the open doc's nets). Emits the chosen net name as a string param. Avoids hardcoding `GND\|VCC`. |
| `LayerCombo` | A copper-layer combo fed from the board stackup (`EcadMirror::layers()`), with the per-layer swatch color. Emits the layer id. |
| `Text`       | A free-text single-line field (symbol `ref`/`value`, label `text`). The PM currently has only numeric `Dim` + enum `Combo`; ECAD needs raw strings. |
| `LibRef`     | A searchable library picker (symbols / footprints) backed by `engine.ecad.library`. Combo is the stopgap. |

These are additive: a new kind needs a `draw` case in `property_manager.cpp` and an `emit` case in
`buildOpFeature`, but does NOT touch any existing field's behavior. The `PMField` runtime struct
already has `comboIndex` (reusable by NetRef/LayerCombo/LibRef) and would need a small `std::string
textBuf` for `Text`.

---

## Data flow recap

```
ribbon "PCB" cmd ──▶ app.cpp dispatcher ──▶ ecadNewDoc / PM form OK ──▶ ecadApplyOps
                                                                            │ worker.call("ecad_apply_ops")
                                                                            ▼
                                              {revision, kind, tree, compile}  (engine/ecad/session.py)
                                                                            │ ui::ecadEditorLoadResponse(res)
                                                                            ▼
                                              EcadMirror::loadFromTree  (parse → world geometry)
                                                                            │ every frame
                                                                            ▼
                          ui::drawEcadEditor(st) ──▶ EcadCanvas::draw  +  layers/nets  +  ERC/DRC strip
```

## What renders, schematic vs board

- **Schematic** (`EcadMirror::isSch()`): symbol bodies are drawn from each symbol's `lib_cache`
  `SymbolDef.graphics` (rect/line/circle/polyline/arc) transformed by the placement (mirror→rotate→
  translate, ported from `schema.py::local_to_world`); pin stubs run root→tip with a dot at the
  connection tip; `ref`/`value` text sit by the origin; wires are orthogonal polylines (buses thicker
  blue); junctions are dots; net labels are text + tick; power ports are a stem+bar glyph + net name.
  The left panel lists nets (with pin counts) + symbols; the bottom strip shows ERC.
- **Board** (`EcadMirror::isPcb()`): board outline on Edge.Cuts (arcs/circles flattened on parse);
  zones as translucent copper-colored fills + outline; tracks drawn back-to-front by copper index so
  F.Cu lands on top, each layer its palette color; footprint courtyards (dashed) + pads (per-layer
  color, drill plug for THT) + ref text; vias as ring+plug; ratsnest airwires as dashed amber lines.
  The left panel is the per-layer visibility list (swatch + checkbox) + the net list; the bottom strip
  shows DRC + the unrouted count.

## Assumptions made about host APIs (could not build-verify)

- `nlohmann::json` via `<nlohmann/json.hpp>` (confirmed in `worker_client.h`/`app_state.h`).
- ImGui **v1.91.6-docking** API (from `host/CMakeLists.txt`): `GetWindowDrawList`, `AddLine/AddRect*/
  AddCircle*/AddText/AddTriangleFilled/AddConvexPolyFilled`, `InvisibleButton` with
  `ImGuiButtonFlags_MouseButton{Left,Right,Middle}`, `IsItemDeactivated`, `BeginChild` with
  `ImGuiChildFlags_Border` (singular, as the host uses), `PushClipRect/PopClipRect`,
  `ImGui::GetIO().MouseWheel/MouseDelta`. The deprecated `ImGuiSelectableFlags_AllowItemOverlap` was
  intentionally avoided (removed in this ImGui version).
- `ui::palette` roles + `ui::Palette::toU32(const float[4])` + `ui::uiScale` (confirmed in
  `ui_theme.h`). The canvas reuses axis/accent/status roles for copper-layer colors; dedicated layer
  roles could be appended to `Palette` later (see the layer-color helpers in `ecad_canvas.cpp` /
  `ecad_panel.cpp` — keep the two in sync).
- `AppState::worker` (`WorkerClient`), `AppState::addLog(const std::string&)`,
  `AppState::toast(ui::ToastLevel, std::string)` — all confirmed in `core/app_state.h`.
- The panel keeps its mirror/canvas in a function-local static (`ecad_panel.cpp::state()`) ONLY because
  the scaffold may not edit `AppState`. The intended permanent home is an AppState field (or an owned
  `ui::EcadEditor*` like `propertyManager`); §(e) shows the minimal AppState additions.

## What remains to make it build + interactive

1. Apply (a)–(c): the panel compiles + renders the empty-state hint. (Verifies the scaffold builds.)
2. Apply (d) + the dispatcher branch + the §(e) helpers: `New Schematic`/`New PCB` create a doc and the
   canvas shows it live. (First interactive milestone.)
3. Move the mirror onto `AppState` (optional but recommended) so undo/redo, scripted shots, and the
   ribbon can reach it without the function-local static.
4. Wire canvas interaction: route `EcadCanvasIO::clicked` + `worldMouse` through `ecad_resolve_pick`
   into PM SelectorWells; let click-to-place ops (symbol/footprint/track) consume the world cursor.
5. Apply (f) forms + the new `PMFieldKind`s + the `property_manager.cpp` ECAD-emit path so editing is
   form-driven. Add Ctrl+Z/Y → `ecad_undo`/`ecad_redo` gated to the ECAD tab.
6. Add a `View` menu toggle + an `AppState::showEcad` flag if you want to hide the panel (the scaffold
   always draws it; it just shows a hint when empty).
7. Tests: a headless `AI_CAD_SHOT=ecad` scenario (mirror the existing `wantSketch`/`wantFea` shot
   flags) that creates the voltage-divider demo doc (`engine/ecad/demo.py`) and screenshots the canvas.
```

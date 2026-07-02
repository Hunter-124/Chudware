# UI Pattern Library — panel cookbook (T-U1)

The coherence contract for every T-U* feature panel (T-U2 parts browser, T-U3 CAM workspace, T-U4
BOM/sourcing, T-U5 requirements, T-U6 sim studies, T-U7 piping/hydraulic/structural viewers, T-U8
model/telemetry transparency). Each is built by a **different** agent — without this doc they
produce eight styles. Read this + `docs/GLASS_DESIGN_SPEC.md` before writing a new panel. Every
pattern below has a real code pointer; copy it, don't reinvent it.

Conformance is checked by `python3 scripts/ui_style_check.py` (wired as an advisory gate stage —
see `scripts/gate.sh`). It enforces: no literal colors outside `theme::`/tokens, no OS `#ifdef`
outside `ui/imgui/src/platform/`, and a `ui/abstract/` view-model header for every panel built on
the scaffold (pattern 0 below).

---

## 0. Glass column anatomy — `ui/imgui/src/ui/panel_scaffold.h`

Every new feature panel is a **glass column**: header → optional search row → scrollable content
region → optional status footer. Don't hand-roll this — instantiate the scaffold:

```cpp
#include "panel_scaffold.h"

ui::PanelScaffoldSpec spec;
spec.id = "parts_browser";        // stable id — scripts/ui_inventory.py and ui_style_check.py key off it
spec.title = "Parts Browser";
spec.hasSearch = true;
spec.searchHint = "Search parts...";

static ui::PanelScaffoldState state;   // per-panel, not per-frame — keep it file-static or in AppState
state.statusText = "128 parts indexed";

ui::drawPanelScaffold(spec, state, [&]() {
    // draw your rows/cards/table here — see patterns 1-3 below
});
```

`drawPanelScaffold` returns `true` the frame the search text changes — debounce/re-query on that
edge. No `ImGui::Begin`/`End` inside it: call it from inside your own docked/closable window, same
convention as `ui::drawPreviewPane()` (pattern 5).

**Code pointer:** `ui/imgui/src/ui/panel_scaffold.{h,cpp}`. Working example:
`ui/imgui/src/ui/scaffold_demo_panel.{h,cpp}` (headless-verifiable via `AI_CAD_SHOT=scaffold_demo`).

---

## 1. View-model-first (state lives in `ui/abstract/`)

A panel's data model is a **pure struct** in `ui/abstract/` — no ImGui, no GL, no GLFW includes.
The `ui/imgui/` backend reads it and renders; a future `ui/qt/` backend could read the same struct.
Mutations go through the worker RPC seam or `ICommandDispatcher`, never straight into ImGui state.

**Exemplar:** `ui/abstract/artifact_view.h` (T-F16) — `PreviewModel`/`ArtifactCard`/`PreviewState`
consumed by `ui/imgui/src/ui/preview_pane.cpp`. **Scaffold-panel exemplar:**
`ui/abstract/scaffold_demo_view.h` — `ScaffoldDemoModel`/`ScaffoldDemoRow` + a pure filter function,
consumed by `scaffold_demo_panel.cpp`.

`ui_style_check.py`'s `view-model-header` rule requires this pairing for every panel that
instantiates `PanelScaffoldSpec`: the `.cpp` must `#include` at least one `ui/abstract/*.h` header.

---

## 2. Row pill lists — `theme::glassRowBackground` / `theme::glassRowAtCursor`

List rows (search results, tree rows, file rows) get a translucent **pill**, not a full-panel wash
(`docs/GLASS_DESIGN_SPEC.md` §5/§6 — "row pills, not full washes"). Two call shapes:

- **Single-line rows** (already know the row is one text line): `theme::glassRowAtCursor(rounding,
  stripe, selected, rowPadV)` — draw-only, does **not** advance the cursor, so call it *first*, then
  draw your text at the same cursor position (it paints behind — draw-list order). See
  `ui/imgui/src/ui/left_dock.cpp` and `ui/imgui/src/ui/file_explorer.cpp`.
- **Multi-line rows** (need a precomputed rect, e.g. label + detail line): capture
  `ImGui::GetCursorScreenPos()`, compute `rowMax` from your known content height, call
  `theme::glassRowBackground(dl, rowMin, rowMax, rounding, stripe, selected, wash)` **before**
  drawing the row's content. See `ui/imgui/src/ui/scaffold_demo_panel.cpp`'s row loop, or the
  document-toolbar row in `ui/imgui/src/ui/feature_tree.cpp` (`drawFeatureManagerTopStrip`).

Wash alphas are tokens, never literals: `theme::rowWash::idle/hover/selected` (0.26 / 0.32 / 0.42).

**Pitfall:** if you draw content first and the background rect second, the background paints
*over* your text (later draw-list commands win). Background always comes first.

---

## 3. Cards — `ui/imgui/src/ui/artifact_cards.cpp` (`drawArtifactCard`)

For grid/list items that need a thumbnail + a couple of actions (parts search results, BOM line
items, sim-study tiles): a bordered glass child via `theme::pushGlassChildStyle`/
`popGlassChildStyle`, a kind label in `theme::pal().accent`, a title, a muted subtitle, an optional
thumbnail (`theme::loadPreviewTexture`), then `ImGui::SmallButton` actions on one row.

**Code pointer:** `ui/imgui/src/ui/artifact_cards.{h,cpp}`. Actions are a plain struct of
`std::function<void()>` members (`ArtifactCardActions`) — copy that shape for a new card type
rather than hardcoding callbacks inline.

---

## 4. Typed empty-state / placeholder vocabulary

**Never render a blank region.** Every state a panel can be in — nothing selected, loading, error,
unsupported, not found — gets a typed, centered placeholder label (+ optional hint line), not an
empty child window. This is the T-F16 precedent, generalized:

- **Enum-driven state precedent:** `ui/abstract/artifact_view.h`'s `PreviewState` +
  `previewStateFromReason()` (projects a contract failure `reason` string to a typed state) +
  `previewStateLabel()` (state → human label). See `ui/imgui/src/ui/preview_pane.cpp`'s
  `drawPreviewBody()` for the render side.
- **Scaffold-panel helper:** `ui::drawPanelEmptyState(label, hint = nullptr)` in
  `panel_scaffold.h/.cpp` — use this for the common "no results" / "nothing selected" case inside a
  scaffold panel's content lambda (see `scaffold_demo_panel.cpp`'s empty-search-results branch).

Rule of thumb: if your panel has a `Loading` / `Ready` / `Error` axis, name a `PanelXState` enum in
your `ui/abstract/` view-model (mirror `PreviewState`) rather than juggling booleans.

---

## 5. Closable, dock-registered panels — `ui/imgui/src/ui/preview_pane.h` + `application.cpp`

A panel that isn't always visible (opened from the explorer, a command, or a scenario) exposes:

```cpp
void drawXPane(app::AppState& s);   // body only, no Begin/End
void openXPane();                    // sets the open flag (+ any focus request)
bool* xPaneOpen();                   // passed to ImGui::Begin as p_open
```

`application.cpp`'s frame loop then does:

```cpp
if (*ui::xPaneOpen()) {
    if (ImGui::Begin("X###x", ui::xPaneOpen(), pflags)) ui::drawXPane(s);
    ImGui::End();
}
```

Zero cost while closed. **Code pointer:** `ui/imgui/src/ui/preview_pane.{h,cpp}` (T-F16, the
production exemplar — see also its dock-slot note in `layout.cpp`) and
`ui/imgui/src/ui/scaffold_demo_panel.{h,cpp}` (the scaffold-only minimal version).

---

## 6. Tables — `ImGui::BeginTable` + tokens

For row/column data that needs sortable headers or aligned numeric columns (BOM rollups, CAM tool
tables, requirements status): plain `ImGui::BeginTable`/`TableSetupColumn`/`TableHeadersRow`, with
row backgrounds still going through `theme::glassRowBackground` per-row (pattern 2) rather than
`ImGuiTableFlags_RowBg`'s default striping (which uses raw ImGui colors, not theme tokens).

**Code pointer:** `ui/imgui/src/ui/design_table_panel.cpp` and `ui/imgui/src/ui/cutlist_panel.cpp`
for the existing table panels to model column setup and numeric formatting from.

---

## 7. Iconography — `theme::icons::draw` / `inlineIcon` / `drawGlass`

Icons are named lookups into `ui/imgui/resources/icons/atlas.json` (union hand-drawn glyphs union
alias names — see `scripts/gui_lint.py`'s `ICONS_HAND_DRAWN`/`ICONS_ALIAS_NAMES` for the full set).
Never draw an icon by raw glyph/codepoint. Three call shapes, from `ui/imgui/src/theme/icons.h`:

- `icons::inlineIcon(name, size, col)` — draws at the current ImGui cursor, inline with text (used
  by `panel_scaffold.cpp`'s header icon).
- `icons::draw(dl, name, center, size, col)` — draws at an explicit draw-list position (toolbar
  buttons, HUD pills).
- `icons::drawGlass(dl, name, center, size, col, haloCol)` — adds a soft halo behind the icon, for
  icons painted directly over the viewport backdrop (contrast against variable luminance).

`scripts/gui_lint.py --rule icons` gates that every icon name you pass actually exists in the atlas
— run it (or the full gate) before shipping a new panel with icons.

---

## Do / Don't gallery

| Do | Don't |
|----|-------|
| `theme::rgba(pal().accent)` / `TCOL(accent)` / `TVEC(accent)` | `IM_COL32(45, 212, 191, 190)` literal |
| `theme::U(1.5f)` for an 8pt-grid-derived spacing value | `24.f` bare pixel literal |
| One `ui/abstract/*.h` per panel's state, no ImGui includes | View-model structs defined inline in the `.cpp` |
| Row pills (`glassRowBackground`) on transparent content | A full-panel `theme::glassBackground` wash under a list |
| A typed placeholder (pattern 4) for every non-happy-path state | An empty `BeginChild`/`EndChild` with nothing drawn |
| `#ifdef _WIN32` only inside `ui/imgui/src/platform/` | OS `#ifdef`s scattered through panel `.cpp` files |

---

## Screenshots

Headless verification for any new panel: `AI_CAD_SHOT=<scenario>` + `--shot out.png` (see
`docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` for the harness). The scaffold demo panel's scenario is
`AI_CAD_SHOT=scaffold_demo` — a one-shot UI scenario (not a `ScenarioRunner` step machine), so it is
unaffected by the step-machine offscreen-deadlock issue tracked as T-G13/issue #7. New panels that
only need a static screenshot (no multi-frame document mutation) should follow the same one-shot
pattern in `ui/imgui/src/ui/scenarios.cpp` (`configureShot`'s "Simple one-frame UI shots" group)
rather than adding a `ScenarioRunner` step machine.

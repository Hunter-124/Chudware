# host-ui track — full codebase review

Track: **host-ui** (ownership: `host/src/ui/**`, `host/assets/**`)  
Date: 2026-06-27  
Scope: Ribbon, sketch mode, property manager, ECAD panels, scenarios; ImGui lifetime, input handling, null checks.

---

## Fixed

### Null-safety / lifetime (applied)

1. **`sketch_mode.cpp` — `drawDatumSelect`** — Added `!st.renderer` to the early-out guard. The `originWins` branch projects plane corners via `st.renderer->camera()` while sibling pickers (`drawGhosts`, `drawRegionPicker`, `drawCurvePathPicker`, `hitTestDatumPlane`) already guarded the renderer.

2. **`viewport_panel.cpp` — `drawViewportPanel`** — Added `if (!st.renderer) return;` before `Renderer& renderer = *st.renderer`. Matches the pattern used in `drawOverlay` and other viewport helpers; currently masked because `app.cpp` only calls this when the renderer is wired.

3. **`view_selector.cpp` — `ViewSelector::draw`** — Added `!st.renderer` to the entry guard. `update()` receives a `Renderer&` from the caller (already non-null); `draw()` had the only unguarded deref.

4. **`property_manager.cpp` — `drawBody`** — Guard changed from `if (!active_)` to `if (!active_ || !form_)` for consistency with `buildPayload`, `schedulePreview`, and other entry points that already enforce both invariants.

5. **`icons.cpp` — `loadIcons`** — Hardened atlas loading:
   - `fread` return value checked against file size.
   - JSON parse + schema validation (`atlas_px`, `cell`) wrapped in a single `try/catch`.
   - Per-cell `contains()` guards; malformed cells are skipped instead of throwing.
   - Invalid/zero `atlas_px` dimensions rejected.

6. **`scenarios.cpp` — `topFace` lambda** — Added explicit `o.contains("centroid")` check before indexing, matching the pattern used elsewhere in the file (lines 496/512/623).

### Verified already correct (no change)

- **`sim_manager.cpp:2701-2706`** — `st.simClient->caps()` is already behind `if (st.simClient)` (Phase 1 flag was stale).
- **`sketch_mode.cpp` — `drawOverlay`** — Already returns early on `!st.renderer` (line 4699); `buildBasis` at 4845 is safe.
- **`drawAxisPicker` / `drawCurvePathPicker`** — Already null-check `pm` before `pm->route…`.
- **`doc_tabs.cpp` — `drawOneTab`** — `idx` always comes from `for (i … size())`; `saveDocTab` bounds-checks separately.

---

## Flagged

### Design smells (defer — not safe one-liners)

1. **`ecad/ecad_panel.cpp:78` — `AppState* g_app`** — Process-wide pointer set each frame by `drawEcadEditor` and read by `state()` / `ecadEditorMirror()` accessors. Safe in the single-threaded ImGui loop today; a reentrant or multi-document parallel draw would see stale state. **Suggested fix:** thread `AppState&` through the public ECAD accessors (small API-shaped refactor).

2. **`scenarios.cpp` — bare `st.propertyManager->` derefs** — Automation/shot paths (m1, multidoc) assume PM is always wired. Acceptable for scripted scenarios inside the full app; would need guards only if headless scenarios run without a PM instance.

3. **`ribbon_def.cpp` + `feature_surface_audit`** — ~31 ribbon commands (drawing/view layout/annotation) have no visible handler wiring (pre-existing gate failure in `scripts/check.py`). UI table is append-only and correct; handler gap is cross-track (`app.cpp` / engine).

### ImGui lifetime / stack balance

4. **`sim_manager.cpp` — push/pop counts** — Manual call-site tally (~21 push vs ~23 pop) is **not** a reliable imbalance signal (`PushStyleColor(3,…)` / `PopStyleColor(3)` move the stack by 3 per call). Spot audit of each `Begin…/End…`, `Push…/Pop…` block shows paired usage. **Recommendation:** verify under `IMGUI_USE_DEBUG_PARANOID` in a debug build if stack asserts appear; do not blind-fix from counts alone.

5. **`property_manager.cpp` — `draw()` floating fallback** — When the FeatureManager dock is invisible, opens `##PMFloating` with correct `Begin`/`End` pairing on all paths (verified). `drawBody` early-returns are safe because the caller owns the window `End`.

6. **`widgets/ribbon.cpp` — deferred flyout open (`s_flyoutOpenReq`)** — Correct fix for PushID scope mismatch between button and `BeginPopup`. Flyout rows balance `PushStyleColor(3)` / `PopStyleColor(3)` and `PushID` / `PopID` per row.

### Input handling (reviewed, no bugs found)

| Area | Pattern | Notes |
|------|---------|-------|
| **Ribbon** | `InvisibleButton` + custom draw | Disabled/stretch rows use `BeginDisabled` or grey tint; flyout deferred open avoids ID hash mismatch. |
| **Sketch mode** | `imgHovered` gates viewport picks | PM owns picks first (`activeWellWantsAxis/Plane/Path`); corner ✓/✗ excluded from canvas hover; `WantTextInput` gates undo/copy/paste; Enter-in-PM gated in `drawBody`. |
| **Property manager** | Enter commit | Only when PM window focused/hovered and `!io.WantTextInput` so chat Enter doesn't commit. |
| **View selector** | Space/Esc | Opens on Space when viewport hovered and not typing; dismisses on empty click or Esc. |
| **ECAD canvas** | `IsItemDeactivated` for click | Avoids click-on-drag; R/F/Delete gated on `io.hovered && !WantTextInput`; route Esc/right-click ends chain. |

### gui_lint scale debt (explicitly out of scope)

7. **`host/src/ui/**` — 285 findings** (`scale=22`, `scale-ws=263` per `gui_lint.py --rule scale`):
   - **Real DPI gaps** (`scale`): `legacy_chat.cpp`, `sim_manager.cpp` (button widths, chart annotations), `viewport_panel.cpp` overlay circles — each needs `* ui::uiScale`.
   - **World-space art** (`scale-ws`): `sketch_mode.cpp`, `ecad_canvas.cpp`, `drawing_panel.cpp` — paper/canvas coordinates; correctly excluded from gating.
   - Batch into a dedicated sizing PR; fixing all 285 risks visual regressions.

### Assets

8. **`host/assets/test_icons.py`** — Solid coverage (contract keys, PNG decode, layout bounds, determinism). Does not test the C++ loader's malformed-json path; candidate test listed below.

9. **`host/assets/icons/atlas.json`** — Schema matches `make_icons.py` output; `gui_lint --rule icons` reports **0 findings** on `host/src/ui`.

### Cross-track (not host-ui ownership)

10. **`host/src/app.cpp` / `host/src/core/doc_tabs.cpp`** — `st.propertyManager` raw pointer borrow lifetime. UI code in this track consistently null-checks before use (`viewport_panel`, `feature_tree`, `sketch_mode`, `title_bar`). Confirm borrow is set before first draw in the `host-core`/`app` track.

---

## Tests added

None (per guardrails: do not add to CMakeLists without explicit request).

**Candidate tests for a follow-up pass:**

- `test_icons_atlas_robust` — feed `loadIcons` a malformed `atlas.json` (missing `cell`, cell missing `w`) and assert no throw + graceful stderr (covers `icons.cpp` hardening).
- `test_sketch_drawDatumSelect_null_renderer` — `drawDatumSelect` with `st.renderer == nullptr` returns `false`.

Existing: `python host/assets/test_icons.py` (atlas pipeline; run from repo root with managed venv).

---

## Lint / tooling

| Gate | Result |
|------|--------|
| `gui_lint --rule icons` on `host/src/ui` | **0 findings** |
| `gui_lint --rule theme` on `host/src/ui` | **0 findings** |
| `gui_lint --rule scale` on `host/src/ui` | **285 findings** (flagged, not fixed) |

---

## Files touched

| File | Change |
|------|--------|
| `host/src/ui/sketch_mode.cpp` | Renderer null guard in `drawDatumSelect` |
| `host/src/ui/viewport_panel.cpp` | Renderer null guard in `drawViewportPanel` |
| `host/src/ui/view_selector.cpp` | Renderer null guard in `ViewSelector::draw` |
| `host/src/ui/property_manager.cpp` | `form_` guard in `drawBody` |
| `host/src/ui/icons.cpp` | Robust atlas.json load (fread, schema, try/catch) |
| `host/src/ui/scenarios.cpp` | `centroid` contains check in `topFace` |
| `docs/review/host-ui.md` | This report |

---

## Area notes (read-only review)

### Ribbon (`widgets/ribbon.cpp`, `ribbon_def.cpp`)

- SolidWorks-style tab/group layout with priority-collapse overflow and last-used variant tracking.
- Contextual tab visibility (Drawing vs 3D, Simulation, PCB) is correct.
- Hidden-small dedup (`sk_ellipse` vs `sk_ellipse_big`) and parent-default mapping (`sk_ellipse_big` → `sk_ellipse`) are well-documented in code.
- Hardcoded `IM_COL32(255,255,255,…)` / `IM_COL32(128,128,128,…)` in flyout icon tints are intentional disabled/enabled tints on atlas glyphs (not theme-role violations for icon texture modulation).

### Sketch mode (`sketch_mode.cpp`, `sketch_mode.h`)

- Large file (~8k lines) with cohesive overlay/pick/dim/tool subsystems.
- Plane-pick, datum select, axis/path pickers, and active-sketch tools are correctly prioritized in `viewport_panel` call order.
- ImGui popups (`drawTextPopup`, `drawAirfoilPopup`, `editOverDefine`) balance style pushes and use `escHandled` to avoid double-Esc.

### Property manager (`property_manager.cpp`, `pm_forms.cpp`)

- Selection-first well re-homing in `drawBody` fixes method-combo well visibility (documented C37 behavior).
- AI assist, preview debounce, and commit/cancel paths are consistent.
- `pm_forms.cpp` is declarative schema only; no runtime issues spotted.

### ECAD (`ecad/ecad_panel.cpp`, `ecad_canvas.cpp`, `ecad_mirror.cpp`)

- Per-tab `EcadEditorState` on `DocTab::ecadState` is the right multi-doc model; `g_app` is the remaining smell.
- Canvas navigation, place/route/select tools, and property inspector are feature-complete for the scaffold stage.
- Comment at file top: "NOT yet build-verified" — integration risk is cross-track.

### Scenarios (`scenarios.cpp`)

- m1/m2/multidoc shot drivers are automation code, not user UI; JSON access is mostly defensive.
- Uses PM and doc mirror heavily; appropriate for regression screenshots.

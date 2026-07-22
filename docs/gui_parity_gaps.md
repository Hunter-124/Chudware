# GUI parity gaps — host/ fixes not yet in ui/imgui

> **SUPERSEDED (2026-07-22):** the `ui/imgui` shell this audit targeted was retired and
> deleted; `host/` is the keeper GUI, so the "trapped fix" premise is inverted — fixes in
> `host/` ARE in the shipping app. Kept for history; the two open interactive items below
> (T-BL37 etc.) remain valid as `host/` feature tasks and are tracked on the board.

Findings from a trapped-fix audit (legacy `host/` vs the keeper `ui/imgui/`) run during GUI
unification (2026-07-06). Fixes that landed only in `host/` never reach the shipping Linux app.

**Resolved in the same sweep** (see git log): T-BL34 (CAM icons), T-BL35 (surf_revolve datum
axis, in `ui/abstract/panel_spec.cpp`), T-BL42 (already at parity), T-BL47 (2D-import Top orient),
T-BL31(b) (ribbon-shot toast suppression). BL02 (DXF/DWG import filter). X1 `/FI` MSVC parity.

The two below are **unported INTERACTIVE features** — they need mouse-pick / active-PM-well
verification in the running app (not headless), so they were filed here rather than shipped blind.

---

## 1. Origin viewport point-picker (T-BL37) — PARTIAL

4/5 pieces already in ui/imgui (`SelectorKinds.point`; FM `hiddenFeatures{"point.origin"}` +
row select/eye/guard at `feature_tree.cpp:2543-2597`; synthetic datum injection at
`viewport.cpp:142-220`; PM `activeWellWantsPoint`/`routeDatumPoint` at `property_manager.cpp:1038,1128`).

**Missing:**
- `drawPointPicker()` — viewport screen-space hit-test + accent highlight + `routeDatumPoint()`
  on click. Host: `host/src/ui/sketch_mode.cpp:4555-4629` (decl `sketch_mode.h:184-191`), wired at
  `host/src/ui/viewport_panel.cpp:2498-2537`. No equivalent in ui/imgui
  `sketch/sketch_mode.h` / `sketch/sketch_overlay.cpp` / `viewport.cpp`.
- FM Origin body-click routing: host `feature_tree.cpp:2296-2302` routes into the active PM point
  well; ui/imgui `feature_tree.cpp:2594-2598` only sets `selectedFeatureId`.

**Port:** add `drawPointPicker()` to `ui/imgui/src/ui/sketch/{sketch_mode.h,sketch_overlay.cpp}`
as a direct port (adapt `st.propertyManager`→ui/imgui PM accessor, `ui::uiScale`→`theme::S()`,
`st.hiddenFeatures`→`featureTreeHiddenFeatures()`); wire into `viewport.cpp`'s non-sketch block and
guard the model id-buffer pick with `!pointPickerHot`; extend the FM click to route the point well.

---

## 2. Extrude dimension-chips (T-BL31 nit a) — PARTIAL / feature not ported

The extrude parameter-callout chips (depth/draft chips that float in the viewport, C27-A) are a
**host-only feature** — `ui/imgui/src/ui/viewport.cpp` has no chip/callout draw loop at all.
The dodge fix (`overlayColumnEdge(1)` so chips don't hide behind the Copilot column) is therefore
moot until the chip feature itself is ported. The infra is ready: `ui::overlayColumnEdge` is
declared in `ui/imgui/src/ui/layout.h:46`.

**When porting the chips:** compute `rightBound` from `ui::overlayColumnEdge(1)` (clamped to the
viewport) and use it for both the dock position and the clamp guard — mirror
`host/src/ui/viewport_panel.cpp:3521-3564`.

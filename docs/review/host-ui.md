# host-ui track — Phase 1 review

Track: **host-ui** (ownership: `host/src/ui/**`, `host/assets/**`)
Branch: `feat/gui-rewrite`
Date: 2026-06-27
Scope: Ribbon, sketch mode, property manager, ECAD panels, scenarios; ImGui lifetime, input handling, null checks.

> Phase 1 was halted early by parent directive before any code edits were made.
> This report records findings gathered from static reading + `gui_lint.py` runs.
> **No source files were modified.** All items below are **Flagged** for a follow-up pass.

---

## Fixed

None — no edits were made this phase (review was interrupted before the fix pass).

---

## Flagged

### Null-safety / lifetime

1. **`host/src/ui/sketch_mode.cpp:4636`** — `const auto& cam = st.renderer->camera();` in
   `SketchMode::drawDatumSelect` is reached on the `originWins` branch **without** a
   `st.renderer` null check. The sibling datum-pickers (`drawGhosts` 3650, `drawRegionPicker`
   3963, `drawCurvePathPicker` 4446, `hitTestDatumPlane` 4159) all guard `!st.renderer` first;
   this one only checks `active_`/`imgHovered`/`pm`. **Risk:** null deref if `drawDatumSelect`
   is ever called before the renderer is wired (headless shots, startup race, future caller).
   **Suggested fix:** add `if (!st.renderer) return false;` near the top of `drawDatumSelect`
   (alongside the existing `active_`/`imgHovered` early-out). One-line, in-lane, safe.

2. **`host/src/ui/viewport_panel.cpp:1288`** — `Renderer& renderer = *st.renderer;` at the top
   of `drawViewportPanel` dereferences unconditionally. Every other viewport entry point guards
   `st.renderer`. **Risk:** latent null deref; currently masked because `app.cpp:2637` only
   calls this from the main loop where the renderer is always set.
   **Suggested fix:** early `if (!st.renderer) return;` before the deref (matches the pattern
   used in `drawOverlay`, `hitTestDatumPlane`, etc.). Safe, in-lane.

3. **`host/src/ui/view_selector.cpp:326`** — `Renderer& renderer = *st.renderer;` in
   `ViewSelector::draw` after only `if (!m_active) return;`. No renderer null guard.
   **Risk:** same latent class as #2; `ViewSelector` is only armed from viewport code where the
   renderer is non-null, so it is not currently triggerable.
   **Suggested fix:** add `if (!st.renderer) return;` after the `m_active` check.

4. **`host/src/ui/icons.cpp:85-101`** — `jroot["atlas_px"]` / `jroot["cell"]` and the
   `it.value()["x"].get<int>()` accesses sit **outside** the `try {} catch (...)` block that
   only wraps `nlohmann::json::parse(buf)` (lines 78-83). A structurally-valid but
   schema-missing `atlas.json` (no `atlas_px`/`cell` keys, or a cell missing `x`/`y`/`w`/`h`)
   throws `nlohmann::json::exception` that is **not caught** → `loadIcons` propagates the
   exception to the caller.
   **Risk:** startup crash on a hand-edited / partially-generated atlas.
   **Suggested fix:** widen the try/catch to cover the cell-parsing loop, or use
   `jroot.value("atlas_px", ...)` + `contains()` guards per cell. In-lane (`host/assets`-adjacent
   loader lives under `host/src/ui/icons.cpp`).

5. **`host/src/ui/icons.cpp:66`** — `std::fread(&buf[0], 1, sz, f);` return value is ignored.
   A short read leaves `buf` with partial atlas bytes; the subsequent `json::parse` is caught,
   so this fails safe but silently. **Risk:** low (caught downstream); masks I/O errors.
   **Suggested fix:** check the returned count against `sz` and bail with the existing stderr
   message. Optional.

6. **`host/src/ui/sim_manager.cpp:2703`** — `nlohmann::json caps = st.simClient->caps();`
   needs a confirmed `st.simClient` null guard at the function entry (the sibling paths at
   1241/1331/1416 all guard `if (!st.simClient) return;`).
   **Risk:** potential null deref if this code path is reachable without a sim client.
   **Suggested fix:** verify/add `if (!st.simClient) return;` at the top of the enclosing
   function (matching the pattern at 1241/1331/1416). Needs a 5-line context check before
   editing — flagging, not fixing, per halt directive.

7. **`host/src/ui/property_manager.cpp:2119`** — `PropertyManager::drawBody` guards only
   `if (!active_) return;`, then later dereferences `form_` (e.g. 2230 `getIcon(form_->icon,...)`,
   2254 `formDescription(form_->type)`). Other methods use the stronger `if (!active_ || !form_)
   return;` (862, 1028, 1267). **Risk:** low — `openForCommand`/`openForEdit` set `form_` before
   `active_`, and `close()` clears both, so `active_==true` implies `form_!=null` today. But the
   invariant is not enforced at the guard.
   **Suggested fix:** change the `drawBody` guard to `if (!active_ || !form_) return;` for
   defense-in-depth and consistency with the other entry points. Safe, in-lane.

8. **`host/src/ui/ecad/ecad_panel.cpp:78`** — process-wide mutable `AppState* g_app = nullptr;`
   written each frame by `drawEcadEditor` and read by `state()`. **Risk:** design smell, not a
   bug — a reentrant or multi-thread caller could see a stale `g_app`. Single-threaded ImGui
   frame loop keeps this safe today.
   **Suggested fix (flag, don't fix):** thread `AppState&` through the ECAD accessors instead of
   the file-static pointer. This is a small refactor with public-API-shaped callers — defer to a
   design pass, not a Phase 1 fix.

### JSON access robustness

9. **`host/src/ui/scenarios.cpp:105`** — `double z = o["centroid"][2].get<double>();` relies on
   the surrounding `try {} catch (...) {}` (103-108) to handle a missing/non-array `centroid`.
   The sibling face resolver at 496 uses the explicit `s.contains("centroid") ? ... : -1e9`
   pattern. **Risk:** low (caught), but the throw/catch path is fragile and inconsistent with
   the rest of the file.
   **Suggested fix:** mirror the `contains()` form used at 496/512. Optional consistency fix.

10. **`host/src/ui/doc_tabs.cpp:63,179`** — `AppState::DocTab& t = st.docTabs[idx];` — verify
    `idx` is bounds-checked by every caller before this deref. `doc_tabs.cpp` itself is
    defensive elsewhere (e.g. 168-169 iterate by index with size check). **Risk:** low; flag for
    a caller audit, not a blind fix.

### ImGui stack balance (needs verification)

11. **`host/src/ui/sim_manager.cpp`** — call-site counts for the push family
    (`PushStyleColor`/`PushStyleVar`/`PushID`/`BeginDisabled` ≈ 21) vs the pop family
    (`PopStyleColor`/`PopStyleVar`/`PopID`/`EndDisabled` ≈ 23) differ by ~2. This is **not**
    asserted as a bug — `PushStyleColor(3,…)`/`PopStyleColor(3)` are single call sites that move
    the stack by 3, and a manual count of call sites is not a reliable imbalance signal.
    **Risk:** if real, an unmatched pop would hit ImGui's `PushStyleColor` underflow assert.
    **Suggested approach:** run with ImGui's `IMGUI_USE_DEBUG_PARANOID` / assert build, or do a
    careful paired-walk of each `Begin…/End…`, `Push…/Pop…` block in `sim_manager.cpp`'s draw
    routines. Flagging for Phase 2 verification — do not blind-fix.

### gui_lint scale debt (do NOT fix in Phase 1)

12. **`host/src/ui/**` — ~295 `gui_lint` `scale`/`scale-ws` findings.** Spot-checked examples:
    - `legacy_chat.cpp:782,784` — raw `12` in `AddCircle` (px, not `*uiScale`)
    - `sim_manager.cpp:1859,1870` — raw `120` in `ImVec2` for button width
    - `sim_manager.cpp:2273,2509,2556-2559` — raw px literals in `AddLine`/`ImVec2`
    - `viewport_panel.cpp:944` — raw `4.0`/`12` in `AddCircle`
    - `drawing_panel.cpp:668,713,812,…` — `scale-ws` (world-space canvas art; **non-gating** by
      linter design — these are paper-space drawing coordinates, not DPI chrome)

    Per the track guardrails, **scale debt is flagged, not fixed** in Phase 1. The `scale-ws`
    findings in `sketch_mode.cpp` / `ecad_canvas.cpp` / `drawing_panel.cpp` are world-space art
    and are correctly excluded from the gating rule. The `scale` findings in
    `legacy_chat.cpp` / `sim_manager.cpp` / `viewport_panel.cpp` are real DPI chrome gaps and
    should be batched into a dedicated sizing pass (each fix is a `* ui::uiScale` multiply, but
    doing 295 in Phase 1 risks regressions and is explicitly out of scope).

### Cross-track (not in host-ui ownership — flagged for the right owner)

13. **`host/src/app.cpp:897,949`** and **`host/src/core/doc_tabs.cpp:50`** — these set
    `st.propertyManager` (raw pointer borrow of the per-`DocSession` `pm`). They live outside
    `host/src/ui/**` so are **not in this track's ownership**. The UI code in this track
    consistently null-checks `st.propertyManager` before use (verified across
    `viewport_panel.cpp`, `feature_tree.cpp`, `sketch_mode.cpp`, `title_bar.cpp`,
    `scenarios.cpp`). Flagging only so the `host-core`/`app` track owner confirms the borrow
    lifetime is sound.

---

## Tests added

None. No new tests were created this phase (review halted before the fix/test pass). Candidate
tests for a follow-up pass (to be placed in the host-ui test dir, **not** added to CMakeLists per
guardrails):

- `test_icons_atlas_robust` — feed `loadIcons` a malformed `atlas.json` (missing `cell` key,
  cell missing `w`) and assert it returns without throwing (covers finding #4).
- `test_sketch_drawDatumSelect_null_renderer` — call `drawDatumSelect` with `st.renderer ==
  nullptr` and assert it returns `false` rather than dereferencing (covers finding #1, once
  fixed).

---

## Files touched

None (report-only). `docs/review/host-ui.md` is the only file written this phase.

---

## Notes for Phase 2

- The clearest in-lane, safe fixes for Phase 2 are findings **#1, #2, #3, #4, #7** — each is a
  one-to-three-line null/form guard matching an existing pattern in the same file, with no
  contract or API impact.
- Finding **#8** (ECAD `g_app` global) is a design smell — flag, do not fix in Phase 1.
- Finding **#11** (ImGui push/pop balance) needs a debug-build verification run, not a blind
  pop-count "fix."
- Finding **#12** (295 `gui_lint` scale findings) is explicitly scoped out per the track
  guardrails; batch into a separate sizing PR.
- `gui_lint.py --rule icons` and `--rule theme` both report **clean** on `host/src/ui` (0
  findings) — the icon-name atlas bug class (the original magenta-`?` PM buttons) is resolved.

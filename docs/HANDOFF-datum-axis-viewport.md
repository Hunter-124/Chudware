# Handoff: datum-axis picking + viewport UX (revolve axis, selection, centering, sketch orbit)

**For:** a fresh agent (use subagents freely — read-only Explore for investigation, then implement).
**Date:** 2026-06-19. **Author:** prior session (Claude). Written because 4 of 5 requested fixes
"compiled but don't behave correctly" — they need interactive testing the prior session couldn't do.

---

## TL;DR — what's broken and the single biggest lead

The user asked for 5 things. **Only #5 (dashed axis) actually works.** The other four compile but
misbehave. **There is one CONFIRMED root-cause bug that breaks #1 and #2** (read it first):

> **CONFIRMED BUG — axis hit-test length mismatch.**
> `hitTestDatumAxis()` (`host/src/ui/sketch_mode.cpp:~3858`) sizes the *clickable* axis segment with
> `r.modelRadius()` (== `Renderer::radius_`), which is **0 whenever the document has no solid body**.
> A sketch is not a body, so during **Revolve** (you have a sketch + a datum axis but no solid yet)
> `radius_==0` → the fallback makes the clickable half-length `1.0*1.6 = 1.6 mm`.
> But the *drawn* axis (`Renderer::drawRefDatums`, `renderer.cpp:~1294`) uses
> `e = (radius_>0?radius_:kEmptySceneRadius=35) ; L = e*1.6 = 56 mm`.
> So the visible amber line is ~56mm long but the pickable region is ~1.6mm around the origin — you
> click the line tens of mm away from the hit segment and nothing registers.
> **Fix:** in `hitTestDatumAxis`, use the renderer's *effective* radius, not `modelRadius()`.
> `r.camera().radius` already equals `sceneR = (radius_>0?radius_:kEmptySceneRadius)` (set in
> `drawCurrent`, `renderer.cpp:754`). So `float ext = r.camera().radius;` (drop the `<=0 → 1.0`
> fallback) makes the clickable segment coincide with the drawn line. **Verify** the same `L` is used
> in both places (drawRefDatums and hitTestDatumAxis) for ALL cases (body / no-body / empty doc).

Do NOT assume the above fully fixes #1/#2 — verify interactively (see "How to test"). There may be
additional issues (well not activated, overlay not called, pick radius too small at glancing angles).

---

## The live engine is C++ (not Python) — verify first

`host/src/worker_client.cpp::cppDocsFlag()` defaults **true**. The runtime log
(`aicad_app.log`, or set `AI_CAD_LOG_FILE`) shows `cppDocs=1 docIsCpp=1 dllLoaded=1`. So the live
doc engine is the pure-C++ core (`core/`, built to `libaicad.dll`). Any *engine* change must go in
`core/doc/*.cpp` and be rebuilt (see Build). The Python engine (`engine/doc/*`) is oracle/fallback
only (`AI_CAD_CPP_DOCS=0`). **An old memory says "docs rolled back to Python" — that is STALE.**

The datum *display* pipeline already works end-to-end (the dashed axis renders), so:
`C++ core compile_document → result.datums → ops_response "datums" → DocMirror::refDatums() →
app.cpp pushes to Renderer::setRefDatums() → Renderer::drawRefDatums()`  — all good. The engine even
accepts a datum-axis ref for revolve already (`core/doc/document.cpp resolve_axis_ref`, verified:
revolving a profile about a `{"kind":"datum","feature":fid}` axis yields the correct tube volume).
**So every remaining problem is host-side interaction/camera, not engine or rendering.**

---

## The 5 requests & current status

| # | Request | Status | Where |
|---|---------|--------|-------|
| 1 | Click the reference axis while **revolving** (fill the revolve "Axis" field) | **BROKEN** (hit-test length bug above; also re-verify the picker is invoked) | `SketchMode::drawAxisPicker`, `hitTestDatumAxis` |
| 2 | Click the axis to **select it generally** (outside any picker) | **BROKEN** (same hit-test bug; also confirm it's wired + reachable) | `SketchMode::drawDatumSelect` |
| 3 | **Center** the view on the model/sketch center, not the origin | **BROKEN / not right** | `Renderer::setOrbitCenterOverride`, `SketchMode::sketchOrbitCenter`, viewport wiring |
| 4 | **Orbit/pan while sketching** (SolidWorks MMB) | **BROKEN / not right** | viewport_panel sketch-mouse branch + `s_sketchRotated` lock relax |
| 5 | Axis **thicker + dashed** | **WORKS** | `Renderer::drawRefDatums` |

---

## Exactly what the prior session changed (so you can audit / revert / fix)

All host-only except the datum-display pipeline (which works). Key files & symbols:

**Renderer** (`host/src/renderer.{h,cpp}`)
- `struct RefDatum`, `setRefDatums()`, `drawRefDatums()` — draws axes (dashed, ~width 3),
  planes (quads), points (cross), csys (RGB). Called after `drawReferencePlanes` in `drawCurrent`.
- `setSelectedDatumId(id)` — `drawRefDatums` brightens/thickens the datum whose `id` matches.
- `setOrbitCenterOverride(on, c)` — in `drawCurrent`, `target = (on ? c : center_) + pan_`.

**DocMirror** (`host/src/core/doc_mirror.{h,cpp}`) — `refDatums()` parsed in `parseOpsResponse`.

**PropertyManager** (`host/src/ui/property_manager.{h,cpp}`)
- `PMSelKinds` gained `bool plane, axis`.
- `activeWellWantsAxis()/activeWellWantsPlane()`, `routeDatumAxis(fid,label)` (emits
  `{"kind":"datum","feature":fid}`), `routeDatumPlane(datumId)` (emits a reserved string).
- `routePick::same()` and `anchorFromSel` guarded against non-object (string) selectors.

**PM forms** (`host/src/ui/pm_forms.cpp`)
- `K_PLANE {face,plane}`, `K_EDGEAXIS {edge,axis}`, `K_FACEEDGEAXIS {face,edge,axis}`.
- revolve "axis" well → `K_EDGEAXIS`; circular_pattern axis well → `K_FACEEDGEAXIS`;
  ref_axis "Two Planes" + ref_plane "Mid Plane" wells → `K_PLANE`.

**SketchMode** (`host/src/ui/sketch_mode.{h,cpp}`)
- static `hitTestDatumAxis(...)` — screen-space distance-to-segment over `docMirror.refDatums()`
  axes. **← has the length bug.**
- `drawAxisPicker` (PM axis well active → `routeDatumAxis`), `drawDatumSelect` (general → set
  `st.selectedFeatureId`), `drawPlanePicker` (origin-plane pick for plane wells), `pmPlaneHover()`.
- `sketchOrbitCenter(Vec3&)` — world bbox center of `ents_` (or plane origin) via `frame_`.
- `enterForFeature` now zeroes `st.pan` (restored on exit) to center the sketch.
- **Gotcha:** `sketch_mode.h` did NOT include the math header — a `Vec3&` in a signature broke the
  whole class. Fixed by forward-declaring `struct Vec3;` at global scope in the header.

**viewport_panel** (`host/src/ui/viewport_panel.cpp`)
- Calls `drawPlanePicker`/`drawAxisPicker`/`drawDatumSelect`; `planePickerHot`/`axisPickerHot`/
  `datumSelectHot` suppress the id-buffer model pick.
- Each frame: `setOrbitCenterOverride(inSketch ? sketchCenter : off)` and
  `setSelectedDatumId(st.selectedFeatureId)`.
- `s_sketchRotated` (file static): the per-frame normal-to lock (~line 1237,
  `if (inSketch && !s_sketchRotated) cameraLock`) releases once the user MMB-orbits in a sketch;
  the `else if (imgHovered && inSketch)` mouse branch added MMB orbit / Ctrl+MMB pan / Shift+MMB zoom.

---

## Hypotheses to investigate per broken item

**#1 Revolve axis pick** — primary: the length bug. Secondary checks:
- Is `drawAxisPicker` even reached? It returns early unless `pm->active() && pm->activeWellWantsAxis()
  && imgHovered`. The user must have clicked the revolve **Axis** well to make it the *active* well.
  Add a temporary `st.addLog(...)`/`AI_CAD_LOG_FILE` trace in `drawAxisPicker` to confirm it runs and
  what `hitTestDatumAxis` returns (best distance, endpoints).
- Pick radius is `9 * uiScale` px — fine head-on, but an axis seen near edge-on projects short; verify.
- Confirm `viewport_panel` actually calls `drawAxisPicker` in the revolve PM state (it's gated on
  `sketch != nullptr && !inSketch && !st.sketchPlanePickActive && !planePickerHot`).

**#2 General axis select** — same length bug; plus confirm `drawDatumSelect` is wired and not
shadowed by the model-pick (it must return `true`/hot to suppress the id-buffer click). Verify
`st.selection` / `st.selectedFeatureId` / `st.treeScrollToSelection` are the right fields and that
setting them actually highlights the tree row + the datum (renderer emphasis via `setSelectedDatumId`).

**#3 Centering** — the override is set every frame while `inSketch`, but:
- `sketchOrbitCenter` reads `ents_`; if `ents_` is empty at the moment sampled it returns the plane
  origin — confirm `ents_` is populated and the lift via `frame_` (row-major) is correct.
- The user may also mean the **model** (not just sketch) orbits around origin. The prior session
  *assumed* `center_` (model bbox center) was already correct — VERIFY THAT AT RUNTIME. Log
  `renderer.modelCenter()` / `radius_` for a part built offset from origin. If `center_` is wrong
  (e.g. frozen by `setFramingFrozen`, or the AICM bbox is off), that's a different fix.
- Consider: does the user want auto-**fit/zoom** so the part fills the view (it may just be off to
  one side), vs. orbit-pivot? Clarify by testing what "centered" should look like.

**#4 Orbit while sketching** — confirm the `else if (imgHovered && inSketch)` branch executes and
that `ImGui::IsMouseDragging(ImGuiMouseButton_Middle)` fires inside the viewport image (another
widget/handler may consume MMB; or the lock at ~1237 still wins because `s_sketchRotated` is being
reset every frame — check its reset condition `if (!inSketch) s_sketchRotated=false;`). Also confirm
nothing else in the frame re-pins `st.yaw/st.pitch` during sketch. Trace with logs.

---

## How to build

- **Host only** (most fixes are host): `./build.bat` (default — compiles host + stages everything
  into `dist\AI-CAD\`). `build.bat host` compiles without staging. Close any running `AI-CAD.exe`
  first (exe lock) — the build copies the exe.
- **Engine (C++ core)** — only if you touch `core/`:
  `MSBuild core\build-occt\aicad.vcxproj /p:Configuration=Release /m` → `libaicad.dll`, then
  `./build.bat` stages it into `dist`. (MSBuild path:
  `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe`.)

## How to TEST (the prior session's gap — DO THIS, don't trust compile)

`--ui-screenshot` is HEADLESS and **cannot drive mouse clicks/drags**, so it cannot validate any of
#1–#4. You MUST test interactively. Options:
- Drive the real app with the **computer-use MCP** (open `dist\AI-CAD\AI-CAD.exe`, sketch a profile,
  open Revolve, click the Axis well, click the axis, drag MMB, etc.) and screenshot to verify.
- Or hand the user a tight checklist and iterate. The user is a SolidWorks expert and tests builds.
- Engine-only checks can use the ctypes harness: `tools/cpp_parity.py::CppCore` →
  `core.call("doc_new",...)`, `core.call("doc_apply_ops", {doc_id, base_revision, ops})`. The prior
  session verified revolve-about-datum-axis this way (tube volume π(R²−r²)h exact).

Add temporary `AI_CAD_LOG_FILE` tracing in the pickers to see what fires; remove before finishing.

## Suggested subagent plan

1. Apply the confirmed length-bug fix (#1/#2) first; build; interactively verify the revolve + general
   axis click with computer-use. (Small, high-confidence.)
2. In parallel, one Explore subagent each to deeply trace #3 (centering) and #4 (sketch orbit) at
   runtime with added logging, since both are "compiles but wrong" and need observation, not just code
   reading. Have them report the actual runtime values (center_, radius_, which mouse branch runs,
   whether the lock re-pins).
3. Implement fixes from those findings; re-test each interactively.

## Key gotchas (learned this session)

- C++ core is the LIVE engine (cppDocs default true); the "Python default" memory is stale.
- `sketch_mode.h` lacks the math header — forward-declare `struct Vec3;` for by-ref signatures.
- `glLineWidth(3–4.5)` for the dashed axis is driver-dependent; if a machine caps line width the
  axis won't look thick (switch to camera-facing ribbon quads if needed).
- Datum geometry is drawn in the renderer overlay pass, NOT the id-buffer — that's why picking is a
  screen-space hit-test (`hitTestDatumAxis`), and why planes/points have no picker yet.
- Related memory file: `~/.claude/.../memory/datum-reference-geometry.md` has the full change log.

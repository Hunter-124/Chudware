---
name: gui-viewport
description: Chudware 3D viewport specialist — OpenGL rendering, orbit camera, mesh display, face/edge picking, selection highlighting, HUD overlays, section views, splat/point rendering. Use for any task about the 3D view, what's drawn in it, how clicks select geometry, or viewport visual effects. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Viewport Specialist

Scope: the 3D scene and everything drawn in or picked from it. Panel chrome around the
viewport (docking, glass columns) belongs to `gui-theme-layout`; ribbon buttons that trigger
view commands belong to `gui-ribbon-commands`. Shared-file ownership, waves and verification:
`gui-orchestrator`.

## File map

| File | Role |
|---|---|
| `host/src/ui/viewport_panel.cpp/.h` | Viewport panel: input handling, gizmo, selection UX, per-frame draw of the 3D region |
| `host/src/renderer.cpp/.h` | Core OpenGL renderer: orbit camera, mesh render, shaders, thumbnail/transparent render path (used by the icon pipeline) |
| `host/src/render/id_pass.cpp/.h` | ID-pass picking: renders face/edge IDs to an offscreen buffer; a click reads back the ID under the cursor |
| `host/src/render/overlays.cpp/.h` | Native GL scene overlays: selection washes, simulation indicators, and timed/glowing CAM path batches |
| `host/src/render/splat_renderer.cpp/.h`, `re_points.cpp` | Gaussian-splat / point-cloud rendering (reverse-engineering capture views) |
| `host/src/render/aicm_loader.cpp` | Loads `.aicm` mesh artifacts |
| `host/src/mesh.cpp` | Host `Mesh` data (vertices, faces, normals) |

## Data flow

libchudware (in `chudware-hostd`) tessellates the document into an AICM file → `doc.changed`
events drained per frame in `host/src/core/engine_events.cpp` → `AppState::loadDocMesh` /
`loadDocMeshAsync` parse it and feed the renderer, ID pass and overlays → user click → id_pass
readback → `render::PickHit` added to `AppState::selection` → PropertyManager / feature ops
consume the selection. Background tabs keep their parsed mesh on `DocSession::cachedDocMesh`;
per-document state is parked on `DocSession` across tab switches
(`captureDocState`/`restoreDocState`).

CAM generation follows a sibling path: `cam_panel.cpp`/`CamMirror` retain the active job's
classified world-space segments, global timing, direction glyphs, stock and simulation frames;
`viewport_panel.cpp` draws them through `Overlays` inside the renderer's existing color FBO.
CAM overlays are Manufacture-only, excluded from the ID pass, section-clipped, and depth-aware.
The remnant-stock aux mesh cache key is `{camSimGen(), frameIndex}` so a new simulation can never
reuse the old frame-zero upload.

## Gaussian reference rendering

- `render/splat_renderer.cpp/.h` loads validated ASCII/little-endian PLY with log
  scales, wxyz quaternions, opacity logits and SH degree 0–3. It projects full
  anisotropic covariance, not point sprites. CPU bounds include three-sigma extents.
  Loader and Brush training share a 2,000,000-Gaussian limit; input files cap at 2 GiB.
- Keep immutable Gaussian attributes in the data texture and update only sorted
  indices for camera motion. Subsequent large-cloud sorts run asynchronously.
  Match Brush's projection Jacobian clamp: image-plane NDC is clamped to ±1.3 before
  differentiating. Unbounded offscreen depth-elongated splats otherwise smear over
  the full image. Preserve SH direction, quaternion order and source-over blending.
- `ui/reveng/reveng_workspace.cpp` owns the calibrated camera and interactive
  orbit/pan/zoom. `view_camera` comes from the matching registered capture batch,
  including its roll/up basis and fx/fy/principal point. Initial bounds-fitting is
  not a substitute for that camera. A new SfM batch changes coordinate frame; do not
  attach its camera to an older PLY.
- `app.cpp` draws the RE previews after ImGui. `kit::previewSlotRect` supplies the
  complete projection viewport; `previewSlotClip` supplies its visible scissor.
  Clear depth with OpenGL/GLES `glClearBufferfv`, not desktop-only clear-depth APIs,
  and restore caller GL state. Worker threads parse files but never touch GL.
- Real GL smoke: `CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=reveng_splat
  CHUDWARE_REPO="$PWD" ./host/build-linux/bin/chudware --ui-screenshot scratch/re.png`.
  Optional `CHUDWARE_SPLAT_SMOKE_PATH` uses an actual trained PLY. The smoke checks
  two rendered views, rotated anisotropy, offscreen leakage, RGB coverage and GL
  errors; diagnostics/PNGs go to `scratch/reveng_splat_shots/`. CPU parsing contract:
  `test_splat_renderer`. Do not replace either proof with a nonempty screenshot check.

## Conventions & gotchas

- Selection and picking are ID-based (topo IDs from libchudware's tessellation), not raycast
  against triangles. If you add a pickable thing, it needs an ID-pass representation.
- The transparent thumbnail path in `renderer.cpp` is a shared dependency of the icon pipeline
  (`scripts/icon_pipeline.py`) — changing its background/alpha behavior breaks icon generation.
- Colors: never hardcode — pull from `ui_theme` (`host/src/ui_theme.h`);
  `scripts/gui_lint.py --rule color` fails the gate otherwise.
- Headless render modes exist for testing: `chudware --render <in.stl> <out.png>` and
  `--contact <in.stl> <out.png>`.

## In-context component edit (ghost mesh)

- Assembly "Edit Model" (see gui-ribbon-commands) makes the component's part-doc the active
  doc and draws the backgrounded assembly as a translucent **ghost**. Renderer state:
  `ghostSubs_` / `showGhost_` / `ghostAlpha_` (~0.28) / `ghostXform_`; API
  `captureCurrentAsGhost()` (steals current `subs_` GL handles — no re-tessellate),
  `clearGhost()`, `setGhostAlpha()`, `setGhostTransform(m16)`. Ghost is drawn in `drawCurrent`
  after the isolate pass and is **excluded from the id/pick pass** (non-pickable).
- `setGhostTransform` gets `inverse(component placement transform)` so the ghost realigns onto
  the live part's origin frame (else mated components float away from their context). Match the
  renderer's existing model-matrix convention exactly.
- Entry/exit live on `AppState`: `enterComponentEdit`/`exitComponentEdit`/`inComponentEdit`
  (`app_state.cpp`); exit does an async **save-then-rebuild** of the assembly. The viewport HUD
  banner ("Editing model: …" + Return-to-assembly) is in `viewport_panel.cpp`; its button calls
  `st.exitComponentEdit()`.

Scenario that exercises the viewport end to end: `CHUDWARE_SHOT=m1` (box + fillet + edit + undo,
`host/src/ui/scenarios.cpp`); splat rendering has its own `reveng_splat` proof above.


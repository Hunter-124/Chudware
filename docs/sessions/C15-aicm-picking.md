# C15 — AICM loader + ID-buffer picking + overlays

Model: **opus** · Wave 2 · Depends: C02 (format), C04 (split) · Follow
`docs/sessions/README.md`. BUILD_DIR: `host/build` (C14 uses host/build2 in parallel).

## Mission
The viewport learns topology: load AICM tessellations with per-face/edge identity, render an
R32UI ID buffer, and turn the mouse into face/edge/vertex picks with hover/selection highlight.

## Read first
1. `contracts/mesh_format.md` (you implement the C++ reader; golden:
   `contracts/fixtures/box.aicm`)
2. `host/src/renderer.{h,cpp}`, `host/src/mesh.{h,cpp}` (current pipeline you extend),
   `host/src/ui/viewport_panel.cpp` (post-split)
3. `docs/PLAN.md` §3.1 (ID encoding: 8-bit body | 2-bit kind | 22-bit topo id; RGBA8 fallback)
4. `contracts/worker_rpc.md` `doc_resolve_pick` (what a pick becomes — wiring to the
   worker is C16's job; you expose the pick result struct)

## Owned paths
`host/src/render/aicm_loader.{h,cpp}` · `host/src/render/id_pass.{h,cpp}` ·
`host/src/render/overlays.{h,cpp}` · `host/src/core/selection.{h,cpp}` ·
surgical extensions to `host/src/renderer.{h,cpp}` · `host/src/ui/viewport_panel.cpp`
(pick/hover wiring) · `host/CMakeLists.txt` (append sources) · `host/src/render/test_aicm_cpp/**`
(optional tiny test exe target, see verification)

## Deliverables
1. `aicm_loader`: parse AICM v1 (skip unknown chunks) → `DocMesh {bodies: [BodyMesh {id, name,
   color, GPU buffers, face_ranges, edge_segments (id→line range), brep_verts}]}`; also parse
   SCLR/DISP/LINS into CPU arrays for overlays (rendering of scalar maps may be minimal
   tonight: store + expose; full colormap pass acceptable as stretch).
2. `id_pass`: offscreen R32UI attachment sized to the viewport; render triangles with
   face-encoded ids, then edges (line width ~3 in ID space so they're pickable over faces),
   then B-rep vertices (point sprites ~6 px); `glReadPixels` 1×1 under cursor → `PickHit
   {body_id, kind (face|edge|vertex|none), topo_id}`. Priority vertex > edge > face at equal
   depth (achieve via draw order + depth offset). RGBA8-encoded fallback behind env
   `AI_CAD_IDBUF=rgba8`.
3. `overlays`: hover highlight (re-draw the hovered face range / edge segment with the theme's
   hoverHighlight role color, slight depth bias) and selected-set highlight
   (selectionHighlight); keep the existing studio shading untouched otherwise.
4. `core/selection`: selection set with kind filters, click = replace, ctrl-click = toggle,
   `clear()`, observer callback (C16/C17 subscribe). Stores PickHits + (later) resolved
   selectors — leave a `pending_resolve` slot.
5. Viewport wiring: hover on mouse move (throttle to once per frame), click selection,
   ESC clears; status-bar text hook "Face 12 of Body1" via a callback.
6. `AI_CAD_SHOT=pick` headless mode: load `contracts/fixtures/box.aicm`, render, simulate
   a center-screen pick, draw the highlight, save shot; ALSO print to stdout the PickHit for
   the center pixel ("pick: body=1 kind=face id=N") so verification is scriptable.

## Verification
Build green; `AI_CAD_SHOT=pick` produces a highlighted-face screenshot AND a sane stdout pick
line (one of face ids 1–6); golden box.aicm loads with 6 face ranges/12 edges/8 verts (assert
in code, print counts). Report renderer API changes for C16/C17.

# C16 — FeatureManager tree + doc mirror + pick→selector round-trip

Model: **sonnet** · Wave 2 · Depends: C11 (worker doc_*), C15 (picking) · Follow
`docs/sessions/README.md`. BUILD_DIR: `host/build2`.

## Mission
The GUI becomes document-aware: a worker-backed document mirror, the left FeatureManager tree,
and picks that resolve to selectors through the worker.

## Read first
1. `docs/contracts/doc_ops.md` (ops-response you consume), `docs/contracts/worker_rpc.md` §2
2. `host/src/core/jobs.cpp` + `host/src/worker_client.{h,cpp}` (how the GUI calls the worker)
3. `host/src/render/aicm_loader.h` + `host/src/core/selection.h` (C15's API)
4. SolidWorks tree look: doc name root; Equations, Material, Front/Top/Right planes, Origin
   nodes; then feature history with status badges; context menu.

## Owned paths
`host/src/core/doc_mirror.{h,cpp}` · `host/src/ui/feature_tree.{h,cpp}` ·
integration hooks in `host/src/app.cpp` (File > New Part menu, tree panel registration,
AICM reload on rebuild) · `host/CMakeLists.txt` (append)

## Deliverables
1. `doc_mirror`: holds current doc JSON + revision; `newPart()`, `applyOps(json ops) ->
   ops-response` (worker `doc_apply_ops` through the existing serialized client; on
   `stale_revision` auto-resync via `doc_tree` and surface a console note), `undo/redo`,
   `save/open` (.step+ paths via a simple text input tonight; native dialogs are C21);
   after every ops-response: load `tess.aicm_path` via C15's loader into the viewport and
   notify listeners (tree refresh).
2. `feature_tree`: ImGui tree over the mirror's doc JSON — fixed nodes (Equations [shows
   global count], Material, the three planes, Origin) then features in order with icons
   (C05 atlas via ui::Icon; fall back to text glyphs if atlas absent), status badges from the
   last rebuild's feature_status (ok/warning/error + tooltip with the error text +
   dangling_refs), suppressed dimming. Context menu: Edit (callback hook for C17), Rename
   (inline), Suppress/Unsuppress, Delete (confirm; cascade prompt on dependency_exists),
   Rollback bar = draggable separator row issuing nothing tonight (visual only).
   Selecting a tree feature highlights its owned faces in the viewport (selection set by
   owner via the loaded AICM + feature_status — if owner data isn't in the AICM, skip and
   note).
3. Pick round-trip: on viewport click, call worker `doc_resolve_pick` with the PickHit →
   store the returned selector in the selection set's `pending_resolve` slot; status bar shows
   "Face (cylinder) of Boss-Extrude1".
4. `AI_CAD_SHOT=tree` headless mode: scripted sequence — doc_new, ops add box, ops add fillet
   on an edge selector (resolve a pick first), then screenshot tree + viewport. Requires the
   worker; reuse the existing shot plumbing that already launches it for chat demos (read how
   AI_CAD_SHOT=chat works post-split). If a live worker in shot mode is infeasible tonight,
   drive the same flow in a `--doc-smoke` CLI mode printing JSON results, and screenshot a
   mirror loaded from `docs/contracts/fixtures/part_bracket.json` instead — note the choice.

## Verification
Build green (host/build2); the scripted flow produces: tree showing Box + Fillet with ok
badges, undo removes fillet from tree, redo restores; screenshot(s) captured; report exact
worker calls made and any contract friction.

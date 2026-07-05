---
id: T-BL43
title: Sketch external references — project external faces/edges/planes into a sketch, fix Convert Entities, select external features while in a sketch
workstream: backlog
owner: opus-brae
status: review
priority: 2
lane: modeling-core
kind: new
depends_on: []
branch: task/T-BL43-sketch-external-references
---

## Context

Direction from Hunter (2026-07-03). Three tightly-related sketch-reference capabilities:
(a) select geometry OUTSIDE the current sketch while editing it, (b) project external
faces/edges/planes into the sketch as reference geometry, (c) make the **Convert Entities**
tool work properly. Together these let a sketch reference the rest of the model.

## Definition of done

- [ ] **Select external features while in a sketch:** the selection system lets the user pick
      faces, edges, planes, body edges, and other sketches' geometry while a sketch is open
      for editing (today the in-sketch selection filter likely blocks external picks — allow
      them for reference/projection).
- [ ] **Convert Entities works:** select external edge(s) / a face loop → project onto the
      active sketch plane as sketch entities. Fix whatever is currently broken so it produces
      correct projected curves on the plane.
- [ ] **Projected geometry as references:** add external faces/edges/planes projected into the
      current sketch as reference geometry that can constrain/drive new sketch entities.
- [ ] **Associativity (ideal):** projected/converted geometry updates when the source
      geometry changes. If full associativity is too large for one task, deliver correct
      one-shot projection now and file a follow-up for associative update — do NOT silently
      ship non-associative as if it were associative; state which you delivered.
- [ ] Real-flow verify: make a body → start a sketch on a face → Convert Entities on an
      external edge → constrain new geometry to the projected edge → extrude. Also project a
      plane/edge from another body. Drive via MCP + confirm in GUI.
- [ ] Screenshots light+dark of the projected geometry in-sketch; eyeball.
- [ ] MCP freshness (WORKFLOW.md §6) if the sketch/convert vocab changes; scripts/gate.sh +
      `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "sketch convert entities projection"`,
  `graphify query "sketch selection filter external"`, `graphify query "project edge onto plane"`.
- Engine-heavy: projecting OCCT edges/faces onto a sketch plane (BRepProj / geometric
  projection) + representing the result as sketch reference entities + the associativity link
  back to the source. This is real geometry-kernel work.
- Selection: find where sketch-edit mode filters selectable entities; it must allow external
  picks for projection without letting the user accidentally add non-coplanar geometry AS
  sketch geometry — projected copies are the mechanism.
- **Tier: OPUS** — OCCT projection + associativity + the selection-boundary change are gnarly
  geometry/kernel work. A Sonnet driver who claims it and hits the projection kernel should
  stop at a repro + question file, not guess.
- Related: T-BL42 (absorbed sketches reusable as refs), T-BL41 (dimension/relation display).

## Progress log

- 2026-07-04 opus-brae: ROOT CAUSE of "Convert Entities broken" found — both GUIs (host
  sketch_mode.cpp applyConvertRef + ui/imgui sketch_overlay.cpp) emit a `sketch_convert` op that
  was contracted in doc_ops.md (C53) but NEVER implemented in the C++ engine, so every Convert
  Entities click failed with `unsupported`. Also: the old contracted math (sketch_project_ref)
  only handled lines + axis-parallel circles, faces only as intersection lines.
- 2026-07-04 opus-brae: shipped (commit f8aedf76 + follow-on): (1) projection kernel in
  core/doc/recon.{h,cpp} — project_edges_to_plane/face_boundary_edges, orthographic GeomProjLib
  projection then classify (oblique circle → ellipse BY CONSTRUCTION; perpendicular edge → point;
  splines → sampled polyline w/ approx flag); (2) op_sketch_convert in session.cpp (contract-
  conformant: real geometry default, fix-pinned, $idx/$idx#k id_map, convert_failed) — fixes BOTH
  GUIs with zero GUI changes; (3) read-only sketch_project_entities RPC (libaicad-dispatched) for
  preview/agent flows; (4) resolve_sketch_plane refactored to doc-level (resolve_sketch_plane_doc)
  so the ops path can resolve planes.
- 2026-07-04 opus-brae: contracts/doc_ops.md — appended dated amendment for the upgraded
  projection math (append-only; ratification question in work/questions/T-BL43.md Q1).
- 2026-07-04 opus-brae: ASSOCIATIVITY HONESTY: delivered ONE-SHOT projection only. No stored
  source link; converted geometry does not follow source-model edits. Follow-up filed:
  work/backlog/T-BL51-associative-convert-entities.md (T-BL49/50 were already taken today —
  interface-fitment-checker / layout-first-critical-dims — so this follow-up is T-BL51).
- 2026-07-04 opus-brae: headless real-flow verify green vs worktree .so (core_bridge): box →
  sketch on top face → project bottom-face loop (4 lines, construction) → insert → coincident-
  constrain new line to projected edge; boss on 45° datum → oblique circle → ELLIPSE via MCP
  path; datum plane → intersection line. Analytic probe: ellipse rx=10 ry=7.071=10·cos45°.
- 2026-07-04 opus-brae: ui/imgui in-GUI seam: interactive sketch entry is being wired by T-BL48
  (opus-tarn) — the Convert tool code path (sketch_overlay.cpp:2707 SketchTool::Convert →
  applyConvertRef → sketch_convert op) is engine-complete from this branch; once T-BL48 lands,
  the in-GUI click flow reaches the now-working op with no further changes here.
- 2026-07-04 opus-brae: SELECTION-BOUNDARY finding (DoD bullet 1): the in-sketch selection
  system ALREADY permits external picks in both GUIs — the Convert Entities tool
  (pickFinalPreferEdge id-pass edge/face picks while a sketch is open; host sketch_mode.cpp
  C53 block, ui/imgui sketch_overlay.cpp:2707) and Smart Dimension's external-ref picks
  (model edge / planar face / datum plane → addProjectedDimRef → projected + fix-pinned
  construction entity). They were dead ends only because the sketch_convert op (Convert) had
  no engine handler — fixed on this branch — and project_ref previews were line/circle-only.
  This matches SolidWorks interaction: external geometry is selectable in-sketch THROUGH the
  reference-consuming tools (convert/dimension), and projected copies are the mechanism that
  keeps non-coplanar geometry from entering the sketch raw (per this task's driver note). No
  blanket allow-external change to the idle-tool sketch selection filter was made — deliberate,
  to avoid colliding with T-BL48's entry-dispatch wiring and T-BL44/41 in the same files.
- 2026-07-04 opus-brae: added ctest regression test_sketch_convert (4 cases / 36 asserts,
  commit 0867513a) + AI_CAD_SHOT=sketch_convert headless GUI scenario (scenarios.{h,cpp}) —
  box → empty sketch on top face → sketch_convert(bottom face) → sketch_enter → capture;
  intentionally OUTSIDE the gui_parity `sketch` diff set (host scenario has no convert step).
- 2026-07-04 opus-brae RESUME (post-crash): rebased onto origin/master (+27 commits; T-BL48
  interactive sketch-entry wiring is at REVIEW, NOT merged — so no in-GUI click flow was built
  on it; the engine/MCP projection path stays the verifiable core, and the shot scenario reaches
  sketch_enter via the worker directly, independent of T-BL48). REBUILT libaicad.so from the
  rebased tree (recon.cpp/session.cpp/document.cpp recompiled) and RE-VERIFIED against the fresh
  .so — stale pre-rebase evidence discarded. Evidence: (a) ctest test_sketch_convert PASS;
  (b) full real-flow MCP/core_bridge script ALL CHECKS PASSED — box→sketch on top face→
  sketch_convert(bottom face loop)→4 fix-pinned REAL lines→coincident-constrain a new line to a
  projected edge→extrude the converted loop (bbox zmax=28.0 = 20 box + 8 boss)→undo×2 clean;
  oblique circle→ELLIPSE; datum plane→intersection line; empty-refs→invalid_op; construction:true
  variant; (c) light+dark headless screenshots captured under Xvfb with the engine attached
  (WorkerClient rev 3, "Engine live" / "Fully Defined"; NOT demo mode) —
  scratch/shots/sketch_convert_{dark,light}.png, eyeballed: in-sketch, Convert tool present,
  box body rendered.
- 2026-07-04 opus-brae: id-collision fix — the associativity follow-up was filed as T-BL49, but
  T-BL49 (interface-fitment-checker, opus-gill) and T-BL50 (layout-first-critical-dims,
  sonnet-rush) were both already claimed today; renamed the follow-up to the next free global id
  T-BL51 (work/backlog/T-BL51-associative-convert-entities.md), verified free on up-to-date
  origin/master (work/backlog + work/tasks) and the live board.

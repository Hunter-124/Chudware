---
name: reverse-engineer
description: Rebuild an existing solid/mesh into editable parametric features, or drive a camera capture session from a physical part to dimensionally-correct CAD.
inject: auto
triggers:
  keywords: [reverse engineer, reverse-engineer, scan this part, scan the part, scan to cad, point cloud, photogrammetry, STEP import, webcam capture, caliper measurement, calipers, replacement part]
  tools: [reverse_engineer, compare_parts, measure_from_photo, convert_face_to_sketch, reveng_status, reveng_start_session, reveng_add_images, reveng_reconstruct, reveng_set_scale, reveng_fit_primitives, reveng_build_parametric, reveng_attach_result, get_reveng_deviation]
  phases: [geometry]
---

# Reverse Engineer

Two lanes. Choose by what you are starting FROM, not by what you want out.

- **Lane A — you already have geometry**: a STEP/IGES/BREP/STL/OBJ file, or an open `doc_id`.
  Deterministic, verified by boolean compare, nothing to install.
- **Lane B — you have the physical part and a camera**: webcam / photos / video, or a datasheet
  for a catalog item. A capture SESSION, external backends, and two human confirm gates.

Lane B ends in geometry, so once a session produces a solid the Lane A tools apply to it.

## Lane A — file or doc_id

1. **Rebuild** — `reverse_engineer(source)` → new parametric part + `match_pct` + residual map.
   Rebuild into features; never freehand over the mesh. `import_part` first only when you want
   the source open as a separate reference.
2. **Compare loop** — for each unexplained region: `hover(sid)` the unclaimed faces, hand-model
   the feature on the rebuilt part, `compare_parts` again — the match must go UP or the model is
   wrong. `convert_face_to_sketch` / `project_into_sketch` turn a face into an editable profile.
3. **Done gate** — `verify_battery`; ledger the source artifact, `match_pct`, and every face you
   could not claim.

## Lane B — physical part + camera

0. `reveng_status()` **first** — capabilities + which backends actually resolved.
1. `reveng_start_session(name?, part_ref?)` → `session_id`; every other call takes it.
   `list_reveng_sessions` / `get_reveng_session` resume one later.
2. `reveng_print_scale_mat("A4")` → print the ArUco mat at 100% (never "fit to page"), tape it
   flat, stand the part ON it so markers and part appear in the same views.
3. **Capture** with autofocus and exposure LOCKED: `reveng_add_images` (webcam or phone stills),
   `reveng_add_video`, or `reveng_ingest_datasheet` for a catalog part — the fastest lane, no
   photogrammetry. `import_scan_mesh` if you already own a structured-light/laser mesh.
   `get_reveng_coverage` reports missing orbit positions and blur; re-shoot before reconstructing.
4. `reveng_reconstruct(session_id, quality)` — COLMAP sparse (feed-forward VGGT/MapAnything
   fallback when under 60% of images register) → OpenMVS dense **or** Brush 3DGS splat → mesh
   cleanup. `reveng_single_photo` is the approximate tier; see Rules.
5. `reveng_set_scale(session_id, "aruco_mat" | "two_point" | "known_dim", …)` — ArUco is
   triangulated across the posed views. → **HUMAN SCALE-CONFIRM GATE**: a person accepts the
   scale before anything proceeds. Every downstream dimension is this number times a shape.
6. `reveng_fit_primitives(session_id)` — RANSAC planes/cylinders/spheres/cones, cross-sections,
   and a hole-vs-boss role per cylinder. Read the roles before you build.
7. `list_reveng_dims` → `confirm_dimensions([{key, value_mm}])`; `measure_from_photo` only where
   a caliper cannot reach. → **HUMAN DIMENSION-CONFIRM GATE**: fit-critical dims carry a TYPED
   caliper number, not a photo estimate.
8. `reveng_build_parametric(session_id)` → real feature tree (sketches/extrudes/revolves/holes),
   with an honest mesh-body fallback when it cannot explain the shape.
9. **Verify** `get_reveng_deviation` / `describe_reveng_result`, fix the worst regions with the
   Lane A tools, re-check. **Deliver** `reveng_attach_result(session_id, part_ref)` into the
   parts hub; `export_reveng_bundle` for the archive.

Catalog shortcuts: `reveng_research_source` (specs/datasheet/images off the web — no scale or
reconstruct stage) and `reveng_make_part_from_library(part_ref)` (seed → research → build → attach).

## Rules

- **Call `reveng_status()` before promising any reconstruction.** COLMAP / OpenMVS / Brush /
  VGGT / TripoSR are discovered from env vars or PATH and are ABSENT in a stock install. Report
  the missing backends to the user instead of launching a job that returns
  `backend_not_provisioned`.
- **Never present an `approximate`-tagged result as a dimensionally usable part.** Single-photo
  tiers (TripoSR / TRELLIS) are reference/preview shapes with no metric scale;
  `reveng_build_parametric` refuses them unless `strategy="override_approximate"`, and taking
  that override makes the consequences yours to state.
- Quote the honesty tier on photo-derived dims: `high` ±2%, `medium` ±5%, `low` ±10%. ±5% on a
  20 mm bore is ±1 mm — that is not a fit.
- Never claim direct scan→parametric. The honest chain is scan → mesh → AI → parametric, with a
  mesh-body fallback you name out loud.
- Never declare done on mesh-only bodies when a parametric rebuild is in scope.
- Poor fit → more views/photos (~70% overlap, two elevations) or tighter region seeds, then
  compare again. More compute does not fix a bad image set.
- Synthetic / test-pattern webcam frames are flagged: a session built on them is a demo, not a part.
- Legacy fallback only — the phone-QR HTTP upload path (`start_capture_session` →
  `submit_part_photos`) still exists to get phone photos onto the machine. Feed those files to
  `reveng_add_images` and drive the session with the `reveng_*` tools above; it is not the
  primary capture path.

Mechanics and parameters: `cad_guide('reverse')`. GUI walkthrough: `cad_guide('reveng')`.

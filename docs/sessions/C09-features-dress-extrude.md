# C09 — Dress features + basic sketch compile + extrude/revolve

Model: **sonnet** · Wave 1 · Depends: C01 (registry/CompileContext), C03 (selector resolve) ·
Follow `docs/sessions/README.md` conventions.

## Mission
Make the M1 integration scenario possible end-to-end in the engine: fillet/chamfer/shell on
selector-picked topology, plus the non-solving sketch compiler (stored coordinates → regions)
and extrude/revolve over those regions.

## Read first
1. `contracts/feature_params.md` (fillet, chamfer, shell, sketch, extrude, revolve)
2. `contracts/selectors.md` §4 (region resolution)
3. `engine/doc/compiler.py` + `engine/doc/features/base.py` (C01's registry — build on it,
   read its report notes in your dispatch prompt if provided)
4. `engine/doc/selectors.py` (C03's describers + resolve API)
5. `engine/kernel.py` `_wrap_edge_op` — the auto-clamp retry pattern you generalize

## Owned paths
`engine/doc/features/dress.py` · `engine/doc/features/sketch.py` ·
`engine/doc/features/extrude.py` · `engine/doc/features/revolve.py` ·
`engine/doc/test_features1.py`

## Deliverables
1. `dress.py`: `fillet` / `chamfer` (resolve edge selectors via C03 against the current body's
   described edges + owner tags; auto-clamp retry on failure: halve ×3, floor 0.2 mm, record a
   warning in feature status) and `shell` (resolve remove-faces; build123d
   `offset(solid, -t, openings=faces)`; on OCCT failure surface the "shell before fillet"
   guidance). Dangling selector → feature error with `dangling_refs` per doc_ops.md.
2. `sketch.py`: compile WITHOUT solving (entities' stored coords are authoritative until M2):
   entities → build123d sketch-plane geometry (lines/arcs/circles/slots/splines; respect
   `construction`) → closed-loop detection → regions (faces) ordered per selectors.md §4,
   exposed for region-selector resolution AND as compile output for tessellation as the
   `sketch:<id>` pseudo-body. The `refs.plane` may be a reserved datum, datum feature, or face
   selector (plane through the face's centroid/axis).
3. `extrude.py` / `revolve.py` per feature_params.md: modes boss/cut, ends blind/through_all/
   mid_plane (through_all = 1.05× combined-bbox extent), flip, merge; draft param may be
   DEFERRED (note it) — do not block on BRepOffsetAPI_DraftAngle tonight. Revolve validates
   the axis doesn't cross the profile interior (clean error).
4. `test_features1.py`:
   - bracket fixture compiles: load `contracts/fixtures/part_bracket.json`, compile —
     EVERY feature ok; final volume ≈ plate volume − 4 pattern holes... pattern (f6) is NOT
     yours: if `linear_pattern` is unimplemented in C01's registry, assert features f1–f5 ok
     and f6 reports a clean "unknown feature type" error instead of crashing; note it.
   - sketch regions: rectangle sketch → 1 region area 4000; add inner circle → 2 loops, outer
     region has a hole (extrude of it has the hole)
   - extrude blind/mid_plane/through_all volumes; cut reduces volume; revolve 360° of a
     rectangle → cylinder shell volume within 1%
   - fillet via selector on a compiled box edge → volume decreases, cylindrical face appears;
     oversized radius auto-clamps with warning; deleted-owner selector → dangling error
   - shell: 20mm cube shell 2mm open top → volume matches analytic within 1%

## Constraints
- Build ONLY on C01's registry/context API — if it's missing something, report the gap rather
  than forking the architecture. Do not edit C01's files except `features/__init__.py` IF C01's
  report designates it append-only for registrations (otherwise report).

## Verification
`& $py -m engine.doc.test_features1` green; C01's `test_compile` and the schema suite still green.

# REVERSE-ENGINEERING ROLE PIPELINE CHEATSHEET

**Who does what, and when a rebuild is allowed to pass.** Three roles:
**RE Modeler** → **RE Datasheet Reader** → **RE Geometry Verifier**. Each emits a
ledger-recorded handoff; the verifier's verdict gates PLM entry.

Mechanics — tool parameters, the recognizer's passes, reconstruction stages, capture
technique, backend requirements — are NOT repeated here. They live in one place:
`cad_guide('reverse')` (`shared/cheatsheets/reverse_engineer.md`).

Routing note: this sheet is a FILE, not a `cad_guide` topic — read it by path. The topic
`cad_guide('reveng')` is a different document again: the Reverse Engineering GUI-workspace
walkthrough (Source → Scale → Reconstruct → Model → Review → Deliver).

---

## WHICH LANE (decide this before assigning anyone)

| You are starting from | Lane | Who leads |
|---|---|---|
| A FILE (STEP/IGES/BREP/STL/OBJ) or an open `doc_id` | **A** — deterministic, nothing to install | `re_modeler` |
| The PHYSICAL PART + a camera (webcam / photos / video) | **B** — capture session, external backends, two human gates | `re_modeler`, with `re_datasheet_reader` in parallel |
| A catalog part with a known MPN or datasheet | **B (datasheet fast-path)** — no photogrammetry | `re_datasheet_reader` |

Lane B ends in geometry, so once a session produces a solid the Lane A tools
(`compare_parts`, `convert_face_to_sketch`) apply to it like any other part.

---

## ROLES AT A GLANCE

| Role | Goal | Key tools |
|------|------|-----------|
| `re_modeler` | Rebuild source geometry (Lane A) or a scanned session (Lane B) into a parametric feature tree | `reverse_engineer`, `reveng_fit_primitives`, `reveng_build_parametric`, `reveng_attach_result`, `compare_parts`, `convert_face_to_sketch`, `add_features` |
| `re_datasheet_reader` | Extract + reconcile dims/tolerances/material from the manufacturer spec | `reveng_ingest_datasheet`, `reveng_research_source`, `research_datasheet_fetch`, `match_part_number`, `spec_sheet_match` |
| `re_geometry_verifier` | Certify match_pct ≥ 98 %, deviation inside tolerance, editability, spec compliance | `compare_parts`, `get_reveng_deviation`, `check_feature_overlap`, `inspect_section`, `wall_thickness` |

---

## LANE A HANDOFF FLOW

```
1. import_part(path)                → source doc open as reference (or pass a doc_id)
2. re_modeler:
     reverse_engineer(source)       → rebuilt doc + match_pct + residual map
     loop: hover unclaimed sids → hand-model the region → compare_parts → match goes UP
3. re_datasheet_reader:
     match_part_number / research_datasheet_fetch → extract dims
     measure_part on rebuilt        → dims_confirmed / dims_conflicting
4. re_geometry_verifier:
     compare_parts                  → match_pct check
     check_feature_overlap          → editability check
     inspect_section                → internal geometry check
     → verdict: pass | conditional_pass | fail
```

---

## LANE B HANDOFF FLOW (the two gates are hard stops, not formalities)

```
0. reveng_status()                              pre-flight: which backends actually resolved
1. reveng_start_session(name?, part_ref?)       → session_id (every later call takes it)
2. reveng_print_scale_mat("A4")                 print at 100%, tape flat, part stands ON it
3. capture: reveng_add_images | reveng_add_video | reveng_ingest_datasheet
            (or import_scan_mesh if a scan already exists)
   get_reveng_coverage                          re-shoot before reconstructing, not after
4. reveng_reconstruct(session_id, quality)      sparse → dense|splat → cleanup
   (reveng_single_photo = the approximate tier — preview only, see HONESTY RULES)
5. reveng_set_scale(session_id, method, ...)    aruco_mat | two_point | known_dim
   ===> HUMAN SCALE-CONFIRM GATE <===           a person accepts the scale. Nothing proceeds
                                                until they do; every dimension downstream is
                                                this number multiplied by a shape.
6. reveng_fit_primitives(session_id)            planes/cylinders/spheres/cones + hole-vs-boss
7. list_reveng_dims → confirm_dimensions([{key, value_mm}])   (measure_from_photo only when a
                                                caliper cannot reach the feature)
   ===> HUMAN DIMENSION-CONFIRM GATE <===       fit-critical dims carry a TYPED caliper value
8. reveng_build_parametric(session_id)          → editable feature tree (or an honest mesh body)
9. re_geometry_verifier: get_reveng_deviation + compare_parts → verdict
10. reveng_attach_result(session_id, part_ref) / export_reveng_bundle(session_id, dest)
```

Catalog shortcuts: `reveng_research_source` (web specs → straight to modelling, no scale or
reconstruct stage) and `reveng_make_part_from_library(part_ref)` (seed → research → build →
attach; also the Parts Library "Generate 3D model" button).

---

## VERDICT THRESHOLDS (re_geometry_verifier)

| match_pct | Verdict |
|-----------|---------|
| ≥ 98 % | **pass** |
| 95–98 %, all residuals named | **conditional_pass** (open_items required) |
| < 95 % | **fail** → replan to re_modeler |

Lane B adds one more bar: `get_reveng_deviation` must be inside the tolerance the PART needs
(a mating bore, not an arbitrary percentage), and the scale gate must show a confirmed scale.
An unconfirmed scale is an automatic **fail** regardless of match_pct.

---

## DATASHEET READER DECISION RULES

- `dims_confirmed`: datasheet value + measured value within tolerance. Both required.
- `dims_conflicting`: delta > 0.05 mm precision / > stated tol → flag, never round.
- `dims_unresolved`: datasheet dimension with no measurement yet → open question to modeler.
- Material stated in datasheet → ledger decision with source URL. Never infer from color.
- Datasheet crops come from `reveng_ingest_datasheet` (session-scoped, keeps provenance) or
  `reveng_research_source` for a part you only have a number/URL for. A datasheet dimension
  beats a photo-derived one every time — it is a stated fact, not a ±% estimate.

---

## HONESTY RULES — ALWAYS STATE IN LEDGER

- **Never claim direct scan→parametric.** The chain is scan → mesh → AI → parametric, with a
  mesh-body fallback. When the build falls back to a mesh body, say so in the handoff.
- **`approximate` is never a usable part.** Single-photo tiers (TripoSR / TRELLIS) are
  force-tagged `approximate`: reference/preview shapes with no metric scale.
  `reveng_build_parametric` refuses them unless `strategy="override_approximate"` — using that
  override is a ledger decision with a named reason, and the result is still not a measurement.
- **Honesty tier on every photo-derived dimension**: `high` ±2%, `medium` ±5%, `low` ±10%.
  ±5% on a 20 mm bore is ±1 mm — quote the tier, do not round it away.
- **Backends are not installed by default.** COLMAP / OpenMVS / Brush / VGGT / TripoSR are
  discovered from env vars or PATH. Call `reveng_status()` BEFORE promising a reconstruction and
  report what is missing; a missing backend returns `backend_not_provisioned`, not a fake mesh.
- **Synthetic / test-pattern webcam frames are flagged** — a session built on them is a demo,
  not a part. Never commit them as real captures.
- **Mesh input** (STL/OBJ, Lane A) is faceted → low deterministic yield. Say so at step 1.
- **`compare_parts` is world-frame**: no alignment/ICP, so a moved copy scores low by design.

---

## TOOL QUICK-REF — EVERY TOOL THIS SHEET NAMES

```python
# Lane A — geometry you already have
import_part(path); reverse_engineer(source); compare_parts(doc_a, doc_b)
convert_face_to_sketch(face); project_into_sketch(sketch, refs); add_features(ops)
describe_part(); hover(sid); verify_battery(doc_id)

# Lane B — session READ (never mutates)
reveng_status(); list_reveng_sessions(); get_reveng_session(sid); get_reveng_coverage(sid)
list_reveng_dims(sid); get_reveng_deviation(sid); describe_reveng_result(sid)

# Lane B — session WRITE (pipeline order)
reveng_start_session(name?, part_ref?); reveng_print_scale_mat(size)
reveng_add_images(sid, paths|dir); reveng_add_video(sid, path); reveng_ingest_datasheet(sid, path)
reveng_reconstruct(sid, quality); reveng_single_photo(sid, tier)   # tier = approximate
reveng_set_scale(sid, method, params); reveng_fit_primitives(sid)
reveng_build_parametric(sid, strategy?); reveng_attach_result(sid, part_ref)

# Dimension review (shared with intake)
confirm_dimensions([{key, value_mm}]); measure_from_photo(photo_id, px_a, px_b, feature, dim)

# External / catalog entry points
import_scan_mesh(sid, path, scale_mm?); export_reveng_bundle(sid, dest)
reveng_research_source(sid, query?|part_number?|url?); reveng_make_part_from_library(part_ref)

# Datasheet + spec reconciliation
research_datasheet_fetch(mpn); match_part_number(text); spec_sheet_match(doc_id, mpn)
measure_part(doc_id)

# Verification
check_feature_overlap(doc_id); inspect_section(doc_id, plane); wall_thickness(doc_id)
check_symmetry(doc_id, axis)

# Ledger (every role, every committed reading)
ledger_get(...); ledger_note(...)
```

Legacy fallback, not the primary capture path: `start_capture_session` / `submit_part_photos`
(the phone-QR HTTP upload page). Use it only to get phone photos onto this machine, then feed
the files to `reveng_add_images` and drive the session with the `reveng_*` tools above.

Parameters, capture technique, reconstruction stages and backend requirements:
`cad_guide('reverse')`.

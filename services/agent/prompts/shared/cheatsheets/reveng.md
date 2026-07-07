# REVERSE-ENGINEERING WORKFLOW CHEATSHEET

Three-role pipeline: **RE Modeler** → **RE Datasheet Reader** → **RE Geometry Verifier**.
Each emits a ledger-recorded handoff; the verifier's verdict gates PLM entry.

---

## ROLES AT A GLANCE

| Role | Goal | Key tools |
|------|------|-----------|
| `re_modeler` | Rebuild imported solid into parametric feature tree | `reverse_engineer`, `compare_parts`, `convert_face_to_sketch`, `add_features` |
| `re_datasheet_reader` | Extract + reconcile dims/tolerances from manufacturer spec | `research_datasheet_fetch`, `match_part_number`, `measure_part`, `spec_sheet_match` |
| `re_geometry_verifier` | Certify match_pct ≥ 98 %, editability, and spec compliance | `compare_parts`, `check_feature_overlap`, `inspect_section`, `wall_thickness` |

---

## TYPICAL FLOW

```
1. import_part(path)          → source doc open as reference
2. re_modeler:
     reverse_engineer(source) → rebuilt doc + match_pct + residual map
     (loop: hand-model residuals → compare_parts → match goes up)
3. re_datasheet_reader:
     match_part_number / research_datasheet_fetch → extract dims
     measure_part on rebuilt → produce dims_confirmed / dims_conflicting
4. re_geometry_verifier:
     compare_parts → match_pct check
     check_feature_overlap → editability check
     inspect_section → internal geometry check
     → verdict: pass | conditional_pass | fail
```

---

## ONE-CALL PATH

```python
reverse_engineer(source)
```

Imports, recognizes, rebuilds, and verifies in one call. Returns:

- `match_pct` — `common_volume / max(volume)`. 100 = identical. `null` = boolean
  failed (degrade to volume/bbox).
- `features` — rebuilt feature ids (live, editable in the new part).
- `rejected` — batches reverted because they made match WORSE. Never silently kept.
- `candidates` — recognized but not applied (fillets, chamfers, small holes). Apply
  with `add_features`, re-check with `compare_parts`.
- `unexplained` — `{missing_mm3, extra_mm3, regions[{center}]}`. Go look there.
- `recognition.unclaimed` — source faces no pass claimed. `hover(sid)` to inspect.

---

## RESIDUAL WORKFLOW

```
for each region in unexplained.regions:
    hover(sid)                          # identify the face on the source doc
    convert_face_to_sketch(face)        # trace planar face wires → new sketch
    add_features([...])                 # model the feature on the rebuilt part
    compare_parts(source, rebuilt)      # match must go UP; if not → revert + try differently
```

---

## VERDICT THRESHOLDS (re_geometry_verifier)

| match_pct | Verdict |
|-----------|---------|
| ≥ 98 % | **pass** |
| 95–98 %, all residuals named | **conditional_pass** (open_items required) |
| < 95 % | **fail** → replan to re_modeler |

---

## CAVEATS — ALWAYS STATE IN LEDGER

- **Mesh input** (STL/OBJ): faceted → low deterministic yield. Say so at step 1.
- **World-frame only**: `compare_parts` assumes no alignment/ICP. A moved copy scores
  low by design.
- **Cbore/csink stacks** recovered as separate coaxial simple holes, not wizard holes.
- **Cone faces, splines, freeform** not claimed deterministically — surface in
  `unclaimed`/`candidates`, never silently approximated.

---

## DATASHEET READER DECISION RULES

- `dims_confirmed`: datasheet value + measured value within tolerance. Both required.
- `dims_conflicting`: delta > 0.05 mm precision / > stated tol → flag, never round.
- `dims_unresolved`: datasheet dimension with no measurement yet → open question to modeler.
- Material stated in datasheet → ledger decision with source URL. Never infer from color.

---

## TOOL QUICK-REF

```python
reverse_engineer(source)                     # one-call import + recognize + rebuild + verify
compare_parts(doc_a, doc_b)                  # boolean match verdict (doc_a = reference)
convert_face_to_sketch(face)                 # trace planar face → new sketch on that plane
project_into_sketch(sketch, refs)            # append projected wires to existing sketch
research_datasheet_fetch(mpn)               # fetch manufacturer datasheet by MPN
match_part_number(text)                     # resolve MPN from text/markings
spec_sheet_match(doc_id, mpn)               # cross-check spec sheet against part geometry
measure_part(doc_id)                        # mass properties + bounding box
inspect_section(doc_id, plane)              # cross-section view for internal geometry
check_feature_overlap(doc_id)               # dead / swallowed features in the feature tree
wall_thickness(doc_id)                      # thin-wall map
check_symmetry(doc_id, axis)                # symmetry check
```

See `cad_guide('reverse')` for the full reverse-engineering narrative guide.

# Parts Library data coverage — what it provides vs. what design validation needs

Cached findings (2026-07-08) from Parts Library Phase 2 spot-checks. Answers two
questions: (1) are proper alloy-property lookups available without MatWeb,
and (2) are `part_search` records sufficient to feed the mechanical/structural
validators without manual re-entry.

## 1. Material properties — first-party registry is sufficient (no MatWeb needed)

`research_material_lookup` (`services/research/material_lookup.py`) serves the
full mechanical property set for named alloys from the **first-party bundled
registry** (`core/data/materials/registry.json`, ASM/MMPDS nominal values), cited,
`confidence:"bundled"`, offline-safe. Verified live for common structural alloys:

| alloy    | yield (MPa) | ultimate (MPa) | endurance/fatigue (MPa) | density (g/cm³) | E (MPa) |
|----------|-------------|----------------|-------------------------|-----------------|---------|
| 6061-T6  | 276 | 310 | 96  | 2.70 | 69 000 |
| 4130     | 435 | 670 | 340 | 7.85 | 205 000 |
| 7075-T6  | 503 | 572 | 159 | 2.81 | 71 700 |

**Conclusion:** yield, fatigue (endurance limit), and density for common aluminium and
steel structural alloys are already available first-party. **MatWeb is not
required for this.** The opt-in external web lookup
(`material_web_lookup.py`, MakeItFrom) is
therefore **redundant for property lookup** and stays **disabled by default**
(`CHUDWARE_ENABLE_MATERIAL_WEB` unset) — keep it only as a user-opt-in
cross-check, not a needed path. To add an alloy that's missing, extend
`registry.json` (first-party, cited) rather than scraping.

Gap worth noting: `research_standard_section` provides section geometry/weights
(AISC etc.); pairing a section with `material_lookup` gives the (A, Sx, ρ, σy)
a beam check needs. Adequate for stock structural shapes.

## 2. Validator input sufficiency — a real gap (datasheet values not captured)

Spot-check: `part_search("6205 bearing")` → record params `{bore: 25, outer_d:
52, width: 15}` (geometry only). The validators need load/strength data the
catalog does **not** carry:

| validator | needs | in `part_search` record? |
|-----------|-------|--------------------------|
| `bearing_life()` (ISO 281 L10) | **C** (dynamic load rating, N) + Fr/Fa/rpm | **No** — catalog carries bore/OD/width only. `bearing_life`'s own docstring says so and refuses to fabricate C. |
| `gear_check()` (AGMA) | AGMA **J** and **I** geometry factors, allowable stresses, Wt | **No** — not a catalog field. |
| `struct_estimate()` | a whole structural **document** (sections + material), not a loose part record | N/A — different input shape; feed it a built structural doc, not a part. |

**Conclusion:** `part_search` records are **NOT** sufficient to feed
`bearing_life`/`gear_check` without manual entry of the datasheet values (C, J,
I). This is expected — those come from the manufacturer datasheet, not the
dimensional catalog — but it means "search a bearing → validate its life" is not
yet one-click.

### Recommended follow-ups (not yet done)
- **Bearing C capture:** add a `dynamic_load_rating_N` (C) field to the bearing
  catalog for standard designations from a cited reference table (e.g. 6205 C ≈
  14.8 kN), and have the vendor bearing adapters (igus/misumi/SKF) lift
  "Dynamic Load Rating" from the acquired spec table into `params.C` so
  `bearing_life` can be fed directly from a searched part.
- **Gear factors:** J/I are geometry-derived (tooth count, pressure angle) — a
  helper could compute nominal J/I from a gear record's params rather than
  requiring a table lookup.
- Until then, callers must supply C / J / I manually; the validators correctly
  refuse to invent them.

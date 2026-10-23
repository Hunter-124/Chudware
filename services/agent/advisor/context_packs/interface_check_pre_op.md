# Interface alignment pre-op context pack (aicad.trigger/1)

**When this fires:** the model mentions interface, alignment, hole patterns, bolt holes,
coaxiality, contact faces, fasteners, or calls `interface_check`, `part_place`.

**Key facts:**
- `interface_check` catches layout errors that **pass** `check_interference`
  (nothing overlaps) but are still wrong: bolt holes that don't register, bores that
  aren't coaxial, contact faces not flush, a screw that can't pass the stack.
- Returns `{findings:[{severity, check, detail, error_mm, fix}], summary}`.
- Checks run per component pair: `hole_pattern`, `coaxiality`, `contact_plane`,
  `fastener_passthrough`.
- Each `fail` finding carries a pre-computed `fix` string — the advisor lifts this
  into a `candidate_fix` so the fixer can apply it without re-running the tool.

**Typical findings and fixes (advisor will specify error_mm):**
- `hole_pattern` fail: hole centers offset > 0.20 mm — shift the bolt circle.
- `coaxiality` fail: axis offset > 0.20 mm or angle > 0.5° — re-datum the bore.
- `contact_plane` fail: faces not flush (signed gap) — adjust the mating face dimension.
- `fastener_passthrough` fail: clearance hole too small for the named fastener — enlarge.

# Design for manufacturability pre-op (aicad.trigger/1)

**When this fires:** manufacturing intent is explicit, or the model calls `set_process`,
`resolve_process`, `dfm_check`, or `wall_thickness`.

**Key facts:**
- `dfm_check` runs `eval_thickness` + `eval_draft` against the bound ProcessProfile
  thresholds and returns `{dfm_pass, checks:[{check, value_mm, limit_mm, ok, note}]}`.
- A **blocker** advisory fires when `dfm_pass=false`. The advisor names the offending
  check, the measured value, the process minimum, and the delta to fix.
- Apply only checks relevant to the selected process. If the process is unknown and changes
  the design decision, resolve it or ask; do not invent a process or block on irrelevant rules.
- Process minimums (from the ProcessProfile table in `core/dfm/process_profiles.cpp`):
  - FDM: min wall ~1.2 mm, no draft required
  - Injection molding: min wall ~1.0 mm, ≥1° draft required on vertical faces
  - SLA: min wall ~0.6 mm, no draft required
  - CNC: wall depends on tool; draft not typically required

**Typical targeted fixes (advisor will compute):**
- `thicken_wall`: increase the thinnest feature by ≥Δ mm (advisor gives exact delta).
- `add_draft`: add draft angle to the listed faces (advisor gives face count + angle).
- Run `dfm_check` again after geometry changes to confirm the fix cleared the blocker.

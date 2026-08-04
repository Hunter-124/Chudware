# T-BL17 Questions & Seam Notes
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Seam note for opus-fell (T-BL04 — CalculiX integration)

**Context:** T-BL17 implements `run_fea_frequency_response` via modal superposition
(Python-side, using mode shapes already returned by `sim_run_modal`). This covers v1
harmonic sweep + Miles random approximation without any CalculiX dependency.

**The seam:** For full-field frequency-domain stress recovery (not just acceleration
response), CalculiX's `*FREQUENCY` + `*STEADY STATE DYNAMICS` steps would provide:
- Physical displacement field (mm) at each sweep point, not just unit-normalized mode shapes
- Stress tensor field (MPa) across the frequency range
- Consistent mass matrix (not the lumped approximation used here)

The current tool returns modal participation factors and SRSS-combined acceleration
response. It explicitly does NOT return physical displacement amplitude (mode shapes
are unit-max-normalized, not physical mm/g). This limitation is documented in the
tool's return payload `limitations` field.

**Recommendation for opus-fell:** When T-BL04's CalculiX `*STEADY STATE DYNAMICS`
output is available, `run_fea_frequency_response` can be upgraded to:
1. Call CalculiX via subprocess (GPL2 policy — subprocess-only, never import)
2. Parse `.frd` output for displacement + stress at each frequency step
3. Replace the modal-superposition harmonic path with the CalculiX result, keeping
   the Miles random path as the PSD approximation (CalculiX can also do `*RANDOM RESPONSE`
   but that is a larger integration lift)

The tool's JSON contract is forward-compatible — the `harmonic` dict can carry
`max_disp_mm` and `max_stress_mpa` fields once the C++ or CalculiX path provides them.

**No action required from opus-fell now.** This note documents the v1 limitation
so T-BL04 can plan the upgrade seam.

## Lumped mass approximation limitation

The modal participation factor uses equal-distribution lumped mass
(total mass / n_nodes). This is correct in total mass but does not weight
participation by local mass density variation. For a uniform-density part
(which all current material specs are) this is exact. For heterogeneous
assemblies it would underestimate participation for heavier regions.

**Resolution:** when `sim_run_modal` is extended to return the assembled mass matrix
diagonal (or `sim_run_static` exposes nodal mass), update `lumped_mass_vector` to
use per-node mass. No API change needed in the MCP tool contract.

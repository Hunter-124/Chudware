---
id: T-BL17
title: Random/harmonic vibration response (frequency response analysis)
workstream: backlog
owner: sonnet-wick
status: review
branch: task/T-BL17-vibration-response
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

**Cut 2026-07-03 (Erik-directed).** `run_fea_modal` (T-D2) already gives natural
frequencies and mode shapes. This extends that into an actual frequency-response
solve: given a harmonic or random (PSD) vibration input at a support/base, compute the
response amplitude (displacement/stress) across a frequency sweep — the standard
shock/vibration-qualification question ("does this part resonate and fail under the
vibe spec"), not just "what are its natural frequencies."

Likely backend: PyNite doesn't do frequency-response natively (check first — modal
superposition on top of PyNite's existing modal results may be buildable in Python
without a new solver dependency); fall back to CalculiX (T-BL04's `*FREQUENCY` +
`*STEADY STATE DYNAMICS`) only if PyNite genuinely can't do it.

## Definition of done

- [x] `run_fea_frequency_response` MCP tool: harmonic input at a support → response
      spectrum (peak displacement/stress vs. frequency) for the structure
- [x] At least a simplified PSD/random-vibration mode (even a Miles'-equation-style
      approximation is acceptable for v1 — cite the method, don't fabricate precision)
- [x] Hand-calc-validated against a textbook single-DOF or simple-beam case (same
      discipline as T-D2/T-BL01 — cited reference, no invented tolerances)
- [x] DAG verify-node end condition (pass/fail against a vibration spec requirement)
- [x] Honest degrade: if the input vibration spec/PSD isn't provided, return the §0
      shape rather than assuming a default profile

## AI hints

- `run_fea_modal` (T-D2, existing) — natural frequencies/mode shapes this builds on
- Check `engine_oracle/structural/` or wherever T-D2 landed PyNite integration before
  assuming a new solver dependency is needed
- `docs/BENCHMARK_VALIDATION.md` / T-BL01's V&V harness — the tolerance-tier and
  citation discipline this should follow

## Delivery notes (sonnet-wick, 2026-07-04)

Landed on branch `task/T-BL17-vibration-response`:

- **Method**: modal superposition on the EXISTING C++ `sim_run_modal` output — it already
  returns `mode_shapes` (n_modes*n_nodes*3, unit-max-normalized); the Python `run_fea_modal`
  wrapper simply dropped them. No new solver dependency, no CalculiX (T-BL04 untouched;
  upgrade seam documented in work/questions/T-BL17.md).
  - Harmonic: participation-factor-weighted SDOF transmissibility per mode, SRSS combine
    (Craig & Kurdila 2006 §12.3; Harris & Piersol 2002 Ch.2 Eq. 2.41).
  - Random: Miles' equation per mode (Steinberg 2000 Eq. 6.6), log-log PSD interpolation,
    SRSS total GRMS — cited as an approximation everywhere it appears.
- **Files**: engine_oracle/sim/vibration.py (+tests), services/mcp/cad_tools.py
  (`run_fea_frequency_response` after `run_fea_modal` — append-only w.r.t. server.py;
  `_SIM_GUIDE` freshness update in the same file), services/dag/verify.py
  (`vibration` check_type, fail-closed), services/mcp/test_vibration_tool.py,
  services/dag/test_verify.py (expected-set extended with the new check type).
- **Validation**: SDOF transmissibility at resonance = 25.020 (zeta=0.02, literal, <1%);
  Miles SDOF GRMS = 12.5332 g @ fn=100 Hz, Q=25, W=0.04 g^2/Hz (literal, <1%);
  steel 50x50x1000 mm cantilever fn1 = 40.769 Hz hand-calc (Craig & Kurdila Table 8.1,
  matches SIM_AUDIT.md ~40.8 Hz) -> Miles GRMS 12.6536 g (<1%). PSD log-log interp at
  EXACT tier. 23 new tests, all engine-free.
- **Honest degrade**: no psd_breakpoints -> random={ok:false, reason:"no_psd_provided",
  hint}; no vibration_spec -> verdict=null; GRMS spec limit without a PSD run FAILS closed.
- **Known limitations** (stated in every result payload + questions file): lumped
  equal-distribution mass; unit-normalized shapes => acceleration response only, no
  physical displacement/stress recovery (that is T-BL04's *STEADY STATE DYNAMICS seam).

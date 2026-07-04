---
id: T-BL17
title: Random/harmonic vibration response (frequency response analysis)
workstream: backlog
owner: sonnet-wick
status: doing
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

- [ ] `run_fea_frequency_response` MCP tool: harmonic input at a support → response
      spectrum (peak displacement/stress vs. frequency) for the structure
- [ ] At least a simplified PSD/random-vibration mode (even a Miles'-equation-style
      approximation is acceptable for v1 — cite the method, don't fabricate precision)
- [ ] Hand-calc-validated against a textbook single-DOF or simple-beam case (same
      discipline as T-D2/T-BL01 — cited reference, no invented tolerances)
- [ ] DAG verify-node end condition (pass/fail against a vibration spec requirement)
- [ ] Honest degrade: if the input vibration spec/PSD isn't provided, return the §0
      shape rather than assuming a default profile

## AI hints

- `run_fea_modal` (T-D2, existing) — natural frequencies/mode shapes this builds on
- Check `engine_oracle/structural/` or wherever T-D2 landed PyNite integration before
  assuming a new solver dependency is needed
- `docs/BENCHMARK_VALIDATION.md` / T-BL01's V&V harness — the tolerance-tier and
  citation discipline this should follow

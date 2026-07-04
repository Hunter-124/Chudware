---
id: T-BL18
title: Gear/bearing life and AGMA tooth-strength checks
workstream: backlog
owner: sonnet-merle
status: review
branch: task/T-BL18-gear-bearing-checks
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

**Cut 2026-07-03 (Erik-directed).** Cheap, deterministic hand-calc addition — same
spirit as T-BL08's tolerance stack-up and the existing D/C-ratio structural checks
(T-D3): no new solver, just well-established closed-form engineering calcs with cited
public references (AGMA 2001/908, ISO 281/6336, or ANSI/AGMA equivalents — clean-room,
same discipline as T-D3's section libraries). High DFM value for anything with power
transmission (gearboxes, chain/belt drives, shafts on bearings).

Scope for v1:

1. Spur/helical gear tooth bending + contact (pitting) stress per AGMA, with a safety
   factor readout
2. Rolling-element bearing L10 fatigue life per ISO 281 given load/speed

## Definition of done

- [x] `gear_check` MCP tool: tooth bending + contact stress, safety factors, byte-exact
      disclaimer (approximate/estimate wording, same as T-D5's cost disclaimer)
- [x] `bearing_life` MCP tool: L10 life estimate from load + speed + bearing catalog
      data (reuse the parts/1 federation's bearing catalog from T-P2 if it carries the
      needed dynamic load rating; don't fabricate a rating if the catalog lacks one)
- [x] Both hand-calc-validated against textbook worked examples (cited reference,
      byte-exact literal expected values — same discipline as T-D3/T-D5)
- [x] Exposed as MCP tools; DAG verify-node end conditions optional (nice-to-have,
      not required for v1)

## AI hints

- `contracts/structural_schema.md`, `services/mcp/struct_check` (T-D3) — the D/C-ratio
  check pattern and disclaimer wording to mirror
- Parts federation (T-P2/T-P3) bearing adapter — check what dynamic-load-rating fields
  it actually carries before assuming `bearing_life` has what it needs
- `docs/HARDWARE_LIBRARY.md` / `hardware.md` (T-B4) — existing clean-room table
  precedent for citing public-domain standard values

## Work log

- 2026-07-04 sonnet-merle: Implemented on task/T-BL18-gear-bearing-checks.
  - `gear_check` + `bearing_life` MCP tools (services/mcp/mechanical_tools.py,
    registered append-only in server.py); calc core in engine_oracle/mechanical/.
  - Catalog finding (DoD item 2): NEITHER bearing catalog carries a dynamic load
    rating — C++ `core/parts/parts_data.h` BearingRow = {size, bore, od, width};
    parts-federation BOLTS table `engine_oracle/data/bolts/bearings.blt.yaml` =
    {bore, outer_d, width}. So C is a required explicit input; bearing_life
    raises ValueError on C<=0 (never fabricates), and cad_guide('mechanical')
    tells agents to get C from the datasheet (research_datasheet_fetch).
  - Validation: hand-calc cases with byte-exact literals from the cited published
    equations (Shigley's MED 10th ed. Eqs. 14-15/14-16 + Table 14-8; ISO 281:2007 /
    Eq. 11-1): sigma_b = 47.5 MPa, sigma_c = 482.9137033257734 MPa,
    L10 = 52.734375 Mrev, L10h = 732.421875 h. Full arithmetic in test docstrings.
    Not verbatim textbook examples (citation-honesty): method-cited hand calcs.
  - Disclaimer (byte-exact, T-D3/T-D5 discipline): "Preliminary engineering
    estimate — not a certified analysis. Verify with a qualified mechanical
    engineer before use in safety-critical applications."
  - Tests: engine_oracle/mechanical/test_gear_bearing.py 36 pass;
    test_mcp_freshness.py 5 pass (mechanical family added). DAG end conditions
    (nice-to-have) not included in v1.

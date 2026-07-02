---
id: T-F2
title: Research source helpers v1 + unified material registry seed
workstream: F
owner: opus-fern
status: doing
branch: task/T-F2-source-helpers
day: 8
depends_on: [T-F1]
---

## Context

Pillar 8 second half: pre-digested, unit-normalized, provenance-stamped source helpers
on top of T-F1's contract. READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 8 — the legal
rules are binding (bundle-safe seeds only: Materials Project CC-BY, USDA Wood Handbook,
first-party fact tables; NEVER MatWeb/MakeItFrom/Matmatch; code converts units, never
the model; ISO 724/ASME B1.1 computed not fetched).

## Files / areas touched

- `engine/research/material_lookup.py`, `thread_spec.py`, `standard_section.py`,
  `datasheet_fetch.py`, `unit_convert.py`, `eng_calc.py`
- `engine/materials/` — new unified registry (merge sim/CAM/DFM tables + provenance)
- `services/mcp/server.py` — registrations

## Steps

1. Bundle the first-party material table (~200 common alloys/plastics) with per-row
   source metadata; `material_lookup` serves bundled first, runtime-fetch-with-citation
   fallback.
2. `thread_spec`/fastener callouts computed from ISO 724 / ASME B1.1 formulas; AISC
   shapes from the free official DB; pipe schedules as first-party B36 transcription.
3. `datasheet_fetch`: PDF→markdown with table preservation (MIT-licensed tooling only —
   pymupdf4llm is AGPL, excluded).
4. `unit_convert`/`eng_calc` deterministic; schema-extracted `{raw_quote, value,
   unit-enum}` with range checks (ksi/MPa 6.9× guard).
5. Unify `engine/sim/materials.json` + CAM cutting DB + DFM profiles into
   `engine/materials/` with provenance; all three subsystems read the registry.

## Definition of done

- [ ] All helpers return the T-F1 answer contract; tests per helper green
- [ ] Material registry unified; sim/CAM/DFM read it; no duplicated tables remain
- [ ] Zero prohibited sources reachable from code; `bash scripts/gate.sh`; `graphify update .`

## Driver notes (opus-fern)

- Open questions in `work/questions/T-F2.md`: (Q1) the CAM cutting DB is C++-side (bridge
  `cam_materials`), outside this task's Python file scope — the Python material tables
  (FEA `_MATERIALS`, DfM densities) are unified onto `engine/materials`; the C++ CAM DB
  unification is recommended as a follow-up. (Q2) `engine/sim/materials.json` is now a
  registry-generated mirror (C++ GUI reads it); a few values reconciled (notably brass
  C360 yield 310->124 MPa, the FEA value) — flagged for advisor sign-off. Neither blocks
  the rest of T-F2.
- Registry is the single Python source: `services/mcp/cad_tools` and `engine/dfm` read it;
  `engine/sim/materials.json` regenerated via `engine/materials/export_sim_json` with an
  anti-drift regen test.

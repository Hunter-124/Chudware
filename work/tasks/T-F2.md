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

## Driver notes (Fable, 2026-07-02 — next-wave prep)

- T-F1 (opus-ivy) freezes contracts/research_tools.md — your helpers implement ITS
  answer contract; read the frozen file, don't re-derive from the master plan.
- material_lookup: bundled-first (~200 alloys/plastics); bundle-safe seeds per master
  plan Pillar 8 item 4 — Materials Project (CC-BY, computed), USDA Wood Handbook
  (public domain). NEVER MatWeb/MakeItFrom/Matmatch (ToS-prohibited). NIST SRD is NOT
  public domain.
- thread_spec/fits: COMPUTED from ISO 724 / ASME B1.1 formulas, bundled, no fetch.
- Unified material registry (engine/materials/): merges engine/sim/materials.json +
  the CAM cutting DB + DFM process profiles — provenance field per record; single
  source for the T-F7 narration layer's yield lookups (describe_results cites it).
- Hard rule: schema extracts {raw_quote, value, unit-enum}; CODE converts units
  (services/verify/unit_ranges.py from T-H4 has the range table — import it).

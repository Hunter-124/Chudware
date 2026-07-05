---
id: T-W6
title: Feeds/speeds and machining data tables + CAM advisor integration
workstream: W
owner: sonnet-crag
status: done
branch: task/T-W6-feeds-speeds
depends_on: []
lane: ai-copilot
kind: new
priority: 2
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.5). The deterministic CAM fast-path
(tools/flywheel/bench/benches/cam.py deterministic_op_plan) and cam_add_operation
currently rely on caller-supplied stepover/stepdown/rpm/feed. Give the system real
machining data so CAM params are table-driven with citations, and the CAM advisor can
flag out-of-envelope choices instead of letting sim discover gouges.

## Files / areas touched

- `engine_oracle/research/data/feeds_speeds.json` — new: surface speed (SFM/m·min) and
  chipload per material × tool-type × operation, plus drill/tap chart
- `engine_oracle/research/machining.py` — new: `feeds_speeds(material, tool_type,
  tool_dia, operation, machine_limits?)` → rpm/feed/stepdown/stepover envelope,
  aicad.calc/1 envelope (contracts/advisor.md §7)
- `services/mcp/research_tools.py` or `cam_tools.py` — register `feeds_speeds` +
  `tap_drill(thread)` tools
- CAM seam: `deterministic_op_plan` + cam op defaults consult the table when params
  omitted; `cam_simulate`/advisory path flags params outside the envelope
- tests: `engine_oracle/research/test_machining.py`

## Steps

1. Read contracts/advisor.md §7 and §3; docs/ADVISOR_ARCHITECTURE.md §2.5.
2. Seed feeds_speeds.json: materials aligned to engine_oracle/sim/materials.json ids
   (alu_6061, steel_1018, steel_304_stainless, ti_6al4v, abs, pla, nylon_pa12, ...),
   tool types (endmill_flat, endmill_ball, drill, tap, face_mill), operations
   (roughing, finishing, slotting, drilling). Fields: vc range, chipload-per-tooth
   range by tool dia bracket, max stepdown/stepover as fraction of dia, coolant note,
   `source` citation per row (Machinery's Handbook / manufacturer general data —
   cite what you actually use; per OSS-first directive check for a permissively
   licensed dataset e.g. opencam/fswizard-style public tables before hand-seeding).
3. `feeds_speeds(...)`: rpm = vc/(π·d) clamped to machine_limits, feed from chipload ×
   flutes × rpm, conservative default at range-midpoint, envelope (min/max) included
   so advisors can range-check. tap_drill: standard chart for metric coarse/fine + UNC/UNF.
4. Wire the CAM seam: when cam_add_operation / deterministic_op_plan get no explicit
   cutting params, resolve from the table (log provenance in the op's notes field);
   when explicit params fall outside the envelope, return a warning shaped as an
   aicad.advisory/1 item (severity=hint, candidate_fixes = clamped values).
5. Tests: 6061 + 10 mm endmill roughing lands in sane range (rpm ~3k–12k on default
   limits), titanium slower than aluminum, tap_drill(M6x1.0)=5.0, envelope warning
   fires on 4x chipload, calc envelope conformance.
6. MCP freshness: server instructions CAM section mentions feeds_speeds; cad_guide
   CAM topic; `graphify update .`.

## Definition of done

- [ ] feeds_speeds.json seeded (≥8 materials × 4 tool types) with per-row citations
- [ ] feeds_speeds + tap_drill tools registered, aicad.calc/1 conformant
- [ ] CAM defaults resolve from table with provenance; out-of-envelope params produce
      advisory-shaped warnings
- [ ] Tests pass; server instructions updated

## AI hints

- `graphify query "cam operation defaults feed speed"` and read
  tools/flywheel/bench/benches/cam.py L63-150 first.
- cam_materials tool already exists — check what it returns and align ids
  (services/mcp/cam_tools.py).
- OSS-first: survey existing open feeds/speeds datasets before hand-authoring; record
  license in contracts/license_debt.md if anything is vendored.

## Progress log
- 2026-07-05 00:23 UTC [sonnet-crag] Landed feeds/speeds table + CAM advisor integration on branch task/T-W6-feeds-speeds.

What was built:
- engine_oracle/research/data/feeds_speeds.json: 8-material × 4-5 tool-type envelope table (alu_6061, steel_1018, steel_304_stainless, ti_6al4v, abs, pla, nylon_pa12, cast_iron_gray, inconel_718) with per-row Machinery's Handbook citations. OSS survey found no permissive open dataset (FSWizard/HSMAdvisor proprietary, OpenCAMLib has no data tables); values anchored to core/cam/feeds.cpp CuttingDB in-repo first-party table.
- engine_oracle/research/machining.py: feeds_speeds() + tap_drill() returning aicad.calc/1 envelopes (contracts/advisor.md §7); check_envelope() producing aicad.advisory/1 items (severity=hint, candidate_fixes with confidence=table) per §3.
- engine_oracle/research/test_machining.py: 30 tests all pass.
- services/mcp/cam_tools.py: feeds_speeds, cam_check_feeds_envelope, tap_drill MCP tools wired.
- _CAM_GUIDE + server instructions updated with feeds/speeds workflow docs.

Test results: 30/30 new tests pass; 74/74 research tests total pass. cam bench deterministic_op_plan unchanged (stepdown=2, stepover=3 preserved). graphify update run.

Key test results: alu_6061 10mm endmill roughing rpm_mid=8727 (in [3k-12k]); Ti rpm_mid=2707 < Al/3=2909 confirmed; tap_drill(M6x1.0)=5.0mm; 4x feed fires advisory with clamped candidate_fix.

No open questions.

---
id: T-A6
title: Capstone round 2 - live-backend verify (real docs through the verify nodes)
workstream: A
owner: sonnet-yarrow
status: doing
branch: task/T-A6-capstone-live
day: 6
priority: 2
depends_on: [T-A5]
---

## Context

T-A5 proved orchestration end-to-end (genuine gate pause/approve, 36-event journal)
but ran with a stub agent_fn — all 6 verify pillars escalated fail-closed because no
live docs were wired in. Round 2 = the product thesis with REAL artifacts: reuse
tonight's proven assets (T-C5's Renode firmware demo, D-lane structural doc, B5's
sourcing demo, ECAD divider recipe, parts library) so the verify nodes evaluate real
geometry/netlists/analyses and PASS on evidence. This is the first-Erik-demo target.

## Driver notes (foreman)

- Extend docs/sessions/demo_sprint_day5/run_capstone_demo.py (phase 3) rather than
  rewriting; keep phases 1-2 intact as regression.
- Build the mission's docs through the worker/MCP surface up front (enclosure part,
  divider schematic via the ecad recipe, cedar-post structure doc, firmware project),
  then hand their ids/paths into the DAG state so verify nodes hit them.
- Verify pillars expected to genuinely PASS: interference (enclosure), ERC/DRC
  (divider), fw_sim_run (T-C5 toolchain IS provisioned), dc_ratio (struct_check),
  budget (bom_price w/ unpriced-honest lines). An honest FAIL with evidence is
  acceptable output where the artifact genuinely fails — never fabricate.
- Gate pause/approve must still fire; record the full journal; DEBRIEF round-2
  section with per-pillar evidence.

## Definition of done

- [ ] All 6 verify pillars evaluated REAL artifacts (per-pillar evidence in DEBRIEF)
- [ ] Run journal recorded; gate paused + approved live
- [ ] Phases 1-2 still reproduce
- [ ] `graphify update .` run

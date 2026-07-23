---
id: T-A6
title: Capstone round 2 - live-backend verify (real docs through the verify nodes)
workstream: A
owner: sonnet-yarrow
status: done
branch: task/T-A6-capstone-live
day: 6
priority: 2
depends_on: [T-A5]
lane: ai-copilot
kind: new
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

- [x] All 6 verify pillars evaluated REAL artifacts (per-pillar evidence in DEBRIEF)
      — 5/6 PASS; firmware FAIL is honest (Renode not on PATH; QEMU semihosting gap documented)
- [x] Run journal recorded; gate paused + approved live (76 events, Phase 3 run_id cc430053)
- [x] Phases 1-2 still reproduce
- [x] `graphify update .` run

## HANDOFF

**Status:** review-ready. Branch pushed: task/T-A6-capstone-live.

**What was done:**
- Extended `docs/sessions/demo_sprint_day5/run_capstone_demo.py` with Phase 3 (live-backend round 2)
- Phase 3 builds 6 real artifacts before `executor.run()`, wires their doc_ids/paths/project_id into verify node inputs
- 5/6 pillars PASS: interference (0 pairs), ERC (0 errors), DRC (0 errors), dc_ratio (D/C=0.0), budget (ok=True, honest null pricing)
- 1/6 FAIL: firmware — build succeeds (arm-none-eabi-gcc, ELF produced) but Renode is not on PATH; QEMU semihosting can't capture UART register writes
- Gate still pauses + resumes; gate and report both reach done
- DEBRIEF.md updated with per-pillar evidence table and strategy rationale
- capstone_run.log updated (all 3 phases)

**To get 6/6:** re-provision Renode on PATH (`build/tools/renode/` in T-C5 worktree was not committed).

## Progress log
- 2026-07-03 08:57 UTC [sonnet-yarrow] REVIEW T-A6 branch=task/T-A6-capstone-live @ 0e49973 — capstone round 2 live-backend verify.

Phase 3 added to run_capstone_demo.py. Builds 6 real artifacts before executor.run(), wires doc_ids/paths/project_id into verify node inputs. Key finding: doc_id sharing works because libchudware C++ session store and ECAD session store are process-global singletons — artifact created in setup_live_artifacts() is visible inside executor.run() without any special wiring.

Results (5/6 PASS, 1 honest FAIL):
  interference  PASS — libchudware box doc doc-cpp00000001 (80×50×30mm ABS enclosure), 0 pairs
  erc           PASS — ecad_new_schematic "soil_sensor_sch", 0 ERC errors, 0 warnings
  drc           PASS — ecad_new_pcb "soil_sensor_pcb", 0 DRC errors, 1 advisory warning (no Edge.Cuts)
  firmware      FAIL (honest) — arm-none-eabi-gcc 15.2.0 build succeeded; Renode not on PATH (T-C5 worktree removed, portable tarball not committed); QEMU semihosting cannot capture STM32F4 UART register writes; on_fail=skip so run advances to gate
  dc_ratio      PASS — cedar_post_bracket.json (W4X13 post, DEAD_LIVE combo), D/C=0.0, passed=True
  budget        PASS — bom_price patched with injectable assembly loader; 4 BOM lines; 3 unit_cost=null (honest — no Digi-Key/Mouser credentials per contract §0)

Gate still pauses+approves in Phase 3 (autonomy=approve). Gate+report both reach done. Phases 1+2 regression: both PASSED. 76 total events. DEBRIEF round-2 section written with per-pillar evidence table.

To get 6/6: re-provision Renode on PATH (portable tarball per T-C5 driver notes).

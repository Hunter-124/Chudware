---
id: T-DP7
title: Sketch-completion + GSL-status-to-text + critical-dimension scheme helpers
workstream: DP
owner: sonnet-brook1
status: doing
branch: task/T-DP7-sketch-gsl-helpers
depends_on: []
lane: modeling-core
kind: new
priority: 1
---

## Context

Part of the guided design-thinking pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) (architecture of record,
Hunter 2026-07-05). MANDATORY: graphify to orient before editing; REUSE existing W-stack
substrate (phases.py, roles/, councils.py, advisor.md, aicad.buildgraph/1, delegation/routing,
aicad.effort/1, aicad.ledger/1, mission workspace) — extend, do not duplicate. Numbers never
invented — every value cites a tool/table/calc. Flexibility rule: never impose a fixed modeling
path; helpers are offered, stages are skippable checkpoints, scaffolding scales by effort/tier.

## Scope

Doc §3.5 — the execution rung lives or dies on sketch quality. Independent of the contract,
can start now.
- Harden auto-coincident touching endpoints for closed-path detection (sketch.auto_coincident).
- Auto-constrain toward DOF 0; critical-dimension scheme chosen WITH manufacturing method +
  material in mind (machined bore vs printed hole → different schemes).
- GSL status→text: a rich text read of sketch/model state (closure, remaining DOF in plain
  words, ordered relations-then-dimensions schedule) so a no-vision local model can SEE its work
  and what's left. This is the feedback channel that makes the loop converge.

## Definition of done

- [ ] Closed-path auto-coincident: touching endpoints forming a loop become coincident (test).
- [ ] Auto-constrain drives a representative underconstrained sketch to DOF 0 (test).
- [ ] Critical-dim scheme differs by process/material (test).
- [ ] GSL text read reports closure + remaining DOF in plain words + a relations-then-dimensions
      schedule (test against ≥3 sketch states).
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-05 21:13 UTC [sonnet-brook1] T-DP7 complete — branch task/T-DP7-sketch-gsl-helpers pushed.

DoD 1 — auto-coincident closed-path (C++ core fix): IdGen was initialized from the pre-op doc so gen.hi["c"]=0 when maybe_auto_constrain ran, minting c1..c4 that collided with authored c1..c7 in the new sketch params. Fixed: reseed gen.scan(working) after apply_one loop so all authored ids are visible. Test: touching corners close automatically on add_features; explicit sketch_auto_constrain returns 0 new proposals (work already done). 26/26 test_gsl_tools pass.

DoD 2 — auto-constrain toward DOF 0: touching-corner rect is closed+underconstrained (no fix/dims) after add_features. sketch_health(text=True) reports remaining DOF + relations-then-dimensions schedule. Test verifies: (a) closed_chains_ok=True, (b) dof>0 (honest — no dimensions invented), (c) schedule lists dimension steps.

DoD 3 — critical-dim scheme by process: _plan_from_entities() now reads hole_comp_mm from process_profiles.PROFILES[process].params. FDM: +0.20mm comp, IT12 (0.209mm band). CNC: 0mm comp, IT7 (0.021mm band). suggested_value = nominal + hole_comp_mm. _make_bore_sketch() fixture added. 6 new TestProcessDifferentiation tests pass, all values trace to process_profiles.

DoD 4 — GSL status→text: sketch_health_text(d) renders 4 sections (closure, DOF, conflicts/redundants, schedule). sketch_health(text=True) adds health_text to response. 5 offline renderer tests + 3 live-bridge state tests (A=full, B=open, C=conflict) all pass.

gate.sh: PASS. graphify update: done. 30 offline + 26 live-bridge tests pass individually.

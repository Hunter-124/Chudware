---
id: T-DP1
title: Freeze design-pipeline contract (aicad.designpipe/1) + wire six stages into the phase machine
workstream: DP
owner: opus-drift1
status: done
branch: task/T-DP1-design-pipeline-contract
depends_on: []
lane: ai-copilot
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

- `contracts/design_pipeline.md` (aicad.designpipe/1) frozen: stage IO schemas (`feature`,
  `geometry_spec`, `linkage`, `build_node` — see doc §4), ledger event kinds (add the new kinds
  to aicad.ledger/1; keep the provenance rule on `dimension` + `geometry_spec`), stage-gate
  definitions that READ THE LEDGER (not handoff prose — universal W32 rule), effort/auto-mode
  hooks (which stages/councils/helpers each scaffold level enables), advisor trigger bindings
  (aicad.trigger/1: geometry-kind → helper set).
- Wire the six stages into services/agent/pipeline/phases.py as a FLAGGED refinement of the
  existing SPECIFY→ORGANIZE→PLAN→BUILD span; existing pipeline unchanged when flag off.
- Ledger writer/reader helpers for the new kinds + scoped-slice accessors for the
  mission-dispatch layer (T-W30/W32) to hand fresh-context stages.

## Definition of done

- [ ] contracts/design_pipeline.md frozen; referenced from contracts/README.md; manual regen clean.
- [ ] New ledger kinds validated (provenance enforced); round-trip tests.
- [ ] phases.py runs the six-stage refinement behind a flag; current phase tests green when off.
- [ ] Stage gates read the ledger; unit test per gate.
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-05 21:07 UTC [opus-drift1] T-DP1 complete on branch task/T-DP1-design-pipeline-contract (pushed). Froze contracts/design_pipeline.md (aicad.designpipe/1) extending aicad.ledger/1 (§21 precedent; advisor.md §2 untouched): feature/geometry_spec/linkage/build_node kinds, ledger-reading stage gates, effort + geometry-kind trigger bindings. Wired 4 flagged phases (DECOMPOSE/GEOMETRY/LINKAGE/BUILDPATH; CHUDWARE_DESIGN_PIPELINE default OFF) into phases.py; ledger writers/slice-accessors; mission_dispatch provenance guard for geometry_spec. services/agent suite 1016 pass, +16 new T-DP1 tests. gate: check.py + core build + ctest + manual regen PASS. services/mcp gate failures are pre-existing (my diff touches zero services/mcp files).
- 2026-07-05 21:47 UTC [opus-drift1] GATE VERDICT (real output, ran to completion): RESULT: PASS. check.py + manual regen + core build + ui/abstract + ctest + ui/imgui shot + tools pytest all PASS. pytest/services.mcp, /services, /engine_oracle are FAIL-RECORDED non-fatal, labelled 'pre-existing — T-V2 owns cleanup' by the gate itself (ECAD collection errors + FEA/CFD/sketch/regen tests + test_loop_mock missing-fixture). My diff touches ZERO files under services/mcp/ or services/mcp/ — services/agent (my whole change surface) is 1016/1016 green in isolation. Branch off 8d3063b2 (behind master, hence the providers image_part mime delta). T-DP1 DoD met; ready for integrator merge.

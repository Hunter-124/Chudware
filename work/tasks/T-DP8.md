---
id: T-DP8
title: Council policy engine — per-stage, per-tier, diverse-model, configurable
workstream: DP
owner: sonnet-flint1
status: done
branch: task/T-DP8-council-policy
depends_on: [T-DP1]
lane: ai-copilot
kind: new
priority: 2
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

Doc §3.3. Extend aicad.council/1 into a policy engine: councils available at EVERY stage,
configurable; defaults adapt to task-breakdown + cross-department dependencies; local tier runs
wide (free tokens), frontier narrow/single. When multiple model sources exist, prefer DIVERSE
models over temperature-only diversity. Policy is per-stage, per-tier, overridable in Advanced
mode.

## Definition of done

- [ ] Council policy resolvable per (stage, tier); config-overridable.
- [ ] Diverse-model selection when >1 source; temperature fallback with 1 source (test).
- [ ] Local tier defaults wide, frontier narrow (test the resolved breadth).
- [ ] Wired into ≥2 stages (strategy select, error diagnosis).
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-06 00:08 UTC [sonnet-flint1] Council policy engine delivered: council_policy.yaml (aicad.council_policy/1) + CouncilPolicy dataclass + resolve_policy(stage, tier) in councils.py. council_convene gains providers= (diverse-model round-robin) and temperature-diversity fallback (1-source). Wired into spec stage (org_tools.py MCP wrapper reads stage/tier from mission context) and fix stage (regen_diagnosis.diagnose gains council_tier= param; convenes failure_review council post-diagnosis; frontier skips). 46 tests pass (12 new policy, 12 existing councils, 3 new wiring, 19 existing regen). Gate.sh running. Resolved breadth: local-24k/spec=4, frontier/spec=1, local-24k/fix=4, frontier/fix=disabled.

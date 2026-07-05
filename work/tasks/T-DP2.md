---
id: T-DP2
title: feature_architect role + functional-decomposition helpers + requirement-coverage checker
workstream: DP
owner: sonnet-teal1
status: doing
branch: task/T-DP2-feature-architect
depends_on: [T-DP1]
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

Stage 1 (doc §2). New role services/agent/roles/feature_architect.yaml with a rich system
prompt: enumerate every FUNCTION the part must perform (mount/mate/seal/bear-load/clear/fasten/
locate/dissipate) BEFORE geometry. Output `feature[]` = {id, function, driving_requirement,
interfaces_with, criticality} written to the ledger.
- Helpers: part-class functional checklists (bracket/housing/shaft/plate/enclosure/fitting…);
  requirement-coverage checker (every requirement → ≥1 feature; every feature → a requirement or
  recorded assumption).
- Completeness-critic council hook ("what function did we miss?").

## Definition of done

- [ ] feature_architect role yaml + prompt; handoff schema validated.
- [ ] Functional checklists for ≥6 part classes; coverage checker with tests.
- [ ] Ledger `feature` events written + read by the Stage-1 gate.
- [ ] Gym: a C1-class task produces a coherent feature list (real run, honest result).
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-05 22:19 UTC [sonnet-teal1] DoD met — all deliverables landed on branch task/T-DP2-feature-architect (worktree /home/hunter/Repositories/CAD-T-DP2).

Commits:
  9e1293bb — feature_architect.yaml, functional_decomposition.py, test_coverage_checker.py, phases.py extension
  2ccfb9e9 — C1-DP2 gym challenge, run_dp2_gym.py, run artifacts (3 seeds)

Deliverables:
1. services/agent/roles/feature_architect.yaml — rich system prompt: 8-primitive function vocabulary (MOUNT/MATE/SEAL/BEAR-LOAD/CLEAR/FASTEN/LOCATE/DISSIPATE), 6 part-class checklists in prompt + 8 in code, UNIVERSAL RULES, completeness-critic slot declared (T-DP8 hook)
2. services/agent/pipeline/functional_decomposition.py — PART_CLASS_CHECKLISTS (8 classes: bracket/housing/shaft/plate/enclosure/fitting/fastener/seal), check_requirement_coverage(), gate_context_from_ledger()
3. services/agent/pipeline/phases.py — _gate_decompose extended: untraced_features/orphan_requirements surface as reasons when caller supplies them; gate name stays "features_present"; DP1 tests unchanged
4. services/agent/pipeline/test_coverage_checker.py — 28 new tests (82 total, all green)
5. Gym C1-DP2: seed1 PASS (5 features, all traced, all 3 reqs covered, DECOMPOSE gate passes); seed2 FAIL-correct (untraced FT-04 surfaces in reasons); seed3 FAIL-correct (orphan R-03 surfaces in reasons)

gate.sh: PASS (exit 0). FAIL-RECORDED items (mcp_server, services, engine_oracle) are all pre-existing, owned by T-V2.
graphify update: 35144 nodes, 71312 edges, 1618 communities.

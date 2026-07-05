---
id: T-W8
title: Failure cards and bounded fixer loop - unified diagnosis with candidate fixes and escalation
workstream: W
owner: sonnet-ferrow
status: review
branch: task/T-W8-failure-cards
depends_on: [T-W5]
lane: ai-copilot
kind: new
priority: 1
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.7, contracts/advisor.md §5). Every failure
class (rebuild, solver, interference, overlap, dfm, sim, cam, export) becomes an
aicad.failure/1 card with deterministic diagnosis where possible, candidate fixes,
and a HARNESS-enforced attempt budget: one candidate per attempt, predicted vs
measured effect recorded, escalation to ask_user/dag_replan on exhaustion. Kills the
endless guess-loop by construction.

## Files / areas touched

- `services/agent/advisor/failures.py` — card factory per class, attempt tracker,
  escalation executor
- `services/mcp/regen_tools.py` — map regen_diagnose → class:rebuild cards
- `services/agent/advisor/engine.py` — blocker advisories promote to cards; fixer
  dispatch
- `services/agent/roles/fixer.yaml` — tighten with card protocol (T-W7 file; if T-W7
  unmerged, ship the card here and note it)
- `services/dag/` — on_fail path routes through cards; dag_replan escalation
- tests: `services/agent/advisor/test_failures.py`

## Steps

1. Read contract §5. Card shape frozen; escalation is harness logic, never model choice.
2. Card factories: rebuild (wrap regen_diagnose verbatim — verbatim-splice policy),
   solver (conflicting_tags from sketch_health → "remove constraint X" candidates),
   interference/overlap (from T-W5 advisory items, promote when severity=blocker),
   dfm (dfm_check findings), sim (margin misses from describe_results + policy),
   cam (gouge/rapid-through from cam_simulate), export failures.
3. Attempt protocol: harness applies/records ONE candidate fix per fixer turn
   (`attempts[]` with predicted_effect before, measured delta after via re-running
   the originating check); `result` classification same|improved|worse|new_failure.
4. Budgets + escalation: per-class defaults (rebuild 3, interference 3, sim 2...);
   on exhaustion the harness (not the model) fires ask_user with evidence (render +
   card) or dag_replan per card.budget.on_exhaust; wire both, config default ask_user.
5. Ledger: cards + attempts are ledger events; fc_ids stable for cross-referencing.
6. Tests: card creation per class from fixtures; one-fix-per-attempt enforcement;
   improved/worse classification against measured deltas; escalation fires exactly at
   budget; regen card verbatim-splice.
7. MCP freshness + `graphify update .`.

## Definition of done

- [ ] All 8 classes produce aicad.failure/1 cards with deterministic `why` where the
      underlying tool provides it
- [ ] One-candidate-per-attempt + predicted/measured recording enforced by harness
- [ ] Escalation (ask_user + dag_replan) fires on budget exhaustion, never before/after
- [ ] Cards/attempts in ledger; tests pass

## AI hints

- `graphify explain "regen_diagnose"` first; its fix-ops card is the precedent.
- Merge-session memory: verbatim-splice policy — do not paraphrase tool diagnoses
  into cards, embed them.
- Coordinate shapes with T-W5's engine.py (same package) — rebase on its branch if
  both in flight; work_board shows the owner.

## Scope update 2026-07-04 (in-app pivot — Hunter directive)

Priority raised to 1: the in-app copilot harness (services/agent + GUI) is the sole
integration target for this task. Changes vs the original spec:
- SKIP the MCP freshness rule steps (server instructions, cad_guide topics, manual
  regen) — external MCP surface polish is parked in T-W20 (deferred batch task).
- Registering tools with the MCP server is OPTIONAL — do it only when it is one
  decorator on the same function and costs nothing; never let it drive design.
- Acceptance = the capability working through the IN-APP path (services/agent hosts,
  loop.py / dag runner / pipeline), proven by python tests. graphify update . still
  required (that serves our own agents).

## Progress log
- 2026-07-05 02:57 UTC [sonnet-ferrow] DONE (sonnet-ferrow). Branch task/T-W8-failure-cards pushed.

Built:
- services/agent/advisor/failures.py — all 8 failure classes produce §5-compliant aicad.failure/1 cards. Deterministic fc_id from hash(class+where+why) — budget-reset proof. Verbatim-splice policy: regen_diagnose card.diagnosis embedded as-is (handles dataclass or dict), dfm/cam/sim tool results embedded verbatim. record_attempt() enforces one-candidate-per-attempt + predicted/measured recording. budget_exhausted() + escalate() fire at exactly budget exhaustion (harness-owned, never model). Both ask_user (advisory blocker item with all attempt evidence) and dag_replan (splice via services/dag/replan.dag_replan with bounded retry back-edge) wired; graceful fallback to ask_user when dag params absent. Cards + attempts written as ledger "artifact" events.
- services/agent/advisor/engine.py — build_advisory() extended to include failure_cards key: blocker items promoted via promote_advisory_blockers().
- services/agent/advisor/test_failures.py — 64 tests covering all 8 factories, stable id, one-fix-per-attempt, improved/worse/same/new_failure classification, off-by-one budget, escalation timing, verbatim-splice, promote_advisory_blockers, ledger integration.

Test results: 64 new pass, 0 new failures. Pre-existing failures (issue #16): test_neutral_part_and_message_helpers, test_h7_shadow::test_offline_mode_refuses_cloud_provider — both unchanged.

Full suite: `287 passed, 1 skipped, 2 failed (pre-existing)` via `.venv-mcp/bin/python -m pytest services/agent -q --ignore=services/agent/test_loop_mock.py`

graphify update . run — 30000 nodes, 61183 edges, 1418 communities.

Tool shape sources used: real dfm_check ("checks" not "findings"), cam_simulate SimResult struct (gouges/rapid_collisions/holder_collisions), doc_sketch_health "conflicting" key (not conflicting_tags). All confirmed from source by subagent.

No deviations from spec. fixer.yaml escalation_policy.max_attempts=1 (per-engagement, single-shot) is distinct from card budget.max_attempts=3 (outer harness loop) — both honored per advisor guidance.

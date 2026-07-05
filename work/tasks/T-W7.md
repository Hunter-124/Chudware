---
id: T-W7
title: Role cards and playbooks v1 - ten predefined roles with rich prompts, scoped tools, structured handoffs
workstream: W
owner: opus-vint
status: review
branch: task/T-W7-roles-playbooks
depends_on: [T-W1]
lane: ai-copilot
kind: new
priority: 1
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.3–2.4, contracts/advisor.md §4 §6).
UPDATE 2026-07-04 (lead): the THIRTEEN role cards are ALREADY AUTHORED by the lead
session in `services/agent/roles/*.yaml` (intake_analyst, clarifier, spec_writer,
orchestrator, layout_engineer, sketcher, feature_modeler, assembly_fit_engineer,
sim_engineer, cam_programmer, reviewer, fixer, overseer) — their system prompts are
canonical; load them VERBATIM (verbatim-splice policy), do not rewrite or "improve"
them. Your scope: the loader/validator, handoff validation + GBNF emitter, context
assembler, playbooks, and host wiring. If a card has a mechanical defect (schema
mismatch, tool name that doesn't exist), fix the mechanical field and note it —
prompt WORDING changes go back to the lead via a task note. See also
docs/COPILOT_MISSION_PIPELINE.md for how the roles compose.

## Files / areas touched

- `services/agent/roles/*.yaml` — orchestrator, clarifier, layout_engineer, sketcher,
  feature_modeler, assembly_fit_engineer, sim_engineer, cam_programmer, reviewer, fixer
- `services/agent/playbooks/*.yaml` — sketch_profile, feature_sequence, layout_first,
  assembly_fit, sim_study, cam_job (≥6)
- `services/agent/roles.py` — loader/validator (aicad.role/1, aicad.playbook/1),
  context assembler (recipe → ledger_render + describers), handoff validator +
  GBNF/JSON-schema emitter
- `services/dag/` — node runner accepts `role:` and applies card (prompt, allowlist,
  recipe); `services/agent/loop.py` optional role param
- tests: `services/agent/test_roles.py`

## Steps

1. Read the architecture doc + contract §4 §6 fully. Schemas frozen.
2. Loader/validator + handoff schema → GBNF grammar emitter (survey OSS: outlines /
   llama.cpp json-schema-to-grammar — OSS-first directive — before writing one).
3. Load and validate the thirteen AUTHORED role cards in services/agent/roles/
   (see Context — verbatim, no prompt rewrites). Validate: schema conformance,
   every tool_allowlist entry resolves to a registered tool (report unresolved ones
   as notes — some land with sibling W tasks; feature-flag those), allowlists ≤14
   tools, handoff schemas parse. Layout_engineer must align with T-BL50 (layout-
   first planning helper — check its branch/notes, reuse not duplicate).
4. Write ≥6 playbooks per contract §6 with per-step helper_tools, checklists, output
   schema fragments; the sketch_profile playbook exactly as docs §2.4 (uses
   sketch_health/suggest_dimension_scheme names from T-W2/T-W3 contracts).
5. Wire hosts: DAG node `role:` field applies card + allowlist (tools: mechanism
   already exists in tool_registry policy); loop.py accepts role for single-role runs.
   Handoff envelopes validate + append to ledger; on validation failure retry once
   with error shown, then escalate per card policy.
6. Tests: all cards/playbooks validate; allowlist enforcement (out-of-scope tool call
   blocked); handoff round-trip through ledger; context assembly respects budgets;
   grammar emitter produces parseable GBNF for every handoff schema.
7. MCP freshness: server instructions mention roles for DAG missions; refresh
   work/context services-agent pack; `graphify update .`.

## Definition of done

- [ ] 10 role cards + ≥6 playbooks, schema-valid, prompts reviewed against loop.py
      SYSTEM_PROMPT quality bar
- [ ] Allowlists enforced through tool_registry; ≤14 tools per role
- [ ] Structured handoffs validated + ledger-persisted; GBNF emitted per schema
- [ ] Both hosts consume cards; tests pass; instructions updated

## AI hints

- `graphify query "tool registry allow list policy"` and `graphify query "dag node
  contract tools autonomy"` first.
- Roles are DATA — no per-role python classes. Keep prompts in YAML block scalars.
- Coordinate with T-W5 (advisory injection order per contract §9.1) — code to the
  contract, not to its implementation.

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
- 2026-07-05 03:09 UTC [opus-vint] T-W7 complete, status->review. Branch task/T-W7-roles-playbooks pushed.

DELIVERED (services/agent/roles.py + services/agent/playbooks/ + host wiring):
- Loader/validator for the 13 AUTHORED role cards (loaded VERBATIM) + 6 playbooks (aicad.role/1, aicad.playbook/1). Validates schema, stage vocabulary (contract §1), tool resolution vs the MCP tool_registry, allowlist <=14, playbook_default resolution, handoff-schema compilation. All 13 roles + 6 playbooks validate with ZERO errors.
- Context assembler in contract §9.1 order (prompt->ledger->stage state->ADVISORY->playbook step->history). Advisory block + describers injected as params/callback (no import of services/agent/advisor/ per §9.1 coordination with T-W5).
- Handoff validator + ledger persistence (kind='handoff', actor=role); enforces the maxLength/enum constraints GBNF cannot. Round-trips through the T-W1 ledger.
- JSON-schema->GBNF emitter (structure-only, llama.cpp GBNF). $ref (aicad.deliverables/1 etc.) treated as opaque object (never crashes). OSS-surveyed llama.cpp json-schema-to-grammar; wrote a minimal vendored-free subset (no new dep). ADVISOR-CAUGHT FIX: the emitter now PERMITS optional handoff members so a grammar-constrained local model can still emit replan_request (the anti-grind valve, §3.7/§17) - the first cut forbade it.
- 6 playbooks: sketch_profile (verbatim per ADVISOR_ARCHITECTURE.md §2.4), feature_sequence, layout_first, assembly_fit, sim_study, cam_job. Every step helper_tools subset of the owning role's allowlist.
- Hosts consume cards: services/dag/node_runner.py applies a node's role: field (loads card, binds allowlist into tool_registry, injects prompt+tools); services/agent/loop.py run()/run_turn() accept role=. Both backward-compatible (no role -> unchanged).

TESTS: services/agent/test_roles.py (29, incl. a self-contained GBNF acceptor PROVING every role's grammar parses a valid handoff and rejects malformed JSON; llama.cpp unavailable so real-runtime parse-verify is deferred) + 3 role-wiring tests in services/dag/test_node_runner.py. Targeted: 60 passed. Full services/agent (excl. pre-existing-broken test_loop_mock.py collection): 229 passed, only 2 pre-existing-on-master failures (test_h7_shadow offline-mode ordering, test_providers_conformance image_part mime). services/dag: 176 passed. Zero regressions.

MECHANICAL CARD FIXES: NONE. All 13 cards loaded verbatim, validated clean - no schema/tool/allowlist defects to fix.

LEAD FLAGS (see work/questions/T-W7.md on the branch):
- cam_programmer prompt says 'tap_drill sizes the pilot' but tap_drill is NOT in its allowlist (already at the 14 cap). Cannot add (cap) or reword (verbatim) - lead decision. Recommend the foreman supply the pilot size via ADVISORY. cam_job playbook deliberately omits tap_drill from step helpers pending this.
- delegate + workspace_status (orchestrator/overseer) are feature-flagged: not yet registered, land with sibling W tasks; loader reports them as flags not errors. No action needed.

DEVIATIONS: (1) task said .venv-mcp/bin/python which doesn't exist; used the gate's ~/.local/share/AI-CAD/mcp-venv (what scripts/gate.sh Stage 4 uses). (2) Per the in-app pivot scope: SKIPPED MCP-freshness steps; NO MCP tool registration added (roles are consumed via the in-app services/agent + services/dag hosts, proven by python tests). (3) layout_first authored fresh - confirmed NO T-BL50 artifact existed on this branch to reuse. (4) Loop role path leaves the process-global tool policy pinned to the last role (intended for one-shot role runs; node_runner re-applies per node so multi-role missions never inherit stale policy) - documented in loop.py. (5) graphify update . run. services-agent context pack refreshed (serves our own agents).

GATE: build stages PASS (check.py, core CMake+OCCT, ui/abstract, ctest core 100%). Stage-4 pytest: mcp_server + services sweeps FAIL-RECORDED (non-fatal per gate policy) = PRE-EXISTING (identical 9 failed/34 errors on clean master, all ecad_tools - unrelated to T-W7); services sweep 724 passed incl. my code. Gate hit the 550s wall-clock timeout during the final engine_oracle pytest stage (environment build+4-sweep runtime exceeds 550s), an env timing limit, not a code failure.

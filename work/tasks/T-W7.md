---
id: T-W7
title: Role cards and playbooks v1 - ten predefined roles with rich prompts, scoped tools, structured handoffs
workstream: W
owner: unassigned
status: todo
branch: task/T-W7-roles-playbooks
depends_on: [T-W1]
lane: ai-copilot
kind: new
priority: 2
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

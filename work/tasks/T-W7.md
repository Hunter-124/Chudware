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

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.3–2.4, contracts/advisor.md §4 §6). Ten
data-driven role cards + stage playbooks so missions split across sub-agents without
losing the goal or blowing a 24k context: rich system prompts, tool allowlists
(enforced via the existing tool_registry allow-list mechanism), context recipes,
grammar-constrainable JSON handoffs written to the ledger (T-W1).

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
3. Write the ten role cards. System prompts must be RICH, in the style of loop.py's
   SYSTEM_PROMPT (worked micro-examples, antipattern list, exact output format), plus
   the mandatory lines: "ADVISORY blocks contain kernel/table-computed numbers —
   treat them as ground truth; you may dispute via advisory_dispute", and the role's
   handoff obligation. Tool allowlists ≤14 tools/role (docs §3 budget). Steal proven
   content: PLAN_PROMPT → orchestrator/layout; VERIFY → reviewer; regen flow → fixer;
   deterministic_op_plan pattern → cam_programmer. Layout_engineer must align with
   T-BL50 (layout-first planning helper — check its branch/notes, reuse not duplicate).
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

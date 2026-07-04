# contracts/advisor.md — Advisory Layer contracts (aicad.advisor/1)

Frozen 2026-07-04. Companion to `docs/ADVISOR_ARCHITECTURE.md` (Pillar 20).
Additive changes bump the minor (`aicad.advisor/1.1`); breaking changes require a new
major and a migration note here. All schemas are JSON; all lengths mm, angles deg,
masses g (canonical units per the interaction contract's `normalize_dimension`).

## §1 Stage vocabulary

```
intake | spec | layout | sketch | feature | part_verify | assembly | sim | cam | deliver | fix
```

- `fix` is re-entrant: it carries a `return_to` stage.
- Stage inference is deterministic from tool-call history (mapping table lives in
  `services/agent/advisor/stages.py`); `stage_set(stage, reason)` overrides.
- Each stage declares: `check_battery` (ordered list of read-only checks),
  `helper_tools` (names advertised in advisories), `exit_gate` (checks that must pass
  before the orchestrator advances), `playbook_default`.

## §2 Design ledger (`aicad.ledger/1`)

File: `<cache_root>/runs/<run_id>/ledger.json`. Append-only `events[]` plus a
materialized `state`. Every event: `{seq, ts, actor, kind, payload}` where `actor` is
a role id, "human", or "foreman".

`state`:

```json
{
  "schema": "aicad.ledger/1",
  "run_id": "r_...",
  "brief": {"title": "", "summary": "", "success_criteria": ["..."]},
  "requirements": [{"req_id": "R1", "text": "", "source": "user|derived", "status": "open|met|waived"}],
  "decisions": [{"id": "D1", "stage": "layout", "text": "", "rationale": "", "actor": "", "ts": ""}],
  "dimensions": [{
    "id": "DIM1", "name": "bore_diameter", "value": 20.02, "unit": "mm",
    "driver": "sliding fit on shaft S1 (H7/g6)", "tolerance": {"fit": "H7", "upper": 0.021, "lower": 0.0},
    "status": "proposed|confirmed|derived", "confirmed_by": null,
    "refs": {"doc_id": "", "feature": "F7", "entity": null}
  }],
  "questions": [{"id": "Q1", "text": "", "status": "open|answered", "answer": null, "blocking": false}],
  "stage_log": [{"stage": "sketch", "entered": "", "exited": "", "exit_gate": "pass|waived"}],
  "artifacts": [{"kind": "doc|render|study|cam_job|export", "id": "", "path": "", "stage": "", "note": ""}]
}
```

Rules: numeric values must originate from a tool result, calculator, table, or an
explicit user answer — the writing tool records `provenance` on dimension events.
`ledger_render(budget_tokens)` emits the deterministic compact view, priority order:
brief → active-stage dimensions → open questions → decisions → requirements → rest;
truncation is by whole entries, never mid-entry.

MCP: `ledger_get(run_id, section?, budget_tokens?)`, `ledger_note(run_id, kind, payload)`.

## §3 Advisory block (`aicad.advisory/1`)

Produced by the foreman after tool calls / node steps; injected as a delimited block:

```
[ADVISORY seq=14 stage=assembly severity=action]
...rendered items...
[/ADVISORY]
```

Item schema:

```json
{
  "schema": "aicad.advisory/1",
  "seq": 14,
  "stage": "assembly",
  "items": [{
    "id": "adv_...",
    "severity": "info|hint|action|blocker",
    "check": "interference",
    "finding": "bodies bracket/shaft interfere 340 mm3 near [12,0,5]; bore d19.80 vs shaft d20.00",
    "refs": {"doc_id": "", "bodies": ["bracket", "shaft"], "features": ["F7"], "faces": []},
    "numbers": {"overlap_mm3": 340, "bore_d": 19.8, "shaft_d": 20.0},
    "candidate_fixes": [{
      "action": "edit_feature_param",
      "params": {"feature": "F7", "param": "d", "value": 20.02},
      "basis": "ISO 286 H7/g6 sliding fit on d20 -> hole 20.000..20.021",
      "predicted_effect": "clearance 0.007..0.041 mm",
      "confidence": "table"
    }],
    "helper_tools": ["fit_calc", "measure_between"],
    "source": {"kind": "kernel|table|calc|llm", "detail": "doc_eval_interference"}
  }]
}
```

Rules: `candidate_fixes` MAY be empty only for `info`. `confidence` ∈
`{kernel, table, calc, heuristic, llm}` — renderers must show non-kernel/table sources
as suggestions, not facts. Rendering budget comes from the tier profile; items are
dropped lowest-severity-first, and a dropped-count line is always appended (no silent
truncation). Advisories are logged to the ledger event stream.

## §4 Role card (`aicad.role/1`) — files `services/agent/roles/<id>.yaml`

```yaml
schema: aicad.role/1
id: sketcher
goal: one sentence
stage_scope: [sketch]
system_prompt: |            # rich: identity, quality bar, worked micro-examples,
  ...                       # output format, "ADVISORY numbers are ground truth" rule
tool_allowlist:             # registry families or tool names; enforced via
  - family: gsl             # tool_registry allow-list mechanism
  - tool: sketch_health
context_recipe:             # what gets assembled into each turn, with budgets
  ledger_tokens: 800
  describers: [{tool: describe_sketch, budget: 1200}]
playbook_default: sketch_profile
ledger_sections: {read: [brief, dimensions, decisions], write: [dimensions, decisions, questions]}
handoff_schema:             # JSON schema the role must emit on completion
  type: object
  required: [status, summary, outputs]
  properties: {...}
escalation_policy: {max_attempts: 3, on_exhaust: ask_user}
```

Handoff envelope (`aicad.handoff/1`): `{role, status: done|blocked|escalated, summary
(<=120 words), outputs (schema above), ledger_writes: [event seqs], next_role_hint}`.
Handoffs are validated against the schema at the harness layer (grammar-constrained
for local tiers) and written to the ledger; the next role receives the envelope, not
the chat.

## §5 Failure card (`aicad.failure/1`)

```json
{
  "schema": "aicad.failure/1",
  "id": "fc_...",
  "class": "rebuild|solver|interference|overlap|dfm|sim|cam|export",
  "stage": "feature",
  "what": "one-line",
  "where": {"doc_id": "", "features": [], "bodies": [], "entities": [], "study": null},
  "why": {"kind": "deterministic|heuristic|unknown", "detail": ""},
  "candidate_fixes": [ /* same shape as advisory candidate_fixes */ ],
  "helper_tools": [],
  "attempts": [{"n": 1, "fix_applied": {}, "result": "same|improved|worse|new_failure", "delta": {}}],
  "budget": {"max_attempts": 3, "on_exhaust": "ask_user|dag_replan"}
}
```

Rules: one candidate fix per attempt; the fixer must record `predicted_effect` before
and measured `delta` after. `attempts` full ⇒ the harness (not the model) escalates.
`regen_diagnose` output maps to `class: rebuild`.

## §6 Playbook (`aicad.playbook/1`) — files `services/agent/playbooks/<id>.yaml`

```yaml
schema: aicad.playbook/1
id: sketch_profile
stage: sketch
steps:
  - id: datums
    goal: identify functional surfaces and datum choice
    helper_tools: [describe_part]
    checklist: ["every mating surface named", "datum stated with reason"]
    output: {type: object, required: [datum, functional_surfaces]}
  - id: entities
    ...
exit_gate: [sketch_fully_constrained, profile_closed, no_conflicting_tags]
```

Enforcement per tier profile: `step_locked` (one step per turn, output validated),
`checklist` (whole playbook as a checklist, single turn allowed), `off`.

## §7 Calculator envelope (`aicad.calc/1`)

Extends the Pillar 8 result contract. Every calculator returns:

```json
{
  "schema": "aicad.calc/1",
  "calc": "fit_calc",
  "answer": {"hole": [20.0, 20.021], "shaft": [19.959, 19.98], "clearance": [0.007, 0.041]},
  "units": "mm",
  "equation": "ISO 286-2 table, IT7/IT6 on d=20",
  "inputs": {"nominal": 20, "fit": "H7/g6", "prefilled": {"nominal": "doc_eval_measure F7"}},
  "source": {"kind": "table|closed_form", "citation": "ISO 286-2:2010"},
  "notes": []
}
```

`inputs.prefilled` records which inputs the foreman filled from live geometry so the
model (and audit) can distinguish measured from assumed values.

## §8 Tier profile (`aicad.tier/1`) — `services/agent/tiers.yaml`

```yaml
schema: aicad.tier/1
profiles:
  local-24k:
    context_tokens: 24576
    budgets: {system: 2500, ledger: 800, state: 1500, advisory: 600, tools: 2500, history: 8000, generation: 8000}
    playbook_enforcement: step_locked
    structured_output: gbnf
    advisory_verbosity: full
    max_tools_per_role: 14
  local-48k: {..., playbook_enforcement: step_locked, structured_output: gbnf}
  mid:       {..., playbook_enforcement: checklist, structured_output: json_mode}
  frontier:  {..., playbook_enforcement: off, structured_output: free, advisory_verbosity: compact}
```

## §9 Injection & precedence rules

1. Assembly order per turn: role system prompt → ledger render → stage state →
   ADVISORY block → playbook step (if step_locked) → history.
2. Advisories never overwrite user instructions; on conflict the advisory renderer
   prepends "user override active" and downgrades to `info`.
3. `blocker` severity gates stage exit (orchestrator refuses advance), it does NOT
   block the model's next action — models may fix or contest (`advisory_dispute` tool
   logs to ledger; disputes with `kernel` sources require ask_user to waive).
4. All injected blocks are reproducible offline from the ledger event log (replay).

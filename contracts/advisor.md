# contracts/advisor.md — Advisory Layer contracts (aicad.advisor/1.7)

Frozen 2026-07-04; §10–§15 added same day (1.0 → 1.1), §16–§17 (1.1 → 1.2),
§18–§21 org layer (1.2 → 1.3), §22 design cognition ladder (1.3 → 1.4),
§23 harness effort presets & user overrides (1.4 → 1.5),
§12 delegation recursion + budget_meter + kill-switch (1.5 → 1.6, additive),
§11 verdict `targeted_reentry` reroute field (1.6 → 1.7, additive). All bumps additive. Companion to
`docs/design/ADVISOR_ARCHITECTURE.md` and `docs/design/COPILOT_MISSION_PIPELINE.md`
(pipeline operation).

1.3 additive amendments to earlier sections:
- §17 replan `scope` gains `org` (restructure departments/channels/hierarchy) —
  routes to orchestrator + overseer audit.
- §12 delegation nodes gain `kind: worker|ponder`. `ponder` = a read-only,
  budget-bounded spin-off of the requesting role for deep-diving one question
  ("what's the best datum scheme here?"); like every delegation kind, it auto-approves
  at every depth, and its result lands as a ledger note.
- §8 tier profiles gain `deliberation: full|standard|minimal`. INVERTED DIAL
  (design decision, 2026-07-04): local tiers default `full` (tokens are free — councils,
  ponders, trigger packs all on), frontier defaults `minimal` (tokens cost money;
  councils only where risk warrants).
Additive changes bump the minor; breaking changes require a new
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

- **Layout addendum (2026-07-23):** events now persist append-only in
  `<cache_root>/runs/<run_id>/ledger.jsonl` (one JSON event per line, O(1) append);
  `ledger.json` carries only the materialized `state` snapshot. Readers must go
  through `services/agent/ledger.py` `Ledger.load()`, which accepts both layouts;
  legacy whole-document `ledger.json` files still load and migrate on first append.

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

Extends the calculator result contract. Every calculator returns:

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

## §10 Preflight result (`aicad.preflight/1`)

Dry-run of planned ops on a shadow doc clone; never mutates the live doc.

```json
{
  "schema": "aicad.preflight/1",
  "id": "pf_...",
  "target": {"doc_id": "", "tool": "add_features", "ops_hash": ""},
  "verdict": "pass|warn|fail",
  "checks": [{
    "check": "refs_resolve|params_sane|rebuild_predict|downstream_impact",
    "result": "pass|warn|fail",
    "detail": "fillet r=5 exceeds adjacent wall 3.2 on F9",
    "refs": {"features": ["F9"], "faces": []}
  }],
  "predicted": {"feature_status": [], "dof": null, "interference_delta": null},
  "downstream": [{"feature": "F12", "relation": "references edited face f_5"}],
  "advisory_items": [ /* aicad.advisory/1 items, ready to inject */ ]
}
```

Rules: freedom rule applies — `fail` never blocks; applying a failed-preflight
mutation without an `advisory_dispute` is logged (`preflight_overridden` ledger
event) and is an automatic audit trigger (§11). Shadow clones are ephemeral; the
implementation must guarantee zero live-doc side effects (clone via doc
serialization, never in-place).

## §11 Overseer audit record (`aicad.audit/1`)

```json
{
  "schema": "aicad.audit/1",
  "id": "aud_...",
  "trigger": "plan_ratify|stage_exit|escalation|preflight_override|change_request|manual",
  "scope": {"run_id": "", "stage": "", "roles": [], "buildgraph_nodes": []},
  "verdicts": [{
    "class": "plan_gap|wrong_order|spec_drift|missed_requirement|bad_fix|cot_error|ok",
    "finding": "plan mills the pocket before drilling datum holes used as fixture refs",
    "evidence": {"ledger_events": [], "advisories": [], "handoffs": [], "preflight": []},
    "required_action": {"kind": "replan|reorder|edit|ask_user|none", "detail": "", "dispatch_role": null},
    "severity": "info|action|blocker"
  }],
  "model": {"provider": "", "id": ""},
  "tokens": 0
}
```

Rules: overseer runs at trigger points only, never per-turn. Verdicts are delivered
as advisory items (`source.kind: llm`, so renderers mark them as review findings,
not kernel facts). Two `blocker` audits with the same `class+finding` fingerprint ⇒
mandatory ask_user. Change-request trigger: every user-issued fix/change first
produces an audit-scoped change plan (affected requirements/dimensions/features/
parts listed with ledger refs) before any mutation is dispatched.

**Additive amendment (1.6 → 1.7):** each non-`ok` verdict gains an optional
`targeted_reentry` object — the **owning design-pipeline stage** a correction routes
back to, so a fix re-enters the stage that produced the error rather than triggering
a blind full retry (doc §3.1). It is computed **deterministically by the harness**
(`services/agent/advisor/reroute.py`) from the verdict's evidence — the model does
not supply it (`validate_audit` strips model-set fields; the reroute is attached
post-validation). The routing keys off the ledger event **kind** cited in
`evidence.ledger_events`, per the frozen `design_pipeline.md` §2/§4 table:

```json
"targeted_reentry": {
  "stage": "decompose|geometry|linkage|buildpath",   // phases.py Phase value
  "role": "feature_architect|geometry_designer|linkage_planner|orchestrator",
  "basis": "why this target (which evidence kind / class fallback)",
  "feature": "G1"           // the specific artifact id to re-enter at (or null)
}
```

`feature`→decompose, `geometry_spec`→geometry, `linkage`→linkage, `build_node`→
buildpath (the earliest owning stage wins when several kinds are cited — fix the
deepest-upstream rung). A `targeted_reentry` is *always* a targeted correction to an
owning stage — never a blind full retry — so there is no `full_restart` field; when
nothing resolves the reroute is simply `null`. Verdict `class` is a fallback signal
only, and that fallback is gated to design-pipeline runs (it needs a DP artifact in
the ledger, or an explicit `dp_active`); a legacy audit therefore routes no verdict
into a DP stage. `ok` verdicts carry `targeted_reentry: null`. This is additive; readers that ignore the field are
unaffected. The same routing map powers the change-request `stage_reentry` on
dispatch rows (a user "thicker wall" re-enters GEOMETRY on the wall's geometry_spec).

## §12 Delegation tree (`aicad.delegation/1`)

**Amendment (1.5 → 1.6):** Removed hard depth cap; added recursive spawn,
human-visible budget meter, and kill-switch. `depth_cap` is retained for backward
compatibility with artifact readers but is NO LONGER enforced as a hard limit.

**2026-08-03 control-plane amendment:** Human/overseer spawn approval was removed end to end.
The delegation-node `approval` object is deleted; every request immediately transitions from
`pending` to `approved` and records `approved_ts`.

```json
{
  "schema": "aicad.delegation/1",
  "run_id": "r_...",
  "nodes": [{
    "id": "dg_...", "parent": null,
    "role": "sketcher", "brief": "<=500 chars, self-contained",
    "requested_by": "orchestrator",
    "kind": "worker|ponder",
    "depth": 0,
    "approved_ts": 0.0,
    "model": {"provider": "", "id": "", "tier": "local-24k"},
    "budget": {"tokens_granted": 0, "tokens_spent": 0, "attempts": 0},
    "docs": [{"doc_id": "", "lease": "write|read"}],
    "status": "pending|approved|running|done|failed|revoked"
  }],
  "depth_cap": 3,
  "soft_depth_limit": 8,
  "killed": false,
  "budget_meter": {
    "tokens_granted": 50000,
    "tokens_spent": 0,
    "tokens_remaining": 50000,
    "token_pct": 0.0,
    "agents_spawned": 0,
    "agents_remaining": 64,
    "max_agents": 64,
    "agent_pct": 0.0,
    "soft_depth_limit": 8,
    "killed": false
  }
}
```

Rules:
- Every delegation auto-approves at every depth.
- **No hard depth cap (design decision).** An overseer/lead may spawn
  sub-agents; those sub-agents may spawn their own recursively, with NO hard cap. The only
  rails are:
  1. `soft_depth_limit` (config, default 8): reaching this depth emits a
     `delegation_depth_warning` advisory event; spawn still proceeds.
  2. `max_agents` (config, default 64): reaching this count emits a
     `delegation_agent_limit_warning` advisory event; spawn still proceeds.
  3. `budget_meter`: tree-wide token+agent tracking, always current in the artifact.
     Token exhaustion of any ancestor auto-revokes the subtree (unchanged).
  4. `kill()` / `kill_delegation_tree` MCP tool: human kill-switch — revokes the whole
     tree from root, sets `killed=True`, rejects all subsequent spawns.
- `depth_cap` is emitted as the legacy constant `3` for backward compat with artifact
  readers (e.g. `agent_inspector_view.h`). New readers should use `soft_depth_limit`.
- `budget_meter` is the **human-visible meter**: tokens spent/remaining + agents
  spawned/remaining. Updated on every `accrue()` call and every new delegation node.
- Budgets inherit: child spend accrues to every ancestor; a node whose ancestor's
  remaining budget hits zero is auto-revoked by the harness (unchanged from 1.5).
- Soft limits are in `routing.yaml` under the `delegation:` block:
  `soft_depth_limit: 8` and `max_agents: 64` (user-configurable, not hardcoded).
- The tree lives in the ledger (`artifacts` kind `delegation`) and every transition
  and advisory warning is a ledger event.

New ledger event kinds (1.6, additive):
- `delegation_depth_warning`: emitted when a spawn reaches `depth >= soft_depth_limit`.
- `delegation_agent_limit_warning`: emitted when `agents_spawned >= max_agents`.
- `delegation_killed`: emitted when `kill()` is called; includes final `budget_meter`
  snapshot.

## §13 Build graph (`aicad.buildgraph/1`)

```json
{
  "schema": "aicad.buildgraph/1",
  "mission": "",
  "nodes": [{
    "id": "bg_...", "kind": "part|feature|op|mate|verify|checkpoint",
    "label": "bore F7 in bracket",
    "doc_id": "", "refs": {"features": [], "parts": []},
    "requires": ["bg_datum1", "bg_boss3"],
    "why": "hole pierces boss3 - must exist first",
    "status": "pending|building|done|failed|skipped",
    "assigned": null
  }]
}
```

Rules: graph is validated deterministically before execution (cycle check, dangling
`requires`, refs resolve against the plan); execution is topological; `verify` nodes
join fan-outs; `checkpoint` nodes emit PLM history entries (§15). Standard prerequisite
rules (datum→sketch, boss→blend/pierce, parts→mates) live in the planner as data, and
the validator's findings are advisory items.

## §14 Model routing & sources (`aicad.routing/1`) — `services/agent/routing.yaml`

```yaml
schema: aicad.routing/1
sources:                      # user-configured in GUI settings
  - id: anthropic_api        # provider seam (no consumer-OAuth reuse)
    kind: anthropic|openai|openrouter|lmstudio|llamacpp|ollama
    models: [{id: claude-sonnet-4-6, tier: frontier}, ...]
assignments:                  # role -> ordered preference list, first available wins
  overseer:      [strongest]  # 'strongest'/'cheapest' resolve against sources at runtime
  orchestrator:  [strongest]
  reviewer:      [strong]
  fixer:         [strong]
  sketcher:      [cheap_local, strong]   # fall through if local source offline
  default:       [cheap_local, strong]
```

Rules: routing changes are ledger events (troubleshooting: "which model did that").
GUI settings edit this file through a validated form, never free text. If only one
source exists, all roles collapse onto it and delegation still works (same model,
scoped contexts).

ALL role assignments are user-owned (design decision, 2026-07-04): every role listed in
`assignments` — including `overseer` — appears as an editable row in the settings
GUI. The values shipped in routing.yaml (overseer→strongest, sketcher→cheap_local,
…) are INITIAL DEFAULTS only; no role→model binding may be hardcoded anywhere
outside routing.yaml, and code must not special-case any role's model choice. The
settings UI shows the resolved model per role (what `strongest` currently resolves
to, given configured sources) so the user sees the actual consequence, and warns —
never blocks — when a choice is likely to hurt (e.g. overseer on a 4B local model:
"audit quality gates the whole mission").

## §15 PLM checkpoint link (`aicad.plmlink/1`)

```json
{
  "schema": "aicad.plmlink/1",
  "checkpoint_ref": "<workstream-R history entry id>",
  "run_id": "", "buildgraph_node": "bg_...",
  "trigger": "stage_exit|plan_ratified|part_done|mission_done|change_request",
  "message": "plain-language, AI-drafted per PLM tier",
  "ledger_events": [/* seqs folded into the history annotation */],
  "item_id": null,
  "item_artifacts": {"<PLM item id>": "<buildgraph participant node>"},
  "change_request": {"audit_id": "aud_...", "modifies_checkpoint": null}
}
```

Rules: agents never touch git directly — only PLM nouns (Project/History/
Checkpoint). Checkpoint messages are drafted from ledger events (provenance rule) and
carry the dimension rationales verbatim. When the R vault is unavailable the links
queue in the ledger and flush on availability (no lost annotations).

Additive attribution: a design-pipeline boundary MAY carry
`item_artifacts`, mapping every changed PLM item to the buildgraph node that
produced it. The vault writes that node into the item's own §3.4 AI metadata, so
one multi-part checkpoint retains participant attribution in every per-item
timeline. Queued links MUST persist both `item_id` and `item_artifacts` unchanged.

## §16 Deliverable manifest (`aicad.deliverables/1`)

Produced at intake by the intake_analyst role + deterministic validation; the
mission's definition of done. Lives in the ledger (`artifacts` kind `manifest`).

```json
{
  "schema": "aicad.deliverables/1",
  "request_verbatim": "the user's words, unedited",
  "deliverables": [{
    "id": "DL1",
    "kind": "part|assembly|drawing|cam_program|analysis|report|modification",
    "name": "bearing mount bracket",
    "description": "<=60 words, in the user's terms",
    "acceptance_criteria": [{
      "id": "AC1",
      "text": "bore is a sliding fit on the d20 g6 shaft",
      "check": {"tool": "fit_report|verify_battery|measure_part|cam_simulate|run_fea_static|review_drawing|manual",
                 "params_hint": {}, "pass_rule": "clearance in [0.007,0.041]"}
    }],
    "depends_on": ["DL2"],
    "target_refs": {"doc_id": null, "features": []},
    "status": "pending|specifying|planned|building|verifying|done|blocked"
  }],
  "ambiguities": [{"text": "", "blocking": false, "question_id": null}],
  "assumptions": [{"text": "", "basis": "", "needs_confirm": true}]
}
```

Rules (validator-enforced, deterministic): every deliverable has ≥1 acceptance
criterion; every criterion names an executable `check` tool (kind `manual` requires
`needs_confirm` on an assumption or an ask_user); `modification` deliverables must
resolve `target_refs` against an existing doc before planning; blocking ambiguities
route to the clarifier before Phase 2. Acceptance criteria are the reviewer's
checklist verbatim — verification is running the named checks, not vibes.

## §17 Replan / deviation request (`aicad.replan/1`)

The flexibility mechanism: any role may propose changing the process instead of
retrying it. Emitted as a handoff output or mid-work tool call (`replan_request`).

```json
{
  "schema": "aicad.replan/1",
  "id": "rp_...",
  "from_role": "feature_modeler",
  "scope": "approach|buildgraph|spec|requirement",
  "reason": "loft between these profiles self-intersects at the neck",
  "evidence": {"failure_cards": ["fc_..."], "advisories": [], "attempts": 2},
  "proposal": {
    "summary": "replace loft with revolve + cut; same envelope, simpler topology",
    "buildgraph_changes": [{"op": "add|remove|reorder|reassign", "node": {}, "why": ""}],
    "spec_changes": [], "requirement_changes": []
  },
  "urgency": "opportunistic|blocked"
}
```

Approval routing (harness-enforced): `approach` (how a node gets built, spec
untouched) → orchestrator approves; `buildgraph` (order/structure) → orchestrator +
overseer audit; `spec` → overseer, and user if any acceptance criterion changes;
`requirement` → always user. Approved requests splice via dag_replan/buildgraph
edit and are ledger events; denied requests return with a reason the role can act
on. A role that has emitted a `blocked` replan_request stops working until ruled on
— it never continues down the failed path.

## §18 Organization runtime (`aicad.org/1`)

Departments are instantiated per mission from the deliverable manifest: each
deliverable cluster (a deliverable plus its depends_on satellites, e.g. part +
its drawing + its cam_program) gets a department. The org chart is a ledger
artifact and re-chartable mid-mission via replan scope=org.

```json
{
  "schema": "aicad.org/1",
  "departments": [{
    "id": "dept_bottom_shell",
    "deliverables": ["DL1", "DL4"],
    "manager": {"role": "department_manager", "delegation_node": "dg_..."},
    "members": [{"role": "sketcher", "delegation_node": "dg_..."}],
    "advisors": ["foreman", "overseer"],
    "status": "forming|active|blocked|delivered|dissolved"
  }],
  "channels": [{
    "id": "ch_shell_lid",
    "between": ["dept_bottom_shell", "dept_lid"],
    "created_from": "interface: lid_parting_plane (layout skeleton)",
    "status": "open|closed"
  }],
  "chart_revision": 3
}
```

Channel messages are ledger events:
`{channel, from_dept, kind: info|question|decision|warning|interface_change,
body <=120 words, refs (DIM/DL/AC/node ids), needs_ack}`. Rules: an
`interface_change` message is MANDATORY before any department alters a
dimension another department cites (the derivation tree §21 knows who cites
it); unacked interface_changes block the sender's stage exit, not the send.
Channels are created automatically from manifest dependencies + layout
interfaces; departments may open more (ledger event, why required).

Worker requests (`aicad.request/1`) — the raise-your-hand primitive:

```json
{
  "schema": "aicad.request/1",
  "id": "rq_...",
  "from": "dg_...", "to": "manager|orchestrator|overseer",
  "kind": "context|resource|information|communication|concern|escalation",
  "body": "the bore spec cites DIM7 but the ledger shows it superseded",
  "refs": {},
  "resolution": {"by": null, "action": null, "ts": null},
  "sla": "next_manager_turn"
}
```

Rules: every request gets an explicit resolution (grant, deny-with-reason,
escalate) — silent expiry is forbidden; unresolved `concern` requests older
than one manager turn auto-surface in the overseer's next audit (accountability
without human intervention). Managers report department status upward every N
node completions (structured: done/next/blocked/requests-open) — reports are
ledger events the orchestrator and transparency UI consume.

## §19 Councils (`aicad.council/1`)

A council is a structured, seat-based deliberation — the token-cheap way to get
multi-angle judgment before committing. Not a chat: each seat produces a typed
position, the chair synthesizes, dissents are RECORDED.

```json
{
  "schema": "aicad.council/1",
  "id": "cn_...",
  "kind": "build_strategy|spec_review|failure_review|org_review|custom",
  "topic": "how to build DL1 bottom-shell",
  "seats": [{"persona": "manufacturing", "focus": "setup count, tool access, stock"},
            {"persona": "tolerancing",  "focus": "datums, stackups, critical dims"},
            {"persona": "fitment",      "focus": "interfaces, clearances, assembly order"},
            {"persona": "simplicity",   "focus": "fewest features that satisfy the ACs"}],
  "rounds": 2,
  "inputs": {"ledger_refs": [], "buildgraph": null},
  "positions": [{"seat": "manufacturing", "position": "", "risks": [], "must_haves": []}],
  "synthesis": {"decision": "", "rationale": "", "rejected_alternatives": []},
  "dissents": [{"seat": "", "dissent": "", "trigger_condition": "revisit if wall <2mm"}],
  "chair": "department_manager"
}
```

Rules: the BUILD-STRATEGY council is a Phase 3 exit requirement per part above
trivial complexity (config: feature count threshold); its synthesis feeds the
buildgraph planner and its must_haves become validator checks. Seats run as
ponder delegations (same model, different persona prompt — cheap on local).
Dissents carry trigger_conditions; the foreman watches them and re-raises the
dissent as an advisory if the condition occurs (a recorded minority report with
teeth). round 2 lets seats respond to each other's round-1 positions —
"councils interact" without freeform chat. Council outputs are ledger events.

## §20 Trigger-based context injection (`aicad.trigger/1`)

Fine-grained companion to the stage battery: a data-driven table mapping what
the model is DOING to context it should HAVE. Evaluated by the foreman after
every model turn (output text) and tool call.

```yaml
schema: aicad.trigger/1        # services/agent/advisor/triggers.yaml
triggers:
  - id: thread_mention
    match: {keywords: ["tap", "thread", "M[0-9]+x?", "UNC", "UNF"], tools: [], events: []}
    inject:
      context_pack: threads    # short authored snippet: callout format, class defaults
      prerun_calcs: [{calc: tap_drill, inputs_from: match}]
      exemplar: "M6x1.0 tapped hole: pilot 5.0, engagement >=1.5d in aluminum"
    budget_tokens: 250
    cooldown_turns: 8          # don't re-inject while it's still in context
  - id: bearing_seat
    match: {keywords: ["bearing", "seat", "bore.*press"], tools: [part_search]}
    inject: {context_pack: bearing_fits, prerun_calcs: [{calc: fit_calc}]}
    budget_tokens: 300
    cooldown_turns: 8
```

Rules: injections render inside the [ADVISORY] block as severity=info CONTEXT
items and count against the advisory slot budget (drop-first when over). Regex
keywords match model OUTPUT and user text routed to the role; `inputs_from:
match` lets prerun calcs pull the matched value (M6x1.0 → tap_drill("M6x1.0")).
Cooldown prevents nagging. The table is data — departments/users can add
triggers; every firing is a ledger event (which trigger, what matched, tokens).

## §21 Dimension derivation tree & invalidation (extends `aicad.ledger/1`)

Additive fields on ledger dimension entries:

```json
{
  "id": "DIM9", "name": "boss_od", "value": 28.0,
  "derived_from": ["DIM7", "calc:press_fit:pf_123", "D14"],
  "derivation": "DIM7 (bore 20 H7) + 2*min_wall(4.0, from wall_thickness calc)",
  "linked": {"eq_id": "eq_boss_od", "expr": "bore_d + 2*boss_wall"},
  "cited_by_departments": ["dept_lid"]
}
```

Rules:
- `derived_from` forms a DAG over dimensions, decisions, calc results, and
  assumptions. Writing a derived dimension WITHOUT derived_from refs is a
  provenance violation (validator warns; overseer audit trigger on repeat).
- LINKED DIMENSIONS FIRST: when a derivation is expressible as an equation, the
  helper binds it into the engine (`eq_set`) so geometry re-drives on change;
  `linked.eq_id` records the binding. Roles' prompts already push this; the
  helper makes it one call (`link_dimension(DIM9)`).
- Invalidation: `mark_faulty(ref, why)` walks dependents through the derivation
  DAG + buildgraph refs + AC checks and emits an impact report
  (`aicad.impact/1`): `{faulty, affected_dims[], affected_nodes[],
  affected_checks[], recommended: [{target, action: relink|recompute|rebuild|
  reverify, why}]}`. The report routes as a change plan (§11) — assessed, not
  auto-executed; linked dimensions typically downgrade "rebuild" to
  "recompute + reverify" (the equation already propagated the value).
- The impact walker is deterministic and unit-tested; no LLM in the walk.

## §22 Design cognition ladder (`aicad.ladder/1`)

The guided multi-step thinking chain for turning a spec into a buildable plan.
Each rung runs in a FRESH context (only the previous rung's typed artifact + a
scoped ledger slice — never the full conversation), so a small model reasons
about exactly ONE abstraction level at a time. Deterministic validators sit
between rungs; councils critique at the marked checkpoints. Rung templates are
authored playbooks (services/agent/playbooks/ladder_*.yaml — canonical, loaded
verbatim); the design_thinker role executes them.

```
spec ─A→ function_map ─B→ geometry_plan ─C→ linkage_map ─D→ build_pathway ─compile→ buildgraph ─E→ execution
         (what it DOES,    (what SHAPES     (how geometry   (what ORDER, what                     (Phase 4)
          what FEATURES)    realize each     REFERENCES      orientation, which
                            feature)         geometry)       plane each sketch on)
```

Rung artifacts (all ledger events, all replayable):

A. `function_map`: {functions: [{id: FN1, statement: "verb-noun, e.g. transmit
   torque from shaft to arm", driven_by: [AC ids]}], features: [{id: FT1,
   name, purpose, serves: [FN ids], interfaces_with: [], criticality:
   critical|important|nice}], omissions_check: [{class, considered: yes|no,
   why}]} — omissions_check MUST cover the standard classes: mounting,
   location/alignment, retention, sealing, venting, clearance/service access,
   strength/stiffening, manufacturability aids (draft/fillets/chamfers). A
   feature manifest without the checklist is invalid: the checklist is the
   forcing function for "think about ALL the major features."

B. `geometry_plan` (fresh context: function_map + envelope/interfaces only):
   {elements: [{id: G1, kind: primitive|profile_op, op: extrude|revolve|loft|
   sweep|hole|pattern|shell|fillet..., description, params: [{name, basis:
   "derived: bore_d + 2*wall" | "spec: DIM7" | "free"}], realizes: [FT ids],
   orientation_hint}], shared: [G ids serving multiple features], conflicts:
   [{pair: [G,G], overlap_concern, resolution_needed}]}. Validator: every FT
   covered by ≥1 element; every element realizes ≥1 FT (orphan geometry is a
   finding); conflicts must carry resolutions before rung D.

C. `linkage_map` (fresh context: geometry_plan only): {datums: [{id, kind:
   plane|axis|point, defined_by, why}], links: [{from: "G3.width", to:
   "G1.bore_d", relation: "= bore_d + 2*wall", why}] (these become §21 linked
   dimensions), constraint_plans: per profile the geometric-then-driving
   schedule, driving_dims: [{name, owner, derived_from}], reference_tree:
   parent/child geometry refs}. Validator: no circular links; every driving
   dim has owner + basis; every datum used ≥1 time; link expressions parse.

D. `build_pathway` (fresh context: geometry_plan + linkage_map): {steps:
   [{n, op, elements: [G ids], sketch_on: datum/face ref, orientation, why:
   "why this step here", risks: []}], orientation_rationale, alternates_
   considered: [{summary, rejected_because}]}. Validator: every element built
   exactly once; datums/faces exist before use (walk the sequence); compiles
   losslessly into aicad.buildgraph/1 (steps → nodes, why → edge whys).

E. Execution: existing Phase 4 machinery. Workers receive the buildgraph node
   plus the SLICE of the ladder artifacts their node cites — never the whole
   ladder.

Rules: rung outputs are grammar-constrained on local tiers; a validator bounce
returns to the SAME rung with the finding (max 2 bounces, then council or
escalate); the build-strategy council (§19) critiques rung D output before
compilation — seats receive geometry_plan + build_pathway, not the chat. The
ladder is mandatory at deliberation=full (local), optional at minimal
(frontier models may emit rung D directly — validators still run). Rungs may
run as ponder delegations of one design_thinker or as separate delegations —
identical contexts either way (fresh-context rule is what matters).

## §23 Harness effort presets & user overrides (`aicad.effort/1`) — `services/agent/effort_presets.yaml`

1.5 additive. §8 tier profiles remain the SHIPPED defaults; this section
adds USER OWNERSHIP of the dial. Same principle as the §14 addendum: shipped
values are initial defaults only — nothing hardcoded outside user-editable
config.

- Presets: `effort_presets.yaml` (`schema: aicad.effort/1`) defines the four
  Simple-mode levels `eco | standard | deliberate | maximum`, each a set of knob
  overrides (`overrides.all`, then `overrides.<tier>`) applied over a baseline
  DERIVED at load time from tiers.yaml and the shipped code constants (ladder
  MAX_BOUNCES, delegation soft_depth_limit/max_agents (1.6; was hard DEPTH_CAP),
  councils seat budgets/threshold, failure attempt budgets, overseer trigger points,
  trigger packs). `standard` is the
  identity preset — the §8 1.3 INVERTED DIAL survives untouched.
- Knob set (grouped for the GUI): Deliberation (`deliberation`, `ladder`,
  `ladder_max_bounces`, `council`, `council_seats`, `council_feature_threshold`,
  `ponder_round1_tokens`, `ponder_round2_tokens`), Parallelization
  (`delegation_depth`, `worker_fan_out` 0=uncapped, `multi_doc_parallel`),
  Safety nets (`preflight`, `overseer_cadence: every_trigger|stage_exit_only|
  manual_only`, `failure_max_attempts` per class), Context
  (`history_budget_tokens`, `trigger_injection`, `trigger_cooldown_scale`,
  `context_pack_verbosity`).
- User overrides: config.json `ai_effort: {level, mode: simple|advanced,
  overrides: {<tier>: {<knob>: value}}}`. Resolution precedence (ONE path,
  `services/agent/effort.resolve_effort`, re-exported by tiers.py; mission
  pipeline, design gym and GUI all use it): shipped < preset.all < preset.<tier>
  < user override. Every resolved knob carries provenance
  (`shipped | preset:<level> | override`).
- Freedom rule: absurd combinations WARN with the reason (mirrors §14
  warn-never-block) and are stored verbatim — never blocked, never silently
  clamped. Note: §12 DEPTH_CAP is NO LONGER a runtime hard cap (since 1.6); the
  effective rail is soft_depth_limit (advisory) + max_agents (advisory) + kill().
- Ledger: mission start appends one `kind: effort_profile` event with the fully
  resolved values + provenance for every tier (replayable).

## Deliverable kinds: work_instruction, weldment_traveler (2026-08-26, additive)

§16's `kind` enum gains two values (append-only; `aicad.deliverables/1` records
with the original seven kinds remain valid):
- `work_instruction` — structured machining/fabrication work instruction
  (routing, setups, fixturing, in-process inspection, signoff), built by
  `services/deliverables/templates/work_instruction.py` via
  `make_work_instruction`.
- `weldment_traveler` — weldment cut list / joint list / WPS / inspection
  traveler, built by `services/deliverables/templates/weldment_traveler.py`
  via `make_weldment_traveler`.
Validators (`services/agent/pipeline/manifest.py::VALID_KINDS`) accept both;
mission compilation routes them like `report`-class deliverables (buildable by
a role granted the corresponding make_* tool). Consumers MUST tolerate the new
kinds per the existing conformance rules.

## Cached native preflight isolation (2026-09-12, supersedes §10 clone mechanism)

Preflight uses `doc_dry_run_ops` on unregistered native scratch state. It copies
the live compile-cache index and reuses immutable cached results, instead of
serializing the live feature history into another registered document and
replaying every feature. Successful and failed predictions must leave the live
tree, revision, geometry, undo history and compile-cache state unchanged. Scratch
work must not publish document events or invalidate dependent live documents.

The native response retains `ok`/`error` and adds `source_revision` plus the actual
candidate `rebuild` diagnostics when compilation is reached. The advisor keeps
feature and document failures, warns when diagnostics are absent, and warns if
the source revision changes across its read-only checks. Reference, parameter
and downstream checks inspect live state; they do not claim post-edit geometry.
An ordinary fail verdict remains advisory; engine connection loss propagates
without replay or mutation.

This removes unconditional cold replay, not the cost of rebuilding genuinely
edited geometry, an invalidated suffix, an uncached source or an assembly solve.
There is no guaranteed two-second wall-clock deadline. Feature-count admission
remains an explicit `skipped` outcome, not a passing prediction.

## Exact planar belt routing (2026-09-13, additive calculator)

`research_eng_calc(calc="belt_route", params={"pulleys":[...]})` uses the
existing calculator registry and `aicad.calc/1` envelope. Each ordered row is
`{"center_mm":[x,y],"pitch_radius_mm":r,"wrap":"ccw"|"cw"}`. The last pulley
connects back to the first. Centers must be measured in one common oriented
plane; radii describe the belt's neutral/pitch line, not uncorrected outside
diameters. Opposite wrap senses give internal common tangents for backside
idlers. Wrap sense describes belt travel around a circle, not shaft speed.

The answer contains `length_mm`, separate straight/arc totals, every tangent
endpoint and span length, and each pulley’s entry/exit, wrap angle and arc
length. It uses exact common-tangent and directed-arc geometry, not a
linearized tensioner gain. Impossible tangents, obstructed routes and
crossed/self-touching planar loops refuse without a length answer. The
reported numerical tolerance is floating-point geometry tolerance, not an
engineering clearance allowance.

This is a neutral-line geometry calculator, not a physical clash, preload,
compliance, belt-strength or supplier-tolerance qualification. A catalog
center-distance tolerance is not implicitly treated as a pitch-length
tolerance. Callers must source the neutral-line offset and tolerance
measurement conditions, evaluate actual mechanism positions, and bind
native measurements through the existing `prefilled` provenance field.

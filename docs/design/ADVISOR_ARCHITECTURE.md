# The Advisory Layer

Frozen schemas live in [`contracts/advisor.md`](../../contracts/advisor.md).
The advisory engine is called the *foreman* in code (`services/agent/advisor/`).

This document is design intent; component sections are not an as-built spec.
Online model execution for the roles it routes now runs inside the official
OMP RPC runtime. Chudware keeps advisories, roles and delegation
governance; see [OMP integration](../dev/omp-integration.md).

## 0. Thesis

Today's loop is **generate → check → fail → guess again**. A checker that only says
"interference detected" forces the model to search dimension-space by trial and error —
tolerable for Opus, fatal for a 24k-context 4B model.

The advisory layer inverts this: **deterministic helpers compute, models decide.** Every check
failure arrives paired with solver-computed candidate fixes and the exact numbers needed
(fit tables, equations, clearances). Every process stage arrives with the calculators,
data tables, and playbook steps relevant to *that* stage, pre-filled from live model
state. The model's job shrinks from "derive mechanical engineering from scratch inside
8k tokens" to "choose between well-posed options and supply intent."

Design rules:

1. **Advise, never railroad.** Advisories are injected context, not control flow. A
   frontier model may ignore them; a small model leans on them. The same machinery
   serves both — only the *dial* (enforcement, verbosity, structure) changes per tier.
2. **Numbers come from the kernel or a table, never from the LLM.** Every number an
   advisory carries has a recorded provenance.
3. **State lives in the ledger, not the chat.** Chat history is a cache; the design
   ledger is the source of truth. Any role can be cold-started from ledger + describers
   inside a 24k window.
4. **Every failure is a card, every card has candidate fixes, every retry is bounded.**
   After N attempts the loop escalates (ask_user or dag_replan) — never endless.
5. **Freedom rule.** Helpers, checkers, and oversight *inform,
   audit, and escalate — they never veto a model's action*. The only hard gates are
   the existing human-gated ones (kernel-fact dispute waivers §9.3, autonomy dial,
   destructive/external actions). Complexity goes into help, not restrictions; the
   tier profile decides how much help is injected, per the model in use.
6. **Everything injected is inspectable.** Every block a model received, every check
   that ran, every delegation and audit verdict is an event on the ledger stream and
   visible in the transparency UI (§2.15) — troubleshooting the helpers is a
   first-class requirement.

## 1. What the layer builds on (reuse map — do not rebuild)

| Existing | Where | Role in the advisory layer |
|---|---|---|
| Agent loop | OMP session host (`services/agent/omp_host.py`) driving `omp --mode rpc` | Host for advisory injection (seam: message assembly before the provider call) |
| DAG orchestrator, autonomy dial, `dag_replan` | `services/dag/` | Host for role cards per node; escalation target |
| Tool registry w/ risk classes + allow-lists | `services/mcp/tool_registry.py` | Enforcement of role-card tool scopes (already supports `CHUDWARE_TOOLS_ALLOW` / node `tools:`) |
| GSL describers (`describe_part/sketch/scene/...`) | `services/mcp/gsl_tools.py` | State→text for ledger snapshots and role handoffs |
| Solver analysis: DOF, redundant/conflicting tags, per-entity under/full/over | `core/solver/nsolve.h` (`System::analyze`), `core/sketch/` | Raw signal for the sketch completion suite |
| Closed-region detection | `session.h` `doc_sketch_regions()` | Profile-closure + auto-coincident targeting |
| Interference/overlap/mass/section/symmetry evals | `session.h` `doc_eval_*` | Raw signal for advisories & failure cards |
| `regen_diagnose` / `regen_resolve` | `services/mcp/regen_tools.py` | First failure-card producer (rebuild class) |
| `verify_battery`, `design_review` | verify/design_review tools | Stage-exit gates; advisory engine wraps their output |
| Research tools (`web_search`, `material_lookup`, `thread_spec`, `eng_calc`, `unit_convert`) | `services/mcp/` research tools | Calculator registry extends `eng_calc`; web/data access for advisors |
| Interaction contract (`ask_user`, answer ledger) | `services/agent/interaction.py` | Escalation transport; critical-dimension confirmation |
| Materials registry | `services/sim/materials.json` | Inputs to DFM/tolerance/sim advisors |

Confirmed gaps this layer fills: auto-constraint inference, tolerance/fit tables (ISO
286), feeds & speeds data, stage-aware advisory injection, role/playbook definitions,
design ledger, failure-card unification, sim pre/post advisors.

## 2. Components

### 2.1 Design Ledger

A persistent, structured record of the mission — the anti-context-loss mechanism.
Sections (schema frozen in contract §2): mission brief, requirements (linked to
`req_*`), decisions (with rationale + who/when), dimension table (value, driver
["bearing seat", "clearance to lid"], tolerance, status: proposed|confirmed|derived),
open questions, stage log, artifact index (doc_ids, renders, study ids).

- Stored at `<cache_root>/runs/<run_id>/ledger.json`; append-only event log +
  materialized current-state view.
- `ledger_render(budget_tokens)` produces the compact text block every role receives —
  a 24k model gets ~800 tokens, Opus gets the full thing. Rendering is deterministic
  and prioritized (brief > active-stage dims > decisions > rest).
- MCP tools: `ledger_get`, `ledger_note` (decision/dimension/question upserts). The
  advisory engine and roles write to it; humans see it in mission control.

### 2.2 Stage model + Advisory Engine

The process is a fixed vocabulary of stages (contract §1):
`intake → spec → layout → sketch → feature → part_verify → assembly → sim → cam → deliver`
(plus `fix` as a re-entrant sub-stage). The DAG orchestrator already models missions;
stages annotate *where inside a node's work* the agent currently is. Stage is inferred
deterministically from the last tool calls (e.g. `add_features` w/ sketch ops ⇒ sketch;
`run_fea_static` ⇒ sim) with an explicit `stage_set` override tool.

After every tool call (agent-loop seam) or node step (DAG seam), the engine:

1. Runs the **stage's check battery** — cheap, deterministic, read-only (e.g. sketch
   stage: `analyze()` DOF + region closure; feature stage: `doc_eval_feature_overlap`;
   assembly: `doc_eval_interference` with fit-aware thresholds).
2. Converts findings into an **advisory block** (contract §3): severity, finding,
   *computed candidate fixes with numbers*, and the named helper tools for this stage.
3. Injects the block into the next model turn (system-adjacent, clearly delimited,
   token-budgeted per tier profile).

Example (the interference case): instead of "interference detected", the block reads:
`bodies bracket/shaft interfere 340 mm³ near [12,0,5]; bore Ø19.80 vs shaft Ø20.00; for
sliding fit on Ø20 use H7/g6 → bore 20.000–20.021; candidate fix: set hole d=20.02
(feature F7 'bore'); tool: fit_calc for other fit classes.`

The engine is deterministic Python — no LLM in the hot path. An optional "senior
advisor" LLM pass (frontier model, off by default) can rank/expand advisories for
ambiguous situations.

### 2.3 Role cards

Predefined sub-agent roles, data-driven (`services/agent/roles/*.yaml`), consumed by
both the DAG node runner and standalone loop. Contract §4 fixes the schema:
`id, goal, system_prompt, tool_allowlist (registry families/names), stage_scope,
ledger_sections (read/write), handoff_schema (structured JSON the role must emit),
context_recipe (which describers at what budget), escalation_policy`.

v1 roles (13, prompts authored in `services/agent/roles/*.yaml` —
canonical, loaded verbatim): `intake_analyst`, `clarifier`, `spec_writer`,
`orchestrator`, `layout_engineer`, `sketcher`, `feature_modeler`,
`assembly_fit_engineer`, `sim_engineer`, `cam_programmer`, `reviewer`, `fixer`,
`overseer`. Each system prompt is *rich*: identity, quality bar, method, worked
micro-examples, exact output format, a shared UNIVERSAL RULES block (numbers never
invented; advisories are ground truth; ledger over chat; honest handoffs; the
two-failures-then-replan anti-grind rule), and role-specific judgment sections.
How they compose into a mission is specified in `docs/design/COPILOT_MISSION_PIPELINE.md`
(phases, exit gates, context recipes, replan routing).

Handoffs are structured JSON, validated against the card's `handoff_schema`, written
into the ledger — the next role never needs the previous role's chat.

### 2.4 Playbooks (removed)

The per-operation playbook files (`services/agent/playbooks/*.yaml`) and their loader
were removed: nothing at runtime consumed them. Step-by-step guidance now lives in
the role system prompts and capability scaffolds (`services/agent/scaffolds.py`).

### 2.5 Calculator & data registry

Extends `research_eng_calc` into a keyed registry where the advisory engine can
**pre-fill inputs from live geometry** (bore diameters, wall thickness, spans measured
via `doc_eval_measure`). Every result uses the research-tool envelope: `{answer, units,
equation, inputs, source, confidence}` — the equation is shown so the model (and human)
can audit.

- Fit data: ISO 286 fit/tolerance tables (H7/g6 & friends, IT grades), standard fit
  recommendations by function (sliding/press/location), `fit_calc(nominal, fit)` and
  `suggest_dimension_scheme(sketch, process, material)`.
- Calculators: bolted joint (preload/torque), thread engagement length, press-fit
  interference stress, beam bending/deflection (also the sim cross-check), min wall
  thickness by process/material, snap-fit, shaft-hub. Auto-prefill layer.
- Machining data: feeds & speeds by material/tool/operation (seed table + citation fields),
  drill/tap charts; wired into the CAM fast-path and `cam_cost_estimate`.

### 2.6 Sketch completion suite

- **Auto-coincident**: a deterministic pass (`sketch_auto_constrain` RPC) that finds
  endpoint pairs within tolerance, proposes/applies `coincident` constraints, and
  reports which chains now close (uses `doc_sketch_regions` before/after). Applied
  automatically on sketch edits (config-gated), so touching endpoints are *already*
  coincident before the model ever sees a "profile not closed" error.
- **Constraint advisor**: wraps `System::analyze` — narrates DOF, names the free
  variables in plain words ("circle C1 radius; line L4 end x/y"), flags
  redundant/conflicting constraint tags with removal suggestions, and proposes an
  ordered constraint schedule (geometric → driving dims) to reach fully-constrained.
- **`sketch_health`**: single MCP call returning the narrated status block (closure,
  DOF, per-entity status, advisor schedule) — the sketch-stage advisory source.
- **Critical dimensions**: `suggest_dimension_scheme` chooses *which* dims to
  drive based on function tags + manufacturing method (e.g. bolt-pattern
  center-to-center over edge-to-hole; process-appropriate default tolerances from the
  DFM profile), and marks dims needing user confirmation via `ask_user`.

### 2.7 Failure cards + fixer loop

One schema (contract §5) for every failure class: rebuild error, solver
conflict/divergence, interference, overlap, DFM violation, sim failure/margin miss,
CAM gouge. Card = `{class, what, where (stable ids), why (deterministic diagnosis when
available), candidate_fixes[{action, params, predicted_effect, confidence}], helper
tools, attempt_count}`. `regen_diagnose` already produces the rebuild class — failure
cards unify the rest and add the **bounded retry policy**: per-class attempt budget;
on exhaustion escalate to `ask_user` (with evidence) or `dag_replan`, never loop.
The `fixer` role consumes cards; its prompt forbids shotgun edits ("change only what
the card names; one candidate per attempt; report predicted vs measured effect").

### 2.8 Simulation verification loop

- **Pre-sim advisor**: BC lint (unconstrained bodies, load on suppressed face),
  mesh-size suggestion from feature size, and an **order-of-magnitude hand-calc
  cross-check** using the registry calculators (beam/plate approximations) — the run is
  annotated "expect ~X MPa" so garbage results are caught, not trusted.
- **Post-sim narrator**: extends `describe_results` with margin policy per material
  (yield/endurance from registry), and geometry-linked suggestions ("max stress at
  fillet f_3 r=1; r=3 typically drops Kt 2.4→1.6 — candidate fix on feature F9").
- Sim advisories feed failure cards when margins miss.

### 2.9 Model tier profiles

`services/agent/tiers.yaml`: per-tier dials — context budget split (system/ledger/
state/advisory/tools/history token allocations), generation headroom, tool-schema
trim (role allowlists trim the 150-tool surface to <20 per role — essential for small
models) and the capability profile letter (A scaffold-hard | B soft gates | C freedom).
Ships with profiles:
`local-24k`, `local-48k`, `mid`, `frontier`. Bench harness runs the same mission
across tiers.

### 2.10 Tool preflight — plans checked before they touch the doc

Every mutating tool family gains a `preflight` capability: the planned ops are
dry-run against a **shadow clone** of the document (serialize → clone → apply →
capture `feature_status`/solver status → discard). Preflight validates: references
resolve (faces/edges/features by stable id), parameters sane (positive dims, tool
reach, fillet ≤ adjacent wall), predicted rebuild outcome, and downstream impact
(which later features reference what's being edited). Results are `aicad.advisory/1`
items (`check: preflight`). Per the freedom rule a failed preflight does NOT block —
but "mutation applied after failed preflight, without a dispute" is recorded and is
an automatic audit trigger (§2.11). External MCP clients get it as an explicit
`preflight_ops` tool; the internal loop runs it automatically.

### 2.11 Overseer & plan audit

A supervisory role bound to the **highest-capability model available** in the user's
configured sources (§2.12 routing). It does NOT review every turn (cost); it fires at
defined checkpoints: plan ratification, stage-exit gates, failure-card escalations,
post-failed-preflight mutations, and **every user change-request**. It consumes the
ledger, the active plan/build graph, role handoffs, advisory history, and preflight
results — and emits `aicad.audit/1` verdicts: finding class (`plan_gap | wrong_order |
spec_drift | missed_requirement | bad_fix | cot_error`), evidence refs, required
action. Verdicts are injected to the working role as advisories; repeated failures on
the same finding escalate to the human via ask_user.

**Change-request routing** is the overseer's second job and the fix for "user asks
for a change and the agent flails": the request is mapped onto the ledger + build
graph (which requirements, dimensions, features, and parts it touches), turned into a
scoped change plan (with preflight), and dispatched to the right role(s) — so a "make
the bore fit the new 25 mm shaft" edit touches exactly the bore feature, its ledger
dimension, its fit calc, and re-runs only affected checks.

### 2.12 Hierarchical delegation & model routing

Any agent may request a sub-agent (`role`, task brief, token/attempt budget). Depth-1
requests from the orchestrator auto-approve; **deeper requests require approval from
the mission overseer** (policy configurable: auto | overseer | human). The delegation
tree — who spawned whom, why, with what
budget and model — is a ledger artifact rendered live in the transparency UI.

Model routing: `services/agent/routing.yaml` maps role × tier → provider/model from
the user's configured sources, editable in GUI settings (§2.15). Defaults: overseer →
strongest source; orchestrator/reviewer → strong; sketcher/feature/cam roles →
cheap/local; fixer → strong (it touches broken states). Budgets inherit down the tree
(a sub-agent's spend counts against its parent's grant); depth cap default 3.

### 2.13 Build-order DAG

A deterministic planner that turns a build plan into `aicad.buildgraph/1`: nodes are
parts, features, and ops with **declared prerequisites** (datum before dependent
sketch; boss before the fillet that blends it; holes after the boss they pierce;
parts before their mates; verify joins after fan-outs). Validation catches missing
prerequisites and dangling references *before anything is built* — the "don't miss
anything on complex components" mechanism. The orchestrator executes in topological
order, marks node status on the graph (mission-control visible), and for assemblies
fans part nodes out to parallel sub-agents (§2.14), joining at mate/verify nodes.
This is the fine-grained complement to `services/dag` missions: mission DAG = which
jobs; build graph = in what order inside a modeling job. `dag_plan` learns to emit
build-graph nodes for modeling missions.

### 2.14 Multi-document agent workspace

Parallel part building can distribute work across document features and assembly
components. Edits to the same document are applied sequentially.

### 2.15 Transparency & control UI

Two GUI surfaces (`host/src/ui/`, per the pinned-glass-column design language):

- **Inspector panel**: per-turn view of exactly what each agent received (ledger
  render, advisory block, playbook step — verbatim), tool calls with preflight
  results, delegation tree with approvals/budgets, audit verdicts, failure cards with
  attempt history. Driven entirely off the ledger event stream, so any session is
  replayable offline — this is the troubleshooting window for the helper layer itself.
- **Settings**: model sources & per-role/tier routing preferences (the routing.yaml
  editor), delegation approval policy, advisory verbosity, auto-coincident and
  preflight toggles. Approval prompts (delegation, waivers) surface through the
  existing interaction contract transports.

### 2.16 PLM / version-control integration

Integration with [PLM version control](PLM_VERSION_CONTROL_PLAN.md), taken as a design
input:

- **Milestone checkpoints**: stage-exit passes, audit-passed plans, and per-part
  completions in an assembly fan-out create history entries in the hidden git vault
  (snapshot-based) — agents produce the same Project/History/Checkpoint
  nouns humans do, never raw git.
- **Plain-language annotations**: ledger decision/dimension events attach to history
  entries ("bore set to 20.02 for H7/g6 sliding fit on shaft S1 — advisory adv_…"),
  which is exactly the AI-drafted history message tier the PLM design already planned.
- **Change-requests reference checkpoints**: the overseer's change plans record which
  checkpoint they modify, so "undo that change" and "what did the agent change for
  this request" are answerable from the PLM timeline.
- Feature-flagged against vault availability; codes to the
  [`contracts/plm.md`](../../contracts/plm.md) contract.

### 2.17 Organization layer (contract §20–§21)

The former department/channel/worker-request runtime, optional ORGANIZE phase,
and seated COUNCILS (contract §18–§19) were removed. What stays: trigger-based context
injection (keyword/tool/event → context packs + pre-run calcs); and the
dimension DERIVATION TREE with linked-dimension eq_set binding and the
deterministic mark_faulty impact walker. Deliberation dial is inverted: local
tiers get full machinery (tokens free), frontier minimal.

## 3. Context budget (local-24k reference layout)

| Slot | Tokens |
|---|---|
| Role system prompt (incl. inline playbook) | 2,500 |
| Ledger render | 800 |
| Stage state (describers, scoped) | 1,500 |
| Advisory block | 600 |
| Tool schemas (role-scoped, ~12 tools) | 2,500 |
| Working history (this role, compacted) | 8,000 |
| Generation headroom | 8,000 |
| **Total** | **~24k** |

The invariant: any role turn is reconstructible from ledger + describers; history is
disposable. This is what lets ten roles collaborate on one mission without any of them
holding the mission in-context.

## 4. Component dependency map

```
design ledger ───┬─→ advisory engine ─┬─→ failure cards/fixer ─┐
                 └─→ roles+playbooks ─┼─→ tier profiles        ├─→ end-to-end bench
sketch suite ─────────────────────────┤                        │
fit/tolerance data ─→ calculators ─→ sim advisor ──────────────┘
feeds/speeds data ─────────────────────────────────────────────┘ (via CAM path)

preflight (no deps) ──────────┬─→ overseer/audit ─→ delegation+routing
build-order DAG (no deps) ────┤    (also ← ledger, advisory engine)  (also ← roles)
ledger + advisory engine ─────┘
advisory engine ─→ transparency UI + settings
roles ─→ multi-doc workspace
ledger ─→ PLM integration (← contracts/plm.md)
advisory engine + roles + build-order DAG ─→ mission pipeline (COPILOT_MISSION_PIPELINE.md)
```

## 5. Design bets

1. **Fix-candidates-first checking.** Nobody ships checkers that answer "so what do I
   change." Pairing every failure with kernel-computed candidate fixes is the single
   biggest iteration-count reducer, and it benefits humans in the GUI too (surface the
   same cards in the app).
2. **The ledger as a product feature.** "Why is this hole Ø20.02?" → the ledger has
   the decision, the fit class, and who confirmed it. That's design intent capture the
   big CAD vendors bolt on badly; the agent architecture yields it directly, and it
   feeds PLM timelines and training data.
3. **Grammar-constrained handoffs make 4B models composable.** Role → role JSON under
   GBNF means a 4B model's sloppiness can't corrupt the pipeline; correctness reduces
   to per-role competence, which playbooks + advisories directly boost.
4. **Advisories as training signal.** Every (advisory shown → model action → check
   outcome) triple is a labeled example for model training — a next-action value
   model gets its dataset from normal operation.
5. **Same advisory bus for the human GUI.** The advisory engine's blocks are
   render-able as in-app hints (the existing overlay/PropertyManager). One engine,
   two audiences.


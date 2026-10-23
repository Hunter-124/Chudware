# The Advisory Layer

Status: adopted design record (2026-07-04, updated 2026-10-04). Frozen schemas live in
[`contracts/advisor.md`](../../contracts/advisor.md). The advisory engine is called the
*foreman* in code (`services/agent/advisor/`). The components below are implemented; the
end-to-end benchmark (Appendix A) and the MCP-surface catch-up for the newest components
remain open.

This document is design intent; component sections are not an as-built spec. Since
2026-08-31, online model execution for the roles it routes runs inside the embedded
Chudcode RPC runtime. Chudware keeps advisories, roles, councils and delegation
governance; see [Chudcode integration](../dev/chudcode-integration.md).

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
| Agent loop (plan→code→render→verify, 4-level compaction) | `core/agent/run.cpp` (the loop behind the `agent_run_v2`/`agent_mission_v1` RPCs dispatched in `core/api/libchudware.cpp`; it replaced the earlier Python agent loop) | Host for advisory injection (seam: message assembly before `provider.complete_ex`) |
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

Handoffs are structured JSON (grammar-constrained for local models), written into the
ledger — the next role never needs the previous role's chat.

### 2.4 Playbooks

Pre-defined multi-step thinking scaffolds per operation type
(`services/agent/playbooks/*.yaml`): ordered steps with `goal`, `helper_tools`,
`checklist`, `output` (schema fragment). Example, `sketch_profile` playbook:

1. Identify functional surfaces & datums (output: named list)
2. Draft entity list (helper: `describe_sketch` after each edit)
3. Close the profile (helper: `sketch_health` — regions + gaps)
4. Geometric constraints first (helper: auto-coincident already applied; advisor lists
   remaining DOF)
5. Driving dimensions, function-critical first (helper: `suggest_dimension_scheme`
   with manufacturing method + material)
6. Exit gate: fully constrained, closed, no conflicting tags.

Small models execute playbooks step-by-step (one step per turn); frontier models get
the playbook as a compact checklist and may batch steps. Enforcement level comes from
the tier profile, not the playbook.

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

`services/agent/tiers.yaml`: per-tier dials — context budget split (ledger/advisory/
playbook/history token allocations), playbook enforcement (step-locked | checklist |
off), structured-output mode (GBNF grammar | JSON-mode | free), advisory verbosity,
max tool-schema exposure (role allowlists trim the 150-tool surface to <20 per role —
essential for small models), compaction aggressiveness. Ships with profiles:
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

Parallel part building for assemblies: a **doc ownership registry** (agent leases a
doc_id; mutations by non-owners are rejected with the owner named; leases expire on
handoff/death), buildgraph fan-out spawning one modeling sub-agent per part doc, and
a per-doc event stream so the GUI can render N live viewports at once — the user
watches every agent's part evolve simultaneously. Assembly-level roles (fit engineer,
reviewer) take read leases; the world/assembly doc has a single writer at a time.

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

### 2.17 Organization layer (contract §18–§21)

Departments (per deliverable cluster, chaired by the authored department_manager
role) with auto-created inter-department channels and an enforced
interface_change discipline; worker requests with mandatory explicit resolution
and concern-aging into overseer audits; seated COUNCILS (authored personas:
manufacturing/tolerancing/fitment/simplicity) gating build strategy before
geometry, with recorded dissents the advisory engine watches; trigger-based context
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

---

# Appendix A — End-to-end advisory benchmark (2026-08-04)

Measured, not projected. The results below came from a live-model benchmark harness
(research tooling that is not part of the public tree) run against hosted frontier and
long-context models.

## What ran

One frozen mission — *bearing mount bracket: 20 mm g6 sliding-fit bore, 4x M5
clearance holes, aluminium, machined, survives a 500 N side load* — through both
loops on two hosted model tiers:

* **baseline** — one linear `general_agent` turn (`run_linear_role_turn`)
* **advisory** — the orchestrator role over a compiled `aicad.dag/1` mission
  graph with role nodes and the advisory engine on (`dag.executor.run`)

Scoring is deterministic, no LLM judges: bore diameter through `fit_calc`
(clearance fit on 20 g6), hole pattern by counting `concavity == "bore"` faces
near Ø5.5 in `query_geometry`, `is_manifold` from the verifier battery, and
`min_fos` from `run_fea_static` under the 500 N load.

**Scope: one seed, not the planned three.** Each advisory leg runs the full DAG
(intake → plan → spec → build → verify → deliver) at ~17–30 min, and the run was held
under its model-usage budget. Seeds 2 and 3 were not run. The matrix is seed-major so
the executed subset is a complete loop × tier block.

## Comparison

| loop | tier | ok | stop | tool calls | tool fails | score | tokens | wall s |
|---|---|---|---|---|---|---|---|---|
| baseline | claude-opus-5 | yes | confirmed | 23 | 3 | **1.00** | 700,916 | 236 |
| advisory | claude-opus-5 | yes | done | 94 | 11 | **1.00** | 367,231 | 1001 |
| baseline | k3-256k | yes | confirmed | 32 | 1 | **0.00** | 397,821 | 704 |
| advisory | k3-256k | yes | done | 48 | 9 | **0.00** | 198,257 | 1778 |

| loop | mean score | tool calls | tool failures | fail rate | tokens |
|---|---|---|---|---|---|
| advisory | 0.50 | 142 | 20 | 14.1% | 565,488 |
| baseline | 0.50 | 55 | 4 | 7.3% | 1,098,737 |

Per-criterion: both opus-5 legs passed all four checks (bore 20.021 mm measured,
clearance fit; 4/4 Ø5.5 holes; manifold; FoS 15.0). Both k3-256k legs failed all
four — no bore face of the right size ever reached the document.

## What this does and does not show

**It does not show the advisory loop winning on quality.** At n=1 per cell the
outcome is decided by the TIER, not the loop: opus-5 satisfied the mission on
both loops, k3-256k on neither. That is a real finding and it is not the one the
layer's thesis predicted.

**The token result is the interesting one.** The advisory loop used roughly HALF
the tokens of the linear loop on both tiers (367k vs 701k on opus-5; 198k vs 398k
on k3-256k) while making 2–4x MORE tool calls. Role-scoped context packs cost far
less per turn than one linear conversation that re-sends a growing transcript on
every iteration. If it holds across seeds, the advisory architecture's case is
economic — more actions per token — rather than higher single-shot quality.

**Do not read the failure rates as loop quality.** The advisory loop's higher
rate (14.1% vs 7.3%) is dominated by mission-scoped tools its nodes are
advertised but cannot resolve in a bench harness (`delegate`, `dag_status`,
`buildgraph_status`). That is a harness-shaped artifact of
how the bench drives the DAG, not evidence about the loop.

## Measurement caveats found the hard way

* **Scoring must be isolated per leg.** Every leg shares one engine, and the
  engine restarts document numbering, so `doc-cpp00000001` is genuinely reissued.
  A first pass that scored "the best open document" had every advisory leg
  grading the BASELINE leg's finished bracket and reported 1.00 across the board.
  The corrected scorer only considers documents that did not exist when the leg
  started. The k3-256k 0.00 rows are what that contamination was hiding.
* **Legs must hold a private daemon**, or a
  concurrent pytest run shares the engine and destroys the leg's document
  mid-run — observed, open-doc count climbing 2 → 15 under a leg.
* **Doc-GC must be pinned off** (`CHUDWARE_DOC_GC=0`); hostd was logging
  `doc-gc: retire doc_save(doc-cpp00000010) failed` against a live leg's own doc.
* **Per-leg token totals are approximate for the DAG loop.** `get_turn_usage()`
  is a process-global accumulator reset per turn, so a multi-node run can lose a
  node's usage (one discarded leg reported 0 tokens across 72 calls).

## Advisory triples

Not delivered. `dag.node.done` carried an `advisory` block on **zero** nodes
across all four legs (`advisories = 0` in every leg record), so there were no
advisory-shown → action → check-outcome triples to log. The advisory engine is
gated on `CHUDWARE_ADVISOR` (default on) AND on the node carrying a `doc_id`;
`node_runner._run_advisory` no-ops without one. Establishing why no node
qualified is prerequisite work for a next-action training dataset, and it is
the first thing to investigate before running seeds 2 and 3.

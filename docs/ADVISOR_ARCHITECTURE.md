# Pillar 20 — The Advisory Layer ("Foreman")

Status: ratified 2026-07-04 (Hunter directive, planned by lead session).
Frozen schemas live in `contracts/advisor.md`. Work is tracked as workstream **W**
(T-W1 … T-W11), lane `ai-copilot`.

## 0. Thesis

Today's loop is **generate → check → fail → guess again**. A checker that only says
"interference detected" forces the model to search dimension-space by trial and error —
tolerable for Opus, fatal for a 24k-context 4B model.

Pillar 20 inverts this: **deterministic helpers compute, models decide.** Every check
failure arrives paired with solver-computed candidate fixes and the exact numbers needed
(fit tables, equations, clearances). Every process stage arrives with the calculators,
data tables, and playbook steps relevant to *that* stage, pre-filled from live model
state. The model's job shrinks from "derive mechanical engineering from scratch inside
8k tokens" to "choose between well-posed options and supply intent."

Design rules:

1. **Advise, never railroad.** Advisories are injected context, not control flow. A
   frontier model may ignore them; a small model leans on them. The same machinery
   serves both — only the *dial* (enforcement, verbosity, structure) changes per tier.
2. **Numbers come from the kernel or a table, never from the LLM.** Same provenance
   rule as Pillar 15.
3. **State lives in the ledger, not the chat.** Chat history is a cache; the design
   ledger is the source of truth. Any role can be cold-started from ledger + describers
   inside a 24k window.
4. **Every failure is a card, every card has candidate fixes, every retry is bounded.**
   After N attempts the loop escalates (ask_user or dag_replan) — never endless.

## 1. What we build on (reuse map — do not rebuild)

| Existing | Where | Role in Pillar 20 |
|---|---|---|
| Agent loop (plan→code→render→verify, 4-level compaction) | `services/agent/loop.py` | Host for advisory injection (seam: message assembly before `provider.complete_ex`) |
| DAG orchestrator, autonomy dial, `dag_replan` | `services/dag/` | Host for role cards per node; escalation target |
| Tool registry w/ risk classes + allow-lists | `services/mcp/tool_registry.py` | Enforcement of role-card tool scopes (already supports `AI_CAD_TOOLS_ALLOW` / node `tools:`) |
| GSL describers (`describe_part/sketch/scene/...`) | `services/mcp/gsl_tools.py` | State→text for ledger snapshots and role handoffs |
| Solver analysis: DOF, redundant/conflicting tags, per-entity under/full/over | `core/solver/nsolve.h` (`System::analyze`), `core/sketch/` | Raw signal for the sketch completion suite |
| Closed-region detection | `session.h` `doc_sketch_regions()` | Profile-closure + auto-coincident targeting |
| Interference/overlap/mass/section/symmetry evals | `session.h` `doc_eval_*` | Raw signal for advisories & failure cards |
| `regen_diagnose` / `regen_resolve` | `services/mcp/regen_tools.py` | First failure-card producer (rebuild class) |
| `verify_battery`, `design_review` | verify/design_review tools | Stage-exit gates; advisory engine wraps their output |
| Research tools (`web_search`, `material_lookup`, `thread_spec`, `eng_calc`, `unit_convert`) | Pillar 8 | Calculator registry extends `eng_calc`; web/data access for advisors |
| Interaction contract (`ask_user`, answer ledger) | `services/agent/interaction.py` | Escalation transport; critical-dimension confirmation |
| Materials registry | `engine_oracle/sim/materials.json` | Inputs to DFM/tolerance/sim advisors |
| Deterministic CAM fast-path (`deterministic_op_plan`) | `tools/flywheel/bench/benches/cam.py` | The proven "pre-defined process + model picks params" pattern we generalize |

Confirmed gaps this pillar fills: auto-constraint inference, tolerance/fit tables (ISO
286), feeds & speeds data, stage-aware advisory injection, role/playbook definitions,
design ledger, failure-card unification, sim pre/post advisors.

## 2. Components

### 2.1 Design Ledger (T-W1)

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

### 2.2 Stage model + Advisory Engine ("foreman") (T-W5)

The process is a fixed vocabulary of stages (contract §1):
`intake → spec → layout → sketch → feature → part_verify → assembly → sim → cam → deliver`
(plus `fix` as a re-entrant sub-stage). The DAG orchestrator already models missions;
stages annotate *where inside a node's work* the agent currently is. Stage is inferred
deterministically from the last tool calls (e.g. `add_features` w/ sketch ops ⇒ sketch;
`run_fea_static` ⇒ sim) with an explicit `stage_set` override tool.

After every tool call (loop.py seam) or node step (DAG seam), the engine:

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

### 2.3 Role cards (T-W7)

Predefined sub-agent roles, data-driven (`services/agent/roles/*.yaml`), consumed by
both the DAG node runner and standalone loop. Contract §4 fixes the schema:
`id, goal, system_prompt, tool_allowlist (registry families/names), stage_scope,
ledger_sections (read/write), handoff_schema (structured JSON the role must emit),
context_recipe (which describers at what budget), escalation_policy`.

v1 roles: `orchestrator`, `clarifier`, `layout_engineer`, `sketcher`,
`feature_modeler`, `assembly_fit_engineer`, `sim_engineer`, `cam_programmer`,
`reviewer`, `fixer`. Each system prompt is *rich*: identity, quality bar, the stage
playbook inline, worked micro-examples, exact output format, and the "you will receive
ADVISORY blocks — treat computed numbers as ground truth" rule.

Handoffs are structured JSON (grammar-constrained for local models), written into the
ledger — the next role never needs the previous role's chat.

### 2.4 Playbooks (T-W7)

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

### 2.5 Calculator & data registry (T-W3, T-W4, T-W6)

Extends `research_eng_calc` into a keyed registry where the advisory engine can
**pre-fill inputs from live geometry** (bore diameters, wall thickness, spans measured
via `doc_eval_measure`). Every result uses the Pillar 8 envelope: `{answer, units,
equation, inputs, source, confidence}` — the equation is shown so the model (and human)
can audit.

- T-W3 data: ISO 286 fit/tolerance tables (H7/g6 & friends, IT grades), standard fit
  recommendations by function (sliding/press/location), `fit_calc(nominal, fit)` and
  `suggest_dimension_scheme(sketch, process, material)`.
- T-W4 calculators: bolted joint (preload/torque), thread engagement length, press-fit
  interference stress, beam bending/deflection (also the sim cross-check), min wall
  thickness by process/material, snap-fit, shaft-hub. Auto-prefill layer.
- T-W6 data: feeds & speeds by material/tool/operation (seed table + citation fields),
  drill/tap charts; wired into the CAM fast-path and `cam_cost_estimate`.

### 2.6 Sketch completion suite (T-W2)

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
- **Critical dimensions**: `suggest_dimension_scheme` (T-W3) chooses *which* dims to
  drive based on function tags + manufacturing method (e.g. bolt-pattern
  center-to-center over edge-to-hole; process-appropriate default tolerances from the
  DFM profile), and marks dims needing user confirmation via `ask_user`.

### 2.7 Failure cards + fixer loop (T-W8)

One schema (contract §5) for every failure class: rebuild error, solver
conflict/divergence, interference, overlap, DFM violation, sim failure/margin miss,
CAM gouge. Card = `{class, what, where (stable ids), why (deterministic diagnosis when
available), candidate_fixes[{action, params, predicted_effect, confidence}], helper
tools, attempt_count}`. `regen_diagnose` already produces the rebuild class — T-W8
unifies the rest and adds the **bounded retry policy**: per-class attempt budget;
on exhaustion escalate to `ask_user` (with evidence) or `dag_replan`, never loop.
The `fixer` role consumes cards; its prompt forbids shotgun edits ("change only what
the card names; one candidate per attempt; report predicted vs measured effect").

### 2.8 Simulation verification loop (T-W9)

- **Pre-sim advisor**: BC lint (unconstrained bodies, load on suppressed face),
  mesh-size suggestion from feature size, and an **order-of-magnitude hand-calc
  cross-check** using T-W4 calculators (beam/plate approximations) — the run is
  annotated "expect ~X MPa" so garbage results are caught, not trusted.
- **Post-sim narrator**: extends `describe_results` with margin policy per material
  (yield/endurance from registry), and geometry-linked suggestions ("max stress at
  fillet f_3 r=1; r=3 typically drops Kt 2.4→1.6 — candidate fix on feature F9").
- Sim advisories feed failure cards when margins miss.

### 2.9 Model tier profiles (T-W10)

`services/agent/tiers.yaml`: per-tier dials — context budget split (ledger/advisory/
playbook/history token allocations), playbook enforcement (step-locked | checklist |
off), structured-output mode (GBNF grammar | JSON-mode | free), advisory verbosity,
max tool-schema exposure (role allowlists trim the 150-tool surface to <20 per role —
essential for small models), compaction aggressiveness. Ships with profiles:
`local-24k`, `local-48k`, `mid`, `frontier`. Bench harness runs the same mission
across tiers.

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

## 4. Workstream W task map

```
T-W1 ledger ─────┬─→ T-W5 advisory engine ─┬─→ T-W8 failure cards/fixer ─┐
                 └─→ T-W7 roles+playbooks ─┼─→ T-W10 tier profiles       ├─→ T-W11 e2e bench
T-W2 sketch suite ─────────────────────────┤                             │
T-W3 fits/tolerance data ─→ T-W4 calculators ─→ T-W9 sim advisor ────────┘
T-W6 feeds/speeds data ──────────────────────────────────────────────────┘ (via CAM path)
```

Wave 1 (no deps, parallel now): T-W1, T-W2, T-W3, T-W6.

## 5. Beyond-the-competition bets (lead suggestions)

1. **Fix-candidates-first checking.** Nobody ships checkers that answer "so what do I
   change." Pairing every failure with kernel-computed candidate fixes is the single
   biggest iteration-count reducer, and it benefits humans in the GUI too (surface the
   same cards in the app).
2. **The ledger as a product feature.** "Why is this hole Ø20.02?" → the ledger has
   the decision, the fit class, and who confirmed it. That's design intent capture the
   big CAD vendors bolt on badly; we get it free from the agent architecture, and it
   feeds Pillar 19 (PLM) timelines and Pillar 16 training data.
3. **Grammar-constrained handoffs make 4B models composable.** Role → role JSON under
   GBNF means a 4B model's sloppiness can't corrupt the pipeline; correctness reduces
   to per-role competence, which playbooks + advisories directly boost.
4. **Advisories as training signal.** Every (advisory shown → model action → check
   outcome) triple is a labeled example for the Pillar 13/16 flywheel — the NNUE
   next-action value head gets its dataset from normal operation.
5. **Same advisory bus for the human GUI.** The foreman's advisory blocks are
   render-able as in-app hints (the existing overlay/PropertyManager). One engine,
   two audiences.

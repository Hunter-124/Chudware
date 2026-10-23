# contracts/design_pipeline.md — Design pipeline contract (`aicad.designpipe/1`)

Frozen 2026-07-05. This is the FROZEN seam for the guided design-thinking
pipeline. Architecture of record: `docs/design/AGENT_DESIGN_PIPELINE.md`
(2026-07-05). It **refines, it does not replace**, the existing phase machine
(`services/agent/pipeline/phases.py`), the design ledger (`aicad.ledger/1`,
`contracts/advisor.md` §2), the design-cognition ladder (`aicad.ladder/1`, §22), the
trigger table (`aicad.trigger/1`, §20), and the effort presets (`aicad.effort/1`, §23).

This contract **extends `aicad.ledger/1`** — exactly as §21 (dimension derivation) does.
It adds four typed ledger event kinds and their stage IO schemas; it does **not** edit
the frozen §2 ledger state schema. A one-line cross-reference may be folded into
advisor.md §2 on a version bump of that contract.

Additive changes bump the minor; breaking changes require a new major + a migration note
here. All schemas are JSON; all lengths mm, angles deg, masses g (canonical units per the
interaction contract's `normalize_dimension`).

---

## §1 What this contract freezes

Per `docs/design/AGENT_DESIGN_PIPELINE.md` §5, this contract freezes exactly five things:

1. **Stage IO schemas** — the typed handoffs `feature`, `geometry_spec`, `linkage`,
   `build_node` (§3), written to the ledger as new event kinds (§4).
2. **Stage-gate definitions** — what makes each stage "complete", **read from the
   ledger, never from handoff prose** (§5). The universal rule: a value that exists
   only in a model's chat does not exist.
3. **How the six stages nest inside `phases.py`** and reuse its gates (§6).
4. **Effort / auto-mode hooks** — which stages / councils / helpers each scaffold level
   enables, expressed in terms of the existing `aicad.effort/1` knobs (§7).
5. **Advisor trigger bindings** — geometry-kind → helper set, expressed as a mapping onto
   existing `aicad.trigger/1` ids (§8).

Out of scope for this contract (built on top of it): the `feature_architect` /
`geometry_designer` / `linkage_planner` roles, the overseer reroute loop, the
diagnostic-advisor expansion, the council policy engine, delegation recursion, and the
auto-mode classifier itself. This contract freezes the **seams** those components build
against.

---

## §2 Relationship to the §22 cognition ladder — the six stages ARE the ladder rungs

The six design-pipeline stages are the same abstraction ladder already named in
`aicad.ladder/1` (advisor.md §22). This contract does not invent a new thinking chain — it
**freezes the typed ledger artifacts and stage gates** for the rungs the ladder already
runs in fresh context:

| DP stage (doc §2) | ladder rung (§22) | rung artifact (§22) | frozen ledger kind (§4) |
|---|---|---|---|
| Stage 0 — INTENT CAPTURE | (pre-ladder: intake) | — | `brief` / `requirement` (§2, existing) |
| Stage 1 — FUNCTIONAL DECOMPOSITION | A `function_map` | `functions[]` + `features[]` | **`feature`** |
| Stage 2 — GEOMETRY REALIZATION | B `geometry_plan` | `elements[]` | **`geometry_spec`** |
| Stage 3 — TOPOLOGY / LINKAGE | C `linkage_map` | `datums` + `links` | **`linkage`** |
| Stage 4 — BUILD-PATH PLANNING | D `build_pathway` → compile | `steps[]` → buildgraph | **`build_node`** |
| Stage 5 — EXECUTION | E execution | (Phase 4 machinery) | (existing buildgraph node events) |

The §22 rung artifacts are the *authoring* form (a design_thinker emits one blob per rung,
grammar-constrained on local tiers). The §4 event kinds below are the *ledger-durable*
form: one event per feature / geometry element / linkage / build node, so the stage gates
(§5) and the scoped-slice accessors (§9) can read them individually. A rung artifact
lowers losslessly into these events; the fields line up 1:1 (§3 notes the mapping per
field).

---

## §3 Stage IO schemas (the typed handoffs)

All four are ledger event payloads (`{seq, ts, actor, kind, payload}` per §2). `id` fields
are stable within a run. Provenance rules are stated per kind and enforced by the writer
helpers (`services/agent/ledger.py`) and the mission-dispatch guard (`ledger_note`).

### 3.1 `feature` — Stage 1 output (written by `feature_architect`)

One event per function/feature the part must perform. Mirrors §22 rung-A `features[]`.

```json
{
  "id": "FT1",
  "function": "mount to chassis rail",
  "driving_requirement": "R2",
  "interfaces_with": ["FT4"],
  "criticality": "critical"
}
```

- `id` — stable feature id (`FT<n>`).
- `function` — verb-noun statement of what the feature DOES (§22 rung-A `statement`).
- `driving_requirement` — the `req_id` (or `assumption:<id>`) this feature traces to.
  **Coverage rule** (checked by the coverage checker, not this gate): every feature
  traces to a requirement or a recorded assumption; every requirement maps to ≥1 feature.
- `interfaces_with` — feature ids this one shares an interface with (bore, mate, seal…).
- `criticality` — `critical | important | nice` (§22 rung-A vocabulary).

### 3.2 `geometry_spec` — Stage 2 output (written by `geometry_designer`)

One event per feature (or feature cluster): the concrete geometry that realizes it, gated
by material and process. Mirrors §22 rung-B `elements[]`. **Provenance-carrying** (see rule).

```json
{
  "id": "G1",
  "realizes": ["FT3"],
  "shapes": [{"op": "revolve", "profile": "journal", "kind": "primitive|profile_op"}],
  "nominal_dims": [{"name": "bore_d", "value": 20.02, "unit": "mm", "dim_ref": "DIM7"}],
  "tolerances": [{"name": "bore_d", "fit": "H7", "upper": 0.021, "lower": 0.0}],
  "datum_refs": ["A", "B"],
  "provenance": "fit_calc(H7/g6, nominal=20) → 20.02 mm [iso286.json]"
}
```

- `id` — stable geometry-element id (`G<n>`).
- `realizes` — feature ids (`FT…`) this element serves (§22 rung-B `realizes`). Orphan
  geometry (realizes nothing) is a geometry-designer finding, not a gate failure here.
- `shapes` — the ops that build it (`extrude | revolve | loft | sweep | hole | pattern |
  shell | fillet | chamfer | …` — the doc-op vocabulary, `contracts/doc_ops.md`).
- `nominal_dims` — critical dimensions. Each may carry a `dim_ref` to a ledger `dimension`
  event (`DIM…`); dimensions themselves keep their own §2 provenance.
- `tolerances` — per-dim tolerance / fit (same shape as the §2 dimension `tolerance`).
- `datum_refs` — datum ids the element references.
- **`provenance` (REQUIRED)** — how the critical dims were obtained: a tool result, a
  calculator, a table, or an explicit user answer. **A `geometry_spec` written without
  `provenance` is rejected** (writer helper raises; `ledger_note` returns an error). This
  is the geometry-side twin of the §2 dimension provenance rule — never recall a number.

### 3.3 `linkage` — Stage 3 output (written by `linkage_planner`)

The skeleton: how the geometry links. Mirrors §22 rung-C `linkage_map`. One event per run
(upsert by `id`, default `LINK_MAIN`) OR one per datum/reference cluster.

```json
{
  "id": "LINK_MAIN",
  "datum_tree": [{"id": "A", "kind": "plane|axis|point", "defined_by": "base face", "why": "primary locating face"}],
  "reference_edges": [{"from": "G3", "to": "G1", "relation": "sketch_on_face", "why": "boss on bore boss face"}],
  "shared_interfaces": [{"features": ["FT2", "FT5"], "on": "G1.bore_d", "must_agree": "diameter+tolerance"}]
}
```

- `datum_tree` — datums and their definitions (§22 rung-C `datums`).
- `reference_edges` — directed reference relationships between geometry elements
  (§22 rung-C `reference_tree` + `links`). **Integrity rules** (checked by the
  reference-integrity checker, not this gate): no dangling refs, no cycles.
- `shared_interfaces` — features sharing an interface must agree on the interface spec
  (two features sharing a bore agree on Ø + tolerance). Interface-consistency is a
  linkage-planner check; this gate only requires the linkage artifact to EXIST.

### 3.4 `build_node` — Stage 4 output (written by `build_strategist`, exists + buildgraph)

One event per ordered construction step. Mirrors §22 rung-D `steps[]`; lowers into an
`aicad.buildgraph/1` node (advisor.md §13).

```json
{
  "id": "BN1",
  "op": "revolve",
  "deps": [],
  "orientation": "base on XY, axis +Z",
  "sketch_plane": "datum A",
  "elements": ["G1"],
  "why": "base feature — everything references the journal"
}
```

- `id` — stable build-node id (`BN<n>`); becomes the buildgraph node id.
- `op` — the doc op this step performs.
- `deps` — build-node ids that must exist before this one (§22 rung-D sequencing rule:
  every reference exists when built). Cycles / missing refs are a linkage/buildgraph
  validator concern (advisor.md §13), surfaced to the PLAN gate as a blocker.
- `orientation` / `sketch_plane` — the base-feature/orientation and sketch-plane choice.
- `elements` — geometry-element ids (`G…`) this node builds.
- `why` — the rationale (becomes the buildgraph edge `why`, §22 rung-D).

---

## §4 New ledger event kinds (extends `aicad.ledger/1`)

Four new `kind` values on the append-only `events[]`, each materialized into a new `state`
section. Existing §2 kinds are untouched.

| kind | written by | state section | upsert key | provenance |
|---|---|---|---|---|
| `feature` | feature_architect (Stage 1) | `state.features[]` | `id` | traces to requirement/assumption (checker) |
| `geometry_spec` | geometry_designer (Stage 2) | `state.geometry_specs[]` | `id` | **required** — writer rejects without it |
| `linkage` | linkage_planner (Stage 3) | `state.linkages[]` | `id` | — |
| `build_node` | build_strategist (Stage 4) | `state.build_nodes[]` | `id` | — |

Rules:
- **Append-only + replay guarantee.** As with every §2 kind, events are never mutated;
  `state` is a pure replay (`_materialize`). A round-trip (append → reload from disk →
  re-materialize) reproduces `state` exactly. Later events with the same `id` upsert
  (last-write-wins field merge), matching the §2 `dimension` / `criterion_result` pattern.
- **`geometry_spec` provenance is enforced at both write paths.** `Ledger.add_geometry_spec`
  raises `ValueError` if `provenance` is empty; `MissionDispatch.ledger_note` returns
  `{"ok": false, error: ...}` for `kind=="geometry_spec"` without `provenance`. This mirrors
  the §2 `dimension` rule so a stage cannot back-door a provenance-less spec.
- **The dimension provenance rule from §2 stands unchanged** — geometry `nominal_dims` that
  reference a ledger `dimension` (`dim_ref`) inherit that dimension's own provenance.

MCP surface (unchanged shape, additive kinds): `ledger_note(run_id, kind, payload)` accepts
the four new kinds; `ledger_get(run_id, section?)` accepts the four new sections
(`features`, `geometry_specs`, `linkages`, `build_nodes`).

---

## §5 Stage-gate definitions (read the ledger, never the prose)

Each design-pipeline stage has a deterministic exit gate that inspects **durable ledger
state only** — no model call. This is the W32 rule every stage inherits: the gate reads
whether the typed artifact EXISTS in the ledger, not whether the model claimed it in chat.

The gates are intentionally minimal: they check **artifact presence**, not
semantic quality. The semantic checkers (coverage, reference-integrity, interface-
consistency, orphan-geometry) belong to the stage roles and layer on top; they
extend the gate context, not the gate signature.

| stage | gate name | passes when (read from ledger) |
|---|---|---|
| DECOMPOSE (Stage 1) | `features_present` | `state.features` is non-empty (≥1 `feature` event). |
| GEOMETRY (Stage 2) | `geometry_specs_present` | every feature id in `state.features` is realized by ≥1 `geometry_spec` (`realizes` covers it) **and** every `geometry_spec` carries `provenance`. |
| LINKAGE (Stage 3) | `linkage_present` | ≥1 `linkage` event exists. |
| BUILDPATH (Stage 4) | `build_nodes_present` | ≥1 `build_node` event exists **and** every `build_node.elements` id resolves to a `geometry_spec` id (no dangling element ref). |

The gate context keys the caller supplies (all pure ledger reads):
`features` (list), `geometry_specs` (list), `linkages` (list), `build_nodes` (list). A
frontier model that satisfies a stage in one pass passes its gate immediately (§10
flexibility rule: stages are skippable checkpoints).

Gate outputs are `GateResult(passed, gate, reasons, overseer)` — the exact type
`phases.py` already returns; the design-pipeline gates set `overseer=True` on the GEOMETRY
and BUILDPATH gates (the two the overseer audits per doc §3.1; the actual audit is
feature-flagged, same pattern as the existing SPECIFY/PLAN gates).

---

## §6 How the six stages nest inside `phases.py`

The six stages refine the existing `SPECIFY → ORGANIZE → PLAN → BUILD` span (doc §2). They
are wired as **flagged phases** — the exact ORGANIZE precedent: new `Phase`
members inserted into `PHASE_ORDER`, skipped by `next_phase` unless the flag is on, so the
existing pipeline is byte-for-byte unchanged when the flag is off.

```
INTAKE → [CLARIFY] → SPECIFY
           → [DECOMPOSE] → [GEOMETRY] → [LINKAGE] → [BUILDPATH]   (design-pipeline stages)
           → [ORGANIZE] → PLAN → BUILD → VERIFY → [CORRECT ↺] → DELIVER
```

- Flag: `PipelineFlags.design_pipeline` (env `CHUDWARE_DESIGN_PIPELINE`), **default False**.
- When OFF: `next_phase(SPECIFY)` returns the next enabled phase exactly as today
  (`ORGANIZE` when the org flag is on, else `PLAN`). The four new phases are `continue`d
  over — no structural change, current phase tests stay green.
- When ON: the machine visits DECOMPOSE → GEOMETRY → LINKAGE → BUILDPATH after SPECIFY and
  before ORGANIZE/PLAN, each enforcing its §5 gate.
- Stage 0 (INTENT CAPTURE) maps to the existing INTAKE/SPECIFY phases (no new phase).
  Stage 4 (BUILD-PATH) writes `build_node` events that the existing PLAN phase compiles
  into the buildgraph; Stage 5 (EXECUTION) is the existing BUILD phase. The design-pipeline
  phases sit strictly between SPECIFY and the existing PLAN span — they add the missing
  rungs, they do not renumber the existing phases.

The gates never call a model (the `PhaseMachine` invariant holds). `Phase` serialises as
its lowercase name into the ledger `stage_log`, so mission status stays a pure ledger read.

---

## §7 Effort / auto-mode hooks (which scaffold level enables what)

The pipeline's depth is governed by the existing `aicad.effort/1` knobs (advisor.md §23) —
this contract adds no new dial. The binding below says which effort level enables which
stages / councils / helpers. **Local tiers default to the full scaffold; frontier tiers dial
it down** (the inverted deliberation dial, §8 addendum: local=`full`, frontier=`minimal`).

| effort `deliberation` | design-pipeline stages | councils (§8) | proactive helpers (§8 triggers) |
|---|---|---|---|
| `full` (local default) | all four stages mandatory, each gated | wide (per §22: completeness critic on Stage 1, regen-stability on Stage 3, build-strategy on Stage 4) | all trigger packs auto-injected |
| `standard` | all four stages, gates enforced | risk-gated (council only where the council knob warrants) | triggers fire on match |
| `minimal` (frontier default) | stages are single-pass checkpoints; a model may emit rung D directly (validators still run) | narrow / single | helpers offered, model may decline |

The mapping is expressed against existing knobs — nothing new is invented:
- **`deliberation` / `ladder`** (§22, `mandatory|optional|off`) gate whether the four
  design-pipeline stages run as discrete gated rungs or collapse to a single checkpoint.
  `design_pipeline` flag on + `ladder=mandatory` = full six-stage run.
- **`council` / `council_seats`** (§19/§23) gate the per-stage councils (completeness /
  regen-stability / build-strategy). The council engine is separate; this contract only
  binds *which* stages offer a council slot.
- **`delegation_depth` / `worker_fan_out`** (§12/§23) govern Stage 5 fan-out (workers over
  independent DAG branches) — unchanged from the existing buildgraph execution.

Auto-mode (a workflow router that classifies the task and self-selects depth) is separate
and out of scope here; this contract freezes only the *level → enabled-set* binding it
targets. The tuning loop (gym bench) learns the best level per task-class (doc §3.6).

---

## §8 Advisor trigger bindings — geometry-kind → helper set

Stage 2 (GEOMETRY REALIZATION) is where "advise, don't recall" lives: the *correct*
calculator / data is auto-loaded for the geometry kind the model is about to build. These
bindings **map geometry kinds onto the EXISTING `aicad.trigger/1` table**
(`services/agent/advisor/triggers.yaml`, advisor.md §20) — no new trigger machinery. A
geometry kind fires the trigger id below; the trigger injects its context pack + prerun
calcs exactly as it does today.

| geometry kind (Stage 2) | existing trigger id (§20) | helper / calc auto-loaded |
|---|---|---|
| mating bore / shaft seat / bearing seat | `bearing_seat`, `tolerance_mention` | `fit_calc` / `recommend_fit`, `iso286.json` |
| press / interference fit | `press_fit_mention` | `fit_calc` (H7/p6), `iso286.json` |
| threaded hole / tapped boss / stud | `thread_mention`, `thread_engagement` | `tap_drill`, `research_thread_spec`, engagement rules |
| bolt circle / fastener pattern / clearance hole | `fastener_pattern` | `tap_drill`, clearance-hole table |
| thin wall / shell / rib | `thin_wall` | `wall_thickness`, process minimums |
| snap fit / cantilever hook / latch | `snap_fit_mention` | snap-fit strain calc |
| material-gated dims (any) | `material_mention` | `research_material_lookup`, `materials.json` |
| general tolerance / fit callout | `tolerance_mention` | `fit_calc`, ISO 286 |

Rules (inherited from §20, restated for the geometry stage):
- The binding is **data** — the geometry_designer / advisory expansion may add
  triggers (e.g. sheet-metal bend allowance, draft for molded) to `triggers.yaml`; this
  table lists the geometry kinds already covered.
- Triggers are **offered, not imposed** — a frontier model may decline (flexibility rule).
- Every firing is a ledger event (which trigger, what matched, tokens) — §20.
- Geometry kinds with no existing trigger (e.g. sheet-metal bend, molded draft) are
  *helpers to add* for the geometry designer — this contract does not fabricate coverage it
  cannot cite.

---

## §9 Scoped-slice accessors (for the mission-dispatch layer)

Fresh-context stages never see the whole ledger history — they get a **scoped slice**
(doc §4, mission dispatch). This contract adds read-only slice accessors on
`services/agent/ledger.py` so the dispatch layer can hand each stage exactly its inputs:

| accessor | returns | consumer |
|---|---|---|
| `ledger.features()` | `state.features[]` | Stage 2 (geometry realizes each feature) |
| `ledger.geometry_specs()` | `state.geometry_specs[]` | Stage 3 (linkage over geometry), Stage 4 |
| `ledger.linkages()` | `state.linkages[]` | Stage 4 (build order from linkage) |
| `ledger.build_nodes()` | `state.build_nodes[]` | PLAN (compile buildgraph), Stage 5 |
| `ledger.slice_for_stage(stage)` | the minimal typed inputs a stage needs (per §22 fresh-context rule) | mission_dispatch |

`slice_for_stage` returns only the previous rung's artifact(s) — never the full ledger —
matching the §22 fresh-context rule (Stage 2 sees features only; Stage 3 sees geometry only;
Stage 4 sees geometry + linkage). The `ledger_get(section=...)` MCP path exposes the same
data to model-facing tools.

---

## §10 Flexibility guarantees (frozen — never choke a good model)

Restated from doc §6 as contract invariants every DP task must honour:

1. Every stage is a **skippable checkpoint** — a model that satisfies its §5 gate in one
   pass advances immediately; nothing forces N steps.
2. Helpers (§8) are **offered, not imposed** — a frontier model may decline.
3. **Local default = full scaffold**; frontier default = minimal; the user overrides in
   Advanced mode (§7 / §23).
4. Council breadth (§8/§19) and delegation depth (§12) are **config, per-tier**, not
   hard-coded in the pipeline.
5. The pipeline **adds context and speed, never a fixed modeling path** — two models given
   the same brief may build different valid parts; the pipeline judges by the ledger + the
   verify battery, not by matching a template.

---

## §11 PLM stage-boundary history

When `PipelineFlags.design_pipeline` is enabled, every successful exit from
`DECOMPOSE`, `GEOMETRY`, `LINKAGE`, and `BUILDPATH` emits one
`aicad.plmlink/1` `stage_exit:<phase>` event after the exit event is durable in
the ledger. The checkpoint message is deterministic from exactly the ledger
slice since the previous checkpoint:

| boundary | narrative source |
|---|---|
| `decompose` | `feature.function` and its stable feature id |
| `geometry` | `geometry_spec.shapes`, `realizes`, and verbatim provenance |
| `linkage` | reference edges, shared interfaces, and their `why` text |
| `buildpath` | `build_node.op`, `elements`, and verbatim `why` text |

No model call drafts this narrative and no numeric value may be introduced
outside those ledger payloads. The boundary is best-effort with respect to PLM:
a vault failure queues the full link and MUST NOT block mission progress.

Multi-part runs pass `item_artifacts` (`item_id` → participant buildgraph node)
through the link. The vault stores the mapped node in each item's own
`meta.json.ai.artifact_id`; a project-wide history point therefore preserves
per-participant attribution when each item's timeline is viewed independently.

## §4 additive: `mfg_plan` (2026-08-26)

Fifth typed ledger event kind, same recipe as the original four:

| kind | written by | state section | upsert key | provenance |
|---|---|---|---|---|
| `mfg_plan` | manufacturing_planner (and any role revising the governing plan) | `mfg_plans` | `id` | REQUIRED on every numeric critical row |

One plan per source document/deliverable (id normally `main`); revisions
upsert the same id, never siblings. The plan's `critical_dimensions[]` rows
are AUDIT-NATIVE — the exact row schema `audit_drawing(spec=...)` consumes
(requirement_id, type, value, tolerance{type,upper,lower}, critical,
provenance, derived_from) — so `mfg_plan_get(view="audit_spec")` projects
`{requirements:[...]}` byte-compatibly. `view="work_instructions"` projects
into `make_work_instruction`'s operation schema. MCP surface:
`mfg_plan_upsert / mfg_plan_get / mfg_plan_check / mfg_plan_render`
(family `cad`; get/check are read-class). `ledger_note` accepts the new kind
and `ledger_get` the new section on BOTH write paths (mission shadow and
standalone ledger_tools) — the §4 "both paths" rule applies. Consumers MUST
tolerate the new kind/section per the existing conformance rules.

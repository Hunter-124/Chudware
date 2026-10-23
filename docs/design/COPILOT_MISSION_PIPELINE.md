# Copilot Mission Pipeline — how a request becomes delivered parts

Status: adopted design record (2026-07-04). Companions:
[`ADVISOR_ARCHITECTURE.md`](ADVISOR_ARCHITECTURE.md) (components),
[`contracts/advisor.md`](../../contracts/advisor.md) (schemas — §16 deliverable manifest
and §17 replan request were added for this pipeline). The canonical role system prompts
live in `services/agent/roles/*.yaml`; the role loader uses them verbatim.

The pipeline is implemented, but this document is design intent, not an as-built spec:
check behavior against `services/agent/` before relying on a detail. The July role counts
are dated; use the current `services/agent/roles/` catalog rather than a fixed count.
Since 2026-08-31, role turns' online model execution runs inside the embedded Chudcode RPC
runtime. The pipeline's phases, roles and governance stay Chudware-owned
([Chudcode integration](../dev/chudcode-integration.md)).

## 0. Shape of the whole thing

```
user request
   │
   ▼ Phase 0 INTAKE      intake_analyst → deliverable manifest (aicad.deliverables/1)
   ▼ Phase 1 CLARIFY     clarifier → ≤4 typed questions, defaults-first (only if needed)
   ▼ Phase 2 SPECIFY     spec_writer per deliverable → engineering spec, overseer-audited
   ▼ Phase 3 PLAN        orchestrator + layout_engineer → mission DAG + build graphs
   ▼ Phase 4 BUILD       sketcher/feature_modeler/assembly_fit_engineer workers,
   │                     parallel across part docs (leases), advisory engine every turn
   ▼ Phase 5 VERIFY      reviewer runs the manifest's acceptance checks — literally
   ▼ Phase 6 CORRECT     failure cards → fixer (bounded) → escalation; change requests
   │                     re-enter here via the overseer          ↺ (loops with 4/5)
   ▼ Phase 7 DELIVER     cam_programmer / drawings / reports per deliverable kind,
                         PLM checkpoints, mission report, ledger closed
                                    ── overseer watches trigger points throughout ──
```

Two spines run through every phase:

- **The ledger** carries all durable state. No role ever needs another role's chat.
- **The advisory engine** injects stage-appropriate advisories, computed
  numbers, and helper-tool pointers into every worker turn.

And one valve keeps it flexible: **replan requests** (`aicad.replan/1`). Any role,
at any phase, can propose changing the approach/plan/spec instead of grinding a
failed path. Approval routes by scope (approach → orchestrator; buildgraph →
orchestrator+overseer; spec → overseer(+user if acceptance criteria move);
requirement → user). This is how "think outside the box" is made safe: creativity
is welcome, undocumented drift is not.

## 1. Phase contracts

Per phase: who runs, what they receive (context recipe), what must exist when the
phase exits (gate). Token figures are the local-24k tier; larger tiers scale up but
the *composition* never changes — that is the context-management discipline.

### Phase 0 — INTAKE (intake_analyst; cheap model OK)

- Receives: the user request VERBATIM (never paraphrased), `describe_project` if a
  project is open, list of open docs. (~1.5k tokens)
- Does: breaks the request into the deliverable manifest — deliverables with kinds,
  dependency links, and *measurable, tool-executable acceptance criteria* (contract
  §16). Flags ambiguities and records assumptions with `needs_confirm`.
- Deterministic validator then enforces: every deliverable ≥1 criterion, every
  criterion names its check tool, modifications resolve to real doc refs. Validator
  failures bounce back to the role once, then escalate.
- Exit gate: manifest valid; blocking ambiguities → Phase 1, else skip to Phase 2.
- Why it matters for small models: the manifest is the mission's definition of done.
  Everything downstream cites deliverable/criterion ids (DL1/AC2) instead of
  re-reading the request — the request is parsed exactly once, by a role whose only
  job is parsing it.

### Phase 1 — CLARIFY (clarifier)

- Receives: manifest ambiguities + assumptions, ledger brief. (~1k)
- Does: ONE batch of ≤4 typed questions via ask_user (interaction contract), every
  question with a suggested default so the mission can proceed unattended;
  non-blocking where defaults are safe. Answers land as ledger requirements;
  assumptions flip to confirmed/corrected.
- Exit gate: no blocking ambiguity remains open.

### Phase 2 — SPECIFY (spec_writer per deliverable; strong-ish model preferred)

- Receives: ONE deliverable (its manifest slice), related requirements/answers,
  interface info from sibling deliverables' specs if they exist. (~2.5k)
- Does: the engineering spec — envelope + interfaces, material + manufacturing
  method (research_material_lookup / resolve_process), functional dimensions WITH
  fit callouts computed via fit_calc (never guessed), standards (thread_spec),
  and the verification plan: each acceptance criterion mapped to concrete check
  invocations with pass rules. Records every dimension in the ledger with driver
  + provenance.
- Overseer audits (trigger: plan_ratify) — spec gaps, criteria not covered, order
  problems. User approval per autonomy dial.
- Exit gate: spec audited; all criteria covered by the verification plan.

### Phase 2.5 — ORGANIZE (org runtime, contract §18; added 1.3)

After specs exist, the org runtime instantiates DEPARTMENTS from deliverable
clusters (part + its drawing/CAM satellites), each chaired by a
department_manager with its own workers, budget, and ledger scope. Channels are
auto-created between departments that share interfaces; the interface_change
message discipline (§18) is what keeps parallel departments from silently
breaking each other. Workers raise requests (context/resource/info/comms/
concern) that managers MUST explicitly resolve; aged concerns surface in
overseer audits automatically. The chart is re-organizable mid-mission via
replan scope=org — the hierarchy serves the mission, not vice versa.

### Phase 3 — PLAN (orchestrator; layout_engineer for assemblies; councils)

- Assemblies first get a layout: layout_engineer builds the skeleton doc (envelopes
  as simple solids, interface planes/axes, clearance budgets) — the deterministic
  fitment reference every part cites. (Aligns with the layout-first helper.)
- Orchestrator turns specs into the mission DAG (services/dag) and per-part build
  graphs (aicad.buildgraph/1); the buildgraph validator must pass (missing
  prerequisites are findings, not surprises); role/model assignments come from
  routing.yaml; multi-part missions pre-plan doc leases.
- DESIGN COGNITION LADDER (contract §22; added 1.4): for each part, Phase 3
  climbs the four-rung ladder — function_map (every feature the functions
  demand, with the 8-class omissions checklist), geometry_plan (shapes that
  realize each feature, params symbolic-with-basis), linkage_map (datums,
  linked-dimension equations, constraint schedules), build_pathway (ordered
  steps, orientation, sketch planes). EACH RUNG IS A FRESH CONTEXT — a small
  model reasons about one abstraction level at a time; deterministic
  validators bounce incomplete rungs (max 2); the pathway compiles losslessly
  into the buildgraph. This is the punch-through-the-abstraction-layers
  mechanism: mandatory at deliberation=full (local), optional at minimal
  (frontier may emit rung D directly; validators still run).
- BUILD-STRATEGY COUNCIL (contract §19; added 1.3): before a part's build graph
  is ratified, its department convenes the four-seat council (manufacturing /
  tolerancing / fitment / simplicity — authored personas, run as cheap ponder
  delegations of the same model). Round 1: independent positions. Round 2: seats
  respond to each other. Chair synthesizes; must_haves become buildgraph
  validator checks; DISSENTS are recorded with trigger_conditions the advisory engine
  watches — a minority report that re-raises itself if its condition occurs.
  This is the "figure out the best way to build it from all angles BEFORE
  building" gate.
- Exit gate: graphs valid, council synthesis recorded (above the complexity
  threshold), overseer ratified, budgets allocated.

### Phase 4 — BUILD (workers; parallel per part doc)

- Each worker turn receives exactly: role system prompt + ledger render scoped to
  ITS deliverable + its buildgraph node (with `why`) + stage state (describers) +
  [ADVISORY] block + playbook step (tier-dependent). Never the whole mission.
- The advisory engine runs the stage battery after every tool call; preflight runs before
  mutations; auto-coincident keeps sketches closing; sketch_health / fit_calc /
  feeds_speeds numbers arrive already computed.
- Node done ⇒ validated handoff → ledger; next ready node dispatches (possibly a
  different agent — nothing is lost because nothing lived in chat).
- Exit per node: node's exit gate checks pass, or a failure card / replan request
  exists. Two failed attempts on one node ⇒ the role's own prompt directs it to
  replan_request rather than attempt three (the anti-grind rule).

### Phase 5 — VERIFY (reviewer)

- Runs the manifest's acceptance checks LITERALLY: each criterion's named tool with
  its pass rule (fit_report for fits, verify_battery for integrity, measure_part
  for dimensions, cam_simulate for toolpaths, run_fea_* for margins...), plus the
  standard battery (design_review). Emits per-criterion PASS/FAIL with tool output
  refs — the honest, citable verdict. FAILs become failure cards.
- Exit gate: all criteria PASS, or user waives named criteria via ask_user.

### Phase 6 — CORRECT (fixer; overseer for change requests)

- Failure cards → fixer: one candidate fix per attempt, predicted vs measured
  effect, harness-enforced budget, escalation on exhaustion (never endless).
- User change requests (any time, any phase): overseer maps request → affected
  ledger entries/manifest items → scoped, preflighted change plan → dispatch. The
  manifest is updated (new/changed acceptance criteria) so "done" stays honest.

### Phase 7 — DELIVER (cam_programmer et al.)

- Per deliverable kind: cam_program → deterministic op-plan path + feeds/speeds +
  cam_simulate proof; drawing → make_drawing + review_drawing; analysis/report →
  make_report from describers (provenance rule). PLM checkpoints at part_done /
  mission_done with ledger-drafted messages. Mission report cites every
  deliverable's criterion results. Ledger closed and archived (replayable in the
  transparency inspector).

## 2. The overseer's watch

Fires at: manifest validation failures (2nd), spec ratification, plan ratification,
stage exits, preflight overrides, failure-card escalations, all change requests,
buildgraph/spec-scope replans. It audits EVIDENCE (ledger, handoffs — which carry
each role's reasoning summary — advisories, preflight results), not raw chat.
Verdict classes: plan_gap, wrong_order, spec_drift, missed_requirement, bad_fix,
cot_error, ok. Two blockers with the same fingerprint ⇒ the human hears about it.
The overseer never edits geometry and never vetoes an action (freedom rule) — it
redirects work and escalates.

## 3. Context management rules (the local-model survival kit)

1. **Parse once, cite forever.** The request is parsed into the manifest exactly
   once; downstream roles cite DL/AC ids.
2. **One deliverable per context.** Workers see their deliverable's slice, never
   the mission. Sibling info arrives as interface specs, not sibling transcripts.
3. **Chat is disposable; the ledger is not.** Any turn is reconstructible from
   ledger + describers. Compaction can drop anything at any time safely.
4. **Structured out, structured in.** Handoffs are schema-validated (GBNF on local
   tiers); the next role receives the envelope, not prose.
5. **Numbers are precomputed.** The advisory engine + calculators put the needed values in the
   advisory block so a 4B model chooses between correct options instead of doing
   mental arithmetic.
6. **Budgets are enforced, not suggested.** The tier profile's slot budgets are
   asserted at context-assembly time; overflow shrinks history first, never the
   advisory/system slots.
7. **Flexibility has a schema.** Replan requests keep out-of-the-box thinking
   inside the ledger where the overseer and the human can see it.
8. **The dial is INVERTED for deliberation (1.3).** Local tokens are free —
   local tiers get FULL deliberation (councils, ponder spin-offs, trigger
   context packs all default-on); frontier tiers get minimal (councils only
   where risk warrants). More machinery for weaker models, not less.
9. **Truth has a tree (1.3).** Derived dimensions record what they derive from
   (§21); linked dimensions bind into engine equations so change propagates by
   math; mark_faulty walks the tree and reports exactly what needs
   relink/recompute/rebuild/reverify — an early error never silently poisons
   later features.

## 4. Worked example (compressed)

"Design a two-piece clamshell enclosure for this PCB with a snap-fit lid, and give
me the CAM program for the bottom half in 6061."

- Phase 0: DL1 part bottom-shell (AC: PCB fits with ≥0.5 mm clearance →
  check_interference vs ecad_to_3d body; wall ≥ min for process → wall_thickness),
  DL2 part lid (AC: snap engages → snap_fit calc within material strain; closes
  flush → measure_between), DL3 assembly fit (AC: fit_report clean), DL4
  cam_program bottom (AC: cam_simulate zero gouges, machined_fraction ≥ target).
  Assumption: material 6061 both halves (needs_confirm — lid could be printed).
- Phase 1: one batch: lid material/process? (default 6061 machined), PCB standoff
  height? (default from board thickness), external fasteners allowed? (default no).
- Phase 2: specs per part; snap dimensions computed via snap_fit calc for 6061's
  allowable strain — advisory will warn 6061 snaps are marginal; spec_writer may
  emit a spec-scope replan proposing PETG lid or a cantilever redesign → overseer →
  user (criterion changes). This is the flexibility valve working as intended.
- Phase 3: layout skeleton (PCB envelope + parting plane + clearance budget);
  build graphs per half; lease plan for two part docs in parallel.
- Phase 4–6: two workers build in parallel; advisories carry wall/DFM and
  fit numbers; reviewer runs the AC checks; snap engagement fails → failure card →
  fixer adjusts per candidate fix once, works.
- Phase 7: cam_programmer runs the deterministic path with feeds_speeds data;
  drawings optional; PLM checkpoints per part + mission; report cites AC results.

## 5. Implementation pointers

- Role prompts: `services/agent/roles/*.yaml` (authored; the loader uses them
  verbatim — do not rewrite prompts in code).
- Pipeline glue: the phase state machine in `services/agent/pipeline/` on top of
  `services/dag` + buildgraph + ledger; intake validator; replan routing per §17.
- Everything here composes from already-specified parts: design ledger, advisory
  engine, roles, preflight, overseer, delegation, buildgraph, multi-doc workspace and PLM
  (see [`ADVISOR_ARCHITECTURE.md`](ADVISOR_ARCHITECTURE.md) §2).

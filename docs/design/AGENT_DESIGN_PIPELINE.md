# Agent Design Pipeline — the guided design-thinking architecture

**Status:** architecture of record (Hunter directive, 2026-07-05). This is the source of
truth for how Chudware drives a design from intent to a coherent, verified model — especially
on **local 24k–48k-context models**, while never choking frontier models (Opus/Sonnet).
Workstream **DP** implements it (T-DP1…T-DP14). It refines, it does not replace, the existing
phase machine (`services/agent/pipeline/phases.py`), role roster (`services/agent/roles/`),
advisory layer (`contracts/advisor.md`), councils (`services/agent/councils.py`),
buildgraph (`aicad.buildgraph/1`), delegation/routing (`aicad.delegation/1`), effort tiers
(`aicad.effort/1`), mission workspace (`aicad.mission`), and PLM vault (`aicad.plm/1`).

---

## 1. The core problem and the thesis

A small local model fails at CAD not because it lacks knowledge but because it cannot hold
the **whole abstraction ladder** in one context and reason across all of it at once:

    user intent → functional features → geometry → topology/linkage → build order → CAD ops

It skips rungs, hallucinates references, forgets the goal, and — worst — *guesses*, then
gets bounced by a checker into an endless edit-guess loop.

**Thesis:** treat design like a compiler lowering intent through IR stages. Each rung of the
ladder is a **discrete stage with a fresh, scoped context** that receives exactly what it
needs from a **protected source of truth (the design ledger)**, does **one cognitive job**,
and writes structured results back. At every stage we **punch through the abstraction the
model can't hold** by (a) pre-decomposing the thinking into steps, (b) **proactively loading
the exact helper/calculator/data/tool the current micro-task needs** so the model never has
to recall or derive it, (c) convening **councils** at decision points to beat single-model
blind spots, and (d) running an **overseer** that audits the chain-of-thought and re-grounds
or re-routes — *diagnosing* problems into targeted corrections rather than blind retries.

The same machinery **speeds up** a frontier model instead of constraining it: stages become
single-pass checkpoints it satisfies and skips, helpers become optional accelerators, and the
scaffolding dials down via effort/auto-mode. **Flexibility is a first-class requirement.**

---

## 2. The design-thinking pipeline (the spine)

Six stages, refining the existing `SPECIFY → ORGANIZE → PLAN → BUILD` span. Each stage is a
role with a rich system prompt, a scoped context recipe, proactive helpers, an optional
council, and an overseer audit gate. Each writes typed events to the ledger (§4).

### Stage 0 — INTENT CAPTURE  *(role: intake_analyst — exists)*
Parse the request into **requirements, constraints, material, manufacturing process, use
environment, acceptance criteria**. Material/functional ambiguity → ask the user; immaterial
ambiguity → sensible default recorded as an assumption. **Writes:** `brief`, `requirement`.

### Stage 1 — FUNCTIONAL DECOMPOSITION  *(role: feature_architect — NEW, T-DP2)*
Enumerate every **function** the part must perform — mount, mate, seal, bear load, clear,
fasten, locate, dissipate heat — *before any geometry*. This is the rung local models skip.
- **Output:** `feature[]` = `{id, function, driving_requirement, interfaces_with, criticality}`.
- **Helpers:** part-class functional checklists (bracket / housing / shaft / plate / enclosure /
  fitting…); **requirement-coverage checker** (every requirement maps to ≥1 feature, every
  feature traces to a requirement or a recorded assumption).
- **Council (completeness critic):** "what function did we miss?"

### Stage 2 — GEOMETRY REALIZATION  *(role: geometry_designer — NEW, T-DP3; fresh context per feature/cluster)*
For each feature, decide the **concrete geometry** that realizes it and the **critical
dimensions**, *gated by material and process*. "load rib" → gusset sketch + extrude; "shaft
seat" → journal Ø with computed fit; "seal" → revolved O-ring groove sized to standard.
- **Output:** `geometry_spec` per feature = `{shapes, nominal_dims, tolerances, datum_refs,
  provenance}`.
- **Proactive op-aware helpers (the heart of "advise, don't recall"):** the *correct*
  calculator/data auto-loaded for the geometry kind — `fit_calc`/`recommend_fit` for mating
  bores, `research_thread_spec` for threads, `wall_thickness`/process minimums for walls,
  bend allowance for sheet, draft for molded, `research_material_lookup` +
  `research_standard_section` + `research_web_search` for lookups. **Never recall a number
  from memory — every dim carries provenance.**
- Manufacturing/material considerations are *inputs to dimensioning*: machined parts get
  machining tolerances, printed parts get printed minimums pulled from process/DFM data.

### Stage 3 — TOPOLOGY / LINKAGE  *(role: linkage_planner — NEW, T-DP4)*
Decide **how the geometry links**: the datum scheme, reference chains (which feature
references which), shared interfaces, parametric relationships, sketch-on-face vs
sketch-on-plane. Produces the skeleton the buildgraph orders.
- **Output:** `linkage` = `{datum_tree, reference_edges[], shared_interfaces[]}`.
- **Helpers:** reference-integrity checker (no dangling refs, no cycles), datum-completeness,
  **interface-consistency** (two features sharing a bore must agree on Ø/tolerance).
- **Council (regen-stability lens):** "is this reference scheme robust to later edits?"

### Stage 4 — BUILD-PATH PLANNING  *(role: build_strategist / orchestrator / layout_engineer — exists + buildgraph)*
Turn the linkage graph into an **ordered construction pathway**: base-feature and orientation
choice, feature order so every reference exists when built, sketch planes, pattern/mirror
opportunities, minimal-setup orientation (helps downstream CAM).
- **Output:** an `aicad.buildgraph/1` DAG — ordered ops with dependencies, **validated**
  (cycles, missing refs) and **preflighted** (T-W12 shadow-clone dry-run per op).
- **Council (build-strategy — exists):** MVP-first vs robustness-first vs manufacturing-first,
  synthesized by a judge.

### Stage 5 — EXECUTION  *(role: feature_modeler workers — exists; fan-out over the build DAG)*
Fresh-context worker models execute each op/op-cluster via CAD tools, guided by the geometry
spec, **injected tool schemas (T-W32)**, proactive per-op advisories, and **auto-applied safe
fixes** (auto-coincident, auto-constrain). Multiple workers run in parallel over independent
DAG branches and across multiple documents (assembly parts) via the mission workspace.

---

## 3. Cross-cutting layers (apply to every stage)

### 3.1 Overseer audit + error correction  *(role: overseer — exists; T-DP5)*
After each stage the overseer inspects the **plan and the chain-of-thought** for errors,
**self-identifies problems**, and routes a *targeted correction back to the specific stage
that owns it* — never a blind full retry. It also maps a **user-requested change** ("make the
wall thicker", "add a mounting tab") to the exact stage/feature to re-enter, so fixes are
implemented properly rather than re-derived from scratch. Overseer holds and re-grounds the
**master intent** so fresh-context stages never drift from the goal.

### 3.2 Advisory helpers — "advise, don't guess-loop"  *(contract: advisor.md; T-DP6)*
Every reactive checker becomes a **proactive/diagnostic advisor**. The interference checker
does not merely fail the loop — it emits: *"boss `$3` collides with cut `$7` by 2.1 mm along
+Z; most likely the boss height; reduce to ≤ X or move the cut to Y"* **plus the tool to fix
it**. Advisors fire **before** an op (inject the right calculator/data/context for what the
model is about to do) and **after** (diagnose, target the fix). This is what converts endless
guess-loops into one-shot corrections. Every acceptance criterion maps to a concrete check
invocation; interface components carry extra context + checkers and are audited on failure.

### 3.3 Guidance councils  *(contract: aicad.council/1; T-DP8)*
N models debate a decision; a judge synthesizes. **Councils are available at every stage and
configurable** (Hunter). Defaults adapt to task-breakdown needs and cross-department
dependencies; local tier runs them **wide** (free tokens), frontier runs **narrow or single**.
When multiple model sources exist, councils prefer **diverse models** over temperature-only
diversity. Council policy is per-stage, per-tier, and overridable in Advanced mode.

### 3.4 Hierarchical delegation  *(contract: aicad.delegation/1 + routing; T-DP9)*
An overseer/lead spawns lesser sub-agents; **sub-agents may spawn their own sub-agents
recursively with no hard cap**, and every delegation auto-approves at every depth. The only
rails are a **human-visible budget meter and a kill-switch** (runaway
protection), plus per-role model routing. Sub-agent **model preferences are a GUI setting**
(T-DP12). Each agent may hold **multiple open documents** so the user can watch every
sub-agent build its part of an assembly simultaneously (T-DP13).

### 3.5 Sketch & GSL accuracy helpers  *(T-DP7)*
The execution rung lives or dies on sketch quality:
- **Auto-coincident** touching endpoints on every edit (`sketch.auto_coincident`, exists —
  harden for closed-path detection).
- **Auto-constrain** toward DOF 0; **critical-dimension scheme** chosen with manufacturing
  method + material in mind (a machined bore vs a printed hole get different schemes).
- **GSL status → text**: a rich text read of sketch/model state (closure, remaining DOF in
  plain words, an ordered relations-then-dimensions schedule) so a no-vision local model can
  *see* what it built and what's left. This is the feedback channel that makes the loop
  converge.

### 3.6 Effort, auto-mode, and the tuning loop  *(contract: aicad.effort/1; T-DP10)*
- **Auto-mode (default):** a **workflow router** classifies the task and lets the model
  self-select how much of the pipeline to use. **Local models default to the full
  tokenmaxxing setup.**
- **Advanced mode:** exposes every stage/council/helper knob to experienced users.
- **The tuning loop:** the **gym bench harness** (`tools/flywheel/bench/gym/`) runs example
  tasks across workflow configurations and **learns which scaffold level is best per
  task-class**, feeding the auto-mode classifier. "Figure out the best way to apply the
  workflows to each example task" (Hunter) is a measured, ongoing process — never a guess.

### 3.7 Transparency  *(T-DP11)*
Every helper, advisory, council vote, tool call, auto-applied fix, and delegation is
**visible and inspectable in a UI transparency panel** — what fired, why, what it injected,
what it changed — so issues can be troubleshot after the fact.

### 3.8 PLM integration  *(contract: aicad.plm/1; T-DP14)*
The pipeline runs **inside the vault**. Stage boundaries cut checkpoints; the design rationale
(feature → geometry → linkage → build decisions, straight from the ledger) becomes the PLM
"why" narrative (T-R3 summaries). Multi-part assembly work is attributed per participant.

---

## 4. The source of truth: the design ledger

The **ledger (`aicad.ledger/1`) is the single protected source of truth**. Fresh-context
stages never see the whole history — they get a **scoped slice** (the mission-dispatch layer,
T-W30/W32) and write back typed events. New/confirmed event kinds for the pipeline:

| kind | written by | payload |
|---|---|---|
| `brief` / `requirement` | intake | request text, acceptance criteria |
| `feature` | feature_architect | function, driving_requirement, interfaces_with, criticality |
| `geometry_spec` | geometry_designer | shapes, nominal_dims, tolerances, datum_refs, **provenance** |
| `linkage` | linkage_planner | datum_tree, reference_edges, shared_interfaces |
| `build_node` | build_strategist | op, deps, orientation, sketch_plane |
| `decision` | any | choice + driver + needs_confirm |
| `advisory` / `audit` | advisors / overseer | finding, diagnosis, targeted fix, tool |
| `dimension` | any | value + tolerance + **provenance** (rejected without it) |

**Rule:** a value that exists only in a model's chat/handoff **does not exist**. The stage
gate reads the *ledger*, not the prose. (This is the exact W32 finding that stalled the gym at
SPECIFY; the spec_writer card now enforces ledger-first — every stage inherits that rule.)

---

## 5. Contract & schema (frozen in `contracts/design_pipeline.md`, aicad.designpipe/1 — T-DP1)

- Stage IO schemas (the typed handoffs above) and the ledger event kinds.
- Stage-gate definitions (what makes each stage "complete" — read from the ledger).
- How the six stages nest inside `phases.py` and reuse its gates.
- Effort/auto-mode hooks (which stages/councils/helpers a scaffold level enables).
- The advisor trigger bindings (`aicad.trigger/1`): geometry-kind → helper set.

---

## 6. Flexibility guarantees (never choke a good model)

1. Every stage is a **skippable checkpoint**: a model that satisfies it in one pass advances
   immediately; nothing forces N steps.
2. Helpers are **offered, not imposed** — a frontier model can decline and proceed.
3. **Auto-mode** lets the model pick its own depth; **Advanced mode** hands control to the
   user; **local default = full scaffold**.
4. Council breadth and delegation depth are **config, per-tier**, not hard-coded.
5. The pipeline **adds context and speed, never a fixed modeling path** — two models given the
   same brief may build different (valid) parts; the pipeline judges by the ledger + verify
   battery, not by matching a template.

---

## 7. Why this beats the competition (design intent)

Commercial CAD copilots bolt an LLM onto a command API and hope. We instead **give the model
the translations, context, and assistance to actually do the job**: the abstraction ladder is
pre-decomposed, the exact tool/calculator/data for the current micro-task is in front of the
model automatically, councils cover its blind spots, an overseer catches its mistakes and
re-routes them precisely, and a text channel lets a no-vision local model *see* its own work.
The result is that a **free local model** can produce a coherent, verified, manufacturable,
version-controlled part — and a frontier model does it faster and more accurately than it
could unaided.

---

## 8. Implementation map (workstream D)

| task | title | deps |
|---|---|---|
| T-DP1 | Freeze `contracts/design_pipeline.md` (aicad.designpipe/1) + wire stages into phases.py | — |
| T-DP2 | feature_architect role + functional-decomposition helpers + coverage checker | T-DP1 |
| T-DP3 | geometry_designer role + **proactive op-aware helper/calculator registry** | T-DP1 |
| T-DP4 | linkage_planner role + reference-integrity/datum/interface checkers | T-DP1 |
| T-DP5 | Overseer audit-&-reroute loop + user-change → stage mapping | T-DP1 |
| T-DP6 | Advisory expansion — every checker → diagnostic advisor w/ targeted fix + tool | T-DP1 |
| T-DP7 | Sketch-completion + GSL-status-to-text + critical-dim scheme helpers | — |
| T-DP8 | Council policy engine — per-stage, per-tier, diverse-model, configurable | T-DP1 |
| T-DP9 | Hierarchical delegation (overseer-discretion recursion) + budget meter/kill-switch | T-DP1 |
| T-DP10 | Auto-mode workflow router + gym-based tuning loop | T-DP2,3,4,8 |
| T-DP11 | GUI transparency panel (helpers/advisories/councils/delegation/tools) | T-DP6 |
| T-DP12 | GUI sub-agent model-preference settings + effort auto/advanced UI | T-DP9 |
| T-DP13 | Multi-file agent view — watch N agents build N parts simultaneously | — |
| T-DP14 | Design-pipeline ↔ PLM wiring (stage checkpoints, rationale narrative) | T-DP1, R5 |

Sequencing: **T-DP1 first (blocks the role/overseer/council/delegation tier).** T-DP7 and T-DP13
are independent and can start immediately. T-DP2/3/4 (roles) parallel after D1. D5/D6
(overseer/advisory) after D1. D8/D9 after roles. D10 after roles+councils. GUI (D11/12/13)
after their backend seams. D14 after the R-stack PLM GUI (R5) matures.

---

## 9. Presentation & workspaces (Hunter directive, 2026-07-05)

The AI system needs a real home in the UI, and the app needs clean switching between its
major modes. Two principles: **full-viewport workspaces** for modes that own the screen
(ECAD today; AI-Agent and PLM-management next), and a **persistent glass copilot overlay**
for modes where you model while chatting (Modeling, Sim, CAM, Drawing).

### 9.1 Workspace-switching architecture  *(T-DP15)*
A single registry of **workspaces**, each declaring its toolbar/menu context and whether it
is **full-viewport** (owns the screen, like ECAD) or **overlay-hosting** (viewport + the
pinned glass copilot column). Switching is a first-class action in the workspace bar
(existing `workspace_bar.cpp`, `host/src/`). Registered workspaces: Modeling, Sketch, Assembly,
Drawing, Simulation, CAM, **ECAD** (full-viewport, exists), **AI-Agent** (full-viewport,
NEW), **PLM-management** (full-viewport project hub, NEW — the cross-project view; per-doc
PLM stays inline via R5), Settings stays a **modal** (consolidated, T-G22 — not a workspace).
Each workspace restores its own toolbar, panels, and camera/scene context on entry.

### 9.2 The AI-Agent workspace (full-screen comprehensive copilot)  *(T-DP16)*
A dedicated full-viewport workspace — like ECAD's takeover — for driving and managing the
agent system. **The glass copilot overlay stays exactly as it is in the modeling section
(same styling)** for chatting while you model; the AI-Agent workspace is the *comprehensive*
surface you switch to when you want to see and steer the whole operation. It contains:
- **The interactive DAG as the centerpiece** (§9.3) — the live mission/build graph.
- **Worker steering**: pause/resume/redirect/kill any agent or sub-agent; adjust a role's
  model or effort on the fly.
- **Session management**: list, switch, fork, and resume agent sessions; the multi-file view
  (T-DP13) so several parts under concurrent work are visible at once.
- **The transparency stream** (T-DP11): helpers/advisories/councils/tool-calls, inline.
Switching INTO this workspace replaces the viewport with the comprehensive view; switching
out returns to the model with the overlay copilot intact.

### 9.3 Interactive agent DAG / flowchart widget  *(T-DP17)*
A clickable node-graph of the running workflow: mission phases, the build DAG
(`aicad.buildgraph/1`), and the **delegation tree** (which agent spawned which sub-agent).
Clicking a node opens that element's detail — the sub-agent's role, model, current tool call,
ledger slice, token spend, and status (queued / running / blocked / done / failed). Live
status colouring; the same widget embeds small in the overlay copilot (compact) and large in
the AI-Agent workspace (full). Reuses the mission workspace event streams (`aicad.mission`)
and the delegation tree (`aicad.delegation/1`). Glass design language, dark-mode contrast.

### 9.4 AI time-remaining estimator  *(T-DP18)*
A live **ETA to part-complete**: `remaining_tokens / effective_tokens_per_sec`, where
`remaining_tokens` = Σ over not-yet-done subtasks of their **expected token cost** (from the
gym-measured per-role/per-stage token tables — the same measurements W31/W32 recorded, fed by
the tuning loop T-DP10), and `effective_tokens_per_sec` = the live aggregate rate across
running slots (the CUDA server's measured throughput). Shown as a **status-bar clock** (whole
mission) and as **per-node ETAs on the DAG** (§9.3). Honest about uncertainty: shows a range
when a subtask has few historical samples; never invents a number. Frontier-model runs use the
provider's observed rate.

### 9.5 Sequencing & collision note
`workspace_bar.cpp` / doc-tabs / `application.cpp` are shared GUI surfaces — DP13 (multi-file)
lands first, then DP15 (workspace framework), then DP16 (AI workspace) builds on both. DP17
(DAG widget) and DP18 (estimator) are more self-contained (a new widget; a backend calc + a
status-bar element) and can proceed alongside. All `host/src/` only (single GUI target).

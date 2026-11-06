# Agent Design Pipeline — the guided design-thinking architecture

This document records the design pipeline and its rationale, not an as-built
specification. Online model execution runs inside the official OMP RPC runtime;
see [OMP integration](../dev/omp-integration.md). The council layer it describes
(§3.3) has been removed.

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
and writes structured results back. At every stage the pipeline **punches through the abstraction the
model can't hold** by (a) pre-decomposing the thinking into steps, (b) **proactively loading
the exact helper/calculator/data/tool the current micro-task needs** so the model never has
to recall or derive it, and (c) running an **overseer** that audits the chain-of-thought and re-grounds
or re-routes — *diagnosing* problems into targeted corrections rather than blind retries.

The same machinery **speeds up** a frontier model instead of constraining it: stages become
single-pass checkpoints it satisfies and skips, helpers become optional accelerators, and the
scaffolding dials down via effort/auto-mode. **Flexibility is a first-class requirement.**

---

## 2. The design-thinking pipeline (the spine)

Six stages, refining the existing `SPECIFY → PLAN → BUILD` span. Each stage is a
role with a rich system prompt, a scoped context recipe, proactive helpers,
and an overseer audit gate. Each writes typed events to the ledger (§4).

### Stage 0 — INTENT CAPTURE  *(role: intake_analyst — exists)*
Parse the request into **requirements, constraints, material, manufacturing process, use
environment, acceptance criteria**. Material/functional ambiguity → ask the user; immaterial
ambiguity → sensible default recorded as an assumption. **Writes:** `brief`, `requirement`.

### Stage 1 — FUNCTIONAL DECOMPOSITION  *(role: feature_architect)*
Enumerate every **function** the part must perform — mount, mate, seal, bear load, clear,
fasten, locate, dissipate heat — *before any geometry*. This is the rung local models skip.
- **Output:** `feature[]` = `{id, function, driving_requirement, interfaces_with, criticality}`.
- **Helpers:** part-class functional checklists (bracket / housing / shaft / plate / enclosure /
  fitting…); **requirement-coverage checker** (every requirement maps to ≥1 feature, every
  feature traces to a requirement or a recorded assumption).
- **Council (completeness critic):** "what function did we miss?"

### Stage 2 — GEOMETRY REALIZATION  *(role: geometry_designer; fresh context per feature/cluster)*
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

### Stage 3 — TOPOLOGY / LINKAGE  *(role: linkage_planner)*
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
  (cycles, missing refs) and **preflighted** (shadow-clone dry-run per op).
- **Council (build-strategy — removed, see §3.3):** MVP-first vs robustness-first vs
  manufacturing-first, synthesized by a judge.

### Stage 5 — EXECUTION  *(role: feature_modeler workers — exists; fan-out over the build DAG)*
Fresh-context worker models execute each op/op-cluster via CAD tools, guided by the geometry
spec, **injected tool schemas**, proactive per-op advisories, and **auto-applied safe
fixes** (auto-coincident, auto-constrain). Multiple workers run in parallel over independent
DAG branches and across multiple documents (assembly parts) via the mission workspace.

---

## 3. Cross-cutting layers (apply to every stage)

### 3.1 Overseer audit + error correction  *(role: overseer)*
After each stage the overseer inspects the **plan and the chain-of-thought** for errors,
**self-identifies problems**, and routes a *targeted correction back to the specific stage
that owns it* — never a blind full retry. It also maps a **user-requested change** ("make the
wall thicker", "add a mounting tab") to the exact stage/feature to re-enter, so fixes are
implemented properly rather than re-derived from scratch. Overseer holds and re-grounds the
**master intent** so fresh-context stages never drift from the goal.

### 3.2 Advisory helpers — "advise, don't guess-loop"  *(contract: advisor.md)*
Every reactive checker becomes a **proactive/diagnostic advisor**. The interference checker
does not merely fail the loop — it emits: *"boss `$3` collides with cut `$7` by 2.1 mm along
+Z; most likely the boss height; reduce to ≤ X or move the cut to Y"* **plus the tool to fix
it**. Advisors fire **before** an op (inject the right calculator/data/context for what the
model is about to do) and **after** (diagnose, target the fix). This is what converts endless
guess-loops into one-shot corrections. Every acceptance criterion maps to a concrete check
invocation; interface components carry extra context + checkers and are audited on failure.

### 3.3 Guidance councils  *(removed)*
The council layer (`aicad.council/1`, `council_convene`) was removed with
`services/agent/councils.py`; the per-stage "Council" bullets above are design
history. Multi-angle review is available through ponder delegations
(`aicad.delegation/1`) and the advisory/overseer audits.

### 3.4 Hierarchical delegation  *(contract: aicad.delegation/1 + routing)*
An overseer or lead may delegate work to sub-agents. Agents can work across the
workspace; edited parts already open to the user update in place, while other
parts are shown when the user opens them or the agent presents them.

### 3.5 Sketch & GSL accuracy helpers
The execution rung lives or dies on sketch quality:
- **Auto-coincident** touching endpoints on every edit (`sketch.auto_coincident`, exists —
  harden for closed-path detection).
- **Auto-constrain** toward DOF 0; **critical-dimension scheme** chosen with manufacturing
  method + material in mind (a machined bore vs a printed hole get different schemes).
- **GSL status → text**: a rich text read of sketch/model state (closure, remaining DOF in
  plain words, an ordered relations-then-dimensions schedule) so a no-vision local model can
  *see* what it built and what's left. This is the feedback channel that makes the loop
  converge.


### 3.7 Transparency
Every helper, advisory, council vote, tool call, auto-applied fix, and delegation is
**visible and inspectable in a UI transparency panel** — what fired, why, what it injected,
what it changed — so issues can be troubleshot after the fact.

### 3.8 PLM integration  *(contract: aicad.plm/1)*
The pipeline runs **inside the vault**. Stage boundaries cut checkpoints; the design rationale
(feature → geometry → linkage → build decisions, straight from the ledger) becomes the PLM
"why" narrative (AI-drafted history summaries). Multi-part assembly work is attributed per
participant.

---

## 4. The source of truth: the design ledger

The **ledger (`aicad.ledger/1`) is the single protected source of truth**. Fresh-context
stages never see the whole history — they get a **scoped slice** (the mission-dispatch
layer) and write back typed events. New/confirmed event kinds for the pipeline:

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
gate reads the *ledger*, not the prose. (Benchmark runs stalled at SPECIFY exactly this way
until the spec_writer card enforced ledger-first — every stage inherits that rule.)

---

## 5. Contract & schema (frozen in `contracts/design_pipeline.md`, aicad.designpipe/1)

- Stage IO schemas (the typed handoffs above) and the ledger event kinds.
- Stage-gate definitions (what makes each stage "complete" — read from the ledger).
- How the six stages nest inside `phases.py` and reuse its gates.
- Effort/auto-mode hooks (which stages/helpers a scaffold level enables).
- The advisor trigger bindings (`aicad.trigger/1`): geometry-kind → helper set.

---

## 6. Flexibility guarantees (never choke a good model)

1. Every stage is a **skippable checkpoint**: a model that satisfies it in one pass advances
   immediately; nothing forces N steps.
2. Helpers are **offered, not imposed** — a frontier model can decline and proceed.
3. Adapt the workflow depth to the task without imposing a fixed scaffold.
4. Council breadth and delegation depth are **config, per-tier**, not hard-coded.
5. The pipeline **adds context and speed, never a fixed modeling path** — two models given the
   same brief may build different (valid) parts; the pipeline judges by the ledger + verify
   battery, not by matching a template.

---

## 7. Why this beats the competition (design intent)

Commercial CAD copilots bolt an LLM onto a command API and hope. This pipeline instead **gives the model
the translations, context, and assistance to actually do the job**: the abstraction ladder is
pre-decomposed, the exact tool/calculator/data for the current micro-task is in front of the
model automatically, an overseer catches its mistakes and
re-routes them precisely, and a text channel lets a no-vision local model *see* its own work.
The result is that a **free local model** can produce a coherent, verified, manufacturable,
version-controlled part — and a frontier model does it faster and more accurately than it
could unaided.

---


## 9. Presentation & workspaces (2026-07-05)

The AI system needs a real home in the UI, and the app needs clean switching between its
major modes. Two principles: **full-viewport workspaces** for modes that own the screen
(ECAD today; AI-Agent and PLM-management next), and a **persistent glass copilot overlay**
for modes where you model while chatting (Modeling, Sim, CAM, Drawing).

### 9.1 Workspace-switching architecture
A single registry of **workspaces**, each declaring its toolbar/menu context and whether it
is **full-viewport** (owns the screen, like ECAD) or **overlay-hosting** (viewport + the
pinned glass copilot column). Switching is a first-class action in the workspace bar
(now the title-bar workspace tabs in `host/src/`). Registered workspaces: Modeling, Sketch,
Assembly, Drawing, Simulation, CAM, **ECAD** (full-viewport, exists), **AI-Agent**
(full-viewport, NEW), **PLM-management** (full-viewport project hub, NEW — the cross-project
view; per-doc PLM stays inline), Settings stays a consolidated **modal** (not a workspace).
Each workspace restores its own toolbar, panels, and camera/scene context on entry.

### 9.2 The AI-Agent workspace (full-screen comprehensive copilot)
A dedicated full-viewport workspace — like ECAD's takeover — for driving and managing the
agent system. **The glass copilot overlay stays exactly as it is in the modeling section
(same styling)** for chatting while you model; the AI-Agent workspace is the *comprehensive*
surface you switch to when you want to see and steer the whole operation. It contains:
- **The interactive DAG as the centerpiece** (§9.3) — the live mission/build graph.
- **Worker steering**: pause/resume/redirect/kill any agent or sub-agent; adjust a role's
  model or effort on the fly.
- **Session management**: list, switch, fork, and resume agent sessions; the multi-file view
  so several parts under concurrent work are visible at once.
- **The transparency stream**: helpers/advisories/tool-calls, inline.
Switching INTO this workspace replaces the viewport with the comprehensive view; switching
out returns to the model with the overlay copilot intact.

### 9.3 Interactive agent DAG / flowchart widget
A clickable node-graph of the running workflow: mission phases, the build DAG
(`aicad.buildgraph/1`), and the **delegation tree** (which agent spawned which sub-agent).
Clicking a node opens that element's detail — the sub-agent's role, model, current tool call,
ledger slice, token spend, and status (queued / running / blocked / done / failed). Live
status colouring; the same widget embeds small in the overlay copilot (compact) and large in
the AI-Agent workspace (full). Reuses the mission workspace event streams (`aicad.mission`)
and the delegation tree (`aicad.delegation/1`). Glass design language, dark-mode contrast.



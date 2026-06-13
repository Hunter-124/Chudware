# AI-CAD → AI-ECAD Expansion — Master Plan

> Status: **DRAFTED autonomously 2026-06-13** (user away; decisions made with rationale and flagged
> for review — see §2). Sibling of `docs/PLAN.md`. Canonical execution reference for the EDA module.
> Contracts live in `docs/contracts/ecad/` and change only via explicit version bump.

## 0. One-paragraph thesis

Add KiCad-class electronics design — **schematic capture → netlist → PCB layout → DRC →
fabrication output → 3D board model** — to AI-CAD, plus an **electrical simulator that is better
than KiCad's** (native, GPL-clean, tightly coupled to the schematic). The whole thing reuses the
existing CAD architecture wholesale (Python-authority document model, ops/revision/undo, worker
RPC, ImGui host mirror, MCP/agent tools) so it feels like one program. The headline integration:
**finish a board, press one button, and the populated PCB drops into the 3D CAD workspace as an
import feature** so the user designs the enclosure around it with the modeling tools that already
exist — with AI assisting end-to-end. We do **not** vendor KiCad as a backend; we clean-room the
concepts and parse its file formats so users keep their libraries.

## 1. Context (what we're building on)

AI-CAD is a SolidWorks-class parametric CAD app (see `docs/PLAN.md`). The architecture we extend:

- **Python is the sole authority** (`engine/`); the C++ GLFW/ImGui host (`host/`) holds a read-only
  mirror and submits ops. → *An entire EDA engine can be built and verified headlessly, with the
  GUI as a later, separable layer.* This is the spine of the autonomous build plan.
- **Document model** (`engine/doc/`): JSON-serializable docs, an ordered feature history, a
  **compiler** that evaluates features sequentially with per-feature caching/timing/error-
  containment, **selectors** (geometric-signature references re-resolved each rebuild), a
  generic numeric solver **`nsolve.py`** (residuals + analytic Jacobian + damped Newton/LM), and a
  **transactional ops vocabulary** (`engine/doc/ops.py`) that is simultaneously the GUI command set
  and the AI tool schema.
- **Worker RPC** (`engine/worker.py`): newline-delimited JSON-RPC over stdio, protocol 2, dispatched
  via a `_METHODS` dict. A **second sim worker** isolates long-running analysis (FEA/CFD) so it
  can't block or crash the document session.
- **Container** (`.step+`, `engine/doc/stepplus.py`): a ZIP of `document.json` + `geometry.step` +
  meta + thumbnail, forward-compatible (unknown entries preserved).
- **Host UI** (`host/src/`): dockspace + ribbon + FeatureManager tree + PropertyManager (PM forms
  with a field-kind system) + OpenGL viewport (AICM mesh + R32UI ID-buffer picking). **Sketch mode
  already draws 2D with ImGui draw lists** — directly reusable for schematic/PCB canvases.
- **AI** (`agent/`, `mcp_server/`): an iterative loop tuned for *weak local models* (4–8k context),
  with editable markdown prompts, stagnation/fail-streak detectors, automatic context compaction,
  and a discipline of feeding the model **rich, actionable feedback** (dims, fill %, fit reports,
  "what to do next" nudges). Providers: Anthropic (`claude-opus-4-8`), OpenAI, local LM Studio.

## 2. Locked decisions (made autonomously; **flagged for user review** ⚑)

1. **Clean-room, GPL-clean.** ⚑ *(highest-stakes — confirm on return.)* KiCad is **GPLv3**.
   `docs/contracts/licenses.md` already mandates a "GPL-clean except where isolated" posture. We
   therefore **copy no KiCad source**. We learn its architecture, reimplement original code, and
   **parse its file formats** (reading a documented S-expression format is interop, not derivation).
   Users import **their own installed** KiCad symbol/footprint/3D libraries; we ship a small
   **clean-room starter library** (passives + IPC-7351-geometry footprints, which are public
   standards). KiCad's official library *content* is CC-BY-SA-4.0 with a design-output exception —
   we do not redistribute it in the repo.
2. **Native simulator, not ngspice.** ⚑ This satisfies *"improve on the electrical simulations"*
   AND stays GPL-clean (ngspice is GPL with mixed BSD parts). We build a **Modified Nodal Analysis**
   engine in numpy/scipy (BSD): DC operating point, DC sweep, transient (companion models +
   Newton), and AC small-signal — plus tight schematic coupling KiCad lacks (click-a-wire-to-probe,
   live waveform overlays, AI-assisted setup). **ngspice remains a documented, subprocess-isolated
   optional backend** (same policy as gmsh) for users who want its mature device library — *not
   bundled by default*. We import/export SPICE netlists either way for interop.
3. **Native ECAD documents, KiCad import/export for interop.** Primary format is our own JSON doc
   model in a `.step+`-style ZIP (`.ecad+`). We **import** `.kicad_sch` / `.kicad_pcb` /
   `.kicad_sym` / `.kicad_mod` and **export** the board/netlist back, so the program is useful to
   the large installed KiCad base without lock-in.
4. **Mirror the CAD architecture exactly.** New code lives in `engine/ecad/` and mirrors
   `engine/doc/` file-for-file in spirit (schema/ops/session/compiler-analog/selectors/formats).
   Same revision/undo/transactionality. Same worker-RPC and PM-form patterns. **No new paradigms** —
   an ECAD doc is "just another document type" to the host mirror and AI tools.
5. **Build order: engine-first, headless-verifiable, UI last.** Foundation → netlist/ERC → sim +
   DRC + fab + 3D-bridge → AI tools → C++ UI. Everything through the 3D bridge is verified by
   pytest with analytic golden cases and needs no GUI. The C++ editor is the final, separable wave.
6. **AI gets automatic context helpers everywhere.** ⚑ Per the user's core mandate ("the AI is
   extremely limited"): every ECAD tool returns structured *state + what's wrong + what to do next*,
   ERC/DRC run automatically and feed back inline, and a compact `ecad_describe` gives a weak model
   a 4–8k-token picture of the design. Tools are designed so a small model can succeed by following
   the hints, the same way the CAD loop's `_feedback_ok` guides it today.

## 3. Architecture

### 3.1 Module layout (new, conflict-isolated — see §6)

```
engine/ecad/
  schema.py           SchematicDocument (aicad.sch/1) + BoardDocument (aicad.pcb/1) validators
  ops.py              closed ECAD op vocabulary (GUI command set == AI tool schema)
  session.py          EcadSession: multi-doc registry, revision, bounded undo/redo (mirrors doc/session.py)
  netlist.py          connectivity graph: pins↔wires↔labels↔buses → nets; the sch→pcb contract
  erc.py              electrical rule check (unconnected pins, output-output, no-driver, ...)
  drc.py              design rule check (clearance, track width, annular ring, hole, courtyard, unrouted)
  selectors.py        ECAD reference resolution (pin/pad/net/segment) — analog of doc/selectors.py
  threed.py           PCB → STEP solid (board + copper + placed 3D component models): the housing bridge
  formats.py          .ecad+ container save/open; KiCad import/export; netlist export
  library/
    sexpr.py          tiny S-expression reader/writer (clean-room; used by KiCad + native parsers)
    kicad_sym.py      .kicad_sym symbol library importer
    kicad_mod.py      .kicad_mod / .pretty footprint importer
    native.py         native symbol/footprint/3D library format + loader
    starter/          clean-room starter library (R/C/L/diode/LED/header/IC + IPC footprints)
  fab/
    gerber.py         RS-274X copper/mask/silk/outline writers
    excellon.py       NC drill writer
    bom.py            BOM (CSV/JSON) grouped by value+footprint
    placement.py      pick-and-place (CPL) writer
  rpc.py              ecad_* worker methods; registered into engine/worker.py via one append hook
engine/sim/
  circuit.py          MNA solver: DC op / DC sweep / transient / AC  (NEW — sim worker)
  devices.py          device companion models: R L C V I D BJT MOSFET OPAMP E F G H switches
  spice.py            SPICE netlist <-> internal circuit import/export
mcp_server/
  ecad_tools.py       @mcp.tool() EDA tools with rich context helpers (imported by server.py)
host/src/ui/ecad/     C++ editor (Wave D): sch_canvas, pcb_canvas, layers_panel, erc_drc_panel,
                      sim_panel, ecad_forms (PM forms), ecad_ribbon
docs/contracts/ecad/  frozen contracts (M0)
tests/ecad/           headless pytest suites + fixtures
```

### 3.2 Document models (summary; full spec in contracts)

- **SchematicDocument** `aicad.sch/1`: `sheets[]` (hierarchical), `symbols[]` (placed component
  instances: lib ref, ref-des, value, footprint assoc, pins with positions), `wires[]`,
  `junctions[]`, `labels[]` (local/global/hierarchical net labels), `buses[]`, `power[]` symbols,
  `no_connect[]`, `text[]`/graphics. Authoritative for connectivity.
- **BoardDocument** `aicad.pcb/1`: `stackup` (layers: copper count, dielectric, thickness),
  `outline` (board edge polygon/arcs), `footprints[]` (placed: ref-des, pads with net, position,
  rotation, layer), `tracks[]` (segment/arc: net, width, layer), `vias[]` (net, drill, pad,
  layers), `zones[]` (copper pour: net, layer, polygon, clearance, thermal), `rules` (clearance,
  track/via min, etc.), `graphics[]` (silk/fab/courtyard). Net assignments tie back to the netlist.
- **Netlist** (derived, not stored): `{nets: {name: [pinrefs]}, components: [...], by_pin: {...}}`.
  Built from the schematic by graph traversal; consumed by ERC, the PCB net-binding, simulation,
  and SPICE export. This is the **single source of connectivity truth** shared sch↔sim↔pcb.

### 3.3 The "compiler" analog

CAD compiles a feature history into a B-rep solid. ECAD has three compile-like passes, each cached
and incremental like `doc/compiler.py`:

- **Schematic compile** → netlist + ERC report (graph build; cache keyed on sch revision).
- **Board compile** → resolved net bindings + DRC report + 2D render geometry per layer + a
  ratsnest (unrouted net airwires). Cache keyed on pcb revision.
- **3D compile** → an OCP/build123d solid (board substrate + copper + soldermask + placed component
  STEP models), exported as STEP and registered as an `import` feature in a CAD `aicad.part/1` doc.

### 3.4 Simulation (the differentiator)

`engine/sim/circuit.py` runs in the **sim worker** (long transients never block editing). Inputs:
the netlist + a `simulation` study block (analyses + probes). Engine = **MNA with companion
models**: stamp G/C matrices, Newton-Raphson for nonlinear devices, trapezoidal integration
(with backward-Euler bootstrap) for transient, complex-MNA for AC. Devices in `devices.py`:
R, L, C, independent V/I (DC/AC/PULSE/SIN/PWL/EXP), controlled sources (E/F/G/H), diode
(Shockley + series R + junction cap), BJT (Ebers-Moll/Gummel-Poon-lite), MOSFET (level-1 + simple
short-channel), ideal + macromodel op-amp, voltage/current-controlled switches. Results stream as
the same **SCLR/LINS-style** overlay chunks the FEA/CFD code already uses, so waveform plots and
node-voltage heat overlays reuse existing rendering. **"Better than KiCad" levers:** probe by
clicking a wire; parameter sweeps and Monte-Carlo tolerance from the schematic; operating-point
annotations drawn *on* the schematic; AI that reads ERC + sim results and proposes fixes.

### 3.5 The 3D bridge (headline integration)

`engine/ecad/threed.py` turns a `BoardDocument` into a parametric solid: extrude the board outline
to stackup thickness, add copper/mask as thin colored layers (optional, for realism), then place
each footprint's associated **3D model** (STEP via existing OCP import; bounding-box proxy when no
model) at its position/rotation/side. Output: a STEP file + a thin `aicad.part/1` document whose
single `import` feature references it — so it opens in the existing CAD viewport and assembly tools.
The user then models a housing around it and uses the **existing interference/fit report** to check
clearance. This is the seam that makes "design PCB → design its enclosure" one continuous flow.

## 4. Contracts (frozen at M0, `docs/contracts/ecad/`)

| Contract | Contents |
|---|---|
| `sch_schema.md` | SchematicDocument JSON, symbol/pin model, hierarchical sheets, units (mm internal) |
| `pcb_schema.md` | BoardDocument JSON, stackup, footprint/pad/track/via/zone model, design-rule block |
| `netlist.md` | Netlist data model + the connectivity algorithm + ERC rule catalog |
| `ecad_ops.md` | Closed op vocabulary (envelope, transactionality, id placeholders, error codes) — also the AI tool schema |
| `ecad_rpc.md` | Worker `ecad_*` methods, protocol-2 reuse, error envelope, sim study handshake |
| `sim.md` | Simulation study schema, device model list + params, analysis types, result/probe format |
| `fab_outputs.md` | Gerber/Excellon/BOM/CPL formats, layer mapping, the STEP 3D-bridge contract |
| `library.md` | Native symbol/footprint/3D library format + KiCad import field mapping + starter inventory |

Plus one appended row block in `docs/contracts/licenses.md` (ECAD posture: KiCad clean-room,
ngspice isolation, library-content terms).

## 5. Waves & model assignment

Contracts-first (M0, single-threaded, me). Then parallel subagent waves with per-task model tiers
chosen to match difficulty and to manage weak-model context. **Opus** = hard algorithmic/architectural;
**Sonnet** = well-specified implementation against a frozen contract; **Haiku** = mechanical
scaffolding/boilerplate/format writers with a worked example.

| Wave | Tasks (owner dir) | Model | Verify gate |
|---|---|---|---|
| **M0** | Plan + all `docs/contracts/ecad/*` + fixtures | Opus (me) | contracts internally consistent; fixtures validate |
| **A1** | `schema.py` (sch+pcb validators) + schema tests | Sonnet | fixture docs validate; round-trip JSON stable |
| **A2** | `ops.py` + `session.py` (revision/undo/transactionality) | Opus | transactional-ops tests; undo/redo; placeholder resolution |
| **A3** | `netlist.py` + `erc.py` | Opus | golden netlist for fixture sch; ERC catches seeded faults |
| **A4** | `library/` (sexpr, kicad_sym, kicad_mod, native, starter) | Sonnet | parse real KiCad lib samples; starter lib loads |
| **B1** | `sim/circuit.py` + `sim/devices.py` (MNA) | Opus | RC τ, divider, diode clamp, RLC ring, op-amp gain vs analytic ≤1% |
| **B2** | `sim/spice.py` + sim worker RPC + study plumbing | Sonnet | SPICE round-trip; transient via worker; kill-9 isolation |
| **B3** | `drc.py` | Opus | seeded clearance/width/annular/unrouted violations all caught |
| **B4** | `fab/` (gerber, excellon, bom, placement) | Sonnet/Haiku | Gerber parses in a reference viewer-grammar test; drill counts; BOM groups |
| **C1** | `threed.py` (PCB→STEP solid) | Opus | board volume == outline×thickness; component placed at xy/rot; STEP imports as CAD feature |
| **C2** | `rpc.py` (`ecad_*`) + registration hook | Sonnet | every op exercised over RPC; revision/error envelope correct |
| **C3** | `mcp_server/ecad_tools.py` + agent context helpers | Opus | `ecad_describe` ≤ budget; tools return next-action hints; mock-model E2E |
| **C4** | `tests/ecad/` cross-cutting integration + fixtures | Sonnet | full slice: build sch → netlist → ERC → sim → place → DRC → fab → STEP, green |
| **D1–D4** | C++ editor (canvases, panels, PM forms, ribbon) | Opus/Sonnet | isolated build; merge-fenced; screenshot smoke (best-effort) |

**Regression gate each wave:** new ECAD suites green **and** the 5 legacy CAD suites still green
(we touch shared files only by append). The Python slice (M0–C) is the "proud-of" deliverable;
Wave D is best-effort scaffolding handed off for integration.

## 6. Conflict-minimization with the parallel CAD agent

The CAD agent is actively editing `engine/doc/` and `host/src/`. We stay out of its way:

- **New code in new dirs** (`engine/ecad/`, `engine/sim/circuit.py|devices.py|spice.py`,
  `mcp_server/ecad_tools.py`, `host/src/ui/ecad/`, `tests/ecad/`, `docs/contracts/ecad/`).
  Zero edits to existing `engine/doc/*` or `engine/sim/{fea,lbm}*`.
- **Shared "merge-by-append" files** (the plan already names these as single-owner append targets):
  `engine/worker.py` `_METHODS` registry, `host/CMakeLists.txt` source list,
  `host/src/ui/ribbon_def.cpp` command table, `mcp_server/server.py` tool imports,
  `host/src/app.cpp` panel-draw loop, `host/src/ui/title_bar.cpp` dock layout. We add **clearly
  fenced `// === ECAD (Wave x) ===` blocks at the end** of the relevant region — append-only, never
  reflow existing lines — so a three-way merge applies cleanly.
- **Reuse, don't fork** `nsolve.py`, `selectors` patterns, `stepplus` container, AICM/SCLR/LINS
  chunks, PM-form field kinds, the agent feedback idioms. If we need a new PM field kind (e.g.
  `NetRef`, `LayerCombo`), we *add* an enum value at the end rather than renumbering.
- **We are on branch `claude/epic-gould-fa9964` in a worktree** → physically isolated; the user
  merges back when ready.

## 7. Risks & mitigations

- **C++ host churn / un-buildable mid-merge.** Mitigation: engine-first; the entire value prop is
  headless-verifiable; UI is the last, separable wave behind fenced hooks. We do not block on a
  green C++ build.
- **Weak local model can't drive EDA.** Mitigation: §2.6 context helpers; tools do the bookkeeping
  (auto-net-naming, auto-ratsnest, "3 pins unconnected: A, B, C") so the model only makes decisions.
- **Sim numerical robustness.** Mitigation: analytic golden tests per device/analysis; gmin
  stepping + source stepping for convergence; trapezoidal-ringing guard; honest non-convergence
  reporting instead of silent garbage.
- **Netlist correctness is load-bearing** (sch↔sim↔pcb all depend on it). Mitigation: it is an
  Opus task with a golden fixture and seeded-fault ERC tests; frozen as a contract.
- **License drift.** Mitigation: clean-room rule in the contract; a licenses.md row; no KiCad
  source enters the tree; ngspice/KiCad-libs are user-supplied/optional.

## 8. Definition of "proud of" for this autonomous session

1. This plan + all M0 contracts written and self-consistent.
2. `engine/ecad/` + `engine/sim/circuit.py` implemented to a **working, tested headless vertical
   slice**: programmatically build a schematic (e.g. a 555 astable or an RC/op-amp filter), generate
   its netlist, run ERC, **simulate** it (transient + AC) with results matching analytic
   expectations, lay out footprints on a board, run DRC, export Gerber/drill/BOM, and **emit a STEP
   solid that imports into a CAD part document**.
3. AI tools (`ecad_*`) exposed via MCP with rich context helpers, exercised by a mock-model E2E test.
4. Wave D C++ scaffolding present, isolated, and documented for later integration — not required to
   compile against the moving host, but structured so it will.
5. Every legacy CAD test still green; all new work on the isolated branch with clear commits.

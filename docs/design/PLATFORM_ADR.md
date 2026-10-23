# ADR-001: Idea-to-Production Platform on Chudware

**Status:** Adopted design record. The decisions below were implemented; that is scoped to
the decisions recorded here, not a blanket feature-shipping or certification claim.
**Date:** 2026-07-02 (status updated 2026-08-17)
**Deciders:** Chudware maintainers

**Authority scope:** the context, implementation patterns and action-item checkboxes
below are the original July architecture record. In particular, the Python engine
pattern in the Decision section predates the native-engine consolidation; it is not
an instruction to restore the retired engine. Current boundaries are in
[ARCHITECTURE.md](../../ARCHITECTURE.md) and
[engineering notes](../dev/engineering-notes.md). Later simulation credibility
notes in this ADR retain their own linked evidence and program scope.

## Context

Chudware is already a working SolidWorks-class parametric modeler: OCCT kernel, feature tree,
assemblies, sheet metal, drawings, FEA (static/modal), LBM CFD + OpenFOAM bridge, native SPICE,
a complete ECAD module (schematic/PCB/sim/Gerber), a full CAM suite with cycle-time simulation,
a three-tier standard/vendor parts library, GSL (deterministic geometry description for
low-capability models), a 100+-tool MCP server, and a C++ mission runner
(`core/agent/mission.h`) that executes a *linear* task list end-to-end.

The goal is to grow it into an idea-to-production suite. New pillars, in priority order:

1. **DAG workflow orchestrator** — projects as checkpointed graphs; limited-context models
   execute nodes; user chooses autonomy per node; explicit end conditions so loops terminate.
2. **Sourcing + cost estimation** — priced BOMs, supplier lookups, manufacturing quotes.
3. **Spec-sheet → model matching** — pair purchased parts (with no CAD) to generated or
   library models from datasheets/listings.
4. **Firmware/embedded environment** — program the boards ECAD designs, with emulation so the
   agent can test firmware without hardware.
5. **Civil/structural/carpentry suite** — structural elements, beam/frame analysis,
   building-code checking, material + labor estimation.
6. **Piping + hydraulic design** (sprint 2) — routed pipe runs, 1D network/pressure-drop
   solving, pump selection, oil-hydraulic circuits, with 3D CFD handoff for detail work.

Plus a seventh track (in flight externally): **proprietary CAD format reverse engineering**
(SolidWorks / Creo / Fusion 360 underway) to break vendor lock-in.

**AI tooling & infrastructure layer:** the layer under every pillar
above — agent web-research/knowledge tools, a unified ask-user interaction contract,
image/camera intake, total state→text narration (GSL parity for CAM/FEA/ECAD/assemblies),
a converged provider/session layer with tool allow-listing, a trainable ≤4B local
translator + NNUE-style evaluators track, and agentic-workflow completion (plan mutation,
workflow templates, design-review battery).

**Constraints:** a small team and a five-day window for the first integrated cut; a
desktop product with strict license hygiene (no GPL *linking* — copyleft tools run
subprocess-only, per [`contracts/licenses.md`](../../contracts/licenses.md); no AGPL at
all); reuse open source aggressively; sim credibility via V&V'd open solvers, not
commercial certification.

## Decision

**Extend in place.** No rewrite, no re-platform. Every pillar follows the two proven repo
patterns: (a) a new Python engine module (the ECAD precedent: own schema/session/ops + MCP tool
family registered in `services/mcp/server.py`), and (b) contract-first (`contracts/*.md`) so the
C++ cutover can absorb each module later without rework.

The 5-day sprint builds a **walking skeleton of every pillar** — each one demo-able end-to-end
through one thin path — with depth deferred to a prioritized backlog (see
the 5-day sprint plan (executed; doc pruned 2026-07-23)).

## Options Considered

### Decision 1 — Build strategy

| Option | Complexity | Time-to-demo | Risk |
|---|---|---|---|
| **A. Extend in place (chosen)** | Low | Days | Monolith grows; mitigated by ECAD-style module isolation + frozen contracts |
| B. Platformize (extract kernel as platform, pillars as apps) | High | Weeks | Pure refactor cost, zero new capability, kills the 5-day timeline |
| C. Fresh start, harvest modules | Very high | Months | Rebuilds years of kernel/parity/GSL work |

The C++ strangler migration *is* the platformization — it's already underway with a parity
harness. New pillars ride it instead of fighting it.

### Decision 2 — DAG orchestrator substrate

| Option | Assessment |
|---|---|
| **A. LangGraph (MIT) in `services/agent/` (chosen)** | SQLite checkpointer (`langgraph-checkpoint-sqlite`), `interrupt()` for human gates, arbitrary/local model backends, fully offline. 2–3 days to a working graph. |
| B. Grow `core/agent/run_mission()` into a C++ DAG | Right long-term home, but means writing checkpointing/branching/HITL from scratch in C++ in 5 days. Do later, against the frozen contract. |
| C. Burr (Apache-2.0) | Credible alternative, cleaner state-machine model, smaller ecosystem. Fallback if LangGraph fights us. |
| D. Prefect/Dagster | Server-grade data-pipeline infra; too heavy to embed in a desktop app. Rejected. |

We define `contracts/dag_schema.md` (nodes, edges, end conditions, autonomy levels, checkpoint
format) as the frozen seam; LangGraph is an implementation detail behind it. The existing
mission stage machine gains a `pause` verdict (`machine.h`) — already specified in
the intake/measurement/DfM plan (doc pruned) Gate 0.

**Node contract (every node type):** `inputs` (doc refs + node-scoped context pack — never full
history), `tools` (allow-listed MCP subset), `end_condition` (machine-checkable predicate),
`verify` (critic step or deterministic check, e.g. `check_interference`, ERC/DRC, D/C ratio),
`on_fail` (retry budget → escalate to gate), `autonomy` (`auto` | `approve` | `manual`).
The autonomy dial is per-node and per-run: "AI does everything" = all `auto` with gates only at
spend/order actions; "AI as advisor" = all `manual` with AI pre-filling each node.

### Decision 3 — Simulation credibility

**V&V'd open-source stack (chosen).** All copyleft solvers run as subprocesses (the
FreeCAD/SimScale pattern): CalculiX & OpenFOAM (GPL, subprocess + source offer in installer),
Elmer (LGPL lib linkable), ngspice (BSD, bundleable), Verilator (Artistic-2.0 path, bundleable)
+ GHDL/Icarus (GPL, subprocess) for the FPGA pillar. The V&V harness uses independently
derived analytic references and openly distributable cases; it records failures and skips
without aggregate masking. The pinned Polyhedral-FEA/PolyMesh fork family is reported
separately, with solver source and linked-binary revision, and remains a tet4/tet10
discretization path into Chudware's solver rather than a VEM/polyhedral-solve claim.

Credibility is derived from a durable evidence registry, never assigned by a solver or UI.
The shared tiers are `S0_unvalidated`, `S1_benchmarked`, `S2_cross_solver`, and
`S3_field_correlated`. Evidence is bound to ids, solver/model revision hashes, expiry and
invalidation state. S2 requires registered independent-solver evidence depending on current
S1 evidence; S3 requires registered field-correlation evidence depending on current S2
evidence. Missing, malformed, expired, invalidated, or revision-mismatched evidence fails
closed to the highest fully supported lower tier. Generated labels are allow-listed internal
V&V descriptions; certification/accreditation language is rejected.

Internal V&V and external approval are separate programs. Internal gates require complete
family inventory, reproducible benchmark provenance, zero unexplained failures/unexpected
skips, three-level discretization studies, cross-solver comparisons, uncertainty budgets,
and field datasets before the corresponding evidence tier can be registered. External QMS,
lawful standards acquisition, accredited-laboratory work, and certification-body engagement
have independent owners and milestones and cannot be completed by code. The measurable
program and current not-complete status are maintained in
[ACCREDITATION_PROGRAM.md](../simulation/ACCREDITATION_PROGRAM.md). Never bundle
NAFEMS/ASME documents themselves.

### Decision 4 — Building-code data posture

Bundle only what's clean: AISC shapes DB (free download), jurisdiction-adoption metadata,
material property tables. **Never bundle IBC/IRC/NDS/Eurocode text** (ICC v. UpCodes is not
settled enough to bet a product on; AWC is view-only). Code checking = agent RAG against public
code viewers at runtime + a deterministic IDS-style rule engine (ifctester pattern, LGPL) for
checks we encode ourselves. Cost data: user-overridable rate tables seeded with ENR national
averages; no retailer scraping.

### Decision 5 — Format interop (reverse-engineering track)

Geometry-first, features-later: reversing yields B-rep payloads long before feature history.
Priority beyond the in-flight SW/Creo/Fusion effort, ranked by leverage ÷ effort:

| Rank | Format | Why / how |
|---|---|---|
| 1 | **Rhino .3dm** | No reversing needed — openNURBS is MIT. Free win, do it in the sprint backlog. |
| 2 | **Solid Edge / NX .prt** | Both embed **Parasolid** payloads; we already read Parasolid. Extract-and-hand-off, not a full reverse. |
| 3 | **CATIA V5 .CATPart/.CATProduct** | Biggest aerospace/auto lock-in; stable format; prior art exists. Highest-value true reverse. |
| 4 | **Inventor .ipt** | ShapeManager (ACIS-derived) payload; medium difficulty, large user base. |
| 5 | **DXF/DWG** | DXF via ezdxf (MIT) now; DWG via ODA membership later (LibreDWG is GPL — subprocess convert only). |
| 6 | **Altium .SchDoc/.PcbDoc + Eagle XML** | ECAD lock-in. Eagle is documented XML (trivial); Altium is well-reversed publicly (KiCad's importer = GPL, use as *spec reference only*, clean-room our parser). |
| 7 | **JT** | ISO 14306 — spec is purchasable, not a reverse. Aerospace viz interchange. |
| — | Revit .rvt | Defer — enormously complex; IFC via IfcOpenShell (LGPL) covers the BIM need. |

Legal guardrails: interoperability reverse engineering is protected (DMCA §1201(f), EU Software
Directive Art. 6) **if clean-room**: one party documents formats in specs; whoever implements
parsers never reads competitor source/SDKs; no EULA-bound tools used for the analysis; no DRM
circumvention (Fusion cloud files are fetched only by the user through their own Autodesk
account).

### Decision 6 — Piping/hydraulics stack (sprint 2)

Entirely permissive-license territory — bundle freely: **EPANET 2.2/WNTR** (MIT/BSD, water
networks), **pandapipes** (BSD-3, gas/heat), **`fluids`/`ht`/`thermo`** (MIT — ships ASME
B36.10/19 pipe-schedule data and fitting K-factors), **CoolProp** (MIT, properties). Routed
pipe geometry = OCCT sweeps + a CSV fitting catalog (B16 dims rebuilt from public manufacturer
data — ASME text itself is not bundleable). Oil-hydraulic circuits clone the ECAD schematic
pattern (ISO 1219 symbols) simulated via **OpenHydraulics** (BSD-3) FMUs run through fmpy —
avoiding the GPL OpenModelica runtime. 3D detail flows hand off to the existing LBM/OpenFOAM
path via internal-volume extraction. No free pump-curve database exists: user-imported CSV
curves + affinity-law scaling.

## Trade-off Analysis

Extending in place trades architectural purity for speed — accepted because the module pattern
(ECAD precedent) has already proven it keeps disciplines decoupled. LangGraph-in-Python trades
"zero-Python end state" for a working orchestrator this week — accepted because the frozen DAG
contract makes the C++ port mechanical later. V&V'd open solvers trade the certified-stamp
market (regulated aerospace/medical sign-off) for zero licensing cost and full automation —
accepted; certified-tool *export* (Nastran deck, OpenFOAM case dirs) stays on the backlog as
the escape hatch. Runtime-RAG for building codes trades offline capability for legal safety —
accepted.

## Consequences

- Easier: every pillar reuses doc-meta persistence, MCP registration, GSL descriptions, and the
  mission runner; agents get new capabilities as MCP tool families with zero UI blocking.
- Harder: Python surface grows while the C++ cutover tries to shrink it — every new module MUST
  ship a `contracts/*.md` on day one or it becomes migration debt.
- Harder: license compliance becomes an ongoing discipline — installer needs a notices file +
  GPL source offer; CI should gate on a license manifest.
- Revisit: DAG engine choice after the C++ agent port; TraceParts/Werk24 paid tiers once
  spec-sheet matching volume justifies it; OSMC membership if OpenModelica system-sim is added.

## Action Items

1. [ ] Freeze `contracts/dag_schema.md`, `contracts/sourcing_schema.md`,
       `contracts/firmware_rpc.md`, `contracts/structural_schema.md` (day 1)
2. [ ] Execute the 5-day sprint plan (executed; the plan document was retired 2026-07-23)
3. [ ] Add license manifest + notices generation to `installer/` and CI
4. [ ] Apply for API keys now (lead-time risk): Nexar, Digi-Key, Mouser, Xometry, JLCPCB,
       McMaster eProcurement
5. [ ] Clean-room protocol memo for the format-reversing collaboration

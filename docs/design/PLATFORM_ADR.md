# ADR-001: Idea-to-Production Platform on Chudware

This record captures the platform design decisions. Current implementation boundaries
are described in [ARCHITECTURE.md](../../ARCHITECTURE.md) and
[engineering notes](../dev/engineering-notes.md).

## Context

The platform extends parametric modeling into an idea-to-production suite with
interoperability, simulation and agent-assisted workflows.

The design pillars are:

1. **DAG workflow orchestrator** — projects as checkpointed graphs; limited-context models
   execute nodes; user chooses autonomy per node; explicit end conditions so loops terminate.
2. **Sourcing + cost estimation** — priced BOMs, supplier lookups, manufacturing quotes.
3. **Spec-sheet → model matching** — pair purchased parts (with no CAD) to generated or
   library models from datasheets/listings.
4. **Firmware/embedded environment** — program the boards ECAD designs, with emulation so the
   agent can test firmware without hardware.
5. **Civil/structural/carpentry suite** — structural elements, beam/frame analysis,
   building-code checking, material + labor estimation.
6. **Piping + hydraulic design** — routed pipe runs, 1D network/pressure-drop
   solving, pump selection, oil-hydraulic circuits, with 3D CFD handoff for detail work.

Interoperability work addresses proprietary CAD formats.

**AI tooling & infrastructure layer:** the layer under every pillar
above — agent web-research/knowledge tools, a unified ask-user interaction contract,
image/camera intake, total state→text narration (GSL parity for CAM/FEA/ECAD/assemblies),
a converged provider/session layer with tool allow-listing, a trainable ≤4B local
translator + NNUE-style evaluators track, and agentic-workflow completion (plan mutation,
workflow templates, design-review battery).

**Constraints:** strict license hygiene (no GPL *linking* — copyleft tools run
subprocess-only, per [`contracts/licenses.md`](../../contracts/licenses.md); no AGPL at
all); reuse open source; simulation credibility via V&V'd open solvers, not
commercial certification.

## Decision

**Extend in place.** Prefer existing subsystem boundaries and contract-first interfaces
over a rewrite or re-platform. Current implementation boundaries are described in
[ARCHITECTURE.md](../../ARCHITECTURE.md).


## Options Considered

### Decision 1 — Build strategy

| Option | Complexity | Time-to-demo | Risk |
|---|---|---|---|
| **A. Extend in place (chosen)** | Low | Days | Monolith grows; mitigated by ECAD-style module isolation + frozen contracts |
| B. Platformize (extract kernel as platform, pillars as apps) | High | Weeks | Pure refactor cost, zero new capability, kills the 5-day timeline |
| C. Fresh start, harvest modules | Very high | Months | Rebuilds years of kernel/parity/GSL work |

The native engine and contract boundaries support incremental integration of new disciplines.

### Decision 2 — DAG orchestrator substrate

| Option | Assessment |
|---|---|
| **A. LangGraph (MIT) (chosen)** | SQLite checkpointing and interrupt/resume for human gates. |
| B. C++ DAG | Requires a separate checkpointing, branching and human-gate implementation. |
| C. Burr (Apache-2.0) | Alternative state-machine model. |
| D. Prefect/Dagster | Server-grade data-pipeline infrastructure, too heavy to embed. |

[`contracts/dag_schema.md`](../../contracts/dag_schema.md) defines the DAG seam;
the implementation is in `services/dag/`.

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

Geometry-first interoperability can recover B-rep payloads independently of feature
history. Format support and editable-history fidelity must be assessed separately.

Legal guardrails: interoperability reverse engineering is protected (DMCA §1201(f), EU Software
Directive Art. 6) **if clean-room**: one party documents formats in specs; whoever implements
parsers never reads competitor source/SDKs; no EULA-bound tools used for the analysis; no DRM
circumvention (Fusion cloud files are fetched only by the user through their own Autodesk
account).

### Decision 6 — Piping/hydraulics

Routed pipe geometry, network solving and CFD handoff should share a coherent
workflow while preserving solver assumptions and data provenance.

## Trade-off Analysis

Extending in place preserves existing subsystem boundaries. Open solvers and
independently derived validation cases support reproducible evidence, but do not
substitute for certification. License compliance and clean-room interoperability
remain ongoing obligations.


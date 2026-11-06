# Documentation

Documentation for Chudware users, operators, contributors and agents. Frozen
interface contracts (worker RPC, document ops and schema, events, manufacturing
gateway, and more) live in [`contracts/`](../contracts/README.md), not in `docs/`.

## Start by audience

- **Users:** the [project README](../README.md) covers getting started, building and
  running. The [generated manual](manual/index.md) contains the ribbon inventory
  and `cad_guide` text used by the agent; the browser deployment's scope and
  limitations are in [web/README.md](../web/README.md).
- **Operators and release maintainers:** start with the [reference index](reference/README.md),
  including the [release](reference/RELEASING.md) and [signing](reference/SIGNING.md)
  runbooks, CI, Windows build/packaging, and host daemon operations.
- **Contributors:** use the [developer index](dev/README.md), the source-linked
  program and GUI flows in [architecture](../ARCHITECTURE.md), and the
  [UX quality map](dev/ux-quality.md) for audited defects, acceptance boundaries
  and evidence status. As-built routing and required, unverified cutover policy
  are identified separately.
- **Interface implementers and interop researchers:** use the
  [contracts index](../contracts/README.md) and the
  [reference index](reference/README.md), which distinguishes its three-file
  public-source clean-room CAD reference from Chudware-specific material.

## Collections by purpose

| Collection | Status and use |
|---|---|
| [dev/](dev/README.md) | Current engineering notes and subsystem invariants: engine/daemon/document rules, official OMP agent-runtime boundary, GUI architecture, theme and layout, workspaces and evaluate tools. |
| [reference/](reference/README.md) | Maintained build, packaging, release and operator runbooks; CAD data-model and file-format references, simulation and reverse-engineering references, and a demo walkthrough with explicit PASS/SKIP limits. |
| [design/](design/README.md) | Design records and rationale; proposals are not evidence that every capability is shipped. |
| [manufacturing/](manufacturing/README.md) | Manufacturing design corpus (machine-control gateway, physical twin, metrology, additive, monitoring, turning, cutting, pick-and-place and robot planning) with an as-built map. Physical execution remains hardware-gated. |
| [simulation/](simulation/) | Simulation credibility/accreditation and learned-optimizer admission programs; the accreditation program is proposed and incomplete, not external approval. |
| [vv/](vv/FEA_VV_REPORT.md) | Generated FEA verification and validation report, results JSON and figures from `scripts/vv_fea_crosscheck.py`; read the recorded outcomes and limitations. |
| [manual/](manual/index.md) | Generated, agent-facing `cad_guide` text, ribbon command inventory and contract summary. Regenerate with `python3 -m tools.manual_gen` (drift check: `--check`); never hand-edit. |
| [SW_PARITY_SPEC.md](SW_PARITY_SPEC.md) | SolidWorks feature-parity capability and acceptance inventory; `scripts/parity_audit.py` checks its evidence pointers. |

## Parts Library

Open the Parts Library workspace to browse categories or search for standard
parts and live catalog results. Refine results with the available filters and
use the list/grid controls to change the preview layout. Select a result to
inspect its details and available actions; use the pagination controls to browse
more results. Source chips identify sources contributing live results.
On narrow layouts, open the category, filter and detail drawers as needed.

Preview images may be CAD renders or vendor product photos, and some parts have
no preview. Inspect the part details, dimensions, source and licensing before
using a result; a product photo does not establish model geometry or fit.
The **Sources…** popup provides vendor sign-in, API provider status, download
queue and health information. Configure your own DigiKey, Mouser and Nexar API
credentials in **Settings** for their official search APIs; Nexar requires
credentials, while DigiKey and Mouser can also try public website search.
Availability and sign-in requirements vary by source.

The agent's `part_search` can return a labeled preview sheet: tile numbers match
the JSON result indices, and missing previews are shown explicitly. Ask the
agent to inspect a candidate's identity and dimensions before acquiring or
placing it. The [parts contract](../contracts/parts.md) describes the data and
acquisition interfaces.

Unscoped searches infer local categories and vendor routes from the query and
include general CAD sources; unknown categories use only general model sources.
Exact designations rank ahead of neighboring sizes. Live searches return partial
results and a summarized timeout notice when the shared deadline expires
(15 seconds by default; set `CHUDWARE_PARTS_LIVE_BUDGET_S` to change it).

## Fluid simulation and piping / industrial design

- For fluid setup, solver choices and their limitations, see the generated
  [simulation guide](manual/guide-simulation.md): coarse LBM external-flow
  estimates and the separately provisioned OpenFOAM path have different
  assumptions and dependencies. Implementation entry points include
  [simulation jobs](../services/sim/jobs.py) and the
  [OpenFOAM subprocess runner](../services/sim/foam_runner.py). The
  [workspace notes](dev/gui-workspaces.md) map the human Simulation, Piping and
  Hydraulics surfaces.
- For routed networks, flow solving, checks, and CFD handoff, use the
  [piping guide](manual/guide-piping.md), the
  [piping schema](../contracts/piping_schema.md) and the
  [cross-simulation handoff contract](../contracts/sim_handoff.md), which records
  units, provenance and comparison envelopes. Implementation is in
  [core/piping/](../core/piping/) and the
  [piping agent tools](../services/mcp/piping_tools.py).
- For vessels, ducts, skids and pipe racks, see the
  [industrial guide](manual/guide-industrial.md) and
  [industrial sizing contract](../contracts/industrial_schema.md). The
  [civil guide](manual/guide-civil.md) covers site and plant tie-ins.
- Simulation output is not itself proof of validation or accreditation; consult
  the [credibility program](simulation/ACCREDITATION_PROGRAM.md), the
  [optimizer program](simulation/OPTIMIZER_PROGRAM.md), and the evidence
  for the specific solver and result.

## Repository media

[Modeling still](media/modeling.png), [modeling sequence](media/modeling.gif), and
[simulation still](media/simulation.png) are repository/README media. The GIF
shows captured modeling steps, not a continuous recording or an agent transcript.
When replacing media, use real application captures and keep captions consistent
with what they show; a screenshot alone does not establish a solver result.
Website images remain separately served from `website/public/images/`.

Related: [`core/docs/kicad_reference.md`](../core/docs/kicad_reference.md) records the
clean-room policy for using KiCad as an ECAD reference.

## Repository-root documents

- [`README.md`](../README.md) — project overview, building, running
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — system architecture
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contributor guide
- [`AGENTS.md`](../AGENTS.md) — guidance for AI coding agents
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting
- [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) — third-party licenses
- [`corpora/`](../corpora/README.md) — consolidated test fixtures

## Maintenance

- Keep documents in the folder that matches their purpose and add new ones to this index.
- Generated manuals and V&V outputs under `docs/` live at the fixed paths `manual/`
  (`tools/manual_gen.py`) and `vv/` (`scripts/vv_fea_crosscheck.py`); regenerate
  them with their generator rather than editing by hand.
- Do not commit other generated artifacts (`__pycache__`, `.pyc`, ad-hoc renders) under
  `docs/`.

# Project Documentation Index

This folder holds the human-facing documentation for the Chudware AI-CAD platform, plus
one generated agent-facing reference (`manual/`) and two dated provenance collections
(`omp-integration/`, `contracts/`) that are kept for history, not as current specs.

Frozen interface contracts (worker RPC, document ops/schema, events, manufacturing
gateway, …) are **not** under `docs/`; they live at the repository root in
[`../contracts/`](../contracts/README.md).

## Layout

| Folder | Kind | Contents |
|--------|------|----------|
| [`design/`](design/) | human | Architecture, UI/UX, agent pipeline, PLM/version-control, and platform ADRs. |
| [`reference/`](reference/README.md) | human | Build/packaging guides, CAD data-model and file-format references, runbooks. |
| [`plans/`](plans/) | human | Roadmaps, overhaul specs, and consolidation refactor plans. |
| [`review/`](review/) | human | Audits, surveys, ship verdicts, and residual-analysis reports (dated snapshots). |
| [`manufacturing/`](manufacturing/README.md) | human | Manufacturing / CNC / additive overhaul design corpus with an as-built map of what landed. |
| [`simulation/`](simulation/) | human | Simulation credibility/accreditation and optimizer program documents. |
| [`vv/`](vv/FEA_VV_REPORT.md) | generated | FEA verification & validation report, results JSON and figures, written by `scripts/vv_fea_crosscheck.py`. |
| [`ops/`](ops/HOST_CRASH_FORENSICS.md) | human | Operational incident forensics (dated). |
| [`refactor-log/`](refactor-log/) | human, frozen | Closed archives of `REFACTOR_LOG.md`: through 2026-07 and 2026-08-01..08. Never edited; append to the running log instead. |
| [`manual/`](manual/index.md) | **generated, agent-facing** | Generated agent-facing reference: the `cad_guide` modeling-guide prompt text addressed to the model, the ribbon command inventory, and a one-line contract summary. Regenerate with `python3 -m tools.manual_gen` (drift check: `--check`); never hand-edit. Despite the generated "User Manual" title it is not a human user manual. |
| [`omp-integration/`](omp-integration/01-DECISIONS.md) | provenance, superseded | Dated 2026-07 research and decision records for integrating the OMP (oh-my-pi) agent harness. Superseded on 2026-08-31 (see the final "Superseding architecture" section of `01-DECISIONS.md`): the OMP bridge and vendored tree were deleted. Kept only as provenance for why the boundary moved; not a privacy engine, not a runbook, and not the current RPC contract. |
| [`cam_samples/`](cam_samples/) | sample output | Example NC/G-code output per supported controller and setup sheets. No automated consumer reads them. |
| [`contracts/`](contracts/fixtures/) | provenance, legacy fixtures | Legacy fixture data (`fixtures/runs`, `routing`, `effort` view JSON, renders, and the `runs/gen_fixtures.py` generator) whose ui/imgui consumers were retired. No current code or test reads it; kept as provenance. This is **not** the contract set — see [`../contracts/`](../contracts/README.md). |

## Documents in `docs/`

| Document | Contents |
|----------|----------|
| [`REFACTOR_LOG.md`](REFACTOR_LOG.md) | Running refactor handoff log, 2026-08-14 onward; earlier entries are in `refactor-log/`. |
| [`RELEASING.md`](RELEASING.md) | Nightly and tagged release pipelines. |
| [`SIGNING.md`](SIGNING.md) | Release signing and notarization. |
| [`PORT_LEDGER.md`](PORT_LEDGER.md) | Permanent record of features that existed only in the retired `ui/imgui/` GUI. |
| [`SW_PARITY_SPEC.md`](SW_PARITY_SPEC.md) | SolidWorks parity specification and dated parity updates. |
| [`AI_TOOLING_MASTER_PLAN.md`](AI_TOOLING_MASTER_PLAN.md) | AI tooling and infrastructure master plan. |
| [`ui-defects.md`](ui-defects.md) | Register of provable, reproducible UI defects with `file:line` anchors. |

## Repository-root documents and collections

These live at the repository root, not under `docs/`:

- [`README.md`](../README.md) — project overview
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — system architecture
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contributor guide
- [`AGENTS.md`](../AGENTS.md) — agent system contract
- [`CLAUDE.md`](../CLAUDE.md) — Claude-specific instructions
- [`contracts/`](../contracts/README.md) — frozen interface contracts, including the current
  worker RPC contract (`worker_rpc.md`, protocol 2) and agent event/interaction contracts
- [`corpora/`](../corpora/README.md) — consolidated test, regression and research fixtures
- [`work/`](../work/WORKFLOW.md) — task board and its workflow policy

## Maintenance

- Keep Markdown source documents in the folder that matches their purpose, and add new
  documents and folders to this index.
- Generated outputs committed under `docs/` are limited to `manual/` (`tools/manual_gen.py`)
  and `vv/` (`scripts/vv_fea_crosscheck.py`); regenerate them with their generator rather
  than editing by hand. The legacy `contracts/fixtures/` data is frozen provenance.
- Do not commit other generated artifacts (`__pycache__`, `.pyc`, ad-hoc renders, fixture
  outputs) under `docs/`.
- Superseded design records stay in place as dated provenance; mark them superseded rather
  than deleting or rewriting them.

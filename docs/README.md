# Project Documentation Index

This folder contains the living documentation for the Chudware AI-CAD platform.

## Layout

| Folder | Contents |
|--------|----------|
| `design/` | Architecture, UI/UX, agent pipeline, PLM/version-control, and platform ADRs. |
| `reference/` | Build/packaging guides, CAD data-model and file-format references, runbooks. |
| `plans/` | Roadmaps, overhaul specs, and consolidation refactor plans. |
| `review/` | Audits, surveys, ship verdicts, and residual-analysis reports. |
| `manual/` | User and contributor guides for simulation, review, reverse-engineering, CAM, etc. |
| `omp-integration/` | OpenMinded / privacy-engine integration research and decision records. |
| `cam_samples/` | Example NC/G-code output files and setup sheets for supported controllers. |
| `contracts/` | **Test fixtures only** — generated run data used by the contract test suite. Not a document collection. |

## Top-level documents

The following documents live at the repository root and are registered as project-level specifications:

- `README.md` — project overview
- `AGENTS.md` — agent system contract
- `ARCHITECTURE.md` — system architecture
- `OMP_INTEGRATION_PLAN.md` — OpenMinded integration plan
- `CONTRIBUTING.md` — contributor guide
- `CLAUDE.md` — Claude-specific instructions

## Maintenance

- Keep Markdown source documents in the folder that matches their purpose.
- Do not commit generated artifacts (`__pycache__`, `.pyc`, renders, fixture outputs) under `docs/`.
- Register new documents and document folders in PLM.

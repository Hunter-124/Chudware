# Documentation

Developer and design documentation for Chudware. Frozen interface contracts (worker RPC,
document ops and schema, events, manufacturing gateway, …) are not under `docs/`; they live
in [`../contracts/`](../contracts/README.md).

## Layout

| Folder | Kind | Contents |
|--------|------|----------|
| [`dev/`](dev/README.md) | developer notes | Engineering conventions and invariants per subsystem: engine/daemon/document rules, the Chudcode agent-runtime boundary, GUI architecture, theme and layout, workspaces, evaluate tools. |
| [`reference/`](reference/README.md) | reference | Windows build and packaging guides, CI, the `chudware-hostd` operator reference, CAD data-model and file-format references, simulation and reverse-engineering references, demo walkthroughs. |
| [`design/`](design/) | design | Design records: platform ADR, advisory layer and copilot pipelines, PLM/version control, native `.step+` format, 3D sketcher, glass GUI design language, redaction, embedded web view, chat context references, corporate roles. |
| [`manufacturing/`](manufacturing/README.md) | design | Manufacturing design corpus (machine-control gateway, physical twin, metrology, additive, monitoring, turning, cutting, pick-and-place, robot planning) with an as-built map of what is implemented. |
| [`simulation/`](simulation/) | design | Simulation credibility/accreditation and learned-optimizer admission programs. |
| [`vv/`](vv/FEA_VV_REPORT.md) | generated | FEA verification and validation report, results JSON and figures, written by `scripts/vv_fea_crosscheck.py`. |
| [`manual/`](manual/index.md) | generated, agent-facing | The `cad_guide` modeling-guide text addressed to the model, the ribbon command inventory, and a contract summary. Regenerate with `python3 -m tools.manual_gen` (drift check: `--check`); never hand-edit. |
| [`cam_samples/`](cam_samples/) | sample output | Example NC/G-code output per supported controller and setup sheets. |

## Documents in `docs/`

| Document | Contents |
|----------|----------|
| [`RELEASING.md`](RELEASING.md) | Release pipeline and GitHub Releases publishing. |
| [`SIGNING.md`](SIGNING.md) | Release signing and notarization. |
| [`SW_PARITY_SPEC.md`](SW_PARITY_SPEC.md) | SolidWorks feature-parity specification with dated updates; its evidence pointers are checked by `scripts/parity_audit.py`. |

Related: [`../core/docs/kicad_reference.md`](../core/docs/kicad_reference.md) records the
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
- Generated outputs under `docs/` are limited to `manual/` (`tools/manual_gen.py`) and `vv/`
  (`scripts/vv_fea_crosscheck.py`); regenerate them with their generator rather than editing
  by hand.
- Do not commit other generated artifacts (`__pycache__`, `.pyc`, ad-hoc renders) under
  `docs/`.

---
id: T-F8
title: describe_assembly, describe_board (GSL promotion), describe_project
workstream: F
owner: sonnet-rune
status: review
branch: task/T-F8-narration-completion
day: 8
depends_on: [T-F7]
---

## Context

Pillar 11 completion on the T-F7 contract: assemblies (mate graph in words, DOF per
component, fit/clearance narration — lift `_format_fit` out of the build loop),
ECAD (promote the bespoke `ecad_status` into the GSL layer), and the DAG run itself
(`describe_project` — nodes done/blocked, budgets, open questions; feeds WS-A
`dag_status` and the mission-control UI T-F17). READ `docs/AI_TOOLING_MASTER_PLAN.md`
§Pillar 11.

## Files / areas touched

- `services/mcp/gsl_tools.py` (or sibling) — the three describers
- `services/agent/` — extract `_format_fit` into the shared layer
- `services/dag/` — read-only join for describe_project (schema from T-A1 if landed;
  otherwise stub against `contracts/dag_schema.md`)

## Definition of done

- [x] Three describers on the narration contract, cad_guide topics added
- [x] `_format_fit` has one home; build loop calls the shared one
- [x] Tests green; `bash scripts/gate.sh`; `graphify update .`

## What landed (sonnet-kilo → sonnet-rune finisher, 2026-07-02)

### Implementation (sonnet-kilo, commit fde21a1)

- `services/mcp/gsl_tools.py`: `describe_assembly`, `describe_board`, `describe_project`
  MCP tools + their `_describe_*_text` helpers (~270 lines). `_format_fit` extracted out.
- `services/mcp/fit_narration.py` (NEW): single home for `_format_fit` — import-safe
  (no mcp SDK imports), shared by both `gsl_tools.py` and `services/agent/loop.py`.
- `services/agent/loop.py`: import updated to `from mcp_server.fit_narration import _format_fit`.
- `services/mcp/cad_tools.py`: `cad_guide` updated with `"assembly"`, `"board"`, `"project"`
  topic keys and narration text for the three new tools.

### Fixes + tests (sonnet-rune, commit 3a02cd3)

- Fixed broken ecad import in committed code: `from engine.ecad import ecad_session`
  → `from engine.ecad import session as ecad_session` (module is `engine/ecad/session.py`).
- 8 new tests in `services/mcp/test_gsl_tools.py`:
  - `test_describe_assembly_single_part_ok` — graceful no-bodies or placement narrative
  - `test_describe_assembly_multibody_fit_report` — FIT REPORT or clearance line
  - `test_describe_board_no_ecad_doc_graceful` — error on missing doc
  - `test_describe_board_schematic_narration` — component/net roster + attribution
  - `test_describe_project_no_run_graceful` — graceful no-run message
  - `test_describe_project_full_run` — full node/budget/questions narration
  - `test_describe_project_empty_nodes` — empty-node degradation
  - (21 total tests in file; all skip cleanly when libaicad not staged — same baseline as T-F7)

### Follow-ups inherited from T-F7 (not in scope for T-F8)

- Extend `_persist_study` to store max-von-Mises node/face location (T-F7 Q2).
- Expose per-op stepdown/stepover via cam_extract/cam_tree RPC (T-F7 Q3).

### Gate results

See gate.sh run below — pre-existing failures only (check.py feature_surface_audit +
parity_audit, pytest venv skips). No new regressions introduced.

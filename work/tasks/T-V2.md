---
id: T-V2
title: MCP runtime reality audit - dead tools, undeclared deps, missing binaries
workstream: V
owner: sonnet-vega
status: doing
branch: task/T-V2-mcp-runtime-audit
day: 2
priority: 2
depends_on: []
---

## Context

Erik directive (2026-07-03, re-cut after the original auditor died with the host
process): hunt the "PyNite pattern" — tools that are registered and tested-green
but DEAD at runtime because a dep was never declared/installed or a binary is
absent. Found so far by accident: PyNite (struct_analyze dead until foreman
provisioned it), reportlab/python-docx gap (T-Q4 flag), trimesh + OCP (4 test
files don't even collect in mcp-venv), engine.paths.DATA AttributeError
(LoggingProvider default — fixed 2026-07-03).

## Driver notes (foreman)

- For every registered MCP tool in services/mcp/server.py (~150+): actually CALL
  it (harmless args / a scratch doc) through the real mcp-venv with
  AI_CAD_LIBAICAD set, and record WORKS / DEGRADED (graceful absence-path) /
  DEAD (import error, missing binary, AttributeError).
- Deliverable: work/audits/MCP_RUNTIME_AUDIT.md + re-runnable
  scripts/mcp_runtime_audit.py (drives the tool surface, emits the verdict
  table; real counts, no all-skip).
- Cross-check every import in services/ + engine_oracle/ against requirements.txt
  and contracts/licenses.md — enumerate undeclared deps with exact pip lines.
- Also audit the full-sweep test pollution found 2026-07-03: ~29 baseline
  failures/44 errors when `pytest services engine_oracle tools training` runs as
  ONE session but each suite passes alone (state leakage: MCP re-registration,
  env vars, singletons). Identify the top polluters; fixes may land as follow-up
  tasks if large.
- No GUI windows. Do not install new deps without a licenses.md row +
  requirements.txt line in the same commit.

## Definition of done

- [ ] Verdict table covers every registered tool (real call counts)
- [ ] Undeclared-dep list with install/declare lines (licenses checked)
- [ ] Sweep-pollution top offenders identified with root causes
- [ ] Proposed-task table for anything DEAD
- [ ] `graphify update .` run

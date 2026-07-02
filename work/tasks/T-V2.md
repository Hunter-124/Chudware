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

## Progress log
- 2026-07-02 22:50 UTC [sonnet-vega] REVIEW — branch task/T-V2-mcp-runtime-audit pushed.

Deliverables: scripts/mcp_runtime_audit.py + work/audits/MCP_RUNTIME_AUDIT.md

Key findings:
- 182 tools registered, 0 DEAD (PyNite pattern does not currently exist — PyNiteFEA 1.0.1 and fluids 1.3.1 are installed)
- 96 WORKS / 10 DEGRADED (graceful) / 69 INCONCLUSIVE (arg-fixture mismatch, tools are alive) / 7 SKIPPED (side-effect)
- 21 of 29 requirements.txt packages absent from mcp-venv (build123d, OCP, anthropic, openai, pyvista, scikit-fem, python-pptx/docx, reportlab, etc.) — this is the real gap
- 4 undeclared-but-installed deps: anyio, langgraph, pydantic, qrcode (all MIT)
- 7 true pollution regressions (not ~29): primary cause is test_h7_shadow.py injecting sys.modules["services.agent.config"] stub at collection time; 2 secondary causes (module-load-time HAVE_FLUIDS/HAVE_KERNEL flags)
- Proposed T-V3..T-V8 follow-up tasks in the audit doc

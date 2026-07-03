---
id: T-V3
title: Runtime env provisioning + test-pollution fixes (T-V2 follow-ups)
workstream: V
owner: sonnet-reed
status: doing
branch: task/T-V3-env-pollution
day: 3
priority: 2
depends_on: [T-V2]
---

## Context

T-V2's 182-tool live audit (work/audits/MCP_RUNTIME_AUDIT.md): 0 DEAD tools, but
10 DEGRADED verdicts trace to 21 requirements.txt packages absent from mcp-venv,
4 installed packages are undeclared, and the combined-sweep test pollution has
three identified root causes. This task consolidates the audit's proposed
T-V3..T-V5 follow-ups into one deliverable.

## Driver notes (foreman)

1. Provision mcp-venv from requirements.txt (`~/.local/share/AI-CAD/mcp-venv/bin/pip
   install -r requirements.txt`), resolving pin conflicts conservatively (never
   downgrade an installed working package without noting it). Heavy packages
   (vtk/pyvista/OCP) are fine — disk is plentiful; watch RAM during any builds.
2. Declare the 4 undeclared-but-installed packages (anyio, langgraph, pydantic,
   qrcode — all MIT): requirements.txt lines + contracts/licenses.md rows.
3. Fix pollution root cause #1: services/agent/test_h7_shadow.py injects a stub at
   sys.modules["services.agent.config"] at COLLECTION time — move into a
   monkeypatch-scoped fixture so it cannot leak into other tests.
4. Fix root causes #2/#3: load-time HAVE_FLUIDS (engine_oracle/piping/test_catalog.py)
   and HAVE_KERNEL (test_cfd_handoff.py) flags → evaluate lazily at call time.
5. Prove it: per-dir suites stay green AND the combined 4-dir sweep's failure count
   drops (record before/after numbers; the 4 known collection-error --ignores from
   gate.sh may now be removable if OCP/trimesh landed in step 1 — if so, remove
   them from scripts/gate.sh and re-run the gate).

## Definition of done

- [ ] mcp-venv satisfies requirements.txt (list any unresolvable pins honestly)
- [ ] 4 undeclared deps declared with license rows
- [ ] Combined sweep failure count measurably reduced; numbers recorded
- [ ] Gate still PASS (or better: fewer --ignores)
- [ ] `graphify update .` run

## Addendum (foreman, 2026-07-03)

6. Also fix the `mcp` namespace shadow: under pytest from repo root, PEP-420 makes
   `services/mcp/` resolve as namespace package `mcp`, shadowing the installed mcp
   SDK ("mcp.server is not a package"). Hit by T-Q2 (collection workaround) and
   T-A4 (one honest engine-test skip in services/dag/test_verify.py). Fix once:
   likely an `__init__.py` decision or import-path rename so both collection paths
   and runtime resolvers see the real SDK.

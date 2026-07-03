---
id: T-V4
title: mcp-venv provisioning - close the 16 remaining declared-dep gaps
workstream: V
owner: unassigned
status: todo
branch: task/T-V4-venv-provisioning
day: 4
priority: 3
depends_on: [T-V3]
---

## Context

T-V2 found 21 requirements.txt packages absent from mcp-venv; T-V3 declared the
undeclared and fixed pollution but deferred bulk provisioning (shared venv,
worktree-isolation risk — sound call). 16 real gaps remain (see
work/audits/MCP_RUNTIME_AUDIT.md dep table: pyvista/vtk/scikit-fem/trimesh/
python-pptx/docx/reportlab/OCP-class etc.), causing the 10 DEGRADED tool verdicts.

## Driver notes (foreman)

- Provision in SMALL BATCHES into ~/.local/share/AI-CAD/mcp-venv (pip install from
  requirements.txt pins), running the affected suite after each batch — never one
  big install you can't bisect. Watch RAM; flock any heavy builds.
- After each batch: re-run scripts/mcp_runtime_audit.py — DEGRADED count should
  drop monotonically; record before/after per batch.
- OCP/trimesh landing may un-break the remaining gate.sh --ignore files
  (test_foam_case, test_lbm) — if so remove those ignores and prove the gate.
- Any pin conflict: resolve conservatively, note it, never downgrade a working
  package silently.

## Definition of done

- [ ] All requirements.txt packages import from mcp-venv (or unresolvable pins listed honestly)
- [ ] mcp_runtime_audit.py DEGRADED count at/near 0 (record final table)
- [ ] Gate --ignores shrunk if collection now clean; gate PASS
- [ ] `graphify update .` run

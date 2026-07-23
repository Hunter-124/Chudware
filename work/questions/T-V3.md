# T-V3 open questions

**Raised by:** sonnet-fenn  
**Date:** 2026-07-03  
**Branch:** task/T-V3-env-pollution

---

## Q1 — Task scope: bundled vs. split per audit proposal

The T-V2 audit proposes:
- T-V3 = Fix `test_h7_shadow.py` sys.modules pollution only
- T-V4 = Provision full mcp-venv (21 missing packages)  
- T-V5 = Fix module-load-time HAVE_FLUIDS/HAVE_KERNEL flags

My launch prompt bundles all three into T-V3. I have proceeded with the
bundled scope (RC#1/2/3 code fixes + 4 dep declarations) on branch
`task/T-V3-env-pollution` since the code fixes are isolated and safe.

**Recommended answer:** Accept the bundled scope. T-V4 (provisioning) is now
partially obsolete — the venv already has build123d 0.11.1, cadquery-ocp
7.9.3.1.1, ocpsvg 0.6.0. Only 14 of the original 21 packages remain missing
(trimesh, networkx, rhino3dm, olefile, pyvista, vtk, anthropic, openai,
scikit-fem, meshio, pyamg, python-pptx, python-docx, openpyxl, reportlab,
pypdfium2). Create a fresh T-V4 with the updated list if provisioning is still
desired. Foreman to back-fill T-V3 task file at merge.

## Q2 — Venv provisioning: who owns it?

The shared mcp-venv at `~/.local/share/Chudware/mcp-venv` cannot be safely
touched from a worktree without coordinating with all other agents. The audit's
"21 missing" list is now stale (venv already provisioned build123d etc. overnight).

**Recommended answer:** Scope T-V3 to code fixes + dep declarations only (done).
Defer provisioning to T-V4 with a serialized flock and fresh pip-list snapshot.

## Q3 — gate.sh --ignores: OCP/trimesh status

The 4 collection-error ignores in gate.sh are:
- `engine_oracle/doc/test_aicm.py`
- `engine_oracle/sim/test_foam_case.py`  
- `engine_oracle/sim/test_lbm.py`
- `services/agent/test_loop_mock.py`

With build123d/OCP now installed, do `test_aicm.py` and the sim files now
collect cleanly? If so, those ignores should be removed.

**Recommended answer:** Run collection check on each file before removing.
Defer to T-V4 provisioning task which will have the full venv + can verify.

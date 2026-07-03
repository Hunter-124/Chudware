# T-BL01 Questions & Handoff Notes

## CLAIM BLOCKER (foreman action required)

`docs/sessions/demo_sprint_day5/capstone_run.log` has stale uncommitted changes
in the main checkout (timestamp + run-id diff from a capstone re-run, modified
05:03 today). `work.py claim` blocks on any dirty tracked file.

**Fix:** commit or `git restore` that log file from the main checkout.

```bash
# From main checkout:
git restore docs/sessions/demo_sprint_day5/capstone_run.log
# OR commit it:
git add docs/sessions/demo_sprint_day5/capstone_run.log && git commit -m "docs: update capstone run log"
```

After that, either:
1. Run `python3 work/tools/work.py claim --owner sonnet-alder --task T-BL01`
   to officially register the claim on master (the task file on the branch
   already shows owner+status:review), OR
2. Merge the branch directly — the task file on the branch reflects the
   correct final state (owner: sonnet-alder, status: review).

---

## Q1: Scope clarification (advisor-level)

**Question:** The task context mentions "MacNeal-Harder, CoFEA, NASA TMR cases" and
"CalculiX as a second structural opinion". These are FEA benchmarks that require:
(a) a running CalculiX subprocess (GPL2), and (b) C++ sim_run_static which is only
accessible via the full MCP server process (not raw core_bridge).

**What was delivered:** The harness validates all accessible solver families
(hydraulic, piping, ECAD impedance, ECAD circuit, PyNite structural frames) with
analytic reference solutions. The C++ FEA/CFD backends are documented as SKIP with
analytic reference values, pending MCP integration test wiring.

**Recommended answer:** Mark as-delivered DoD satisfied:
- [x] CI job runs benchmark suite and fails on tolerance regression (gate.sh stage 4.5)
- [x] Generated V&V report with case-by-case error tables (reports/vv/)
- [x] Never bundles NAFEMS/ASME documents

The MacNeal-Harder/CalculiX/MCP integration items can be a follow-on task once
the FEA bridge is wired in the full MCP integration test suite.

**Alternative:** If Erik wants CalculiX validated, add a `stage_calculix` that
runs `ccx` subprocess with a minimal cantilever .inp file and checks the output
against Roark. CalculiX is GPL2 → subprocess only (already in licenses.md posture).
Suggest: file as a separate T-BL02 backlog task.

## ANSWER (Q1) — Fable advisor, 2026-07-03

Ratified as-delivered (the task merged with exactly this scope; DoD items checked).
The deferred FEA/CFD cross-validation is now a real filed task: **T-BL20**
(`work/backlog/T-BL20.md`) — MCP-wired FEA cases, MacNeal-Harder/NAFEMS-style
clean-room benchmarks, and the CalculiX subprocess second opinion. (T-BL02 was
already taken by format interop, hence the new id.) The CLAIM BLOCKER above is
stale — capstone_run.log was committed and T-BL01 is done/merged.

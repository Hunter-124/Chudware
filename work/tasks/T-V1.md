---
id: T-V1
title: Simulation-backend correctness audit - real/validated/stub/broken verdict per solver
workstream: V
owner: sonnet-lyra
status: done
branch: task/T-V1-sim-backend-audit
day: 2
priority: 2
depends_on: []
lane: platform-infra
kind: fix
---

## Context

Erik directive (2026-07-03, re-cut after the original auditor died with the host
process): verify the backends are ACTUALLY correct across all design suites and
simulation software. This is a READ-mostly audit + cheap physics checks, not a
rewrite. The prior CAM audit (work/audits/CAM_AUDIT.md) found 4 machine-safety
bugs this way — assume the sim surface hides similar surprises.

## Driver notes (foreman)

- Surface to audit: run_fea_static / run_fea_modal (built-in + PyNite path),
  run_cfd_flow / run_cfd_accurate (incl. foam_case/LBM — note their tests don't
  even collect: trimesh + OCP missing from mcp-venv, same class as the dead-PyNite
  find), piping_solve/operating_point, hydraulic sizing, struct_analyze/check,
  ecad_simulate/tf/impedance, thermal paths if any.
- For each solver: verdict REAL (implements the physics), VALIDATED (matches an
  independent hand-calc/textbook case you run), STUB, or BROKEN — with evidence.
  Run at least one analytic check per family (cantilever tip deflection, pipe
  head loss vs Darcy-Weisbach, plate modal freq, RC filter tf, etc.).
- OSS-first (Erik directive): where a backend is stub/weak, recommend an existing
  OSS solver to adopt instead of hand-rolling (license-check: MIT/BSD/Apache
  in-process, GPL/MPL subprocess-only, AGPL never) — e.g. scikit-fem, CalculiX
  (subprocess), OpenFOAM (already?), fluids/thermo (MIT). Recommendations only;
  no new deps landed in this audit.
- Deliverable: work/audits/SIM_AUDIT.md + a re-runnable scripts/sim_audit.py that
  executes the analytic checks (real counts, no all-skip); proposed task list for
  gaps, with priorities.
- Verify against the REAL engine: AI_CAD_LIBAICAD=core/build-linux/bin/libaicad.so
  via ~/.local/share/AI-CAD/mcp-venv/bin/python. No GUI windows.

## Definition of done

- [ ] Every sim/analysis MCP tool has a verdict row with evidence
- [ ] scripts/sim_audit.py runs the analytic checks with real executed counts
- [ ] Gap → proposed-task table (with OSS-adoption recommendations where apt)
- [ ] Provisioning gaps (trimesh/OCP class) enumerated with exact install cmds
- [ ] `graphify update .` run

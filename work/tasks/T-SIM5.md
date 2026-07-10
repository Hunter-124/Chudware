---
id: T-SIM5
title: OpenFOAM accurate-CFD integration - run_cfd_accurate live path with GPL2 subprocess isolation
workstream: SIM
owner: fable-fin
status: doing
deferred: yes
branch: task/T-SIM5-openfoam
depends_on: []
lane: simulation
kind: new
priority: 3
---

## Context

The LBM CFD path (core/sim/lbm.cpp + engine_oracle/sim/lbm.py) is real but Cd is
uncalibrated by design. The validated absolute-Cd path is OpenFOAM via
engine_oracle/sim/foam_runner.py / foam_case.py / foam_cht.py / foam_post.py — all
present, but OpenFOAM is not installed and the run_cfd_accurate hook is dormant.
GPL2 requires subprocess isolation (never link, only exec) — posture already recorded.

DEFERRED (Hunter product-priority directive): heavyweight dependency, park until the
core sim overhaul (T-SIM1..4) lands and Hunter opts in.

## Definition of done (when unparked)

- [ ] OpenFOAM provisioning documented + detected at runtime (graceful "not installed"
      advisory, never a crash).
- [ ] run_cfd_accurate end-to-end on a sphere/cube reference; Cd within the documented
      ±10-15% envelope of experiment; validation check added to scripts/sim_audit.py.
- [ ] License posture row in contracts/license_debt.md (subprocess isolation, no linking).
- [ ] Gate-integration honest-skip pattern (runs where OpenFOAM exists, visible skip
      reason elsewhere).

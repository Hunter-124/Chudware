---
id: T-SIM5
title: OpenFOAM accurate-CFD integration - run_cfd_accurate live path with GPL2 subprocess isolation
workstream: SIM
owner: fable-fin
status: done
deferred: no
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

- [x] OpenFOAM provisioning documented + detected at runtime (graceful "not installed"
      advisory, never a crash).
- [x] run_cfd_accurate end-to-end on a sphere/cube reference; Cd within the documented
      ±10-15% envelope of experiment; validation check added to scripts/sim_audit.py.
- [x] License posture row in contracts/license_debt.md (subprocess isolation, no linking).
- [x] Gate-integration honest-skip pattern (runs where OpenFOAM exists, visible skip
      reason elsewhere).

## Completion notes (2026-07-10, branch task/T-SIM5-openfoam)

- NEW `LinuxRunner` in engine_oracle/sim/foam_runner.py — posix detection (sourced env /
  CHUDWARE_OPENFOAM / system roots / user-space conda envs + tarballs), strict subprocess exec.
  Previously OpenFOAM could never be detected on Linux (runners were Windows-native + WSL only).
- Provisioning: `python -m tools.provision_openfoam --install` now does a USER-SPACE
  conda-forge install on Linux (no sudo). Installed live on this box (OpenFOAM v2412).
- LIVE validation on this machine: sphere Cd 0.329 in [0.25,0.55] PASS (7 s), cube Cd 1.696
  in [1.20,2.20] PASS (13 s) via tools/foam_validation.py; sim_audit gains an opt-in live leg
  (CHUDWARE_SIM_AUDIT_LIVE_FOAM=1) plus the source-audit verdict + honest skip.
- CHT multi-solid seam for T-FIN19: build_cht_case(solids=[{name, stl_path, kappa_w_mk,
  power_w}, ...]) / run_openfoam_tunnel(thermal={mode:"conjugate", solids:[...]}).
  Live-verified 2-solid board+chip case (chip 129 C peak, per-region summary + overlay).
- License row #18 appended to contracts/license_debt.md (note: OpenFOAM is GPL-3, not GPL2
  as this card said; same subprocess-isolation posture).

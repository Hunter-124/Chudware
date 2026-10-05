---
id: T-SIM10
title: Simulate tab — full multi-physics setup tree with per-part-type disclosure
workstream: SIM
owner: unassigned
status: dropped
branch: task/T-SIM10-simulate-tab-multiphysics
depends_on: []
lane: simulation
kind: new
priority: 1
---

## Context

Hunter directive 2026-07-06 (see `docs/plans/GUI_OVERHAUL_SPEC.md` §5 — the canonical register).
The host/ GUI Simulate workspace tab must host EVERY simulation kind in one place, with the
option surface scoped to what the user actually has open so nobody is overwhelmed:

1. **All sim kinds in the ONE Simulate tab**: structural FEA (static, modal, thermal, buckling,
   fatigue, frequency response, contact), CFD (fast + accurate), kinematic/motion studies,
   **electronics simulation** (SPICE/impedance via the ecad engine — see `ecad_simulate`,
   `ecad_check_impedance` worker endpoints) and **firmware emulation** (`fw_sim_run` /
   `fw_debug_*`). No separate simulation tabs anywhere.
2. **Progressive disclosure by open part type**: electronics-sim + emulation study types are
   offered ONLY when electronics docs (Schematic/PCB/firmware project) are open; mechanical
   study kinds for Part/Assembly docs; motion only when an assembly with mates is open.
3. **Feature/model tree hidden in the Simulate view.** The left pane shows ONLY the simulation
   setup tree (study → material / fixtures / loads / mesh / results; per-kind vocabularies,
   e.g. netlist/probes for electronics sim) rendered in the same visual style as the feature
   tree (same row layout, icons, padding rules from the polish pass).
4. Controls per sim kind stay separated — one setup-tree grammar, per-kind node vocabularies.

## Files / areas touched

- `host/src/ui/sim_manager.cpp/.h` — study-type registry keyed by open-doc capabilities;
  electronics/emulation study nodes.
- `host/src/ui/layout.cpp` + `host/src/app.cpp` — Simulate workspace left pane = sim setup tree
  only (no FeatureManager tabs).
- `host/src/ui/ecad/*`, `services/worker` ecad/fw endpoints — wiring for electronics sim +
  emulation runs and result display.
- Scenario in `host/src/ui/scenarios.cpp` proving: (a) mechanical-only doc shows no
  electronics options; (b) with a PCB open, electronics sim + emulation appear.

## Definition of done

- Every study kind creatable and runnable from the Simulate tab against its engine endpoint
  (or an honest "engine pending" state where the worker lacks the endpoint — documented).
- Disclosure matrix verified by headless shots (both cases above).
- Feature tree absent in Simulate view; setup tree matches feature-tree styling.
- Light + dark theme verified; `docs/GUI_OVERHAUL_SPEC.md` §5 boxes flipped to [x].

## Notes

Builds on the fleet run wf_b556d138-1bf output (motion studies + Simulate workspace routing).
Do not start while that fleet holds `sim_manager.*` — check `git log -5 -- host/src/ui/sim_manager.cpp` is quiet first.


## Supersession note (2026-07-10)

**SUPERSEDED by T-FIN24** (merged 2026-07-10, host/ Simulate tab multiphysics tree +
worker seam). Status moved to `backlog` + `deferred: yes` so the ready queue stays
honest. Original disclosure-matrix intent (mech-only vs electronics-open, handoff
actions, honest engine-pending states) shipped under T-FIN24 against the live `host/`
GUI (not the deleted ui/imgui path this brief once targeted). Do not re-claim this
card — open a follow-up only for gaps found against T-FIN24 DoD.

**2026-10-04 dropped.** The supersession above is explicit and terminal, so the card is
`dropped` and archived in `work/tasks/done/` instead of sitting on the live board as
deferred backlog. Gaps against T-FIN24 get a new card.

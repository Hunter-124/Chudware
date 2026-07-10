# Content-finalization plan: firmware suite, PCB verification, simulation unification

**Date:** 2026-07-10 · **Workstream:** FIN (26 tasks, `work.py board --workstream FIN`) ·
**Goal:** finish the *content* of the program — MCU programming suite with emulation +
simulated sensors/GPIO, pre-fab verification of MCU/FPGA/CPU host PCBs, and a unified,
expanded simulation stack — so the next phase can be pure fixes/fine-tuning.

This is a **monolithic DAG**: every task is a `work/tasks/T-FIN*.md` file with
`depends_on` edges; `work/DAG.md` is the regenerated graph. Execute with subagents
wave-by-wave (see §4); each task file is a self-contained subagent brief.

---

## 1. Where we are (survey findings, 2026-07-10)

Three parallel deep surveys (firmware, ECAD, sim stack) established:

**Firmware suite (engine_oracle/firmware/, services/mcp/firmware_tools.py)** — the
compile→emulate→flash→debug loop is live and production-shaped (CMake builds, Renode/QEMU
emulation, probe-rs/esptool hardware path, pinmap.h from ECAD netlists, graceful
degradation, contracts/firmware_rpc.md frozen). The two holes that keep it from being a
*finished* suite: `gpio_states` is a permanent empty dict (pin assertions can never pass),
and there is **no peripheral/sensor simulation** — only stm32f4_discovery has even a UART.
No GUI surface at all (MCP-only pillar).

**ECAD (engine ecad pkg, core/ecad/, services/mcp/ecad_tools.py)** — schematic→PCB→
ERC/DRC/DFM→Gerber flow live with C++/Python parity; MNA circuit sim validated
(AC/DC/transient/MC/noise); IPC-2141A impedance validated ±5%. Gaps: `ecad_to_3d` exports
bare geometry (no component bodies, no power/thermal metadata — blocks every physics
handoff); **zero MCU/FPGA-specific verification** (no decoupling/sequencing/boot-strap
checks, no I/O-bank or constraint export, no length-matching/termination/via-stub/BGA
rules, no stackup solver, no PDN); ngspice absent.

**Sim stack (core/sim/, services/mcp/)** — 7 FEA types + LBM CFD + OpenFOAM path + motion
kinematics + piping/hydraulic/struct/EM/stackup all live with real solvers and V&V
(T-BL20). Gaps: thermal FEA steady-state only; motion has no dynamics/forces; EM is
one-shot RPC with toast-only GUI; **the only cross-sim handoff is piping→CFD**
(`piping_cfd_handoff`/`piping_verify_cfd`) — a proven pattern, not yet a generalized seam.

## 2. Strategy

1. **One handoff contract, many couplings.** Freeze `aicad.simhandoff/1` (T-FIN15)
   generalizing the piping→CFD pattern (handoff envelope → target study → verify verdict
   with provenance). Every coupling (ECAD→thermal, motion→FEA, hydraulic→piping/FEA,
   thermal→CFD) implements against that one seam.
2. **Metadata is the keystone.** T-FIN8 (component power/thermal/package/rail metadata +
   ecad_to_3d bodies + `aicad.boardphys/1` sidecar) unblocks the host-board rule packs,
   PDN, thermal handoff, vibration screening, and the power budget. It is the highest-
   fan-out node in the DAG.
3. **Honest capability labels.** Formula/heuristic checks (SI pack, vibe screen, power
   budget) state their limits in tool output; anything needing an absent binary
   (renode, OpenFOAM, ngspice) degrades with provisioning guidance, never fakes.
4. **Finish = proven.** The DAG terminates in an E2E golden demo (T-FIN25: sensor-node
   board designed → verified → firmware emulated with simulated sensors → thermally
   solved → heatsink picked → power-budgeted → report) wired into the gate, then a docs/
   MCP freshness sweep (T-FIN26).

## 3. The DAG

```
A. Firmware suite                      B. PCB verification            C. Sim unification/expansion
T-FIN1 gpio extraction ──┐             T-FIN8 component metadata ─┐   T-FIN15 simhandoff/1 contract
  └→ T-FIN2 platforms    │               ├→ T-FIN9  host-board rules   ├→ T-FIN20 motion dynamics→FEA
       └→ T-FIN3 sensors │               ├→ T-FIN10 SI rules           └→ T-FIN22 hydraulic couplings
            └→ T-FIN7 power profile      ├→ T-FIN11 FPGA pack        T-FIN18 transient thermal
T-FIN4 fw doctor ─┐      │               └→ T-FIN13 PDN/IR-drop      T-SIM5 OpenFOAM (existing)
  └→ T-FIN5 fw GUI ←─────┘             T-FIN12 stackup advisor
T-FIN6 fw↔board coverify ←(T-FIN1,T-FIN8)   T-FIN14 ngspice opt.

D. Marquee integrations                        E. Surface & proof
T-FIN16 ECAD→thermal FEA ←(FIN8,FIN13,FIN15)   T-FIN24 Simulate tab (ui/imgui, supersedes
  ├→ T-FIN17 heatsink selection loop                    T-SIM10) ←(FIN1,FIN15,FIN16)
  ├→ T-FIN19 airflow/CHT handoff ←(+T-SIM5)    T-FIN25 E2E golden demo + gate
  └→ T-FIN21 PCB vibration screening                    ←(FIN3,6,9,11,17,23,24)
T-FIN23 power budget ←(FIN7,FIN13)             T-FIN26 docs/MCP freshness sweep ←(FIN25)
```

Existing open nodes linked in: **T-SIM5** (OpenFOAM live path — dependency of T-FIN19,
un-defer when starting wave 2), **T-SIM11** (EM solver family — parallel, optional;
T-FIN24 wires the EM GUI forms regardless), **T-SIM10** (superseded by T-FIN24, deferred).

### New simulations added by this plan
transient thermal FEA (T-FIN18) · rigid-body dynamics with joint reactions (T-FIN20) ·
PDN DC IR-drop / current density (T-FIN13) · PCB modal/vibration screening (T-FIN21) ·
system power/energy budget with firmware-derived duty cycles (T-FIN23) · conjugate-HT
board airflow (T-FIN19) · sensor/peripheral emulation scenarios (T-FIN3).

## 4. Subagent execution (waves from the DAG)

Run waves as parallel subagent fleets; each agent gets its task file + the relevant
`ctx-*` pack, works on `task/<id>-<slug>` branches, merges to master behind a green gate,
per the standing workflow. MCP-freshness rule applies per task.

| Wave | Tasks (parallel) | Notes |
|---|---|---|
| 1 | T-FIN1, T-FIN4, T-FIN8, T-FIN12, T-FIN14, T-FIN15, T-FIN18, T-SIM5 | all roots; FIN8+FIN15 are the keystones — review these two most carefully |
| 2 | T-FIN2, T-FIN5, T-FIN9, T-FIN10, T-FIN11, T-FIN13, T-FIN20, T-FIN22 | fan-out over the keystones |
| 3 | T-FIN3, T-FIN6, T-FIN16 | sensors, co-verify, marquee thermal handoff |
| 4 | T-FIN7, T-FIN17, T-FIN19, T-FIN21, T-FIN24 | loops + GUI surface |
| 5 | T-FIN23, then T-FIN25 | budget, then the golden demo |
| 6 | T-FIN26 | freshness sweep, board reconciliation |

Priorities encode the critical path (P2 spine: FIN1→2→3, FIN8→13→16→17, FIN15, FIN9,
FIN24, FIN25). If capacity is short, P3/P4 leaves (FIN14, FIN21, FIN22) defer without
breaking the spine.

## 5. Definition of "content finalized"

- `fw_sim_run` passes GPIO+sensor-scenario assertions on ≥4 MCU families; fw GUI live.
- A routed MCU/FPGA board can be verified pre-fab: ERC + DRC + host-board pack + SI pack
  + FPGA bank check/constraint export + PDN, all with seeded-violation test fixtures.
- `ecad_thermal_handoff` → heatsink pick is one call / one GUI click; piping-style verify
  verdicts exist for every coupling.
- T-FIN25 golden demo green in the gate.
- After that: feature freeze on this surface; the board pivots to fix/fine-tune tasks.

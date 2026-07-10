# FIN content capability map (live vs gated)

**Date:** 2026-07-10 · **After:** T-FIN1..26 finalize · **Convention:** T-V1 audit style —
label each surface **LIVE** (runs on this machine with shipped defaults), **GATED** (needs an
optional binary / env and degrades honestly), or **GUI** (host/ surface only).

| Surface | Status | Notes |
|---------|--------|-------|
| GPIO pin assertions (`gpio_states` / ODR snapshot) | LIVE | T-FIN1 Renode hooks; QEMU needs AICAD_GPIO_TRACE |
| MCU platform pack (stm32f4/f1, nrf52, rp2040, …) | LIVE / GATED | Emulation GATED on renode/qemu; build GATED on cross-toolchain |
| Sensor scenarios (`aicad.fwscenario/1`) | GATED | Renode path only; QEMU refuses unsupported tracks honestly |
| `fw_status` doctor | LIVE | Never fails; install hints |
| Firmware workspace GUI | GUI | T-FIN5 host/ firmware workspace |
| `fw_verify_board` static co-verify | LIVE | Dynamic leg GATED on emulator / trace |
| Power profile from emulation | GATED | Needs successful `fw_sim_run` + board link |
| Component eng metadata + boardphys | LIVE | T-FIN8 `ecad_to_3d` sidecar |
| Host-board / SI / FPGA / PDN packs | LIVE | Formula/heuristic honesty labels in tool output |
| Stackup advisor | LIVE | T-FIN12 |
| ngspice backend | GATED | Falls back to internal MNA |
| `aicad.simhandoff/1` | LIVE | T-FIN15 builders + envelope validation |
| ECAD → thermal FEA | LIVE / GATED | Geometry LIVE with build123d; mesh/solve GATED on sim libaicad |
| Heatsink selection | LIVE / GATED | Network estimate without mesh; FEA re-solve with libaicad |
| Transient thermal | LIVE | T-FIN18 core path |
| Board airflow / CHT | GATED | OpenFOAM (T-SIM5 / T-FIN19) |
| Motion dynamics → FEA | LIVE | T-FIN20 |
| PCB vibe screen | LIVE | T-FIN21 Miles/Steinberg heuristic |
| Hydraulic couplings | LIVE | T-FIN22 |
| System power budget | LIVE | Degraded without firmware profile |
| Simulate tab multiphysics | GUI + LIVE | T-FIN24; worker multiphysics_rpc seam |
| EM GUI forms | GUI + LIVE | Live backend; deeper solvers = T-SIM11 (deferred) |
| E2E golden demo | LIVE | `scripts/demo_sensor_node.py` + gate 4.7; fw_sim SKIP without Renode |

## Supersessions

- **T-SIM10** → superseded by **T-FIN24** (status `backlog`, `deferred: yes`)
- **T-SIM11** → remains deferred optional EM solver family; T-FIN24 wires live EM GUI forms

## Proof

- Gate: `stage_fin_content` in `scripts/gate.sh`
- Demo narrative: `docs/FIN25_SENSOR_NODE_GOLDEN_DEMO.md`
- Plan: `docs/SIM_FW_FINALIZE_PLAN_2026-07-10.md`

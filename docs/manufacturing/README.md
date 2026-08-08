# Manufacturing / CNC / Additive overhaul — master index

Design corpus for the Manufacture overhaul: real machine control, a deterministic physical
twin, additive manufacturing with an internally modifiable slicer, probing/metrology, a
home-grown predictive perception model, and the breadth tracks (industrial monitoring,
turning, laser/plasma/waterjet, pick-and-place, robot offline planning).

Read in this order. `01_definition_of_done.md` is the authority when two documents disagree;
`10_gateway.md` owns every shared schema and the stable rule ids (`MFG-*`) the other
documents cite.

| file | what it is |
|---|---|
| `00_decisions.md` | locked product decisions (autonomy, controllers, slicer, printers, model, retention) |
| `01_definition_of_done.md` | the 20 acceptance criteria every wave is measured against |
| `02_plan_review.md` | the independent architecture review; each finding is closed in the owning doc |
| `10_gateway.md` | **foundation** — identities, `FreshValue<T>`, capability truth, journal/seq, invalidation, Tier-0, gates, approval, arbiter, lease, Grbl boundary, printer adapters, MCP risk gating, stable rule ids |
| `11_twin.md` | deterministic engineering twin (tick clock, Grbl emulation, stock/collision, virtual sensors, replay, training wrapper) |
| `12_metrology.md` | probing + quality: evidence classes, accuracy tiers, offsets, inspection |
| `13_additive.md` | additive as a sibling domain + the AGPL PrusaSlicer sidecar fork contract |
| `14_perception.md` | Forecast-to-Compress MLDP: the predictive model, datasets, training, ONNX deployment |
| `15_ux.md` | the operator surfaces, Offline vs Live truth boundary, gate/waiver rendering |
| `16_monitoring.md` | read-only MTConnect then OPC UA telemetry |
| `17_turning.md` | single-spindle XZ turning |
| `18_cutting.md` | laser / plasma / waterjet process profiles |
| `19_pnp.md` | OpenPnP virtual + job interchange |
| `20_robot.md` | ROS 2 / MoveIt offline planning |
| `90_research_printers.md` | primary-source printer-integration research |

## Non-negotiables (short form)

1. Capability truth levels are explicit per device: `plan generate post parse simulate monitor
   transport execute safety_integration`. `assumed` disables exactly like absent, and names the
   missing level.
2. One external freshness shape, `FreshValue<T>`; `v` is null unless `state=="ok"`.
3. Two-stage gates. Stage 1 admits (`state==ok && src==probed`, or a bound `procedural_fact`);
   admission is never sufficient. Stage 2 is `pass|fail|not_applicable`.
4. Simulation and perception never satisfy an execute-side gate.
5. One command owner per device; approval is validated only in the deterministic arbiter.
6. No transparent resume after reset, alarm, hard limit, disconnect, or power loss.
7. Software hold/abort is not a safety function and never substitutes for E-stop/interlocks.
8. Zero new normal-gate SKIPs; optional hardware qualification lives in opt-in scripts.

## Execution phases

Cross-domain phases. Gateway-internal wave ids (`W1.1` …) are `10_gateway.md` §19.

| phase | contents | unblocks |
|---|---|---|
| **P0** | baseline landed (`scripts: scope hostd staleness inputs`, `docs: restore refactor log ordering`) | — |
| **P1** | gateway `W1.1` + `W1.2`: contract, `core/device/*.h`, `FreshValue<T>`, journal + seq + result registry (all ten domains' kinds) + invalidation, family default-off, contract section allocation | everything |
| **P2** | parallel domain cores: `grbl_protocol` (W2.1), twin core, additive model + slicer sidecar, metrology schemas, perception schemas + training core, monitoring adapter, turning, cutting, PnP, robot | P3, P4 |
| **P3** | authorization path: `tier0` + `policy`, `gates`, `approval` + `lease` + `arbiter`, `grbl_adapter` + `emulator_transport` (E1–E3) | P4 |
| **P4** | process + reach: `chudware-mfgd`, `gateway_rpc.py` bridge, `mfg_tools.py` + risk classes, host `manufacture/**` panels | P5 |
| **P5** | real transports (serial/TCP/WS), Moonraker → OctoPrint → PrusaLink, per-domain UI surfaces | P6, P7 |
| **P6** | perception data capture, training runs, published model/training core | — |
| **P7** | E4→E6 pilot campaign on real hardware, then `execute` advertisement | commercial machine control |

`P7` is operator-attended and hardware-gated by design: nothing before it may advertise
`execute` on a physical machine.

# Manufacturing / CNC / Additive overhaul — master index

Design corpus for the Manufacture overhaul: real machine control, a deterministic physical
twin, additive manufacturing with an internally modifiable slicer, probing/metrology, and the
breadth tracks (industrial monitoring, turning, laser/plasma/waterjet, pick-and-place, robot
offline planning).

Read in this order. `01_definition_of_done.md` is the authority when two documents disagree;
`10_gateway.md` owns every shared schema and the stable rule ids (`MFG-*`) the other
documents cite.

These are **design** documents. The landed implementation is a subset with a different file
layout; [As-built map](#as-built-map) below records, per document, what exists and which proposed
contracts, trees and tests were never built. A path a design doc names that the map lists as not
built is a proposal, not a missing file to go looking for.

The decision record (`00_decisions.md`, `01_definition_of_done.md`) also covers a camera-based
predictive perception model. That research track is not part of this repository; the rules it
imposes on the rest of the system — perception is advisory and never satisfies an execute-side
gate — still apply to any future perception input.

| file | what it is |
|---|---|
| `00_decisions.md` | locked product decisions (autonomy, controllers, slicer, printers, model, retention) |
| `01_definition_of_done.md` | the 20 acceptance criteria every wave is measured against |
| `10_gateway.md` | **foundation** — identities, `FreshValue<T>`, capability truth, journal/seq, invalidation, Tier-0, gates, approval, arbiter, lease, Grbl boundary, printer adapters, MCP risk gating, stable rule ids |
| `11_twin.md` | deterministic engineering twin (tick clock, Grbl emulation, stock/collision, virtual sensors, replay, training wrapper) |
| `12_metrology.md` | probing + quality: evidence classes, accuracy tiers, offsets, inspection |
| `13_additive.md` | additive as a sibling domain + the AGPL PrusaSlicer sidecar fork contract |
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

| phase | status | contents |
|---|---|---|
| **P0** | landed | baseline: the pre-overhaul tree landed in coherent units |
| **P1** | landed | `contracts/manufacturing_gateway.md`, `core/device/*.h`, `FreshValue<T>`, journal + durable seq + ten-domain result registry + invalidation, `manufacture` family default-off, contract section allocation |
| **P2** | landed | `grbl_protocol`, twin core, additive model, slicer sidecar, metrology core, MTConnect monitor, turning, cutting, PnP, robot |
| **P3** | landed | `tier0` + `policy`, `gates`, `approval` + `lease` + `arbiter`, `grbl_adapter` + `emulator_transport` (E1–E3) |
| **P4** | landed | `core/hostd/mfg_lane` serving the `mfg_*` surface, `services/mcp/mfg_tools.py` + motion/safety risk classes, `host/src/ui/manufacture/**` operator surfaces |
| **P5** | landed | serial/TCP/WebSocket transports behind a deny-by-default commissioning allowlist, Moonraker/OctoPrint/PrusaLink adapters, durable attention + notification path, and the lane transport-selection seam |
| **P6** | blocked on hardware | E4–E6 pilot campaign, then and only then `execute` advertisement |

**Two deliberate deviations from the design, both standing:**

1. **No separate `chudware-mfgd` process.** The lane runs inside `chudware-hostd`. The safety
   property the split existed to guarantee is preserved structurally: exactly one lane object owns a
   device, opening one requires a commissioning allowlist entry, and the arbiter remains the only
   approval validator. Revisit only if a device driver turns out to need fault isolation from the
   document daemon.
2. **Printer adapters are Python** (`services/manufacturing/printers/**`), reached through an
   injected worker-call seam. An HTTP + WebSocket client in C++ with no new dependencies is not worth
   its cost. The lane still owns claims, approval, durable seq and events, and strips any authority a
   worker result tries to assert.

`P6` is operator-attended and hardware-gated by design: nothing before it may advertise
`execute` on a physical machine.

Phase status above records the cross-domain phase closing; it does not mean every artifact a design
document proposed was built. The landed subset per document is below.

## Migrating obsolete serial allowlists

A serial entry requires `usb_vid`, `usb_pid`, and `usb_serial` together. A
tupleless entry invalidates the whole allowlist; a stable `/dev/serial/by-id`
path alone is not device identity. The refusal names the file and line and
reports `serial USB identity requires vid, pid, and serial together`.

With the equipment idle, remove an obsolete entry or recommission it using the
device's observed complete USB identity. Do not invent identifiers or weaken the
allowlist to retain an old claim. An initialized lane reloads the corrected file
on its next request and applies normal claim invalidation. A lane disabled by an
invalid allowlist at startup requires a planned idle restart after correction;
never restart active manufacturing merely to clear this diagnostic.

## As-built map

Verified against the tree on 2026-10-04. "Not built" lists proposed artifacts the design documents
name that do not exist; design docs are not rewritten to match.

| doc | landed (where it lives) | proposed in the doc, not built |
|---|---|---|
| `10_gateway.md` | `contracts/manufacturing_gateway.md`; `core/device/*` (capability, ids, Grbl protocol/adapter, emulator transport); `core/manufacturing/*` (`FreshValue`, journal, result registry, invalidation, tier0, policy, gates, approval, lease, arbiter) built as `chudware_manufacturing` together with `core/hostd/mfg_lane.*` and the deny-by-default transports `services/manufacturing/gateway/{device_allowlist,transport_serial,transport_tcp,transport_ws}.*`; `services/mcp/mfg_tools.py`, `services/mcp/mfg_plan_tools.py`; `services/manufacturing/notify/`; tests `core/tests/test_{mfg_foundation,grbl_protocol,device_lane,mfg_authorization,mfg_lane,transport_serial,transport_net}.cpp`; fixtures `fixtures/mfg/gateway/`; qualification `scripts/qualify_mfg_e3.{py,sh}`, `scripts/qualify_mfg_printer.{py,sh}` | separate `chudware-mfgd` process (standing deviation 1); per-module test files (`test_mfg_arbiter`, `test_mfg_gates`, `test_mfg_policy`, `test_mfg_tier0`, `test_grbl_adapter`, …); `host/src/ui/mfg/**`, `host/src/ui/print/**`; `scripts/qualify_cut_e2.sh`, `scripts/qualify_pnp_q1.sh` |
| `11_twin.md` | `core/twin/{twin.h,twin.cpp,wide_math.h}` (`chudware_twin`, linked to the shared Grbl codec); `core/tests/test_twin.cpp`; `fixtures/mfg/twin/` | `contracts/manufacturing_twin.md`; split headers `core/twin/{clock,entity,state,faults,grbl_emu,record,vsensor,vcamera}.h`; `core/cam/stock_step.*`; `docs/TWIN.md`; the eleven `test_twin_*` executables; `services/manufacturing/twin/` |
| `12_metrology.md` | Python only: `services/manufacturing/metrology/` (evidence, tiers, probe, registration, offsets, transform, limits, inspection) with `test_metrology.py`; `fixtures/mfg/metrology/` | `contracts/manufacturing_metrology.md`; `core/metrology/**` (incl. `offsets.cpp`, `session.cpp`); `services/mcp/metrology_tools.py`; `contracts/fixtures/metrology/`; `core/tests/test_metrology_frames.cpp`; `host/src/ui/manufacture/inspectors.cpp` |
| `13_additive.md` | `core/print/model.{h,cpp}` (`chudware_print`) + `core/tests/test_print_model.cpp`; `services/manufacturing/slicer/` (arms-length argv/file/subprocess seam to an installed PrusaSlicer, vendor-bundle reader, G-code checks); `services/manufacturing/printers/` (Moonraker, OctoPrint, PrusaLink adapters + `worker_rpc.py`); `services/manufacturing/print_media.py`; `fixtures/mfg/{print,printers}/` | `contracts/manufacturing_print.md`; vendored slicer fork (`vendor/slicer/`, `scripts/vendor_slicer.py`); `core/print/gcode_parse`; host print UI (`host/src/ui/print/**`, `host/src/ui/manufacture/print/**`); `services/mcp/mfg_print_tools.py`; `services/gateway/` |
| `15_ux.md` | `host/src/ui/manufacture/` (`manufacture_panel`, `device_panel`, `run_panel`, `approval_dialog`, `attention_panel`, `mfg_mirror`) | `contracts/manufacturing_ux.md`; `manufacture_workspace.cpp`; `host/tests/test_mfg_{fresh,mirror}.cpp`; per-domain panels (print, monitor, turning, cut, PnP, robot) |
| `16_monitoring.md` | `services/manufacturing/monitor/` (`mtconnect`, `normalize`, `replay`, `opcua/` client/codec/protocol/machine tools/fake server) with tests; `fixtures/mfg/monitor/` | `services/mcp/mfg_monitor_tools.py`; monitor `service`/`model`/`tier0` modules; `collector_opcua/`; `services/manufacturing/rpc/journal_client.py`; host monitor panel; `scripts/mfg_monitor_readonly_gate.sh`, `scripts/qualify_monitor_e{1,2}.sh` |
| `17_turning.md` | `core/cam/turn/process.{h,cpp}`; `core/tests/test_cam_processes.cpp`; `fixtures/mfg/turn/` | split modules (`turn_profile`, `turn_tool`, `turn_semantics`, `workholding`, `post_turn`, `turn_tier0`); `core/cam/stock_step.*`; `services/manufacturing/turning/`; turning panel; `core/tests/test_cam_turn.cpp`; `scripts/qualify_turn_e2.sh` |
| `18_cutting.md` | `core/cam/cut/process.{h,cpp}`; `core/tests/test_cam_processes.cpp`; `fixtures/mfg/cut/` | per-process modules (`op_laser`, `op_plasma`, `op_waterjet`, `recipe`, `vector_plan`, `plasma_state`, `parse_gcode`, `post_cut`, `cut_sim`, `emulator`); cut process panel |
| `19_pnp.md` | Python only: `services/manufacturing/pnp/` (`capability`, `model`, `openpnp`, `simulator`) with tests; `fixtures/mfg/pnp/` | `contracts/mfg_pnp.md`; `core/pnp/**`; `pnp/{rpc,tier0,evidence}.py`; `services/manufacturing/rpc/`; PnP panel; `scripts/qualify_pnp_q1.sh`, `scripts/smoke_pnp_offline.sh` |
| `20_robot.md` | Python only: `services/manufacturing/robot/` (`capability`, `model`, `urdf`, `kinematics`, `collision`, `trajectory`, `safety_boundary`) with tests; `fixtures/mfg/robot/` | `contracts/mfg_robot.md`; `core/robot/**`; robot panel and mirror; further robot modules (`cell`, `collide`, `determinism`, `evidence`, `export_traj`, `mesh_io`, …); `scripts/qualify_robot_e3.sh` |

`00`, `01` and `90` are decision and research records; they describe no
separate implementation tree.

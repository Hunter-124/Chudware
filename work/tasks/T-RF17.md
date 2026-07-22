---
id: T-RF17
title: RF stream-Z ECAD gaps cutover to core/ecad (node R4c)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF17-ecad-gaps-cpp
depends_on: [T-RF3]
lane: ecad-firmware
kind: overhaul
priority: 3
---

## Context

ECAD routes C++ by default on Linux/macOS. Probe-verified 2026-07-22
(contracts/web_parity.md): `ecad_fill_zones` + `ecad_simulate` are ALREADY
libaicad-dispatched. Remaining gaps: doc-id forms of ecad_autoroute / ecad_to_3d /
ecad_route_diff_pair, plus ecad_design and ecad_import_kicad*. On Windows ECAD still
defaults Python.

## Scope

Port the remaining ECAD methods to core/ecad per contracts/ecad/, reach parity on the
ECAD corpus (incl. services/mcp/test_ecad_sim_e2e.py scenarios), flip routing, delete
the Python modules in the same commit. Unblocks web ECAD and the Windows default flip.

## Definition of done

- [ ] All ecad_* served by libaicad on Linux; corpus + e2e parity green.
- [ ] Python ecad modules deleted same commit; routing flipped (integrator merge).
- [ ] gate.sh green; REFACTOR_LOG handoff; web_parity inventory shrinks.

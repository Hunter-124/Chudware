---
id: T-FIN14
title: Optional ngspice backend - provisioned pass-through beside the internal MNA solver
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN14-ecad-ngspice-backend
depends_on: []
lane: ecad-firmware
kind: new
priority: 4
---

## Context

The Python MNA solver is validated but caps model fidelity (no vendor .SUBCKT/.MODEL
libraries). Add ngspice as an optional subprocess backend: provision detection (dnf
install ngspice; wire into the provisioning registry), netlist emission from the existing
schematic model, result parity mapping back into the ecad_simulate result shape, and a
backend param (auto|mna|ngspice) defaulting to mna. Vendor model import (.lib/.subckt
attach to a component) rides along. MNA remains the deterministic default; ngspice unlocks
real op-amp/regulator vendor models.

## Files / areas touched

- engine sim circuit backend seam + ngspice_backend.py
- provisioning registry entry + status probe
- services/mcp/ecad_tools.py — backend param on ecad_simulate/ecad_tf
- parity tests: RC/divider identical results across backends

## Definition of done

- With ngspice installed, backend='ngspice' runs transient/AC/DC and matches MNA on parity fixtures
- Vendor .subckt attached to a symbol simulates (one op-amp demo)
- Absent ngspice degrades honestly to mna with a note
- Docs updated (cad_guide('ecad') sim section)

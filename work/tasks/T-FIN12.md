---
id: T-FIN12
title: Stackup advisor - impedance-target-driven layer stackup solve
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN12-ecad-stackup-advisor
depends_on: []
lane: ecad-firmware
kind: new
priority: 3
---

## Context

Given layer count, impedance targets per net class (50R SE, 90R/100R diff), and a
fab-capability table (min trace/space, dielectric menu), solve a stackup: layer order,
prepreg/core heights, reference-plane assignment, and the trace geometries that hit each
target — inverting the existing validated IPC-2141A formulas. Output feeds DRC net-class
widths and the SI pack. Include 2-3 canned fab profiles (e.g. JLC-style 4/6-layer menus)
as data.

## Files / areas touched

- engine ecad stackup.py — solver over impedance.py inverses
- fab capability profiles as data files
- services/mcp/ecad_tools.py — ecad_stackup_solve; wire result into board stackup + net classes
- tests: known-good stackups reproduce published geometries within tolerance

## Definition of done

- 4-layer 50R/100R solve matches a published reference stackup within 5%
- Solved geometries land in board net classes so autoroute/DRC honor them
- Infeasible targets fail honestly with the binding constraint named

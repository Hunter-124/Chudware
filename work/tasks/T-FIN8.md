---
id: T-FIN8
title: Component engineering metadata layer + ecad_to_3d with bodies and thermal/power map
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN8-ecad-component-metadata
depends_on: []
lane: ecad-firmware
kind: overhaul
priority: 2
---

## Context

Keystone for board verification and every ECAD->physics handoff. Today ecad_to_3d
exports bare board+copper; components carry no engineering data. Extend the component
library/BOM schema with: power_dissipation_w (typ/max), theta_ja/theta_jc, package body
dims/height, per-rail supply currents (active/sleep), voltage domains, sequencing
requirements, temperature limits (Tj/Ta max). Seed values for the bundled library's common
parts; allow per-instance overrides in sim_params. Upgrade ecad_to_3d to place component
bodies (box/cylinder from package dims at footprint locations) and emit a machine-readable
sidecar map: component -> solid ref, power_w, theta, limits (aicad.boardphys/1). This
sidecar is the seam T-FIN13/16/21/23 and the rule packs consume.

## Files / areas touched

- engine ecad library schema + bundled component lib seeding
- engine_oracle/ecad/threed.py — component bodies + aicad.boardphys/1 sidecar
- contracts/ecad/ — boardphys schema doc
- services/mcp/ecad_tools.py — surface metadata in ecad_status/describe_board
- services/mcp/test_ecad_tools.py — metadata roundtrip + to_3d sidecar tests

## Definition of done

- ecad_to_3d output shows component bodies; sidecar maps each to power/thermal metadata
- Bundled library parts (regulators, MCUs, passives) have seeded credible values with source notes
- describe_board reports per-component power + board total
- Schema documented; tests cover missing-metadata degradation (honest nulls, not zeros)

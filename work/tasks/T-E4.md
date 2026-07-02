---
id: T-E4
title: 3D CFD handoff for pipe internals
workstream: E
owner: opus-finch
status: doing
branch: task/T-E4-cfd-handoff
day: 8
depends_on: [T-E2]
---

## Context

For details 1D cannot answer (mixing, erosion hotspots, dead legs): extract the internal fluid volume from a routed pipe run (OCCT boolean) and hand it to the existing LBM/OpenFOAM CFD path. DAG verify-node checks: velocity limits, pressure-drop budget vs the 1D result.

## Definition of done

- [ ] internal_volume tool produces a watertight fluid solid from a pipe run
- [ ] One-click handoff to run_cfd_flow / run_cfd_accurate
- [ ] Verify node compares 1D vs 3D pressure drop within tolerance

## AI hints

- `graphify query "cfd flow study setup"`
- Existing: run_cfd_flow, run_cfd_accurate MCP tools

---
id: T-BL04
title: Fatigue + thermal FEA
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL04-fatigue-thermal
depends_on: []
---

## Context

S-N curve fatigue post-processing on existing static FEA results; CalculiX thermal via subprocess. Expose both as DAG verify-node checks.

## Definition of done

- [ ] Fatigue life estimate tool on static results
- [ ] Thermal steady-state solve via CalculiX subprocess
- [ ] Both callable as MCP tools and DAG end conditions

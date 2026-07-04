---
id: T-BL08
title: Tolerance stack-up analysis
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL08-tolerance-stackup
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

1D worst-case + RSS stack-up over feature dimensions; cheap to build on the existing dimension/constraint model, high value in DFM review.

## Definition of done

- [ ] stackup_analyze MCP tool with worst-case and RSS modes
- [ ] Report names contributing dims sorted by sensitivity
- [ ] Hand-calc-validated against a textbook stack-up example (cited reference, literal
      expected values — T-D3/T-BL18 discipline)
- [ ] MCP freshness (WORKFLOW.md §6): server instructions + cad_guide review/DFM topic
      mention the tool in the same branch

## Progress log

- 2026-07-04 [fable-helm] Promoted backlog→todo per Hunter's live fitment/critical-dims
  directive (this is the tolerance-math leg of the fitment toolkit: T-BL49 interface
  alignment, T-BL50 critical-dim coverage, T-BL08 stack-up). Coordinate: sim MCP
  registrations append-only, rebase early — T-BL04/16/17/18 in flight same day.

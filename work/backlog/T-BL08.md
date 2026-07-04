---
id: T-BL08
title: Tolerance stack-up analysis
workstream: backlog
owner: sonnet-brill
status: review
branch: task/T-BL08-tolerance-stackup
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

1D worst-case + RSS stack-up over feature dimensions; cheap to build on the existing dimension/constraint model, high value in DFM review.

## Definition of done

- [x] stackup_analyze MCP tool with worst-case and RSS modes
- [x] Report names contributing dims sorted by sensitivity
- [x] Hand-calc-validated against a textbook stack-up example (cited reference, literal
      expected values — T-D3/T-BL18 discipline)
- [x] MCP freshness (WORKFLOW.md §6): server instructions + cad_guide review/DFM topic
      mention the tool in the same branch

## Progress log

- 2026-07-04 [fable-helm] Promoted backlog→todo per Hunter's live fitment/critical-dims
  directive (this is the tolerance-math leg of the fitment toolkit: T-BL49 interface
  alignment, T-BL50 critical-dim coverage, T-BL08 stack-up). Coordinate: sim MCP
  registrations append-only, rebase early — T-BL04/16/17/18 in flight same day.
- 2026-07-04 [sonnet-brill] Implementation complete:
  - engine/tolerance/stackup.py — pure-Python 1D WC+RSS math, no C++ dependency
  - engine/tolerance/test_stackup.py — 25 tests, hand-calc validated against textbook
    3-dim gap example (Fischer CRC 2011 Ch.3): G_nom=3.000, T_wc=0.115, T_rss=0.06874 mm
  - services/mcp/tolerance_tools.py — stackup_analyze MCP tool (mode: wc/rss/both)
  - services/mcp/test_tolerance_tools.py — 14 MCP envelope tests
  - server.py patched (append-only): register_tolerance_tools(mcp)
  - cad_tools.py: tolerance_stackup guide topic + fallback list + cad_guide docstring
  - test_mcp_freshness.py: tolerance_stackup family + signals + required lists updated
  All 44 tests pass (25 engine + 14 MCP + 5 freshness).

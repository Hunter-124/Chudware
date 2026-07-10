---
id: T-FIN10
title: High-speed/SI rule pack - length matching, termination, via stubs, BGA fanout
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN10-ecad-highspeed-rules
depends_on: [T-FIN8]
lane: ecad-firmware
kind: new
priority: 3
---

## Context

Formula/heuristic-based SI verification (no field solver, honest about it): diff-pair
intra-pair skew and inter-pair length-matching groups (user-declared match groups, e.g.
DDR byte lanes) with tolerance; termination presence/topology checks (series R near
driver, parallel/Thevenin at end) driven by net class; via-stub length warnings vs signal
rise time; BGA fanout checks (via-in-pad flags, escape pattern feasibility, courtyard
congestion). Builds on existing diff-pair routing + IPC-2141A impedance modules.

## Files / areas touched

- engine ecad si_rules module + net-class/match-group schema additions
- engine_oracle/ecad/impedance.py — reuse for stub/termination math
- services/mcp/ecad_tools.py — ecad_run_sicheck + net-class declaration path
- fixtures: DDR-ish match group board, unterminated clock board

## Definition of done

- Match-group with out-of-tolerance member flagged with per-net delta
- Termination and via-stub checks trip on seeded fixtures, pass on clean
- Limitations (no field solver, no IBIS) stated in tool output + docs
- cad_guide('ecad') gains an SI-verification worked example

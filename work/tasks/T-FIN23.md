---
id: T-FIN23
title: System power/energy budget - rails tree + firmware duty cycle + battery verdict
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN23-system-power-budget
depends_on: [T-FIN7, T-FIN13]
lane: simulation
kind: new
priority: 3
---

## Context

Unify the power story: a rail tree model (source -> converters with efficiency
curves -> loads from T-FIN8 component metadata), duty-cycle weighting from the firmware
emulation power profile (T-FIN7) when present, cross-checked against PDN copper capability
(T-FIN13). Extends verify_battery into a real system energy budget: battery life for a
scenario, per-rail loading vs converter ratings, worst-rail warnings. Pure bookkeeping
math, fully testable.

## Files / areas touched

- services/mcp/power_budget.py — rail tree model + budget solve
- verify_battery extension consuming powerprofile + boardphys artifacts
- tests: sensor-node fixture with sleep/wake duty cycle -> battery-life hand-calc match

## Definition of done

- Sensor-node demo: battery life within hand-calc tolerance using the emulation profile
- Converter overload seeded fixture flagged; margins reported per rail
- Works degraded (no firmware profile -> datasheet-typ duty assumptions, labeled)

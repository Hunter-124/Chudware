---
id: T-RF9
title: RF wave-1 mode allow-list parity + leftover TODO seams (node R1f)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF9-mode-parity
depends_on: [T-RF3]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

Agent|Plan|Swarm|Edit|Ask tool gating is implemented twice: Python
services/agent/copilot_mode_tools.py (438 lines, gates agent_turn.run_turn) and C++
core/agent/run.cpp (options.mode at :575, lighter allowlist at :1533) — enforcement can
diverge between in-app (C++) and DAG/MCP (Python) runs. Leftover placeholder seams:
services/agent/workspace.py:417-426 literal "[TODO T-W14] spawn modeler",
advisor/overseer.py:73 (T-W7), advisor/overseer_tools.py:64 (T-W14).

## Scope

Single source for mode→tool gating (JSON table under contracts/ or services/agent/)
consumed by BOTH runtimes, plus a parity test running the same mode matrix against C++
and Python paths. Resolve the three placeholder seams for real or delete the dead
surface behind them (YAGNI — prefer delete if no live consumer).

## Definition of done

- [x] One mode-gating table; parity test green across runtimes.
- [x] No [TODO T-W*] placeholder strings left in services/agent.
- [ ] gate.sh green; REFACTOR_LOG handoff; MCP freshness if prompts/tools touched.

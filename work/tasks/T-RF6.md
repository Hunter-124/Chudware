---
id: T-RF6
title: RF wave-1 split legacy_chat.cpp god file + reconcile ai_workspace (node R1c)
workstream: RF
owner: unassigned
status: done
branch: task/T-RF6-copilot-split
depends_on: [T-RF3]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

host/src/ui/legacy_chat.cpp is 13,374 lines / 645 KB and is the LIVE copilot (misnamed
"legacy"); host/src/ui/ai/ai_workspace.cpp (1,693) duplicates the chat surface via
reuse hooks. Board task T-DP16 (AI-Agent workspace) depends on this reconciliation.
Pure moves + header splits — no behavior change.

## Scope

Break legacy_chat.cpp into host/src/ui/copilot/ modules: chat_log, composer,
activity_timeline, cards (plan/clarify/swarm/ai-vc), panel host. One chat
implementation, two layouts (overlay pane + full-page AI workspace). Rename away from
"legacy". Verify with CHUDWARE_SHOT scenarios (populated mission, empty state), EYEBALL
the PNGs.

## Definition of done

- [x] No file over ~3k lines in the copilot surface; legacy_chat.* gone.
- [x] ai_workspace renders from the same chat modules (no forked logic).
- [x] Shot scenarios pixel-plausible (eyeballed); gate.sh green.
- [x] REFACTOR_LOG handoff; T-DP16 unblocked on the board.

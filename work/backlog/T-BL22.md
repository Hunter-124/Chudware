---
id: T-BL22
title: Wire dormant AI seams into the live loop - adapter routing/telemetry + regen_diagnose binding
workstream: backlog
owner: fable-foreman
status: doing
branch: task/T-BL22-live-seam-wiring
depends_on: []
lane: ai-copilot
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-H9 Q2 and T-H11 Q1).** Two shipped,
CI-tested seams have no live caller (verified on master 2026-07-03 by grep — zero
references outside their own modules/tests):

1. **Adapter routing + telemetry tag (T-H9):** `RoutingTable.resolve` and
   `RouteDecision.telemetry_tag()` (services/serving/routing.py) exist, but
   `services/agent/loop.py` still builds one provider via `get_provider()` — no
   per-insertion-point routing, no adapter tag on live responses. The whole
   per-adapter canary/telemetry story is inert until this lands.
2. **regen_diagnose (T-H11 / IP-18):** registered as an MCP tool but nothing binds it
   to the rebuild-failure event in the GUI/copilot, so it harvests ZERO real
   correction traces. T-F17 (mission control) merged WITHOUT wiring it.

## Definition of done

- [ ] loop.py resolves provider per insertion point through RoutingTable and stamps
      telemetry_tag on responses (fallback path preserved when no table configured)
- [ ] Rebuild failure in the GUI/copilot surfaces the regen_diagnose fix-preview and
      records the accept/edit/reject trace through the T-H1 telemetry envelope
- [ ] One end-to-end test each: routed live call carries the adapter tag; a corrupt
      doc's rebuild failure produces a diagnosis event

## AI hints

- `work/questions/T-H9.md` Q2, `work/questions/T-H11.md` Q1 — the deferral notes and
  intended call sites
- `graphify query "agent loop provider selection"` and
  `graphify query "rebuild failure event handling"` before touching anything

---
id: T-H9
title: Adapter serving - vLLM per-task LoRA routing, canary rollout, rollback story
workstream: H
owner: unassigned
status: todo
branch: task/T-H9-adapter-serving
day: 14
depends_on: [T-F9, T-H8]
---

## Context

Pillar 16 (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.7–5.8). Server mode: one vLLM
instance with per-task LoRA routing (--enable-lora, adapter per insertion point),
exposed OpenAI-compatible — it enters the app as just another provider under the T-F9
contract with an `adapter` field. Local mode: merged GGUF per task via llama.cpp/LM
Studio (P13 serving posture). Task→model routing table in the T-F9 registry:
`{insertion_point → provider, model, adapter_version, fallback}` where fallback is
ALWAYS deterministic-path-or-frontier (kill-switch rule). Rollout ladder: shadow
(T-H7) → canary (N% of an insertion point's calls, per-user "early adopter" opt-in)
→ promote (human audit gate #2, needs the T-H8 bench report). Rollback = routing-table
flip to the previous DVC-versioned adapter; nothing overwritten. Serving state visible
in Settings — the user always knows which brain answered. HUMAN-GATED: promotion
criteria sign-off and any hosted-relay interaction (master plan AI-CAD Cloud seam).

## Files / areas touched

- vLLM serving config + adapter registry (DVC-pulled adapters)
- provider adapter + routing table in the T-F9 registry (Python + C++ read side)
- canary controls: per-user opt-in toggle, per-insertion-point traffic pct, Settings
  visibility of active adapter/version
- rollback runbook + smoke test

## Definition of done

- [ ] Two different LoRA adapters served from one vLLM instance, routed by insertion
      point, reachable from the app on both OSes as a provider entry
- [ ] Canary flip 0%→10%→0% demonstrated live without restart; every canary response
      tagged in telemetry
- [ ] Rollback to previous adapter version in <1 min via routing flip (documented +
      tested)
- [ ] Fallback path proven: adapter failure degrades to deterministic/frontier without
      user-facing error
- [ ] `graphify update .`; gate green

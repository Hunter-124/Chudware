---
id: T-H9
title: Adapter serving - vLLM per-task LoRA routing, canary rollout, rollback story
workstream: H
owner: opus-flint
status: doing
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

## Driver notes (Fable, 2026-07-02 — pre-paid pointers + Erik's ratified promotion gate)

- ERIK RATIFIED PROMOTION CRITERIA (2026-07-02, binding): challenger must beat-or-match
  the incumbent on BOTH verifier-score AND accept-rate over N real shadow jobs (T-H7's
  shadow_report.py output is the evidence artifact), THEN Erik manually signs off EACH
  promotion. Automated evidence, human trigger. Encode it: a routing-table promote
  change REQUIRES a reference to a signed-off bench/shadow report; there is NO
  auto-promote path in v1. Rollback = routing-table flip to the previous DVC-versioned
  adapter; nothing overwritten, no confirmation needed (rollback is always safe).
- The `routing` stub in contracts/providers.md was RESERVED for this task (recorded
  hot-spot) — fill it there; schema {insertion_point → provider, model, adapter_version,
  fallback} with fallback ALWAYS deterministic-path-or-frontier (kill-switch rule).
- Server mode enters as just another T-F9 provider with an `adapter` field — no new
  provider stack; LoggingProvider (T-H7) must wrap it unchanged.
- Settings visibility: read-only "which brain answered" line; if the settings panel files
  are contested by a G-wave owner, post a question file per first-claimer rule.
- Anything touching AI-CAD Cloud relay = HUMAN-GATED, do not build; batch to Erik.

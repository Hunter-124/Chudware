---
id: T-H7
title: Frontier-API logging middleware + shadow-mode comparison harness
workstream: H
owner: unassigned
status: todo
branch: task/T-H7-frontier-logging
day: 12
depends_on: [T-H1]
---

## Context

Pillar 16 (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.8, §6). "Ship frontier-backed,
log everything": a provider-layer middleware (both `services/agent/providers/` and
`core/agent/providers.cpp` — implement once per the T-F9 contract seam) that wraps
every model call in the T-H1 artifact envelope: full request/response sidecars,
insertion-point tag, verifier scores of whatever the output produced downstream.
Initial corpus = team dogfood (consent full). Plus SHADOW MODE: a second
model/adapter runs the same call in parallel (or async replay from the log), its
output scored by the T-H4 battery but never shown — the safe stage-1 of
canary rollout and the cheapest honest eval of a custom model on real traffic.
Insertion-point tagging for the three frontier-first tasks (IP-12 elicitation, IP-18
regen-fix, IP-20 drawing/doc gen) lands here even before those features fully ship.

## Files / areas touched

- provider middleware in Python + C++ (thin; conforms to `contracts/providers.md`
  seam from T-F9 when it lands — do not fork the provider stack)
- shadow executor: replay-from-log mode (async, rate-limited) + live-parallel mode
- `tools/flywheel/shadow_report.py` — incumbent vs challenger verifier-score /
  accept-rate comparison tables

## Definition of done

- [ ] Every agent model call on both paths emits the artifact envelope with sidecars;
      overhead <10 ms per call
- [ ] Shadow replay over ≥100 logged calls produces a comparison report (verifier
      scores, latency, cost) between two providers/adapters
- [ ] Consent + offline rules respected end-to-end (shadow never calls cloud in
      offline mode)
- [ ] `graphify update .`; gate green

## Driver notes (Fable, 2026-07-02 — next-wave prep)

- Emission helpers already exist (T-H1): emit_ai_artifact/emit_ai_artifact_action in
  core/agent/events.{h,cpp} (call-site precedent: core/agent/run.cpp run_design);
  Python _emit_ai_artifact in services/agent/loop.py. Your middleware WRAPS provider
  calls and reuses these — do not add a third emission path.
- T-F9 (opus-pike) is freezing contracts/providers.md in parallel — your middleware
  must conform to it; if it hasn't merged when you start, read its branch
  (task/T-F9-providers-contract) and coordinate via work/questions/T-H7.md.
- GUI target cannot link core/agent (C-ABI bridge only — see work/questions/T-H1.md);
  middleware lives in the agent/services layer, never in host/ui code.

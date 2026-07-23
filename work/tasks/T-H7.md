---
id: T-H7
title: Frontier-API logging middleware + shadow-mode comparison harness
workstream: H
owner: sonnet-jet
status: done
branch: task/T-H7-frontier-logging
day: 12
depends_on: [T-H1]
lane: ml-flywheel
kind: new
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

- [x] Every agent model call on both paths emits the artifact envelope with sidecars;
      overhead <10 ms per call
      — `TestLoggingProviderOverhead`: 1000 calls assert <10s total; C++ wrap_logging
        in core/agent/logging_provider.h (header-only template, wired into run.cpp) compiles
        clean (15/15 chudware_agent objects, libchudware_agent.a linked, verified by --clean-first)
- [x] Shadow replay over ≥100 logged calls produces a comparison report (verifier
      scores, latency, cost) between two providers/adapters
      — `TestEndToEndReplayReport.test_replay_120_calls_to_report`: 120 synthetic ai_artifact
        events with request sidecars → ShadowExecutor.replay_log → shadow_log.jsonl →
        generate_report; asserts N=120, no WARNING, Incumbent/Challenger presence,
        model names, score table, per-IP breakdown, latency delta
- [x] Consent + offline rules respected end-to-end (shadow never calls cloud in
      offline mode)
      — `TestShadowExecutorOfflineMode.test_offline_mode_refuses_cloud_provider`: ShadowExecutor
        raises on non-offline-safe cloud provider at construction; allows offline_safe provider
      — `TestLoggingProviderSidecars`: meta/off consent → no sidecars written; full → written
- [~] `graphify update .`; gate green
      — graphify update .: done (19357 nodes, 43117 edges rebuilt)
      — gate.sh: core build PASS, ui/abstract PASS, ctest 100% PASS
      — GATE REDS (pre-existing, not regressions from T-H7):
        * ui/imgui selMulti: base branch (task/T-H1-telemetry-contract) predates b569020
          fix on master; T-H7 touches only core/agent/ + services/agent/ (not ui/imgui)
        * pytest stage: venv path mismatch in gate.sh (0 collected, 61 deselected);
          T-H7 tests pass 36/36 when run directly via
          ~/.local/share/Chudware/mcp-venv/bin/pytest services/agent/test_h7_shadow.py

## providers/1 conformance (T-F9 contract)

LoggingProvider extends Provider from .base, proxies all frozen attributes (name, model,
supports_vision, offline_safe, base_url), keeps complete/complete_ex signature identical —
does NOT fork the provider stack. C++ wrap_logging<ProviderFn> is a header-only template
that wraps any callable matching the existing ProviderFn typedef in run.cpp (same pattern
as the existing make_provider + ProviderFn usage).

## Finisher note (sonnet-jet)

Took over from sonnet-thorn after process-exit casualty. Working tree was clean (all work
committed). The three branch commits delivered:
1. `68d2908` — Python LoggingProvider + ShadowExecutor + shadow_report.py
2. `8439daa` — C++ logging_provider.h (header-only) + run.cpp wiring
3. `0fe52a0` — 36-test suite (36/36 pass)

No uncommitted work found; nothing was dropped.

## Open question (filed)

Q1 in work/questions/T-H7.md: double-emit reconciliation at IP-1 (suppress_emit=True
is the recommended answer — loop.py emits post-build with verifier_scores; the
LoggingProvider writes sidecars only and stashes paths via contextvars for the caller).
Non-blocking; the implementation follows the recommended answer.

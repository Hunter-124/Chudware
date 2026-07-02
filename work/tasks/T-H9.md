---
id: T-H9
title: Adapter serving - vLLM per-task LoRA routing, canary rollout, rollback story
workstream: H
owner: opus-flint
status: done
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

- [~] Two different LoRA adapters served from one vLLM instance, routed by insertion
      point, reachable from the app on both OSes as a provider entry.
      DONE (code+CI): the `vllm` provider (OpenAI-compatible, LoRA-by-`model`-field,
      `services/agent/providers/vllm_provider.py`) is wired into `config.py`
      `_resolve_provider` + loopback offline classification; the routing table
      (`services/serving/routing.py`) routes any number of insertion points to distinct
      `provider/model/adapter_version`. 9/9 provider tests pass with `openai` present
      (verified in a scratch venv; 4 run + 5 SDK-skip in the gate venv, exactly like the
      whole openai/anthropic/openrouter family).
      LIVE-GATED (needs a real GPU + vLLM, cannot run in CI): actually serving two adapters
      from one live instance + two-OS reachability. FOR ERIK — exact commands in
      `docs/serving_runbook.md` §1 (launch `vllm serve --enable-lora --lora-modules
      IP-13/v3=... IP-1/v2=...`) and §7 (WSL2 for Windows).
- [~] Canary flip 0%→10%→0% demonstrated live without restart; every canary response
      tagged in telemetry.
      DONE (code+CI): `serving_set_canary` rewrites the `config.json` `routing` subtree
      in place (no restart); deterministic SHA-256 bucketing makes the flip reproducible
      (`test_canary_flip_live_zero_ten_zero`). The telemetry tag is produced by
      `RouteDecision.telemetry_tag()` → `ai_artifact.model.adapter`
      (`test_every_canary_response_is_tagged`); both the Python (logging_provider.py:274)
      and C++ (run.cpp:836) emit paths already reserve the `adapter` slot.
      SEAM (see work/questions/T-H9.md Q2 — foreman to rule): the LIVE loop-side wiring
      (loop.py resolving per-IP + splicing the tag into its emit) is a follow-up on the
      loop-integration surface — loop.py today builds one provider via get_provider() and
      has no per-IP routing (even T-H7's wrap() isn't wired into loop.py live yet), so
      threading it now = building routing scaffold in a hot file overlapping T-F17.
- [x] Rollback to previous adapter version in <1 min via routing flip (documented +
      tested). `serving_rollback` / `promotion.rollback` = pure routing flip, nothing
      overwritten (adapters are DVC-versioned), no confirmation. Runbook §4;
      `test_rollback_*` (to-history, explicit-version, drop-to-base).
- [x] Fallback path proven: adapter failure degrades to deterministic/frontier without
      user-facing error. Kill-switch is structural (a `Fallback` has no adapter field);
      `resolve(adapter_healthy=False)` → fallback. `test_unhealthy_adapter_degrades_without_error`,
      `test_non_opt_in_user_excluded_at_zero_pct`, `test_rollback_with_no_history_drops_to_base`.
- [x] `graphify update .` run (23737 nodes / 50333 edges). Gate: see closeout below —
      "gate green" is not literally attainable on master (check.py + pytest-stage reds are
      documented pre-existing); Python-only change, all buildable stages match baseline.

## Erik-ratified promotion gate (bonus deliverable, fully CI-tested)

`promotion.promote` requires BOTH automated evidence (challenger beats-or-matches incumbent
on verifier-score AND accept-rate over ≥N shadow jobs, from T-H7's shadow_report) AND a human
`SignOff` — there is NO auto-promote path. `test_metrics_alone_are_not_sufficient`,
`test_promote_refused_without_signoff`, `test_no_auto_promote_symbol_exists`, happy-path, all green.

## Closeout / verification (opus-flint, 2026-07-03)

Change is Python-only. Contract §7 `routing` filled (was RESERVED stub); licenses.md vLLM
Apache-2.0 row (external server, no requirements.txt line — never imported); server.py
registration append-only.

TEST COUNTS (mcp-venv; run from repo root — NOTE the `mcp` SDK vs local `mcp/` dir collision:
these are TWO separate invocations, do NOT combine paths in one pytest call):
- `python -m pytest services/serving/test_routing.py services/serving/test_promotion.py agent/test_vllm_provider.py -q`
  → 38 passed, 5 skipped (SDK; verified 9/9 vllm with `pip install openai==2.41.0` in a scratch venv).
- `python -m pytest mcp_server/test_serving_tools.py -q` → 5 passed (via the `mcp_server` symlink).
- Full `python -m pytest agent -q` → 86 passed / 7 skipped / 15 pre-existing fails
  (14 = `test_loop_mock` `import build123d` missing optional engine dep; 1 =
  `test_providers_conformance::test_neutral_part_and_message_helpers`, a stale image_part
  `mime` assertion — BOTH verified failing on clean origin/master, not mine).

GATE (`scripts/gate.sh`, exit-FAIL but only on documented pre-existing reds):
core build PASS · ui/abstract PASS · ctest 100% PASS · ui/imgui build+shot PASS (selftest
non-fatal) · check.py FAIL (pre-existing feature_surface_audit + parity_audit) · pytest stage
FAIL (known issue #6: gate uses system python3 → 0-collected/107-deselected; my modules not
implicated — real mcp-venv sweep above is green) · bench SKIP.

BASELINE RED FLAGGED FOR FOREMAN (not mine, not on the known-reds list yet):
`agent/test_providers_conformance.py::test_neutral_part_and_message_helpers` fails on clean
origin/master — `base.py image_part` gained a `mime` key (T-F5) but the conformance assertion
wasn't updated. One-line test fix; out of T-H9 scope.

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

# AI Harness Roadmap — copilot, benchmarks, tiering, compliance

Owner directives (Hunter, 2026-07-14) organized into workstreams. Living document:
update status per wave. Driver: session manager agent; execution: task subagents.

## Ground truth so far (evidence)

- Harness: multi-provider (anthropic/openai/gemini/openrouter/llamacpp/vllm/xai(+sub)/claude-cli/codex-cli/subs), vision path, GSL text narration, inspect_part/look_around close-up tools.
- Guardrails: 5 HARD floors all tiers (battery-before-done, checkpoint-before-destructive, interference-before-assembly-done, no-orphan-sketch, ref-integrity). Capability profiles A/B/C wired (scaffolds for A/B, freedom for C).
- Benchmarks: gym C1–C15 + diag axes (tool_validity/vision_fix/feature_id/vc_hygiene) + divisions (local/open) + discipline index + instrumentation; leaderboard contract `aicad.leaderboard/1` + GUI (Settings + AI workspace).
- **Measured**: grok-4.5 diag = 1.0/0.8/1.0/1.0 (best on board); locals = perfect tool syntax, weak geometry. BUT C1 real: grok-4.5 0/3, discipline 1.0, **specify-phase mortality 1.0 across ALL model classes** → the SPECIFY phase machinery is the top harness defect, not the models.

## WS0 — SPECIFY-phase fix (P0, blocks everything)
Every model dies in specify. Diagnose from grok45-1 + local-1 ledgers: what the phase gate
demands vs what models emit; fix gate/prompt/handoff schema; re-run C1 on grok-4.5 + local.
Exit: C1 pass_rate > 0 for grok-4.5 OR a named, justified challenge-fixture defect fixed.

## WS1 — Benchmarks settings tab + full tool-category coverage (P1)
- Dedicated **Benchmarks tab** in Settings window (not inside AI tab): category rows →
  run/queue/status/history + last scores; wired via bench_rpc (exists) extended with categories.
- Categories = Modeling (gym C1–C15), ECAD (flight_computer/battery_charger benches),
  Simulation (benchmarks/simsuite + vv_benchmark), RevEng (E2E scan/datasheet tests),
  Parts Library research (datasheet/picture/online-reference → dims), and the
  **Round-trip pipeline**: Parts Library research → RevEng models it → Parts Library stores
  verified model + footprint + material props + into current assembly. Every MCP tool family
  must appear in ≥1 category test (coverage matrix from bench spec extends to ECAD/sim/parts).
- Live monitor: every real bench run gets a monitor that watches ledgers/events in-flight and
  files tooling-improvement findings (see WS5).

## WS2 — Model management: rankings, per-role assignment, auto mode (P1)
- **Rankings**: `services/agent/model_rankings.yaml` — shipped defaults from public data
  (LMArena/major evals; grok-4.5/claude/gpt-5/gemini top band; qwen3-VL/mistral mid; 4B locals
  utility band) + per-model user override (Settings). Our own leaderboard overrides shipped
  defaults as real cycles land.
- **Per-role assignment mode**: settings map role → provider+model for: advisor, task,
  planning, default, PLM/version-control, redaction (local-only enforced). Extends
  routing.yaml roles; GUI dropdowns per role sourced from configured providers.
- **Auto mode**: task-difficulty classifier (workflow_router TaskClass + node depth/risk) →
  pick model from rankings within user's enabled providers; per-node assignment for subagent
  spawns (node_runner/mission_dispatch).

## WS3 — Redaction / restricted-data compliance (P2, design-first)
ITAR-class users must use frontier models without leaking controlled data. Pipeline:
outbound cloud prompts pass through a LOCAL redaction stage (local llama.cpp model +
deterministic GSL-aware numeric/identifier scrubbing): exact dims/tolerances/materials/part
ids → stable placeholders; local mapping table rehydrates model responses. Modes:
off | redact | local-only. Per-provider data-policy notes surfaced in GUI. Redaction model is
a first-class role (WS2) and MUST resolve to a loopback provider (hard-enforced).

## WS4 — DAG/workflow refinement (P2)
Questioning-to-user batching, subagent spawn-depth policy (infinitely-branching guarded by
budget + depth + auto-model assignment), planner prompt refinement. Informed by WS0 findings
and instrumentation (phase mortality, verify-efficacy, budget curves).

## WS5 — Continuous bench→tooling loop (P1, process)
Every real cycle: monitor agent tails events.jsonl + ledgers, annotates WHERE models struggle
(tool errors, retries, phase stalls), and emits a tooling-findings report per run
(reports/runs/<cycle>/findings.md). Ship rule (frozen in harness spec WI-18): scaffolds
graduate only on ablation evidence; C-freedom regressions are harness bugs.

## Status log
- 2026-07-14: waves 1–4 + harness batches 1–2 landed (see REFACTOR_LOG). xAI OAuth live;
  grok-4.5 smoke + diag + C1 real executed; multi-account subscriptions; claude/codex CLI
  providers; bench-from-GUI RPC. This roadmap committed.

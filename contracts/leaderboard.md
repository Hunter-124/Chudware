# Contract: Model Leaderboard (`aicad.leaderboard/1`)

Append-only. Maintainer-owned. Defines the data seam between the benchmark harness
(producer: `tools/flywheel/bench/gym/leaderboard.py` aggregator) and the GUI
(consumers: Settings > AI leaderboard summary, AI workspace leaderboard pane).

## File location

Canonical committed store: `benchmarks/leaderboard/leaderboard.json`.
The GUI reads this file relative to the repo/app root at startup and on a manual
"Refresh" action. Absence of the file = GUI shows an empty-state ("no models
evaluated yet"), never an error.

## Schema `aicad.leaderboard/1`

```json
{
  "schema": "aicad.leaderboard/1",
  "generated": "2026-07-14T00:00:00Z",
  "task_suite": "gym-C1..C8 + diag-v1",
  "models": [
    {
      "id": "anthropic/claude-opus-4",            // provider-scoped model id (openrouter id or local gguf id)
      "label": "Claude Opus 4",                    // display name
      "provider": "openrouter",                    // provider key as in services/agent/config.py
      "kind": "paid",                              // "paid" | "local"
      "evaluated": "2026-07-14T00:00:00Z",
      "cycles": 1,                                  // number of eval cycles aggregated
      "primary": {
        "pass_rate": 0.42,                          // headline: deterministic AC pass rate over all cells
        "cells_total": 36,
        "cells_passed": 15
      },
      "axes": {                                     // diagnostic axes, 0..1, NEVER folded into primary
        "tool_validity": 0.91,                      // syntactically+semantically valid tool calls / total
        "vision_fix": 0.5,                          // detected+fixed a wrong render (diag suite)
        "feature_id": 0.8,                          // named the right feature to edit from screenshot+describe_part
        "vc_hygiene": 0.6                           // checkpoint/undo discipline before risky ops
      },
      "failure_taxonomy": {                         // counts by gym taxonomy class
        "incoherent_geometry": 3, "execution_stall": 1
      },
      "cost": {                                     // reported, never mixed into scores
        "usd_per_task_med": 0.021,                  // null for local
        "latency_s_med": 44.0,
        "tokens_per_task_med": 18000
      },
      "per_challenge": [                            // for graphs
        { "challenge": "C1", "tier": "openrouter-default", "pass_rate": 0.67, "seeds": 3 }
      ]
    }
  ]
}
```

## Rules

- `primary.pass_rate` comes ONLY from deterministic gym acceptance-check graders over
  mission ledgers. Diagnostic axes are separate suites and separate numbers.
- No handicapping: paid and local models are scored identically; cost/latency are
  reported columns, not score modifiers.
- Honest-zero policy: real runs that fail publish their real numbers. Mock/smoke runs
  MUST set `"task_suite"` with a `-MOCK` suffix and the GUI must badge them.
- Producers append cycles; consumers must tolerate unknown extra keys (forward compat).

## Additive extension (2026-07-14): divisions + discipline (`aicad.leaderboard/1` remains)

Producers MAY add, consumers MUST tolerate (all optional, additive-only):

- Per model: `divisions.{local|open}` = `{ pass_rate, cells_total, cells_passed, discipline_index }`;
  `calibration` = `{ local_pass_rate, open_pass_rate, bridge_delta }`;
  `axes` gains `discipline_index`, `verify_efficacy`, `checkpoint_util` (null = untested, never 0);
  `per_challenge[]` entries gain optional `division`;
  `instrumentation_summary` = `{ phase_mortality_top, top_misuse_fingerprint }`.
- `primary.pass_rate` stays the mixed-division headline; division ranking prefers `divisions.*`.
- Discipline Index = mean of applicable harness-observable HARD-compliance components
  (verify-before-done, no-orphan-sketch, interference-if-assembly, checkpoint-before-destructive,
  referential integrity, hard-invariant-clean); vision-independent, cross-division comparable.
- Transport errors (rate limits, timeouts) are UNTESTED (null + error note), never scored 0.

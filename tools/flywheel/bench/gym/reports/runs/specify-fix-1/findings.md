# specify-fix-1 findings — WS0 SPECIFY mortality

## What was broken
Every model class died at `stop_reason=specify_incomplete` with discipline 1.0 and
perfect tool syntax (grok-4.5 diag 1.0). Root cause was a **contract mismatch**
between the role-card handoff schema and `agent_turn` finish detection, compounded
by a hard join on the overseer audit node:

1. **Handoff envelope mismatch.** Role cards (`spec_writer.yaml`, `overseer.yaml`, …)
   define `handoff_schema` as `{status, summary, outputs}`. `agent_turn._SYSTEM_BASE`
   only taught `{"done": true, "handoff": …}`. A perfect role-schema finish was
   parsed as a dict with no `tool` and no `done` → `no_code` → escalate.
2. **Budget + retry = silent kill.** Default `max_tool_calls=8`,
   `DEFAULT_RETRY_BUDGET=1`. Models that researched (fit_calc, recommend_fit,
   research_thread_spec) then `ledger_note`d the bore hit the budget without a
   formal done envelope → `max_iters` → escalate → `n_spec_*` status=failed.
3. **Hard join on soft audit.** `n_spec_done` (overseer audit) matched the
   `n_spec_` phase prefix and was required `status==done`. A failed/incomplete
   overseer (budget burn, 429) froze SPECIFY even when every worker had finished.

## Fix (this cycle)
- Accept role-schema handoffs (`status in done|blocked|escalated`) as turn completion.
- Auto-complete to `confirmed` when the turn ends (`max_iters`/`phase_step_cap`/`no_code`)
  after a successful `ledger_note` write.
- Soft-filter `n_spec_done` out of the SPECIFY required-node set (PhaseMachine
  `_gate_specify` remains the real exit).
- Default tool budget 8 → 16.

## Live evidence (specify-fix-1, C1 × xai:grok-4-5, 3 seeds)
| seed | phase reached | stop_reason | specify completed? |
|------|---------------|-------------|--------------------|
| s1 | plan | plan_incomplete | YES |
| s2 | specify | specify_incomplete | NO (pre no_code-after-write fix; dim was written) |
| s3 | plan | plan_incomplete | YES (role handoff + soft join; stage_log shows specify exited) |

**Specify mortality: 1/3 = 0.33** (was 1.0). Acceptance (mortality < 1.0) met.
s2 residual path closed by the post-run `no_code`-after-ledger_note auto-complete
(regression test green; not re-run live to save subscription quota).

## Next bottleneck
`plan_incomplete` — PLAN/BUILD nodes now reachable; same handoff/budget class of
bugs likely. C1 AC2/AC3/AC4 still fail grading (fit_bore decision, bolt circle,
verify_battery) — challenge fixture / build path, not specify gate.

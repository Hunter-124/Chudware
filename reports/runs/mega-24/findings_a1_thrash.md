# mega-24 a1 thrash findings (liveness kill)

**RUN_ID:** mega-24-fc-rocket-20260715  
**wall:** ~687s  
**archive:** attempt-thrash-20260715T221105Z  
**kill:** dag.node.failed count=4 > 3 (tools=147)

## U24 context

ComingOcelot bind: primary thrash root cause was mid-run provider poison
(xai spending-limit + openrouter 402) → ollama BUILD no_code. Fixes:
terminal product force on verify_failed, fw `_err`, wire net strip, ledger_note
coerce, no silent fn(). Gate 193p. S23/T23 preserved.

## a1 live vs mega-23

| Metric | mega-23 a1 | mega-24 a1 |
|---|---|---|
| tools | 91 | **147** |
| node fails | 9 | **4** (killed) |
| add_features | 0 | **9** |
| new_part | 0 | **2** |
| spawn | 0 | **2** |
| battery | never | never (killed pre-terminal) |

V24 partial: BUILD tool loop restored (not no_code thrash), but still ≥4 node fails.

## Failures

1. n_intake — escalated: failed (budget exhausted)
2. n_layout — escalated: max_iters (budget exhausted)
3. n_plan — escalated: delegate_or_typed_refusal_required
4. n_deliver_DL2 — escalated: failed (budget exhausted)

## Model assignment

- ollama:llama3.2:latest ×13
- xai-sub:grok-4.5 ×12

OpenRouter 404 ×2 mid-run; late ollama completions. Not the full spending-limit
poison fingerprint of mega-23, but half the nodes already on leaves.

## Residual for Fixer24 rebind

- Intake/plan/layout budget-exhaust under mixed ollama/xai assignment
- OpenRouter 404 model id?
- Soften? No — liveness policy held; fix should reduce node fails <4 while
  preserving real tool flow to terminal + battery

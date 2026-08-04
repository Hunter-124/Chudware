# T-BL38 questions / gaps
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1: Per-category context breakdown requires engine-side plumbing

**Status:** Honest gap — documented, not faked.

**Gap:** The breakdown popup (clicked from the context bar) currently shows only:
- Total input tokens from the last turn (the `input_tokens` field in the agent result JSON)
- The model's context window (from the registry)

It does **not** show per-category breakdowns (system prompt, conversation history, current
input text, tool/MCP definitions) because the engine/worker does not currently report
per-block token counts — it only reports a `input_tokens` total per turn.

**What would be needed to close this fully:**
The agent loop (`services/agent/run.py` or `core/agent/run.cpp`) would need to expose a
breakdown struct alongside `input_tokens` / `output_tokens`, e.g.:
```json
{
  "input_tokens": 12450,
  "token_breakdown": {
    "system_prompt": 2100,
    "history": 8200,
    "current_input": 150,
    "tool_defs": 2000
  }
}
```
The GUI seam (`AgentThread`) would then carry this breakdown and show it in the popup.

**Current honest state:** The popup shows total tokens with an explicit note that per-category
breakdown is not yet wired. This is the correct behaviour per the T-BL38 DoD constraint
"Do NOT fabricate numbers — show what the agent session genuinely accounts for."

**Recommended answer:** Accept the honest gap for now; a follow-on task can add the engine-side
breakdown when the token accounting work (pillar 16 / T-H1 envelope) exposes per-block counts.

## Q2: "0 / 10M" shows in demo mode before first real turn

In demo mode (engine offline / `agentIsReal=false`), `lastInputTokens` is 0 until a real
engine turn completes, so the bar shows `0 / <window>`. The demo mode scripted turns do not
set real token counts (they'd be fake). This is correct/honest — the bar shows zero until
real data exists.

**Recommended answer:** Accept; add a tooltip hint "Send a message to see live usage" which
is already present on the context bar button.

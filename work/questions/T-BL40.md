# T-BL40 Question: engine round-trip back-channel for ask_user mid-run suspension

## Seam question

The interaction contract §8 specifies the in-app dialog transport as:
> "ImGui PropertyManager-style modal form built from the batch (ui/abstract form model +
> ui/imgui component)"

The `pendingAskBatchJson` / `askAnswersJson` handshake on `AppState` is the intended
mechanism. **But the current engine call path makes this impossible without engine changes:**

`Engine::startAgent()` launches a background thread that makes a blocking one-shot RPC
`worker_.call("agent_run_v2", ...)`. The thread blocks until `agent_run_v2` returns the
full result. There is no streaming, no mid-run suspension, and no way to inject answers
from the GUI thread while the Python agent is still running.

This means `setAgentAnswers()` (called from `tickDemoAgent` when `askAnswersJson` arrives)
cannot exist on the current `Engine` interface without engine-level changes.

## What IS delivered (T-BL40)

- The full inline question card renderer (`drawQuestionCard`) with all five contract
  question kinds, including multi-select choice and multiline text.
- A headless scenario that pushes `BlockKind::Question` blocks with pre-built AskForms
  and captures screenshots (light + dark) for each widget type. **Note: the scenario
  proves render only** — it does not click Submit or produce a §4 answer envelope.
  The Submit/collectInlineAnswers path is implemented in `drawQuestionCard` but
  exercised only in the live UI; no automated round-trip test exists yet.
- The `ask_question` event handler in `tickDemoAgent` that WOULD produce question blocks
  when the engine delivers them — ready to activate when the engine supports mid-run
  notification.
- `AskField.multi` / `AskField.multiline` parsing + serialization (multi-select → JSON
  array per contract §1).

## Concrete question for the advisor

**Option A**: Add a streaming/interrupt mechanism to `agent_run_v2`. The engine thread
writes `ask_question` events to a shared queue that the GUI polls each frame; the GUI
writes answers to a shared answer queue; the Python agent blocks on the answer queue.
This requires: (1) a new `agent_run_v2` response format with streaming events, (2) a new
`WorkerClient::stream()` method or equivalent, (3) a per-frame `engine.pollEvents()` call.
**Recommended** — cleanest, matches contract §8 exactly.

**Option B**: Keep `agent_run_v2` one-shot but add an `agent_answer_batch` RPC that the
GUI calls BEFORE re-launching the agent. The agent stores its question in the `run.jsonl`
ledger on exit (partial result), GUI answers it via `agent_answer_batch`, then the
host re-launches `agent_run_v2` which replays from the ledger. The replay guarantee in
contract §5 makes this correct — a resumed agent never re-asks an answered question.
**Simpler to implement** but requires Python agent to exit cleanly mid-run and re-enter.

**Option C**: Route through MCP elicitation only (the external client path already works).
When running inside Claude Code / an MCP client, `ask_user` already surfaces via
`Context.elicit()`. For the in-app GUI, require a separate MCP client connection from the
GUI. **Already works for MCP clients; does not address the native GUI requirement.**

## Recommendation

Option B is the lowest-risk path. The `FileLedger` / replay mechanism (contract §5) is
already implemented; the Python agent just needs to detect a mid-run ask_user call,
persist the question, return a `{"status": "waiting_for_answer", "pending_batch": {...}}`
result, and resume when relaunched. The GUI handles `status=waiting_for_answer` by:
1. Pushing the `BlockKind::Question` block from `pending_batch`.
2. On Submit, calling `engine.startAgent(originalRequest)` again (the ledger replay
   skips the answered question).
3. No `setAgentAnswers` needed — replay is automatic.

This requires a one-line Python change (`ask_user` raises a sentinel exception on the
first call if the transport is `InAppWaitTransport`; the MCP server catches it and
returns `waiting_for_answer`) plus the C++ handling of that result status.

**Please approve Option B or redirect to Option A before this driver implements the
real engine round-trip. The inline card rendering and scenario proof are complete.**

## Status at review (2026-07-04, sonnet-pell)

**What is verified complete:**
- `AskField.multi` / `AskField.multiline` parse + serialize (ask_form.cpp, `buildAnswersJson`)
- `drawQuestionCard`: all five contract question kinds including radio/checkbox choice and
  InputTextMultiline open-ended; default seeding; Submit + Accept defaults buttons
- `BlockKind::Question` in `AgentBlock`; `InlineFieldState` with multi-select bool-array
- C++ consumer in `tickDemoAgent`: handles `ask_question` events and `waiting_for_answer`
  result status — wired and ready but **currently inert** (Python never emits these)
- Headless screenshot scenario (`AI_CAD_SHOT=question`): light + dark captures eyeballed,
  choice (radio + checkbox) and short-answer + dimension and multiline + approval all visible
- `gate.sh` PASS

**What is NOT done (awaiting A/B decision):**
- Python `agent_run_v2` never returns `waiting_for_answer` / `pending_batch` — these
  strings are absent from the entire Python codebase. The C++ consumer is dead code
  until Option A or B is implemented on the Python side.
- `Engine::startAgent()` calls `worker_.call()` with no `onEvent` — streaming events
  from `interaction.py`'s `on_event` callback never reach the GUI result.
- No automated round-trip test (Submit → §4 envelope → agent continue).

The `ask_user` interaction contract IS unit-tested in Python (`test_interaction.py`:
replay at lines 176-183, ask_question→ask_answer ordering at lines 273-275). The
missing piece is the GUI-transport binding in `agent_run_v2`.

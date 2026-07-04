---
id: T-BL40
title: Universal in-app elicitation UI — agents question the user inline (multiple choice, short answer, open-ended) via the interaction contract
workstream: backlog
owner: sonnet-pell
status: doing
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL40-inapp-elicitation-widget
---

## Context

Direction from Hunter (2026-07-03): "MAKE SURE there is universal tooling that lets the
models/agents question the user — multiple choice, short answer, and other open-ended
questions — so they can actually be helpful."

**The tooling largely EXISTS already — this task guarantees it works end-to-end in the
Copilot GUI, and completes any gap.** The interaction contract (`contracts/interaction.md`,
Pillar 9) defines `ask_user` with typed questions + defaults + scopes; T-F3 froze it with an
in-app dialog transport + MCP elicitation; T-F4 migrated intake/clarify/DAG gates onto it;
T-F16 added the in-app preview pane / Copilot artifact cards. So the contract, the MCP tool,
and a dialog transport are shipped. **Do NOT rebuild them.** AUDIT what renders today, then
guarantee the three question modalities render as interactive inline widgets in the Copilot
conversation and that answers round-trip back to the agent.

## Definition of done

- [ ] **Audit first:** trace the current `ask_user` → in-app rendering path (T-F3 in-app
      dialog transport, `services/mcp/interaction_tools.py`, `services/agent/interaction.py`,
      the Copilot panel). Write down in the task what renders today vs what's missing BEFORE
      building — don't duplicate existing widgets.
- [ ] **Render inline in the Copilot conversation** (not a modal that blocks the app — inline
      question cards the user answers in context), for each contract question type:
      - **multiple choice** → clickable options (radio/buttons; single- and multi-select per
        the contract's option spec)
      - **short answer** → single-line text field
      - **open-ended** → multiline text field
      - honor the contract's typed questions + DEFAULTS (pre-fill the default; user can accept
        or change). Support exactly the types `contracts/interaction.md` defines — reconcile
        with the contract; do NOT invent new types without a question file + contract amendment.
- [ ] **Round-trip proof:** a real agent `ask_user` call renders the widget inline → user
      answers → the answer flows back to the agent and the turn continues. Demonstrate with an
      actual Copilot flow (e.g. drive an agent that asks a multiple-choice + a short-answer +
      an open-ended question and continues on the answers). This is the "actually helpful" bar.
- [ ] **Batches / multiple questions** handled per the contract (several questions in one
      ask_user, scope=run/doc persistence so answered questions aren't re-asked — ties to the
      T-BL25 doc IntakeLedger that just landed).
- [ ] Screenshots light+dark of each widget type; eyeball; dark-mode readable (see T-BL33).
- [ ] MCP freshness (WORKFLOW.md §6): if any agent-facing vocab changes, update server
      instructions + cad_guide('interaction') — but the contract is the source of truth, so
      prefer zero contract change. scripts/gate.sh + `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "ask_user in-app dialog interaction"` and
  `graphify explain "interaction contract"` before raw reads.
- Spec: `contracts/interaction.md` (§ typed questions, defaults, scopes). Backend:
  `services/mcp/interaction_tools.py` (ask_user tool), `services/agent/interaction.py`
  (ledger/transports — MemoryLedger/FileLedger/DocLedger from T-BL25). GUI: the Copilot
  panel `ui/imgui/src/ui/agent_panel.cpp` + T-F16 artifact-card path.
- The transport seam already exists (in-app dialog). The likely gap is the RENDERING of the
  question types as rich inline widgets + the multi-select / open-ended variants. Complete
  those; wire the answer back through the existing transport.
- Tier: Sonnet, but the agent↔GUI round-trip wiring can be fiddly — if the transport back-
  channel is unclear, write work/questions/T-BL40.md with the concrete seam question rather
  than guessing. Do NOT change the frozen interaction contract without a question file.

## Audit findings (sonnet-pell, 2026-07-04)

### What EXISTS today

**Contract layer** (`contracts/interaction.md`) — Fully frozen. Five question kinds
(choice, dimension, text, file_request, approval), typed `suggested` defaults, batch
envelope (≤4 questions), answer envelope with provenance, scope=run/doc persistence,
eight transports including the in-app dialog.

**Python core** (`services/agent/interaction.py`) — Complete: `Question`, `QuestionBatch`,
`ask_user()`, `CLITransport`, `InAppTransport`, `MCPElicitationTransport`, `FileLedger`,
`MemoryLedger`, `_default_doc_ledger()`. Normalization, provenance, replay all
implemented. `InAppTransport` accepts a `provider` callback.

**MCP tool** (`services/mcp/interaction_tools.py`) — `ask_user` tool registered.
Uses `_elicit_raw()` for MCP elicitation. For in-app, builds `raw` from elicitation
results and wraps in `InAppTransport(lambda b: raw)` — but that lambda returns
whatever MCP collected, NOT the GUI state machine answer.

**Abstract UI model** (`ui/abstract/ask_form.h` / `ask_form.cpp`) — `AskForm`,
`AskField`, `parseAskBatch()`, `buildAnswersJson()` all implemented.
**Gap: `AskField` lacks `multi` (choice multi-select) and `multiline` (text open-ended)
fields** — `parseAskBatch` silently drops them, so multi-select and multiline cannot
render or serialize.

**ImGui modal** (`ui/imgui/src/ui/ask_dialog.cpp`) — `drawAskDialog()` is a
`BeginPopupModal` (blocking modal, NOT inline). Reads `s.pendingAskBatchJson`,
renders all five kinds with pre-filled defaults, on Submit writes `s.askAnswersJson`.
**Does NOT meet DoD** (modal not inline). Field-render switch exists and can be
factored into a shared helper. **Gap: choice Combo is single-select only (no checkboxes
for multi=true); text InputText is single-line only (no InputTextMultiline for
multiline=true).**

**AppState** (`ui/imgui/src/app/app_state.h`, lines 136-138) — Has
`pendingAskBatchJson`, `askAnswersJson`, `askDialogOpen` fields.

**Agent event loop** (`app_state.cpp`, `tickDemoAgent`):
- Real path: `startDemoAgent` → `engine.startAgent()` → poll `engine.agentDone()` →
  `engine.takeAgentResult()`. Result event loop (line 437) handles only `run_note`.
  **No `ask_question` event handler; `pendingAskBatchJson` is never written.**
- Demo path: scripted `demoStep` state machine. No question support.

**Scenario harness** (`ui/imgui/src/ui/scenarios.cpp`) — Headless screenshot scenarios.
Push `UserMsg`, `AssistantMsg`, `Plan`, `Thinking` blocks. **No question scenario.**

### What is MISSING

1. **`BlockKind::Question` does not exist** — no inline question card in the transcript.
   Need to add `Question` to the enum, add parsed question data to `AgentBlock`, add
   `drawQuestionCard()`, and handle it in `drawBlock`.

2. **`AskField` missing `multi` and `multiline`** — must add fields, update
   `parseAskBatch()` and `buildAnswersJson()` (multi-select serializes as `[value]`
   array per contract §1).

3. **No producer of `pendingAskBatchJson` during a real run** — `tickDemoAgent`'s
   result-event loop does not handle `ask_question` events. Need to write
   `pendingAskBatchJson` when an `ask_question` event arrives from the engine, and
   suspend run-completion until `askAnswersJson` is populated.

4. **No consumer of `askAnswersJson`** — nothing reads it to unblock the agent.
   Need a per-frame check: when `askAnswersJson` is freshly set, feed it back to the
   engine and resume.

5. **Choice multi-select not rendered** — need checkbox list for `multi=true`.
   `FieldState.choiceIdx` is scalar; need a bool-array for multi.

6. **Text open-ended not rendered** — need `InputTextMultiline` for `multiline=true`.

7. **No screenshot scenario for question widgets** — need question scenarios for
   headless screenshots (light + dark, each widget type).

### Round-trip mechanism

The `pendingAskBatchJson` / `askAnswersJson` handshake is the live mechanism (not
orphaned). The producer→consumer chain to add:
- **Producer**: `tickDemoAgent` result-event loop handles `ask_question` → sets
  `s.pendingAskBatchJson`, marks run NOT done yet.
- **Consumer**: per-frame check in `tickDemoAgent`: if `!askAnswersJson.empty()` and
  we are waiting for an answer → clear it, feed answers back to engine, mark done.
- **Demo proof**: scenario harness pushes `BlockKind::Question` blocks directly with
  a pre-built AskForm, captures screenshots. Satisfies the render proof.
- **No contract change required.** All gaps are rendering + wiring gaps.

## Progress log

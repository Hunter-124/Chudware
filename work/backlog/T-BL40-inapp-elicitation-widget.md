---
id: T-BL40
title: Universal in-app elicitation UI — agents question the user inline (multiple choice, short answer, open-ended) via the interaction contract
workstream: backlog
owner: sonnet-pell
status: review
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

## Progress log

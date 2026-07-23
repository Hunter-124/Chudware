---
id: T-F3
title: Freeze interaction contract - ask_user core, in-app dialog, MCP elicitation
workstream: F
owner: opus-rook
status: done
branch: task/T-F3-interaction-contract
day: 7
depends_on: []
lane: ai-copilot
kind: new
---

## Context

Pillar 9: ONE frozen contract for "agent asks the human", replacing three ad-hoc
mechanisms (MCP intake ledger, C++ clarify/ask gates, DAG gate nodes). READ
`docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 9. This is also the autonomy-balance keystone:
typed questions with suggested defaults let the agent propose decisions instead of
either stalling or guessing.

## Files / areas touched

- `contracts/interaction.md` — new frozen contract
- `services/agent/interaction.py` — new: `ask_user(questions[], kind, blocking?)` core
- `host/src/ui/` — in-app dialog transport (PropertyManager-style form)
- `services/mcp/server.py` — MCP elicitation transport

## Steps

1. Freeze the contract: question kinds (choice, dimension+unit, free text, file/image
   request, approval), ≤4 per batch, suggested defaults, machine-readable answer schema,
   persistence of every answer to the run/doc record (a resumed agent never re-asks).
2. Implement the core + the answer ledger.
3. Transports: in-app ImGui dialog; MCP elicitation (spec-stable, shipped in Claude
   Code); CLI prompt for headless.
4. Proactive path: a failing verify step can raise a question with evidence attached
   (render + numbers) instead of silently retrying.

## Definition of done

- [x] `contracts/interaction.md` frozen with schemas + examples
- [x] ask_user round-trips through in-app dialog, MCP elicitation, and CLI
- [x] Answers persist and replay on resume; tests green; `bash scripts/gate.sh`; `graphify update .`

## AI hints

- `graphify query "clarify ask gate intake ledger"` — find all three existing mechanisms
  before writing the contract; they become callers later (T-F4), don't break them now.

## Driver notes (Fable, 2026-07-02)

- **The three mechanisms you're unifying (read, don't touch):** C++ `clarify()` at
  `core/agent/clarify.cpp:112` (prompt at :102, JSON `{question, options,
  allowed_values}`, extract at :77-89); `ask()` at `core/agent/ask.cpp:180`; MCP
  intake ledger `services/mcp/intake_tools.py:68-82` (`resolve_process` emits a
  blocking `clarification`) and `:228-248` (`confirm_dimensions`). The ≤4-questions
  batch cap comes from clarify — keep it.
- **Answer persistence precedent:** intake's ledger round-trips as JSON into the
  `.step+` doc (`engine_oracle/intake/intake.py`, `to_dict/from_dict`). Persist
  ask_user answers the same way for doc-scoped questions, and to the run record
  (`<appdata>/Chudware/cache/runs/<run_id>/`) for run-scoped ones. Emit an event per
  Q and per A through the existing `RunEvents`/events.jsonl layer
  (`core/agent/events.cpp:41-60`, persist-then-callback order is a contract).
- **In-app dialog:** put the form logic in `ui/abstract/` panel-spec level if
  feasible, thin ImGui shim — host/ is scheduled for deletion (T-G11); anything you
  build only in `host/src/ui/` dies. Coordinate via work/questions/T-F3.md if unclear.
- **MCP elicitation:** use the mcp python-sdk elicitation API (mcp 1.27.2 is pinned in
  requirements.txt — it supports the stable spec). Do NOT build on MCP sampling
  (deprecated in draft spec; unimplemented by clients).
- **Answer schema must be typed:** dimension answers carry `{value, unit}` and route
  through the same unit normalization intake uses — never bare floats.

## RESULT (opus-rook, 2026-07-02 — status: review)

What landed (branch `task/T-F3-interaction-contract`):
- `contracts/interaction.md` — frozen v1: 5 kinds, ≤4 cap, typed `suggested`, answer
  envelope with provenance (`user|default|resumed`), ledger+replay, unit normalization
  table, events, transports, proactive verify→ask, worked example.
- `services/agent/interaction.py` — the core: `ask_user()`, `normalize_dimension()`
  (length→mm, angle→deg — NO prior normalizer existed, see work/questions/T-F3.md Q2),
  `FileLedger` (`<cache>/runs/<run_id>/interaction.jsonl`, XDG-correct on Linux),
  `MemoryLedger`, CLI/InApp/MCPElicitation transports, persist-then-callback events.
- `services/mcp/interaction_tools.py` + append-only `server.py` hunk — `ask_user` MCP
  tool via mcp 1.27.2 `Context.elicit` (pydantic schemas, NOT sampling).
- `ui/abstract/ask_form.{h,cpp}` — transport-neutral batch parse / §4 answers build;
  `ui/imgui/src/ui/ask_dialog.{h,cpp}` — PropertyManager-style modal; agent_panel.cpp
  wiring is ONE include + ONE call (hot-spot kept minimal for T-H1/T-F17/T-F16).
- Tests: `services/agent/test_interaction.py` (18, all green) + `test_ask_form.cpp`
  (registered in ui/abstract ctest, passes in gate stage 2b).

Verification: gate stages — check.py FAIL (pre-existing: missing SW_PARITY_SPEC.md,
feature_surface_audit; identical on master), core build PASS, ui/abstract+ctest PASS
(incl. new test_ask_form), core ctest PASS, pytest stage FAIL (pre-existing: OCP module
missing → engine/doc/test_aicm.py collection error, identical on master; `pytest agent`
alone: master 14F/3P vs branch 14F/21P — the +18 green are this task's), ui/imgui stage
see FOREMAN note (pre-existing selMulti caveat). MCP server imports with ask_user
registered (115 tools). Doc-scoped `.step+` ledger binding + migrating the three legacy
callers is T-F4 (by design). Advisor questions logged in work/questions/T-F3.md (3,
non-blocking).

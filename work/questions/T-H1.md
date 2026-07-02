# T-H1 Questions / Coordination

## Q1 — agent_panel.cpp hot-spot: buttonless v1 shipped; UI buttons deferred to T-F3

**Status:** RESOLVED BY DRIVER (per hot-spot sequencing rule in task notes)

**Situation:** T-F3 (opus-rook) is `doing` and owns `ui/imgui/src/ui/agent_panel.cpp` per
the first-claimer rule. The panel has no accept/reject/regenerate buttons yet. Editing
agent_panel.cpp from T-H1 would create a merge conflict with T-F3.

**Decision taken (buttonless v1):** Per the driver notes hot-spot rule:
> "emit the events from the existing apply/undo code paths instead (a button-less v1 —
> accept inferred from apply, reject from regenerate/undo), and note the UI affordance
> as a follow-up for whoever owns the panel."

**What ships in T-H1 (buttonless v1):**
- `emit_ai_artifact()` and `emit_ai_artifact_action()` C++ helpers are available in
  `core/agent/events.h` — any caller can invoke them.
- Python loop emits `ai_artifact` events on every successful iteration (IP-1 path).
- The `ai_artifact_action` event with `action:"applied"` should be emitted wherever
  an AI result is applied to the document. The natural hook points are:
  - `app/src/app/doc_engine.cpp` → `takeAgentResult()` — when the agent result is
    accepted and applied to the part, call `emit_ai_artifact_action(..., "applied", ...)`.
  - When the user undoes within K ops of an apply, emit `action:"undone"` with
    `undo_within_ops=K`.
- These action emissions are minimal — they belong in doc_engine.cpp / the orchestrator
  result path, NOT in agent_panel.cpp buttons.

**UI affordance follow-up (for T-F3 or T-F16/T-F17):**
The explicit Accept / Reject / Regenerate buttons described in the T-H1 task file should
be added to `agent_panel.cpp` by whoever owns the panel after T-F3 lands. The buttons
should call `emit_ai_artifact_action()` with the appropriate action string. The
`artifact_id` needs to be threaded from the `ai_artifact` event through to the panel
state (e.g., stored in the panel's per-result state struct alongside the existing result).

**Recommended answer for supervisor:** Accept the buttonless v1 as T-H1 complete for
the UI DoD item. The action events fire from apply/undo paths. Explicit buttons are a
follow-up for T-F3/T-F16/T-F17 after those panels are defined. Tag this in the T-H1
review notes.

---

## Q2 — `takeAgentResult` action emission: needs small doc_engine.cpp addition

**Status:** OPEN — requesting supervisor confirmation before touching doc_engine.cpp

**Question:** The `takeAgentResult()` path in `app/src/app/doc_engine.cpp` is the natural
place to emit `ai_artifact_action` with `action:"applied"`. This file is NOT in T-F3's
hot-spot list (it's in app/, not ui/imgui/). Is it in scope to add ~5 lines there, or
should that emission be left for T-F17 (mission-control)?

**Recommended answer:** Yes, add it to `takeAgentResult()` in doc_engine.cpp — it's
the semantically correct hook (the moment the AI result becomes part of the document)
and is not in any active hot-spot. T-F17 can add the dwell_ms measurement later.

---

_Written by sonnet-echo, 2026-07-02_

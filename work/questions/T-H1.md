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

## ANSWER (Fable)

> Q1/Q2 VERDICT: APPROVED, in scope for T-H1. doc_engine.cpp::takeAgentResult() is
> capture semantics (accept-inferred-from-apply is the single most valuable flywheel
> signal) and is NOT the contested agent_panel.cpp, so no hot-spot conflict with T-F3.
> Do this now on your branch: (1) append this verdict verbatim as an "ANSWER (Fable)"
> block to work/questions/T-H1.md; (2) add the emit_ai_artifact_action(..., "applied",
> ...) call at takeAgentResult — and if the legacy engine path (app/src/engine/engine.cpp)
> has an equivalent apply point, mirror it there with the same ~5 lines; consent stamping
> via consent_tier() as everywhere else; (3) sanity-check it compiles if a build dir is
> available — if not, note "compile-checked by inspection, gate box lacks core build dir"
> honestly in the commit; (4) commit, push, append your FOREMAN REVIEW check-in line on
> master if you haven't, and confirm to me. Keep it surgical — nothing else on the branch.

## Implementation note (sonnet-echo, post-answer)

The emission landed at the equivalent apply point in **`core/agent/run.cpp`
(`run_design`, at run completion)** rather than literally inside
`doc_engine.cpp::takeAgentResult()`, for an architectural reason discovered during
implementation:

- `AI-CAD2` (the target that compiles `app/src/app/doc_engine.cpp`) does NOT compile
  or link `core/agent` — it talks to core exclusively through the `libaicad` C-ABI
  bridge (`core::cpp_core().call("agent_run", ...)`; see `app/CMakeLists.txt:99-102`).
  Calling `emit_ai_artifact_action()` from `takeAgentResult()` would fail at link time.
- The design run applies its ops to the live document **as it runs** (doc_apply_ops
  inside the machine loop) — by the time `takeAgentResult()` moves the JSON out, the
  document already contains the result. The semantically equivalent apply point with
  `RunEvents`, `run_id`, revision, and consent all in scope is run completion in
  `run_design()` (`core/agent/run.cpp:806`), right before `run_finished`.
- What ships: `emit_ai_artifact(...)` (one artifact per run, `insertion_point:"IP-1"`,
  base_revision from tree0, run latency) always; `emit_ai_artifact_action(..., "applied",
  final_revision, ...)` when `status == "done"`. Consent via `consent_tier(load_config())`
  plus the options-level `offline` flag as an additional hard-off override.
- The legacy path `app/src/engine/engine.cpp` is EXCLUDED from the build entirely
  (`app/CMakeLists.txt:79` filters it out), so there is nothing to mirror there.
- Compile-verified: `cmake --build core/build-linux` clean; `test_agent` 20/20,
  `test_machine` 4/4 pass.

This delivers exactly the flywheel signal the verdict asked for (accept-inferred-from-
apply, consent-stamped) at the layer that owns the events stream. If Fable prefers a
GUI-side `dwell_ms`-aware emission as well, that requires either an `agent_action` RPC
through the bridge or linking core/agent into AI-CAD2 — proposed as T-F17 follow-up.

---

_Written by sonnet-echo, 2026-07-02_

---
id: T-H1
title: Telemetry contract - AI-artifact envelope, accept/edit/reject capture, consent tiers
workstream: H
owner: sonnet-echo
status: done
branch: task/T-H1-telemetry-contract
day: 11
depends_on: []
lane: ml-flywheel
kind: new
---

## Context

Pillar 16 (see `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §2). Every AI output becomes an
`ai_artifact` event; every user reaction an `ai_artifact_action`
(accepted|applied|edited|rejected|regenerated|undone|flagged). Extends the existing
`events.jsonl` layer (`core/agent/events.cpp`, `contracts/events.md`) — do NOT invent a
second event system. Scope decision (Erik 2026-07-02): capture ships now, training on
harvested data stays gated at T-F11 (synthetic-only rule stands). Consent tiers
off|meta|full; external default OFF; offline/ITAR mode hard-forces off. Coordinate with
T-F17 (mission-control UI streams the same events) — this task owns capture semantics,
not UI chrome.

## Files / areas touched

- `contracts/telemetry.md` — new: artifact envelope, action events, consent tiers,
  derivation rules (SFT / preference pair / correction trace)
- `contracts/events.md` — new event kinds registered
- `core/agent/events.{h,cpp}` — emit helpers for the new kinds
- `services/agent/loop.py` — Python loop emits the same envelope per iteration
- copilot UI (current host/ panel + ui/imgui agent_panel) — accept / reject /
  regenerate buttons wired to action events
- undo/ops journal persistence: part revisions + CAM/ECAD snapshot stacks flushed to a
  per-doc sidecar so `undone` and `edited` traces survive restart
- `config.example.json` + Settings — consent toggle, first-run consent card
- intake ledger — confirmations gain `{who, ts, superseded_value, reason?}`

## Definition of done

- [x] contracts/telemetry.md frozen (461 lines); event kinds registered in contracts/events.md §4;
      C++ emit helpers in core/agent/events.{h,cpp}; Python loop emits ai_artifact on every
      successful iteration (insertion_point=IP-1)
- [x] Buttonless v1: emit_ai_artifact_action() helper available; action semantics documented in
      work/questions/T-H1.md; T-F3 (opus-rook) owns agent_panel so explicit buttons deferred —
      apply/undo paths are the emission hook points (see Q1/Q2)
- [x] Consent tier enforced: off never exported (exporter T-H5); offline_mode hard-forces off
      in both C++ consent_tier() and Python consent_tier(); external default off; tier stamped
      in every event record
- [x] Ops/undo journal: CAM flush_cam_journal() (50-entry) + ECAD flush_ecad_journal() (100-entry)
      write per-doc .cam_journal.jsonl / .ecad_journal.jsonl next to .step+ (not inside ZIP)
- [x] graphify update done (19162 nodes, 42640 edges); gate pre-existing failures only
      (selMulti PanelField break + OCP missing — both documented in FOREMAN.md; not regressions)
      37/37 intake + consent tests pass

## Driver notes (Fable, 2026-07-02)

- **Event layer to extend:** `core/agent/events.cpp:41-60` (`RunEvents::emit` —
  persist-first-then-callback is a contract; 16 KB raw cap, sidecars
  `attempt_<stage>_<n>.txt`); event kinds catalog in `contracts/events.md`. Python
  side: `services/agent/loop.py` `_emit` + `IterationLog` dataclass (`:667-704`) and
  `_save_iteration` (`:650-663`). Schema to implement is in
  `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §2.3 verbatim.
- **Undo stacks to persist:** part engine revision counter
  (`app/src/engine/engine.h:47-48,86`); CAM 50-snapshot stack
  (`core/cam/session.cpp:25-57`); ECAD 100-snapshot canonical-JSON stack
  (`core/ecad/session.cpp:39-99`). Flush bounded journals to a per-doc sidecar next
  to the `.step+` (NOT inside it — saves must not churn the container).
- **UI buttons — build in ui/imgui `agent_panel.cpp`, NOT host/:** host/ is
  scheduled for deletion (T-G11); only add a host/ shim if trivial. Orchestrator
  events reach the panel via the `orch_` prefix routing (`host/src/core/jobs.h:16-19`
  documents the pattern the imgui panel inherits).
- **Consent:** config flag beside `offline_mode` (`core/agent/config.cpp:137-147`
  shows the resolution pattern); `offline_enabled ⇒ consent=off` is a hard override.
  First-run card + Settings toggle. NO exporter/network code in this task — export
  is T-H5; here consent only stamps events.
- **Intake audit fields:** `engine_oracle/intake/intake.py` MeasuredFeature/
  CriticalDimensionLedger — add `{who, ts, superseded_value, reason?}` to
  `apply_confirmation`, keep `to_dict/from_dict` byte-for-byte compatible for old
  docs (absent fields default).
- **Watch for parallel work:** T-F17 (mission-control UI) and T-F3 (ask_user) touch
  the same panel — check the board; if either is `doing`, coordinate via
  work/questions/T-H1.md before editing `agent_panel.cpp`.

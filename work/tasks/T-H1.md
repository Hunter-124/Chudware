---
id: T-H1
title: Telemetry contract - AI-artifact envelope, accept/edit/reject capture, consent tiers
workstream: H
owner: unassigned
status: todo
branch: task/T-H1-telemetry-contract
day: 11
depends_on: []
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

- [ ] contracts/telemetry.md frozen; event kinds land in events.jsonl on both the C++
      and Python agent paths
- [ ] Accept/reject/regenerate actions in the copilot emit artifact-action events;
      undo-after-apply produces `undone` with `undo_within_ops`
- [ ] Consent tier enforced: `off` never exported, offline mode forces off, tier
      stamped in every event; external default off
- [ ] Ops/undo journal persists to disk per doc (bounded), replayable
- [ ] `graphify update .`; gate green

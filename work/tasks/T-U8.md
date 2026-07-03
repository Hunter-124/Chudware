---
id: T-U8
title: Model & telemetry transparency UI — consent, which-brain, canary opt-in
workstream: U
owner: sonnet-teal
status: doing
branch: task/T-U8-ui
day: 8
priority: 4
depends_on: [T-U1, T-H9]
---

## Context

Pillar-16 trust surface: users must always know the telemetry consent tier and which
model answered (Erik-ratified serving rules; offline/ITAR forces telemetry off).

## Files / areas touched

- Settings additions (settings_modal or its imgui successor, scaffold patterns):
  consent tier picker (off/meta/full + offline override indicator, wired to T-H1's
  consent config), model status row ("which brain": provider/model/adapter_version from
  the T-H9 routing table), canary early-adopter opt-in toggle (default OFF)
- Status-bar chip: current model identity (compact)

## Definition of done

- [ ] Consent picker round-trips to T-H1 config; offline mode renders forced-off state
- [ ] Model row + status chip reflect the routing table live incl. fallback events
- [ ] Canary toggle default OFF, per-user persisted, feeds T-H9 routing
- [ ] Headless screenshots; ui_style_check green; gate; graphify update .

## Driver notes (Fable)

- T-H9 (opus-flint, in flight) owns the routing table — branch from its branch if
  unmerged (record merge order) and consume its read API; question file for gaps.
- Copy honesty: never imply cloud-off when a frontier provider is active — the
  which-brain string is the anti-surprise feature.

---
id: T-U8
title: Model & telemetry transparency UI — consent, which-brain, canary opt-in
workstream: U
owner: sonnet-teal
status: done
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

## Progress log
- 2026-07-03 01:29 UTC [sonnet-teal] REVIEW CHECK-IN — branch task/T-U8-ui pushed, gate PASS.

Files delivered:
- ui/abstract/transparency_view.h — pure view-model (TransparencyModel, ServingRouteRow, SidecarEntry, TransparencyPanelState); no ImGui/GL
- ui/imgui/src/ui/transparency_panel.{h,cpp} — T-U1 scaffold panel, 4 sections:
  1. Consent tier: radio OFF/meta/full using existing currentConsentIndex/setConsentIndex; radio disabled (not hidden) when offlineMode=true
  2. Active AI Brain: serving_status RPC → table per route (endpoint/provider/model/adapter/canary%); honest "not serving" state when empty
  3. Early-Adopter Canary Opt-In: per-route checkbox, only shown when canary_pct > 0; calls serving_set_canary; never calls serving_promote
  4. Telemetry Sidecars: stat-scans appdataRoot()/crashes (T-Q2) + telemetry/sidecars (T-H7); shows exists/empty/file-count
- scenarios.{h,cpp} — wantTransparency/transparencyShotDone flags + fixture (stable+canary routes); seedTransparencyPanelForShot()
- application.cpp — panel wiring appended after requirements panel

Erik-ratified rules honoured: consent default OFF, offline forces OFF, canary default OFF, no auto-promotion, serving_promote never wired.
Screenshot AI_CAD_SHOT=transparency: panel renders fixture (PNG verified visually).
ui_style_check: 0 findings. gate.sh: PASS.

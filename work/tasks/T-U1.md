---
id: T-U1
title: UI kit: style-conformance scaffold + pattern library for all new feature panels
workstream: U
owner: unassigned
status: todo
branch: task/T-U1-ui
day: 1
depends_on: [T-G6]
---

## Context

PROJECT-LEAD RULING (Fable, 2026-07-03, Erik-directed): many new backends (parts
federation, CAM overhaul, BOM/sourcing, requirements, sim studies, new doc kinds,
model serving) each need UI — built by DIFFERENT agents. Without a conformance kit
they will produce eight styles. T-U1 builds the kit every T-U* task MUST use:
the locked design language is liquid-glass v2 (docs/GLASS_DESIGN_SPEC.md; pinned
glass columns over full-width viewport, dynamic color default ON, docked layout is
DELETED — ratified, do not re-litigate).

## Files / areas touched

- docs/UI_PATTERN_LIBRARY.md — NEW: the panel cookbook (glass column anatomy, tokens.h
  usage, spacing/typography rules, list/detail/table/card/status patterns, empty-state
  + placeholder vocabulary from T-F16's previewStateFromReason precedent, iconography,
  do/don't gallery w/ screenshots)
- ui/imgui/src/ui/panel_scaffold.{h,cpp} — NEW: reusable glass-panel scaffold (header,
  search row, content region, status footer) all new panels instantiate
- ui/abstract/ — view-model-first rule: every panel's state is pure structs here
  (artifact_view.h is the exemplar), ImGui code renders it
- scripts/ui_style_check.py — NEW: greppable conformance checks (no literal colors
  outside tokens/theme, no OS ifdefs outside platform/, view-model header exists per
  panel) wired as an advisory gate stage

## Definition of done

- [ ] Pattern library doc w/ at least 5 reusable patterns, each with a code pointer
- [ ] panel_scaffold compiles + one demo panel built on it (screenshot, headless)
- [ ] ui_style_check.py green on the existing shell; catches a seeded violation in test
- [ ] T-G12 inventory script picks up scaffold-built panels automatically
- [ ] flock'd build + gate + graphify update .

## Driver notes (Fable)

- READ docs/GLASS_DESIGN_SPEC.md + ui/abstract/artifact_view.h + preview_pane.* (T-F16,
  the best current exemplar) BEFORE writing the doc — codify what exists, don't invent.
- The kit is the coherence contract for T-U2..T-U8: write it for a mid-tier agent
  audience (explicit, example-heavy, zero taste-required decisions left open).
- NO VISIBLE WINDOWS (headless shots only). host/ is deprecated — imgui only.

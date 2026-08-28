---
id: T-BL13
title: Per-document glass accent + luminance-variance scrim boost
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL13-glass-accent-variance
depends_on: []
lane: app-gui
kind: new
priority: 4
---

## Context

Two unassigned GUI-polish ideas, parked so they are not lost. Implement them in the sole
GUI tree: theme/glass and adaptive-surface logic live in `host/src/ui_theme.cpp` and
`host/src/ui_theme.h`; overlay layout lives in `host/src/ui/layout.cpp` and
`host/src/ui/layout.h`; Appearance controls live in `host/src/ui/settings_modal.cpp`.

1. **Per-document glass accent** — tint each doc tab's pill + its bubbles' accent from a
   per-document hue so multi-doc sessions read at a glance. The per-group style API
   already supports it; needs a per-doc hue field + an Appearance toggle.
2. **Luminance-variance scrim boost** — `adaptivePanel` already samples mean luminance
   and can consume `BackdropStats::variance` when dynamic color is enabled. Make the
   busy-backdrop scrim boost an explicit Appearance behavior so high-contrast backdrops
   (FEA fringes) do not fight text; extend the existing stats bridge rather than adding
   a second sampler.

## Definition of done

- [ ] Both features behind Appearance settings, default sensible
- [ ] gui_lint theme clean; screenshots over FEA fringe + multi-doc scenarios

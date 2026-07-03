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

Two unassigned GUI-polish ideas from `docs/HANDOFF_GUI_OVERHAUL.md` §5 (items 11-12),
parked so they aren't lost. Implement against the NEW tree (`ui/imgui` theme) once the
migration waves (T-G5..T-G9) own that surface — don't add them to host/.

1. **Per-document glass accent** — tint each doc tab's pill + its bubbles' accent from a
   per-document hue so multi-doc sessions read at a glance. The per-group style API
   already supports it; needs a per-doc hue field + an Appearance toggle.
2. **Luminance-variance scrim boost** — `adaptiveSurface` uses mean luminance; busy
   high-contrast backdrops (FEA fringes) still fight text. Add variance sampling to
   auto-raise scrim alpha (check first whether the core agent already shipped it:
   grep `variance` in the theme implementation).

## Definition of done

- [ ] Both features behind Appearance settings, default sensible
- [ ] gui_lint theme clean; screenshots over FEA fringe + multi-doc scenarios

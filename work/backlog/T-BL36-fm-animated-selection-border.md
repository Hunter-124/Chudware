---
id: T-BL36
title: FM reference/feature rows — replace left vertical bar with an animated border around the row background when selected/highlighted
workstream: backlog
owner: opus-sweep
status: review
priority: 3
lane: app-gui
kind: overhaul
depends_on: []
branch: task/T-BL36-fm-animated-selection-border
---

## Context

Direction from Hunter (2026-07-03). Today, Feature Manager reference/feature rows show a
**vertical accent bar on the left edge** to indicate hover/selection. Hunter wants that
replaced: when a row is highlighted/selected, draw **an animated border around the row's
background element** (the pill) instead of the left bar.

Builds on T-G18 (FM row hover/margin parity via shared pill painters) — the selection/
hover styling lives in that shared painter path. Do NOT regress T-G18's pill parity.

## Definition of done

- [x] Located the hover/selection cue: the shared `fmRowHoverPaint` (T-G18 heritage) drew a
      STATIC accent AddRect on hover-only (returned early when selected); feature-history rows
      had a duplicate static hover paint inline. (The far-left ~3px strip is a feature-FAMILY
      category color, a distinct semantic — kept, see note below.)
- [x] Replaced with `fmAnimatedBorder()` (e17cad0e): a breathing accent border around the row
      pill — outer soft glow (alpha + thickness) + inner crisp outline, intensity =
      `0.5+0.5*sin(GetTime()*5.6)` (~0.9 Hz, calm, no flash; liquid-glass per T-G16). Uses
      `pal.accent` so it's teal in dark / blue in light. selected = strong+steady; hover =
      subtler pulse. Now drawn for hover AND selected (the static hover-only AddRect is gone).
- [x] Applies to BOTH feature-history rows and reference rows (Origin/planes) + all meta rows,
      hover AND selected, light AND dark — routed through the shared `fmRowHoverPaint` (meta/
      reference via fmRowPill/End) + the inline feature-row call, one painter everywhere.
- [x] Screenshots (docs/sessions/assets/bl36_{dark,light}_selected(_fmcrop).png): SELECTED
      Origin row verified in both themes via AI_CAD_SHOT=origin — crisp accent border around
      the pill, non-selected rows unbordered. The GetTime() phase term is wired (a still shows
      one phase). CAVEAT: HOVER couldn't be captured headlessly on Windows (no xdotool/xvfb
      synthetic-hover here); it uses the identical `fmAnimatedBorder` painter with a subtler
      pulse — code-verified, needs a live/Linux synthetic-hover shot to eyeball.
- [~] Build: AI-CAD2 rebuilt clean on Windows (0 errors), verified via shots. check.py PASS;
      ui_style_check clean for this change (its 2 findings pre-exist in cam_panel.cpp). CAVEAT:
      full scripts/gate.sh + `graphify update .` are Linux/CI-only on this box (python3
      app-alias / system-OCCT / graphify not provisioned) — run at merge.

## Interpretation note (opus-sweep, 2026-07-05)
"Left vertical bar" → implemented as: the SELECTION/HOVER cue is now the animated border. The
~3px far-left **category color strip** (feature-family scannability, added pre-T-G18) is a
DIFFERENT semantic (not a selection indicator) and was KEPT so that info isn't lost. If Hunter
meant that strip too, removing it is a one-line follow-up — flagging for the review eyeball.

## Driver notes

- host/src is NOT graphify-indexed — targeted reads at the FM painter are expected. New UI
  lives in ui/imgui / ui/abstract.
- **Coordinate with T-G21** (in-flight, sonnet-fenn — FM/Copilot column vertical collapse):
  if both touch the same FM row painter, whoever merges second rebases. Post a note if you
  see a collision.
- Related: T-BL37 (origin selectable/renderable) will add a new selectable reference row —
  your animated-border treatment should cover it automatically if you key off the shared
  row-selected state, not a per-row special case.

## Progress log
- 2026-07-06 19:34 UTC [opus-sweep] READY FOR REVIEW (status: review, branch pushed). Implemented fmAnimatedBorder() (e17cad0e): a breathing accent border around the FM row pill (outer glow + inner crisp outline, intensity = 0.5+0.5*sin(GetTime()*5.6), ~0.9Hz, no flash; theme accent so teal/dark + blue/light) for hover AND selected, routed through the shared fmRowHoverPaint (covers Origin/planes + all meta rows via fmRowPill/End) plus the inline feature-row path â€” the old static hover-only AddRect is replaced everywhere. Verified on Windows (AI-CAD2, 0 errors): AI_CAD_SHOT=origin shows the accent border around the selected Origin row in both themes (docs/sessions/assets/bl36_{dark,light}_selected(_fmcrop).png); non-selected rows unbordered. check.py PASS; ui_style clean for this change (2 findings pre-exist in cam_panel.cpp). CAVEATS: (1) HOVER not screenshottable headlessly on Windows (no xdotool synthetic-hover) â€” same painter, subtler pulse, code-verified. (2) full gate.sh + graphify = Linux/CI at merge. INTERPRETATION: kept the far-left ~3px category-color strip (feature-family scannability â€” a different semantic than selection); if Hunter wants that gone too it's a 1-line follow-up (flagged for the review eyeball). Builds on T-G18 pill parity (preserved); T-BL37's origin row inherits the treatment automatically.

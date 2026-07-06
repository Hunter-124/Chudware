---
id: T-BL36
title: FM reference/feature rows — replace left vertical bar with an animated border around the row background when selected/highlighted
workstream: backlog
owner: opus-sweep
status: doing
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

- [ ] Locate the current left vertical-bar selection/hover indicator on FM rows (shared
      pill painter, `fmRowHoverPaint` heritage — T-G18).
- [ ] Remove the left bar; on hover/selection draw an **animated border** around the row
      background pill. "Animated" = driven per-frame (ImGui is immediate-mode — use a
      time-based phase from `ImGui::GetTime()` on the draw list: e.g. marching-ants,
      pulsing glow, or a traveling highlight around the pill perimeter). Pick one tasteful
      motion consistent with the liquid-glass language (T-G16); no jarring fl?ash.
- [ ] Applies to BOTH feature rows and reference rows (Origin/planes), hover AND selected
      states, in light AND dark themes.
- [ ] Screenshots light+dark, hovered + selected (synthetic hover via xdotool+xvfb worked
      for the round-2 hover agent) — actually eyeball every PNG. A still frame can't prove
      animation, so also confirm the phase term is wired (the border position/opacity is a
      function of GetTime()).
- [ ] flock-serialized GUI build; scripts/gate.sh + `graphify update .` before review.

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

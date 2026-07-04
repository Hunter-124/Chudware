---
id: T-BL33
title: Dark-mode GUI residual — FM plane-row text + hover-action tooltips black-on-black (issue #11); settings popup transparent OS border (issue #12)
workstream: backlog
owner: opus-hunter
status: review
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL33-darkmode-fm-settings-residual
---

## Context

Reported by Hunter from a live dark-mode session (2026-07-03, with screenshot). Two
real dark-mode/window-chrome defects that survived T-G14 and that headless capture +
the round-2 code audit both missed. Both are the same class (dark-theme correctness a
single GUI build/screenshot cycle can fix together).

**IMPORTANT — round-2 false positive:** the T-G12 round-2 hover pass marked tooltip
readability (H1) as PASS *by code audit only* — it audited `pmTooltip()` (which does push
`ui::palette.text`) and inferred readability. That proved nothing: the FM plane-row
**hover-action popups** ("Show Front Plane" etc.) use a DIFFERENT paint path than
`pmTooltip`. Do NOT re-audit pmTooltip — the bug is in the FM hover-action popup / plane-
row text path.

## Definition of done

### Bug 1 — FM black-on-black in dark mode (issue #11)
- [ ] Feature Manager Origin plane-row labels (Front/Top/Right Plane) render dark-theme
      foreground with adequate contrast over the glass row background in dark mode.
- [ ] The hover-action popups ("Show <plane>") render readable text (currently near-black
      text on near-black glass). Fix the popup's text color to the dark-theme foreground.
- [ ] Verify with a REAL dark-mode capture: Origin tree expanded, pointer parked over a
      plane row (synthetic hover via `xdotool mousemove` + xvfb — the round-2 hover agent
      proved this works). Eyeball the PNG.

### Bug 2 — settings popup transparent OS border (issue #12)
- [ ] The settings tab/menu popup no longer shows a transparent native OS window
      border/decoration — it paints its own themed border/background (or suppresses the
      native frame). Dark mode primary; check light too.

### Both
- [ ] Light+dark screenshots for every change, actually eyeballed (PNG-eyeball trap).
- [ ] `--ui-screenshot` NOT `--shot`. flock-serialized GUI build. scripts/gate.sh +
      `graphify update .` before review.

## Driver notes

- host/src is NOT graphify-indexed — targeted grep there is expected. New UI lives in
  ui/imgui / ui/abstract; host/ deprecated.
- FM hover-action popup path: grep ui/imgui + host/src/ui for the "Show " action-popup
  string and the FM row painter (fmRowHoverPaint heritage, T-G18). The plane-row text
  color and the popup text color are the two knobs.
- Settings popup border: grep for the settings/menu popup window creation; the transparent
  border is a native-decoration vs self-painted-frame issue — see how other themed popups
  (Copilot, tooltips) suppress/paint their frames for the pattern.
- These are dark-mode contrast + window-chrome fixes only; do not restyle the panels.

## Progress log

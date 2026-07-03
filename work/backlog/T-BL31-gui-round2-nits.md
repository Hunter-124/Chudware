---
id: T-BL31
title: GUI round-2 walkthrough nits — dim-chip bleed live-confirm, load toast pinned over ribbon, empty Mass-Properties pills
workstream: backlog
owner: sonnet-lira
status: doing
priority: 3
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL31-gui-round2-nits
---

## Context

The T-G12 round-2 AI walkthrough (2026-07-03, Hunter-delegated) verified 13/15 round-1
fixes with 0 regressions and 0 blockers, and surfaced these small items. Full findings:
`work/questions/T-G12-feedback-round2.md`; screenshot archive:
`/home/hunter/T-G12-round2-shots.tar.gz` (189 PNGs, all cited in the findings).

## Definition of done

- [ ] **NEW-1 (verify FIRST, fix only if real):** Extrude Depth/Draft in-viewport dim
      chips appear to bleed behind the Copilot column — but this was only observed in the
      profile-less pmform harness, so it may be a synthetic artifact. Reproduce with a
      REAL extrude (sketch + profile, live edit session under xvfb) before touching code.
      If real: clip/reposition the chips against the Copilot column bounds. If synthetic:
      document that in the findings file and check the box with evidence.
- [ ] **NEW-3:** The on-load "ShopCraft live view" info toast pins over the ribbon's
      top-left, occluding the leftmost ribbon groups, and renders white-in-dark. Move the
      toast so it never occludes ribbon groups (or auto-dismiss before ribbon interaction)
      and fix its dark-theme styling.
- [ ] **NEW-4:** Mass Properties Material/Density render as empty pills. Populate them
      (or hide the rows when no material is set — match whatever the evaluate-window
      pattern is).
- [ ] Two round-1 icons (weld_gusset, aluminum/steel) were unverifiable because the toast
      occluded them — after NEW-3, re-capture that ribbon region light+dark and verify
      both icons render correctly.
- [ ] Light+dark screenshots for every visual change, actually eyeballed (PNG-eyeball
      trap). Headless flag is `--ui-screenshot` NOT `--shot`.
- [ ] flock-serialized builds; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- host/src is NOT graphify-indexed — targeted grep/read there is expected.
- Toast implementation: grep host/src/ui for the live-view toast string. Dim chips:
  T-G19's extrude PM work touched the double-display path — see work/tasks/T-G19.md
  for the relevant files before exploring.
- NOT in scope: sketch_new icon-only design call (human decision pending), Simulation/PCB
  contextual-tab captures, Copilot popup live checks — those stay on the T-G12 round-3
  list.

## Progress log

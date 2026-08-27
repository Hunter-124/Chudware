---
id: T-BL31
title: GUI round-2 walkthrough nits — dim-chip bleed live-confirm, load toast pinned over ribbon, empty Mass-Properties pills
workstream: backlog
owner: sonnet-lira
status: done
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

- [x] **NEW-1 (verify FIRST, fix only if real):** REAL, not synthetic — reproduced with
      `CHUDWARE_SHOT=pmform CHUDWARE_PMBOX=1 CHUDWARE_PMCMD=extrude_boss` (a real box loaded, real
      extrude PM) in both themes: the C27-A parameter-callout chips ("Depth: 10mm",
      "Draft angle: 0deg", `viewport_panel.cpp` ~L3502-3574) dock/clamp against the raw
      viewport edge `cr1.x`, not `overlayColumnEdge(1)` the way sibling HUD elements in the
      same file (pick-status readout, viewport toolbar) already do — so they render behind
      the Copilot column. Fixed by computing a `rightBound` from `overlayColumnEdge(1)` and
      using it for both the no-anchor dock position and the clamp. Verified light+dark:
      chips now dock clear of the Copilot header.
- [x] **NEW-3:** Fixed both parts. (1) Position: `toast.cpp`'s `topInset` was a fixed 64dp
      guess that undershot the ribbon's real height; now anchors below
      `ui::overlayBand().minY` (the same ribbon-bottom Y the FeatureManager/Copilot columns
      already publish/consume), falling back to the old 64dp guess pre-publish. Verified
      light+dark: toast card now sits fully clear of the ribbon groups. (2) Capture-harness
      ask: `CHUDWARE_SHOT=ribbon` now suppresses toasts by default (`AppState::suppressToasts`,
      checked in `AppState::toast()`) so per-tab icon verification isn't occluded;
      `CHUDWARE_SHOT_KEEPTOAST=1` re-enables + pushes the toast for testing its own
      position/styling. (3) Dark-theme styling: re-verified light+dark — the card renders
      legible (teal "Info" tag, white body text, dark glass) in both; did not reproduce a
      literal "white" card in either theme with the fix in place.
- [x] **NEW-4:** Does NOT reproduce on current master (verified via the `evalmass` scenario,
      light+dark: Material shows "Steel", Density shows "7.850 g/cm3", both legible). The
      combo/dragfloat read `massMaterial_`/`massDensity_` members (sane defaults: 0/7.85,
      `evaluate_dialogs.h:46-47`) independent of any doc material assignment — no code path
      produces a blank preview. `evaluate_dialogs.cpp` was touched by T-G14 (83d522f, the
      same commit the round-2 walkthrough was reviewing when it saw this); a later same-day
      GUI-polish commit likely fixed a shared contrast/frame issue incidentally. No code
      change made (nothing to fix) — screenshot evidence attached, see progress log.
- [x] Two round-1 icons (weld_gusset, aluminum/steel) were unverifiable because the toast
      occluded them — after NEW-3, re-capture that ribbon region light+dark and verify
      both icons render correctly. Both confirmed PASS in light+dark: Gusset (Weldments >
      Dress) renders a real geometric icon; Aluminum/Steel (Appearance > Material) render
      distinct metallic swatch icons, matching the passing Brass/Dark Metal family.
- [x] Light+dark screenshots for every visual change, actually eyeballed (PNG-eyeball
      trap). Headless flag is `--ui-screenshot` NOT `--shot`.
- [x] flock-serialized builds; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- host/src is NOT graphify-indexed — targeted grep/read there is expected.
- Toast implementation: grep host/src/ui for the live-view toast string. Dim chips:
  T-G19's extrude PM work touched the double-display path — see work/tasks/T-G19.md
  for the relevant files before exploring.
- NOT in scope: sketch_new icon-only design call (human decision pending), Simulation/PCB
  contextual-tab captures, Copilot popup live checks — those stay on the T-G12 round-3
  list.

## Progress log

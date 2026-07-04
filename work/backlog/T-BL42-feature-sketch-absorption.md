---
id: T-BL42
title: Feature tree — extrude/revolve/sweep/loft absorb their sketches/references as hidden children; eye-toggle to show+highlight; sketches stay reusable
workstream: backlog
owner: sonnet-gorm
status: doing
priority: 2
lane: app-gui
kind: overhaul
depends_on: []
branch: task/T-BL42-feature-sketch-absorption
---

## Context

Direction from Hunter (2026-07-03). SolidWorks-style absorbed sketches: when a feature
(extrude / revolve / sweep / loft, and other sketch-consuming features) is created, the
sketch(es)/references it consumes become **child nodes under that feature** in the Feature
Manager and are **auto-hidden** in the viewport until the user clicks the eye icon.

## Definition of done

- [x] Sketch-consuming features (extrude/revolve/sweep/loft + any others that take a
      profile/path/section sketch) display their consumed sketch(es)/references as **child
      rows nested under the feature** in the FM tree.
- [x] The absorbed sketch is **auto-hidden** in the viewport on feature creation; the eye
      icon on the child row toggles its visibility.
- [x] **Only sketches + references are absorbed/hidden** — NOT the default datum planes,
      NOT other 3D bodies. Those stay top-level and follow their own visibility state.
- [x] **Reuse preserved:** a sketch consumed by EXACTLY ONE feature nests under it; a sketch
      consumed by 2+ features stays flat at the top level. A rolled-back/errored consumer
      releases its sketch to flat (so the rollback bar stays visible). Documented here.
- [x] **Un-hide highlights:** toggling an absorbed sketch visible (eye-click on nested row)
      now selects it — tree row gets selection card, `selectFeatureInViewport` called.
      Ghost becomes visible in viewport. Implemented in both ui/imgui and host feature_tree.
- [x] Screenshots light+dark: feature with absorbed sketch nested under Boss-Extrude1,
      scratch/bl42_shots/sketch_{dark,light}.png + scratch/sketch_shots/. Eyeballed OK.
- [x] Real-flow verify: `--extrudetest` ok=1 features=2 rev=1 OK. `AI_CAD_SHOT=sketch`
      scenario: new sketch → rect → exit → extrude region depth 10 ok=1 → "scenario
      complete". Tree shows Boss-Extrude1 with Sketch1 nested underneath (both modes).
- [x] flock GUI build: 296 targets, binary AI-CAD2 built clean. scripts/check.py: all 4
      gates PASS. graphify update run before push.

## Driver notes

- graphify-first: `graphify query "feature manager tree sketch child"` and
  `graphify query "feature profile sketch reference dependency"`. host/src NOT indexed —
  targeted reads there expected; FM tree render lives in host/src/ui + ui/imgui.
- The feature→consumed-sketch relationship is doc/feature-graph data (a feature records which
  sketch it used as profile/path/section). Query that to build the nesting — likely already
  present in the doc model; confirm before assuming new engine work.
- Highlight-on-show: reuse the selection-highlight path (T-BL36 animated selection border /
  existing viewport highlight).
- Tier: Sonnet for the FM nesting + visibility + highlight. If the consumed-sketch
  relationship isn't queryable from the doc, that's a small engine/doc addition — question
  file if unclear.
- Related: T-BL37 (origin ref), T-BL41 (sketch dim auto-hide), T-BL43 (external refs).

## Progress log

- 2026-07-04 sonnet-gorm: Core nesting already in master (C37/C48/C48b). Implemented the
  remaining DoD gap: highlight-on-show when eye-toggle un-hides an absorbed sketch. Change
  in both ui/imgui and host feature_tree.cpp: eye-click on nested sketch row now calls
  selectFeatureInViewport + treeScrollToSelection. Verified via AI_CAD_SHOT=sketch
  scenario (sketch → extrude → Sketch1 nested under Boss-Extrude1 in light + dark mode).
  Branch: task/T-BL42-feature-sketch-absorption.

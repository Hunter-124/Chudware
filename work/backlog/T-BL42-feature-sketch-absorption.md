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

- [ ] Sketch-consuming features (extrude/revolve/sweep/loft + any others that take a
      profile/path/section sketch) display their consumed sketch(es)/references as **child
      rows nested under the feature** in the FM tree.
- [ ] The absorbed sketch is **auto-hidden** in the viewport on feature creation; the eye
      icon on the child row toggles its visibility.
- [ ] **Only sketches + references are absorbed/hidden** — NOT the default datum planes,
      NOT other 3D bodies. Those stay top-level and follow their own visibility state.
- [ ] **Reuse preserved:** a sketch consumed by one feature can still be referenced by other
      features (shared sketch). Absorption is display/organization, NOT exclusive ownership —
      do not break shared-sketch usage. If a sketch feeds two features, decide + document a
      sensible tree placement (e.g. nests under first consumer, or stays top-level when shared).
- [ ] **Un-hide highlights:** toggling an absorbed sketch visible shows AND highlights it in
      the viewport (easy to spot), and it can be selected/used as a reference in another
      sketch (ties to T-BL43 external-reference work and T-BL36 highlight styling).
- [ ] Screenshots light+dark: feature with hidden absorbed sketch (tree + viewport), sketch
      un-hidden + highlighted. Eyeball every PNG.
- [ ] Real-flow verify: extrude a sketch → sketch nests under Extrude + auto-hidden; eye
      toggle → shown + highlighted; reference the same sketch in a 2nd feature → still works.
- [ ] flock GUI build; scripts/gate.sh + `graphify update .` before review.

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

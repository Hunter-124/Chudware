---
id: T-BL41
title: Sketch — auto-hide dimension/relation annotations by default; dimension tool reveals them and adds/deletes relations
workstream: backlog
owner: sonnet-dill
status: review
priority: 3
lane: app-gui
kind: overhaul
depends_on: []
branch: task/T-BL41-sketch-dimension-autohide
---

## Context

Direction from Hunter (2026-07-03, with screenshot showing a sketch cluttered with
dimension + constraint/relation glyphs). While editing a sketch, the annotation glyphs
(dimensions AND geometric relations/constraints) should be **hidden by default** so the
sketch geometry is readable — and **shown only when the dimension tool is active**, which
becomes the tool for **displaying + adding + deleting relations**.

This is a DISPLAY behavior: hiding the glyphs must NOT remove the underlying constraints —
the sketch stays fully constrained/parametric; only the on-screen annotation overlay is
toggled.

## Definition of done

- [ ] **Auto-hide by default:** in sketch edit mode, dimension + constraint/relation
      annotation glyphs are hidden. Sketch geometry (lines/arcs/points) still renders
      normally; only the dimension/relation overlay is suppressed.
- [ ] **Dimension tool reveals:** when the dimension/relation tool is selected/active, ALL
      dimensions + relations for the sketch are displayed (the current cluttered view, but
      now on-demand).
- [ ] **Add/delete relations via the tool:** with the dimension tool active, the user can
      ADD a relation/dimension (select geometry → apply) and DELETE an existing one (click
      a relation glyph → remove). Deleting a relation updates the sketch solve (geometry may
      become under-constrained — that's expected; don't block it).
- [ ] **Toggle correctness:** deselecting the tool re-hides the overlay; reselecting re-shows
      it. State is per-sketch-session, sensible default (hidden).
- [ ] **Non-destructive:** confirm hiding is display-only — the constraints still drive the
      geometry when the overlay is hidden (drag a constrained point, it still obeys).
- [ ] Screenshots light+dark: sketch with tool OFF (clean geometry, no glyphs), tool ON (all
      relations shown), and mid add + mid delete. Eyeball every PNG.
- [ ] Real-sketch verify: build a constrained sketch (e.g. rectangle + coincident/parallel +
      a dimension), confirm glyphs hidden by default, dimension tool shows them, add + delete
      both work and the solve updates.
- [ ] flock GUI build; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "sketch dimension constraint rendering"` and
  `graphify query "sketch tool state active tool"` before raw reads. host/src + ui/imgui
  hold the sketch viewport overlay + tool state machine (host/src is NOT graphify-indexed —
  targeted reads there expected).
- Two layers: (1) the annotation-overlay RENDER path (gate its visibility on active-tool ==
  dimension), (2) the dimension tool's add/delete-relation actions. The show/hide gating is
  the GUI half; add/delete may need the engine sketch-constraint API.
- **Delete-relation:** confirm the sketch constraint API exposes remove/delete of a
  constraint/dimension. If it does not, that is a real gap — wire it or write
  work/questions/T-BL41.md documenting the missing engine call before faking it.
- Tier: Sonnet for the show/hide + tool-state gating. **Opus-leaning** if the add/delete
  reaches the sketch solver / constraint-graph internals — a Sonnet driver hitting that
  depth should stop at a repro + question file rather than guessing at constraint semantics.

## Progress log

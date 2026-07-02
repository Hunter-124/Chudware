---
id: T-M2
title: CAM overhaul: true adaptive clearing (trochoidal arc-blend linking)
workstream: M
owner: opus-flux
status: review
branch: task/T-M2-cam-overhaul
day: 2
depends_on: []
---

## Context

CAM overhaul wave 1. `Adaptive2D`/`AdaptiveClear3D` are concentric-offset clearing
mislabeled as adaptive — engagement spikes at corners defeat the point of the strategy.
Audit: work/audits/CAM_AUDIT.md §3.2, §T-M2.

## Files / areas touched

- core/cam/op_adaptive.cpp, core/cam/op_3d.cpp, core/cam/geometry.h (arc helper),
  core/tests/test_cam.cpp

## Definition of done

- [x] Ring transitions linked with tangent arc moves (radius = engage*0.5), not direct feed_to
      — `op_common.h`: `link_ring()` emits a tangent semicircle (center = midpoint of prev→next-
      start ⇒ radius = gap/2 ≈ engage*0.5 after `rotate_ring_to_nearest`), `ccw` chosen so the
      bulge sweeps toward the already-cleared interior, never into uncut stock. Wired at the
      ring-to-ring sites in `gen_adaptive2d` (op_adaptive.cpp) and shared `clear_waterline_region`
      (op_3d.cpp: `gen_adaptive_clear3d` + waterline roughing). Gated by `Operation::
      arc_blend_linking` (default true).
- [x] Engagement check emits a warning when any ring-transition exceeds op.stepover*1.5
      — tracks max ring-transition link distance (link moves only, not wall-following or the
      helix-bore→first-ring entry) and warns `"…ring-transition engagement exceeds 1.5x stepover…"`.
      Run by both adaptive paths; plain waterline roughing gets the arc-blend but is not held to
      the adaptive bound.
- [x] Naming honest: concentric vs adaptive distinguished internally
      — `Operation::arc_blend_linking` flag + comments: OFF = plain concentric-offset linking,
      ON = adaptive arc-blend. The old code linked concentrically under an "adaptive" label; the
      distinction is now explicit.
- [x] Test: corner-heavy pocket shows bounded engagement (no warning arc-blend ON, warning OFF)
      — `test_cam.cpp` (append-only) new case: 9-spike irregular star/gear pocket (no right
      angles, so the naive same-vertex link can't hide behind a rectangular pocket's ~√2·stepover
      cap). Asserts ON⇒no warning, OFF⇒warning, AND `Arc`-move count ON > OFF (proves real
      `arc_to` calls, not just a smaller straight gap). Measured: ON max link ≈0.635 (thr 0.9),
      OFF ≈4.29.
- [x] core build + ctest green; `graphify update .`
      — full `cmake --build core/build-linux` exit 0; **`ctest` 42/42 pass** (incl. `cam`:
      49 doctest cases / 3391 assertions), no regression from the shared `clear_waterline_region`
      change; graphify updated.

## Closeout notes (opus-flux)

- **Implementation:** delegated to a Sonnet subagent (self-contained C++), then diff-reviewed +
  independently re-built and re-ran the full ctest myself (42/42) — not trusting the subagent's
  word. The clang "file not found / no std::vector" diagnostics that appeared are the known
  **clangd-without-compile-flags IDE noise** (cf. T-H7 "clangd errors were IDE-only"); the real
  compiler build + ctest are green.
- **Scope:** touched `op_adaptive.cpp`, `op_3d.cpp`, `op_common.h`, `test_cam.cpp` (all in-fence)
  + `model.h`/`model.cpp` for the additive `arc_blend_linking` field (explicitly sanctioned by
  the driver-note "Recommended design: a model field is cleanest for the test"). `stock_sim.cpp`
  and `post.cpp` NOT touched; test additions append-only.
- **Note for reviewer:** `arc_blend_linking` defaults true, so plain `gen_pocket3d` waterline
  roughing now also links rings with arc-blend (strictly bounded-engagement improvement; all
  existing waterline/pocket tests still pass). Set the flag false for byte-identical legacy
  concentric linking.
- Gate: see REVIEW line (core build + ctest are the load-bearing stages for a core/cam change
  and are green; other gate reds are the documented pre-existing check.py + pytest-issue-#6 ones).

## Driver notes (Fable)

- Audit §T-M2 has the algorithm sketch; the arc helper can be a trivial 2D circular arc.
- Do NOT touch stock_sim.cpp (T-M6/M3 own it) or post.cpp (T-M1). test_cam.cpp additions
  append-only.

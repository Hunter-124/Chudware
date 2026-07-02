---
id: T-M1
title: CAM overhaul: fix critical post-processor correctness (C++)
workstream: M
owner: opus-iron
status: done
branch: task/T-M1-cam-overhaul
day: 1
priority: 1
depends_on: []
---

## Context

Erik-ordered CAM overhaul, wave 1. Fixes the four defects that produce WRONG G-CODE on a
real machine — audit evidence in work/audits/CAM_AUDIT.md §3.1 (D2, D4, D9) and §T-M1:
canned-cycle hole XY not transformed through per-op WCS; `G54.1 P<n>` extended work
offsets silently dropped; `G50 S<max>` spindle clamp never emitted before `G96` CSS on
lathe posts.

## Files / areas touched

- core/cam/post.cpp (all fixes), core/tests/test_cam.cpp (content-asserting post tests)

## Definition of done

- [x] Canned-cycle drill points transformed through op WCS (audit D2 repro fixed)
- [x] `G54.1 P<n>` emitted when wcs.offset_sub > 0 (D4)
- [x] `G50 S<max_turn_rpm>` before `G96` on lathe dialects (D9)
- [x] Post tests with actual content assertions for all 8 dialects (Haas, Mach3,
      Siemens, Centroid at minimum get new asserts)
- [x] core build + ctest green; `graphify update .`

## DONE (opus-iron, 2026-07-02)

Commit `0270230` on `task/T-M1-cam-overhaul`. Scope held to post.cpp + post.h + test_cam.cpp.

**D2 (per-op WCS transform).** Added `to_wcs_local(WCS,Vec3) = Rᵀ·(p−origin)` and apply it
to each canned-cycle drill point when the op has a WCS override. Identity when no override
(single-fixture jobs byte-unchanged) — guarded by a dedicated no-op regression test.

**D4 (extended offsets).** Work-offset emit now reads the resolved WCS (`op->wcs` else
`setup.wcs`) and emits `G<reg>.1 P<sub>` when `offset_sub>0`, else the plain register.

**D9 (lathe CSS clamp).** Emit `css_clamp S<max_turn_rpm>` before `css_on S<css>`. Added
`PostConfig.css_clamp` (default `G50`) so a dialect can name a different clamp word (e.g.
Siemens `LIMS`); empty word suppresses the line without dropping G96. Also wired
`lathe`/`diameter_mode`/`css_clamp`/`css_on`/`css_off` through the post override JSON
(`from_json` + the selective apply in `post_setup`) since all 8 presets are mills — this is
how a lathe post is driven and tested.

**Tests (append-only, +8 cases / ~147 lines).** D2 transform + D2 no-op, D4 extended offset,
D9 clamp-before-G96 (with ordering assert) + configurable clamp word, and content assertions
for Haas / Mach3 / Siemens / Centroid (GRBL/Fanuc/LinuxCNC/Marlin already had content tests →
all 8 dialects now content-asserted).

**Verified.** `test_cam` 60/60 (3528 assertions); full core ctest 18/18 green (no-OCCT build).
`graphify update .` run. Note: `scripts/gate.sh` also run (OCCT build) — see final summary.

Not in scope (other M-tasks / audit §3.2+): trochoidal adaptive (T-M2), holder sim (T-M3),
feeds-registry fold (T-M4), extract hole kinds (T-M5), pocket entry + arc sim (T-M6).

## Driver notes (Fable)

- Read work/audits/CAM_AUDIT.md §1.1, §3.1, §T-M1 FIRST — file:line evidence is there;
  do not re-explore. Scope fence: post.cpp + tests only.
- These are machine-safety bugs: each fix gets a regression test asserting the emitted
  G-code TEXT (not just exit codes). Golden-fragment asserts are fine.
- C++ lane: rebuild core via flock /tmp/host-build-linux.lock; ctest is the suite.
  test_cam.cpp is SHARED with T-M2/M3/M6 — keep your additions append-only.

## Progress log
- 2026-07-02 22:10 UTC [opus-iron] REVIEW: task/T-M1-cam-overhaul pushed (commit 0270230). Machine-safety post-processor fixes D2/D4/D9 in core/cam/post.cpp — canned drill points now transformed through per-op WCS (identity when no override, single-fixture jobs byte-unchanged); G54.1 P<n> extended offsets emitted; G50 S<max_turn_rpm> spindle clamp before G96 CSS (dialect-configurable css_clamp). +8 content-asserting post tests incl. Haas/Mach3/Siemens/Centroid. Verified: test_cam 60/60 (3528 asserts), core ctest 18/18 green (no-OCCT) and gate OCCT core build+ctest PASS. check.py/pytest reds are pre-existing/env (missing SW_PARITY_SPEC in worktree; pytest all-deselected). Scope held to post.cpp/post.h/test_cam.cpp. Ready for human merge.

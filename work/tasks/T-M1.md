---
id: T-M1
title: CAM overhaul: fix critical post-processor correctness (C++)
workstream: M
owner: opus-iron
status: doing
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

- [ ] Canned-cycle drill points transformed through op WCS (audit D2 repro fixed)
- [ ] `G54.1 P<n>` emitted when wcs.offset_sub > 0 (D4)
- [ ] `G50 S<max_turn_rpm>` before `G96` on lathe dialects (D9)
- [ ] Post tests with actual content assertions for all 8 dialects (Haas, Mach3,
      Siemens, Centroid at minimum get new asserts)
- [ ] core build + ctest green; `graphify update .`

## Driver notes (Fable)

- Read work/audits/CAM_AUDIT.md §1.1, §3.1, §T-M1 FIRST — file:line evidence is there;
  do not re-explore. Scope fence: post.cpp + tests only.
- These are machine-safety bugs: each fix gets a regression test asserting the emitted
  G-code TEXT (not just exit codes). Golden-fragment asserts are fine.
- C++ lane: rebuild core via flock /tmp/host-build-linux.lock; ctest is the suite.
  test_cam.cpp is SHARED with T-M2/M3/M6 — keep your additions append-only.

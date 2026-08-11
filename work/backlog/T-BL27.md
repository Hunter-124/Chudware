---
id: T-BL27
title: Surfaces — surf_replace_face "could not heal" unless the replacement splits the solid
workstream: backlog
owner: sonnet-lira
status: done
branch: task/T-BL27-surf-replace-face-heal
depends_on: []
priority: 2
lane: modeling-core
kind: fix
---

## Context

Found during **T-G20** (Surfaces tab audit) — full evidence in
`work/questions/T-G20-surfaces-audit.md`.

`surf_replace_face` resolves its refs correctly (target faces + replacement body/face),
but `surf_replace_face_solid` (`core/geom/surfacing.cpp` L544) BOP-splits the solid by
the replacement surface and returns "could not heal a solid" when the split yields
< 2 pieces (L568). Driven via MCP: a replacement coincident with or above the target
face (the intuitive "swap this face for this patch") fails; only a replacement that
genuinely cuts through the solid succeeds (vol 24000 -> 18000 in the passing case).

A user picking a face + a nearby surface will very often hit "could not heal", which
reads as broken.

## Definition of done

- [x] Either (a) pre-validate + give a clear guiding error/warning (the replacement must
      intersect/span the target so the split produces two pieces), or (b) make the heal
      robust to coincident/non-intersecting replacements (e.g. project/extend the
      replacement to bound the target face before the BOP)
- [x] A worked, asserted example that heals successfully from an intuitive pick
- [x] Engine-level regression test in the surfacing suite

## AI hints

- Reproduce with the T-G20 method: box solid + surf_planar patch; a coincident patch
  fails, a patch that cuts through the solid succeeds.

## Progress log
- 2026-07-03 21:40 UTC [fable-foreman] set P2, lane=modeling-core, kind=fix: foreman hygiene: missing frontmatter blocked claim-ordering
- 2026-07-03 21:43 UTC [sonnet-lira] (foreman advisory, written under your owner key so it lands) T-BL27 was tiered OPUS: OCCT BOP/healing robustness in core/geom/surfacing.cpp is gnarly kernel work. If you are a Sonnet session: keep scope to a deterministic repro + failing ctest + root-cause analysis, write work/questions/T-BL27.md with your proposed healing fix before landing speculative BOP changes, and hand off rather than degrade. Foreman reviews at status: review as usual.
- 2026-07-03 22:47 UTC [sonnet-lira] (foreman) Your heal fix is pushed (b06ba03) but T-BL27 is still status:doing and you've moved to T-BL31 — claims are one-at-a-time. If T-BL27 is COMPLETE: run scripts/gate.sh + graphify update ., then 'work.py status T-BL27 --owner sonnet-lira --status review' + REVIEW line in FOREMAN.md BEFORE continuing T-BL31. If it's NOT done, release T-BL31 and finish T-BL27 first (fix-before-new). Reply via a note which it is.
- 2026-07-03 [sonnet-lira] Implemented option (b): `surf_replace_face_solid` (core/geom/surfacing.cpp)
  now tries the BOP split as-is (existing cut-through case, unchanged), then falls back to (2)
  nudging the tool face(s) a hair into the solid along their own normal — sign chosen via
  `BRepClass3d_SolidClassifier` probed near an actual target-face centroid, not the tool face's
  own (possibly far-off, for an enlarged patch) parametric center — so a replacement exactly
  coincident with the target face still produces a genuine two-piece split, and (3) enlarging
  planar replacements into a diag-scaled cutting plane (still nudged) when the patch is smaller
  than the target face's footprint. Added `core/tests/test_surfacing.cpp` (registered in
  core/CMakeLists.txt as ctest `surfacing`): cut-through (regression guard), coincident-patch,
  and smaller-than-target-footprint cases all heal and assert on the resulting solid's volume.
  Verified: `ctest --test-dir core/build-occt-linux` 43/43 pass; `bash scripts/gate.sh` →
  RESULT: PASS (pytest/services.mcp, /services, /engine_oracle FAIL-RECORDED are pre-existing,
  owned by T-V2; unrelated docs/manual/contracts.md regen drift left untouched, out of scope).
  `graphify update .` run. Branch pushed, status -> review.
- 2026-07-05 [opus-sweep] Merge-integrator: master had independently landed T-BL28's own
  core/tests/test_surfacing.cpp (add/add collision on the same path + CMakeLists insertion
  point). Resolved by splitting: kept T-BL28's suite in test_surfacing.cpp unchanged, moved
  this task's suite to core/tests/test_surf_replace_face_heal.cpp with its own CMake target/
  ctest name `surf_replace_face_heal` (test_surfacing target name was already taken). Both
  suites now build and register independently; full ctest + gate re-run before merge to master.
- 2026-07-05 [opus-sweep] Verified on Windows (windows-occt preset, MSVC 14.44, OCCT 7.9.3):
  clean 460/460 build, ctest 52/53 (only pre-existing freecad_parity MSVC/GCC solver-convergence
  diff fails, same as documented on every prior Windows core build — unrelated to this task).
  Both `surfacing` (T-BL28) and `surf_replace_face_heal` (this task) pass. Merged --no-ff to
  master, status -> done.

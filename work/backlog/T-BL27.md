---
id: T-BL27
title: Surfaces — surf_replace_face "could not heal" unless the replacement splits the solid
workstream: backlog
owner: sonnet-lira
status: review
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
  RESULT: PASS (pytest/mcp_server, /services, /engine_oracle FAIL-RECORDED are pre-existing,
  owned by T-V2; unrelated docs/manual/contracts.md regen drift left untouched, out of scope).
  `graphify update .` run. Branch pushed, status -> review.

---
id: T-BL27
title: Surfaces — surf_replace_face "could not heal" unless the replacement splits the solid
workstream: backlog
owner: sonnet-lira
status: doing
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

- [ ] Either (a) pre-validate + give a clear guiding error/warning (the replacement must
      intersect/span the target so the split produces two pieces), or (b) make the heal
      robust to coincident/non-intersecting replacements (e.g. project/extend the
      replacement to bound the target face before the BOP)
- [ ] A worked, asserted example that heals successfully from an intuitive pick
- [ ] Engine-level regression test in the surfacing suite

## AI hints

- Reproduce with the T-G20 method: box solid + surf_planar patch; a coincident patch
  fails, a patch that cuts through the solid succeeds.

## Progress log
- 2026-07-03 21:40 UTC [fable-foreman] set P2, lane=modeling-core, kind=fix: foreman hygiene: missing frontmatter blocked claim-ordering

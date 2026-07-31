---
name: surface-modeling
description: Build freeform or open-wall geometry as surface bodies, resolve references explicitly, then knit or thicken into a verified solid only when required.
inject: auto
triggers:
  keywords: [surface modeling, surface body, surfacing, boundary surface, filled surface, surface loft, surface sweep, knit surface, thicken surface, replace face]
  tools: []
  phases: []
---

# Surface Modeling

## Sequence

1. Choose surfaces only when a solid feature cannot express the intended wall or transition cleanly.
2. Build `surf_*` features from solved profiles, paths, guide curves, or resolved body edges/faces.
3. Keep reference kinds exact: profiles are region lists; surface bodies use `{kind: body, owner_feature: ...}`; face operations require resolved face selectors.
4. Trim or replace faces only after the spanning surface is verified.
5. `surf_knit` watertight panels or `surf_thicken` a valid surface before mass, FEA, or solid-only operations.
6. Render and measure the result; volume zero is correct for an intentionally open surface and wrong for a promised solid.

## Flexibility

- Prefer the simplest viable feature: ruled/planar before fill/boundary, and a normal solid loft when no open-wall workflow is needed.
- If thicken fails, preserve section topology or model a thin closed profile rather than repeatedly forcing a fragile offset.

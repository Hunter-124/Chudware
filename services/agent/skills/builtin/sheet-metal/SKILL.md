---
name: sheet-metal
description: Model constant-thickness folded sheet-metal parts with base flange, bends, and flat pattern — not stacked boxes.
inject: auto
triggers:
  keywords: [sheet metal, sheet-metal, bend, flange, flat pattern, K-factor, press brake, hem, jog]
  tools: [bend_allowance, add_features, wall_thickness]
  phases: [feature]
---

# Sheet Metal

## Sequence

1. **Base first** — `sm_base_flange` on a closed sketch region (marks the body sheet-metal).
2. **Bend allowance** — `bend_allowance` for K-factor / BA before critical dims.
3. **Walls** — `sm_edge_flange` / `sm_hem` / `sm_jog` on RESOLVED straight thickness-face edges (query_geometry first).
4. **Verify** — `wall_thickness`, `describe_part` / `render_part` after bends.
5. **Flat** — `sm_flat_pattern` when the blank is needed; export folded body before unfold if both are required.

## Rules

- Do not approximate bends with stacked boxes.
- Bend edge refs must be resolved edge selectors, not bare queries.
- Corner relief limits multi-adjacent flanges — prefer 1–2 flange channels when unsure.

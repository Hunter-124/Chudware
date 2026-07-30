---
name: source-parts
description: Find and place standard parts from the library — correct search phrasing, taxonomy facets, part_source for plain-language needs, never hand-model catalog hardware.
inject: auto
triggers:
  keywords: [fastener, screw, bolt, bearing, purchase, catalog, off the shelf, standard part, McMaster, MISUMI, source a, source an, source the, nema, stepper, nut, washer, standoff, dowel pin, o-ring]
  tools: [part_search, part_place, part_source, parts_taxonomy, bom_rollup]
  phases: [feature, assembly, layout]
---

# Source Parts

## Search phrasing that works

Prefer **spec-like queries** over prose:

| Need | Good query |
|------|------------|
| Hex cap screw | `M6x1.0 socket head cap screw 20mm` |
| SHCS short | `SHCS M3x8` |
| Bearing | `608ZZ ball bearing` or `bearing 6204-2RS` |
| Extrusion | `2020 aluminum extrusion` / `80/20 1515` |
| Heat-set insert | `M3 heat set insert brass` |
| Tube | `4130 tube OD 25.4 wall 1.25` |

Avoid: "a small bolt", "bearing for the shaft" without size.

## Sequence

1. Browse if lost: `parts_taxonomy(node_id?)`.
2. Narrow: `part_search(query, category_id?, facets?)` — use facet keys the taxonomy returns.
3. Plain-language need: `part_source(need, constraints?)` → scored candidates each with `artifact_plan` (`part_place` | scrape | `make_standard_part`).
4. Place: `part_place(part_id)` — editable, sourced, BOM-ready.
5. Vendors: `vendors_health()`; if `needs_human_login`, ask the human to `vendor_login` — never fabricate stock/price.
6. CAM tools from catalog: `cam_tool_import(part_id)` when cutting tools are needed.
7. After several placements: `bom_rollup()` → optional `bom_price()`.

## Rules

- **Never hand-model** a catalog fastener/bearing/section when the library has it.
- Mating interfaces: place the hardware (or measure it) **before** modeling the hole that accepts it.
- Verify-before-done: after place, `describe_part` / assembly interference as relevant.

## Deeper docs

- `cad_guide('parts')`, `cad_guide('sourcing')`, `cad_guide('cost')`

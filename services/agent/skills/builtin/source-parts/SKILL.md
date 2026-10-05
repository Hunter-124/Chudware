---
name: source-parts
description: Acquire sourced catalog hardware or product CAD before modeling around it — search, match, fetch, verify and place with provenance.
inject: auto
triggers:
  keywords: [fastener, screw, bolt, bearing, purchase, catalog, off the shelf, standard part, McMaster, MISUMI, source a, source an, source the, servo, micro servo, MG90S, SG90, MG996R, stepper, NEMA, motor, actuator, reference model, STEP file, grabcad, printables, nut, washer, standoff, dowel pin, o-ring]
  tools: [part_search, part_place, part_source, match_model, make_standard_part, fetch_cad_model, import_artifact, parts_taxonomy, bom_rollup]
  phases: []
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

## Reference acquisition order

1. `part_search` / `make_standard_part` for catalog and standard parts (servos, steppers, bearings, fasteners, extrusions); use `part_get` to confirm dimensions, `list_standard_parts` for family/size keys, and `part_place` for a catalog record.
2. `match_model` / `part_source` for a discrete product's CAD. Follow the candidate's artifact plan; confirm ambiguous product identities.
3. `fetch_cad_model(url)` for a specific model page or STEP/IGES file found by web search; if `import_part=False`, pass its `artifact_path` to `import_artifact`.
4. Web search only for specs/datasheets absent from the library. Then COMMIT to cited dimensions with clearance and build; never loop on research. For an MG90S six-axis arm, search MG90S first, use its referenced servo envelope and mount dimensions, then design mating brackets.

Browse `parts_taxonomy(node_id?)` for facet keys when needed. Vendors requiring human login need `vendor_login`; never invent stock or price. Use `cam_tool_import(part_id)` for catalog cutting tools and `bom_rollup(doc_path=<saved assembly>)` after placements (optionally `bom_price(doc_path=...)`).

## Rules

- **Never hand-model** a catalog fastener/bearing/section when the library has it.
- Mating interfaces: place the hardware (or measure it) **before** modeling the hole that accepts it.
- Verify-before-done: after place, `describe_part` / assembly interference as relevant.

## Deeper docs

- `cad_guide('parts')`, `cad_guide('sourcing')`, `cad_guide('cost')`

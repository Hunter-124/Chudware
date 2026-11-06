---
name: harness
description: Design, route, check and document an electrical wiring harness with explicit connector, conductor and bus assumptions.
inject: auto
triggers:
  keywords: [wiring harness, wire harness, wire loom, formboard]
  tools: [harness_new, harness_add_connector, harness_add_wire, harness_connect, harness_from_netlist, harness_route, harness_check, harness_build_geometry, harness_report, harness_formboard, harness_get, harness_edit, harness_electrical_check, harness_bus_check, harness_cable_model, harness_to_bus_links]
---

# Wiring harness design

Use `cad_guide('harness')` and the individual tool schemas for the live API. Coordinates and lengths are world mm. A connector frame origin is its wire exit; `exit_dir` points into free space.

1. Establish the actual connector pinout, positions, loads, conductor/insulation specifications, cable construction, clearance, protection and termination details. Do not treat inferred net classes or defaults as verified ratings. Use cable datasheet bend radii and actual bundle construction where available; otherwise review the scope and assumptions of sourced estimates and override as appropriate.
2. Create the harness, add connectors and conductors and connect cavities, or seed from a netlist and review its inferences. Each wire record represents one conductor between one pin pair. Twisted pairs require separate conductor records with the same group and explicit cable construction geometry for bus checks. Ribbon needs width and thickness; routing approximates it by a conservative circular envelope and does not model orientation.
3. Route with the real assembly and keepouts. Add a clamp by splitting an interior routed segment with `harness_edit(action='add_clamp', segment_id=..., distance_mm=...)` or `point=...`. Use `edit_segment` to set or clear segment-specific OD, centerline radius or packing overrides. Inspect connectivity, bend-radius, clearance, segregation and shield-termination findings and resolve failures. Re-route and recheck when relevant geometry or wiring changes.
4. Use `harness_electrical_check` for supplied loads and conductor data. Use `harness_cable_model` and `harness_bus_check` with actual topology and cable properties for bus analysis; selected saved wires or explicit analysis links represent series sections of one signal path. Missing evidence is unknown, not a pass or certification. Read assumptions, limitations and all paginated findings. `harness_to_bus_links` instead converts explicit firmware link templates to firmware simulation bus_links.
5. Build geometry and produce a report or formboard as requested. Inspect connector pinout and from/to tables. Routed length excludes service-loop and strip allowances; inspect the cut lengths and missing data. Formboard drawing-sheet integration is not supported. Preserve and inspect the existing harness study rather than recreating it from chat.

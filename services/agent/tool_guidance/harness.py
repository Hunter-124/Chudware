"""Curated guidance for wiring-harness design tools (ECAD registry family)."""

ENTRIES = {
    "harness_new": {"prompt_note": "Create a harness study; define connectors, wires and their connections before routing."},
    "harness_add_connector": {"prompt_note": "Specify connector identity, cavities and placement; frame origin is the wire exit and exit_dir points into free space."},
    "harness_add_wire": {"prompt_note": "Each record is one conductor between one pin pair. Twisted pairs need separate grouped records and explicit cable construction geometry. Specify conductor, insulation and termination data; insulation accepts PVC, PTFE, FEP, ETFE, XLPE, silicone and TPE. Ribbon uses width and thickness, routed as a conservative circular envelope without orientation."},
    "harness_connect": {"prompt_note": "Connect the actual connector cavities; inspect the resulting connectivity before routing."},
    "harness_from_netlist": {"prompt_note": "Seed connectivity from a netlist, then review inferred signals and supply connector placements and wire properties."},
    "harness_route": {"prompt_note": "Route against the actual assembly and keepouts. Sourced bend and bundle estimates are assumptions, not cable-specific ratings; override with actual cable data and inspect assumptions, failures, clearances and bends."},
    "harness_check": {"prompt_note": "Review connectivity, geometry, segregation and shield-termination findings. Sourced bend and bundle estimates are assumptions, not certification; override with actual cable data."},
    "harness_build_geometry": {"prompt_note": "Build geometry from the routed harness; check the resulting model and keep the underlying harness editable."},
    "harness_report": {"prompt_note": "Review connector pinout and from/to tables, wire cut lengths, materials and missing allowances; routed length is not the complete cut length."},
    "harness_formboard": {"prompt_note": "Generate the flattened formboard output; drawing-sheet integration is not supported."},
    "harness_get": {"prompt_note": "Inspect the persisted harness and its current derived state rather than reconstructing it from chat."},
    "harness_edit": {"prompt_note": "Edit the existing harness and re-route or re-check affected derived results. add_clamp splits an interior segment; edit_segment sets or clears segment-specific OD, centerline bend radius and packing overrides."},
    "harness_electrical_check": {"prompt_note": "Check conductor electrical limits using actual load, conductor and operating assumptions; review missing data and limitations."},
    "harness_bus_check": {"prompt_note": "Check actual topology and cable data; inspect assumptions and paginated findings. Missing evidence is unknown, not a pass or certification."},
    "harness_cable_model": {"prompt_note": "Derive a cable model from explicit construction data for bus analysis; inspect assumptions and units."},
    "harness_to_bus_links": {"prompt_note": "Convert saved harness routes using complete firmware bus-link templates with explicit endpoints and topology; this produces firmware simulation bus_links, not harness_bus_check links."},
}

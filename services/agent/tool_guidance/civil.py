"""tool_guidance.civil — curated options for the civil_* site/utility surface.

The civil tools are registered by ``services/mcp/civil_tools.py``,
``civil_hydro_tools.py`` and ``civil_link_tools.py`` and tagged family ``"sim"``
in the tool registry, but the discipline (terrain, utilities, drainage,
quantities) is its own workflow, so this module is the SOLE owner of guidance
for every ``civil_*`` name — no ``civil_*`` key exists in ``sim.py``.

Conventions every note leans on (skill ``civil-design``): civil units are SI —
m, m³/s, head m, kPa, s (rainfall alone is mm / mm/h); results persist into the
document's ``civil_site`` study ONLY when ``doc_id`` is passed; model edits
(site_edit, import, surface/grading/alignment, applied sizing) clear every stored
result, so dependent analyses must be rerun.
"""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "civil_site_new": {
        "prompt_note": (
            "Start a NEW civil site (a reopened project uses civil_load instead); the CRS must be "
            "projected metres — never guess an EPSG code — and pass doc_id or nothing persists. "
            "Next: civil_import or civil_site_edit, then civil_surface_build."
        ),
    },
    "civil_load": {
        "prompt_note": (
            "Resume a saved civil_site study (model AND stored results) after a reopen instead of "
            "rebuilding from chat memory; pass site=<saved name> when the doc holds several sites."
        ),
    },
    "civil_site_edit": {
        "prompt_note": (
            "Atomic ops=[{op: upsert|remove|set_crs|set_criteria|rename, collection, element|id}] "
            "in SI m/m³/s/kPa; any model change clears ALL stored results, so finish edits first "
            "and rerun dependent analyses, quantities and cost."
        ),
    },
    "civil_site_summary": {
        "prompt_note": (
            "Read-only check of counts, CRS, validation errors and stored result keys — run it "
            "after imports/edits before analyzing; detail=True only when you need full arrays."
        ),
    },
    "civil_import": {
        "prompt_note": (
            "format=landxml|geojson|geopackage|epanet|swmm|dem_ascii|dem_xyz; converts foreign units "
            "to SI and merges into the active site (merge=False replaces; id collisions need "
            "options.replace=True). Review dropped fields/provenance — an imported network is not a "
            "solved design."
        ),
    },
    "civil_export": {
        "prompt_note": (
            "Writes path in landxml|geojson|geopackage|ifc|epanet|swmm|qto_csv|cost_csv|msproject; "
            "epanet/swmm REQUIRE network_id, and the csv/msproject formats need civil_quantities/"
            "civil_cost_estimate/civil_schedule first. Review dropped/warning rows."
        ),
    },
    "civil_crs_transform": {
        "prompt_note": (
            "Stateless horizontal XY(Z) transform between CRS codes/local grids — elevation is passed "
            "through unchanged (no vertical datum shift) and the site model is not edited; set the "
            "site CRS deliberately with civil_site_edit set_crs."
        ),
    },
    "civil_surface_build": {
        "prompt_note": (
            "Build/update a TIN from EXACTLY ONE of points (XYZ m, optional breaklines/boundary) or "
            "dem_path (ESRI ASCII, dem_stride subsamples); clears stored results. Next: "
            "civil_surface_analyze or civil_grade_pad."
        ),
    },
    "civil_surface_analyze": {
        "prompt_note": (
            "kind=contours (interval)|slope|profile (polyline)|volume (other_surface_id = bottom; "
            "positive = fill)|volume_to_elevation|stage_storage (polygon+stages); volume kinds persist "
            "under earthwork for civil_quantities."
        ),
    },
    "civil_grade_pad": {
        "prompt_note": (
            "cut_slope/fill_slope are positive H:V ratios (2 = 2H:1V), pad_elevation m; creates a "
            "distinct graded surface (graded_surface_id must differ from surface_id) and stores "
            "cut/fill earthwork. Recheck cover/drainage on the graded surface afterwards."
        ),
    },
    "civil_watershed": {
        "prompt_note": (
            "D8 delineation from an ASCII DEM at a projected outlet_xy; a NEW catchment needs "
            "outlet_node (an existing gravity node). Then set runoff C/CN/Tc via civil_site_edit "
            "before civil_hydrology."
        ),
    },
    "civil_alignment": {
        "prompt_note": (
            "Supply EXACTLY ONE of pis (+radii/spiral_lengths) or explicit horizontal segments "
            "(headings in radians); vertical={pvis:[...]}, stations/elevations in m. Clears stored "
            "results. Next: civil_alignment_analyze."
        ),
    },
    "civil_alignment_analyze": {
        "prompt_note": (
            "kind=profile|design_vs_existing|sight_distance|sections|earthwork; ground kinds need "
            "surface_id, sections/earthwork also a template, sight_distance the real design_speed_kmh. "
            "Sections/earthwork persist under earthwork for civil_quantities."
        ),
    },
    "civil_water_solve": {
        "prompt_note": (
            "Solve a pressurized water network by network_id (head m, summary kPa); check "
            "convergence/residuals before reading the failing-first check rows. The stored heads feed "
            "civil_thrust_restraint; pass doc_id to persist them."
        ),
    },
    "civil_fire_flow": {
        "prompt_note": (
            "Give flow_m3s (m³/s) to test a required withdrawal at junction node_id, or omit it to "
            "find the residual-limited available flow; initial-state only — it does not prove "
            "fire-duration storage."
        ),
    },
    "civil_gravity_analyze": {
        "prompt_note": (
            "Steady HGL/EGL screening of a gravity network: flows={node|conduit id: m³/s}; "
            "catchment_peaks=True adds rational peaks (storm intensity/IDF) — never ALSO pass those "
            "flows. Pass surface_id for terrain cover; screening only, not final code acceptance."
        ),
    },
    "civil_gravity_design": {
        "prompt_note": (
            "Supply the real available_diameters (m) and run apply=False first; apply=True writes the "
            "sizes and clears results, so rerun civil_gravity_analyze. Larger pipes never fix an "
            "adverse slope."
        ),
    },
    "civil_open_channel": {
        "prompt_note": (
            "Stateless (nothing persisted): kind=normal_depth (n, slope)|critical_depth|standard_step "
            "(length_m, control_depth_m)|hydraulic_jump (y1_m)|capacity; shape={shape: "
            "circular|rectangular|trapezoidal, diameter|width|height, side_slope}, SI m and m³/s."
        ),
    },
    "civil_hydrology": {
        "prompt_note": (
            "kind=rational|scs_runoff|tc|unit_hydrograph|design_storm|runoff_hydrograph|idf; area m², "
            "time s, rainfall mm and mm/h, catchment_id fills omitted area/C/CN/Tc. IDF data must come "
            "from the user — never invent it; rational gives a peak, not a volume."
        ),
    },
    "civil_detention_route": {
        "prompt_note": (
            "Modified Puls routing: inflow={t_s, q_m3s} or catchment_id+storm, stage_storage "
            "[[stage_m, m³]] or surface_id+polygon+stages, explicit outlets; structure_id writes a "
            "detention_pond. Check mass balance; downstream gravity screening does not route storage."
        ),
    },
    "civil_culvert": {
        "prompt_note": (
            "HDS-5 headwater for a culvert spec or site structure_id; tailwater_m is depth above the "
            "outlet invert (required), flows is one m³/s value or an ascending list for a performance "
            "curve. Refused points must not be dropped to make a curve pass."
        ),
    },
    "civil_buried_pipe_check": {
        "prompt_note": (
            "inputs={cover_m, od_m, gamma_kn_m3, wheel/contact loads, ring stiffness/EI, E' or "
            "soil_class, groundwater, ...} in m/kPa/kN — no truck or soil values are assumed; "
            "conduit/network/surface ids sample cover at ONE station. Next: civil_fea_handoff "
            "source='buried'."
        ),
    },
    "civil_thrust_restraint": {
        "prompt_note": (
            "Fitting thrust in kPa/m/kN (angle_deg degrees); omitted pressure_kpa reads a stored "
            "civil_water_solve head at node_id — not surge/test pressure. Give fitting.direction "
            "(global unit XYZ) if a thrust FEA handoff follows; bearing_kpa needs fitting.block geometry."
        ),
    },
    "civil_pipe_supports": {
        "prompt_note": (
            "Support spacing on a piping model (paths mm): supply elastic_modulus_gpa, "
            "allowable_stress_mpa (available bending budget at temperature) and pipe_density_kg_m3 — "
            "none inferred from a material name. Tributary screening, not ASME acceptance; verify "
            "with run_fea_static."
        ),
    },
    "civil_piping_couple": {
        "prompt_note": (
            "Fixed-point couple of a civil water network_id with a real plant piping solve at "
            "tie-ins (civil m/m³/s, plant mm, kPa, m³/h); needs steady DDA and liquid piping. Check "
            "converged AND per-tie agreement within tol_rel; re-couple after editing either side."
        ),
    },
    "civil_fea_handoff": {
        "prompt_note": (
            "Builds a civil_to_fea envelope from stored source='thrust'|'buried' results (kN/kPa -> "
            "N/Pa) — it never solves. Pass target_doc_id and face_selector to bind the load, then run "
            "run_fea_static."
        ),
    },
    "civil_requirement_evidence": {
        "prompt_note": (
            "Maps stored check rows of source_key (water, gravity, buried, ...) to requirement ids; "
            "requirement_ids_by_check maps each check name to one id or a list. Edits nothing — "
            "attach the evidence with req_update, and a failing row stays failing."
        ),
    },
    "civil_quantities": {
        "prompt_note": (
            "Run after FINAL edits/checks: measures pipes, structures, trench (needs bedding_depth_m, "
            "sample_step_m, width or clearance + physical OD — not nominal ID) and stored earthwork; "
            "persists qto for civil_cost_estimate."
        ),
    },
    "civil_cost_estimate": {
        "prompt_note": (
            "Prices the stored qto (no_qto -> run civil_quantities) with a USER price_book of "
            "unit/unit_price/currency/source/date; never invent rates — unpriced items are listed, "
            "not zeroed."
        ),
    },
    "civil_schedule": {
        "prompt_note": (
            "CPM from user tasks [{id, duration_d, predecessors [{id, type FS|SS|FF|SF, lag_d}]}] "
            "linked to model elements for 4D; start_date (ISO) enables msproject export, day returns "
            "planned state, not measured progress."
        ),
    },
}

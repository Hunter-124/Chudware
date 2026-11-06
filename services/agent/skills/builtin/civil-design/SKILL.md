---
name: civil-design
description: Design terrain, site utilities, drainage and plant tie-ins with SI civil models, profile-driven checks, coupled piping/FEA evidence and persisted deliverables.
inject: auto
triggers:
  keywords: [civil engineering, site design, site grading, terrain, earthwork, water distribution, fire flow, sewer, stormwater, catchment, detention, culvert, buried pipe, thrust restraint, utility connection, LandXML, GeoPackage]
  tools: [civil_site_new, civil_load, civil_site_edit, civil_import, civil_surface_build, civil_grade_pad, civil_alignment, civil_water_solve, civil_fire_flow, civil_gravity_analyze, civil_gravity_design, civil_hydrology, civil_detention_route, civil_culvert, civil_buried_pipe_check, civil_piping_couple]
---

# Civil design

Use this procedure for site/utility design and plant utility connections. Tool signatures and limitations are in the civil guide topic; `contracts/civil_schema.md` defines `aicad.civil/1` and its persistence/couplings. Civil lengths/elevations/diameters are m, flow m³/s, head m, pressure kPa, time s; rainfall alone uses mm and mm/h. Project x/y are easting/northing; z uses the project vertical datum. Piping geometry is mm, boundary flow m³/h and pressure kPa gauge. Coupling converts; never rescale twice.

Fetch each relevant procedure with `cad_skill`:

- `cad_skill(name="civil-design", path="site-and-water.md")`: design basis, site, terrain, grading, alignments, water and fire flow.
- `cad_skill(name="civil-design", path="drainage-and-structural.md")`: storm/sewer, detention, culverts, burial, thrust, supports and FEA handoff.
- `cad_skill(name="civil-design", path="coupling-and-evidence.md")`: plant coupling, quantities, cost, schedule, deliverables and evidence rules.

If not already available, call `enable_tools(tools=["civil_crs_transform", "civil_site_summary", "civil_surface_analyze", "civil_alignment_analyze", "civil_watershed", "civil_open_channel", "civil_thrust_restraint", "civil_pipe_supports", "civil_fea_handoff", "civil_quantities", "civil_cost_estimate", "civil_schedule", "civil_requirement_evidence", "civil_export", "run_fea_static", "save_part", "ask_user"])` and wait for it to succeed before using them. Mode and role restrictions still apply.

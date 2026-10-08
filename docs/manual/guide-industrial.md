# Guide: Industrial

```text
INDUSTRIAL — pressure vessels, duct systems, skids and pipe racks

Tools: vessel_size / duct_size / piping_route ... (cad_guide('piping')) /
       struct_analyze / struct_check / struct_estimate / add_features
       (weld_member frames) / run_pipe_flow

UNITS: mm for geometry, kPa GAUGE for pressure, degC, m3/s for air/fluid flow,
  m for duct segment lengths, Pa for duct pressure drops.

PRESSURE VESSELS — vessel_size(inner_diameter_mm, design_pressure_kpa,
  tan_tan_length_mm=None | volume_m3=None, design_temp_c=40, material='SA-516-70'
  (also SA-240-304, SA-240-316, SA-285-C), corrosion_allowance_mm=3,
  joint_efficiency=0.85, head_type='ellipsoidal_2_1'|'hemispherical'|
  'torispherical', fluid_density_kgm3=1000, fill_fraction=0.8,
  orientation='vertical'|'horizontal', doc_id=None, name=None)
  ASME VIII-1: shell UG-27(c)(1) t = PR/(SE-0.6P), heads UG-32; nominal
  thickness rounded up to standard plate. Returns shell/heads t_required_mm,
  t_nominal_mm, mawp_kpa, head depth_mm, hydrotest_kpa (UG-99(b)), volume_m3,
  tan_tan_length_mm and empty/operating/full-of-water masses.
  Then MODEL it: revolve the shell+head profile at the returned thicknesses
  (cad_guide('overview')), add nozzles, and route piping to the nozzle faces
  with piping_route(doc_id=..., ignore_bodies=[<vessel body>]).

DUCTS — duct_size(segments=[{id, parent=None, airflow_m3s, length_m,
  k_fittings=0, shape='round'|'rect', aspect_ratio=2}, ...],
  method='equal_friction', friction_pa_per_m=1.0, max_velocity_ms=10,
  air_temp_c=20, roughness_mm=0.09, doc_id=None, name=None)
  Segments form a tree through `parent` (the fan end has none). Standard round
  sizes, Huebscher equivalent diameter for rectangular ducts, Colebrook/
  Clamond friction. Returns per-segment size, velocity, Reynolds, friction and
  fitting dP, the critical_path and fan_static_pa (size the fan from it).

SKIDS, PIPE RACKS, SUPPORTS — structural frames:
  geometry: sketch member centrelines, then add_features with a weld_member op
  (real stock profile, miter/butt/cope; cad_skill('weldment-frame')).
  analysis: build an aicad.structure/1 doc (part_place(domain='section') for
  AISC sections) carrying the pipe/vessel operating loads, then
  struct_check(doc_path) for AISC LRFD D/C ratios and struct_analyze for raw
  member forces; struct_estimate for a rate-table budget. Pipe support spacing
  comes from piping_spec_check support_span_m — place supports at or inside it.

PERSISTENCE: doc_id + name on vessel_size / duct_size persist a `vessel` /
  `duct_system` study in that doc (saved into the .step+ by save_part).
  Decision procedure: cad_skill('piping-design').
```


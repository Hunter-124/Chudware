# Contract: industrial process sizing RPCs (`vessel_size`, `duct_size`)

> **Frozen seam.** Append-only; changes require maintainer sign-off (see
> `contracts/README.md`). Executable form: `core/piping/vessel.cpp` and
> `core/piping/duct.cpp` (library `chudware_piping`), dispatched by the piping
> method table; tests in `core/tests/test_industrial.cpp` (ctest `industrial`).
> Both are pure functions of their params (no document state, OCCT-free).

## 1. Conventions

- **Units:** lengths mm (duct segment lengths m), pressures kPa **gauge**
  (duct pressures Pa), temperatures °C, flows m³/s, densities kg/m³, masses kg.
- **Success envelope:** `{ok:true, …, warnings:[str], hint:str, next_actions:[str]}`.
- **Refusal envelope:** `{ok:false, reason:"<snake_case>", hint:str}`. Refusals never
  return partial numbers. A key that is present but not a finite number refuses with
  `invalid_parameter`; an absent or `null` key takes its default.
- **Persistence:** results are saved into the `.step+` through the existing
  `studies[]` records (`sim_study_add` / `sim_study_edit`) as
  `{type:"vessel"|"duct_system", name, engine:"cpp-industrial", params, results}`.

## 2. `vessel_size` — ASME Section VIII Division 1

### Request

| key | type | default | meaning |
|---|---|---|---|
| `inner_diameter_mm` | number > 0 | required | shell inside diameter (new condition) |
| `tan_tan_length_mm` | number ≥ 0 | — | tangent-to-tangent shell length; exactly one of this or `volume_m3` |
| `volume_m3` | number > 0 | — | target inside volume; the tan-tan length is solved |
| `design_pressure_kpa` | number > 0 | required | internal gauge design pressure **at the top** of the vessel |
| `design_temp_c` | number | 40 | design metal temperature |
| `material` | string | `SA-516-70` | `SA-516-70`, `SA-285-C`, `SA-240-304`, `SA-240-316` (spacing/hyphens/`Gr`/`TP` tolerated) |
| `corrosion_allowance_mm` | number ≥ 0 | 3 | added to every pressure thickness; R and D are taken corroded |
| `joint_efficiency` | (0, 1] | 0.85 | UW-12 E, applied to shell and heads |
| `head_type` | string | `ellipsoidal_2_1` | `ellipsoidal_2_1`, `hemispherical`, `torispherical` (standard F&D: L = D, r = 0.06L) |
| `fluid_density_kgm3` | number ≥ 0 | 1000 | operating fluid; 0 = gas service (no static head) |
| `fill_fraction` | [0, 1] | 0.8 | operating liquid fraction of the inside volume (mass only) |
| `orientation` | string | `vertical` | `vertical` or `horizontal` (sets the static-head height) |

### Method

All in mm and MPa; P is the calculation pressure, S the Table 1A allowable at the
design temperature, E the joint efficiency, R = D/2 + CA, D_c = D + 2·CA.

| part | formula | MAWP back-calc (t = t_nominal − CA) |
|---|---|---|
| shell, circumferential | UG-27(c)(1) `t = PR/(SE − 0.6P)` | `SEt/(R + 0.6t)` |
| shell, longitudinal | UG-27(c)(2) `t = PR/(2SE + 0.4P)` | `2SEt/(R − 0.4t)` |
| 2:1 ellipsoidal head | UG-32(d) `t = PD_c/(2SE − 0.2P)` | `2SEt/(D_c + 0.2t)` |
| hemispherical head | UG-32(f) `t = PL/(2SE − 0.2P)`, L = D_c/2 | `2SEt/(L + 0.2t)` |
| torispherical head | UG-32(e) `t = 0.885PL/(SE − 0.1P)`, L = D_c | `SEt/(0.885L + 0.1t)` |

- **Static head (UG-22(b)):** P = design pressure + ρ·g·H of the vessel full of the
  operating fluid, H = tan-tan + 2·head depth (vertical) or ID (horizontal). Reported
  MAWP values are **at the top**: component MAWP − static head.
- **Required thickness:** `t_required = max(t_pressure, 1.5 mm UG-16(b)) + CA`, then
  `t_nominal` = next plate in 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32,
  36, 40, 45, 50, 55, 60, 65, 70, 75, 80, 90, 100, 110, 120, 130, 140, 150 mm.
- **Allowable stress:** ASME II-D Table 1A customary-unit values (ksi × 6.894757),
  linear in °F; below the first tabulated temperature the first value holds down to the
  material minimum (−29 °C carbon steel, −196 °C austenitic). Austenitic values are the
  lower (non-G5) set.
- **Hydrotest (UG-99(b)):** `hydrotest_kpa = 1.3 · MAWP · S(20 °C)/S(design)` at the top;
  `hydrotest_bottom_kpa` adds the water head of the full vessel.
- **Geometry:** inside head volume = k·D³ with k = π/24 (2:1), π/12 (hemi), 0.0809
  (F&D); inside dish depth = 0.25D, 0.5D, 0.16934D. Volume = πD²/4·L_tt + 2·k·D³.
- **Masses:** shell ring + two heads (head metal = k·((D + 2t)³ − D³)) at the material
  density (7850 carbon, 7930 304, 7980 316 kg/m³); operating adds `fill_fraction`·V·ρ;
  full-water adds V·1000. Nozzles, supports, internals and straight flanges excluded.
- **Data sources:** BPVC II-D 2023 Table 1A (same values in 2021), cross-checked against the
  II-D values reproduced in ASME B31.1-2022 Tables A-1/A-3 and B31.3-2024 Table A-1C;
  `material.source` names the edition.

### Response

```
{ok:true, code:"ASME VIII-1",
 material:{name, description, allowable_mpa, allowable_test_mpa, density_kgm3, source},
 design:{pressure_kpa, temp_c, static_head_kpa, calc_pressure_kpa, corrosion_allowance_mm,
         joint_efficiency, orientation, fluid_density_kgm3, fill_fraction},
 shell:{inner_diameter_mm, t_pressure_mm, t_required_mm, t_nominal_mm, mawp_kpa, formula},
 heads:{type, t_pressure_mm, t_required_mm, t_nominal_mm, mawp_kpa, depth_mm,
        volume_each_m3, formula},
 mawp_kpa, governing:"shell"|"heads", lowest_stress_ratio, hydrotest_kpa,
 hydrotest_bottom_kpa, volume_m3, tan_tan_length_mm, overall_length_mm,
 mass_empty_kg, mass_operating_kg, mass_full_water_kg, warnings, hint, next_actions}
```

`t_pressure_mm` excludes CA; `t_required_mm` includes CA and the UG-16(b) minimum.

### Validity limits and refusals

| reason | when |
|---|---|
| `missing_inner_diameter` / `invalid_inner_diameter` | ID absent or ≤ 0 |
| `missing_length` / `conflicting_length` | neither / both of `tan_tan_length_mm`, `volume_m3` |
| `invalid_tan_tan_length` / `invalid_volume` | negative length / non-positive volume |
| `volume_too_small_for_diameter` | two heads alone exceed `volume_m3` |
| `missing_design_pressure` / `invalid_design_pressure` | absent / zero |
| `external_pressure_not_supported` | negative design pressure (UG-28 not covered) |
| `unknown_material` | material not in the table (hint lists supported) |
| `temperature_below_table` | below the material minimum (UCS-66/UHA-51 MDMT evaluation needed) |
| `temperature_above_table` | above the last Table 1A entry: SA-516-70 538 °C, SA-285-C 482 °C, SA-240-304/316 649 °C |
| `thick_wall_beyond_ug27` | P > 0.385SE or t > R/2 (Appendix 1-2 territory) |
| `thick_head_beyond_ug32` | hemispherical P > 0.665SE or t > 0.356L (Appendix 1-3) |
| `plate_beyond_series` | required thickness > 150 mm |
| `unknown_head_type`, `invalid_orientation`, `invalid_joint_efficiency`, `invalid_corrosion_allowance`, `invalid_fluid_density`, `invalid_fill_fraction` | bad enumerations / ranges |

Warnings: P within 80 % of 0.385SE; ellipsoidal/torispherical t/L < 0.002 (Appendix
1-4(f)); design pressure < 103 kPa (outside VIII-1 scope, U-1(c)(2)(h)); carbon steel
above 425 °C (creep/graphitization); tan-tan/ID outside 1.5–6.

## 3. `duct_size` — equal-friction air ducts

### Request

| key | type | default | meaning |
|---|---|---|---|
| `segments` | array, non-empty | required | duct tree, see below |
| `method` | string | `equal_friction` | only `equal_friction` is supported |
| `friction_pa_per_m` | number > 0 | 1.0 | target friction rate |
| `max_velocity_ms` | number > 0 | 10 | velocity cap; segments upsize until met |
| `air_temp_c` | number | 20 | air density (ideal gas, 101.325 kPa) and Sutherland viscosity via `fluid_properties("air")` |
| `roughness_mm` | number ≥ 0 | 0.09 | absolute roughness (galvanized steel 0.09) |

Segment: `{id: string|integer, parent?: id|null, airflow_m3s > 0, length_m ≥ 0,
k_fittings ≥ 0 = 0, shape: "round"|"rect" = "round", aspect_ratio ∈ [1, 8] = 2}`.
`parent` null/absent marks a root (fan outlet). `aspect_ratio` is width/height.

### Method

- Exact round diameter D*: bisection on `f·(1/D)·½ρV² = friction_pa_per_m` with
  V = Q/(πD²/4), Darcy f from Colebrook (Clamond's exact solution) for Re ≥ 2300,
  64/Re below.
- **Round:** next size ≥ D* in the ISO R20 / EN 1506 series 100, 112, 125, 140, 150, 160,
  180, 200, 224, 250, 280, 300, 315, 355, 400, 450, 500, 560, 600, 630, 710, 800, 900,
  1000, 1120, 1250, 1400, 1600, 1800, 2000 mm; upsized further while V > cap.
- **Rect:** Huebscher `De = 1.30·(ab)^0.625/(a+b)^0.25`; with w = AR·h, De = c·h,
  c = 1.30·AR^0.625/(1+AR)^0.25, so h = D*/c, w = AR·h, each rounded up to 50 mm
  (minimum 100 mm); width grows in 50 mm steps while V = Q/(wh) > cap.
- Friction uses the (equivalent) round diameter at the segment flow:
  `dp_friction = f·L/De·½ρV_De²`; fittings use the actual mean velocity:
  `dp_fittings = k·½ρV²`; `dp_total = dp_friction + dp_fittings`.
- `path_pa` = Σ dp_total from the root to the segment; `critical_path` = root→segment ids
  of the largest `path_pa`; `fan_static_pa` = that maximum (duct system only).

### Response

```
{ok:true, method:"equal_friction",
 air:{temp_c, density_kgm3, viscosity_pas, source},
 friction_pa_per_m, max_velocity_ms, roughness_mm,
 segments:[{id, parent, shape, airflow_m3s, length_m, diameter_exact_mm,
            diameter_mm (null for rect), width_mm?, height_mm?, equivalent_diameter_mm,
            velocity_ms, reynolds, friction_factor, friction_rate_pa_per_m,
            dp_friction_pa, dp_fittings_pa, dp_total_pa, path_pa,
            upsized_for_velocity, children:[ids]}],
 critical_path:[ids], fan_static_pa, warnings, hint, next_actions}
```

### Validity limits and refusals

| reason | when |
|---|---|
| `missing_segments` | no non-empty `segments` array |
| `invalid_segment`, `invalid_segment_id`, `duplicate_segment_id` | malformed segment / id |
| `invalid_parent` | parent not an id |
| `unknown_parent` | parent names no segment (hint names both ids) |
| `cycle_detected` | parent links loop (hint names the segment) |
| `missing_airflow` / `invalid_airflow` | airflow absent / ≤ 0 |
| `missing_length` / `invalid_length` | length absent / negative |
| `invalid_k_fittings`, `invalid_shape`, `invalid_aspect_ratio` | out-of-range segment fields |
| `unsupported_method` | method other than `equal_friction` |
| `invalid_friction_rate`, `invalid_max_velocity`, `invalid_roughness` | out-of-range globals |
| `air_properties_unavailable` | air model failed (e.g. temperature ≤ −273.15 °C) |
| `duct_exceeds_series` | a round segment needs > 2000 mm (or cannot meet the cap); split the flow or use `rect` |

Warnings: velocity upsizing; rounded rect aspect ratio > 4:1 (SMACNA); Re < 4000;
parent airflow below the sum of its children (continuity); multiple roots (treated as
parallel branches off one fan plenum). `fan_static_pa` excludes terminals, coils,
filters and dampers.

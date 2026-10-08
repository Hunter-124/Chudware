# Contract: hydraulic motor sizing and pump drive selection

> RPCs: `hydraulic_size_motor`, `pump_drive_size`
> Owner module: `core/hydraulic/motor.cpp` (`chudware::hydraulic::size_motor`,
> `chudware::hydraulic::pump_drive_size`, both `ojson f(const ojson& params)`)
> Tests: `core/hydraulic/motor_selftest.inc` (included by `core/tests/test_hydraulic.cpp`)
> Units: bar, L/min, m³/h, m, N·m, rpm, kW, hp, V, A, Hz

Both are pure functions: no document state, OCCT-free. Persisted results go
into the `hydraulic_circuit` study (`results.sizing.motors` / `results.sizing.drives`)
or the `piping_network` study (`results.pump_drive`).

## Envelopes

Success: `{ "ok": true, ..., "warnings": [string], "hint": string, "next_actions": [string] }`.

Refusal: `{ "ok": false, "reason": "<snake_case>", "hint": string, "param"?: string }`.
Every input refusal names the parameter:

| reason | when |
|---|---|
| `missing_<param>` | required parameter absent or `null` (`missing_flow` / `missing_pressure` for the either-or pairs) |
| `invalid_<param>` | wrong JSON type, non-finite, non-positive, efficiency outside (0, 1], unknown enum value |
| `ambiguous_flow` / `ambiguous_pressure` | both members of an either-or pair given |
| `displacement_exceeds_catalog` | required displacement above the largest step of the motor family (adds `displacement_cc_rev`, `catalog_max_cc_rev`) |
| `drive_exceeds_ladder` | required motor power above 400 kW (IEC) / 500 hp (NEMA) |

Selections never undersize: the chosen standard size is always the smallest
ladder step ≥ the requirement. A requirement above the ladder is refused, not
clamped.

---

## 1. `hydraulic_size_motor`

### Request

| field | type | default | notes |
|---|---|---|---|
| `torque_nm` | number > 0 | required | output torque at the shaft |
| `speed_rpm` | number > 0 | required | output speed |
| `pressure_bar` | number > 0 | required | differential pressure available across the motor |
| `mech_efficiency` | (0, 1] | 0.9 | hydro-mechanical efficiency ηm |
| `vol_efficiency` | (0, 1] | 0.95 | volumetric efficiency ηv |
| `type` | `"gear"`\|`"vane"`\|`"piston"`\|`"orbital"` | — | omitted → generic ISO 3662 ladder |

### Math

- Displacement `D = 2π·T / (Δp·ηm)` (m³/rev → cc/rev).
- Flow `Q = D·n / ηv` (L/min).
- Output `P_out = T·ω`; hydraulic input `P_in = Δp·Q = P_out / (ηm·ηv)`.
- At the selected standard step `D_s`: `Δp_s = 2π·T / (D_s·ηm)`, `Q_s = D_s·n / ηv`.

### Response

`type`, `ladder_basis`, echoed inputs, `displacement_cc_rev` (exact),
`standard_displacement_cc_rev` (next catalogue step), `flow_lpm` (exact D),
`flow_lpm_at_standard`, `pressure_bar_at_standard`, `output_power_kw`,
`input_hydraulic_power_kw`, `overall_efficiency`, `suitable_types` (families
whose speed window, pressure rating and ladder all accept the duty),
`ladder_cc_rev`, `warnings`, `hint`, `next_actions`.

### Displacement ladders (cc/rev)

| type | range | steps | basis |
|---|---|---|---|
| generic | 1–1000 | 1 1.25 1.6 2 2.5 3.15 4 5 6.3 8 10 12.5 16 20 25 31.5 40 50 63 80 100 125 160 200 250 315 400 500 630 800 1000 | ISO 3662 R10 preferred geometric displacements |
| gear | 4–100 | 4 5 6 8 10 11 14 16 19 22 25 28 32 36 40 45 50 56 63 71 80 90 100 | external gear motor catalogue steps |
| vane | 10–200 | 10 16 20 25 32 40 50 63 80 100 125 160 200 | balanced vane motor catalogue steps |
| piston | 10–500 | 10 12 16 23 28 32 45 56 63 80 90 107 125 160 180 200 250 355 500 | fixed bent-axis piston (A2FM-type frame sizes) |
| orbital | 8–800 | 8 12.5 20 32 40 50 63 80 100 125 160 200 250 315 400 500 630 800 | orbital LSHT (OML…OMV-type steps) |

### Warning thresholds (representative catalogue values)

| type | typical speed window, rpm | continuous Δp, bar |
|---|---|---|
| gear | 500–4000 | 250 |
| vane | 100–3000 | 210 |
| piston | 50–5000 | 400 |
| orbital | 10–1000 | 175 |

Also warned: the standard step exceeding 1.5× the exact displacement (states
the resulting flow and working Δp). Generic has no speed/pressure checks.

Hand check: 100 N·m, 500 rpm, 200 bar, ηm 0.9, ηv 0.95 → D = 34.91 cc/rev,
Q = 18.37 L/min, P_out = 5.236 kW, P_in = 6.124 kW; gear step 36, generic step 40.

---

## 2. `pump_drive_size`

### Request

| field | type | default | notes |
|---|---|---|---|
| `kind` | `"hydraulic"`\|`"centrifugal"` | required | |
| `flow_lpm` \| `flow_m3h` | number > 0 | exactly one required | |
| `pressure_bar` \| `head_m` | number > 0 | exactly one required | pump differential |
| `fluid_density_kgm3` | number > 0 | 870 hydraulic / 998 centrifugal | converts head ↔ pressure |
| `pump_efficiency` | (0, 1] | 0.88 hydraulic (overall) / 0.7 centrifugal | |
| `speed_rpm` | number > 0 | 4-pole synchronous at `frequency_hz` (1500 @ 50 Hz, 1800 @ 60 Hz) | pump/duty shaft speed |
| `standard` | `"IEC"`\|`"NEMA"` | `"IEC"` | |
| `service_factor` | number ≥ 1 | 1.15 | |
| `voltage_v` | number > 0 | 400 IEC / 460 NEMA | line-to-line (3-ph) or line (1-ph) |
| `phases` | 1 \| 3 | 3 | |
| `frequency_hz` | number > 0 | 50 IEC / 60 NEMA | |
| `efficiency_class` | `"IE2"`\|`"IE3"`\|`"IE4"` | `"IE3"` | |

### Math

- `Δp = p` or `ρ·g·H` (g = 9.80665); `P_hyd = Δp·Q`.
- `P_shaft = P_hyd / η_pump`; `P_required = P_shaft · SF`.
- Rating: smallest ladder step ≥ `P_required` (NEMA: `P_required / 0.7457` hp).
- Poles: the largest even pole count `p ∈ {2…12}` with `120·f/p ≥ speed_rpm`
  (induction motors run below sync by their slip). `slip = (sync − speed)/sync`;
  slip > 8 % warns (needs VFD or belt/gear drive); speed above 2-pole sync picks
  2 poles and warns.
- Efficiency η_m: IEC 60034-30-1 nominal, interpolated linearly in log(kW) at
  the motor rating.
- Power factor: typical full-load value interpolated in log(kW).
- `rated_input_power_kw = rating / η_m`;
  `full_load_current_a = rated_input / (√3·V·pf)` (3-ph) or `/(V·pf)` (1-ph).
- `input_power_kw = P_shaft / η_m` (electrical draw at the duty point).

### Response

`kind`, `standard`, `flow_lpm`, `flow_m3h`, `pressure_bar`, `head_m`,
`fluid_density_kgm3`, `pump_efficiency`, `service_factor`,
`hydraulic_power_kw`, `shaft_power_kw`, `required_motor_kw`,
`motor_rating_kw` (always; NEMA = hp × 0.7457), `motor_rating_hp` (NEMA only),
`poles`, `sync_speed_rpm`, `speed_rpm`, `slip`, `load_factor` (shaft/rating),
`efficiency_class`, `motor_efficiency` (fraction), `efficiency_basis`,
`power_factor`, `power_factor_basis`, `input_power_kw`, `rated_input_power_kw`,
`full_load_current_a`, `voltage_v`, `phases`, `frequency_hz`,
`iec_frame` (IEC) or `nema_frame` (NEMA) — `null` when untabulated (warned),
`frame_basis`, `shaft_torque_nm`, `motor_rated_torque_nm`, `warnings`, `hint`,
`next_actions`.

### Ladders and tables

**IEC 60072-1 rated outputs, kW:** 0.12 0.18 0.25 0.37 0.55 0.75 1.1 1.5 2.2 3
4 5.5 7.5 11 15 18.5 22 30 37 45 55 75 90 110 132 160 200 250 315 355 400.

**IEC frames (TEFC IC411, IM B3), EN 50347 output↔frame assignment:**

| kW | 2-pole | 4-pole | 6-pole |
|---|---|---|---|
| 0.12 | — | 63M | — |
| 0.18 | 63M | 63M | 71M |
| 0.25 | 63M | 71M | 71M |
| 0.37 | 71M | 71M | 80M |
| 0.55 | 71M | 80M | 80M |
| 0.75 | 80M | 80M | 90S |
| 1.1 | 80M | 90S | 90L |
| 1.5 | 90S | 90L | 100L |
| 2.2 | 90L | 100L | 112M |
| 3 | 100L | 100L | 132S |
| 4 | 112M | 112M | 132M |
| 5.5 | 132S | 132S | 132M |
| 7.5 | 132S | 132M | 160M |
| 11 | 160M | 160M | 160L |
| 15 | 160M | 160L | 180L |
| 18.5 | 160L | 180M | 200L |
| 22 | 180M | 180L | 200L |
| 30 | 200L | 200L | 225M |
| 37 | 200L | 225S | 250M |
| 45 | 225M | 225M | 280S |
| 55 | 250M | 250M | 280M |
| 75 | 280S | 280S | 315S |
| 90 | 280M | 280M | 315M |
| 110 | 315S | 315S | — |
| 132 | 315M | 315M | — |
| 160 / 200 | — | 315L | — |
| 250 / 315 | — | 355M | — |
| 355 / 400 | — | 355L | — |

4-pole rows above 132 kW are manufacturer-typical (EN 50347 stops at 315M).
Other pole counts → `iec_frame: null` + warning.

**NEMA MG 1 hp ratings:** ¼ ⅓ ½ ¾ 1 1.5 2 3 5 7.5 10 15 20 25 30 40 50 60 75
100 125 150 200 250 300 350 400 450 500.

**NEMA T-frames, 4-pole (1800 rpm) TEFC:** ≤ ¾ hp 56; 1 143T; 1.5–2 145T;
3 182T; 5 184T; 7.5 213T; 10 215T; 15 254T; 20 256T; 25 284T; 30 286T;
40 324T; 50 326T; 60 364T; 75 365T; 100 404T; 125 405T; 150 444T; 200 445T;
250–300 447T; 350–500 449T (≥ 250 hp manufacturer-typical). Other pole
counts → `nema_frame: null` + warning.

**IEC 60034-30-1:2014 nominal efficiency, 50 Hz, 4-pole, %:**

| kW | IE2 | IE3 | IE4 |
|---|---|---|---|
| 0.12 | 59.1 | 64.8 | 69.8 |
| 0.18 | 64.7 | 69.9 | 74.7 |
| 0.25 | 68.5 | 73.5 | 77.9 |
| 0.37 | 72.7 | 77.3 | 81.1 |
| 0.55 | 77.1 | 80.8 | 83.9 |
| 0.75 | 79.6 | 82.5 | 85.7 |
| 1.1 | 81.4 | 84.1 | 87.2 |
| 1.5 | 82.8 | 85.3 | 88.2 |
| 2.2 | 84.3 | 86.7 | 89.5 |
| 3 | 85.5 | 87.7 | 90.4 |
| 4 | 86.6 | 88.6 | 91.1 |
| 5.5 | 87.7 | 89.6 | 91.9 |
| 7.5 | 88.7 | 90.4 | 92.6 |
| 11 | 89.8 | 91.4 | 93.3 |
| 15 | 90.6 | 92.1 | 93.9 |
| 18.5 | 91.2 | 92.6 | 94.2 |
| 22 | 91.6 | 93.0 | 94.5 |
| 30 | 92.3 | 93.6 | 94.9 |
| 37 | 92.7 | 93.9 | 95.2 |
| 45 | 93.1 | 94.2 | 95.4 |
| 55 | 93.5 | 94.6 | 95.7 |
| 75 | 94.0 | 95.0 | 96.0 |
| 90 | 94.2 | 95.2 | 96.1 |
| 110 | 94.5 | 95.4 | 96.3 |
| 132 | 94.7 | 95.6 | 96.4 |
| 160 | 94.9 | 95.8 | 96.6 |
| 200–1000 | 95.1 | 96.0 | 96.7 |

**Typical full-load power factor** (4-pole TEFC IE3, 400 V 50 Hz;
representative of ABB M3BP/M3AA and Siemens SIMOTICS 1LE1 LV catalogues):
0.12 kW 0.64 · 0.18 0.66 · 0.25 0.68 · 0.37 0.70 · 0.55 0.72 · 0.75 0.74 ·
1.1 0.77 · 1.5 0.79 · 2.2 0.81 · 3 0.81 · 4 0.82 · 5.5 0.83 · 7.5 0.84 ·
11 0.84 · 15–22 0.85 · 30–55 0.86 · 75–132 0.87 · ≥ 160 0.88.

### Warnings

- motor rating > 1.5 × pump shaft power (light-load efficiency/pf penalty);
- slip > 8 % or speed above 2-pole sync (VFD/belt/gear drive needed);
- non-mains frequency (not 50/60 Hz ± 1); NEMA motor on a non-60 Hz supply;
- single-phase above ≈ 3.7 kW (5 hp);
- no tabulated frame for the pole count/rating.

### Limits

- Efficiency and power factor are nominal/typical, not nameplate. The 50 Hz
  4-pole efficiency table is applied to every pole count and frequency; the
  deviation is stated in `efficiency_basis`.
- Starting duty, run-out power on the pump curve, NPSH, altitude/ambient
  derating and VFD harmonics are out of scope (listed in `next_actions`).
- Low-voltage ladders only: > 400 kW / 500 hp is refused.

Hand checks: centrifugal 50 m³/h, 30 m, η 0.7 → P_hyd 4.078 kW, shaft
5.826 kW, ×1.15 = 6.699 kW → IEC 7.5 kW, 4-pole, 132M, IE3 90.4 %, FLC 14.26 A
@ 400 V. Hydraulic HPU 40 L/min, 180 bar, η 0.85 → 12.0 kW, shaft 14.12 kW,
×1.15 = 16.24 kW → IEC 18.5 kW (180M); NEMA → 21.77 hp → 25 hp, 284T @ 460 V 60 Hz.

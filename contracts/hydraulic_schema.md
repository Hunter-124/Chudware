# Contract: `aicad.hydraulic/1` — ISO 1219 Hydraulic Circuit Schema

> Schema string: `"aicad.hydraulic/1"`
> Owner module: `core/hydraulic/schema.cpp`
> Units: mm (positions), bar (pressures), L/min (flows), kN (forces)

---

## 1. Document Envelope

```json
{
  "schema": "aicad.hydraulic/1",
  "id": "<uuid>",
  "name": "<circuit name>",
  "units": {
    "pressure": "bar",
    "flow":     "L/min",
    "force":    "kN",
    "position": "mm"
  },
  "components":   [...],
  "hoses":        [...],
  "annotations":  [...]
}
```

---

## 2. Component Types (ISO 1219)

| type | ISO 1219 symbol class | description |
|---|---|---|
| `pump` | pump (rotary) | hydraulic pump (gear/vane/piston) |
| `cylinder_linear` | linear actuator | double- or single-acting hydraulic cylinder |
| `cylinder_rotary` | rotary actuator | limited-rotation motor / rotary actuator |
| `valve_directional` | directional control valve | 2/2, 3/2, 4/2, 4/3, etc. |
| `valve_check` | check valve | non-return valve |
| `valve_relief` | pressure control valve | relief / safety valve |
| `valve_flow_control` | flow control valve | throttle / flow-control valve |
| `accumulator` | accumulator | bladder / piston / diaphragm |
| `reservoir` | reservoir | hydraulic fluid tank |
| `motor_hydraulic` | hydraulic motor | continuous-rotation motor |
| `filter` | filter | inline return or pressure filter |
| `heat_exchanger` | heat exchanger | oil cooler / heater |

---

## 3. Component Object

```json
{
  "id":       "c1",
  "type":     "<component_type>",
  "name":     "Lift Cylinder",
  "position": [x_mm, y_mm],
  "angle":    0.0,
  "ports":    [
    {"id": "p1", "port_type": "pressure", "position_offset": [0, 10]},
    {"id": "p2", "port_type": "work_a",   "position_offset": [0, -10]}
  ],
  "params":   { "<key>": "<value>" },
  "sourcing": {
    "mpn":          null,
    "manufacturer": null,
    "description":  null,
    "quantity":     1,
    "unit":         "ea",
    "category":     null
  }
}
```

### 3.1 Port Types

| port_type | description |
|---|---|
| `pressure` | high-pressure supply port (P) |
| `tank` | return-to-tank port (T) |
| `work_a` | cylinder/motor work port A |
| `work_b` | cylinder/motor work port B |
| `drain` | case drain port (L) |
| `generic` | unclassified connection |

### 3.2 Component Parameters (advisory)

**pump:**
- `pump_type`: `"gear"` | `"vane"` | `"piston_axial"` | `"piston_radial"` | `"screw"`
- `displacement_cc_rev`: displacement in cc/rev
- `speed_rpm`: rated speed
- `max_pressure_bar`: rated max pressure

**cylinder_linear:**
- `actuator_type`: `"single_acting"` | `"double_acting"`
- `bore_mm`: bore diameter
- `rod_mm`: rod diameter (0 for single-acting/rodless)
- `stroke_mm`: stroke length

**cylinder_rotary:**
- `actuator_type`: `"single_acting"` | `"double_acting"`
- `max_angle_deg`: rotation range

**valve_directional:**
- `positions`: number of valve positions (2 or 3)
- `ways`: number of ports/ways (2, 3, or 4)
- `actuator`: `"solenoid"` | `"manual"` | `"pilot"` | `"spring"`
- `center_condition`: `"open"` | `"closed"` | `"tandem"` (for 3-position)

**valve_relief:**
- `set_pressure_bar`: cracking pressure

**valve_flow_control:**
- `Kv`: flow coefficient (m³/h at 1 bar)
- `Cv`: flow coefficient (gpm at 1 psi)

**accumulator:**
- `volume_l`: total gas volume
- `pre_charge_bar`: pre-charge nitrogen pressure
- `style`: `"bladder"` | `"piston"` | `"diaphragm"`

**motor_hydraulic:**
- `displacement_cc_rev`: motor displacement
- `max_speed_rpm`: maximum speed
- `max_pressure_bar`: rated max pressure

---

## 4. Hose / Line Net

Hoses connect component ports (analogous to ECAD wire nets). A hose may connect two or more ports.

```json
{
  "id":       "h1",
  "net_name": "PRESSURE_LINE",
  "connections": [
    {"component_id": "c1", "port": "p1"},
    {"component_id": "c2", "port": "p2"}
  ]
}
```

`net_name` is advisory (for readability); `connections` is the authoritative topology.

---

## 5. Sourcing Fields

Every component carries a `sourcing` block compatible with the VendorPart shape
(join seam with `bom_rollup` at B-chain merge time):

| field | type | description |
|---|---|---|
| `mpn` | string \| null | manufacturer part number |
| `manufacturer` | string \| null | manufacturer name |
| `description` | string \| null | part description |
| `quantity` | integer | quantity in circuit |
| `unit` | string | unit of measure (default `"ea"`) |
| `category` | string \| null | sourcing category |

---

## 6. Validation Rules

- `schema` field must equal `"aicad.hydraulic/1"`
- Component `id` must match `^c\d+$`; must be unique
- Component `type` must be a member of `HYD_COMPONENT_TYPES`
- Port `port_type` must be a member of `HYD_PORT_TYPES`
- Hose `id` must match `^h\d+$`; must be unique
- Hose connections must reference existing component ids
- `position` must be a 2-element list of numbers
- `angle` must be a number (degrees)

---

## 7. Units Reference

| quantity | unit | notes |
|---|---|---|
| position | mm | component placement on schematic canvas |
| pressure | bar | 1 bar = 100 kPa = 14.504 psi |
| flow | L/min | litres per minute |
| force | kN | kilonewtons (cylinder output) |
| displacement | cc/rev | cubic centimetres per revolution |
| power | kW | kilowatts (pump shaft power) |
| torque | N·m | newton-metres (pump shaft torque) |
| volume | L | litres (accumulator) |

---

## 8. Transient Simulation — `hydraulic_simulate`

Owner module: `core/hydraulic/sim.cpp` (`chudware::hydraulic::simulate`), dispatched as the
`hydraulic_simulate` RPC. Lumped-parameter circuit model integrated with an adaptive,
L-stable implicit Euler scheme (Newton + finite-difference Jacobian, steps land on every
valve-ramp breakpoint). **`hydraulic_fmu_simulate` is retired**: the always-skipped FMU/fmpy
stub and its RPC are removed; `hydraulic_simulate` is the only circuit simulator.

### 8.1 Request

```json
{
  "doc": { "schema": "aicad.hydraulic/1", ... },
  "duration_s": 5,            "output_dt_s": 0.01,      "max_step_s": 0.01,
  "valve_schedule": [{"t_s": 0.5, "component_id": "c3", "position": "a"}],
  "loads":   {"c4": {"force_n": 10000, "mass_kg": 200, "friction_n": 0, "viscous_ns_m": 0},
              "c6": {"torque_nm": 40, "inertia_kgm2": 0.02, "friction_nm": 0, "viscous_nms": 0}},
  "fluid":   {"density_kgm3": 870, "bulk_modulus_bar": 15000, "viscosity_cst": 32,
              "air_fraction": 0.002},
  "initial": {"c4": {"position_mm": 0}, "c7": {"pressure_bar": 150}, "h2": {"pressure_bar": 0}}
}
```

- `duration_s` ∈ (0, 3600]; `output_dt_s` > 0 with at most 200 000 samples; `max_step_s` > 0
  (default `min(0.01, duration_s)`).
- `valve_schedule` targets `valve_directional` only. `position`: `"a"` (= +1: P→A, B→T),
  `"b"` (= −1: P→B, A→T), `"center"` (= 0, 3-position valves only), or a number in [−1, 1]
  (proportional opening; + = a side). The spool ramps at `response_ms` per full 0→1 travel.
- `loads` (constant, keyed by component id): cylinder_linear `force_n` (> 0 resists extension),
  `mass_kg`, `friction_n` (Coulomb), `viscous_ns_m`; motor_hydraulic / cylinder_rotary
  `torque_nm` (> 0 resists A→B rotation), `inertia_kgm2`, `friction_nm`, `viscous_nms`.
  Load mass/inertia/friction add to the component's own params.
- `initial`: cylinder_linear `position_mm` (default 0 = retracted), cylinder_rotary
  `angle_deg`, motor_hydraulic `speed_rpm`, accumulator `pressure_bar` (sets the oil volume via
  the gas law and the attached node pressure), or a hose id `pressure_bar` (node pressure).
  Nodes otherwise start at 0 bar(g).
- `fluid.air_fraction`: entrained-air volume fraction at atmospheric pressure (softens β at
  low pressure; keeps absolute pressure > 0 under cavitation).

### 8.2 Topology and ports

Every hose — and every set of hoses sharing a component port — is one pressure node with
capacitance `V/β_eff(p) + V_hose/β_wall`. `V` = hose bore volume (`id_mm`, default 12.7;
`length_mm`, default 1000) plus the attached actuator chamber volumes; optional hose
`wall_bulk_modulus_bar` adds wall compliance. A node touching a `reservoir` is a fixed-pressure
boundary. Node ids are the id of the node's first hose.

A connection's `port` is resolved case-insensitively per type, or through the component's
declared `ports[{id, port_type}]` (`pressure`/`tank`/`work_a`/`work_b`/`drain` map to the roles):

| type | ports (aliases) | required |
|---|---|---|
| `pump` | `P` (pressure, out, outlet), `T` (s, suction, tank, in), `L` (drain) | `P`; unconnected `T` draws from an implicit 0 bar(g) tank |
| `motor_hydraulic`, `cylinder_rotary` | `A` (work_a, in), `B` (work_b, out), `L` | `A`, `B` |
| `cylinder_linear` | `A` (cap, bore), `B` (rod, annulus) | `A`; `B` unless `actuator_type: "single_acting"` (vented rod side) |
| `valve_directional` | `P`, `T` (R), `A`, `B` | ways 4: P,T,A,B; ways 3: P,A,T; ways 2: P,A |
| `valve_relief` | `P` (in), `T` (out) | both |
| `valve_check`, `valve_flow_control`, `filter`, `heat_exchanger` | `IN` (inlet, P, A, 1), `OUT` (outlet, T, B, 2) | both |
| `accumulator`, `reservoir` | any name | accumulator: its one port |

Declared `ports` entries that no hose references (other than drains, an implicit pump suction,
or a single-acting rod side) refuse with `unconnected_port`.

### 8.3 Component parameters read (defaults; **bold** = required)

Flow capacity (valves, filter, heat exchanger) resolves in order: `Kv` (m³/h at 1 bar) >
`Cv` (Kv = 0.865·Cv) > `orifice_area_mm2` (+ `cd`, 0.7) > `rated_flow_lpm` + `rated_dp_bar`;
if none is given the circuit's total pump flow at the type's rated Δp is assumed and a
warning names the component. Orifices follow Q = CdA·√(2Δp/ρ) with a laminar transition
(Re_crit 100, `viscosity_cst`).

| type | params |
|---|---|
| `pump` | **`displacement_cc_rev` + `speed_rpm`** or **`flow_lpm`** (delivered flow); `vol_efficiency` 0.95 (delivered = D·n·ηv); `mech_efficiency` 0.9 (shaft power); `compensator_bar` (pressure-compensated: displacement destrokes to 0 over `compensator_band_bar`, default max(2, 3 %)); `max_pressure_bar` (warning) |
| `cylinder_linear` | **`bore_mm`**, **`stroke_mm`**, `rod_mm` 0, `actuator_type` double_acting, `piston_mass_kg` (steel rod + piston estimate), `friction_n` 0, `viscous_ns_m` 0, `dead_volume_ml` (area × 10 mm per chamber); hard end stops at 0 and `stroke_mm` |
| `cylinder_rotary` | **`displacement_cc_rev`** (volume per 360°), **`max_angle_deg`**, `mech_efficiency` 0.95, `inertia_kgm2` 0.001, `friction_nm` 0, `dead_volume_ml`; end stops at 0 and `max_angle_deg` |
| `motor_hydraulic` | **`displacement_cc_rev`**; `vol_efficiency` 0.95 (Q = D·ω/ηv motoring), `mech_efficiency` 0.9 (T = D·Δp·ηm motoring), `inertia_kgm2` 5e-4 + 2e-5·D, `friction_nm` 0, `max_speed_rpm` / `max_pressure_bar` (warnings) |
| `valve_directional` | `ways` 4, `positions` 3, `center_condition` closed \| tandem (P→T) \| open (P,A,B→T) \| float (A,B→T); capacity per path (rated Δp default 5 bar); `response_ms` 30; `initial_position` (center for 3-position, `"b"` for 2-position) |
| `valve_check` | `cracking_pressure_bar` 0.5, `opening_band_bar` 1.0; capacity (rated Δp default 1 bar) |
| `valve_relief` | **`set_pressure_bar`** (cracking); `override_bar` 10 % of set; `rated_flow_lpm` (total pump flow) — Q = rated · (Δp − set)/override above cracking |
| `valve_flow_control` | **`set_flow_lpm`** (pressure-compensated: Q = set·tanh(Δp/`compensator_dp_bar` 3); `reverse_free_flow` false adds a free reverse check) **or** throttle `Kv` / `Cv` / `orifice_area_mm2` |
| `filter`, `heat_exchanger` | capacity (rated Δp default 1.0 / 0.5 bar) |
| `accumulator` | **`volume_l`**, **`pre_charge_bar`**; `polytropic_n` 1.4 (p·V^n = const, absolute pressures), `port_area_mm2` 80, `cd` 0.7; an empty accumulator's poppet closes |
| `reservoir` | `pressure_bar` 0 |

### 8.4 Result

```json
{
  "ok": true,
  "time_s": [0, 0.01, ...],
  "nodes": {"h1": {"pressure_bar": [...], "members": ["c1.P", "c2.P"], "boundary": false}},
  "components": {"c4": {"kind": "cylinder_linear", "series": {"position_mm": [...], ...}}},
  "summary": {
    "peak_pressure_bar": 210.0, "pump_energy_kj": 5.0, "shaft_energy_kj": 5.85,
    "useful_work_kj": 1.9, "heat_kj": 3.1, "stored_energy_change_kj": 0.01,
    "boundary_energy_kj": 0.0, "energy_balance_error_pct": 0.0, "efficiency": 0.38,
    "relief_open_time_s": 0.55,
    "cylinders": {"c4": {"stroke_time_s": 1.64, "end_reached_at_s": 1.75,
                         "max_velocity_mms": 121.0, "end_reached": true}},
    "motors": {"c6": {"steady_speed_rpm": 1350, "steady_torque_nm": 44.1, "max_speed_rpm": 1428}},
    "pumps": {"c1": {"mean_pressure_bar": 70.2, "mean_power_kw": 1.67, "peak_pressure_bar": 210}}
  },
  "solver": {"method": "...", "steps": 2455, "rejected_steps": 69, "newton_failures": 72,
             "states": 5, "rtol": 0.001},
  "warnings": [], "hint": "...", "next_actions": [...]
}
```

Series per type: pump `flow_lpm, pressure_bar` (outlet), `power_kw` (hydraulic Δp·Q),
`shaft_power_kw`; cylinder_linear `position_mm, velocity_mms, pressure_a_bar, pressure_b_bar,
force_n, flow_lpm`; cylinder_rotary `angle_deg, speed_rpm, pressure_a_bar, pressure_b_bar,
torque_nm, flow_lpm`; motor_hydraulic `speed_rpm, torque_nm, flow_lpm, pressure_bar` (A−B),
`power_kw` (shaft); valve_directional `spool, flow_lpm` (P port), `flow_t_lpm`; valve_relief
`flow_lpm, pressure_bar`; valve_check / valve_flow_control / filter / heat_exchanger
`flow_lpm, dp_bar`; accumulator `pressure_bar` (gas), `gas_volume_l, flow_lpm` (into);
reservoir `flow_lpm` (net return).

Summary semantics: energies integrate every solver step;
`pump = useful + heat + stored + boundary` (`energy_balance_error_pct` reports the residual).
`useful_work_kj` = work against the constant loads (`force_n`·v, `torque_nm`·ω); `heat_kj` =
orifice/valve/relief/filter losses, motor/actuator efficiency losses, friction, end stops;
`efficiency` = useful / pump (null without pump energy). `relief_open_time_s` = time any
relief passes > 1 % of its rated flow. Cylinders (and rotary actuators, reported under
`cylinders` with `max_speed_rpm`): `stroke_time_s` from first motion to reaching the end
opposite the start (null if never). Motors: `steady_*` = mean over the last 10 % of samples.
Pumps: time-averaged outlet pressure and hydraulic power.

### 8.5 Refusals (`{ok: false, reason, hint, ...}`)

| reason | when (extra fields) |
|---|---|
| `invalid_circuit` | no doc / schema errors (`errors`) |
| `invalid_settings` | bad duration/output_dt/max_step/fluid (`errors`) |
| `unknown_port` | a hose port the type does not have (`hose_id`, `component_id`, `port`) |
| `unconnected_port` | a required or declared port with no hose (`component_id`, `port`) |
| `missing_params` | required params absent (`missing`: `["c4.bore_mm", ...]`) |
| `invalid_param` | a param out of range or of the wrong JSON type (`errors`) |
| `unknown_component` | schedule / loads / initial references an unknown id (`component_id`) |
| `invalid_schedule` | bad schedule entry or a non-directional-valve target |
| `invalid_load` / `invalid_initial` | load / initial key not valid for the component type |
| `no_pressure_source` | no pump, accumulator or pressurised initial node |
| `pressure_runaway` | a node exceeded 5000 bar (`node_id`, `t_s`) — e.g. fixed pump dead-heading without a relief |
| `solver_failed` | Newton / step-size underflow or step limit (`t_s`) |

## 9. Templates

Owner module: `core/hydraulic/templates.cpp`. Desktop and MCP callers consume the same
native definitions; no client-side circuit or simulation-template dialect exists.
Templates produce ordinary editable `aicad.hydraulic/1` documents, not a separate schema.

### 9.1 Catalog — `hydraulic_templates`

Request: `{}`. Response:

```json
{
  "ok": true,
  "templates": [
    {"id": "blank", "name": "Blank circuit", "description": "...", "components": 0, "hoses": 0},
    {"id": "power_unit", "name": "Hydraulic power unit", "description": "...", "components": 4, "hoses": 4},
    {"id": "cylinder_circuit", "name": "Cylinder circuit", "description": "...", "components": 6, "hoses": 6},
    {"id": "motor_circuit", "name": "Motor circuit", "description": "...", "components": 6, "hoses": 6}
  ]
}
```

`components` and `hoses` are counts. Names and descriptions are human-readable catalog
labels; use the stable `id` to create a circuit.

### 9.2 Creation — `hydraulic_new`

The optional string `template` defaults to `"blank"`; omission retains the original empty
circuit behavior. `name` retains its existing default, `"Hydraulic Circuit"`.

| id | circuit | suggested transient |
|---|---|---|
| `blank` | Empty components and hoses | Default 5 s setup; add a connected pressure source before simulating |
| `power_unit` | 60 L reservoir, 16 cc/rev gear pump at 1450 rpm (ηv 0.95, ηm 0.9), 160 bar relief (10 bar override at 30 L/min), 40 L/min return filter | 1 s relief-bypass run |
| `cylinder_circuit` | Power unit + metered 4/3 tandem-centre solenoid valve + double-acting Ø50/28 × 300 mm cylinder | 4 s: extend at 0, retract at 2 s, centre/hold at 2.5 s; example 10 kN load, 50 kg attached mass, 2000 N·s/m viscous damping |
| `motor_circuit` | Power unit + metered 4/3 tandem-centre solenoid valve + bidirectional 80 cc/rev gear motor | 4 s: forward at 0, reverse at 2 s, forward again at 2.5 s; example constant 50 N·m torque, 0.01 kg·m² attached inertia, 0.1 N·m·s viscous damping |

Non-blank templates explicitly declare and connect all required ports, including pump
suction. Return flow passes through the filter before reaching the reservoir. Components
have readable names and separated schematic positions, with actuator at the top, valve
below it, pump/relief below the valve and tank at the bottom (positive schematic Y is up).
Every hose has `length_mm`, `id_mm`, `rating_bar` and a readable net name: Pressure,
Return, Filtered return, Suction, and Cylinder A/B or Motor A/B. Pressure/work hoses are
rated 280 bar, return hoses 40 bar and suction 10 bar. These are example industrial
parameters, not proof of suitability for a particular machine.

The directional valve starts in side A and meters each path at 10 L/min at 5 bar for the
cylinder, or 8 L/min at 5 bar for the motor. This meter-out capacity controls the assisting
load during reverse motion, rather than allowing an overrunning actuator to cavitate.
Initial pressure on the supply and A-work hoses balances the static load at zero speed.
The cylinder reaches full extension before the reverse command, then holds partly
retracted; its net lifted work remains positive. The motor demonstrates both directions
and finishes at steady forward speed. A complete return-to-start cycle under a constant
signed load has zero net load work, so these examples intentionally do not imply that
signed `useful_work_kj` measures gross work over a closed gravity cycle.

Example request:

```json
{"name": "Lift study", "template": "cylinder_circuit"}
```

The successful `hydraulic_new` envelope retains `ok`, `doc`, `warnings`, `hint` and
`next_actions`, and adds `template` (selected id) and `simulation`:

```json
{
  "ok": true,
  "doc": {"schema": "aicad.hydraulic/1", "...": "..."},
  "template": "cylinder_circuit",
  "simulation": {
    "duration_s": 4.0, "output_dt_s": 0.01, "max_step_s": 0.01,
    "valve_schedule": [
      {"t_s": 0.0, "component_id": "c5", "position": "a"},
      {"t_s": 2.0, "component_id": "c5", "position": "b"},
      {"t_s": 2.5, "component_id": "c5", "position": "center"}
    ],
    "loads": {"c6": {"force_n": 10000.0, "mass_kg": 50.0, "viscous_ns_m": 2000.0}},
    "fluid": {"density_kgm3": 870.0, "bulk_modulus_bar": 15000.0,
              "viscosity_cst": 32.0, "air_fraction": 0.002},
    "initial": {"c6": {"position_mm": 0.0},
                "h1": {"pressure_bar": 50.92958178940651},
                "h5": {"pressure_bar": 50.92958178940651}}
  },
  "warnings": [], "hint": "...", "next_actions": ["..."]
}
```

`simulation` uses the exact §8 request keys, without `doc`: call `hydraulic_simulate` with
`{"doc": result.doc, ...result.simulation}`. Creation does not run a simulation. Example
constant force/torque resists positive motion and assists negative motion (§8.1); adapt
loads, timing and component ratings to the real duty before treating a run as evidence.

An unknown id is refused without creating a circuit:

```json
{"ok": false, "reason": "unknown_template",
 "hint": "Valid templates: blank, power_unit, cylinder_circuit, motor_circuit."}
```

### 9.3 MCP persistence

`hydraulic_templates()` forwards the native catalog. `hydraulic_new(name=..., template=...,
doc_id=...)` forwards the selected id to the native engine, sets the returned circuit as
the active session circuit, and persists it as `params.circuit` in the existing
`hydraulic_circuit` study. The returned `simulation` keys are persisted directly into
`params` (duration, output step, schedule, loads, fluid and initial conditions), just like
an explicit `hydraulic_simulate` setup. They are not nested under `params.simulation`.
`hydraulic_load` restores the circuit and returns those keys in `simulation_setup`.
Refusals retain the native `reason` and `hint`, leave the previous session circuit intact
and do not create or change a study.


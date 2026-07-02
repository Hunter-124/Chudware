# Contract: `aicad.hydraulic/1` — ISO 1219 Hydraulic Circuit Schema

> Schema string: `"aicad.hydraulic/1"`
> Owner module: `engine/hydraulic/schema.py`
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

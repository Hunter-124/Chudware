# Contract: PCB / Board Document Schema — v1

> `BoardDocument` (`schema: "aicad.pcb/1"`). JSON-serializable. Canonical units **mm internally**.
> Ids assigned, never reused. Unknown keys preserved. Net assignments reference netlist net names
> (see `netlist.md`). Changes require a version bump.

## 1. Top-level

```json
{
  "schema": "aicad.pcb/1",
  "id": "uuid4",
  "name": "Astable 555",
  "linked_sch": "uuid4|null",               // schematic this board is annotated from
  "stackup": Stackup,
  "outline": Outline,
  "footprints": [Footprint, ...],
  "tracks": [Track, ...],
  "vias": [Via, ...],
  "zones": [Zone, ...],
  "graphics": [Graphic, ...],               // silk/fab/courtyard/edge text & shapes not in footprints
  "rules": DesignRules,
  "nets": [{"name":"GND","class":"default"}, ...],   // net registry (mirrors netlist net names)
  "net_classes": {"default": NetClass, "power": NetClass},
  "origin": [x, y]                          // drill/place origin
}
```

## 2. Layers & stackup

Layer ids are canonical strings (KiCad-compatible names eased import):

- Copper: `"F.Cu"`, `"In1.Cu"`, `"In2.Cu"`, ..., `"B.Cu"`.
- Technical: `"F.SilkS"`,`"B.SilkS"`,`"F.Mask"`,`"B.Mask"`,`"F.Paste"`,`"B.Paste"`,
  `"F.Fab"`,`"B.Fab"`,`"F.CrtYd"`,`"B.CrtYd"`,`"Edge.Cuts"`,`"Cmts.User"`,`"Dwgs.User"`.

```json
Stackup: {
  "copper_layers": 2,                       // 1,2,4,6,...
  "thickness": 1.6,                         // total board mm
  "layers": [                               // physical order top->bottom (copper + dielectric)
    {"id":"F.Cu","type":"copper","thickness":0.035},
    {"id":"diel1","type":"core","thickness":1.53,"er":4.5},
    {"id":"B.Cu","type":"copper","thickness":0.035}
  ]
}
```

## 3. Outline

```json
Outline: {"shapes":[{"type":"line","p1":[x,y],"p2":[x,y]},
                    {"type":"arc","center":[x,y],"radius":r,"a0":deg,"a1":deg},
                    {"type":"circle","center":[x,y],"radius":r}],
          "closed": true}
```
Must form one closed loop (validator checks endpoint chaining within tol). Lives on `Edge.Cuts`.

## 4. Footprint (placed) & Pad

```json
Footprint: {
  "id":"fp1", "fpid":"Resistor_SMD:R_0603_1608Metric",
  "ref":"R1", "value":"10k",
  "pos":[x,y], "angle":0, "side":"top|bottom",
  "pads":[Pad, ...],
  "graphics":[Graphic, ...],               // silk/fab/courtyard in footprint-local mm
  "model_3d":"path or libref|null",        // STEP/WRL model for the 3D bridge
  "attr":"smd|tht|virtual",
  "linked_symbol":"s1"                      // back-link to schematic instance (annotation)
}

Pad: {
  "id":"p1", "number":"1",
  "pos":[x,y], "angle":0,                   // footprint-local
  "shape":"rect|roundrect|circle|oval|trapezoid|custom",
  "size":[w,h], "roundrect_ratio":0.25,
  "type":"smd|tht|np_tht|connector",
  "layers":["F.Cu","F.Mask","F.Paste"],     // smd: one side; tht: F.Cu,B.Cu + drill
  "drill":{"diameter":0.6,"shape":"round|oval","offset":[0,0]}|null,
  "net":"GND|null",                         // net name (from netlist)
  "pin":"3"                                 // schematic pin this pad maps to
}
```

Pad world position = footprint `pos` + rotate/mirror(side) of local `pos`. Helper in `schema.py`.

## 5. Track / Via / Zone

```json
Track: {"id":"t1","net":"GND","layer":"F.Cu","width":0.25,
        "kind":"segment","p1":[x,y],"p2":[x,y]}          // kind "arc": +center,a0,a1
Via:   {"id":"v1","net":"GND","at":[x,y],"drill":0.4,"diameter":0.8,
        "layers":["F.Cu","B.Cu"],"type":"through|blind|buried|micro"}
Zone:  {"id":"z1","net":"GND","layer":"F.Cu","polygon":[[x,y],...],
        "clearance":0.3,"min_width":0.25,"fill":"solid|hatch",
        "thermal":{"gap":0.3,"spoke_width":0.3},"filled_polys":[[...]]}   // filled_polys cached by compile
```

## 6. Design rules

```json
DesignRules: {
  "clearance": 0.2,                         // min copper-copper (mm)
  "track_width_min": 0.15,
  "via_diameter_min": 0.45, "via_drill_min": 0.2,
  "annular_ring_min": 0.13,
  "hole_to_hole_min": 0.25,
  "edge_clearance": 0.3,
  "silk_to_mask_min": 0.15
}
NetClass: {"clearance":0.2,"track_width":0.25,"via_diameter":0.8,"via_drill":0.4}
```

DRC (see `drc.py` in `ECAD_PLAN §3.3`) reports violations as
`{rule, severity, locs:[[x,y,layer]], items:[id,...], message}`; net-class values override globals
per net. Unrouted nets are reported via the **ratsnest** (board compile emits airwire segments
between the geometric nearest unconnected pads of each net).

## 7. Validation (`schema.py`)

- `validate_board(doc) -> []|[errors]`: schema tag; id uniqueness & prefixes (`fp/p/t/v/z/g`);
  every `pad.net`/`track.net`/`via.net`/`zone.net` either `null` or present in `nets`; layer ids in
  the stackup's valid set; outline closes; via layers subset of copper layers; smd pad has no drill,
  tht pad has drill.
- Net binding to schematic is advisory in the doc but enforced by the **board compile** (which
  re-derives expected nets from the linked netlist and flags mismatches as warnings, never rejects).

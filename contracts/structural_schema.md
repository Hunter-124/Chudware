# Contract: Structural Schema — v1

> The structural pillar's frozen document + tool surface. Doc kind **`aicad.structure/1`**. A
> structural document is a flat list of parametric structural elements (beams, columns, posts,
> joists, stud walls, footings) plus load cases; every element compiles to an OCCT B-rep solid
> (`engine.structural.brep.element_to_brep`) so it renders in the normal viewport alongside CAD
> bodies. Mirrors the ECAD precedent (`ecad_rpc.md` / `pcb_schema.md`): a self-contained engine
> module owns schema + geometry; the C++ host and MCP bridge call it directly.
>
> **Frozen first** because cost takeoff → BOM rollup reads element `material` +
> `section_name` specs; those field names are stable from here.

## 1. Document — `aicad.structure/1`

```jsonc
{
  "schema": "aicad.structure/1",
  "name": "Structure1",
  "units": "mm",                 // canonical mm internally (matches engine.doc / engine.ecad)
  "elements": [ <Element>, ... ],
  "load_cases": [ <LoadCase>, ... ]
}
```

`doc_kind(doc)` returns `"structure"` for such a doc; `is_structure(doc)` gates it. There is no
separate central Python doc-kind registry — a doc self-declares via its `schema` field, exactly as
`aicad.sch/1` / `aicad.pcb/1` do (the parametric doc engine lives in the C++ core now).

## 2. Element

Common fields on every element (`engine.structural.elements`):

| field | type | notes |
|---|---|---|
| `element_type` | str | one of `beam`, `column`, `post`, `joist`, `stud_wall`, `footing` |
| `id` | str | unique within the doc |
| `section_name` | str | section spec, parsed by `brep.section_profile` (§4) |
| `material` | str | e.g. `steel_a36`, `cedar`, `df_no2`, `concrete_25mpa` — a BOM/takeoff key |
| `length_mm` | float | extrusion length (member length; wall length; footing depth) |
| `transform` | [float]×16 | row-major 4×4 placement matrix (identity = local, extruded +Z) |

Per-type meaning of `length_mm` / `section_name`:

| element_type | section_name is… | length_mm is… |
|---|---|---|
| `beam`, `column`, `joist` | the cross-section (I-shape / RHS / rect) | member length |
| `post` | square/rect lumber section (e.g. `100x100`) | post height |
| `stud_wall` | `<stud_depth>x<wall_height>` rectangle | wall run length |
| `footing` | plan footprint `<W>x<L>` | footing depth (thickness) |

## 3. Load cases & analysis results

`LoadCase` (preset-driven; magnitudes in N, N/mm, or N/mm² as noted):

```jsonc
{ "id": "lc1", "kind": "dead|live|wind|snow|combo", "label": "D",
  "gravity": [0,0,-1], "factor": 1.0,
  "distributed": { "w_n_per_mm": 5.0 },   // optional UDL on line members
  "point": { "at_mm": 1200, "p_n": 1000 } // optional point load
}
```

`AnalysisResult` (returned by the analysis tool; one entry per element per combo):

| field | type | notes |
|---|---|---|
| `element_id` | str | |
| `axial_n` | float | member axial force (N; + tension) |
| `shear_n` | float | max shear (N) |
| `moment_nmm` | float | max bending moment (N·mm) |
| `deflection_mm` | float | max deflection (mm) |
| `dc_ratio` | float | demand/capacity (≤1.0 passes) |
| `pass` | bool | `dc_ratio <= 1.0` |

## 4. Section profiles — `section_profile(name) -> TopoDS_Wire`

Returns a closed OCCT wire centred on the local origin in the XY plane, describing the section's
OUTER boundary. `element_to_brep` builds the full section FACE — voided (outer-minus-inner) for
hollow forms — and extrudes it along +Z by `length_mm`, then applies `transform`. Parsed forms:

| form | example | profile |
|---|---|---|
| `WxH` (2 dims, mm) | `100x100`, `38x235` | solid rectangle (lumber / generic) |
| `RHS WxHxT` / `HSS WxHxT` / `WxHxT` | `RHS100x50x5`, `HSS100x100x6` | rectangular hollow → true outer-minus-inner prism, wall thickness `T` (falls back to a solid outer rectangle if `T` doesn't physically fit the envelope) |
| `CHS ODxT` / `PIPE ODxT` | `CHS114.3x6.02` | circular hollow (pipe/tube) → true annulus prism, wall thickness `T` |
| `I HxBxTWxTF` / AISC `W…` | `I200x100x6x10`, `W200x100` | I-shape wire (12-point; solid open-thin-wall profile, not voided) |

Unknown/empty names fall back to a sensible default rectangle for the element type so
`element_to_brep` always yields a valid non-null solid.

Note: `analysis.section_properties` (member stiffness / D-C-ratio path) still treats `RHS`/`HSS` as
a solid outer rectangle — voiding its `A`/`Iy`/`Iz`/`J` for hollow sections is a separate, unscoped
follow-up.

## 5. Tool signatures (MCP + worker RPC; day 3 registration)

| tool / method | params → result |
|---|---|
| `struct_new` | `{name?}` → `{doc}` (empty `aicad.structure/1`) |
| `struct_add_element` | `{element_type, section_name, material, length_mm, transform?}` → `{id, doc}` |
| `struct_to_brep` | `{element_id}` → `{step_path}` (compiled solid, for viewport/export) |
| `struct_set_loads` | `{load_cases:[LoadCase]}` → `{doc}` |
| `struct_analyze` | `{combo?}` → `{results:[AnalysisResult]}` |

Error envelope identical to `worker_rpc.md`: `{ "code", "message", "detail" }`.

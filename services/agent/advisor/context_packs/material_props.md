# Material properties context pack
# Source: engine_oracle/sim/materials.json (E_mpa, yield_mpa, density_g_cm3, alpha_per_C)
#         engine_oracle/research/data/feeds_speeds.json (SFM/chipload ranges per material)

## Common materials (materials.json)
| Material      | E [MPa]| Yield [MPa]| ρ [g/cm³]| α [×10⁻⁶/°C]|
|---------------|--------|------------|-----------|--------------|
| 6061-T6 Al    | 68900  | 276        | 2.70      | 23.6         |
| 304 SS        | 193000 | 310        | 7.93      | 17.2         |
| A36 steel     | 200000 | 250        | 7.85      | 11.7         |
| Ti-6Al-4V     | 114000 | 880        | 4.43      | 8.6          |
| ABS           | 2300   | 40         | 1.06      | 90.0         |
| PC            | 2400   | 62         | 1.20      | 65.0         |

## Machinability note (feeds_speeds.json)
- Aluminum: high SFM (300–600 SFM carbide), free-cutting
- 304 SS: work-hardens; low SFM (100–200 SFM), flood coolant
- Titanium: low SFM (80–150 SFM), sharp tools, aggressive coolant

## Tool: research_material_lookup for full properties; set_material to bind to doc

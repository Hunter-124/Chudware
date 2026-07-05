# Press fit context pack
# Source: engine_oracle/research/data/iso286.json (shaft deviations k/n/p/r/s tabulated,
#         ISO 286-1:2010 Table 9); fits.py _shaft_es_um(); fit_recommendations.json

## Callout format
- Designation: `H7/p6` (light press) | `H7/r6` (medium) | `H7/s6` (heavy)
- Drawing note: "PRESS FIT H7/p6" with interference range in notes block

## Interference ranges on Ø20 (from fit_calc, ISO 286-2:2010)
- H7/k6: transition  −0.006..+0.021 mm (may be clearance or press)
- H7/p6: light press  0.017..0.042 mm interference
- H7/s6: medium press 0.035..0.060 mm interference

## Assembly considerations
- Loctite 638 (retaining compound) allows h6 shaft for disassembly
- Thermal assembly: heat housing OR freeze shaft (ΔT ≈ interference / (α × D))
- Typical steel α ≈ 11.7×10⁻⁶ /°C (materials.json: steel_1020.alpha_per_C)

## Prerun calc: fit_calc — answer.clearance[0] < 0 confirms interference

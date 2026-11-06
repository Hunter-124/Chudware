# Press fit context pack
# Source: services/research/fits.py (_shaft_deviation_um and _HOLE_ROWS),
#         ISO 286-1:2010 Tables 4–5; iso286.json IT grades; fit_recommendations.json

## Callout format
- Designation: `H7/p6` (light press) | `H7/r6` (medium) | `H7/s6` (heavy)
- Drawing note: "PRESS FIT H7/p6" with interference range in notes block

## Interference ranges on Ø20 (from fit_calc, ISO 286-2:2010)
- H7/k6: transition clearance −0.015..+0.019 mm (may be clearance or press)
- H7/p6: light press 0.001..0.035 mm interference
- H7/s6: press 0.014..0.048 mm interference

## Assembly considerations
- Loctite 638 (retaining compound) allows h6 shaft for disassembly
- Thermal assembly: heat housing OR freeze shaft (ΔT ≈ interference / (α × D))
- Typical steel α ≈ 11.7×10⁻⁶ /°C (materials.json: steel_1020.alpha_per_C)

## Prerun calc: fit_calc — answer.clearance[0] < 0 confirms interference

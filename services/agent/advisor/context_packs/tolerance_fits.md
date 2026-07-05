# Tolerance and fit context pack
# Source: engine_oracle/research/data/iso286.json (IT grades, shaft deviations)
#         ISO 286-1:2010; ISO 286-2:2010; fit_recommendations.json

## Fit class guide (ISO 286, iso286.json + fit_recommendations.json)
| Application         | Fit     | Character   | Typ. clearance (Ø20) |
|---------------------|---------|-------------|----------------------|
| Free running        | H8/f7   | clearance   | +0.020..+0.074 mm    |
| Sliding / locating  | H7/g6   | clearance   | +0.007..+0.041 mm    |
| Close sliding       | H7/h6   | clearance   | 0..+0.021 mm         |
| Transition          | H7/k6   | transition  | −0.006..+0.021 mm    |
| Light press         | H7/p6   | press       | −0.042..−0.017 mm    |
| Medium press        | H7/s6   | press       | −0.060..−0.035 mm    |

Values from fit_calc(); verify with `fit_calc(nominal, "H7/g6")`.

## IT grade reference (iso286.json it_grades)
- IT6 ≈ 13 µm, IT7 ≈ 21 µm, IT8 ≈ 33 µm on Ø20 mm (closed-form formula)

## Callout format
- "Ø20 H7 (HOLE)" / "Ø20 g6 (SHAFT)" — always call out both sides
- Tolerances in ±0.XXX format on fabrication drawings

## Prerun calc: fit_calc — answer.clearance, answer.fit_character, answer.hole_absolute

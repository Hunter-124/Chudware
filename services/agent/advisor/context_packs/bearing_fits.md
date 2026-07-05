# Bearing seat fit context pack
# Source: engine_oracle/research/data/iso286.json (IT grades, shaft deviations)
#         ISO 286-1:2010; Shigley's Mechanical Engineering Design 10th ed. Table 11-1

## Callout format
- Shaft seat: `Ø<nom> k6` (light press) or `m6` (medium press) on the inner race
- Housing bore: `Ø<nom> H7` (standard) or `J7` (easy removal needed)
- Drawing note: "SHAFT SEAT Ø20 k6" / "HOUSING BORE Ø47 H7"

## Typical bearing fit classes (ISO 286, iso286.json)
- Rotating inner race (most common):
    shaft = k6 (light-to-medium press) or m6 (medium press)
    housing = H7 (stationary, slip-fit for removal)
- Rotating outer race:
    shaft = h6 (clearance) or js6 (transition)
    housing = N7 or M7 (light press)

## Rule: never use clearance fit on the rotating ring
## Prerun calc: fit_calc — use fit_calc(nominal, "H7/k6") to verify the band

# Thread / Tap context pack
# Source: services/research/data/feeds_speeds.json (tap_drill table, metric_coarse)
#         Machinery's Handbook 31st ed. §Tap Drill Sizes; ISO 2306; ASME B94.9

## Callout format
- Thread callout: `M<size>×<pitch>` (ISO metric) or `#<N>-<TPI> UNC/UNF` (unified)
- Tap drill = major_dia − pitch_mm (ISO 2306, 75% thread engagement)
- Drawing note: "TAP M6×1.0 THRU" or "TAP M6×1.0, 15 DEEP MIN"

## Common metric coarse pilot sizes (from feeds_speeds.json tap_drill.metric_coarse)
- M3×0.5  → pilot 2.5 mm
- M4×0.7  → pilot 3.3 mm
- M5×0.8  → pilot 4.2 mm
- M6×1.0  → pilot 5.0 mm
- M8×1.25 → pilot 6.8 mm
- M10×1.5 → pilot 8.5 mm
- M12×1.75→ pilot 10.2 mm

## Engagement depth rule (Machinery's Handbook 31st ed.)
- Steel-in-steel:    1.0 × D minimum
- Aluminum body:     1.5 × D minimum
- Cast iron / brass: 1.25 × D minimum
- Plastic body:      2.0 × D minimum (or inserts preferred)

## Prerun calc: tap_drill — answer.tap_drill_mm is the pilot drill diameter

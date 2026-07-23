# Fastener pattern context pack
# Source: services/research/data/feeds_speeds.json (tap_drill.metric_coarse);
#         ISO 273:1979 Clearance holes for bolts and screws (medium series)

## Clearance hole series (ISO 273 medium series)
- M3:  clearance 3.4 mm, countersink/bore to match head
- M4:  clearance 4.5 mm
- M5:  clearance 5.5 mm
- M6:  clearance 6.6 mm
- M8:  clearance 8.5 mm (pilot 6.8 mm if tapping; feeds_speeds.json metric_coarse)
- M10: clearance 10.5 mm (pilot 8.5 mm)
- M12: clearance 13.0 mm (pilot 10.2 mm)

## Edge distance and spacing (Shigley's §8 bolted joints)
- Edge distance ≥ 1.5 × clearance_hole_diameter from nearest edge
- Bolt spacing  ≥ 3.0 × clearance_hole_diameter centre-to-centre (minimum)
- PCD (bolt circle) sizing: PCD ≥ N × fastener_dia / (2 × sin(π/N)) + 2 × head_dia

## Callout format
- "4× M6 THRU, EQ SP ON Ø50 B.C."
- "6× CBORE Ø11 THRU, Ø18 × 6 DEEP (M10 SHCS)"

## Prerun calc: tap_drill — answer.tap_drill_mm for the tapped-hole side

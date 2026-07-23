# Thread engagement context pack
# Source: services/research/data/feeds_speeds.json (tap_drill table);
#         Machinery's Handbook 31st ed. §Screw Thread Systems — Engagement;
#         Shigley's Mechanical Engineering Design 10th ed. §8-2

## Minimum engagement depth by material combination
- Steel bolt in steel tapped hole:    L_min = 1.0 × D_nominal
- Steel bolt in aluminum:             L_min = 1.5 × D_nominal
- Steel bolt in cast iron / brass:    L_min = 1.25 × D_nominal
- Any bolt in plastic:                L_min = 2.0 × D_nominal (or use inserts)

## Boss geometry for tapped features
- Minimum boss OD ≥ 2.5 × D_tap  (boss wall ≥ 0.75 × D_tap each side)
- Blind tap: add 2–3 thread pitches beyond nominal engagement depth for run-out

## Callout format (drawing)
- "TAP M6×1.0, 15 DEEP MIN (DO NOT DRILL THROUGH)"
- "HELICOIL INSERT M6×1.0, 9 MM LONG" (for aluminum with high-cycle loads)

## Tool: research_thread_spec for full thread parameters; tap_drill for pilot size

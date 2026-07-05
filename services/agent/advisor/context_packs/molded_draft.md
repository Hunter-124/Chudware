# Molded-draft context pack
# Source: engine_oracle/research/calcs/draft_check.py
#         engine_oracle/dfm/process_profiles.py (injection profile: draft.min_deg=1.0)
#         Reference: Bryce, Plastic Injection Molding Vol. 2 (draft guidelines)

## Draft is MANDATORY for injection release — size it, don't skip it
- taper per side over a draw depth = draw_depth · tan(draft_angle)
- Call the draft_check calc — never recall "1 degree".

## Recommended draft by surface finish (draft_check.py _DRAFT_BY_FINISH_DEG)
- Smooth (SPI A/B polished):   1.0°   (process_profiles injection min_deg)
- Matte (SPI C):               1.5°
- Light texture (MoldTech):    3.0°
- Heavy texture / grain:       5.0°
- Rule of thumb: +1° per 0.025 mm of texture depth; +draft on deep ribs.

## Injection DFM minimums (process_profiles injection)
- min_wall_mm 1.0; uniform wall (ribs ≈ 60% of nominal wall to avoid sink)
- draft required=True, min_deg=1.0 on every draw face
- fit classes: slip 0.10, location 0.05, press 0.02, free 0.20 mm (diametral)
- shrink modeled as a global % (not a per-bore add); hole_comp 0

## Prerun calc: draft_check — answer.draft_deg, answer.offset_per_side_mm

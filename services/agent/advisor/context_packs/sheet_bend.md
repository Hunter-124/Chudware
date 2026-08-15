# Sheet-metal bend context pack
# Source: services/research/calcs/bend_allowance.py
#         core/dfm/process_profiles.cpp (sheet_metal profile)
#         Reference: Machinery's Handbook 29th ed. (Bending Sheet Metal);
#                    SME Die Design Handbook

## Flat-pattern rule (the one that saves scrap)
- flat length across ONE bend = leg_a + leg_b − bend_deduction (BD)
- BA  = θ·(π/180)·(r + K·t)          bend allowance (neutral-axis arc)
- BD  = 2·tan(θ/2)·(r + t) − BA      bend deduction
- Call the bend_allowance calc — never recall a flat length.

## K-factor by r/t ratio (bend_allowance.py _KFACTOR_BY_RT)
- r/t ≤ 0.5:  K ≈ 0.33   (tight bend, neutral axis near inside face)
- r/t ≤ 1.0:  K ≈ 0.35
- r/t ≤ 2.0:  K ≈ 0.40
- r/t ≤ 3.0:  K ≈ 0.43
- r/t  > 3.0: K ≈ 0.45   (generous radius, neutral axis near mid-thickness)

## Sheet-metal DFM minimums (process_profiles sheet_metal)
- min wall = gauge (the sheet thickness IS the wall); min_wall_mm 1.0
- inside bend radius ≥ material thickness (r ≥ t) to avoid cracking
- hole/edge to bend ≥ 2.5×t + r; hole diameter ≥ t
- tolerance band ≈ 0.10 mm; holes punched to size (hole_comp 0)

## Prerun calc: bend_allowance — answer.bend_allowance_mm, answer.bend_deduction_mm, answer.k_factor

# Snap-fit context pack
# Source: services/research/calcs/snap_fit.py;
#         services/sim/materials.json (E_mpa, yield_mpa per material_id)
#         Reference: Bayer Design Guide "Snap-Fit Joints for Plastics" (1998);
#                    Shigley's §4-1 cantilever deflection

## Key formula (cantilever, rectangular cross-section)
- Root strain: ε = 1.5 × h × δ / L²
- Allowable:   ε_allow = safety_factor × (yield_mpa / E_mpa)
- Default safety_factor = 0.6  (Bayer Guide: use 50–70% of yield strain)
- Snap is viable if ε_root ≤ ε_allow; margin_pct = (ε_allow/ε_root − 1) × 100

## Geometry variables
- L: arm length [mm] (longer arm → lower strain)
- h: arm thickness at root [mm] (thinner → lower strain but weaker)
- b: arm width [mm]
- δ: tip deflection = undercut depth [mm]

## Material yield strains from materials.json (E_mpa, yield_mpa)
- ABS:  E=2300 MPa, yield=40 MPa  → ε_y ≈ 0.017 (1.7%)
- PC:   E=2400 MPa, yield=62 MPa  → ε_y ≈ 0.026 (2.6%)
- POM:  E=3100 MPa, yield=70 MPa  → ε_y ≈ 0.023 (2.3%)
- Nylon66: E=2800 MPa, yield=83 MPa → ε_y ≈ 0.030 (3.0%)

## Prerun calc: snap_fit — answer.viable=True/False, answer.margin_pct

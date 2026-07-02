# Material properties (typical values, for early sizing)

Density ρ, Young's modulus E, yield strength σy — enough to run a first-pass mass/FEA check and
pick a sane material for the manufacturing process. These are TYPICAL published values for
generic stock, not a certified datasheet — use them for early sizing and FEA material entry, and
flag to the user when a real cert/datasheet matters (aerospace, pressure, safety-critical).

## 1. Property table

| Material | Density (g/cm³) | E (GPa) | Yield σy (MPa) | Selection guidance |
|---|---|---|---|---|
| Mild steel (1018) | 7.87 | 200 | 370 | Default "steel" when the user just says steel; weldable, cheap, machines well. |
| 6061-T6 aluminum | 2.70 | 69 | 276 | Default lightweight structural metal; good machinability, corrosion-resistant, weldable (loses temper near weld). |
| 304 stainless | 8.00 | 193 | 215 | Corrosion/hygiene/food-contact parts; work-hardens, harder to machine than mild steel, non-magnetic. |
| Brass (C360) | 8.50 | 97 | 310 | Bearings/bushings, decorative or electrical-contact parts, low-friction sliding, easy machining. |
| ABS | 1.05 | 2.3 | 40 | Tough printed/molded housings, snap-fits, higher temp resistance than PLA; can warp on large FDM prints. |
| PLA | 1.24 | 3.5 | 50 | Default FDM prototyping material — easiest to print, rigid, but brittle and low heat resistance (~55 °C softening). |
| PETG | 1.27 | 2.1 | 50 | FDM parts needing more toughness/outdoor-UV/moisture resistance than PLA with easier printing than ABS. |
| Nylon (PA12) | 1.01 | 1.7 | 48 | Wear-resistant, flexible/tough printed parts (living hinges, gears); absorbs moisture — dry before printing. |

## 2. Notes on use

- **FEA material entry**: plug ρ/E/σy directly into `run_fea_static`'s material fields for a
  first-pass stress/deflection check; treat results as directional until a real material cert is
  supplied for a load-bearing or safety-critical part.
- **Mass estimates**: density × solid volume from `measure_part` gives a fast weight check before
  committing to a process (e.g. is this small enough to print, light enough to ship).
- **Printed-material anisotropy**: PLA/ABS/PETG/nylon values above are for well-bonded, standard
  print orientation loaded in-plane; interlayer (Z-axis) strength is typically 50–70% of the
  in-plane values above — orient load-bearing prints so the primary stress runs in-plane, not
  through layers.
- **Yield vs. ultimate**: printed-polymer "yield" above is closer to an onset-of-permanent-
  deformation number; brittle polymers (PLA) may fracture near or before this value rather than
  yielding plastically — don't assume ductile margin the way you would for the metals.
- Default the material to PLA for a generic "3D print this" request (matches the FDM/PLA default
  in `docs/INTAKE_MEASUREMENT_DFM_PLAN.md`'s process profile) unless the user specifies otherwise.

Sources: MatWeb typical/generic material datasheets (steel, aluminum 6061-T6, 304 stainless,
brass C360, ABS/PLA/PETG/PA12), consistent with the material table already in `hardware.md` §7.

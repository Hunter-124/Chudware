# Limits & fits (ISO 286)

Hole-basis system: the HOLE is always the nominal size at grade H; the SHAFT tolerance grade
picks the fit. Use this to size two mating diameters (shaft into bore/hole) so they actually
work as the user intends — slide, locate, or lock together.

## 1. Common fit classes — clearance/interference by nominal size (mm, machined parts)

Values are the ISO 286 tolerance-zone range for the size bracket (min…max clearance is positive
= gap, negative = interference i.e. press). Pick the row matching your nominal Ø, then add that
range as the shaft's deviation from the hole's basic size.

| Fit (Hole/Shaft) | Intent | 3–6 mm | 6–10 mm | 10–18 mm | 18–30 mm | 30–50 mm |
|---|---|---|---|---|---|---|
| H11/c11 | loose running (dirty, high tol) | +0.070…+0.145 | +0.080…+0.170 | +0.095…+0.205 | +0.110…+0.240 | +0.130…+0.280 |
| H9/d9 | free running, no accuracy needed | +0.030…+0.078 | +0.040…+0.098 | +0.050…+0.120 | +0.065…+0.149 | +0.080…+0.180 |
| H7/g6 | sliding / accurate location | +0.004…+0.016 | +0.005…+0.020 | +0.006…+0.024 | +0.007…+0.028 | +0.009…+0.034 |
| H7/k6 | transition — light tap to seat, locates | −0.009…+0.010 | −0.010…+0.013 | −0.012…+0.016 | −0.015…+0.019 | −0.018…+0.023 |
| H7/p6 | press — light interference, needs press/heat | −0.020…−0.008 | −0.024…−0.009 | −0.029…−0.011 | −0.035…−0.014 | −0.042…−0.017 |
| H7/s6 | heavy press — permanent, high interference | −0.027…−0.015 | −0.032…−0.017 | −0.039…−0.021 | −0.048…−0.027 | −0.059…−0.034 |

- Positive value = shaft is smaller than hole (a gap you can feel/slide); negative = shaft is
  larger than hole (interference — needs force or a temperature differential to assemble).
- H7/g6 is the default "this pin should locate and turn/slide freely" choice.
- H7/k6 is the default "this should seat with a firm tap and stay put without glue" choice
  (matches the bearing-shaft guidance in `bearings.md`).
- H7/p6 and H7/s6 assume rigid isotropic metal; do not use on plastic or printed parts (creep
  relaxes interference over weeks — see §2).

## 2. FDM-printing guidance (this is what the modeler should actually do)

Printed parts are NOT dimensionally exact like machined ones (extrusion width, first-layer
squish, cooling shrink all add up), so use looser numbers than the ISO table above:

| Intended fit | Machined equivalent | Printed clearance to add (mm, per side of Ø) | Notes |
|---|---|---|---|
| Free/slip (rotates or slides) | H9/d9 | +0.30…+0.50 total on Ø | Test-print one coupon before committing the full part. |
| Located/registers, light friction | H7/g6→H7/k6 | +0.15…+0.30 total on Ø | Good default for pins, dowels, alignment bosses. |
| "Press" fit | H7/p6+ | **do not model interference** | See below — use inserts/fasteners instead. |
| Snap/friction fit (small features) | — | +0.10…+0.20 on the mating wall | Only for features < ~10 mm; add a 0.5×45° lead-in chamfer. |

- **Never model a true interference (negative-clearance) fit for FDM plastic.** Layer adhesion
  is weaker than the bulk polymer, elastic/plastic creep relaxes any interference within days to
  weeks, and the print tolerance band (±0.2–0.3 mm typical FDM) is often larger than the
  interference you'd want anyway. This matches the process profile in
  `core/dfm/process_profiles.cpp` (`hole_comp_mm ≈ 0.20`, press fits steered to
  inserts rather than interference).
  - **Permanent joins**: use heat-set threaded inserts (see `bearings.md`/`hardware.md` insert
    table) or a fastener, not a press fit.
  - **Bearings**: press the bearing into a metal sleeve/washer boss, or oversize the pocket
    slightly and retain with a shoulder + snap ring / screws rather than relying on plastic
    interference to hold it (see `bearings.md`).
- Vertical (Z) holes print undersize (bridging/sag) more than horizontal ones — favor the high
  end of the clearance range for a hole printed with its axis along Z.
- Calibrate per printer/material when precision matters; treat the numbers above as a starting
  point, not a guarantee, and note the assumption in the design if fit is critical.

Sources: ISO 286-2:2010 (fundamental tolerances/deviations, hole-basis system) and
general FDM process guidance consistent with `core/dfm/process_profiles.cpp`.

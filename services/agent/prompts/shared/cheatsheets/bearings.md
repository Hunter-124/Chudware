# Common bearing dimensions & fit guidance

Deep-groove ball bearings, the parts you'll actually be asked to mount. Use the bore/OD/width to
size the shaft and housing pocket; use the fit guidance to decide which ring (inner/outer) gets
the tight tolerance.

## 1. Bore × OD × width (mm)

| Bearing | Bore | OD | Width | Typical use |
|---|---|---|---|---|
| 608 | 8 | 22 | 7 | skateboard/fidget bearing, small wheels, low-cost rotary joints |
| 625 | 5 | 16 | 5 | small pulleys, RC parts |
| 688 | 8 | 16 | 4 | slim/thin-section, tight radial space |
| 6000 | 10 | 26 | 8 | small shafts, light-duty spindles |
| 6001 | 12 | 28 | 8 | NEMA17-adjacent shafts, light gearboxes |
| 6002 | 15 | 32 | 9 | medium shafts |
| 6003 | 17 | 35 | 10 | common motor-shaft size |
| 6004 | 20 | 42 | 12 | larger spindles, wheel hubs |
| 6005 | 25 | 47 | 12 | heavier wheel hubs, larger spindles |

## 2. Shaft / housing fit — which ring rotates decides which gets the tight tolerance

The ring that ROTATES relative to the load needs the tighter (interference-leaning) fit so it
can't creep/spin in its seat; the STATIONARY ring gets a looser fit so it can be assembled and
adjusted without a press.

| Condition | Inner ring (shaft) fit | Outer ring (housing) fit |
|---|---|---|
| Rotating shaft, stationary housing (most common: motor shaft, wheel axle) | **k6** (light interference/transition, locates the inner race) | **H7** (slip fit, housing stays still, allows disassembly) |
| Stationary shaft, rotating housing (idler pulley on a fixed pin) | **h6/g6** (slip, shaft stays still) | **K7/M7** (light interference, housing race must not spin) |
| Light/uncertain load, want easy service | **h6** shaft, **H7** housing (both slip) | shoulder + retaining ring/clip to prevent axial walk |

- Shaft Ø = bearing bore exactly, machined/toleranced to k6 for the rotating case (see `fits.md`
  H7/k6 row for the numeric deviation range at your bore size).
- Housing bore Ø = bearing OD exactly, H7 for the stationary case.
- Always add a shoulder (or retaining ring / snap-ring groove) on the axial side that takes thrust
  — a fit alone does not locate the bearing axially.

## 3. Printed-housing guidance (FDM)

- **Do not rely on plastic interference to retain a bearing.** Creep will let it walk loose within
  weeks even at a nominal "press" dimension. Instead:
  - Print the housing bore at OD + 0.10…0.20 mm (a light slip fit, per `fits.md` §2) and retain the
    bearing axially with a printed shoulder on one side + a screw-on cap or snap ring on the other.
  - For the shaft side, if the shaft itself is printed plastic, undersize it slightly less than the
    ISO k6 press number would suggest — use a slip/registers fit (shaft = bore − 0.05…0.10 mm) and
    add a shoulder or a set screw/retaining ring to prevent axial and rotational slip, since printed
    plastic will not hold a metal-appropriate interference.
  - Prefer a METAL shaft (steel dowel pin, per `hardware.md` §5) through a printed housing over an
    all-printed shaft when the bearing must actually rotate under load — printed shafts wear and
    go out-of-round faster than the bearing's rated life.
- Vent any blind pocket around the bearing OD so trapped air doesn't blow out the first layers
  during printing or resist seating during assembly.

Sources: SKF/common ball-bearing catalog dimensions (608/6000-series/625/688 bore-OD-width), SKF
general fit-selection guidance (rotating ring = tight fit), ISO 286 shaft/hole grades referenced
from `fits.md`.

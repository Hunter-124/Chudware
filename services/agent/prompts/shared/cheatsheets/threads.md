# Thread dimensions (ISO metric coarse + UNC)

Use to pick a tap-drill (cut female threads), a clearance hole (bolt passes through freely), or a
minor/root diameter (for a modeled thread profile or thread-relief check).

## 1. ISO metric coarse (ISO 261 / ISO 262), M2–M12

All dims in mm. Tap-drill uses the "~75% thread engagement" convention (nominal − pitch).
Clearance is the "normal" (medium) fit — use the tighter value for a locating bolt, looser for a
loose slot.

| Size | Pitch | Tap drill Ø | Minor Ø (internal, ≈ D−1.0825p) | Clearance drill Ø (close/normal/loose) |
|---|---|---|---|---|
| M2 | 0.40 | 1.60 | 1.567 | 2.2 / 2.4 / 2.6 |
| M2.5 | 0.45 | 2.05 | 2.013 | 2.7 / 2.9 / 3.1 |
| M3 | 0.50 | 2.50 | 2.459 | 3.2 / 3.4 / 3.6 |
| M4 | 0.70 | 3.30 | 3.242 | 4.3 / 4.5 / 4.8 |
| M5 | 0.80 | 4.20 | 4.134 | 5.3 / 5.5 / 5.8 |
| M6 | 1.00 | 5.00 | 4.917 | 6.4 / 6.6 / 7.0 |
| M8 | 1.25 | 6.80 | 6.647 | 8.4 / 9.0 / 10.0 |
| M10 | 1.50 | 8.50 | 8.376 | 10.5 / 11.0 / 12.0 |
| M12 | 1.75 | 10.20 | 10.106 | 13.0 / 13.5 / 14.5 |

## 2. UNC (Unified Coarse, ANSI/ASME B1.1), #4–3/8"

Tap drill is the nearest standard/number drill; clearance is the "normal" ANSI free-fit hole.

| Size | Major Ø (in / mm) | TPI | Tap drill | Minor Ø (in) | Clearance drill (in / mm) |
|---|---|---|---|---|---|
| #4 | 0.112 / 2.84 | 40 | #43 (0.089) | 0.0844 | #32 (0.116) / 2.95 |
| #6 | 0.138 / 3.51 | 32 | #36 (0.106) | 0.1064 | #27 (0.144) / 3.66 |
| #8 | 0.164 / 4.17 | 32 | #29 (0.136) | 0.1332 | #18 (0.170) / 4.31 |
| #10 | 0.190 / 4.83 | 24 | #25 (0.150) | 0.1512 | #9 (0.196) / 4.98 |
| 1/4" | 0.250 / 6.35 | 20 | #7 (0.201) | 0.2019 | 17/64 (0.266) / 6.75 |
| 5/16" | 0.3125 / 7.94 | 18 | F (0.257) | 0.2614 | 21/64 (0.328) / 8.33 |
| 3/8" | 0.375 / 9.53 | 16 | 5/16 (0.3125) | 0.3141 | 25/64 (0.391) / 9.92 |

- Convert to mm for a metric-unit model: multiply the "in" column by 25.4.
- Rough M↔inch equivalence for quick substitution: M2≈#4-40 wt, M3≈#4/#6, M4≈#8, M5≈#10,
  M6≈1/4", M8≈5/16", M10≈3/8" — dimensions differ slightly, don't mix real hardware across systems.

## 3. Printed-thread guidance

- **Do not model a cut/tapped thread below M6 (or #10-24) for FDM** — the root diameter and pitch
  get lossy at small sizes (layer height vs. thread pitch), and the plastic thread strips easily
  under repeated use.
- **M6 and larger**: printing the thread directly is viable for low-cycle, hand-tight use; oversize
  the tap-drill by the FDM hole compensation (see `fits.md` §2, ≈ +0.2 mm on the nominal tap-drill
  Ø) since printed holes come out undersize.
- **Below M6, or any load-bearing/reusable joint**: use a heat-set threaded insert instead of a
  printed thread (boss hole Ø + depth per size — see `bearings.md`/`hardware.md`), or a self-tapping
  screw straight into an undersized printed pilot hole (pilot ≈ minor Ø, no relief needed for
  infrequent assembly).
- A printed EXTERNAL thread (bolt/screw shape) mates better with a heat-set nut or a tapped metal
  part than with a printed internal thread — printed-on-printed threads bind from combined
  tolerance stack-up; loosen the fit by ~0.1–0.2 mm on the pitch diameter if both halves are printed.

Sources: ISO 261 / ISO 262 (ISO general-purpose metric screw threads, basic profile & tap-drill
convention), ANSI/ASME B1.1 (Unified inch screw threads, UNC series), standard number/letter
drill-size tables.

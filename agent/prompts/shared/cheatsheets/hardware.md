# Standard hardware catalog

Real published dimensions for common off-the-shelf hardware, so the planner/modeler commit to
pockets, bosses, clearance holes and counterbores that **actually fit** named parts instead of
guessing. All values **millimetres** unless an inch size is named. This is the engineer's
reference; the parametric parts library (`parts_make` RPC, `core/parts`) generates real geometry
from the SAME numbers, and the vendor-parts registry (`engine/vendor_parts.py`) can cache supplier
CAD by part number. See the import seam at the bottom.

> Rule of thumb when a value isn't listed: clearance-hole Ø ≈ nominal + (0.4 small … 1.0 large);
> tap-drill Ø ≈ nominal − pitch; counterbore Ø ≈ head Ø + 0.5; min wall around a hole ≥ 1.5×
> (metric) the hole-edge-to-part-edge. Prefer a real listed size over a guess.

---

## 1. Metric fasteners — coarse thread (ISO)

Head types: **SHCS** socket head cap (ISO 4762 / DIN 912), **BHCS** button head (ISO 7380),
**FHCS** flat/countersunk 90° (ISO 10642 / DIN 7991), **HHCS** hex head (ISO 4017/4014).

| Size | Pitch | Tap-drill Ø | Clear Ø (close / normal / loose) | SHCS head Ø×H | SHCS hex key | BHCS head Ø×H | FHCS head Ø (90°) | Hex head A/F |
|------|-------|-------------|----------------------------------|---------------|--------------|---------------|-------------------|--------------|
| M1.6 | 0.35  | 1.25 | 1.7 / 1.8 / 2.0   | 3.0 × 1.6  | 1.5 | 3.0 × 1.0  | 3.6  | 3.2  |
| M2   | 0.40  | 1.60 | 2.2 / 2.4 / 2.6   | 3.8 × 2.0  | 1.5 | 3.8 × 1.3  | 4.4  | 4.0  |
| M2.5 | 0.45  | 2.05 | 2.7 / 2.9 / 3.1   | 4.5 × 2.5  | 2.0 | 4.7 × 1.5  | 5.5  | 5.0  |
| M3   | 0.50  | 2.50 | 3.2 / 3.4 / 3.6   | 5.5 × 3.0  | 2.5 | 5.7 × 1.65 | 6.3  | 5.5  |
| M4   | 0.70  | 3.30 | 4.3 / 4.5 / 4.8   | 7.0 × 4.0  | 3.0 | 7.6 × 2.2  | 8.4  | 7.0  |
| M5   | 0.80  | 4.20 | 5.3 / 5.5 / 5.8   | 8.5 × 5.0  | 4.0 | 9.5 × 2.75 | 10.4 | 8.0  |
| M6   | 1.00  | 5.00 | 6.4 / 6.6 / 7.0   | 10.0 × 6.0 | 5.0 | 10.5 × 3.3 | 12.6 | 10.0 |
| M8   | 1.25  | 6.80 | 8.4 / 9.0 / 10.0  | 13.0 × 8.0 | 6.0 | 14.0 × 4.4 | 17.3 | 13.0 |
| M10  | 1.50  | 8.50 | 10.5 / 11.0 / 12.0| 16.0 × 10.0| 8.0 | 17.5 × 5.5 | 20.0 | 16.0 |
| M12  | 1.75  | 10.20| 13.0 / 13.5 / 14.5| 18.0 × 12.0| 10.0| 21.0 × 6.6 | 24.0 | 18.0 |

- Counterbore for a SHCS = head Ø + 0.5 mm, depth = head H + 0.2…0.5 (flush) or + length to recess.
- Countersink for FHCS: cut an 90° cone to the FHCS head Ø.
- Fine pitch (when named "M8×1.0" etc.): use the named pitch; tap-drill = nominal − pitch.
- Common lengths (mm): 4,5,6,8,10,12,16,20,25,30,35,40,45,50,55,60,65,70,80,90,100. Length is
  under-head for SHCS/BHCS/HHCS, **overall** for FHCS (head sinks flush).

## 2. Imperial / SAE fasteners (Unified UNC coarse / UNF fine)

| Size  | Major Ø (in / mm) | UNC TPI | UNC tap-drill | UNF TPI | UNF tap-drill | Clear close (in/mm) | Clear free (in/mm) | SHCS head Ø×H (in) | SHCS hex key |
|-------|-------------------|---------|---------------|---------|---------------|---------------------|--------------------|--------------------|--------------|
| #2    | 0.086 / 2.18 | 56 | #50 (1.78) | 64 | #50 (1.78) | #43 (0.089/2.26) | #41 (0.096/2.44) | 0.140 × 0.086 | 5/64 |
| #4    | 0.112 / 2.84 | 40 | #43 (2.26) | 48 | #42 (2.37) | #32 (0.116/2.95) | #30 (0.128/3.26) | 0.183 × 0.112 | 3/32 |
| #6    | 0.138 / 3.51 | 32 | #36 (2.71) | 40 | #33 (2.87) | #27 (0.144/3.66) | #18 (0.170/4.31) | 0.226 × 0.138 | 7/64 |
| #8    | 0.164 / 4.17 | 32 | #29 (3.45) | 36 | #29 (3.45) | #18 (0.170/4.31) | #9 (0.196/4.98)  | 0.270 × 0.164 | 9/64 |
| #10   | 0.190 / 4.83 | 24 | #25 (3.80) | 32 | #21 (4.04) | #9 (0.196/4.98)  | #2 (0.221/5.61)  | 0.312 × 0.190 | 5/32 |
| 1/4   | 0.250 / 6.35 | 20 | #7 (5.11)  | 28 | #3 (5.41)  | 17/64 (0.266/6.75) | 9/32 (0.281/7.14) | 0.375 × 0.250 | 3/16 |
| 5/16  | 0.3125/ 7.94 | 18 | F (6.53)   | 24 | I (6.91)   | 21/64 (0.328/8.33) | 11/32 (0.344/8.73)| 0.469 × 0.312 | 1/4  |
| 3/8   | 0.375 / 9.53 | 16 | 5/16(7.94) | 24 | Q (8.43)   | 25/64 (0.391/9.92) | 13/32 (0.406/10.3)| 0.562 × 0.375 | 5/16 |
| 1/2   | 0.500 /12.70 | 13 | 27/64(10.7)| 20 | 29/64(11.5)| 33/64 (0.516/13.1) | 17/32 (0.531/13.5)| 0.750 × 0.500 | 3/8  |

- Number-size major Ø formula: Ø(in) = 0.060 + 0.013 × number (e.g. #6 = 0.060+0.078 = 0.138").
- 1 inch = 25.4 mm. Convert to mm for the model unless the user works in inches.

## 3. Nuts & washers

**Hex nuts** (width across flats A/F = the hex-head A/F above), thickness ≈ 0.8×nominal:
M3 A/F 5.5 × t 2.4; M4 7.0 × 3.2; M5 8.0 × 4.0; M6 10.0 × 5.0; M8 13.0 × 6.5; M10 16.0 × 8.0;
M12 18.0 × 10.0. **Nylock** (ISO 10511): add ~40% to thickness for the nylon collar.

**Flat washers** (ISO 7089, ID / OD / thick): M3 3.2/7/0.5; M4 4.3/9/0.8; M5 5.3/10/1.0;
M6 6.4/12/1.6; M8 8.4/16/1.6; M10 10.5/20/2.0; M12 13/24/2.5. **Split lock** ≈ same ID/OD, t≈nominal/4.

---

## 4. Clearance & tolerance by manufacturing process

Pick the hole oversize and tolerance from how the part is MADE. "Oversize" is added to the
nominal clearance-hole Ø from §1.

| Process | Typical linear tol | Hole position tol | Extra oversize on a clearance hole | Notes |
|---------|--------------------|-------------------|------------------------------------|-------|
| CNC milled / turned | ±0.05 (±0.025 reamed) | ±0.05 | 0 (use §1 close fit) | Datum off finished faces. |
| Drilled (hand/press) | ±0.1 | ±0.2 | +0.1…0.2 | Drill wanders; use normal/loose fit. |
| Reamed / bored | ±0.01…0.025 | ±0.02 | 0 | For dowel/bearing press & slip fits. |
| Laser cut (sheet) | ±0.1 | ±0.1 | +0.1, kerf ~0.1–0.2 | Slight taper; size to mid-thickness. |
| Waterjet | ±0.1…0.2 | ±0.15 | +0.15, taper on thick stock | Lead-in/taper on >10 mm. |
| Plasma | ±0.4…0.8 | ±0.5 | +0.5 | Bevel edge; rough. |
| Punched (sheet) | ±0.1 | ±0.15 | +0.1; min hole Ø ≥ material t | Die-side breakout. |
| FDM 3D print | ±0.3…0.5, ±0.2/mm shrink | — | +0.3…0.5 (holes shrink) | Model vertical holes oversize; bridges sag. |
| SLA / resin | ±0.1…0.2 | — | +0.1…0.2 | Cure shrink; thin features curl. |
| SLS nylon | ±0.2…0.3 | — | +0.3 | Powder in blind holes; vent them. |
| Injection mould | ±0.05…0.1 + 0.4–2% shrink | — | per shrink | Add draft (§7); avoid thick sections (sink). |
| Sheet-metal bend | ±0.2 over a bend | — | — | Hole ≥ 1.5×t from a bend or it deforms. |

**ISO limit fits** (hole-basis H, common shaft tolerances), for designed mating Ø:
- **Loose clearance** H11/c11 — wide running, dirty service.
- **Free running** H9/d9 — general clearance, no tight accuracy.
- **Close running / location** H8/f7 — accurate location, free assembly.
- **Sliding / location** H7/g6 — slides freely, small clearance (locating pins, shafts).
- **Locational transition** H7/k6 — accurate location, light tap to seat.
- **Press / interference** H7/p6 (light) … H7/s6 (heavy) — needs press/heat; bearings, dowels.
- Quick numbers (radial): slip fit 0.025–0.075 clearance; close slip 0.01–0.03; light press
  0.01–0.03 interference; heavy press 0.03–0.08. Bearing on shaft = H7/k6; bearing in housing = H7/N7→ light press.

---

## 5. Bearings, inserts, dowels, retaining

**Deep-groove ball bearings** (bore × OD × width, mm):
623 3×10×4 · 624 4×13×5 · 625 5×16×5 · 626 6×19×6 · 608 8×22×7 (skate) · 6000 10×26×8 ·
6001 12×28×8 · 6002 15×32×9 · 6003 17×35×10 · 6004 20×42×12 · 6800 10×19×5 · 6801 12×21×5 ·
6802 15×24×5 · 6900 10×22×6 · 6901 12×24×6. Housing bore = OD slip-to-light-press (H7); shaft = bore k6.

**Heat-set / press-fit threaded inserts** (recommended boss **hole** Ø × min depth, brass):
M2 → 3.2 × 4 · M2.5 → 3.5 × 5 · M3 → 4.0 × 6 (insert OD ~4.6) · M4 → 5.6 × 8 (OD ~5.9) ·
M5 → 6.4 × 10 (OD ~6.4) · M6 → 8.1 × 12. Boss OD ≥ hole + 2×wall (≥1.5 wall). Taper the mouth 0.5×45°.

**Dowel pins** (h6, press into reamed hole): Ø 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10. Hole = nominal H7
(slip) or m6 (press). **Set screws** (grub, ISO 4029) hex key: M3→1.5, M4→2, M5→2.5, M6→3, M8→4.

**Retaining rings** (external E-clip groove Ø × width) for shaft Ø: 3→2.7/0.4 · 4→3.7/0.4 ·
5→4.7/0.6 · 6→5.6/0.7 · 8→7.4/0.9 · 10→9.6/1.1 · 12→11.5/1.1.

**Round / hex standoffs (M/F & F/F)**: common OD round 4.5/5/6/8; hex A/F 5/5.5/7/8; thread M2.5/M3/M4.
Common lengths 6,8,10,12,15,20,25,30,35,40 mm. PCB standoff default M2.5 or M3, 6–11 mm.

---

## 6. Profiles, actuators, electronics

**Aluminium T-slot extrusion** (profile, slot width, default screw, T-nut):
2020 20×20, slot 6, M5 T-nut · 2040 20×40 · 3030 30×30, slot 8, M6 · 4040 40×40, slot 8, M8 ·
1515 (15×15, slot ~6.2, M3). Bolt through with a button-head + drop-in/roll-in T-nut.

**Servos** (Body L×W×H, mounting tab span, mount screw):
- **SG90 / MG90S micro**: body 22.8 × 12.0 × 22.5; flange span 32.2 across tabs, ~2.5 thick at ~16 up
  the body; 2× M2 holes (clear Ø 2.2) 4.6 in from each tab end; output boss one end, allow ⌀6 above
  for the horn. MOUNT: cut a 23.0 × 12.2 pocket + the two flange holes; flange rests on the wall.
- **MG996R standard**: body 40.7 × 19.7 × 42.9; flange span 49.5; 4× M3 (clear 3.2), boss for ⌀7 horn.
- **DS3218 / DS3225 (1/4-scale)**: body 40 × 20 × 40.5; flange span 49.5; 4× M3; high-torque.

**Stepper motors** (NEMA frame = body sq mm; mount square; screw; pilot boss; shaft):
- **NEMA17**: 42.3 sq, mount square 31.0, 4× M3, pilot ⌀22 × 2, shaft ⌀5 (length 20–24).
- **NEMA23**: 57.0 sq, mount square 47.14, 4× M5/⌀5.2, pilot ⌀38.1, shaft ⌀6.35.
- **DC gearmotors**: N20 ⌀12 body, 3 mm D-shaft, 2× M2 face; 37D ⌀37 body, 6 mm D-shaft, faceplate 2× M3.

**Electronics mount patterns** (hole pattern, screw):
- **Arduino Uno**: ~68.6 × 53.3 footprint, M3 holes (irregular); **Mega** 101.6 × 53.3.
- **Raspberry Pi 4/5**: 85 × 56 board, 4× M2.5 on 58 × 49 rectangle, 3.5 in from edges.
- **PCB mount hole** default M2.5 or M3 clear; standoff 5–11 mm; keep-out ≥ 3 mm around a hole.
- **18650 cell** ⌀18.4 × 65.2; **LiPo** model from stated L×W×H; **GT2 belt** 2 mm pitch, 6/9 mm wide,
  pulley bore ⌀5/⌀8, 20T pulley ⌀12.7 pitch. **O-ring gland**: groove width ≈ 1.4×cord, depth ≈ 0.75×cord.

---

## 7. Materials (for mass, FEA, fits)

Density ρ (g/cm³) · yield σy (MPa) · Young's E (GPa):
- **Al 6061-T6** 2.70 · 276 · 69 — default light structure.
- **Al 7075-T6** 2.81 · 503 · 71 — high-strength aero.
- **Steel 1018 (mild)** 7.87 · 370 · 200 · **4140** 7.85 · 655 · 205 · **SS 304** 8.00 · 215 · 193.
- **Brass C360** 8.50 · 310 · 97 · **Titanium Ti-6Al-4V** 4.43 · 880 · 114.
- **ABS** 1.05 · 40 · 2.3 · **PLA** 1.24 · 50 · 3.5 · **PETG** 1.27 · 50 · 2.1 · **Nylon (PA12)** 1.01 · 48 · 1.7 ·
  **Delrin/POM** 1.41 · 65 · 3.1 · **PC** 1.20 · 62 · 2.3.

## 8. Engineering rules of thumb

- **Min wall**: metal ≥ 1.0 (Al), plastic moulded 1.0–3.0 (uniform!), FDM ≥ 2 perimeters (~0.8–1.2).
- **Draft** for moulded/cast walls: ≥ 1° per side (2–3° textured); none needed for machined/printed.
- **Fillet** internal corners r ≥ 0.5×wall (cast/moulded ≥ wall); fillet to relieve stress, chamfer to assemble.
- **Thread engagement length** ≥ 1×Ø in steel, ≥ 1.5×Ø in aluminium, ≥ 2×Ø in plastic/soft.
- **Edge distance** for a hole ≥ 1.5×Ø to the part edge (2×Ø preferred); hole-to-hole ≥ 2×Ø.
- **Sheet-metal bend radius** ≥ material thickness (inside); hole/slot ≥ 2×t + radius from a bend.
- **Lead-in chamfer** 0.5–1.0 × 45° on pins/shafts that insert; **knurl** for press-in inserts.
- **Counterbore depth** so the screw head sits flush or below the surface; leave ≥ 1 mm under the head.

---

## Adding more parts (import seam)

Two routes, both data-driven so the modeller context stays uniform:
1. **Generate it parametrically** — the `parts_make` RPC (`core/parts`) builds fasteners, nuts,
   washers, standoffs, bearings, inserts, dowels from the tables above to exact spec. Prefer this
   for catalog hardware; the geometry is owned, offline, and editable.
2. **Register vendor CAD** — drop a STEP/BREP/STL into the watched parts-library folder (auto-
   registered), or call `vendor_part_register_local_cad(supplier, part_number, cad_path, specs)`.
   Cached under `%APPDATA%/AI-CAD/vendor_parts`, surfaced to the agent via `context_card()` and
   inserted with `vendor_part_insert` / `import_ops_for_part()` as a frozen import feature. The
   McMaster-Carr Product Information API and other supplier APIs populate the SAME registry when
   credentials are configured — **do not scrape supplier websites**; use approved APIs or downloads.

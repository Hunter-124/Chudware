# Rover Competition Ruleset — Cached Reference (NASA HERC)

**Status:** LIVE FETCH SUCCEEDED — this is a cached snapshot for design reference.

- **Competition:** NASA Human Exploration Rover Challenge (HERC) — the successor to the
  former "NASA Great Moonbuggy Race."
- **Source document:** *NASA's Human Exploration Rover Challenge — 2025–2026 Handbook* (official NASA PDF).
- **Source URL:** https://www.nasa.gov/wp-content/uploads/2025/08/nasas-herc-2026-handbook.pdf
- **Found via:** repo research layer `services.research.web_search("NASA Human Exploration Rover Challenge rules requirements handbook")`, fetched with `curl` (the `fetch_page` helper returns lossy-decoded bytes for large binary PDFs, so the raw PDF was pulled directly and text-extracted with `pdftotext -layout`).
- **Fetch date:** 2026-07-08
- **One-time cache:** do NOT re-scrape on every build. Re-fetch only when a new handbook year is published.

This snapshot is for checking wheel / frame / weight / drivetrain part choices against real
competition constraints. Where a specific number was not present in the fetched source, it is
marked "not found in fetched source" rather than invented.

---

## Key Engineering Constraints

There are two divisions: **Human-Powered (HP)** and **Remote-Controlled (RC)**. AI-CAD design
work is primarily the HP rover; RC numbers are included for completeness.

### Size / stowed-dimension limits
- **HP stowed volume:** rover must fit inside a **5 ft × 5 ft × 5 ft** cube in the *stowed*
  configuration (inspected at Mission Readiness Review). [§5.5.3, §8 scoring]
  - Tapes/straps/other restraints used to hold the stowed form are allowed **but count toward
    total weight**. [§5.5.6]
- **HP assembled width:** competition-ready (unfolded) rover shall be **no wider than 5 ft at
  any point**. [§6.3.3]
- **HP assembled height/length:** **no constraint** on overall height and length of the
  assembled rover — BUT a rover judged to have too high a center of gravity or a weight
  imbalance may be barred from the course (tip-over risk). [§5.5.7]
- **RC (for reference):** must fit fully within a **2.5 ft × 2.5 ft × 2.5 ft** cube in
  excursion-ready state. [§5.5.4]

### Weight limits (scored, not hard-capped for HP)
- **HP rover** is weighed in the *stowed* position with all mission components. Weight is scored
  (points carry across both excursions): [§5.5.8, §8]
  - `< 130 lb` → 5 pts
  - `131 – 170 lb` → 3 pts
  - `171 – 210 lb` → 1 pt
  - `> 210 lb` → 0 pts
  - → **Practical HP design target: keep total stowed weight under ~130 lb** for full points;
    210 lb is the effective "beyond this you score nothing" ceiling.
- **RC rover** hard limit: **≤ 60 lb** (§6.4.5). RC weight scoring: `<20 lb`=5, `20–30`=3,
  `30–60`=1, `>60`=0.
- **Task Tool** (optional mission tool) weight scoring: `<20 lb`=5, `20–30`=3, `30–60`=1, `>60`=0.

### Wheel requirements / restrictions
- **Non-pneumatic wheels only.** Teams shall **design and fabricate** their own wheels,
  including the terrain-contacting outer surface (treads) **and** the supporting structure
  (rims, spokes, etc.). [§6.3.5]
- **Only commercial wheel component permitted:** wheel **hubs containing bearings and/or
  bushings**. Everything else on the wheel must be team-made. [§6.3.5]
- Wheels must be designed to handle the terrain obstacles (e.g. crack/crevasse widths vary
  **1–4 in**; boulder-field rocks **3–12 in**; craters ~**2 ft dia × 8 in deep**), so wheel
  geometry must not fall into or hang up on these features. [§9 course description]
- **Material restrictions:** no explicit banned-material list found in the fetched source
  beyond the "team-fabricated, non-pneumatic, no commercial wheel except hub" rule.
- **RC note:** middle/high-school RC teams may use a commercial chassis/drive/suspension, **but
  wheels and tires must still be team-designed and manufactured** (§6.4.2.1).

### Frame / structure requirements
- **Slope stability:** vehicle *including pilots* shall have a center of gravity low enough to
  safely handle **30° slopes front-to-back and side-to-side**. [§6.3.1]
- **Ground clearance:** with pilots in position, ≥ **12 in** clearance between the ground and
  the lowest point of the pilot's appendages. [§6.3.4]
- **Foldability:** structure must collapse into the 5×5×5 ft stowed cube and be unfolded/
  assembled by the two pilots against a timer (`0–30 s`=2 pts, `31–60 s`=1 pt, `>60 s`=0). [§5.5.9, §8]
- **Sharp edges:** design to eliminate/guard sharp edges; final call by the safety judge. [§6.3.9]
- **Explicit frame material / tubing-spec restriction:** not found in fetched source (no named
  material grade, wall-thickness, or extrusion-size rule in the requirements section).

### Drivetrain rules
- **HP must be purely human-powered.** Energy-storage devices — **springs, flywheels, or
  batteries — are NOT allowed** in the drive train. [§6.3.6]
- **Turning radius:** HP vehicle shall achieve a turning radius of **10 ft or less**. [§6.3.2]
- **Braking:** at least one adequate **mechanical** braking system (cable, hydraulic, or other
  mechanical mechanism acting on the rotating members). It must **hold the loaded rover on a
  30° incline**. Hands/body on the wheels or drivetrain to slow the rover is prohibited. [§6.3.8]
- **RC drivetrain (reference):** battery-powered, no flammable liquids; top speed ≤ **5 mph
  (8 km/h)** with software/mechanical limiter; insulated battery enclosure; fuse/breaker on the
  main battery for conductive chassis. [§6.4.1, §6.4.4, §6.4.6, §6.4.7]

### Rider / occupant requirements
- **HP crew:** exactly **two pilots** ride and power the rover; only pilots may be on the rover. [§5.4.6, §5.5.9]
- **Seat restraints:** robust practical seat restraints per pilot, capable of preventing
  ejection on a sudden stop; **3-point motor-vehicle seat belt is the preferred method**; must
  always be worn while driving. [§6.3.7]
- **Pilot PPE:** commercial head protection (bike helmet), full-fingered gloves, long-sleeve/
  long-torso shirt, long pants (secured), long socks, enclosed shoes (laces secured); no stilts
  or foot apparatuses. [§6.1.1]
- Pilot appendage clearance ties into the 12-in ground-clearance rule above (§6.3.4).

---

## How to use this in AI-CAD

Map competition constraints to concrete part-choice checks:

| Constraint (source) | AI-CAD check on part choices |
|---|---|
| **5×5×5 ft stowed cube** (§5.5.3) | When sizing frame extrusions / fold geometry, verify the *folded* bounding box ≤ 60 in on every axis. Drives max member length + hinge/fold count. |
| **Assembled width ≤ 5 ft** (§6.3.3) | Check overall model width in deployed configuration ≤ 60 in. |
| **HP weight → 130 lb sweet spot / 210 lb ceiling** (§5.5.8) | Roll up BOM/material+section mass (`bom_rollup`, `struct_estimate`). Prefer lighter sections/materials (e.g. thin-wall Al / CrMo tube) to stay < 130 lb; flag any design projecting > 210 lb. |
| **Team-fabricated non-pneumatic wheels, only commercial part = bearing/bushing hub** (§6.3.5) | Reject off-the-shelf pneumatic tires/complete wheels in wheel sub-assemblies; only allow a purchased hub-with-bearing standard part. Wheel rim/spoke/tread must be modeled parts, not catalog parts. |
| **No springs/flywheels/batteries in HP drivetrain** (§6.3.6) | Flag any energy-storage part (spring, flywheel, battery) placed in the HP drive path. |
| **Turning radius ≤ 10 ft** (§6.3.2) | Steering-geometry / wheelbase check on the layout. |
| **Brake holds loaded rover on 30° incline** (§6.3.8) | Size braking part (torque) against `weight × sin(30°) × wheel radius`; verify mechanical (not hand/body) actuation. |
| **CoG safe on 30° both axes; 12 in ground clearance** (§6.3.1, §6.3.4) | CoG/mass-properties check with pilots loaded; frame ride-height ≥ 12 in to lowest pilot appendage. |
| **Two pilots + 3-point restraints** (§6.3.7) | Ensure seat + 3-point-belt mounting hardpoints exist in the frame for two occupants. |

**Re-fetch policy:** this is a cached snapshot of the 2026 handbook. When NASA publishes a new
HERC handbook year, re-run the `web_search` → download → `pdftotext` steps above and refresh
this file; do not scrape it repeatedly during normal design/build runs.

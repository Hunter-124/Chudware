"""Modeling tasks M1-M5: real parametric parts (revolved shaft, bolted flange, formed
bracket, counterbored plate, hex standoff) graded on the reopened ``deliverable.step+``.

Each task pins the design intent a drawing would: every feature's size, position, axis
and count is measured natively (cylinders with their end circles, cones, planar faces,
fillet axes), relative to the part's own bbox so a rigid placement of the whole part is
not graded. Volume is a secondary invariant only. Planar checks use the UNSIGNED plane
axis, never the signed face normal (revolved planar faces can come back mis-signed;
core/doc/picks.cpp "CAP PREFERENCE").
"""
from __future__ import annotations

import functools
import math
from typing import Any, Callable

from benchmarks.artifact_validity import geometry_checks as gc
from benchmarks.artifact_validity.evidence import Evidence, gate
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

X: gc.Vec = (1.0, 0.0, 0.0)
Y: gc.Vec = (0.0, 1.0, 0.0)
Z: gc.Vec = (0.0, 0.0, 1.0)
TOL_MM = 0.05
ANGLE_TOL_DEG = 0.1
AXIS_DEG = 0.5
AREA_TOL_MM2 = 1.0
VOLUME_REL = 0.005


def _result(cid: str, ok: bool, measured: Any, expected: Any, tol: Any,
            detail: str = "") -> CheckResult:
    return CheckResult(cid, Status.PASS if ok else Status.FAIL, measured=measured,
                       expected=expected, tolerance=tol, detail=detail)


def _gated(cid: str) -> Callable[[Callable[[Evidence, str], CheckResult]], Callable]:
    def deco(body: Callable[[Evidence, str], CheckResult]):
        @check(cid)
        @functools.wraps(body)
        def run(ev: Evidence) -> CheckResult:
            return gate(ev, cid) or body(ev, cid)

        return run

    return deco


def _solid(cid: str, ev: Evidence) -> CheckResult:
    s = gc.solid_bodies(ev)
    return _result(cid, s["count"] == 1 and s["sound"], s["bodies"],
                   "exactly one valid, closed solid", None,
                   "" if s["count"] == 1 else f"{s['count']} bodies")


def _size(cid: str, ev: Evidence, size: tuple[float, float, float]) -> CheckResult:
    got = gc.part_bbox(ev)["size"]
    return _result(cid, gc.within(got, size, TOL_MM), [round(v, 4) for v in got],
                   [round(v, 4) for v in size], TOL_MM, "bbox X×Y×Z in mm")


def _volume(cid: str, ev: Evidence, expected: float) -> CheckResult:
    v = gc.volume_mm3(ev)
    return _result(cid, abs(v - expected) / expected <= VOLUME_REL, round(v, 3),
                   round(expected, 4), f"{VOLUME_REL:.1%}", "secondary invariant")


def _history(cid: str, ev: Evidence) -> CheckResult:
    faults = gc.history_faults(ev)
    return _result(cid, not faults, [f.get("type") for f in ev.features],
                   "editable feature history that rebuilds cleanly", None,
                   "; ".join(faults))


def _centre(bb: dict) -> gc.Vec:
    return tuple((a + b) / 2.0 for a, b in zip(bb["min"], bb["max"]))  # type: ignore[return-value]


def _flats(ev: Evidence, axis: gc.Vec, coord_axis: int, coord: float) -> list[dict]:
    """Planar faces whose (unsigned) plane normal is ``axis`` and that lie at ``coord``."""
    return [f for f in gc.resolve_faces(ev, {"of": "face", "surf": "plane"})
            if f.get("axis") is not None and gc.angle_deg(f["axis"], axis) <= AXIS_DEG
            and abs(float(f["centroid"][coord_axis]) - coord) <= TOL_MM]


def _dia(c: gc.Cylinder, d: float) -> bool:
    return abs(c.diameter - d) <= TOL_MM


def _cj(cyls: list[gc.Cylinder]) -> list[dict]:
    return [c.to_json() for c in cyls]


# ============================================================================ M1

M1_D1, M1_L1 = 30.0, 40.0            # Ø30 x 40 at the -X end
M1_D2, M1_L2 = 20.0, 60.0            # Ø20 x 60 to the free (+X) end
M1_CH = 1.0                          # 1 x 45° chamfer on the Ø20 free end
M1_SIZE = (M1_L1 + M1_L2, M1_D1, M1_D1)
#: pi*(15^2*40 + 10^2*59) + pi/3*1*(10^2 + 10*9 + 9^2): two cylinders (the Ø20 one cut
#: short by the chamfer) plus the 1 mm frustum r 10 -> 9.
M1_VOLUME = (math.pi * ((M1_D1 / 2) ** 2 * M1_L1 + (M1_D2 / 2) ** 2 * (M1_L2 - M1_CH))
             + math.pi / 3 * M1_CH * ((M1_D2 / 2) ** 2 + (M1_D2 / 2) * (M1_D2 / 2 - M1_CH)
                                      + (M1_D2 / 2 - M1_CH) ** 2))   # 47 093.5211 mm^3


def _m1_steps(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder],
                                     list[gc.Cylinder]]:
    cyls = gc.cylinders(ev)
    xs = [c for c in gc.along(cyls, X) if c.kind == "boss"]
    return (gc.part_bbox(ev), cyls, [c for c in xs if _dia(c, M1_D1)],
            [c for c in xs if _dia(c, M1_D2)])


def _cones(ev: Evidence) -> list[dict]:
    return gc.resolve_faces(ev, {"of": "face", "surf": "cone"})


@_gated("M1.solid")
def m1_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("M1.envelope")
def m1_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, M1_SIZE)


@_gated("M1.steps")
def m1_steps(ev: Evidence, cid: str) -> CheckResult:
    """Exactly two X walls: Ø30 over the first 40 mm from -X, Ø20 from there to the end
    (its free end may stop short of the face by the chamfer leg, which M1.chamfer pins)."""
    bb, cyls, big, small = _m1_steps(ev)
    x0, x1 = bb["min"][0], bb["max"][0]
    ok_big = len(big) == 1 and gc.spans_exactly(big[0], x0, x0 + M1_L1, TOL_MM)
    ok_small = False
    if len(small) == 1 and small[0].span:
        lo, hi = small[0].span
        ok_small = (abs(lo - (x0 + M1_L1)) <= TOL_MM
                    and x1 - M1_CH - TOL_MM <= hi <= x1 + TOL_MM
                    and abs(small[0].length_mm - (hi - lo)) <= TOL_MM)
    ok = len(cyls) == 2 and ok_big and ok_small
    return _result(cid, ok, _cj(cyls),
                   f"Ø{M1_D1:g} over x_min..x_min+{M1_L1:g}, Ø{M1_D2:g} from x_min+{M1_L1:g} "
                   f"to the free end, nothing else cylindrical", TOL_MM)


@_gated("M1.coaxial")
def m1_coaxial(ev: Evidence, cid: str) -> CheckResult:
    _bb, _cyls, big, small = _m1_steps(ev)
    cones = _cones(ev)
    if len(big) != 1 or not small:
        return _result(cid, False, {"d30": len(big), "d20": len(small)}, "one of each",
                       None, "needs the Ø30 and Ø20 walls")
    ref = big[0]
    rows = [("d20", c.axis, c.point) for c in small] + [
        ("cone", gc.canonical_axis(c["axis"]), tuple(float(v) for v in c["centroid"]))
        for c in cones if c.get("axis")]
    errs = {}
    for name, axis, point in rows:
        w = [point[i] - ref.point[i] for i in range(3)]
        t = sum(w[i] * ref.axis[i] for i in range(3))
        off = math.sqrt(max(0.0, sum(v * v for v in w) - t * t))
        errs.setdefault(name, []).append({"offset_mm": round(off, 4),
                                          "angle_deg": round(gc.angle_deg(axis, ref.axis), 4)})
    ok = all(e["offset_mm"] <= TOL_MM and e["angle_deg"] <= AXIS_DEG
             for es in errs.values() for e in es)
    return _result(cid, ok, errs, "Ø20 wall and chamfer cone on the Ø30 axis line",
                   {"offset_mm": TOL_MM, "angle_deg": AXIS_DEG})


@_gated("M1.chamfer")
def m1_chamfer(ev: Evidence, cid: str) -> CheckResult:
    """One cone at the +X end bounded by a Ø20 circle 1 mm in from the end face and a Ø18
    circle on it: a 1 × 45° chamfer and no other."""
    bb = gc.part_bbox(ev)
    x1 = bb["max"][0]
    _y, cy, cz = _centre(bb)
    cones = _cones(ev)
    circles = [e for e in gc.circle_edges(ev)
               if e["axis"] and gc.angle_deg(e["axis"], X) <= AXIS_DEG
               and math.hypot(e["centroid"][1] - cy, e["centroid"][2] - cz) <= TOL_MM]

    def ring(r: float, x: float) -> bool:
        return any(abs(e["radius"] - r) <= TOL_MM and abs(e["centroid"][0] - x) <= TOL_MM
                   for e in circles)

    r2 = M1_D2 / 2
    at_end = [c for c in cones if c.get("axis") and gc.angle_deg(c["axis"], X) <= AXIS_DEG
              and x1 - M1_CH - TOL_MM <= float(c["centroid"][0]) <= x1 + TOL_MM]
    ok = (len(cones) == 1 and len(at_end) == 1 and ring(r2, x1 - M1_CH)
          and ring(r2 - M1_CH, x1))
    return _result(cid, ok, {"cones": [{"centroid": c.get("centroid"),
                                        "area": c.get("area_or_len")} for c in cones],
                             "end_circles": [{"r": round(e["radius"], 4),
                                              "x": round(e["centroid"][0], 4)}
                                             for e in circles if e["centroid"][0] > x1 - 2]},
                   {"cones": 1, "r10_at": round(x1 - M1_CH, 4), "r9_at": round(x1, 4)},
                   TOL_MM, "chamfer legs from its bounding circles")


@_gated("M1.revolved")
def m1_revolved(ev: Evidence, cid: str) -> CheckResult:
    revs = [f for f in ev.features if f.get("type") == "revolve"
            and not f.get("suppressed") and not f.get("failed")]
    return _result(cid, bool(revs), [f.get("type") for f in ev.features],
                   "a live revolve feature in the history", None)


@_gated("M1.volume")
def m1_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, M1_VOLUME)


@_gated("M1.history")
def m1_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


M1 = Task(
    id="M1", version=1, domain="cad",
    prompt=(
        "Model a stepped shaft whose axis lies along the X axis: a Ø30 mm section 40 mm "
        "long at the -X end, followed coaxially by a Ø20 mm section 60 mm long (overall "
        "length 100 mm). Put a 1 mm × 45° chamfer on the free end of the Ø20 section and "
        "leave every other edge sharp. Model the shaft as a revolved profile, as an "
        "editable parametric part, and save it in Chudware's native format as "
        "`deliverable.step+` in the working directory."
    ),
    checks=(m1_solid, m1_envelope, m1_steps, m1_coaxial, m1_chamfer, m1_revolved,
            m1_volume, m1_history),
    budget_s=900,
    notes=("Oracle: bbox 100×30×30. Boss X cylinders: Ø30 spanning x_min..x_min+40 exactly "
           "(end circles + full-revolution length), Ø20 from x_min+40 to x_max-1 (chamfer "
           "may not be graded twice: M1.steps accepts an end in [x_max-1, x_max]); no other "
           "cylinder. Chamfer: exactly one cone, its axis ∥X, centroid within the last "
           "1 mm; circle r10 at x_max-1 and r9 at x_max on the axis (legs 1 and 1, ±0.05). "
           "Coaxial: Ø20 wall and cone axis lines within 0.05 mm / 0.5° of the Ø30 axis. "
           "V = pi*(15^2*40 + 10^2*59) + pi/3*(10^2+10*9+9^2) = 47 093.5211 mm^3 "
           "(secondary, 0.5 %; an unchamfered shaft is only +30.4 mm^3). M1.revolved: a "
           "live revolve feature, as the prompt asks."),
)


# ============================================================================ M2

M2_D, M2_T = 100.0, 12.0
M2_BORE_D = 40.0
M2_HOLES, M2_HOLE_D, M2_PCD = 6, 9.0, 75.0
M2_PITCH_DEG = 360.0 / M2_HOLES
M2_CENTERS = [(M2_PCD / 2 * math.cos(math.radians(M2_PITCH_DEG * k)),
               M2_PCD / 2 * math.sin(math.radians(M2_PITCH_DEG * k))) for k in range(M2_HOLES)]
#: pi*12*(50^2 - 20^2 - 6*4.5^2)
M2_VOLUME = math.pi * M2_T * ((M2_D / 2) ** 2 - (M2_BORE_D / 2) ** 2
                              - M2_HOLES * (M2_HOLE_D / 2) ** 2)    # 74 587.6928 mm^3


def _m2_frame(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder],
                                     list[gc.Cylinder]]:
    """(bbox, every bore, Z through bores on the flange axis, Z through bores off it)."""
    bb = gc.part_bbox(ev)
    cx, cy, _ = _centre(bb)
    bores = gc.bores(ev)
    through = [b for b in gc.along(bores, Z)
               if gc.spans_exactly(b, bb["min"][2], bb["max"][2], TOL_MM)]

    def off(b: gc.Cylinder) -> float:
        p = b.center_in_plane(Z)
        return math.hypot(p[0] - cx, p[1] - cy)

    return (bb, bores, [b for b in through if off(b) <= TOL_MM],
            [b for b in through if off(b) > TOL_MM])


def _m2_polar(ev: Evidence) -> list[tuple[float, float]]:
    """(radius, angle°) of every off-axis Ø9 through hole about the flange centre."""
    bb, _bores, _axial, ring = _m2_frame(ev)
    cx, cy, _ = _centre(bb)
    out = []
    for b in ring:
        if _dia(b, M2_HOLE_D):
            p = b.center_in_plane(Z)
            dx, dy = p[0] - cx, p[1] - cy
            out.append((math.hypot(dx, dy), math.degrees(math.atan2(dy, dx)) % 360.0))
    return sorted(out, key=lambda t: t[1])


@_gated("M2.solid")
def m2_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("M2.envelope")
def m2_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, (M2_D, M2_D, M2_T))


@_gated("M2.bore")
def m2_bore(ev: Evidence, cid: str) -> CheckResult:
    _bb, _bores, axial, _ring = _m2_frame(ev)
    ok = len(axial) == 1 and _dia(axial[0], M2_BORE_D)
    return _result(cid, ok, _cj(axial),
                   f"one Ø{M2_BORE_D:g} Z through bore on the flange axis", TOL_MM)


@_gated("M2.holes")
def m2_holes(ev: Evidence, cid: str) -> CheckResult:
    _bb, bores, axial, ring = _m2_frame(ev)
    others = [b for b in bores if b not in axial and b not in ring]
    ok = (len(ring) == M2_HOLES and all(_dia(b, M2_HOLE_D) for b in ring) and not others)
    return _result(cid, ok, {"off_axis_through": [round(b.diameter, 4) for b in ring],
                             "other_bores": _cj(others)},
                   f"exactly {M2_HOLES} off-axis Ø{M2_HOLE_D:g} Z through holes, no other "
                   f"bores", TOL_MM)


@_gated("M2.pcd")
def m2_pcd(ev: Evidence, cid: str) -> CheckResult:
    polar = _m2_polar(ev)
    radii = [round(r, 4) for r, _a in polar]
    ok = bool(polar) and all(abs(r - M2_PCD / 2) <= TOL_MM for r, _a in polar)
    return _result(cid, ok, radii, M2_PCD / 2, TOL_MM,
                   "radius of each Ø9 hole axis from the flange axis")


@_gated("M2.spacing")
def m2_spacing(ev: Evidence, cid: str) -> CheckResult:
    angles = [a for _r, a in _m2_polar(ev)]
    gaps = [round((angles[(i + 1) % len(angles)] - angles[i]) % 360.0, 4)
            for i in range(len(angles))] if len(angles) > 1 else []
    ok = len(angles) == M2_HOLES and all(abs(g - M2_PITCH_DEG) <= ANGLE_TOL_DEG for g in gaps)
    return _result(cid, ok, gaps, M2_PITCH_DEG, ANGLE_TOL_DEG,
                   "angular gap (deg) between consecutive holes")


@_gated("M2.clocking")
def m2_clocking(ev: Evidence, cid: str) -> CheckResult:
    angles = [a for _r, a in _m2_polar(ev)]
    err = min((min(a, 360.0 - a) for a in angles), default=None)
    ok = err is not None and err <= ANGLE_TOL_DEG
    return _result(cid, ok, [round(a, 4) for a in angles], 0.0, ANGLE_TOL_DEG,
                   "a hole on the +X side of the axis (angle 0°)")


@_gated("M2.volume")
def m2_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, M2_VOLUME)


@_gated("M2.history")
def m2_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


M2 = Task(
    id="M2", version=1, domain="cad",
    prompt=(
        "Model a flat circular mounting flange Ø100 mm and 12 mm thick, its axis along Z, "
        "with a centred Ø40 mm through bore and six Ø9 mm through holes equally spaced on "
        "a Ø75 mm bolt circle concentric with the bore. Clock the pattern so the first hole "
        "is on the +X side: its centre lies on the line through the flange axis parallel "
        "to +X. Build it as an editable parametric part and save it in Chudware's native "
        "format as `deliverable.step+` in the working directory."
    ),
    checks=(m2_solid, m2_envelope, m2_bore, m2_holes, m2_pcd, m2_spacing, m2_clocking,
            m2_volume, m2_history),
    budget_s=900,
    notes=("Oracle: bbox 100×100×12. Bore: exactly one Z through bore (end circles on both "
           "faces) on the bbox-centre axis, Ø40 ±0.05. Holes: exactly 6 off-axis Ø9 Z through "
           "bores, no other bore. PCD: each hole axis 37.5 ±0.05 mm from the flange axis. "
           "Spacing: consecutive angular gaps 60 ±0.1°. Clocking: one hole at 0 ±0.1° "
           "(+X); centres (37.5cos60k, 37.5sin60k). V = pi*12*(50^2 - 20^2 - 6*4.5^2) = "
           "74 587.6928 mm^3 (secondary, 0.5 %; one missing hole is -1.02 %)."),
)


# ============================================================================ M3

M3_T, M3_W = 3.0, 40.0                 # sheet thickness, width along Y
M3_LX, M3_HZ = 60.0, 50.0              # base length along X, overall height along Z
M3_RI, M3_RO = 5.0, 8.0                # inside / outside bend radius (concentric)
M3_HOLE_D = 6.5
M3_BASE_HOLE = (30.0, 20.0)            # (x - x_min, y - y_min), axis Z
M3_UP_HOLE = (20.0, 25.0)              # (y - y_min, z - z_min), axis X
#: L profile = straight base + straight upright + quarter annulus, extruded 40, minus
#: two Ø6.5 x 3 holes: (52*3 + 42*3 + pi/4*(8^2-5^2))*40 - 2*pi*3.25^2*3.
M3_PROFILE = ((M3_LX - M3_RO) * M3_T + (M3_HZ - M3_RO) * M3_T
              + math.pi / 4 * (M3_RO ** 2 - M3_RI ** 2))           # 312.6305 mm^2
M3_VOLUME = M3_PROFILE * M3_W - 2 * math.pi * (M3_HOLE_D / 2) ** 2 * M3_T   # 12 306.1227


def _m3_bends(ev: Evidence) -> tuple[dict, list[dict]]:
    bb = gc.part_bbox(ev)
    faces = [f for f in gc.resolve_faces(ev, {"of": "face", "surf": "cylinder"})
             if f.get("axis") is not None and gc.angle_deg(f["axis"], Y) <= AXIS_DEG]
    return bb, faces


def _m3_holes(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder],
                                     list[gc.Cylinder]]:
    bb = gc.part_bbox(ev)
    bores = [b for b in gc.bores(ev) if gc.angle_deg(b.axis, Y) > AXIS_DEG]
    return (bb, bores, [b for b in gc.along(bores, Z) if _dia(b, M3_HOLE_D)],
            [b for b in gc.along(bores, X) if _dia(b, M3_HOLE_D)])


@_gated("M3.solid")
def m3_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("M3.envelope")
def m3_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, (M3_LX, M3_W, M3_HZ))


@_gated("M3.thickness")
def m3_thickness(ev: Evidence, cid: str) -> CheckResult:
    """Each leg's two largest flats are its outer face (on the bbox) and its inner face
    exactly 3 mm inside it."""
    bb = gc.part_bbox(ev)
    lo = bb["min"]
    rows = {}
    for name, axis, i in (("base", Z, 2), ("upright", X, 0)):
        flats = sorted((f for f in gc.resolve_faces(ev, {"of": "face", "surf": "plane"})
                        if f.get("axis") is not None
                        and gc.angle_deg(f["axis"], axis) <= AXIS_DEG),
                       key=lambda f: -float(f.get("area_or_len") or 0.0))[:2]
        rows[name] = sorted(round(float(f["centroid"][i]) - lo[i], 4) for f in flats)
    ok = all(len(v) == 2 and abs(v[0]) <= TOL_MM and abs(v[1] - M3_T) <= TOL_MM
             for v in rows.values())
    return _result(cid, ok, rows, {"base": [0.0, M3_T], "upright": [0.0, M3_T]}, TOL_MM,
                   "offsets of each leg's two largest flats from its outer bbox face")


@_gated("M3.bend")
def m3_bend(ev: Evidence, cid: str) -> CheckResult:
    """Exactly two Y rounds: concave R5 and convex R8, each a quarter turn across the full
    width, both on the axis line (x_min+8, z_min+8)."""
    bb, faces = _m3_bends(ev)
    lo = bb["min"]
    want = (lo[0] + M3_RO, (lo[1] + bb["max"][1]) / 2.0, lo[2] + M3_RO)
    width = bb["size"][1]
    rows, ok = [], len(faces) == 2
    for r, kind in ((M3_RI, "bore"), (M3_RO, "boss")):
        hit = [f for f in faces if abs(float(f["radius"]) - r) <= TOL_MM
               and ("bore" if f.get("concavity") in ("bore", "concave") else "boss") == kind]
        if len(hit) != 1:
            ok = False
            rows.append({"r": r, "kind": kind, "found": len(hit)})
            continue
        f = hit[0]
        p, sweep = gc.arc_axis_point(f, width, want)
        err = math.hypot(p[0] - want[0], p[2] - want[2])
        area = float(f.get("area_or_len") or 0.0)
        quarter = math.pi / 2 * r * M3_W
        ok = ok and err <= TOL_MM and abs(area - quarter) <= AREA_TOL_MM2
        rows.append({"r": round(float(f["radius"]), 4), "kind": kind,
                     "axis_xz_from_corner": [round(p[0] - lo[0], 4), round(p[2] - lo[2], 4)],
                     "sweep_deg": round(sweep, 3), "area": round(area, 4)})
    return _result(cid, ok, rows,
                   {"inside": f"R{M3_RI:g} concave", "outside": f"R{M3_RO:g} convex",
                    "axis_xz_from_corner": [M3_RO, M3_RO], "sweep_deg": 90.0,
                    "areas": [round(math.pi / 2 * r * M3_W, 4) for r in (M3_RI, M3_RO)]},
                   {"axis_mm": TOL_MM, "area_mm2": AREA_TOL_MM2},
                   f"other Y rounds: {len(faces)} total")


@_gated("M3.holes")
def m3_holes(ev: Evidence, cid: str) -> CheckResult:
    """One Ø6.5 Z hole through the base (z_min..z_min+3) and one Ø6.5 X hole through the
    upright (x_min..x_min+3); no other holes."""
    bb, bores, zh, xh = _m3_holes(ev)
    lo = bb["min"]
    ok = (len(bores) == 2 and len(zh) == 1 and len(xh) == 1
          and gc.spans_exactly(zh[0], lo[2], lo[2] + M3_T, TOL_MM)
          and gc.spans_exactly(xh[0], lo[0], lo[0] + M3_T, TOL_MM))
    return _result(cid, ok, _cj(bores),
                   f"Ø{M3_HOLE_D:g} through each leg: Z over z_min..+{M3_T:g}, X over "
                   f"x_min..+{M3_T:g}", TOL_MM)


@_gated("M3.hole_positions")
def m3_hole_positions(ev: Evidence, cid: str) -> CheckResult:
    bb, _bores, zh, xh = _m3_holes(ev)
    lo = bb["min"]
    if len(zh) != 1 or len(xh) != 1:
        return _result(cid, False, {"z_holes": len(zh), "x_holes": len(xh)}, "one each",
                       None, "needs one Ø6.5 hole per leg")
    pz, px = zh[0].center_in_plane(Z), xh[0].center_in_plane(X)
    got = {"base_xy": [round(pz[0] - lo[0], 4), round(pz[1] - lo[1], 4)],
           "upright_yz": [round(px[1] - lo[1], 4), round(px[2] - lo[2], 4)]}
    ok = (math.dist(got["base_xy"], M3_BASE_HOLE) <= TOL_MM
          and math.dist(got["upright_yz"], M3_UP_HOLE) <= TOL_MM)
    return _result(cid, ok, got, {"base_xy": list(M3_BASE_HOLE),
                                  "upright_yz": list(M3_UP_HOLE)}, TOL_MM,
                   "hole axes from the outer bend corner (x_min, y_min, z_min)")


@_gated("M3.volume")
def m3_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, M3_VOLUME)


@_gated("M3.history")
def m3_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


M3 = Task(
    id="M3", version=1, domain="cad",
    prompt=(
        "Model an L-shaped mounting bracket of 3 mm thick sheet, 40 mm wide along Y. The "
        "base leg lies flat on the XY plane and is 60 mm long along X; the upright leg "
        "rises along +Z from the -X end of the base to an overall height of 50 mm (both "
        "lengths measured to the outside of the bend). The bend has a 5 mm inside radius "
        "and an 8 mm outside radius, so the wall stays 3 mm thick all the way round. Put "
        "one Ø6.5 mm through hole in each leg, centred across the 40 mm width: the base "
        "hole 30 mm from the outer face of the upright, the upright hole 25 mm above the "
        "underside of the base. Build it as an editable parametric part and save it in "
        "Chudware's native format as `deliverable.step+` in the working directory."
    ),
    checks=(m3_solid, m3_envelope, m3_thickness, m3_bend, m3_holes, m3_hole_positions,
            m3_volume, m3_history),
    budget_s=900,
    notes=("Oracle (outer corner = bbox min): bbox 60×40×50. Thickness: the two largest "
           "Z-normal flats at z_min and z_min+3, the two largest X-normal flats at x_min "
           "and x_min+3 (±0.05). Bend: exactly two Y rounds, R5 concave and R8 convex, each "
           "a 90° quarter across the width (area pi/2*r*40 ±1 mm^2: 314.1593 / 502.6548), "
           "axis line at (x_min+8, z_min+8) ±0.05 — recovered from the face centroid, which "
           "sits r*sin(t/2)/(t/2) off the axis (gc.arc_axis_point). Holes: one Ø6.5 Z bore "
           "spanning exactly z_min..z_min+3 at (30, 20) from (x_min, y_min), one Ø6.5 X bore "
           "spanning exactly x_min..x_min+3 at (y 20, z 25); no other holes. V = (52*3 + "
           "42*3 + pi/4*(8^2-5^2))*40 - 2*pi*3.25^2*3 = 12 306.1227 mm^3 (secondary; a "
           "sharp R5/R5 bend is +2.7 %)."),
)


# ============================================================================ M4

M4_SIZE = (80.0, 60.0, 10.0)
M4_DRILL_D, M4_CB_D, M4_CB_DEPTH = 6.6, 11.0, 6.5
M4_CENTERS = [(sx * 30.0, sy * 20.0) for sx in (1, -1) for sy in (1, -1)]
#: The annular floor of each counterbore.
M4_FLOOR = math.pi / 4 * (M4_CB_D ** 2 - M4_DRILL_D ** 2)              # 60.8212 mm^2
#: 80*60*10 - 4*pi*(5.5^2*6.5 + 3.3^2*3.5)
M4_VOLUME = (M4_SIZE[0] * M4_SIZE[1] * M4_SIZE[2]
             - 4 * math.pi * ((M4_CB_D / 2) ** 2 * M4_CB_DEPTH
                              + (M4_DRILL_D / 2) ** 2 * (M4_SIZE[2] - M4_CB_DEPTH)))
#                                                                       45 050.1702 mm^3


def _m4_bores(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder],
                                     list[gc.Cylinder]]:
    bb = gc.part_bbox(ev)
    bores = gc.bores(ev)
    zs = gc.along(bores, Z)
    return (bb, bores, [b for b in zs if _dia(b, M4_DRILL_D)],
            [b for b in zs if _dia(b, M4_CB_D)])


@_gated("M4.solid")
def m4_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("M4.envelope")
def m4_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, M4_SIZE)


@_gated("M4.through")
def m4_through(ev: Evidence, cid: str) -> CheckResult:
    """Four Ø6.6 Z bores, each running from the counterbore floor out through the bottom."""
    bb, _bores, drill, _cb = _m4_bores(ev)
    z0, z1 = bb["min"][2], bb["max"][2]
    ok = len(drill) == 4 and all(gc.spans_exactly(b, z0, z1 - M4_CB_DEPTH, TOL_MM)
                                 for b in drill)
    return _result(cid, ok, _cj(drill),
                   f"4 × Ø{M4_DRILL_D:g} over z_min..z_max-{M4_CB_DEPTH:g}", TOL_MM)


@_gated("M4.counterbores")
def m4_counterbores(ev: Evidence, cid: str) -> CheckResult:
    bb, bores, drill, cb = _m4_bores(ev)
    z1 = bb["max"][2]
    others = [b for b in bores if b not in drill and b not in cb]
    ok = (len(cb) == 4 and not others
          and all(gc.spans_exactly(b, z1 - M4_CB_DEPTH, z1, TOL_MM) for b in cb))
    return _result(cid, ok, {"counterbores": _cj(cb), "other_bores": _cj(others)},
                   f"4 × Ø{M4_CB_D:g} over z_max-{M4_CB_DEPTH:g}..z_max, no other bores",
                   TOL_MM)


@_gated("M4.floors")
def m4_floors(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    floors = _flats(ev, Z, 2, bb["max"][2] - M4_CB_DEPTH)
    areas = [round(float(f.get("area_or_len") or 0.0), 4) for f in floors]
    ok = len(floors) == 4 and all(abs(a - M4_FLOOR) <= AREA_TOL_MM2 for a in areas)
    return _result(cid, ok, areas, round(M4_FLOOR, 4),
                   {"area_mm2": AREA_TOL_MM2, "z_mm": TOL_MM},
                   f"flat annular floors at z_max-{M4_CB_DEPTH:g}")


@_gated("M4.positions")
def m4_positions(ev: Evidence, cid: str) -> CheckResult:
    bb, _bores, drill, cb = _m4_bores(ev)
    cx, cy, _ = _centre(bb)

    def rel(bs: list[gc.Cylinder]) -> list[tuple[float, float]]:
        return [(p[0] - cx, p[1] - cy) for p in (b.center_in_plane(Z) for b in bs)]

    md = gc.match_points(rel(drill), M4_CENTERS, TOL_MM)
    mc = gc.match_points(rel(cb), M4_CENTERS, TOL_MM)
    # Counterbore presence/count is M4.counterbores' verdict; here every one found must sit
    # on a hole position.
    ok = md["matched"] == 4 and not md["missing"] and not md["extra"] and not mc["extra"]
    return _result(cid, ok, {"drill": md, "counterbore": mc},
                   "4 drill axes at (±30, ±20) from the plate centre, every counterbore "
                   "axis on one of them", TOL_MM)


@_gated("M4.volume")
def m4_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, M4_VOLUME)


@_gated("M4.history")
def m4_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


M4 = Task(
    id="M4", version=1, domain="cad",
    prompt=(
        "Model a rectangular plate 80 mm along X, 60 mm along Y and 10 mm thick along Z with "
        "four counterbored holes for M6 socket-head cap screws, entered from the top (+Z) "
        "face. The hole centres are at (±30, ±20) mm in X and Y from the plate centre. Each "
        "hole is a Ø6.6 mm through hole with a Ø11 mm counterbore 6.5 mm deep measured from "
        "the top face. Build it as an editable parametric part and save it in Chudware's "
        "native format as `deliverable.step+` in the working directory."
    ),
    checks=(m4_solid, m4_envelope, m4_through, m4_counterbores, m4_floors, m4_positions,
            m4_volume, m4_history),
    budget_s=900,
    notes=("Oracle: bbox 80×60×10. Through: exactly 4 Ø6.6 Z bores each spanning exactly "
           "z_min..z_max-6.5 (3.5 long). Counterbores: exactly 4 Ø11 Z bores each spanning "
           "exactly z_max-6.5..z_max; no other bores. Floors: exactly 4 flat faces at "
           "z_max-6.5, each pi/4*(11^2-6.6^2) = 60.8212 ±1 mm^2. Positions: the 4 drill "
           "axes matched 1:1 to (±30, ±20) from the bbox centre and every counterbore axis "
           "on one of them, ±0.05. "
           "V = 80*60*10 - 4*pi*(5.5^2*6.5 + 3.3^2*3.5) = 45 050.1702 mm^3 (secondary)."),
)


# ============================================================================ M5

M5_AF, M5_LEN, M5_BORE_D = 10.0, 25.0, 5.0
M5_AC = M5_AF / math.cos(math.radians(30.0))                         # 11.5470 across corners
M5_SIDE_AREA = M5_AF / math.sqrt(3.0) * M5_LEN                        # 144.3376 mm^2
#: (sqrt(3)/2)*10^2*25 - pi*2.5^2*25
M5_VOLUME = (math.sqrt(3.0) / 2.0 * M5_AF ** 2 * M5_LEN
             - math.pi * (M5_BORE_D / 2) ** 2 * M5_LEN)              # 1 674.1897 mm^3


def _m5_sides(ev: Evidence) -> list[tuple[float, dict]]:
    """(direction° in 0..180, face) of every planar face whose normal lies in XY."""
    out = []
    for f in gc.resolve_faces(ev, {"of": "face", "surf": "plane"}):
        a = f.get("axis")
        if a is None or gc.angle_deg(a, Z) < 90.0 - AXIS_DEG:
            continue
        out.append((math.degrees(math.atan2(float(a[1]), float(a[0]))) % 180.0, f))
    return sorted(out, key=lambda t: t[0])


def _m5_pairs(ev: Evidence) -> list[list[tuple[float, dict]]]:
    """Side faces grouped by parallel direction (wraps 180°)."""
    groups: list[list[tuple[float, dict]]] = []
    for d, f in _m5_sides(ev):
        for g in groups:
            diff = abs(d - g[0][0]) % 180.0
            if min(diff, 180.0 - diff) <= AXIS_DEG:
                g.append((d, f))
                break
        else:
            groups.append([(d, f)])
    return groups


@_gated("M5.solid")
def m5_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("M5.envelope")
def m5_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, (M5_AC, M5_AF, M5_LEN))


@_gated("M5.hex_sides")
def m5_hex_sides(ev: Evidence, cid: str) -> CheckResult:
    """Six flat sides in three parallel pairs whose directions are 60° apart, all the same
    full-length face (a regular hexagon, whatever its size)."""
    groups = _m5_pairs(ev)
    dirs = sorted(g[0][0] for g in groups)
    gaps = [round((dirs[(i + 1) % len(dirs)] - dirs[i]) % 180.0, 4)
            for i in range(len(dirs))] if len(dirs) > 1 else []
    areas = [round(float(f.get("area_or_len") or 0.0), 4) for g in groups for _d, f in g]
    ok = (len(groups) == 3 and all(len(g) == 2 for g in groups)
          and all(abs(g - 60.0) <= ANGLE_TOL_DEG for g in gaps)
          and max(areas) - min(areas) <= AREA_TOL_MM2)
    return _result(cid, ok, {"directions_deg": [round(d, 4) for d in dirs], "gaps": gaps,
                             "areas": areas},
                   "3 parallel pairs of equal sides, directions 60° apart",
                   {"angle_deg": ANGLE_TOL_DEG, "area_mm2": AREA_TOL_MM2})


@_gated("M5.across_flats")
def m5_across_flats(ev: Evidence, cid: str) -> CheckResult:
    rows = []
    for g in _m5_pairs(ev):
        if len(g) != 2:
            rows.append(None)
            continue
        d = math.radians(g[0][0])
        n = (math.cos(d), math.sin(d))
        (_a, f1), (_b, f2) = g
        c1, c2 = f1["centroid"], f2["centroid"]
        rows.append(round(abs((float(c1[0]) - float(c2[0])) * n[0]
                              + (float(c1[1]) - float(c2[1])) * n[1]), 4))
    ok = len(rows) == 3 and all(r is not None and abs(r - M5_AF) <= TOL_MM for r in rows)
    return _result(cid, ok, rows, M5_AF, TOL_MM, "distance between each parallel side pair")


@_gated("M5.flat_orientation")
def m5_flat_orientation(ev: Evidence, cid: str) -> CheckResult:
    dirs = [round(g[0][0], 4) for g in _m5_pairs(ev)]
    ok = any(abs(d - 90.0) <= AXIS_DEG for d in dirs)
    return _result(cid, ok, dirs, 90.0, AXIS_DEG,
                   "side-face normal directions (deg from +X); one pair must face ±Y")


@_gated("M5.bore")
def m5_bore(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    cx, cy, _ = _centre(bb)
    bores = gc.bores(ev)
    through = [b for b in gc.along(bores, Z)
               if gc.spans_exactly(b, bb["min"][2], bb["max"][2], TOL_MM)]
    ok = len(bores) == 1 and len(through) == 1 and _dia(through[0], M5_BORE_D)
    off = None
    if ok:
        p = through[0].center_in_plane(Z)
        off = [round(p[0] - cx, 4), round(p[1] - cy, 4)]
        ok = math.hypot(*off) <= TOL_MM
    return _result(cid, ok, {"bores": _cj(bores), "axis_offset": off},
                   f"one coaxial Ø{M5_BORE_D:g} Z through bore", TOL_MM)


@_gated("M5.volume")
def m5_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, M5_VOLUME)


@_gated("M5.history")
def m5_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


M5 = Task(
    id="M5", version=1, domain="cad",
    prompt=(
        "Model a hexagonal standoff: a regular hexagonal prism 10 mm across flats and 25 mm "
        "long along Z, oriented so that two of its flats are parallel to the XZ plane (their "
        "normals point along ±Y), with a Ø5 mm through bore on its axis. Build it as an "
        "editable parametric part and save it in Chudware's native format as "
        "`deliverable.step+` in the working directory."
    ),
    checks=(m5_solid, m5_envelope, m5_hex_sides, m5_across_flats, m5_flat_orientation,
            m5_bore, m5_volume, m5_history),
    budget_s=900,
    notes=("Oracle: bbox (10/cos30 = 11.5470) × 10 × 25. Hex sides: exactly 6 planar faces "
           "with normals in XY, in 3 parallel pairs whose directions are 60 ±0.1° apart and "
           "areas equal within 1 mm^2 (each 10/sqrt(3)*25 = 144.3376). Across flats: each "
           "pair 10 ±0.05 apart. Orientation: one pair's normal at 90 ±0.5° from +X (±Y). "
           "Bore: the only bore, Ø5 Z, spanning z_min..z_max, axis on the bbox centre ±0.05. "
           "V = (sqrt(3)/2)*10^2*25 - pi*2.5^2*25 = 1 674.1897 mm^3 (secondary)."),
)


TASKS: tuple[Task, ...] = (M1, M2, M3, M4, M5)

"""Hard tasks H1-H6: swept and lofted thin-wall parts, a design-to-requirement cantilever
with its own stress study, a hinge assembly with a driven revolute, a history-preserving
redesign of a supplied flange, and a shelled enclosure with internal features — graded on
the reopened ``deliverable.step+`` only.

These separate models that pass every M task: each needs several dependent features (or
documents) to come out right together, and H3 needs an engineering decision the prompt does
not hand over. Every size, position, axis and count is measured natively: cylinders with
their end circles, torus faces through ``doc_face_points`` (their analytic surface: origin,
axis, major/minor radius — ``doc_query`` carries no torus radii), planar faces, cutting-plane
sections (``eval_section_props``), and for the assembly the native mate solver
(``simulate_motion``) and exact interference (``eval_interference``). Volume is a secondary
invariant only. Planar checks use the UNSIGNED plane axis, never the signed face normal.
"""
from __future__ import annotations

import functools
import math
from pathlib import Path
from typing import Any, Callable, Sequence

from benchmarks.artifact_validity import geometry_checks as gc
from benchmarks.artifact_validity.evidence import Evidence, build_evidence, gate, sha256_file
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

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


def _box(cid: str, ev: Evidence, lo: gc.Vec, hi: gc.Vec, detail: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    ok = gc.within(bb["min"], lo, TOL_MM) and gc.within(bb["max"], hi, TOL_MM)
    return _result(cid, ok, {"min": [round(v, 4) for v in bb["min"]],
                             "max": [round(v, 4) for v in bb["max"]]},
                   {"min": list(lo), "max": list(hi)}, TOL_MM, detail)


def _volume(cid: str, ev: Evidence, expected: float) -> CheckResult:
    v = gc.volume_mm3(ev)
    return _result(cid, abs(v - expected) / expected <= VOLUME_REL, round(v, 3),
                   round(expected, 4), f"{VOLUME_REL:.1%}", "secondary invariant")


def _history(cid: str, ev: Evidence) -> CheckResult:
    faults = gc.history_faults(ev)
    return _result(cid, not faults, [f.get("type") for f in ev.features],
                   "editable feature history that rebuilds cleanly", None,
                   "; ".join(faults))


def _live(ev: Evidence, ftype: str) -> list[dict]:
    return [f for f in ev.features if f.get("type") == ftype
            and not f.get("suppressed") and not f.get("failed")]


def _has_live(cid: str, ev: Evidence, ftype: str) -> CheckResult:
    return _result(cid, bool(_live(ev, ftype)), [f.get("type") for f in ev.features],
                   f"a live {ftype} feature in the history", None)


def _centre(bb: dict) -> gc.Vec:
    return tuple((a + b) / 2.0 for a, b in zip(bb["min"], bb["max"]))  # type: ignore[return-value]


def _off_line(p: Sequence[float], origin: Sequence[float], axis: Sequence[float]) -> float:
    """Distance of point ``p`` from the line through ``origin`` along ``axis``."""
    a = gc.unit(axis)
    w = [p[i] - origin[i] for i in range(3)]
    t = sum(w[i] * a[i] for i in range(3))
    return math.sqrt(max(0.0, sum(v * v for v in w) - t * t))


def _on_line(c: gc.Cylinder, direction: gc.Vec, point: Sequence[float]) -> bool:
    return (gc.angle_deg(c.axis, direction) <= AXIS_DEG
            and _off_line(point, c.point, c.axis) <= TOL_MM)


def _planes(ev: Evidence) -> list[dict]:
    return [f for f in gc.resolve_faces(ev, {"of": "face", "surf": "plane"})
            if f.get("axis") is not None]


def _flats(ev: Evidence, axis: gc.Vec) -> list[dict]:
    """Planar faces whose UNSIGNED plane normal is ``axis``."""
    return [f for f in _planes(ev) if gc.angle_deg(f["axis"], axis) <= AXIS_DEG]


def _area(f: dict) -> float:
    return float(f.get("area_or_len") or 0.0)


def _face_row(f: dict) -> dict:
    return {"centroid": [round(float(v), 4) for v in f["centroid"]], "area": round(_area(f), 4)}


def _match_faces(faces: list[dict], want: list[tuple[int, float, float]],
                 key_axis: int | None = None) -> tuple[bool, list]:
    """1:1 match planar faces to (coord_axis, coord, area) rows; with ``key_axis`` the face
    centroid must also sit at the row's 4th element along that axis. Returns (all matched
    and no face left over, unmatched rows)."""
    left = list(faces)
    missing = []
    for row in want:
        axis, coord, area = row[0], row[1], row[2]
        hit = [f for f in left if abs(float(f["centroid"][axis]) - coord) <= TOL_MM
               and abs(_area(f) - area) <= AREA_TOL_MM2
               and (key_axis is None or abs(float(f["centroid"][key_axis]) - row[3]) <= TOL_MM)]
        if len(hit) != 1:
            missing.append({"row": list(row), "found": len(hit)})
            continue
        left.remove(hit[0])
    return not missing and not left, missing + [{"extra": _face_row(f)} for f in left]


def _cj(cyls: list[gc.Cylinder]) -> list[dict]:
    return [c.to_json() for c in cyls]


def _dia(c: gc.Cylinder, d: float) -> bool:
    return abs(c.diameter - d) <= TOL_MM


def _section(ev: Evidence, origin: Sequence[float], normal: Sequence[float]) -> dict:
    return ev.rpc("eval_section_props", {"plane": {"origin": [float(v) for v in origin],
                                                   "normal": [float(v) for v in normal]}})


# ============================================================================ H1

H1_OD, H1_ID, H1_R = 25.0, 21.0, 50.0                     # tube Ø, bore Ø, bend radius
H1_CORNERS = ((0.0, 0.0, 0.0), (300.0, 0.0, 0.0), (300.0, 0.0, 200.0), (500.0, 0.0, 200.0))
#: (direction, point on the centreline, span along it) of each straight between the bends
H1_STRAIGHTS = ((X, (0.0, 0.0, 0.0), (0.0, 250.0)),
                (Z, (300.0, 0.0, 0.0), (50.0, 150.0)),
                (X, (0.0, 0.0, 200.0), (350.0, 500.0)))
#: bend centres (torus origins); both bends lie in the XZ plane, torus axis ∥ Y
H1_BENDS = ((250.0, 0.0, 50.0), (350.0, 0.0, 150.0))
H1_RING = math.pi / 4 * (H1_OD ** 2 - H1_ID ** 2)              # 144.5133 mm^2
H1_CENTRELINE = 250.0 + 100.0 + 150.0 + 2 * (math.pi / 2 * H1_R)  # 657.0796 mm
#: Pappus: annulus area x centreline length (the bends are full-section tori quarters)
H1_VOLUME = H1_RING * H1_CENTRELINE                             # 94 956.7212 mm^3


def _h1_tori(ev: Evidence) -> list[dict]:
    """Every torus face with its analytic surface (doc_face_points)."""
    out = []
    for f in gc.resolve_faces(ev, {"of": "face", "surf": "torus"}):
        s = ev.rpc("doc_face_points", {"selector": f, "max_n": 4}).get("surface") or {}
        if s.get("type") != "torus":
            raise ValueError(f"torus face {f.get('sid')} has no analytic torus readback: {s!r}")
        out.append({"sid": f.get("sid"), "area": _area(f),
                    "origin": tuple(float(v) for v in s["origin"]),
                    "axis": tuple(float(v) for v in s["axis"]),
                    "major": float(s["major_radius"]), "minor": float(s["minor_radius"])})
    return out


def _torus_row(t: dict) -> dict:
    return {"origin": [round(v, 4) for v in t["origin"]], "axis": [round(v, 4) for v in t["axis"]],
            "major": round(t["major"], 4), "minor": round(t["minor"], 4),
            "sweep_deg": round(math.degrees(t["area"] / (2 * math.pi * t["minor"] * t["major"])),
                               3)}


def _quarter(t: dict) -> bool:
    """A 90° sweep of a full tube section: area = (2 pi r)(pi/2 R) = pi^2 R r."""
    return abs(t["area"] - math.pi ** 2 * t["major"] * t["minor"]) <= AREA_TOL_MM2


@_gated("H1.solid")
def h1_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("H1.envelope")
def h1_envelope(ev: Evidence, cid: str) -> CheckResult:
    r = H1_OD / 2
    return _box(cid, ev, (0.0, -r, -r), (500.0, r, 200.0 + r),
                "tube on the given world centreline, y = 0 plane")


@_gated("H1.straights")
def h1_straights(ev: Evidence, cid: str) -> CheckResult:
    """Three straights, each a Ø25 outer wall and a Ø21 bore on the centreline segment
    between the bend tangent points, nothing else cylindrical."""
    cyls = gc.cylinders(ev)
    rows, ok = [], len(cyls) == 2 * len(H1_STRAIGHTS)
    for direction, point, (lo, hi) in H1_STRAIGHTS:
        for kind, d in (("boss", H1_OD), ("bore", H1_ID)):
            hit = [c for c in cyls if c.kind == kind and _dia(c, d) and _on_line(c, direction, point)
                   and gc.spans_exactly(c, lo, hi, TOL_MM)]
            ok = ok and len(hit) == 1
            rows.append({"kind": kind, "d": d, "span": [lo, hi], "found": len(hit)})
    return _result(cid, ok, {"expected_hits": rows, "cylinders": _cj(cyls)},
                   "x 0..250 (y=z=0), z 50..150 (x=300), x 350..500 (z=200): one Ø25 boss and "
                   "one Ø21 bore each", TOL_MM)


@_gated("H1.bends")
def h1_bends(ev: Evidence, cid: str) -> CheckResult:
    """Two 90° bends of centreline radius 50: per bend one outer (r 12.5) and one inner
    (r 10.5) torus quarter on the bend centre, axis ∥ Y."""
    tori = _h1_tori(ev)
    ok = len(tori) == 4
    misses = []
    for centre in H1_BENDS:
        for r in (H1_OD / 2, H1_ID / 2):
            hit = [t for t in tori if math.dist(t["origin"], centre) <= TOL_MM
                   and gc.angle_deg(t["axis"], Y) <= AXIS_DEG
                   and abs(t["major"] - H1_R) <= TOL_MM and abs(t["minor"] - r) <= TOL_MM
                   and _quarter(t)]
            if len(hit) != 1:
                ok = False
                misses.append({"centre": list(centre), "minor": r, "found": len(hit)})
    return _result(cid, ok, {"tori": [_torus_row(t) for t in tori], "missing": misses},
                   {"centres": [list(c) for c in H1_BENDS], "major": H1_R,
                    "minor": [H1_OD / 2, H1_ID / 2], "sweep_deg": 90.0, "axis": "Y"},
                   {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H1.wall")
def h1_wall(ev: Evidence, cid: str) -> CheckResult:
    """Uniform 2 mm wall: every outer wall (cylinder or torus) has an inner wall on the same
    axis line / bend centre, radially exactly 2 mm inside it, and vice versa."""
    cyls = gc.cylinders(ev)
    tori = _h1_tori(ev)
    unpaired = []
    for c in cyls:
        other = "bore" if c.kind == "boss" else "boss"
        mate = [o for o in cyls if o.kind == other and _on_line(o, c.axis, c.point)
                and abs(abs(o.radius - c.radius) - (H1_OD - H1_ID) / 2) <= TOL_MM]
        if len(mate) != 1:
            unpaired.append(c.to_json())
    for t in tori:
        mate = [o for o in tori if o is not t and math.dist(o["origin"], t["origin"]) <= TOL_MM
                and gc.angle_deg(o["axis"], t["axis"]) <= AXIS_DEG
                and abs(o["major"] - t["major"]) <= TOL_MM
                and abs(abs(o["minor"] - t["minor"]) - (H1_OD - H1_ID) / 2) <= TOL_MM]
        if len(mate) != 1:
            unpaired.append(_torus_row(t))
    ok = not unpaired and bool(cyls) and bool(tori)
    return _result(cid, ok, {"cylinders": len(cyls), "tori": len(tori), "unpaired": unpaired},
                   "every outer wall paired with a concentric inner wall 2 mm inside", TOL_MM)


@_gated("H1.open_ends")
def h1_open_ends(ev: Evidence, cid: str) -> CheckResult:
    """The only flat faces are the two annular ends (pi/4*(25^2-21^2)) at the path ends."""
    flats = _planes(ev)
    want = [((0.0, 0.0, 0.0), X), ((500.0, 0.0, 200.0), X)]
    rows, ok = [], len(flats) == 2
    for p, axis in want:
        hit = [f for f in flats if math.dist([float(v) for v in f["centroid"]], p) <= TOL_MM
               and gc.angle_deg(f["axis"], axis) <= AXIS_DEG
               and abs(_area(f) - H1_RING) <= AREA_TOL_MM2]
        ok = ok and len(hit) == 1
        rows.append({"at": list(p), "found": len(hit)})
    return _result(cid, ok, {"flats": [_face_row(f) for f in flats], "ends": rows},
                   {"ends": [list(w[0]) for w in want], "area": round(H1_RING, 4)},
                   {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H1.swept")
def h1_swept(ev: Evidence, cid: str) -> CheckResult:
    return _has_live(cid, ev, "sweep")


@_gated("H1.volume")
def h1_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, H1_VOLUME)


@_gated("H1.history")
def h1_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


H1 = Task(
    id="H1", version=1, domain="cad",
    prompt=(
        "Model a handrail tube: 25 mm outside diameter, 21 mm inside diameter (2 mm wall), "
        "swept along a planar centreline in the XZ plane (y = 0). The centreline follows the "
        "polyline (0, 0, 0) -> (300, 0, 0) -> (300, 0, 200) -> (500, 0, 200) mm, with each of "
        "its two 90° corners replaced by a tangent arc of 50 mm centreline bend radius. Both "
        "ends stay open: the bore runs through from end to end, no caps. Make it ONE solid by "
        "sweeping the tube cross-section along the path, as an editable parametric part, and "
        "save it in Chudware's native format as `deliverable.step+` in the working directory."
    ),
    checks=(h1_solid, h1_envelope, h1_straights, h1_bends, h1_wall, h1_open_ends, h1_swept,
            h1_volume, h1_history),
    budget_s=1200,
    notes=("Oracle (world frame, as the prompt pins the path): bbox (0,-12.5,-12.5)..(500,12.5,"
           "212.5). Straights between the bend tangent points: x 0..250 on (y,z)=(0,0); z 50..150 "
           "on (x,y)=(300,0); x 350..500 on (y,z)=(0,200) — each exactly one Ø25 boss and one "
           "Ø21 bore spanning that interval (end circles + full-revolution area), 6 cylinders "
           "total. Bends: exactly 4 torus faces read back analytically via doc_face_points "
           "(doc_query has no torus radii): per bend centre (250,0,50) and (350,0,150) one "
           "minor r 12.5 and one r 10.5, major 50, axis ∥ Y, area pi^2*R*r (a 90° sweep: "
           "6168.5028 / 5181.5423 ±1 mm^2). Wall: every outer wall has a concentric inner wall "
           "exactly 2 mm inside. Open ends: the only flats are two annuli pi/4*(25^2-21^2) = "
           "144.5133 mm^2 at (0,0,0) and (500,0,200), normal ∥ X. Pappus: V = 144.5133 * "
           "(250+100+150+pi*50) = 94 956.7212 mm^3 (secondary). H1.swept: a live sweep."),
)


# ============================================================================ H2

H2_RECT = (100.0, 60.0)                 # outer rectangle at z 0..10 (X, Y)
H2_ROUND_D = 80.0                       # outer circle at z 110..120
H2_T, H2_H, H2_COLLAR = 2.0, 120.0, 10.0
H2_RECT_RING = H2_RECT[0] * H2_RECT[1] - (H2_RECT[0] - 2 * H2_T) * (H2_RECT[1] - 2 * H2_T)  # 624
H2_ROUND_RING = math.pi / 4 * (H2_ROUND_D ** 2 - (H2_ROUND_D - 2 * H2_T) ** 2)  # 490.0885
#: A meridian section (vertical plane through the axis along X or Y) cuts two wall strips;
#: a 2 mm wall measured horizontally gives 2 * 2 * 120 = 480 mm^2 whatever the loft shape.
H2_MERIDIAN = 2 * H2_T * H2_H
H2_MERIDIAN_TOL = 0.05 * 2 * H2_H       # ±0.05 mm of mean horizontal wall = ±12 mm^2
H2_LEVELS = (30.0, 60.0, 90.0)
#: Horizontal ring area through the transition: 2 mm times a perimeter that moves from the
#: rectangle's (320) to the circle's (251.3); the end rings bound it, 3 % slack.
H2_LEVEL_BAND = (H2_ROUND_RING * 0.97, H2_RECT_RING * 1.03)


@_gated("H2.solid")
def h2_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("H2.envelope")
def h2_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _box(cid, ev, (-H2_RECT[0] / 2, -H2_ROUND_D / 2, 0.0),
                (H2_RECT[0] / 2, H2_ROUND_D / 2, H2_H),
                "rectangle 100 × 60 at z 0, Ø80 at z 120, centred on the Z axis")


@_gated("H2.rect_collar")
def h2_rect_collar(ev: Evidence, cid: str) -> CheckResult:
    """Exactly 8 side flats: outer x=±50 / y=±30 and inner x=±48 / y=±28, each 10 mm tall
    (centroid z 5) — a straight 2 mm rectangular collar."""
    side = _flats(ev, X) + _flats(ev, Y)
    a, b, t, c = H2_RECT[0] / 2, H2_RECT[1] / 2, H2_T, H2_COLLAR
    zc = c / 2
    want = [(0, s * a, 2 * b * c, zc) for s in (1, -1)] + \
           [(1, s * b, 2 * a * c, zc) for s in (1, -1)] + \
           [(0, s * (a - t), 2 * (b - t) * c, zc) for s in (1, -1)] + \
           [(1, s * (b - t), 2 * (a - t) * c, zc) for s in (1, -1)]
    ok, miss = _match_faces(side, want, key_axis=2)
    return _result(cid, ok, {"side_flats": [_face_row(f) for f in side], "unmatched": miss},
                   "outer x ±50 (600), y ±30 (1000); inner x ±48 (560), y ±28 (960); "
                   "centroid z 5", {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H2.round_collar")
def h2_round_collar(ev: Evidence, cid: str) -> CheckResult:
    """Ø80 / Ø76 walls on the Z axis, 10 mm long (full-revolution area) and ending at the
    z 120 face. The z 110 end is not read from an end circle: the edge shared with the loft
    comes back as a B-spline, not a circle, so only the free end carries one."""
    cyls = gc.cylinders(ev)
    lo, hi = H2_H - H2_COLLAR, H2_H

    def collar(c: gc.Cylinder) -> bool:
        return (c.span is not None and abs(c.span[1] - hi) <= TOL_MM
                and abs(c.length_mm - H2_COLLAR) <= TOL_MM and _on_line(c, Z, (0.0, 0.0, 0.0)))

    boss = [c for c in cyls if c.kind == "boss" and _dia(c, H2_ROUND_D) and collar(c)]
    bore = [c for c in cyls if c.kind == "bore" and _dia(c, H2_ROUND_D - 2 * H2_T) and collar(c)]
    ok = len(boss) == 1 and len(bore) == 1 and len(cyls) == 2
    return _result(cid, ok, _cj(cyls),
                   f"Ø80 boss and Ø76 bore on the Z axis over z {lo:g}..{hi:g}, no other "
                   "cylinder", TOL_MM)


@_gated("H2.open_ends")
def h2_open_ends(ev: Evidence, cid: str) -> CheckResult:
    """The only Z-normal flats are the two end rings: 100×60 - 96×56 = 624 at z 0 and
    pi/4*(80^2-76^2) at z 120 (no cap, ledge or internal bulkhead)."""
    flats = _flats(ev, Z)
    ok, miss = _match_faces(flats, [(2, 0.0, H2_RECT_RING), (2, H2_H, H2_ROUND_RING)])
    return _result(cid, ok, {"z_flats": [_face_row(f) for f in flats], "unmatched": miss},
                   {"z0_ring": H2_RECT_RING, "z120_ring": round(H2_ROUND_RING, 4)},
                   {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H2.wall")
def h2_wall(ev: Evidence, cid: str) -> CheckResult:
    """Wall samples: the two meridian sections (vertical planes through the axis along X and
    along Y) each cut two strips whose total area is 2 * t * 120, so t is their mean
    horizontal wall over the full height; three horizontal rings through the transition
    stay inside the band the end rings set."""
    meridians = {name: _section(ev, (0.0, 0.0, H2_H / 2), n)["area_mm2"]
                 for name, n in (("xz_plane", Y), ("yz_plane", X))}
    levels = {z: _section(ev, (0.0, 0.0, z), Z)["area_mm2"] for z in H2_LEVELS}
    ok = (all(abs(a - H2_MERIDIAN) <= H2_MERIDIAN_TOL for a in meridians.values())
          and all(H2_LEVEL_BAND[0] <= a <= H2_LEVEL_BAND[1] for a in levels.values()))
    return _result(cid, ok,
                   {"meridian_area": {k: round(v, 3) for k, v in meridians.items()},
                    "mean_wall_mm": {k: round(v / (2 * H2_H), 4) for k, v in meridians.items()},
                    "ring_area": {str(k): round(v, 3) for k, v in levels.items()}},
                   {"meridian_area": H2_MERIDIAN,
                    "ring_band": [round(v, 2) for v in H2_LEVEL_BAND]},
                   {"meridian_mm2": H2_MERIDIAN_TOL})


@_gated("H2.lofted")
def h2_lofted(ev: Evidence, cid: str) -> CheckResult:
    return _has_live(cid, ev, "loft")


@_gated("H2.history")
def h2_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


H2 = Task(
    id="H2", version=1, domain="cad",
    prompt=(
        "Model a duct adapter as ONE solid with its axis along Z, a 2 mm wall and both ends "
        "open. Bottom: a straight rectangular collar, 100 mm along X by 60 mm along Y outside, "
        "from z = 0 to z = 10. Top: a straight round collar, Ø80 mm outside, from z = 110 to "
        "z = 120. Between z = 10 and z = 110 a lofted transition blends the rectangle into the "
        "circle. Everything is centred on the Z axis, and the wall is 2 mm thick in every "
        "horizontal cross-section (inside the collars: 96 × 56 mm and Ø76 mm). No caps, "
        "ledges or internal walls. Build the transition with a loft feature, as an editable "
        "parametric part, and save it in Chudware's native format as `deliverable.step+` in "
        "the working directory."
    ),
    checks=(h2_solid, h2_envelope, h2_rect_collar, h2_round_collar, h2_open_ends, h2_wall,
            h2_lofted, h2_history),
    budget_s=1200,
    notes=("Oracle (world frame): bbox (-50,-40,0)..(50,40,120). Rect collar: exactly 8 side "
           "flats, outer x ±50 (area 600), y ±30 (1000), inner x ±48 (560), y ±28 (960), all "
           "with centroid z 5 (10 tall). Round collar: Ø80 boss + Ø76 bore on the Z axis, "
           "10 long (full-revolution area) ending on a circle at z 120 (the z 110 edge shared "
           "with the loft reads back as a B-spline, so it carries no end circle), no other "
           "cylinder. Open ends: the only Z-normal flats "
           "are the rings 624 at z 0 and 490.0885 at z 120 (±1 mm^2). Wall: meridian sections "
           "(planes y=0 and x=0) each 2*2*120 = 480 ±12 mm^2 (mean horizontal wall 2 ±0.05 "
           "over the full height — exact for any loft shape because the area is the integral "
           "of the strip widths); rings at z 30/60/90 within [475.39, 642.72] (2 mm times a "
           "perimeter between the circle's and the rectangle's; reference 598.6/559.2/518.2). "
           "A 1.5 or 2.5 mm wall misses the meridian by 120 mm^2; an internal bulkhead adds its "
           "full span. No volume oracle: the loft surface between a rectangle and a circle "
           "has no closed form. H2.lofted: a live loft."),
)


# ============================================================================ H3

H3_L, H3_F = 150.0, 400.0                     # arm length along X, tip load (N, -Z)
H3_E, H3_NU, H3_YIELD = 68_900.0, 0.33, 276.0  # 6061-T6, MPa
H3_SIGMA_MAX, H3_DELTA_MAX, H3_B_MIN = 80.0, 0.5, 8.0
H3_MASS_FACTOR = 1.25
H3_HOLE_D, H3_HOLE_X = 5.5, 140.0
H3_FEA_REL = 0.25
H3_STUDY_TYPES = ("fea_static", "fea_adaptive")
H3_NONSINGULAR = "node_averaged_excluding_fixture_ring2"
H3_LIMIT_REL = 1e-3                           # measurement slack on the two design limits
#: Minimum area: with b fixed, sigma needs b h^2 >= 6FL/sigma and delta needs
#: b h^3 >= 4FL^3/(E delta); A = b h grows with b under either (A ~ b^(1/2), b^(2/3)), so
#: the optimum sits at b = 8 with the governing (larger) h.
H3_H_STRESS = math.sqrt(6 * H3_F * H3_L / (H3_SIGMA_MAX * H3_B_MIN))              # 23.717
H3_H_DEFL = (4 * H3_F * H3_L ** 3 / (H3_E * H3_DELTA_MAX * H3_B_MIN)) ** (1 / 3)  # 26.957
H3_A_MIN = H3_B_MIN * max(H3_H_STRESS, H3_H_DEFL)                                  # 215.66


def _h3_section(ev: Evidence) -> dict:
    """b (Y) and h (Z) from the exact bbox, plus the mid-span section area."""
    bb = gc.part_bbox(ev)
    c = _centre(bb)
    b, h = bb["size"][1], bb["size"][2]
    area = _section(ev, (H3_L / 2, c[1], c[2]), X)["area_mm2"]
    return {"bb": bb, "b": b, "h": h, "area": area}


def _h3_sigma(b: float, h: float) -> float:
    return 6 * H3_F * H3_L / (b * h * h)


def _h3_delta(b: float, h: float) -> float:
    return 4 * H3_F * H3_L ** 3 / (H3_E * b * h ** 3)


def _h3_study(ev: Evidence) -> dict | None:
    hits = [s for s in ev.studies if s.get("type") in H3_STUDY_TYPES]
    return hits[-1] if hits else None


def _no_study(cid: str, ev: Evidence) -> CheckResult:
    return CheckResult(cid, Status.FAIL, measured=[s.get("type") for s in ev.studies],
                       expected=f"a persisted study of type {H3_STUDY_TYPES}",
                       detail="no static stress study saved in the deliverable")


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _vec(x: Any) -> gc.Vec | None:
    if not isinstance(x, (list, tuple)) or len(x) != 3:
        return None
    vals = [_num(c) for c in x]
    return None if any(v is None for v in vals) else (vals[0], vals[1], vals[2])


def _rel(measured: float, expected: float) -> float:
    return abs(measured - expected) / abs(expected)


def _faces_of(ev: Evidence, queries: list) -> tuple[list[dict], list[str]]:
    faces: list[dict] = []
    bad: list[str] = []
    for q in queries:
        hit = gc.resolve_faces(ev, q)
        if not hit:
            bad.append(f"{q!r} resolves to no face")
        faces.extend(hit)
    return faces, bad


@_gated("H3.solid")
def h3_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("H3.envelope")
def h3_envelope(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    ok = abs(bb["min"][0]) <= TOL_MM and abs(bb["max"][0] - H3_L) <= TOL_MM
    return _result(cid, ok, {"x": [round(bb["min"][0], 4), round(bb["max"][0], 4)]},
                   {"x": [0.0, H3_L]}, TOL_MM, "arm runs x 0..150")


@_gated("H3.section")
def h3_section(ev: Evidence, cid: str) -> CheckResult:
    """A solid rectangle b × h at mid-span (section area = bbox Y × Z) with b >= 8."""
    s = _h3_section(ev)
    rect = s["b"] * s["h"]
    ok = abs(s["area"] - rect) / rect <= VOLUME_REL and s["b"] >= H3_B_MIN - TOL_MM
    return _result(cid, ok, {"b_y": round(s["b"], 4), "h_z": round(s["h"], 4),
                             "section_area": round(s["area"], 3), "b_x_h": round(rect, 3)},
                   {"section_area": "b*h", "b_min": H3_B_MIN},
                   {"area": f"{VOLUME_REL:.1%}", "b": TOL_MM})


@_gated("H3.stress_limit")
def h3_stress_limit(ev: Evidence, cid: str) -> CheckResult:
    s = _h3_section(ev)
    sig = _h3_sigma(s["b"], s["h"])
    return _result(cid, sig <= H3_SIGMA_MAX * (1 + H3_LIMIT_REL), round(sig, 3),
                   f"<= {H3_SIGMA_MAX:g} MPa", f"{H3_LIMIT_REL:.1%}",
                   f"6FL/(b h^2) at the root, b={s['b']:.4f} h={s['h']:.4f}")


@_gated("H3.deflection_limit")
def h3_deflection_limit(ev: Evidence, cid: str) -> CheckResult:
    s = _h3_section(ev)
    d = _h3_delta(s["b"], s["h"])
    return _result(cid, d <= H3_DELTA_MAX * (1 + H3_LIMIT_REL), round(d, 5),
                   f"<= {H3_DELTA_MAX:g} mm", f"{H3_LIMIT_REL:.1%}",
                   f"FL^3/(3EI), E={H3_E:g} MPa, b={s['b']:.4f} h={s['h']:.4f}")


@_gated("H3.mass")
def h3_mass(ev: Evidence, cid: str) -> CheckResult:
    s = _h3_section(ev)
    a = s["b"] * s["h"]
    return _result(cid, a <= H3_MASS_FACTOR * H3_A_MIN, round(a, 3),
                   f"<= {H3_MASS_FACTOR:g} × {H3_A_MIN:.3f} mm^2", None,
                   f"section area (mass per length) vs the lightest feasible section "
                   f"(b {H3_B_MIN:g}, h {max(H3_H_STRESS, H3_H_DEFL):.4f}); ratio "
                   f"{a / H3_A_MIN:.4f}")


@_gated("H3.hole")
def h3_hole(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    c = _centre(bb)
    bores = gc.bores(ev)
    hits = [b for b in gc.along(bores, Z) if _dia(b, H3_HOLE_D)
            and gc.spans_exactly(b, bb["min"][2], bb["max"][2], TOL_MM)]
    pos = None
    ok = len(bores) == 1 and len(hits) == 1
    if ok:
        p = hits[0].center_in_plane(Z)
        pos = [round(p[0], 4), round(p[1] - c[1], 4)]
        ok = abs(pos[0] - H3_HOLE_X) <= TOL_MM and abs(pos[1]) <= TOL_MM
    return _result(cid, ok, {"bores": _cj(bores), "x_and_y_from_centre": pos},
                   f"the only bore: Ø{H3_HOLE_D:g} through Z at x {H3_HOLE_X:g}, centred in Y",
                   TOL_MM)


@_gated("H3.study_saved")
def h3_study_saved(ev: Evidence, cid: str) -> CheckResult:
    st = _h3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    res = st.get("results") if isinstance(st.get("results"), dict) else {}
    need = {k: _num(res.get(k)) for k in ("max_disp_mm", "max_von_mises_mpa")}
    ok = all(v is not None for v in need.values()) and isinstance(st.get("reactions"), dict)
    return _result(cid, ok, {"id": st.get("id"), "type": st.get("type"), **need,
                             "reactions": isinstance(st.get("reactions"), dict)},
                   "solved study with results and reactions", None)


@_gated("H3.material")
def h3_material(ev: Evidence, cid: str) -> CheckResult:
    st = _h3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    mat = st.get("material") if isinstance(st.get("material"), dict) else {}
    e, nu, sy = _num(mat.get("E_mpa")), _num(mat.get("nu")), _num(mat.get("yield_mpa"))
    ok = (e is not None and _rel(e, H3_E) <= 0.01 and nu is not None
          and abs(nu - H3_NU) <= 0.02 and sy is not None and _rel(sy, H3_YIELD) <= 0.02)
    return _result(cid, ok, {"name": mat.get("name"), "E_mpa": e, "nu": nu, "yield_mpa": sy},
                   {"E_mpa": H3_E, "nu": H3_NU, "yield_mpa": H3_YIELD},
                   {"E": "1%", "nu": 0.02, "yield": "2%"})


@_gated("H3.fixture")
def h3_fixture(ev: Evidence, cid: str) -> CheckResult:
    st = _h3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    fixtures = st.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        return _result(cid, False, fixtures, "the x=0 end face", None, "no fixtures stored")
    s = _h3_section(ev)
    faces, bad = _faces_of(ev, fixtures)
    wrong = [f.get("sid") for f in faces
             if not gc.planar_end_face(f, (-1, 0, 0), 0, 0.0, TOL_MM)]
    area = sum(_area(f) for f in faces)
    ok = bool(faces) and not bad and not wrong and abs(area - s["b"] * s["h"]) <= 0.5
    return _result(cid, ok, {"queries": fixtures, "faces": len(faces),
                             "area_mm2": round(area, 3), "off_target": wrong},
                   f"exactly the x=0 end face ({s['b'] * s['h']:.3f} mm^2) fixed", None,
                   "; ".join(bad))


@_gated("H3.load")
def h3_load(ev: Evidence, cid: str) -> CheckResult:
    st = _h3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    loads = st.get("loads")
    if not isinstance(loads, list) or not loads:
        return _result(cid, False, loads, "400 N -Z on the x=150 face", None, "no loads stored")
    total = [0.0, 0.0, 0.0]
    faults: list[str] = []
    for i, ld in enumerate(loads):
        if not isinstance(ld, dict):
            faults.append(f"loads[{i}] is not an object")
            continue
        vec = _vec(ld.get("force"))
        if vec is None:
            faults.append(f"loads[{i}] is not a force vector ({sorted(ld)})")
            continue
        total = [t + v for t, v in zip(total, vec)]
        faces, bad = _faces_of(ev, [ld.get("face")])
        faults += [f"loads[{i}]: {b}" for b in bad]
        faults += [f"loads[{i}] face {f.get('sid')} is not the x={H3_L:g} end face"
                   for f in faces if not gc.planar_end_face(f, (1, 0, 0), 0, H3_L, TOL_MM)]
    err = math.dist(total, (0.0, 0.0, -H3_F))
    ok = not faults and err <= 0.01 * H3_F
    return _result(cid, ok, {"total_force_n": [round(t, 4) for t in total], "loads": loads},
                   [0.0, 0.0, -H3_F], "1% of |F|", "; ".join(faults))


@_gated("H3.fea_stress")
def h3_fea_stress(ev: Evidence, cid: str) -> CheckResult:
    """The study's non-singular peak von Mises agrees with beam theory for the MEASURED
    section within 25 %."""
    st = _h3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    s = _h3_section(ev)
    beam = _h3_sigma(s["b"], s["h"])
    res = st.get("results") or {}
    vm = _num(res.get("max_von_mises_mpa"))
    red = res.get("max_von_mises_reduction")
    faults = []
    if red != H3_NONSINGULAR or not _num(res.get("summary_nodes_excluded")):
        faults.append(f"stress reduction {red!r} is not the non-singular {H3_NONSINGULAR!r}")
    if vm is None or _rel(vm, beam) > H3_FEA_REL:
        faults.append(f"von Mises {vm} MPa outside {H3_FEA_REL:.0%} of beam theory {beam:.3f}")
    return _result(cid, not faults, {"max_von_mises_mpa": vm, "reduction": red},
                   round(beam, 4), f"{H3_FEA_REL:.0%}", "; ".join(faults))


@_gated("H3.history")
def h3_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


H3 = Task(
    id="H3", version=1, domain="sim",
    prompt=(
        "Design a straight cantilever arm of solid rectangular cross-section in 6061-T6 "
        "aluminium (E = 68.9 GPa, Poisson's ratio 0.33, yield strength 276 MPa). It runs "
        "150 mm along +X, from x = 0 to x = 150 mm; its section is b wide along Y and h tall "
        "along Z. It is fully fixed at the x = 0 end face and carries a 400 N force in the "
        "-Z direction on the x = 150 mm end face. Choose b and h so that, by Euler-Bernoulli "
        "beam theory, the maximum bending stress is at most 80 MPa and the tip deflection is "
        "at most 0.5 mm, with b at least 8 mm, while making the arm as light as possible "
        "(its mass must be within 25 % of the lightest section that meets these limits). Add "
        "an M5 clearance hole, Ø5.5 mm, through the arm along Z at x = 140 mm, centred "
        "across the width. Then run a linear static stress study of exactly that load case "
        "(fixture, load and material as stated) and keep the solved study saved in the part. "
        "Build it as an editable parametric part and save it in Chudware's native format as "
        "`deliverable.step+` in the working directory."
    ),
    checks=(h3_solid, h3_envelope, h3_section, h3_stress_limit, h3_deflection_limit, h3_mass,
            h3_hole, h3_study_saved, h3_material, h3_fixture, h3_load, h3_fea_stress,
            h3_history),
    budget_s=1800,
    notes=("Oracle: b = bbox Y, h = bbox Z (load along -Z, so h is the bending depth); the "
           "mid-span (x 75) section area must equal b*h (solid rectangle, 0.5 %). sigma = "
           "6FL/(b h^2), delta = 4FL^3/(E b h^3) with F 400 N, L 150, E 68 900 MPa; limits 80 "
           "MPa and 0.5 mm (0.1 % measurement slack). Lightest section: sigma needs b h^2 >= "
           "4500 mm^3, delta needs b h^3 >= 156 748.9 mm^4; at fixed b, A = b h grows with b "
           "under both (A ∝ b^(1/2) resp. b^(2/3)), so b = 8 and h = max(23.717, 26.957) = "
           "26.957: A_min = 215.66 mm^2 (deflection governs); H3.mass needs b*h <= 1.25 * "
           "215.66 = 269.57 mm^2 (mass per length; the Ø5.5 hole is the same feature on every "
           "design). The hole sits at x 140 where M = 400*10 = 4000 N mm: even at b 8 the net "
           "section stress is 6M/((b-5.5) h^2) <= 17.1 MPa (x Kt ~3 = 51 MPa) < 80, so beam "
           "theory at the root governs and the hole is graded only for size/position (the only "
           "bore: Ø5.5 ±0.05, through z_min..z_max, x 140 ±0.05, centred in Y ±0.05). Study "
           "(last fea_static/fea_adaptive): material E 68 900 (1 %), nu 0.33 (±0.02), yield "
           "276 (2 %); fixtures re-resolve to only the x=0 face (area b*h ±0.5 mm^2); loads sum "
           "to (0,0,-400) N (1 %) on only the x=150 face; non-singular von Mises "
           "(node_averaged_excluding_fixture_ring2) within 25 % of 6FL/(b h^2) for the measured "
           "section. Reference b 10 × h 25.2: sigma 56.69, delta 0.4897, A 252 (1.1685 × "
           "A_min); FEA 53.34 MPa (-5.9 %), FEA tip 0.5014 mm (shear + clamp), so the limits are "
           "graded on beam theory as the prompt states."),
)


# ============================================================================ H4

H4_LEN, H4_PIN_D = 40.0, 4.0
H4_KNUCKLE_D, H4_BORE_D = 10.0, 4.2
H4_PLATE = (40.0, 30.0, 3.0)                  # X, Y, Z
H4_RELIEF = 6.0                               # no plate within 6 mm of the axis there
H4_SPAN = 10.0                                # each knuckle's X length
H4_FOLD_DEG = 90.0
H4_SWEEP_STEPS = 7                            # 15° samples over the fold
H4_PIN_VOLUME = math.pi * (H4_PIN_D / 2) ** 2 * H4_LEN                         # 502.6548
#: One leaf in section (Y, Z about the pin axis), per 10 mm span:
#:   own-knuckle spans (2): knuckle disk ∪ plate strip minus the Ø4.2 bore,
#:   area = 25pi + 30*3 - [lens: |z| <= 1.5, 0 <= y <= 5 inside r 5] - pi*2.1^2;
#:   relieved spans (2): plate strip y 6..30 = 24*3.
_H4_LENS = (1.5 * math.sqrt(25.0 - 1.5 ** 2) + 25.0 * math.asin(1.5 / 5.0))    # 14.7719 mm^2
H4_LEAF_VOLUME = (2 * H4_SPAN * (math.pi * (H4_KNUCKLE_D / 2) ** 2 + H4_PLATE[1] * H4_PLATE[2]
                                 - _H4_LENS - math.pi * (H4_BORE_D / 2) ** 2)
                  + 2 * H4_SPAN * (H4_PLATE[1] - H4_RELIEF) * H4_PLATE[2])      # 4238.2706
H4_FIT = (H4_BORE_D - H4_PIN_D) / 2           # 0.1 mm radial clearance pin <-> knuckle bores
H4_FIT_TOL = 0.02


def _h4_parts(ev: Evidence) -> dict:
    """{pin: the smallest-volume world body (None without bodies), rest: the others}. The
    pin is told apart by role, not by its size, so a wrong-sized pin is graded as a pin."""
    bodies = sorted(ev.bodies, key=lambda b: _num(b.get("volume_mm3")) or 0.0)
    return {"pin": bodies[0] if bodies else None, "rest": bodies[1:]}


def _bb(b: dict) -> tuple[gc.Vec, gc.Vec]:
    bb = b.get("bbox") or {}
    return tuple(float(v) for v in bb["min"]), tuple(float(v) for v in bb["max"])  # type: ignore[return-value]


def _row_major(t: Sequence[float]) -> tuple[list[list[float]], list[float]]:
    return ([[float(t[0]), float(t[1]), float(t[2])], [float(t[4]), float(t[5]), float(t[6])],
             [float(t[8]), float(t[9]), float(t[10])]], [float(t[3]), float(t[7]), float(t[11])])


def _delta(t0: Sequence[float], t1: Sequence[float]) -> tuple[list[list[float]], list[float]]:
    """World motion D = T1 * T0^-1 of a body between two poses (rotation, translation)."""
    r0, p0 = _row_major(t0)
    r1, p1 = _row_major(t1)
    r = [[sum(r1[i][k] * r0[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
    p = [p1[i] - sum(r[i][k] * p0[k] for k in range(3)) for i in range(3)]
    return r, p


def _apply(r: list[list[float]], p: list[float], v: Sequence[float]) -> list[float]:
    return [sum(r[i][k] * v[k] for k in range(3)) + p[i] for i in range(3)]


def _rotation(r: list[list[float]]) -> tuple[float, gc.Vec | None]:
    """(angle°, unit axis or None for a null rotation)."""
    c = max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1.0) / 2.0))
    ang = math.degrees(math.acos(c))
    w = (r[2][1] - r[1][2], r[0][2] - r[2][0], r[1][0] - r[0][1])
    n = math.sqrt(sum(v * v for v in w))
    return ang, (tuple(v / n for v in w) if n > 1e-9 else None)  # type: ignore[return-value]


def _h4_pin_axis(ev: Evidence) -> tuple[gc.Vec, gc.Vec] | None:
    pin = _h4_parts(ev)["pin"]
    if pin is None:
        return None
    lo, hi = _bb(pin)
    yc, zc = (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2
    return (lo[0], yc, zc), (hi[0], yc, zc)


def _h4_drive(ev: Evidence) -> dict:
    """Drive every live angle mate +/-90° from its saved value with the native solver and
    return the first sweep that folds exactly one leaf 90° about the pin axis (with its
    interference verdict), plus every attempt for the record."""
    axis = _h4_pin_axis(ev)
    parts = _h4_parts(ev)
    leaf_ids = {b.get("component") for b in parts["rest"]}
    pin_id = (parts["pin"] or {}).get("component")
    attempts: list[dict] = []
    if axis is None or len(leaf_ids) != 2:
        return {"ok": False, "attempts": attempts, "reason": "needs one pin and two leaves"}
    mates = [m for m in ev.tree.get("mates") or [] if isinstance(m, dict)
             and m.get("type") == "angle" and not m.get("suppressed")]
    for m in mates:
        v0 = _num((m.get("value") or {}).get("value")) if isinstance(m.get("value"), dict) \
            else _num(m.get("value"))
        v0 = 0.0 if v0 is None else v0
        for sign in (1.0, -1.0):
            row: dict = {"mate": m.get("id"), "start": v0, "stop": v0 + sign * H4_FOLD_DEG}
            attempts.append(row)
            try:
                sm = ev.rpc("simulate_motion", {"mate_id": m.get("id"), "start": v0,
                                                "stop": v0 + sign * H4_FOLD_DEG,
                                                "steps": H4_SWEEP_STEPS,
                                                "check_interference": True})
            except Exception as exc:  # noqa: BLE001 — an undrivable mate is a fact, not a crash
                row["error"] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            traj = sm.get("trajectory") or []
            if not sm.get("ok") or len(traj) < 2 or not all(f.get("converged") for f in traj):
                row["error"] = f"sweep not ok/converged (ok={sm.get('ok')}, frames={len(traj)})"
                continue
            first = {b.get("component"): b.get("transform") for b in traj[0].get("bodies") or []}
            last = {b.get("component"): b.get("transform") for b in traj[-1].get("bodies") or []}
            moved, motions = [], {}
            for cid_ in first:
                if cid_ not in last:
                    continue
                r, p = _delta(first[cid_], last[cid_])
                ang, ax = _rotation(r)
                shift = max(math.dist(_apply(r, p, q), q) for q in axis)
                motions[cid_] = {"angle_deg": round(ang, 4),
                                 "axis": [round(v, 5) for v in ax] if ax else None,
                                 "pin_axis_shift_mm": round(shift, 5)}
                if ang > ANGLE_TOL_DEG or math.hypot(*p) > TOL_MM:
                    moved.append(cid_)
            row["motions"] = motions
            moving_leaves = [c for c in moved if c in leaf_ids]
            others_ok = all(c == pin_id and motions[c]["pin_axis_shift_mm"] <= TOL_MM
                            for c in moved if c not in leaf_ids)
            if len(moving_leaves) != 1 or not others_ok:
                row["error"] = f"moved {moved}: need exactly one leaf to fold"
                continue
            mv = motions[moving_leaves[0]]
            if not (abs(mv["angle_deg"] - H4_FOLD_DEG) <= AXIS_DEG and mv["axis"] is not None
                    and gc.angle_deg(mv["axis"], X) <= AXIS_DEG
                    and mv["pin_axis_shift_mm"] <= TOL_MM):
                row["error"] = "leaf does not turn 90° about the pin axis"
                continue
            return {"ok": True, "mate": m.get("id"), "leaf": moving_leaves[0], "motion": mv,
                    "interference": sm.get("interference"), "attempts": attempts}
    return {"ok": False, "attempts": attempts,
            "reason": "no live angle mate folds one leaf 90° about the pin axis"
                      if mates else "no live angle mate"}


@_gated("H4.assembly")
def h4_assembly(ev: Evidence, cid: str) -> CheckResult:
    """An assembly of exactly three loaded components, one sound solid each."""
    comps = [c for c in ev.tree.get("components") or [] if isinstance(c, dict)
             and not c.get("suppressed")]
    bodies = ev.bodies
    per = {}
    for b in bodies:
        per.setdefault(b.get("component"), []).append(b)
    ok = (len(comps) == 3 and len(bodies) == 3 and len(per) == 3
          and all(b.get("valid") and b.get("closed") for b in bodies)
          and {c.get("id") for c in comps} == set(per) and not ev.probe_errors)
    return _result(cid, ok, {"components": [{k: c.get(k) for k in ("id", "name", "fixed",
                                                                     "source")} for c in comps],
                             "bodies": [{k: b.get(k) for k in ("component", "name", "valid",
                                                               "closed", "volume_mm3")}
                                        for b in bodies],
                             "probe_errors": ev.probe_errors},
                   "assembly: 3 components (pin + 2 leaves), each one valid closed solid", None)


@_gated("H4.pin")
def h4_pin(ev: Evidence, cid: str) -> CheckResult:
    parts = _h4_parts(ev)
    pin = parts["pin"]
    if pin is None:
        return _result(cid, False, {"bodies": 0}, "one Ø4 × 40 body", None)
    lo, hi = _bb(pin)
    v = _num(pin.get("volume_mm3")) or 0.0
    ok = (gc.within(lo, (0.0, -H4_PIN_D / 2, -H4_PIN_D / 2), TOL_MM)
          and gc.within(hi, (H4_LEN, H4_PIN_D / 2, H4_PIN_D / 2), TOL_MM)
          and abs(v - H4_PIN_VOLUME) / H4_PIN_VOLUME <= VOLUME_REL)
    return _result(cid, ok, {"min": [round(c, 4) for c in lo], "max": [round(c, 4) for c in hi],
                             "volume_mm3": round(v, 3)},
                   {"min": [0.0, -2.0, -2.0], "max": [H4_LEN, 2.0, 2.0],
                    "volume_mm3": round(H4_PIN_VOLUME, 4)},
                   {"mm": TOL_MM, "volume": f"{VOLUME_REL:.1%}"},
                   "Ø4 × 40 pin on the X axis, x 0..40")


@_gated("H4.leaves")
def h4_leaves(ev: Evidence, cid: str) -> CheckResult:
    """Two leaves at the flat-open pose: one spanning y -5..30, the other y -30..5, both
    x 0..40 and z -5..5 (plate mid-plane on the pin axis), each with the leaf volume."""
    rest = _h4_parts(ev)["rest"]
    r = H4_KNUCKLE_D / 2
    want = {"+y": ((0.0, -r, -r), (H4_LEN, H4_PLATE[1], r)),
            "-y": ((0.0, -H4_PLATE[1], -r), (H4_LEN, r, r))}
    rows, found = [], set()
    for b in rest:
        lo, hi = _bb(b)
        v = _num(b.get("volume_mm3")) or 0.0
        side = next((k for k, (wl, wh) in want.items()
                     if gc.within(lo, wl, TOL_MM) and gc.within(hi, wh, TOL_MM)), None)
        vol_ok = abs(v - H4_LEAF_VOLUME) / H4_LEAF_VOLUME <= VOLUME_REL
        if side and vol_ok:
            found.add(side)
        rows.append({"component": b.get("component"), "min": [round(c, 4) for c in lo],
                     "max": [round(c, 4) for c in hi], "volume_mm3": round(v, 3),
                     "side": side, "volume_ok": vol_ok})
    ok = len(rest) == 2 and found == set(want)
    return _result(cid, ok, rows, {"bboxes": {k: [list(a), list(b)] for k, (a, b) in want.items()},
                                   "volume_mm3": round(H4_LEAF_VOLUME, 4)},
                   {"mm": TOL_MM, "volume": f"{VOLUME_REL:.1%}"})


@_gated("H4.pin_fit")
def h4_pin_fit(ev: Evidence, cid: str) -> CheckResult:
    """The pin runs in BOTH leaves' Ø4.2 bores: exact pin-to-leaf clearance 0.1 mm each."""
    rep = ev.rpc("eval_interference", {})
    parts = _h4_parts(ev)
    pin = (parts["pin"] or {}).get("component")
    rows = [c for c in rep.get("clearances") or []
            if pin is not None and pin in (c.get("component_a"), c.get("component_b"))]
    dists = [_num(c.get("distance")) for c in rows]
    ok = (pin is not None and len(rows) == 2
          and all(d is not None and abs(d - H4_FIT) <= H4_FIT_TOL for d in dists))
    return _result(cid, ok, [{"pair": [c.get("a"), c.get("b")], "distance": c.get("distance")}
                             for c in rows], H4_FIT, H4_FIT_TOL,
                   "minimum distance pin <-> each leaf (radial bore clearance)")


@_gated("H4.revolute")
def h4_revolute(ev: Evidence, cid: str) -> CheckResult:
    d = _h4_drive(ev)
    return _result(cid, d["ok"], {k: d.get(k) for k in ("mate", "leaf", "motion", "reason",
                                                         "attempts")},
                   "a live angle mate that folds exactly one leaf 90° about the pin axis "
                   "(native simulate_motion)", {"deg": AXIS_DEG, "axis_shift_mm": TOL_MM})


@_gated("H4.clearance")
def h4_clearance(ev: Evidence, cid: str) -> CheckResult:
    """No overlap between any two parts at the saved (0°) pose and at every 15° sample of
    the 90° fold (touching faces are allowed)."""
    rep = ev.rpc("eval_interference", {})
    static_ok = (bool(rep.get("complete")) and not rep.get("interferences")
                 and not rep.get("pair_failures") and not rep.get("geometry_failures"))
    d = _h4_drive(ev)
    itf = d.get("interference") or {}
    sweep_ok = (d["ok"] and bool(itf.get("checked")) and bool(itf.get("complete"))
                and itf.get("first_collision_step") is None and not itf.get("pair_failures")
                and not itf.get("geometry_failures"))
    return _result(cid, static_ok and sweep_ok,
                   {"saved_pose": {"interferences": rep.get("interferences"),
                                   "complete": rep.get("complete")},
                    "fold": {"driven": d["ok"], "mate": d.get("mate"),
                             "frames_checked": itf.get("frames_checked"),
                             "first_collision_step": itf.get("first_collision_step"),
                             "complete": itf.get("complete")}},
                   "no interference at 0° and over the 0..90° fold", None,
                   "" if d["ok"] else f"fold not drivable: {d.get('reason')}")


H4 = Task(
    id="H4", version=1, domain="cad",
    prompt=(
        "Model a hinge as an ASSEMBLY of three separate parts. Save each part as its own "
        "native part file in the working directory, and save the assembly (whose components "
        "reference those part files) in Chudware's native format as `deliverable.step+` in "
        "the working directory. Coordinates are in the assembly frame, hinge flat open (0°):\n"
        "- Pin: a Ø4 mm × 40 mm cylinder on the X axis, from x = 0 to x = 40.\n"
        "- Leaf A (grounded): a 3 mm plate 40 mm along X (x 0..40) by 30 mm along Y (y 0..30), "
        "its mid-plane at z = 0, plus two knuckles: Ø10 mm cylinders on the X axis spanning "
        "x 0..10 and x 20..30.\n"
        "- Leaf B: the same plate on the other side (y -30..0, mid-plane z = 0) with its "
        "knuckles spanning x 10..20 and x 30..40.\n"
        "- Each knuckle has a Ø4.2 mm through bore on the X axis for the pin, and each leaf's "
        "plate is cut back so that it has no material within 6 mm of the X axis over the "
        "OTHER leaf's knuckle spans.\n"
        "Mate it: leaf A fixed, the pin held on the hinge axis, and leaf B on a revolute "
        "joint about the pin axis implemented as an angle mate (drivable, 0° in this "
        "flat-open pose). Leaf B must be able to fold 90° with no interference between any "
        "two parts at any angle from 0° to 90°."
    ),
    checks=(h4_assembly, h4_pin, h4_leaves, h4_pin_fit, h4_revolute, h4_clearance),
    budget_s=1800,
    notes=("Assembly deliverable: components reference sibling part files (saved relative "
           "to the assembly, so the moved workspace still opens). Oracle at the saved pose: "
           "exactly 3 live components, 3 valid closed world bodies (one per component). Pin: "
           "the smallest-volume body, at (0,-2,-2)..(40,2,2), V = pi*2^2*40 = "
           "502.6548 (0.5 %). Leaves: the other two, bboxes (0,-5,-5)..(40,30,5) and "
           "(0,-30,-5)..(40,5,5), each V = 2*10*(25pi + 90 - lens - pi*2.1^2) + 2*10*24*3 = "
           "4238.2706 mm^3 (lens = 1.5*sqrt(22.75) + 25*asin(0.3) = 14.7719, the plate/knuckle "
           "overlap). Pin fit: eval_interference exact clearance pin<->each leaf 0.1 ±0.02 mm. "
           "Revolute: some live angle mate, driven by the native simulate_motion from its saved "
           "value by +90° or -90° (7 samples), moves exactly one leaf (the pin may only spin "
           "about its own axis) by a rotation of 90 ±0.5° whose axis is ∥ X ±0.5° and which "
           "keeps the pin axis line fixed (±0.05 mm); the other leaf stays put. Clearance: "
           "eval_interference at the saved pose has no interference (touching knuckle ends "
           "are contact, not overlap) and that sweep's interference is complete with no "
           "collision at any sample. No history check: an assembly document has no part "
           "features of its own."),
)


# ============================================================================ H5

H5_FIXTURE = "h5_flange.step+"
#: Bytes the grader was written against; regenerate via fixtures/build_h5_flange.py.
H5_FIXTURE_SHA256 = "b74c39e4b31f94bfd8bf02206a5d6c45fe526dbe1ee02f0fca107bdab2779abe"
H5_D, H5_T, H5_BORE_D = 110.0, 12.0, 40.0
H5_HOLES, H5_HOLE_D, H5_PCD = 8, 9.0, 80.0
H5_PITCH_DEG = 360.0 / H5_HOLES
H5_CH = 1.0
#: The only parameters the redesign may change, as (feature id, param) -> new value.
H5_EDITS = {("f1", "radius"): H5_D / 2, ("f3", "count"): H5_HOLES, ("f3", "pcd"): H5_PCD}
#: pi*12*(55^2 - 20^2 - 8*4.5^2) - 2*pi*(55 - 1/3)*1^2: the disc minus bore and holes, minus
#: two 45° chamfer rings (triangle 1^2/2 revolved at its centroid radius 55 - 1/3).
H5_VOLUME = (math.pi * H5_T * ((H5_D / 2) ** 2 - (H5_BORE_D / 2) ** 2
                               - H5_HOLES * (H5_HOLE_D / 2) ** 2)
             - 2 * math.pi * (H5_D / 2 - H5_CH / 3) * H5_CH ** 2)               # 92 509.4317


@functools.lru_cache(maxsize=1)
def _h5_reference() -> dict:
    """The pristine fixture's feature history (suite copy, digest verified)."""
    path = FIXTURES_DIR / H5_FIXTURE
    digest = sha256_file(path)
    if digest != H5_FIXTURE_SHA256:
        raise RuntimeError(f"fixture {path.name} sha256 {digest} != declared "
                           f"{H5_FIXTURE_SHA256}; regenerate and re-declare it")
    ref = build_evidence(path)
    if ref.doc_id is None or not ref.features:
        raise RuntimeError(f"fixture {path.name} did not open: "
                           f"{ref.engine_error or ref.open_error or ref.probe_errors}")
    return {"features": [{k: f.get(k) for k in ("id", "type", "name", "params", "refs")}
                         for f in ref.features]}


def _h5_frame(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder],
                                     list[gc.Cylinder]]:
    """(bbox, every bore, Z through bores on the axis, Z through bores off it)."""
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


def _h5_polar(ev: Evidence) -> list[tuple[float, float]]:
    bb, _bores, _axial, ring = _h5_frame(ev)
    cx, cy, _ = _centre(bb)
    out = []
    for b in ring:
        if _dia(b, H5_HOLE_D):
            p = b.center_in_plane(Z)
            dx, dy = p[0] - cx, p[1] - cy
            out.append((math.hypot(dx, dy), math.degrees(math.atan2(dy, dx)) % 360.0))
    return sorted(out, key=lambda t: t[1])


@_gated("H5.fixture_declared")
def h5_fixture_declared(ev: Evidence, cid: str) -> CheckResult:
    """Grader integrity: the reference fixture is the declared one (mismatch raises -> ERROR)."""
    feats = _h5_reference()["features"]
    return _result(cid, True, H5_FIXTURE_SHA256, H5_FIXTURE_SHA256, 0,
                   f"{len(feats)} reference features")


@_gated("H5.solid")
def h5_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("H5.envelope")
def h5_envelope(ev: Evidence, cid: str) -> CheckResult:
    r, t = H5_D / 2, H5_T / 2
    return _box(cid, ev, (-r, -r, -t), (r, r, t), "Ø110 × 12 where the fixture put it")


@_gated("H5.bore")
def h5_bore(ev: Evidence, cid: str) -> CheckResult:
    _bb, _bores, axial, _ring = _h5_frame(ev)
    ok = len(axial) == 1 and _dia(axial[0], H5_BORE_D)
    return _result(cid, ok, _cj(axial), f"one Ø{H5_BORE_D:g} Z through bore on the axis",
                   TOL_MM)


@_gated("H5.holes")
def h5_holes(ev: Evidence, cid: str) -> CheckResult:
    _bb, bores, axial, ring = _h5_frame(ev)
    others = [b for b in bores if b not in axial and b not in ring]
    ok = len(ring) == H5_HOLES and all(_dia(b, H5_HOLE_D) for b in ring) and not others
    return _result(cid, ok, {"off_axis_through": [round(b.diameter, 4) for b in ring],
                             "other_bores": _cj(others)},
                   f"exactly {H5_HOLES} off-axis Ø{H5_HOLE_D:g} Z through holes, no other "
                   "bores", TOL_MM)


@_gated("H5.pcd")
def h5_pcd(ev: Evidence, cid: str) -> CheckResult:
    polar = _h5_polar(ev)
    ok = bool(polar) and all(abs(r - H5_PCD / 2) <= TOL_MM for r, _a in polar)
    return _result(cid, ok, [round(r, 4) for r, _a in polar], H5_PCD / 2, TOL_MM,
                   "radius of each Ø9 hole axis from the flange axis")


@_gated("H5.spacing")
def h5_spacing(ev: Evidence, cid: str) -> CheckResult:
    angles = [a for _r, a in _h5_polar(ev)]
    gaps = [round((angles[(i + 1) % len(angles)] - angles[i]) % 360.0, 4)
            for i in range(len(angles))] if len(angles) > 1 else []
    ok = (len(angles) == H5_HOLES and all(abs(g - H5_PITCH_DEG) <= ANGLE_TOL_DEG for g in gaps)
          and any(min(a, 360.0 - a) <= ANGLE_TOL_DEG for a in angles))
    return _result(cid, ok, {"angles": [round(a, 4) for a in angles], "gaps": gaps},
                   {"pitch": H5_PITCH_DEG, "first": 0.0}, ANGLE_TOL_DEG,
                   "45° pitch with one hole on +X")


@_gated("H5.chamfers")
def h5_chamfers(ev: Evidence, cid: str) -> CheckResult:
    """Exactly two cones, one at each face of the rim: each bounded by an r55 circle 1 mm in
    from the face and an r54 circle on it (1 × 45°), and the Ø110 wall spans between them."""
    bb = gc.part_bbox(ev)
    cx, cy, _ = _centre(bb)
    z0, z1 = bb["min"][2], bb["max"][2]
    cones = [c for c in gc.resolve_faces(ev, {"of": "face", "surf": "cone"})
             if c.get("axis") and gc.angle_deg(c["axis"], Z) <= AXIS_DEG]
    circles = [e for e in gc.circle_edges(ev)
               if e["axis"] and gc.angle_deg(e["axis"], Z) <= AXIS_DEG
               and math.hypot(e["centroid"][0] - cx, e["centroid"][1] - cy) <= TOL_MM]
    r = H5_D / 2

    def ring(rad: float, z: float) -> bool:
        return any(abs(e["radius"] - rad) <= TOL_MM and abs(e["centroid"][2] - z) <= TOL_MM
                   for e in circles)

    rim = [c for c in gc.cylinders(ev) if c.kind == "boss" and _dia(c, H5_D)
           and gc.spans_exactly(c, z0 + H5_CH, z1 - H5_CH, TOL_MM)]
    ok = (len(cones) == 2 and len(rim) == 1
          and ring(r, z0 + H5_CH) and ring(r - H5_CH, z0)
          and ring(r, z1 - H5_CH) and ring(r - H5_CH, z1))
    return _result(cid, ok, {"cones": [{"centroid": c.get("centroid"), "area": c.get("area_or_len")}
                                       for c in cones],
                             "rim_wall": _cj(rim),
                             "rim_circles": [{"r": round(e["radius"], 4),
                                              "z": round(e["centroid"][2], 4)}
                                             for e in circles if e["radius"] > r - 2]},
                   {"cones": 2, "r55_at": [z0 + H5_CH, z1 - H5_CH], "r54_at": [z0, z1]},
                   TOL_MM)


@_gated("H5.history_kept")
def h5_history_kept(ev: Evidence, cid: str) -> CheckResult:
    """The revision went through the fixture's history: f1/f2/f3 survive with their types,
    names and refs; only Disk.radius, Bolts.count and Bolts.pcd changed (to 55, 8, 80); any
    added feature is a chamfer."""
    ref = _h5_reference()["features"]
    faults = gc.history_faults(ev)
    got = {f.get("id"): f for f in ev.features}
    want_ids = [f["id"] for f in ref]
    added = [f for f in ev.features if f.get("id") not in want_ids]
    faults += [f"added {f.get('id')}:{f.get('type')} (only chamfers may be added)"
               for f in added if f.get("type") != "chamfer"]
    if not added:
        faults.append("no chamfer feature added")
    for r in ref:
        f = got.get(r["id"])
        if f is None:
            faults.append(f"{r['id']} ({r['type']} {r['name']!r}) missing")
            continue
        if f.get("suppressed"):
            faults.append(f"{r['id']} suppressed")
        for key in ("type", "name", "refs"):
            if f.get(key) != r[key]:
                faults.append(f"{r['id']}.{key} changed: {r[key]!r} -> {f.get(key)!r}")
        keys = set(r["params"] or {}) | set(f.get("params") or {})
        for k in sorted(keys):
            want = H5_EDITS.get((r["id"], k), gc.param_value(r, k))
            have = gc.param_value(f, k)
            same = (have == want if not (isinstance(have, (int, float))
                                         and isinstance(want, (int, float)))
                    else abs(float(have) - float(want)) <= 1e-6)
            if not same:
                faults.append(f"{r['id']}.{k} = {have!r}, expected {want!r}")
    return _result(cid, not faults, [f"{f.get('id')}:{f.get('type')}" for f in ev.features],
                   f"fixture history {want_ids} (+ chamfer) with "
                   f"{ {f'{a}.{b}': v for (a, b), v in H5_EDITS.items()} }", None,
                   "; ".join(faults))


@_gated("H5.volume")
def h5_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, H5_VOLUME)


H5 = Task(
    id="H5", version=1, domain="cad",
    prompt=(
        "The working directory contains `h5_flange.step+`, a native, parametric mounting "
        "flange: Ø100 mm × 12 mm along Z, a Ø40 mm through bore, and six Ø9 mm through holes "
        "on a Ø75 mm bolt circle with the first hole on +X. Revise it by editing its existing "
        "feature history — keep its existing features (do not delete, rebuild or re-create "
        "them): make the outside diameter 110 mm; make the bolt pattern eight Ø9 mm through "
        "holes equally spaced on a Ø80 mm bolt circle, first hole still on +X; keep the Ø40 "
        "bore and the 12 mm thickness; and add a 1 mm × 45° chamfer on both outer circular "
        "edges of the Ø110 rim (top and bottom). Leave the part where it is. Save the revised "
        "part in Chudware's native format as `deliverable.step+` in the working directory; "
        "leave `h5_flange.step+` as the untouched input."
    ),
    checks=(h5_fixture_declared, h5_solid, h5_envelope, h5_bore, h5_holes, h5_pcd, h5_spacing,
            h5_chamfers, h5_history_kept, h5_volume),
    fixtures=(H5_FIXTURE,),
    budget_s=1200,
    notes=("Fixture (sha256 declared above; builder fixtures/build_h5_flange.py) is the M2 "
           "reference: f1 Disk cylinder r50 h12 (z -6..6), f2 Bore hole Ø40 through_all, f3 "
           "Bolts bolt_circle 6 × Ø9 PCD 75 start 0°. Oracle after the revision: bbox "
           "(-55,-55,-6)..(55,55,6); one Ø40 Z through bore on the axis; exactly 8 off-axis Ø9 "
           "Z through holes, no other bore, axes 40 ±0.05 from the axis at 45k ±0.1°; exactly "
           "2 Z cones, r55 circles at z_min+1 / z_max-1 and r54 circles at z_min / z_max (legs "
           "1 and 1), the Ø110 wall spanning exactly z_min+1..z_max-1. History: f1/f2/f3 keep "
           "type, name and refs; every param equals the fixture's except f1.radius = 55, "
           "f3.count = 8, f3.pcd = 80; only chamfer features may be added (at least one); "
           "nothing fails to rebuild. V = pi*12*(55^2-20^2-8*4.5^2) - 2*pi*(55-1/3)*1^2 = "
           "92 509.4317 mm^3 (secondary; without chamfers +343.5)."),
)


# ============================================================================ H6

H6_SIZE = (120.0, 80.0, 40.0)
H6_T, H6_R = 2.5, 5.0                     # wall, outer corner radius (inner = R - t)
H6_BOSS_D, H6_BOSS_H = 6.0, 30.0          # above the inside floor
H6_PILOT_D, H6_PILOT_DEPTH = 2.5, 25.0
H6_BOSSES = [(sx * 50.0, sy * 30.0) for sx in (1, -1) for sy in (1, -1)]
_H6_RI = H6_R - H6_T


def _rrect(a: float, b: float, r: float) -> float:
    return a * b - (4 - math.pi) * r * r


H6_OUTER = _rrect(H6_SIZE[0], H6_SIZE[1], H6_R)                                # 9 578.5398
H6_INNER = _rrect(H6_SIZE[0] - 2 * H6_T, H6_SIZE[1] - 2 * H6_T, _H6_RI)        # 8 619.6350
H6_RIM = H6_OUTER - H6_INNER                                                    # 958.9049
H6_FLOOR = H6_INNER - len(H6_BOSSES) * math.pi * (H6_BOSS_D / 2) ** 2          # 8 506.5381
#: outer prism - cavity + bosses - pilots (flat-bottomed; a drill point is ~1.2 mm^3 each)
H6_VOLUME = (H6_OUTER * H6_SIZE[2] - H6_INNER * (H6_SIZE[2] - H6_T)
             + len(H6_BOSSES) * math.pi * ((H6_BOSS_D / 2) ** 2 * H6_BOSS_H
                                           - (H6_PILOT_D / 2) ** 2 * H6_PILOT_DEPTH))  # 62 807.33


def _h6_frame(ev: Evidence) -> tuple[dict, gc.Vec]:
    bb = gc.part_bbox(ev)
    return bb, _centre(bb)


@_gated("H6.solid")
def h6_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("H6.envelope")
def h6_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, H6_SIZE)


@_gated("H6.walls")
def h6_walls(ev: Evidence, cid: str) -> CheckResult:
    """Exactly 8 side flats: the outer faces on the bbox and the inner faces 2.5 mm inside,
    the inner ones floor-to-rim (37.5 tall: centroid z_min+21.25), all with the straight
    length the corner radii leave (side - 10)."""
    bb, c = _h6_frame(ev)
    (x0, y0, z0), (x1, y1, z1) = bb["min"], bb["max"]
    h, hi = H6_SIZE[2], H6_SIZE[2] - H6_T
    lx, ly = H6_SIZE[1] - 2 * H6_R, H6_SIZE[0] - 2 * H6_R    # straight lengths of X/Y walls
    want = [(0, x0, lx * h, z0 + h / 2), (0, x1, lx * h, z0 + h / 2),
            (1, y0, ly * h, z0 + h / 2), (1, y1, ly * h, z0 + h / 2),
            (0, x0 + H6_T, lx * hi, z0 + H6_T + hi / 2), (0, x1 - H6_T, lx * hi, z0 + H6_T + hi / 2),
            (1, y0 + H6_T, ly * hi, z0 + H6_T + hi / 2), (1, y1 - H6_T, ly * hi, z0 + H6_T + hi / 2)]
    side = _flats(ev, X) + _flats(ev, Y)
    ok, miss = _match_faces(side, want, key_axis=2)
    return _result(cid, ok, {"side_flats": [_face_row(f) for f in side], "unmatched": miss},
                   "outer walls on the bbox, inner walls 2.5 inside, floor to rim",
                   {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H6.floor_and_top")
def h6_floor_and_top(ev: Evidence, cid: str) -> CheckResult:
    """Solid bottom (one full rounded-rect face at z_min), inside floor 2.5 above it (around
    the boss feet), and an open top: the only face at z_max is the 2.5 mm rim."""
    bb, _c = _h6_frame(ev)
    z0, z1 = bb["min"][2], bb["max"][2]
    zf = _flats(ev, Z)
    at = {name: [f for f in zf if abs(float(f["centroid"][2]) - z) <= TOL_MM]
          for name, z in (("bottom", z0), ("floor", z0 + H6_T), ("top", z1))}
    want = {"bottom": H6_OUTER, "floor": H6_FLOOR, "top": H6_RIM}
    ok = all(len(at[k]) == 1 and abs(_area(at[k][0]) - a) <= AREA_TOL_MM2
             for k, a in want.items())
    return _result(cid, ok, {k: [round(_area(f), 4) for f in v] for k, v in at.items()},
                   {k: round(v, 4) for k, v in want.items()}, AREA_TOL_MM2,
                   "face areas at z_min, z_min+2.5, z_max")


@_gated("H6.corners")
def h6_corners(ev: Evidence, cid: str) -> CheckResult:
    """Four convex R5 rounds full height on the outer vertical corners and four concave
    R2.5 rounds (floor to rim) on the same axes inside: a uniform wall round the corners."""
    bb, _c = _h6_frame(ev)
    (x0, y0, z0), (x1, y1, _z1) = bb["min"], bb["max"]
    axes = [(x0 + H6_R, y0 + H6_R), (x0 + H6_R, y1 - H6_R), (x1 - H6_R, y0 + H6_R),
            (x1 - H6_R, y1 - H6_R)]
    faces = [f for f in gc.resolve_faces(ev, {"of": "face", "surf": "cylinder"})
             if f.get("axis") is not None and gc.angle_deg(f["axis"], Z) <= AXIS_DEG]
    rows, ok = [], True
    for r, kind, length in ((H6_R, "boss", H6_SIZE[2]), (_H6_RI, "bore", H6_SIZE[2] - H6_T)):
        hit = [f for f in faces if abs(float(f["radius"]) - r) <= TOL_MM
               and ("bore" if f.get("concavity") in ("bore", "concave") else "boss") == kind]
        found = []
        for f in hit:
            cz = float(f["centroid"][2])
            near = min(axes, key=lambda a: math.dist(a, [float(v) for v in f["centroid"][:2]]))
            p, sweep = gc.arc_axis_point(f, length, (near[0], near[1], cz))
            found.append({"axis_xy": [round(p[0], 4), round(p[1], 4)],
                          "sweep_deg": round(sweep, 3), "area": round(_area(f), 4)})
        m = gc.match_points([fr["axis_xy"] for fr in found], axes, TOL_MM)
        quarter = math.pi / 2 * r * length
        ok = ok and (len(hit) == 4 and m["matched"] == 4 and not m["extra"]
                     and all(abs(fr["area"] - quarter) <= AREA_TOL_MM2 for fr in found))
        rows.append({"r": r, "kind": kind, "faces": found, "match": m})
    return _result(cid, ok, rows,
                   {"outer": f"4 × R{H6_R:g} convex, {H6_SIZE[2]:g} tall",
                    "inner": f"4 × R{_H6_RI:g} concave, {H6_SIZE[2] - H6_T:g} tall",
                    "axes_from_bbox_corner": [H6_R, H6_R]},
                   {"mm": TOL_MM, "area_mm2": AREA_TOL_MM2})


@_gated("H6.bosses")
def h6_bosses(ev: Evidence, cid: str) -> CheckResult:
    bb, (cx, cy, _cz) = _h6_frame(ev)
    z0 = bb["min"][2]
    bosses = [b for b in gc.along(gc.cylinders(ev), Z) if b.kind == "boss" and _dia(b, H6_BOSS_D)]
    good = [b for b in bosses if gc.spans_exactly(b, z0 + H6_T, z0 + H6_T + H6_BOSS_H, TOL_MM)]
    pts = [(p[0] - cx, p[1] - cy) for p in (b.center_in_plane(Z) for b in good)]
    m = gc.match_points(pts, H6_BOSSES, TOL_MM)
    ok = len(bosses) == 4 and len(good) == 4 and m["matched"] == 4 and not m["extra"]
    return _result(cid, ok, {"bosses": _cj(bosses), "match": m},
                   f"4 × Ø{H6_BOSS_D:g} at (±50, ±30) from the centre, z_min+2.5..z_min+32.5",
                   TOL_MM)


@_gated("H6.pilots")
def h6_pilots(ev: Evidence, cid: str) -> CheckResult:
    bb, (cx, cy, _cz) = _h6_frame(ev)
    z0 = bb["min"][2]
    top = z0 + H6_T + H6_BOSS_H
    pilots = [b for b in gc.along(gc.bores(ev), Z) if _dia(b, H6_PILOT_D)]
    good = [b for b in pilots if gc.spans_exactly(b, top - H6_PILOT_DEPTH, top, TOL_MM)]
    pts = [(p[0] - cx, p[1] - cy) for p in (b.center_in_plane(Z) for b in good)]
    m = gc.match_points(pts, H6_BOSSES, TOL_MM)
    ok = len(pilots) == 4 and len(good) == 4 and m["matched"] == 4 and not m["extra"]
    return _result(cid, ok, {"pilots": _cj(pilots), "match": m},
                   f"4 × Ø{H6_PILOT_D:g} on the boss axes, {H6_PILOT_DEPTH:g} deep from the "
                   "boss tops", TOL_MM)


@_gated("H6.volume")
def h6_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, H6_VOLUME)


@_gated("H6.history")
def h6_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


H6 = Task(
    id="H6", version=1, domain="cad",
    prompt=(
        "Model an open-top enclosure as one solid: outside 120 mm along X by 80 mm along Y by "
        "40 mm tall along Z, base flat on the bottom. Round its four vertical outside edges "
        "R5. Hollow it to a uniform 2.5 mm wall — floor and side walls, so the inside "
        "vertical corners are R2.5 — open at the top (+Z). Inside, four cylindrical screw "
        "bosses Ø6 mm stand on the floor at X/Y offsets (±50, ±30) mm from the enclosure's "
        "centre, each rising 30 mm above the inside floor surface, each with a Ø2.5 mm pilot "
        "hole 25 mm deep from the boss top. Build it as an editable parametric part and save "
        "it in Chudware's native format as `deliverable.step+` in the working directory."
    ),
    checks=(h6_solid, h6_envelope, h6_walls, h6_floor_and_top, h6_corners, h6_bosses,
            h6_pilots, h6_volume, h6_history),
    budget_s=1500,
    notes=("Oracle (bbox-relative): bbox 120×80×40. Walls: exactly 8 side flats — outer at the "
           "bbox faces (X walls 70×40 = 2800, Y walls 110×40 = 4400, centroid z_min+20) and "
           "inner 2.5 inside them (70×37.5 = 2625, 110×37.5 = 4125, centroid z_min+21.25: floor "
           "to rim). Floor/top: one Z face at z_min of the full rounded rectangle 120*80 - "
           "(4-pi)*25 = 9578.5398, one at z_min+2.5 of the inner rounded rectangle 115*75 - "
           "(4-pi)*6.25 minus 4 boss feet = 8506.5381, and one at z_max, the rim 958.9049 "
           "(open top), ±1 mm^2. Corners: 4 convex R5 Z quarter-rounds 40 tall (area "
           "pi/2*5*40 = 314.1593) and 4 concave R2.5 37.5 tall (147.2622), axes at "
           "(x_min+5, y_min+5) etc. ±0.05 (gc.arc_axis_point). Bosses: exactly 4 Ø6 Z bosses "
           "spanning z_min+2.5..z_min+32.5 at (±50, ±30) from the bbox centre. Pilots: exactly "
           "4 Ø2.5 Z bores spanning z_min+7.5..z_min+32.5 on the boss axes. V = 9578.5398*40 - "
           "8619.6350*37.5 + 4*pi*(9*30 - 1.5625*25) = 62 807.33 mm^3 (secondary)."),
)


TASKS: tuple[Task, ...] = (H1, H2, H3, H4, H5, H6)

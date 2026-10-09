"""Solid-modeling tasks S1-S3, migrated from the retired prompt set (mod-plate-hole,
mod-cube-boss, mod-shell-box) with artifact checks that pin design intent, not just
volume: hole/boss location and size, the open face and the wall thickness are measured
on the reopened ``deliverable.step+``.

Retired prompts NOT migrated (no checkable artifact): mod-hole-grid became C1
(tasks/core.py); sim-cantilever became C3; sim-mass-cube and ctl-self-intersection asked
for a number in prose; dwg-* and ecad-* have no declared native deliverable (README.md).
"""
from __future__ import annotations

import functools
import math
from typing import Any, Callable

from benchmarks.artifact_validity import geometry_checks as gc
from benchmarks.artifact_validity.evidence import Evidence, gate
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

Z: gc.Vec = (0.0, 0.0, 1.0)
TOL_MM = 0.05
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
                   list(size), TOL_MM, "bbox X×Y×Z in mm")


def _volume(cid: str, ev: Evidence, expected: float) -> CheckResult:
    v = gc.volume_mm3(ev)
    return _result(cid, abs(v - expected) / expected <= VOLUME_REL, round(v, 3),
                   round(expected, 4), f"{VOLUME_REL:.1%}", "secondary invariant")


def _history(cid: str, ev: Evidence) -> CheckResult:
    faults = gc.history_faults(ev)
    return _result(cid, not faults, [f"{f['id']}:{f['type']}" for f in ev.features],
                   "editable feature history, every feature rebuilt", None,
                   "; ".join(faults))


def _bbox_centre(ev: Evidence) -> tuple[dict, gc.Vec]:
    bb = gc.part_bbox(ev)
    return bb, tuple((a + b) / 2.0 for a, b in zip(bb["min"], bb["max"]))  # type: ignore[return-value]


def _planar(ev: Evidence, normal: gc.Vec, axis: int, coord: float) -> list[dict]:
    return [f for f in gc.resolve_faces(ev, {"of": "face"})
            if gc.planar_end_face(f, normal, axis, coord, TOL_MM)]


def _area(faces: list[dict]) -> float:
    return round(sum(float(f.get("area_or_len") or 0.0) for f in faces), 4)


# ============================================================================ S1

S1_SIZE = (90.0, 50.0, 12.0)
S1_HOLE_D = 10.0
#: 90*50*12 - pi*5^2*12
S1_VOLUME = 90.0 * 50.0 * 12.0 - math.pi * 5.0 ** 2 * 12.0   # 53 057.5222 mm^3


def _s1_through(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder]]:
    bb = gc.part_bbox(ev)
    bores = gc.bores(ev)
    through = [b for b in gc.along(bores, Z)
               if gc.spans(b, bb["min"][2], bb["max"][2], TOL_MM)]
    return bb, bores, through


@_gated("S1.solid")
def s1_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("S1.plate_size")
def s1_plate_size(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, S1_SIZE)


@_gated("S1.hole")
def s1_hole(ev: Evidence, cid: str) -> CheckResult:
    _bb, bores, through = _s1_through(ev)
    others = [b.to_json() for b in bores if b not in through]
    ds = [round(b.diameter, 4) for b in through]
    ok = len(through) == 1 and not others and abs(through[0].diameter - S1_HOLE_D) <= TOL_MM
    return _result(cid, ok, {"through_z_diameters": ds, "other_bores": others},
                   f"exactly one Z through bore of Ø{S1_HOLE_D:g}", TOL_MM)


@_gated("S1.hole_centre")
def s1_hole_centre(ev: Evidence, cid: str) -> CheckResult:
    bb, _bores, through = _s1_through(ev)
    if len(through) != 1:
        return _result(cid, False, len(through), 1, None, "needs exactly one through bore")
    _bb, c = _bbox_centre(ev)
    p = through[0].center_in_plane(Z)
    off = [round(p[0] - c[0], 4), round(p[1] - c[1], 4)]
    return _result(cid, math.hypot(*off) <= TOL_MM, off, [0.0, 0.0], TOL_MM,
                   "XY offset of the hole axis from the plate centre")


@_gated("S1.volume")
def s1_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, S1_VOLUME)


@_gated("S1.history")
def s1_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


S1 = Task(
    id="S1", version=1, domain="cad",
    prompt=(
        "Model a flat steel plate 90 mm along X, 50 mm along Y and 12 mm thick along Z, "
        "with a single 10 mm diameter through hole parallel to Z at the centre of the "
        "plate. Build it as an editable parametric part and save it in Chudware's native "
        "format as `deliverable.step+` in the working directory."
    ),
    checks=(s1_solid, s1_plate_size, s1_hole, s1_hole_centre, s1_volume, s1_history),
    budget_s=600,
    notes=("Migrated from mod-plate-hole. Oracle V = 90*50*12 - pi*5^2*12 = 53 057.5222 "
           "mm^3 (secondary). Hole: concave Z cylinder spanning both plate faces, Ø10 "
           "+-0.05; centre = axis crossing relative to the bbox centre, +-0.05 mm."),
)


# ============================================================================ S2

S2_CUBE = 40.0
S2_BOSS_D, S2_BOSS_H = 12.0, 15.0
S2_SIZE = (S2_CUBE, S2_CUBE, S2_CUBE + S2_BOSS_H)
#: 40^3 + pi*6^2*15
S2_VOLUME = S2_CUBE ** 3 + math.pi * (S2_BOSS_D / 2) ** 2 * S2_BOSS_H   # 65 696.4602 mm^3
#: The cube's top face minus the boss footprint.
S2_SHOULDER = S2_CUBE ** 2 - math.pi * (S2_BOSS_D / 2) ** 2               # 1 486.9027 mm^2


def _s2_boss(ev: Evidence) -> tuple[dict, list[gc.Cylinder]]:
    return gc.part_bbox(ev), gc.cylinders(ev)


@_gated("S2.solid")
def s2_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("S2.envelope")
def s2_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, S2_SIZE)


@_gated("S2.boss")
def s2_boss(ev: Evidence, cid: str) -> CheckResult:
    bb, cyls = _s2_boss(ev)
    zmax = bb["max"][2]
    bosses = [c for c in gc.along(cyls, Z) if c.kind == "boss"
              and gc.spans(c, zmax - S2_BOSS_H, zmax, TOL_MM)
              and abs(c.length_mm - S2_BOSS_H) <= TOL_MM]
    ok = (len(cyls) == 1 and len(bosses) == 1
          and abs(bosses[0].diameter - S2_BOSS_D) <= TOL_MM)
    return _result(cid, ok, [c.to_json() for c in cyls],
                   f"exactly one Ø{S2_BOSS_D:g} Z boss spanning the top {S2_BOSS_H:g} mm",
                   TOL_MM)


@_gated("S2.boss_centre")
def s2_boss_centre(ev: Evidence, cid: str) -> CheckResult:
    _bb, cyls = _s2_boss(ev)
    z_bosses = [c for c in gc.along(cyls, Z) if c.kind == "boss"]
    if len(z_bosses) != 1:
        return _result(cid, False, len(z_bosses), 1, None, "needs exactly one Z boss")
    _bb2, c = _bbox_centre(ev)
    p = z_bosses[0].center_in_plane(Z)
    off = [round(p[0] - c[0], 4), round(p[1] - c[1], 4)]
    return _result(cid, math.hypot(*off) <= TOL_MM, off, [0.0, 0.0], TOL_MM,
                   "XY offset of the boss axis from the cube centre")


@_gated("S2.top_face")
def s2_top_face(ev: Evidence, cid: str) -> CheckResult:
    """The boss stands on the cube's +Z face: that face remains as an annular shoulder."""
    bb = gc.part_bbox(ev)
    shoulder = _planar(ev, Z, 2, bb["max"][2] - S2_BOSS_H)
    area = _area(shoulder)
    return _result(cid, abs(area - S2_SHOULDER) <= AREA_TOL_MM2, area,
                   round(S2_SHOULDER, 4), AREA_TOL_MM2,
                   "+Z cube face area around the boss footprint")


@_gated("S2.volume")
def s2_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, S2_VOLUME)


@_gated("S2.history")
def s2_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


S2 = Task(
    id="S2", version=1, domain="cad",
    prompt=(
        "Create a 40 mm cube (edges along X, Y and Z) with a 12 mm diameter, 15 mm tall "
        "cylindrical boss standing on the centre of its top (+Z) face, as one solid. "
        "Build it as an editable parametric part and save it in Chudware's native format "
        "as `deliverable.step+` in the working directory."
    ),
    checks=(s2_solid, s2_envelope, s2_boss, s2_boss_centre, s2_top_face, s2_volume,
            s2_history),
    budget_s=600,
    notes=("Migrated from mod-cube-boss. Oracle V = 40^3 + pi*6^2*15 = 65 696.4602 mm^3; "
           "shoulder = 40^2 - pi*6^2 = 1 486.9027 mm^2 at z_max-15. Boss = convex Z "
           "cylinder Ø12 +-0.05 whose end circles sit at z_max-15 and z_max."),
)


# ============================================================================ S3

S3_EDGE, S3_WALL = 50.0, 3.0
S3_INNER = S3_EDGE - 2 * S3_WALL                 # 44
S3_DEPTH = S3_EDGE - S3_WALL                     # 47
#: 50^3 - 44*44*47
S3_VOLUME = S3_EDGE ** 3 - S3_INNER ** 2 * S3_DEPTH   # 34 008 mm^3
S3_RIM = S3_EDGE ** 2 - S3_INNER ** 2                 # 564 mm^2
S3_FLOOR = S3_INNER ** 2                              # 1 936 mm^2
S3_INNER_WALL = S3_INNER * S3_DEPTH                   # 2 068 mm^2


@_gated("S3.solid")
def s3_solid(ev: Evidence, cid: str) -> CheckResult:
    return _solid(cid, ev)


@_gated("S3.envelope")
def s3_envelope(ev: Evidence, cid: str) -> CheckResult:
    return _size(cid, ev, (S3_EDGE,) * 3)


@_gated("S3.open_top")
def s3_open_top(ev: Evidence, cid: str) -> CheckResult:
    """Only the +Z face is removed: the top is a rim, the bottom a full face."""
    bb = gc.part_bbox(ev)
    top = _area(_planar(ev, Z, 2, bb["max"][2]))
    bottom = _area(_planar(ev, (0.0, 0.0, -1.0), 2, bb["min"][2]))
    ok = abs(top - S3_RIM) <= AREA_TOL_MM2 and abs(bottom - S3_EDGE ** 2) <= AREA_TOL_MM2
    return _result(cid, ok, {"top": top, "bottom": bottom},
                   {"top": S3_RIM, "bottom": S3_EDGE ** 2}, AREA_TOL_MM2,
                   "outward +Z face area (rim) and -Z face area (closed)")


@_gated("S3.wall_thickness")
def s3_wall_thickness(ev: Evidence, cid: str) -> CheckResult:
    """Floor and four inner walls sit exactly 3 mm inside the outer faces."""
    bb = gc.part_bbox(ev)
    lo, hi = bb["min"], bb["max"]
    rows = {
        "floor": (_area(_planar(ev, Z, 2, lo[2] + S3_WALL)), S3_FLOOR),
        "-x": (_area(_planar(ev, (1.0, 0.0, 0.0), 0, lo[0] + S3_WALL)), S3_INNER_WALL),
        "+x": (_area(_planar(ev, (-1.0, 0.0, 0.0), 0, hi[0] - S3_WALL)), S3_INNER_WALL),
        "-y": (_area(_planar(ev, (0.0, 1.0, 0.0), 1, lo[1] + S3_WALL)), S3_INNER_WALL),
        "+y": (_area(_planar(ev, (0.0, -1.0, 0.0), 1, hi[1] - S3_WALL)), S3_INNER_WALL),
    }
    ok = all(abs(got - want) <= AREA_TOL_MM2 for got, want in rows.values())
    return _result(cid, ok, {k: v[0] for k, v in rows.items()},
                   {k: v[1] for k, v in rows.items()},
                   {"area_mm2": AREA_TOL_MM2, "position_mm": TOL_MM},
                   "inner faces 3 mm inside each outer face (areas in mm²)")


@_gated("S3.volume")
def s3_volume(ev: Evidence, cid: str) -> CheckResult:
    return _volume(cid, ev, S3_VOLUME)


@_gated("S3.history")
def s3_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


S3 = Task(
    id="S3", version=1, domain="cad",
    prompt=(
        "Make a 50 x 50 x 50 mm box (edges along X, Y and Z) and hollow it out to a "
        "uniform 3 mm wall thickness, leaving the top (+Z) face open. Build it as an "
        "editable parametric part and save it in Chudware's native format as "
        "`deliverable.step+` in the working directory."
    ),
    checks=(s3_solid, s3_envelope, s3_open_top, s3_wall_thickness, s3_volume, s3_history),
    budget_s=600,
    notes=("Migrated from mod-shell-box. Oracle V = 50^3 - 44*44*47 = 34 008 mm^3; rim "
           "50^2-44^2 = 564 mm^2; floor 44^2 = 1 936 mm^2 at z_min+3; each inner wall "
           "44*47 = 2 068 mm^2 at 3 mm from its outer face. Areas +-1 mm^2, planes "
           "+-0.05 mm."),
)


TASKS: tuple[Task, ...] = (S1, S2, S3)

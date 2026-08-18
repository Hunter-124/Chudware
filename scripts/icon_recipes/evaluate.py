"""Icon recipes: Evaluate ribbon tab (measure / analysis / diagnostics)."""
import math

from icon_recipes.helpers import *  # noqa: F401,F403

STEEL = (0.66, 0.69, 0.73)


def steel(body_id: str = "b1") -> dict:
    """Steel-grey stock body appearance."""
    return {"op": "set_appearance", "id": body_id, "color": list(STEEL),
            "metallic": 0.85, "roughness": 0.38}


def paint(body_id: str, rgb, metallic: float, roughness: float) -> dict:
    return {"op": "set_appearance", "id": body_id, "color": list(rgb),
            "metallic": metallic, "roughness": roughness}


def rect(x0, y0, x1, y1, pfx) -> list:
    """Closed 4-line rectangular loop."""
    p = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [{"type": "line", "p1": list(p[i]), "p2": list(p[(i + 1) % 4]),
             "id": f"{pfx}{i}"} for i in range(4)]


def quad(pts, pfx) -> list:
    """Closed loop through an arbitrary list of 2D points."""
    n = len(pts)
    return [{"type": "line", "p1": [float(pts[i][0]), float(pts[i][1])],
             "p2": [float(pts[(i + 1) % n][0]), float(pts[(i + 1) % n][1])],
             "id": f"{pfx}{i}"} for i in range(n)]


def ext(name, fid, cx, cy, depth, merge=True, mode="boss") -> dict:
    f = {"type": "extrude", "name": name,
         "params": {"mode": mode, "depth": dim(depth)},
         "refs": {"profile": [region(fid, cx, cy)]}}
    if not merge:
        f["merge"] = False
    return {"op": "add_feature", "feature": f}


# --- zebra: 6 touching arc-chord plates sweeping a gentle curve -----------------------
_ZR, _ZSPAN, _ZLEN, _ZH = 40.0, 12.0, 26.0, 4.0
_zebra_steps: list = []
for _i in range(6):
    _a0 = math.radians(90.0 + (_i - 3) * _ZSPAN)
    _a1 = math.radians(90.0 + (_i - 2) * _ZSPAN)
    _pts = []
    for _r in (_ZR - _ZLEN / 2, _ZR + _ZLEN / 2):
        _pts.append((_r * math.cos(_a0), _r * math.sin(_a0)))
    for _r in (_ZR + _ZLEN / 2, _ZR - _ZLEN / 2):
        _pts.append((_r * math.cos(_a1), _r * math.sin(_a1)))
    _cx = sum(p[0] for p in _pts) / 4.0
    _cy = sum(p[1] for p in _pts) / 4.0
    _fid = f"f{2 * _i + 1}"
    _zebra_steps += sketch_with_id(quad(_pts, f"z{_i}_"), "plane.top", f"Stripe{_i}", _fid)
    _zebra_steps.append(ext(f"Stripe{_i}E", _fid, _cx, _cy, _ZH, merge=False))
for _i in range(6):
    _zebra_steps.append(paint(f"b{_i + 1}", (0.08, 0.08, 0.09) if _i % 2 == 0
                              else (0.93, 0.93, 0.95),
                              0.25 if _i % 2 == 0 else 0.0,
                              0.45 if _i % 2 == 0 else 0.6))

# --- performance: gauge needle rectangle at 40 deg ------------------------------------
_na = math.radians(40.0)
_nu = (math.cos(_na), math.sin(_na))
_np = (-math.sin(_na), math.cos(_na))
_needle_pts = [
    (-2 * _nu[0] - 1.25 * _np[0], -2 * _nu[1] - 1.25 * _np[1]),
    (14 * _nu[0] - 1.25 * _np[0], 14 * _nu[1] - 1.25 * _np[1]),
    (14 * _nu[0] + 1.25 * _np[0], 14 * _nu[1] + 1.25 * _np[1]),
    (-2 * _nu[0] + 1.25 * _np[0], -2 * _nu[1] + 1.25 * _np[1]),
]
_ncx = sum(p[0] for p in _needle_pts) / 4.0
_ncy = sum(p[1] for p in _needle_pts) / 4.0

# --- curvature: crescent sheet loop (outer r=37 out, inner r=15 back) -----------------
_crescent_pts = [(37.0 * math.cos(math.radians(a)), 37.0 * math.sin(math.radians(a)))
                 for a in range(30, 151, 10)]
_crescent_pts += [(15.0 * math.cos(math.radians(a)), 15.0 * math.sin(math.radians(a)))
                  for a in range(150, 29, -10)]


RECIPES: dict[str, dict] = {

    # measure: a steel caliper. Grey beam + fused fixed jaw (b1); teal sliding jaw (b2)
    # and teal slider block (b3) at 60% along the beam = the moving measuring element.
    "measure": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-24, -4, 24, 4, "bm"), "plane.top", "BeamSk", "f1"),
        ext("Beam", "f1", 0, 0, 7, merge=False),
        # fixed jaw at the left end, fused into the beam body
        *sketch_with_id(rect(-24, -4, -18, 4, "fj"), "plane.top", "FixJawSk", "f3"),
        ext("FixJaw", "f3", -21, 0, 28),
        # sliding jaw at 60% along the beam
        *sketch_with_id(rect(4.8, -4, 10.8, 4, "sj"), "plane.top", "SlideJawSk", "f5"),
        ext("SlideJaw", "f5", 7.8, 0, 28, merge=False),
        # slider block riding on top of the beam
        *sketch_with_id(rect(0, -7, 12, 7, "sb"), "plane.top", "SliderSk", "f7", offset=7),
        ext("Slider", "f7", 6, 0, 7, merge=False),
        steel("b1"), accent("b2"), accent("b3"),
    ]},

    # mass_props: teal block whose top face sits at z=0, with a small steel sphere
    # half-embedded at the centre of that face = the centre-of-mass marker.
    "mass_props": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-15, -12, 15, 12, "mb"), "plane.top", "MassSk", "f1",
                        offset=-18),
        ext("MassBlock", "f1", 0, 0, 18, merge=False),
        sphere(6.0, "CoM"),
        accent("b1"), steel("b2"),
    ]},

    # section_props: two grey half-blocks split by a 6mm gap with a thin teal plate
    # standing in the gap = the cutting/section plane whose properties are evaluated.
    "section_props": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-21, -13, -3, 13, "sl"), "plane.top", "LeftSk", "f1"),
        ext("LeftHalf", "f1", -12, 0, 20, merge=False),
        *sketch_with_id(rect(3, -13, 21, 13, "sr"), "plane.top", "RightSk", "f3"),
        ext("RightHalf", "f3", 12, 0, 20, merge=False),
        *sketch_with_id(rect(-1, -13, 1, 13, "sp"), "plane.top", "PlaneSk", "f5"),
        ext("SectPlane", "f5", 0, 0, 20, merge=False),
        steel("b1"), steel("b2"), accent("b3"),
    ]},

    # interference_eval: a teal bar driven straight through a grey block at mid-height,
    # bodies kept separate so the interpenetration is visible = clash detection.
    "interference_eval": {"yaw": 35, "pitch": 26, "steps": [
        box(28, 22, 18, "Housing"),
        *sketch_with_id(rect(-5, -5, 5, 5, "ib"), "plane.right", "BarSk", "f2",
                        offset=-22),
        ext("Bar", "f2", 0, 0, 44, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # draft_analysis: a strongly tapered block in plain steel grey -- the analysis
    # inspects existing draft, it does not create material, so no teal here.
    "draft_analysis": {"yaw": 35, "pitch": 22, "steps": [
        *sketch_with_id(rect(-26, -20, 26, 20, "da"), "plane.top", "DraftAnSk", "f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "DraftAnBlock",
            "params": {"mode": "boss", "depth": dim(34), "draft": dim(18)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        steel("b1"),
    ]},

    # thickness: an open shelled box in steel grey; the annotation calls out the wall.
    "thickness": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "shell", "name": "ThickShell",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}},
        steel("b1"),
    ]},

    # curvature: a smoothly curved crescent sheet (2mm thick) in teal. The engine's
    # arc_slot entity yields no extrudable region (extrude ok=1 but rev unchanged), so
    # the crescent is a polyline loop: outer r=37 arc out, inner r=15 arc back.
    "curvature": {"yaw": 35, "pitch": 32, "steps": [
        *sketch_with_id(quad(_crescent_pts, "cv"), "plane.top", "CurveSk", "f1"),
        ext("CurveSheet", "f1", 0, 26, 2),
        accent("b1"),
    ]},

    # zebra: a curved strip of six touching plates, alternating black / white =
    # zebra-stripe surface continuity analysis.
    "zebra": {"yaw": 35, "pitch": 30, "steps": _zebra_steps},

    # symmetry_check: a grey half with an asymmetric step and its teal mirror image,
    # split on the x=0 plane (the dashed annotation).
    "symmetry_check": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id(quad([(-24, 0), (0, 0), (0, 14), (-10, 14),
                              (-10, 22), (-24, 22)], "syl"),
                        "plane.front", "LeftHalfSk", "f1"),
        ext("LeftHalf", "f1", -17, 8, 16, merge=False),
        *sketch_with_id(quad([(0, 0), (24, 0), (24, 22), (10, 22),
                              (10, 14), (0, 14)], "syr"),
                        "plane.front", "RightHalfSk", "f3"),
        ext("RightHalf", "f3", 17, 8, 16, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # check: a plain steel block; the check badge carries the "verify" meaning.
    "check": {"yaw": 35, "pitch": 26, "steps": [
        box(30, 24, 16, "CheckBlock"),
        steel("b1"),
    ]},

    # geometry_analysis: a grey block with a small teal patch proud of the top face =
    # the flagged region the analysis found.
    "geometry_analysis": {"yaw": 35, "pitch": 26, "steps": [
        box(34, 26, 18, "GeoBlock"),
        *sketch_with_id(rect(-4, -4, 4, 4, "gp"), "plane.top", "PatchSk", "f2", offset=9),
        ext("Patch", "f2", 0, 0, 2, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # performance: a face-on gauge -- grey dial disc with a teal needle standing proud
    # of the dial face at ~40 deg.
    "performance": {"yaw": 0, "pitch": 0, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "dial"},
        ], "plane.front", "DialSk", "f1"),
        ext("Dial", "f1", 0, 0, 4, merge=False),
        # the front view sees the +Y side, so the needle sits proud at y=4..5.5
        *sketch_with_id(quad(_needle_pts, "nd"), "plane.front", "NeedleSk", "f3",
                        offset=4),
        ext("Needle", "f3", _ncx, _ncy, 1.5, merge=False),
        steel("b1"), accent("b2"),
    ]},
}


ANN: dict[str, dict] = {
    "measure":           {"darrow": [0.30, 0.62, 0.62, 0.62]},
    "mass_props":        {"ring": [0.50, 0.32, 0.12]},
    "section_props":     {"dash": [0.50, 0.10, 0.50, 0.90]},
    "interference_eval": {"arrow": [0.16, 0.50, 0.52, 0.50]},
    "draft_analysis":    {"arc": [0.30, 0.42, 0.18, 250, 340]},
    "thickness":         {"darrow": [0.42, 0.30, 0.58, 0.30]},
    "curvature":         {"arc": [0.50, 0.55, 0.30, 210, 330]},
    "zebra":             {"arc": [0.50, 0.58, 0.30, 200, 340]},
    "symmetry_check":    {"dash": [0.50, 0.15, 0.50, 0.85]},
    "check":             {"badge": "check"},
    "geometry_analysis": {"ring": [0.50, 0.34, 0.13]},
    "performance":       {"arc": [0.50, 0.50, 0.30, 180, 320]},
}

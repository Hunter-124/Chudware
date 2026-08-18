"""Icon recipes: simulation (Simulation ribbon tabs + ws_simulate tile)."""
from icon_recipes.helpers import *  # noqa: F401,F403

STEEL = {"op": "set_appearance", "color": [0.66, 0.69, 0.73],
         "metallic": 0.85, "roughness": 0.38}


def steel(bid="b1"):
    d = dict(STEEL)
    d["id"] = bid
    return d


def rect(x0, y0, x1, y1, p):
    """Closed rectangle loop, entity ids prefixed p."""
    return [
        {"type": "line", "p1": [x0, y0], "p2": [x1, y0], "id": p + "a"},
        {"type": "line", "p1": [x1, y0], "p2": [x1, y1], "id": p + "b"},
        {"type": "line", "p1": [x1, y1], "p2": [x0, y1], "id": p + "c"},
        {"type": "line", "p1": [x0, y1], "p2": [x0, y0], "id": p + "d"},
    ]


def ext(fid, name, depth, cx, cy, mode="boss", merge=None):
    f = {"type": "extrude", "name": name,
         "params": {"mode": mode, "depth": dim(depth)},
         "refs": {"profile": [region(fid, cx, cy)]}}
    if merge is not None:
        f["merge"] = merge
    return {"op": "add_feature", "feature": f}


# --- static / ws_simulate shared scene -------------------------------------
def _static_steps():
    """Grey wall at x=-26..-14 + teal cantilever bar x=-14..26."""
    return [
        # wall: 12 (X) x 30 (Y) x 30 (Z) via plane.top sketch, extrude +30
        *sketch_with_id(rect(-26, -15, -14, 15, "w"), "plane.top", "WallSk", "f1"),
        ext("f1", "Wall", 30, -20, 0, merge=False),
        # bar: 40 (X) x 10 (Y) x 10 (Z), sitting mid-height of the wall (offset 10)
        *sketch_with_id(rect(-14, -5, 26, 5, "r"), "plane.top", "BarSk", "f3", offset=10),
        ext("f3", "Bar", 10, 6, 0, merge=False),
    ]


RECIPES = {
    "study_static": {"yaw": 35, "pitch": 26, "steps": [
        *_static_steps(),
        steel("b1"),
        accent("b2"),
    ]},

    # Tuning fork: closed U profile on plane.front, extruded 8mm in +Y.
    "study_modal": {"yaw": 34, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-13, -10], "p2": [13, -10], "id": "u1"},
            {"type": "line", "p1": [13, -10], "p2": [13, 24], "id": "u2"},
            {"type": "line", "p1": [13, 24], "p2": [5, 24], "id": "u3"},
            {"type": "line", "p1": [5, 24], "p2": [5, 0], "id": "u4"},
            {"type": "line", "p1": [5, 0], "p2": [-5, 0], "id": "u5"},
            {"type": "line", "p1": [-5, 0], "p2": [-5, 24], "id": "u6"},
            {"type": "line", "p1": [-5, 24], "p2": [-13, 24], "id": "u7"},
            {"type": "line", "p1": [-13, 24], "p2": [-13, -10], "id": "u8"},
        ], "plane.front", "ForkSk", "f1"),
        ext("f1", "Fork", 8, 0, -5),
        accent("b1"),
    ]},

    # Bent pipe: circle profile on plane.right swept along an L path on plane.top.
    "study_flow": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 6, "id": "fc1"},
        ], "plane.right", "FlowProf", "f1"),
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [30, 0], "id": "fp1"},
            {"type": "line", "p1": [30, 0], "p2": [30, 26], "id": "fp2"},
        ], "plane.top", "FlowPath", "f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "FlowPipe", "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", 15, 0)}}},
        accent("b1"),
    ]},

    # Hot/cold half-blocks side by side.
    "study_thermal": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-21, -14, -1, 14, "h"), "plane.top", "HotSk", "f1"),
        ext("f1", "Hot", 16, -11, 0, merge=False),
        *sketch_with_id(rect(1, -14, 21, 14, "c"), "plane.top", "ColdSk", "f3"),
        ext("f3", "Cold", 16, 11, 0, merge=False),
        {"op": "set_appearance", "id": "b1", "color": [0.85, 0.30, 0.25],
         "metallic": 0.1, "roughness": 0.5},
        {"op": "set_appearance", "id": "b2", "color": [0.25, 0.45, 0.85],
         "metallic": 0.1, "roughness": 0.5},
    ]},

    # Dog-bone tensile specimen (polyline waist), plane.top, 6mm thick.
    "study_fatigue": {"yaw": 32, "pitch": 36, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-28, -9], "p2": [-13, -9], "id": "d1"},
            {"type": "line", "p1": [-13, -9], "p2": [-8, -4], "id": "d2"},
            {"type": "line", "p1": [-8, -4], "p2": [8, -4], "id": "d3"},
            {"type": "line", "p1": [8, -4], "p2": [13, -9], "id": "d4"},
            {"type": "line", "p1": [13, -9], "p2": [28, -9], "id": "d5"},
            {"type": "line", "p1": [28, -9], "p2": [28, 9], "id": "d6"},
            {"type": "line", "p1": [28, 9], "p2": [13, 9], "id": "d7"},
            {"type": "line", "p1": [13, 9], "p2": [8, 4], "id": "d8"},
            {"type": "line", "p1": [8, 4], "p2": [-8, 4], "id": "d9"},
            {"type": "line", "p1": [-8, 4], "p2": [-13, 9], "id": "d10"},
            {"type": "line", "p1": [-13, 9], "p2": [-28, 9], "id": "d11"},
            {"type": "line", "p1": [-28, 9], "p2": [-28, -9], "id": "d12"},
        ], "plane.top", "BoneSk", "f1"),
        ext("f1", "Bone", 9, 0, 0),
        accent("b1"),
    ]},

    # Teal block gripped by two grey L-brackets.
    "fixture": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-12, -10, 12, 10, "p"), "plane.top", "PartSk", "f1"),
        ext("f1", "Part", 16, 0, 0, merge=False),
        # left L bracket
        *sketch_with_id([
            {"type": "line", "p1": [-19, -12], "p2": [-12, -12], "id": "la"},
            {"type": "line", "p1": [-12, -12], "p2": [-12, 12], "id": "lb"},
            {"type": "line", "p1": [-12, 12], "p2": [-19, 12], "id": "lc"},
            {"type": "line", "p1": [-19, 12], "p2": [-19, -12], "id": "ld"},
        ], "plane.top", "L1Sk", "f3"),
        ext("f3", "Clamp1", 22, -15.5, 0, merge=False),
        *sketch_with_id(rect(12, -12, 19, 12, "m"), "plane.top", "L2Sk", "f5"),
        ext("f5", "Clamp2", 22, 15.5, 0, merge=False),
        accent("b1"),
        steel("b2"),
        steel("b3"),
    ]},

    "load_force": {"yaw": 35, "pitch": 26, "steps": [
        box(36, 24, 14), steel("b1")]},

    "load_pressure": {"yaw": 35, "pitch": 26, "steps": [
        box(36, 24, 14), steel("b1")]},

    # 3x3 grid of 10mm teal cubes, 2mm gaps.
    "mesh": {"yaw": 34, "pitch": 30, "steps": []},

    # Tet element: loft triangle -> tiny triangle.
    "mesh_tet": {"yaw": 34, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-14, -8], "p2": [14, -8], "id": "t1"},
            {"type": "line", "p1": [14, -8], "p2": [0, 16], "id": "t2"},
            {"type": "line", "p1": [0, 16], "p2": [-14, -8], "id": "t3"},
        ], "plane.top", "TetBot", "f1"),
        *sketch_with_id([
            {"type": "line", "p1": [-2, -1.2], "p2": [2, -1.2], "id": "t4"},
            {"type": "line", "p1": [2, -1.2], "p2": [0, 2.3], "id": "t5"},
            {"type": "line", "p1": [0, 2.3], "p2": [-2, -1.2], "id": "t6"},
        ], "plane.top", "TetTop", "f2", offset=26),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "Tet", "params": {"mode": "boss"},
            "refs": {"sections": [region("f1", 0, 0), region("f2", 0, 0)]}}},
        accent("b1"),
    ]},

    # 2x2 cluster of 12mm teal cubes.
    "mesh_hex": {"yaw": 34, "pitch": 30, "steps": []},

    # One teal tet + one grey cube side by side.
    "mesh_auto": {"yaw": 34, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, -8], "p2": [-4, -8], "id": "at1"},
            {"type": "line", "p1": [-4, -8], "p2": [-14, 10], "id": "at2"},
            {"type": "line", "p1": [-14, 10], "p2": [-24, -8], "id": "at3"},
        ], "plane.top", "AutoTetBot", "f1"),
        *sketch_with_id([
            {"type": "line", "p1": [-15.4, -1.6], "p2": [-12.6, -1.6], "id": "at4"},
            {"type": "line", "p1": [-12.6, -1.6], "p2": [-14, 0.9], "id": "at5"},
            {"type": "line", "p1": [-14, 0.9], "p2": [-15.4, -1.6], "id": "at6"},
        ], "plane.top", "AutoTetTop", "f2", offset=20),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "AutoTet", "params": {"mode": "boss"},
            "refs": {"sections": [region("f1", -14, -2), region("f2", -14, -0.8)]}}},
        *sketch_with_id(rect(4, -7, 18, 7, "ac"), "plane.top", "AutoCubeSk", "f4"),
        ext("f4", "AutoCube", 14, 11, 0, merge=False),
        accent("b1"),
        steel("b2"),
    ]},

    # Play button: triangle on plane.front, near face-on camera.
    "run_study": {"yaw": 20, "pitch": 14, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-12, 16], "p2": [-12, -16], "id": "pl1"},
            {"type": "line", "p1": [-12, -16], "p2": [18, 0], "id": "pl2"},
            {"type": "line", "p1": [18, 0], "p2": [-12, 16], "id": "pl3"},
        ], "plane.front", "PlaySk", "f1"),
        ext("f1", "Play", 8, -4, 0),
        accent("b1"),
    ]},

    # Fringe legend: 5 thin bars.
    "results": {"yaw": 30, "pitch": 24, "steps": []},

    # S-curved swept pipe.
    "flow_arrow": {"yaw": 38, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 5, "id": "sc1"},
        ], "plane.right", "SProf", "f1"),
        *sketch_with_id([
            {"type": "line", "p1": [-24, -12], "p2": [-6, -12], "id": "sp1"},
            {"type": "line", "p1": [-6, -12], "p2": [-6, 12], "id": "sp2"},
            {"type": "line", "p1": [-6, 12], "p2": [22, 12], "id": "sp3"},
        ], "plane.top", "SPath", "f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "SPipe", "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", -15, -12)}}},
        accent("b1"),
    ]},

    # Workspace tile: static scene + a 2x2 mesh patch on the bar near the wall.
    "ws_simulate": {"yaw": 30, "pitch": 30, "steps": []},
}

# --- grid-built recipes (loops) -------------------------------------------


def _grid(fid_start, name, cells, size, depth, offset=None, gap=2.0):
    """cells: list of (cx, cy) centers. Returns steps: sketch+extrude per cube."""
    steps = []
    fid = fid_start
    h = size / 2.0
    for i, (cx, cy) in enumerate(cells):
        sk = "f%d" % fid
        steps += sketch_with_id(rect(cx - h, cy - h, cx + h, cy + h, "%s%d" % (name, i)),
                                "plane.top", "%sSk%d" % (name, i), sk, offset=offset)
        steps.append(ext(sk, "%s%d" % (name, i), depth, cx, cy, merge=False))
        fid += 2
    return steps, fid


_mesh_cells = [(x * 12.0, y * 12.0) for y in (-1, 0, 1) for x in (-1, 0, 1)]
_mesh_steps, _ = _grid(1, "M", _mesh_cells, 10.0, 10.0)
RECIPES["mesh"]["steps"] = _mesh_steps + [accent("b%d" % i) for i in range(1, 10)]

_hex_cells = [(x * 14.0, y * 14.0) for y in (-0.5, 0.5) for x in (-0.5, 0.5)]
_hex_steps, _ = _grid(1, "H", _hex_cells, 12.0, 12.0)
RECIPES["mesh_hex"]["steps"] = _hex_steps + [accent("b%d" % i) for i in range(1, 5)]

_bar_colors = [[0.23, 0.35, 0.95], [0.20, 0.75, 0.90], [0.25, 0.80, 0.35],
               [0.95, 0.80, 0.20], [0.90, 0.25, 0.20]]
_bar_steps = []
_fid = 1
for _i in range(5):
    _x = (_i - 2) * 7.5
    _sk = "f%d" % _fid
    _bar_steps += sketch_with_id(rect(_x - 3, -2, _x + 3, 2, "bar%d" % _i),
                                 "plane.top", "BarSk%d" % _i, _sk)
    _bar_steps.append(ext(_sk, "Bar%d" % _i, 26, _x, 0, merge=False))
    _fid += 2
RECIPES["results"]["steps"] = _bar_steps + [
    {"op": "set_appearance", "id": "b%d" % (i + 1), "color": c,
     "metallic": 0.0, "roughness": 0.5} for i, c in enumerate(_bar_colors)]

# ws_simulate: static scene (f1..f4) + 2x2 mesh patch on the bar top (Z=20), near wall.
_patch_cells = [(-6.0, -2.5), (0.5, -2.5), (-6.0, 2.5), (0.5, 2.5)]
_patch_steps, _ = _grid(5, "P", _patch_cells, 5.0, 5.0, offset=20.0)
RECIPES["ws_simulate"]["steps"] = (
    _static_steps() + _patch_steps
    + [steel("b1"), accent("b2")]
    + [accent("b%d" % i) for i in range(3, 7)]
)

ANN = {
    "study_static": {"arrow": [0.74, 0.30, 0.74, 0.55]},
    "study_flow": {"arrow": [0.30, 0.60, 0.70, 0.30]},
    "load_force": {"arrow": [0.50, 0.05, 0.50, 0.38]},
    "load_pressure": {"arrows": [[0.32, 0.05, 0.32, 0.34],
                                 [0.50, 0.05, 0.50, 0.38],
                                 [0.68, 0.05, 0.68, 0.34]]},
    "flow_arrow": {"arrow": [0.18, 0.18, 0.78, 0.30]},
}

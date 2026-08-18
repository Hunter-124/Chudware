"""Icon recipes: ECAD (PCB Design + Schematic ribbon tabs)."""
import math

from icon_recipes.helpers import *  # noqa: F401,F403

# --- palette ----------------------------------------------------------------
PCB_GREEN = ([0.11, 0.45, 0.22], 0.05, 0.55)
COPPER = ([0.72, 0.38, 0.12], 0.35, 0.45)
BLACK = ([0.10, 0.10, 0.12], 0.2, 0.5)
STEEL = ([0.66, 0.69, 0.73], 0.85, 0.38)
WHITE = ([0.92, 0.93, 0.95], 0.0, 0.6)


def mat(body_id, spec):
    color, metallic, rough = spec
    return {"op": "set_appearance", "id": body_id, "color": list(color),
            "metallic": metallic, "roughness": rough}


# --- geometry builders ------------------------------------------------------
_seq = {"n": 1}


def _fid():
    """Allocate the next sketch feature id (each solid consumes 2 ids)."""
    i = _seq["n"]
    _seq["n"] += 2
    return f"f{i}"


def reset(recipe_steps=None):
    _seq["n"] = 1
    return []


_uid = {"n": 0}


def _eid(tag):
    _uid["n"] += 1
    return f"{tag}{_uid['n']}"


def _loop(pts, fid_tag):
    ents = []
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        ents.append({"type": "line", "p1": [round(a[0], 4), round(a[1], 4)],
                     "p2": [round(b[0], 4), round(b[1], 4)], "id": _eid(fid_tag)})
    return ents


def poly(pts, h, z=None, name="Poly", merge=True, mode="boss", plane="plane.top"):
    """Extrude a closed polygon loop. Returns steps; centroid = polygon average."""
    fid = _fid()
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    feat = {"type": "extrude", "name": name,
            "params": {"mode": mode, "depth": dim(h)},
            "refs": {"profile": [region(fid, round(cx, 4), round(cy, 4))]}}
    if not merge:
        feat["merge"] = False
    return [*sketch_with_id(_loop(pts, "e"), plane=plane, name=name + "Sk",
                            fid=fid, offset=z),
            {"op": "add_feature", "feature": feat}]


def rect(x0, y0, x1, y1, h, z=None, name="Rect", merge=True, mode="boss",
         plane="plane.top"):
    return poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], h, z, name, merge, mode,
                plane)


def plate(lx, ly, h, z=None, cx=0.0, cy=0.0, **kw):
    return rect(cx - lx / 2, cy - ly / 2, cx + lx / 2, cy + ly / 2, h, z, **kw)


def disc(cx, cy, r, h, z=None, name="Disc", merge=True, mode="boss"):
    fid = _fid()
    feat = {"type": "extrude", "name": name,
            "params": {"mode": mode, "depth": dim(h)},
            "refs": {"profile": [region(fid, cx, cy)]}}
    if not merge:
        feat["merge"] = False
    return [*sketch_with_id([{"type": "circle", "center": [cx, cy], "radius": r,
                              "id": _eid("c")}],
                            plane="plane.top", name=name + "Sk", fid=fid, offset=z),
            {"op": "add_feature", "feature": feat}]


def bar(p1, p2, width, h, z=None, name="Bar", merge=True, cap=True):
    """A rectangular bar from p1 to p2 (optionally capped by half-width at each end)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    px, py = -uy, ux
    e = width / 2 if cap else 0.0
    a = (p1[0] - ux * e, p1[1] - uy * e)
    b = (p2[0] + ux * e, p2[1] + uy * e)
    w = width / 2
    pts = [(a[0] + px * w, a[1] + py * w), (b[0] + px * w, b[1] + py * w),
           (b[0] - px * w, b[1] - py * w), (a[0] - px * w, a[1] - py * w)]
    return poly(pts, h, z, name, merge)


def chain(pts, width, h, z=None, name="Ch", merge=True):
    """A poly-chain of bars; every segment is its own body."""
    steps = []
    for i in range(len(pts) - 1):
        steps += bar(pts[i], pts[i + 1], width, h, z, f"{name}{i}", merge=merge)
    return steps


def zigzag_pts(seg=5.0, n=5, amp=1.6, x0=-9.6, lead=6.0):
    """Resistor zigzag polyline (with straight leads at both ends)."""
    ang = math.degrees(math.asin(amp * 2 / seg)) if seg else 0
    dx = math.sqrt(max(seg * seg - (2 * amp) ** 2, 0.01))
    pts = [(x0 - lead, -amp), (x0, -amp)]
    x, y = x0, -amp
    for i in range(n):
        x += dx
        y = amp if y < 0 else -amp
        pts.append((x, y))
    pts.append((x + lead, y))
    return pts, ang


ZIG_PTS, _ = zigzag_pts()


def zig_steps(z, name="Zg"):
    return chain(ZIG_PTS, 1.5, 0.8, z, name, merge=False)


def sine_pts(amp=6.0, wl=28.0, n=10):
    pts = []
    for i in range(n + 1):
        x = -wl / 2 + wl * i / n
        pts.append((x, amp * math.sin(2 * math.pi * (x + wl / 2) / wl)))
    return pts


def colors(specs):
    return [mat(f"b{i + 1}", s) for i, s in enumerate(specs)]


def build(fn):
    reset()
    _uid["n"] = 0
    return fn()


# ---------------------------------------------------------------------------
# PCB Design tab
# ---------------------------------------------------------------------------

def _pcb():
    return [
        *plate(44, 32, 3, name="Board"),
        *plate(14, 14, 4, z=3, cx=-2, cy=1, name="Chip1", merge=False),
        *plate(8, 8, 3, z=3, cx=14, cy=-9, name="Chip2", merge=False),
        *disc(-16, 11, 2.6, 1.2, z=3, name="Pad1", merge=False),
        *disc(15, 11, 2.6, 1.2, z=3, name="Pad2", merge=False),
        *colors([PCB_GREEN, BLACK, BLACK, COPPER, COPPER]),
    ]


def _board3d():
    return [
        *plate(44, 32, 3, name="Board"),
        *plate(16, 16, 8, z=3, cx=-6, cy=2, name="IC", merge=False),
        *plate(20, 8, 10, z=3, cx=2, cy=-11, name="Conn", merge=False),
        *disc(15, 10, 3, 8, z=3, name="Cap", merge=False),
        *colors([PCB_GREEN, BLACK, STEEL, COPPER]),
    ]


def _footprint():
    steps = [*plate(30, 24, 2, name="Board")]
    specs = [PCB_GREEN]
    for sx in (-7, 7):
        for sy in (-6, 0, 6):
            steps += plate(6, 3, 0.8, z=2, cx=sx, cy=sy, name="Pad", merge=False)
            specs.append(COPPER)
    return steps + colors(specs)


def _route():
    pts = [(-15, -6), (-4, -6), (4, 2), (15, 2)]
    return [
        *plate(36, 28, 2, name="Board"),
        *chain(pts, 2.5, 1.5, 2, "Trace", merge=False),
        *colors([PCB_GREEN] + [COPPER] * 3),
    ]


def _via():
    return [
        *plate(26, 26, 2.5, name="Board"),
        *disc(0, 0, 4, 12, z=-4.5, name="Via", merge=False),
        *colors([PCB_GREEN, COPPER]),
    ]


def _zone():
    return [
        *plate(34, 28, 2, name="Board"),
        *plate(26, 20, 0.8, z=2, name="Pour", merge=False),
        *rect(5, 4, 13, 10, 0.9, z=2, name="Notch", mode="cut"),
        *colors([PCB_GREEN, COPPER]),
    ]


def _autoroute():
    steps = [*plate(38, 28, 2, name="Board")]
    specs = [PCB_GREEN]
    for y in (-9, -1, 7):
        pts = [(-17, y), (-5, y), (-1, y + 4), (17, y + 4)]
        steps += chain(pts, 2.0, 1.4, 2, f"T{y}", merge=False)
        specs += [COPPER] * 3
    return steps + colors(specs)


def _fill():
    return [
        *plate(34, 28, 2, name="Board"),
        *plate(30, 24, 0.8, z=2, name="Pour", merge=False),
        *disc(-8, 4, 2.5, 0.9, z=2, name="Clr1", mode="cut"),
        *disc(7, -5, 2.5, 0.9, z=2, name="Clr2", mode="cut"),
        *colors([PCB_GREEN, COPPER]),
    ]


def _gerber():
    return [
        *plate(32, 26, 1.6, name="L1"),
        *plate(28, 22, 1.2, z=7, name="L2", merge=False),
        *plate(24, 18, 1.6, z=14, name="L3", merge=False),
        *colors([PCB_GREEN, COPPER, PCB_GREEN]),
    ]


def _drill():
    steps = [*plate(32, 26, 3, name="Board")]
    for sx in (-7, 7):
        for sy in (-7, 7):
            steps += disc(sx, sy, 2.0, 3.2, z=0, name="Hole", mode="cut")
    return steps + colors([PCB_GREEN])


def _drc():
    return [
        *plate(34, 28, 3, name="Board"),
        *chain([(-13, -5), (0, -5), (6, 1), (14, 1)], 2.5, 1.5, 3, "Trace",
               merge=False),
        *colors([PCB_GREEN] + [COPPER] * 3),
    ]


def _update():
    return [
        *plate(34, 28, 3, name="Board"),
        *disc(-11, 8, 2.6, 1.2, z=3, name="Pad1", merge=False),
        *disc(11, -8, 2.6, 1.2, z=3, name="Pad2", merge=False),
        *bar((-11, 8), (11, -8), 2.2, 1.2, 3, "Link", merge=False),
        *colors([PCB_GREEN, COPPER, COPPER, COPPER]),
    ]


# ---------------------------------------------------------------------------
# Schematic tab
# ---------------------------------------------------------------------------
SHEET = (36, 28, 1.5)


def _sheet():
    return plate(SHEET[0], SHEET[1], SHEET[2], name="Sheet")


def _sch_new():
    n = len(ZIG_PTS) - 1
    return [*_sheet(), *zig_steps(SHEET[2]), *colors([WHITE] + [BLACK] * n)]


def _sch_symbol():
    n = len(ZIG_PTS) - 1
    box_pts = [(-12, -5), (12, -5), (12, 5), (-12, 5)]
    steps = [*_sheet(), *zig_steps(SHEET[2])]
    for i in range(4):
        steps += bar(box_pts[i], box_pts[(i + 1) % 4], 1.2, 0.8, SHEET[2],
                     f"Ob{i}", merge=False)
    return steps + colors([WHITE] + [BLACK] * (n + 4))


def _sch_wire():
    return [
        *_sheet(),
        *bar((-14, 6), (2, 6), 1.5, 0.8, SHEET[2], "W1", merge=False),
        *bar((2, 6), (2, -6), 1.5, 0.8, SHEET[2], "W2", merge=False),
        *plate(2.5, 2.5, 2.5, z=SHEET[2], cx=2, cy=6, name="Dot", merge=False),
        *colors([WHITE, BLACK, BLACK, BLACK]),
    ]


def _sch_power():
    steps = [*_sheet(), *bar((0, 10), (0, 2), 1.5, 0.8, SHEET[2], "Stub",
                             merge=False, cap=False)]
    for w, y in ((14, 1.0), (9, -2.0), (4, -5.0)):
        steps += plate(w, 1.5, 0.8, z=SHEET[2], cy=y, name="Gnd", merge=False)
    return steps + colors([WHITE] + [BLACK] * 4)


def _sch_label():
    return [
        *_sheet(),
        *rect(-3, -4, 11, 4, 0.8, SHEET[2], name="Flag", merge=False),
        *poly([(-3, -4), (-3, 4), (-9, 0)], 0.8, SHEET[2], name="Tip", merge=False),
        *bar((-16, 0), (-9, 0), 1.2, 0.8, SHEET[2], "Stub", merge=False, cap=False),
        *colors([WHITE, BLACK, BLACK, BLACK]),
    ]


def _sch_erc():
    return _sch_new()


def _sch_simulate():
    pts = sine_pts()
    n = len(pts) - 1
    return [*_sheet(), *chain(pts, 1.2, 0.8, SHEET[2], "Sn", merge=False),
            *colors([WHITE] + [BLACK] * n)]


ISO = {"yaw": 34, "pitch": 28}
FLAT = {"yaw": 16, "pitch": 52}

RECIPES: dict[str, dict] = {
    "ecad_pcb": {**ISO, "steps": build(_pcb)},
    "ecad_board3d": {"yaw": 34, "pitch": 24, "steps": build(_board3d)},
    "ecad_footprint": {"yaw": 22, "pitch": 44, "steps": build(_footprint)},
    "ecad_route": {"yaw": 20, "pitch": 46, "steps": build(_route)},
    "ecad_via": {"yaw": 34, "pitch": 10, "steps": build(_via)},
    "ecad_zone": {"yaw": 22, "pitch": 44, "steps": build(_zone)},
    "ecad_autoroute": {"yaw": 20, "pitch": 48, "steps": build(_autoroute)},
    "ecad_fill": {"yaw": 22, "pitch": 44, "steps": build(_fill)},
    "ecad_gerber": {"yaw": 34, "pitch": 26, "steps": build(_gerber)},
    "ecad_drill": {"yaw": 26, "pitch": 40, "steps": build(_drill)},
    "ecad_drc": {"yaw": 22, "pitch": 44, "steps": build(_drc)},
    "ecad_update": {"yaw": 24, "pitch": 42, "steps": build(_update)},
    "sch_new": {**FLAT, "steps": build(_sch_new)},
    "sch_symbol": {**FLAT, "steps": build(_sch_symbol)},
    "sch_wire": {**FLAT, "steps": build(_sch_wire)},
    "sch_power": {**FLAT, "steps": build(_sch_power)},
    "sch_label": {**FLAT, "steps": build(_sch_label)},
    "sch_erc": {**FLAT, "steps": build(_sch_erc)},
    "sch_simulate": {**FLAT, "steps": build(_sch_simulate)},
}

ANN: dict[str, dict] = {
    "ecad_route": {"arrow": [0.24, 0.66, 0.78, 0.40]},
    "ecad_via": {"darrow": [0.5, 0.16, 0.5, 0.86]},
    "ecad_autoroute": {"arrow": [0.20, 0.60, 0.82, 0.42]},
    "ecad_drill": {"arrow": [0.5, 0.10, 0.5, 0.42]},
    "ecad_drc": {"badge": "check"},
    "ecad_update": {"arc": [0.5, 0.45, 0.34, -40, 210]},
    "ecad_gerber": {"darrow": [0.5, 0.12, 0.5, 0.88]},
    "sch_new": {"badge": "+"},
    "sch_wire": {"arrow": [0.20, 0.34, 0.62, 0.34]},
    "sch_erc": {"badge": "check"},
    "sch_label": {"arrow": [0.16, 0.5, 0.38, 0.5]},
}

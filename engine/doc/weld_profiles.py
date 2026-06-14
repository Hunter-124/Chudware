"""Structural-profile library for the Weldments module.

A *profile* is a 2-D cross-section (a closed wire, possibly with an inner void for hollow
sections) that ``features/weldment.py`` sweeps along the segments of a sketch to build weldment
members. Cross-sections are built in the profile's LOCAL frame: the section lies in the XY plane
and is swept along +Z by the member compiler (which then re-orients the section perpendicular to
each path segment). Canonical units are millimetres.

Each profile has:
- a set of named parameter keys (dimensions, mm) with defaults;
- a builder that turns the resolved dimensions into a build123d ``Face`` — the OUTER boundary
  minus any inner void (hollow tubes are genuinely hollow);
- a cross-sectional *area* (mm²) used by the member compiler to sanity-check swept volumes and
  documented here so the cut list / mass roll-up is exact.

Justification (``center`` | ``corner``) shifts the section in its local frame so the swept member
either centres on the sketch segment (``center``, default) or hangs its lower-left corner on the
segment (``corner``) — the two SolidWorks "Locate Profile" extremes. The shift is applied by
:func:`build_profile_face`, not by the individual builders (which always draw centred on origin).

The library is intentionally small but the cross-sections are REAL: hollow tubes subtract the
inner wall, the L-angle is a true L (not a filled rectangle), the C-channel is an open C.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from build123d import Face, Plane, Vector, Wire
from build123d import Edge


# --------------------------------------------------------------------------- low-level builders


def _rect_wire(w: float, h: float, cx: float = 0.0, cy: float = 0.0) -> Wire:
    """Closed rectangle wire (w × h) centred on (cx, cy), in the XY plane."""
    hw, hh = w / 2.0, h / 2.0
    pts = [
        Vector(cx - hw, cy - hh, 0),
        Vector(cx + hw, cy - hh, 0),
        Vector(cx + hw, cy + hh, 0),
        Vector(cx - hw, cy + hh, 0),
    ]
    edges = [Edge.make_line(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    return Wire(edges)


def _polyline_wire(pts: list[tuple[float, float]]) -> Wire:
    """Closed wire through an ordered list of (x, y) points (XY plane)."""
    vecs = [Vector(x, y, 0) for x, y in pts]
    n = len(vecs)
    edges = [Edge.make_line(vecs[i], vecs[(i + 1) % n]) for i in range(n)]
    return Wire(edges)


def _face_from(outer: Wire, inners: list[Wire] | None = None) -> Face:
    """Build a planar Face from an outer wire minus inner (void) wires."""
    face = Face(outer)
    if inners:
        face = Face(outer, inners)
    return face


# --------------------------------------------------------------------------- per-profile builders
# Every builder draws the section CENTRED on the local origin and returns a build123d Face.


def _square_tube(size: float, thickness: float) -> Face:
    if thickness <= 0 or thickness >= size / 2.0:
        raise ValueError(f"square_tube: thickness {thickness} invalid for size {size}")
    outer = _rect_wire(size, size)
    inner = _rect_wire(size - 2 * thickness, size - 2 * thickness)
    return _face_from(outer, [inner])


def _rect_tube(w: float, h: float, thickness: float) -> Face:
    if thickness <= 0 or thickness >= min(w, h) / 2.0:
        raise ValueError(f"rect_tube: thickness {thickness} invalid for {w}x{h}")
    outer = _rect_wire(w, h)
    inner = _rect_wire(w - 2 * thickness, h - 2 * thickness)
    return _face_from(outer, [inner])


def _round_tube(od: float, thickness: float) -> Face:
    if thickness <= 0 or thickness >= od / 2.0:
        raise ValueError(f"round_tube: thickness {thickness} invalid for od {od}")
    outer = Wire([Edge.make_circle(od / 2.0, Plane.XY)])
    inner = Wire([Edge.make_circle(od / 2.0 - thickness, Plane.XY)])
    return _face_from(outer, [inner])


def _angle_l(leg: float, thickness: float) -> Face:
    """Equal-leg L angle: an L of leg length ``leg`` and wall ``thickness``.

    Drawn with its bounding box (leg × leg) centred on the origin so justification math is
    uniform with the closed sections.
    """
    if thickness <= 0 or thickness >= leg:
        raise ValueError(f"angle_L: thickness {thickness} invalid for leg {leg}")
    t = thickness
    # L outline in a leg×leg box with lower-left at (0,0): two legs of width t.
    pts = [
        (0.0, 0.0),
        (leg, 0.0),
        (leg, t),
        (t, t),
        (t, leg),
        (0.0, leg),
    ]
    # Centre the leg×leg bounding box on the origin.
    pts = [(x - leg / 2.0, y - leg / 2.0) for x, y in pts]
    return _face_from(_polyline_wire(pts))


def _flat_bar(w: float, thickness: float) -> Face:
    if w <= 0 or thickness <= 0:
        raise ValueError(f"flat_bar: w {w}, thickness {thickness} must be > 0")
    return _face_from(_rect_wire(w, thickness))


def _c_channel(h: float, w: float, thickness: float) -> Face:
    """Open C-channel: web of height ``h`` with two flanges of width ``w`` and wall ``thickness``.

    Opening faces +X. Bounding box (w × h) centred on the origin.
    """
    if thickness <= 0 or thickness >= w or 2 * thickness >= h:
        raise ValueError(f"c_channel: thickness {thickness} invalid for {w}x{h}")
    t = thickness
    # C outline in a w×h box with lower-left at (0,0), opening to +X:
    pts = [
        (0.0, 0.0),
        (w, 0.0),
        (w, t),
        (t, t),
        (t, h - t),
        (w, h - t),
        (w, h),
        (0.0, h),
    ]
    pts = [(x - w / 2.0, y - h / 2.0) for x, y in pts]
    return _face_from(_polyline_wire(pts))


def _i_beam(h: float, w: float, tw: float, tf: float) -> Face:
    """Doubly-symmetric W / I-beam: depth ``h``, flange width ``w``, web thickness ``tw`` and
    flange thickness ``tf``. Bounding box (w × h) centred on the origin.

    The section is the classic 12-vertex I outline (two flanges joined by a central web). Drawn
    with sharp corners (no fillets) — a faithful structural-area approximation of a rolled shape.
    """
    if tw <= 0 or tf <= 0:
        raise ValueError(f"i_beam: web {tw} / flange {tf} thickness must be > 0")
    if tw >= w:
        raise ValueError(f"i_beam: web thickness {tw} must be < flange width {w}")
    if 2 * tf >= h:
        raise ValueError(f"i_beam: flange thickness {tf} too large for depth {h}")
    hw, hh = w / 2.0, h / 2.0
    hwt = tw / 2.0  # half web thickness
    yf = hh - tf    # inner face of the top flange
    # Walk the outline clockwise starting at the lower-left flange corner.
    pts = [
        (-hw, -hh), (hw, -hh),          # bottom of lower flange
        (hw, -yf), (hwt, -yf),          # up the right side, in to the web
        (hwt, yf), (hw, yf),            # up the web, out to the upper flange
        (hw, hh), (-hw, hh),            # top of upper flange
        (-hw, yf), (-hwt, yf),          # down the left side, in to the web
        (-hwt, -yf), (-hw, -yf),        # down the web, out to the lower flange
    ]
    return _face_from(_polyline_wire(pts))


def _t_section(h: float, w: float, tw: float, tf: float) -> Face:
    """T-section: overall depth ``h``, flange width ``w``, web thickness ``tw``, flange thickness
    ``tf``. The flange is the top bar; the web hangs down. Bounding box (w × h) centred on origin.
    """
    if tw <= 0 or tf <= 0:
        raise ValueError(f"t_section: web {tw} / flange {tf} thickness must be > 0")
    if tw >= w:
        raise ValueError(f"t_section: web thickness {tw} must be < flange width {w}")
    if tf >= h:
        raise ValueError(f"t_section: flange thickness {tf} too large for depth {h}")
    hw, hh = w / 2.0, h / 2.0
    hwt = tw / 2.0
    yf = hh - tf  # inner face of the flange
    # Outline: flange across the top, web hanging from the middle. Start at lower-left of the web.
    pts = [
        (-hwt, -hh), (hwt, -hh),    # bottom of the web
        (hwt, yf),                  # up the right side of the web to the flange underside
        (hw, yf), (hw, hh),         # out to the right flange tip, up to the top
        (-hw, hh), (-hw, yf),       # across the flange top, down the left tip
        (-hwt, yf),                 # back in to the web
    ]
    return _face_from(_polyline_wire(pts))


# --------------------------------------------------------------------------- registry


class ProfileSpec:
    """One library profile: its dimension keys (with defaults), a Face builder, and an
    analytic cross-section area function (mm²) for the cut-list mass roll-up."""

    def __init__(self, name: str, params: dict[str, float],
                 builder: Callable[..., Face], area: Callable[..., float],
                 envelope: Callable[..., tuple[float, float]]) -> None:
        self.name = name
        self.params = params          # key -> default (mm)
        self._builder = builder
        self._area = area
        self._envelope = envelope

    def resolve(self, supplied: dict[str, Any]) -> dict[str, float]:
        """Merge supplied dims over defaults; only known keys are kept."""
        out = dict(self.params)
        for k in self.params:
            if k in supplied and supplied[k] is not None:
                out[k] = float(supplied[k])
        return out

    def face(self, dims: dict[str, float]) -> Face:
        return self._builder(**{k: dims[k] for k in self.params})

    def area(self, dims: dict[str, float]) -> float:
        return self._area(**{k: dims[k] for k in self.params})

    def envelope(self, dims: dict[str, float]) -> tuple[float, float]:
        """(width, height) of the section bounding box (mm) — used for justification."""
        return self._envelope(**{k: dims[k] for k in self.params})


PROFILES: dict[str, ProfileSpec] = {
    "square_tube": ProfileSpec(
        "square_tube", {"size": 20.0, "thickness": 2.0},
        builder=_square_tube,
        area=lambda size, thickness: size * size - (size - 2 * thickness) ** 2,
        envelope=lambda size, thickness: (size, size),
    ),
    "rect_tube": ProfileSpec(
        "rect_tube", {"w": 40.0, "h": 20.0, "thickness": 2.0},
        builder=_rect_tube,
        area=lambda w, h, thickness: w * h - (w - 2 * thickness) * (h - 2 * thickness),
        envelope=lambda w, h, thickness: (w, h),
    ),
    "round_tube": ProfileSpec(
        "round_tube", {"od": 20.0, "thickness": 2.0},
        builder=_round_tube,
        area=lambda od, thickness: math.pi * ((od / 2.0) ** 2 - (od / 2.0 - thickness) ** 2),
        envelope=lambda od, thickness: (od, od),
    ),
    "angle_L": ProfileSpec(
        "angle_L", {"leg": 30.0, "thickness": 3.0},
        builder=_angle_l,
        area=lambda leg, thickness: 2 * leg * thickness - thickness * thickness,
        envelope=lambda leg, thickness: (leg, leg),
    ),
    "flat_bar": ProfileSpec(
        "flat_bar", {"w": 40.0, "thickness": 6.0},
        builder=_flat_bar,
        area=lambda w, thickness: w * thickness,
        envelope=lambda w, thickness: (w, thickness),
    ),
    "c_channel": ProfileSpec(
        "c_channel", {"h": 50.0, "w": 25.0, "thickness": 3.0},
        builder=_c_channel,
        area=lambda h, w, thickness: h * w - (h - 2 * thickness) * (w - thickness),
        envelope=lambda h, w, thickness: (w, h),
    ),
    # W / I-beam — doubly symmetric: depth h, flange width w, web tw, flange tf.
    # Area = web rectangle + the two flange overhangs = h·tw + 2·tf·(w − tw).
    "i_beam": ProfileSpec(
        "i_beam", {"h": 100.0, "w": 50.0, "tw": 5.0, "tf": 7.0},
        builder=_i_beam,
        area=lambda h, w, tw, tf: h * tw + 2.0 * tf * (w - tw),
        envelope=lambda h, w, tw, tf: (w, h),
    ),
    # T-section — flange across the top, web hanging down.
    # Area = flange bar + web below it = w·tf + tw·(h − tf).
    "t_section": ProfileSpec(
        "t_section", {"h": 60.0, "w": 60.0, "tw": 6.0, "tf": 6.0},
        builder=_t_section,
        area=lambda h, w, tw, tf: w * tf + tw * (h - tf),
        envelope=lambda h, w, tw, tf: (w, h),
    ),
}


# --------------------------------------------------------------------------- standard named sizes
# A small catalogue of real-world standard structural sizes (mm), so the PM / agent can pick a
# named member ("HSS 50x50x3") instead of typing four dimensions. Each entry maps a catalogue name
# to ``(profile_name, dims)``. Sizes are nominal, metric, and intentionally representative — not an
# exhaustive AISC/EN table. Use :func:`standard_size` to resolve one to (profile, dims).

STANDARD_SIZES: dict[str, tuple[str, dict[str, float]]] = {
    # HSS square tube (size × wall).
    "HSS 40x40x3":  ("square_tube", {"size": 40.0, "thickness": 3.0}),
    "HSS 50x50x3":  ("square_tube", {"size": 50.0, "thickness": 3.0}),
    "HSS 50x50x5":  ("square_tube", {"size": 50.0, "thickness": 5.0}),
    "HSS 80x80x5":  ("square_tube", {"size": 80.0, "thickness": 5.0}),
    "HSS 100x100x6": ("square_tube", {"size": 100.0, "thickness": 6.0}),
    # HSS rectangular tube (w × h × wall).
    "HSS 60x40x3":  ("rect_tube", {"w": 60.0, "h": 40.0, "thickness": 3.0}),
    "HSS 80x40x4":  ("rect_tube", {"w": 80.0, "h": 40.0, "thickness": 4.0}),
    "HSS 100x50x5": ("rect_tube", {"w": 100.0, "h": 50.0, "thickness": 5.0}),
    # Round pipe / tube (OD × wall).
    "PIPE 21.3x2.8": ("round_tube", {"od": 21.3, "thickness": 2.8}),   # ~1/2" NPS sch40
    "PIPE 33.4x3.4": ("round_tube", {"od": 33.4, "thickness": 3.4}),   # ~1" NPS sch40
    "PIPE 48.3x3.7": ("round_tube", {"od": 48.3, "thickness": 3.7}),   # ~1-1/2" NPS sch40
    "TUBE 25x2":    ("round_tube", {"od": 25.0, "thickness": 2.0}),
    "TUBE 50x3":    ("round_tube", {"od": 50.0, "thickness": 3.0}),
    # C-channel (depth × flange × wall).
    "C 76x38x5":    ("c_channel", {"h": 76.0, "w": 38.0, "thickness": 5.0}),
    "C 102x51x6":   ("c_channel", {"h": 102.0, "w": 51.0, "thickness": 6.0}),
    "C 152x76x6":   ("c_channel", {"h": 152.0, "w": 76.0, "thickness": 6.0}),
    # L equal angle (leg × wall).
    "L 25x25x3":    ("angle_L", {"leg": 25.0, "thickness": 3.0}),
    "L 40x40x4":    ("angle_L", {"leg": 40.0, "thickness": 4.0}),
    "L 50x50x5":    ("angle_L", {"leg": 50.0, "thickness": 5.0}),
    "L 75x75x6":    ("angle_L", {"leg": 75.0, "thickness": 6.0}),
    # W / I-beam (depth × flange-width × web × flange).
    "W 100x50":     ("i_beam", {"h": 100.0, "w": 50.0, "tw": 5.0, "tf": 7.0}),
    "W 150x75":     ("i_beam", {"h": 150.0, "w": 75.0, "tw": 5.0, "tf": 7.0}),
    "W 200x100":    ("i_beam", {"h": 200.0, "w": 100.0, "tw": 6.0, "tf": 9.0}),
    "W 250x125":    ("i_beam", {"h": 250.0, "w": 125.0, "tw": 6.0, "tf": 9.0}),
    # T-section (depth × flange-width × web × flange).
    "T 60x60":      ("t_section", {"h": 60.0, "w": 60.0, "tw": 6.0, "tf": 6.0}),
    "T 80x80":      ("t_section", {"h": 80.0, "w": 80.0, "tw": 8.0, "tf": 8.0}),
}


def standard_size_names() -> list[str]:
    """All catalogue names, sorted."""
    return sorted(STANDARD_SIZES)


def standard_size(name: str) -> tuple[str, dict[str, float]]:
    """Resolve a catalogue name to ``(profile_name, dims)``. Raises ``KeyError`` if unknown."""
    spec = STANDARD_SIZES.get(name)
    if spec is None:
        raise KeyError(name)
    profile, dims = spec
    return profile, dict(dims)


def profile_names() -> list[str]:
    return sorted(PROFILES)


def get_profile(name: str) -> ProfileSpec:
    spec = PROFILES.get(name)
    if spec is None:
        raise KeyError(name)
    return spec


def build_profile_face(name: str, dims_supplied: dict[str, Any], *,
                       justify: str = "center", angle: float = 0.0) -> tuple[Face, dict[str, float], float]:
    """Build a profile Face in the LOCAL section frame.

    Returns ``(face, resolved_dims, area)``. The face is drawn centred on the origin, then:
    - rotated by ``angle`` degrees about the section normal (local +Z, the sweep direction);
    - shifted for ``justify``: ``center`` leaves the section centred on the segment, ``corner``
      moves the section so its lower-left bounding-box corner sits on the segment.

    Raises ``KeyError`` for an unknown profile name and ``ValueError`` for degenerate dimensions
    (e.g. a wall thicker than the tube) — the caller maps both to a contained FeatureError.
    """
    spec = get_profile(name)
    dims = spec.resolve(dims_supplied)
    face = spec.face(dims)
    area = spec.area(dims)

    if abs(angle) > 1e-9:
        from build123d import Rot
        face = Rot(0, 0, float(angle)) * face

    if justify == "corner":
        w, h = spec.envelope(dims)
        from build123d import Pos
        face = Pos(w / 2.0, h / 2.0, 0) * face

    return face, dims, area

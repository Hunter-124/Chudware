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
}


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

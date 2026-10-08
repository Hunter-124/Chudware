"""Civil alignment geometry, profiles and section-based earthwork, in SI metres.

Geometry follows the Euler spiral integral (Raph Levien, *The Euler spiral: a
mathematical history*, UCB/EECS-2008-111) and LandXML 1.2 CoordGeom conventions.
Vertical curves and sight distance follow AASHTO, *A Policy on Geometric Design
of Highways and Streets*, 7th ed., 2018, ch. 3, and the TxDOT Roadway Design
Manual, Vertical Alignment. Earthwork uses the average-end-area/prismoidal
formulas documented in LibreTexts, Fundamentals of Transportation, §7.3.

Factories return contract alignment records; geometry queries return scalars or
coordinates as specified by the API. Analyses return the civil result envelope.
Stationing is horizontal distance; station equations and superelevation are not
part of the aicad.civil/1 alignment record. No extrapolation is performed.
"""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Callable

import numpy as np
from scipy.integrate import quad
from scipy.optimize import minimize_scalar
from scipy.special import fresnel

from .errors import CivilError

_GEOMETRY_REFERENCE = "Euler/clothoid integral; LandXML 1.2 CoordGeom; Levien (2008), UCB/EECS-2008-111, https://www2.eecs.berkeley.edu/Pubs/TechRpts/2008/EECS-2008-111.html"
_VERTICAL_REFERENCE = "AASHTO Green Book, 7th edition (2018), Chapter 3; TxDOT Roadway Design Manual, Vertical Alignment"
_EARTHWORK_REFERENCE = "LibreTexts, Fundamentals of Transportation, 7.3 Earthwork: average-end-area and prismoidal formulas"
_XY_TOL = 1e-6


def _result(name: str, reference: str, **data) -> dict:
    return {"ok": True, "warnings": [], "assumptions": [],
            "method": {"name": name, "reference": reference}, **data}


def _number(value, label: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float, np.number)):
        raise CivilError("invalid_number", f"{label} must be a finite number", field=label)
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        raise CivilError("invalid_number", f"Invalid value for {label}", field=label)
    return number


def _xy(value, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CivilError("invalid_coordinate", f"{label} must be [x, y] in metres")
    return _number(value[0], label + ".x"), _number(value[1], label + ".y")


def _turn(value) -> int:
    if value not in ("cw", "ccw"):
        raise CivilError("invalid_curve_direction", "dir must be cw or ccw")
    return 1 if value == "ccw" else -1


def _curvature(radius, sign: int, label: str) -> float:
    return 0.0 if radius is None else sign / _number(radius, label, positive=True)


def _spiral_local(s: float, k0: float, rate: float) -> complex:
    """Integral exp(i*(k0*u + rate*u²/2)) du, using completed-square Fresnel.

    Quadrature evaluates the same integral in the near-constant-curvature limit,
    where subtraction of two distant Fresnel values loses coordinate precision.
    """
    if s == 0:
        return 0j
    if rate == 0:
        if k0 == 0:
            return complex(s, 0)
        half = k0 * s / 2
        return 2 * math.sin(half) / k0 * complex(math.cos(half), math.sin(half))
    scale = math.sqrt(math.pi / abs(rate))
    u0 = k0 / rate / scale
    u1 = u0 + s / scale
    if max(abs(u0), abs(u1)) > 1000 or abs(u1 - u0) < 1e-4 * max(1, abs(u0)):
        phase = lambda u: k0 * u + rate * u * u / 2
        real, err_x = quad(lambda u: math.cos(phase(u)), 0, s, epsabs=1e-10, epsrel=1e-11, limit=500)
        imag, err_y = quad(lambda u: math.sin(phase(u)), 0, s, epsabs=1e-10, epsrel=1e-11, limit=500)
        if max(err_x, err_y) > max(1e-8, s * 1e-10):
            raise CivilError("spiral_integration_not_converged", "Reduce the spiral's angular extent")
        return complex(real, imag)
    s0, c0 = fresnel(u0)
    s1, c1 = fresnel(u1)
    phase = -k0 * k0 / (2 * rate)
    rotation = complex(math.cos(phase), math.sin(phase))
    return scale * rotation * complex(float(c1 - c0), math.copysign(1, rate) * float(s1 - s0))


@dataclass(frozen=True)
class _Segment:
    kind: str
    start: tuple[float, float]
    end: tuple[float, float]
    length: float
    heading: float
    k0: float
    k1: float

    def at(self, s: float) -> tuple[float, float, float]:
        if self.kind == "line":
            u = s / self.length
            return (self.start[0] + u * (self.end[0] - self.start[0]),
                    self.start[1] + u * (self.end[1] - self.start[1]), self.heading)
        rate = (self.k1 - self.k0) / self.length
        local = _spiral_local(s, self.k0, rate)
        rotation = complex(math.cos(self.heading), math.sin(self.heading))
        xy = complex(*self.start) + rotation * local
        if s == self.length:
            xy = complex(*self.end)
        return xy.real, xy.imag, self.heading + self.k0 * s + rate * s * s / 2


def _horizontal(al: dict) -> list[_Segment]:
    if not isinstance(al, dict):
        raise CivilError("invalid_alignment", "Alignment must be a dictionary")
    _number(al.get("start_station"), "start_station")
    if al.get("station_equations"):
        raise CivilError("station_equations_unsupported", "aicad.civil/1 uses continuous horizontal-distance stations")
    elements = al.get("horizontal")
    if not isinstance(elements, list) or not elements:
        raise CivilError("horizontal_elements_required", "Supply at least one nonzero line, arc or spiral")
    segments = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            raise CivilError("invalid_horizontal_element", element_index=index)
        kind = element.get("type")
        start = _xy(element.get("start"), "start")
        if kind == "line":
            end = _xy(element.get("end"), "end")
            size = math.dist(start, end)
            if size == 0:
                raise CivilError("zero_length_element", element_index=index)
            heading = math.atan2(end[1] - start[1], end[0] - start[0])
            k0 = k1 = 0.0
        elif kind == "arc":
            end = _xy(element.get("end"), "end")
            center = _xy(element.get("center"), "center")
            radius = _number(element.get("radius"), "radius", positive=True)
            sign = _turn(element.get("dir"))
            if max(abs(math.dist(start, center) - radius), abs(math.dist(end, center) - radius)) > _XY_TOL:
                raise CivilError("arc_radius_mismatch", element_index=index)
            if math.dist(start, end) < 1e-12:
                raise CivilError("ambiguous_full_circle", "A coincident arc start/end does not define a sweep", element_index=index)
            a0 = math.atan2(start[1] - center[1], start[0] - center[0])
            a1 = math.atan2(end[1] - center[1], end[0] - center[0])
            sweep = (sign * (a1 - a0)) % math.tau
            size, heading = radius * sweep, a0 + sign * math.pi / 2
            k0 = k1 = sign / radius
        elif kind == "spiral":
            sign = _turn(element.get("dir"))
            size = _number(element.get("length"), "length", positive=True)
            heading = _number(element.get("start_heading"), "start_heading")
            if "radius_start" not in element or "radius_end" not in element:
                raise CivilError("spiral_radius_required", "Use null for infinite radius")
            k0 = _curvature(element["radius_start"], sign, "radius_start")
            k1 = _curvature(element["radius_end"], sign, "radius_end")
            local = _spiral_local(size, k0, (k1 - k0) / size)
            xy = complex(*start) + complex(math.cos(heading), math.sin(heading)) * local
            end = xy.real, xy.imag
            if "end" in element and math.dist(end, _xy(element["end"], "end")) > _XY_TOL:
                raise CivilError("spiral_endpoint_mismatch", element_index=index)
        else:
            raise CivilError("unsupported_horizontal_element", element_type=kind, element_index=index)
        if segments:
            previous = segments[-1]
            if math.dist(previous.end, start) > _XY_TOL:
                raise CivilError("horizontal_not_contiguous", element_index=index)
            prior_heading = previous.at(previous.length)[2]
            heading += math.tau * round((prior_heading - heading) / math.tau)
        segments.append(_Segment(kind, start, end, size, heading, k0, k1))
    return segments


def _vertical(al: dict) -> tuple[list[dict], list[dict]]:
    vertical = al.get("vertical")
    if vertical is None:
        raise CivilError("vertical_profile_required", "Define at least two PVIs")
    pvis = vertical.get("pvis") if isinstance(vertical, dict) else None
    if not isinstance(pvis, list) or len(pvis) < 2:
        raise CivilError("vertical_pvis_required", "Define at least two PVIs")
    rows = []
    for item in pvis:
        if not isinstance(item, dict):
            raise CivilError("invalid_vertical_pvi")
        rows.append({"station": _number(item.get("station"), "station"),
                     "elevation": _number(item.get("elevation"), "elevation"),
                     "curve_length": _number(item.get("curve_length"), "curve_length", nonnegative=True)})
    if any(b["station"] <= a["station"] for a, b in zip(rows, rows[1:])):
        raise CivilError("pvi_stations_not_increasing")
    if rows[0]["curve_length"] or rows[-1]["curve_length"]:
        raise CivilError("endpoint_vertical_curve", "Only interior PVIs can have symmetric curves")
    curves = []
    for index in range(1, len(rows) - 1):
        before, pvi, after = rows[index - 1:index + 2]
        g1 = (pvi["elevation"] - before["elevation"]) / (pvi["station"] - before["station"])
        g2 = (after["elevation"] - pvi["elevation"]) / (after["station"] - pvi["station"])
        size = pvi["curve_length"]
        pvc, pvt = pvi["station"] - size / 2, pvi["station"] + size / 2
        if pvc < rows[0]["station"] or pvt > rows[-1]["station"]:
            raise CivilError("vertical_curve_outside_profile", pvi_index=index)
        if curves and pvc < curves[-1]["pvt_station_m"] - 1e-9:
            raise CivilError("vertical_curves_overlap", pvi_index=index)
        change_pct = 100 * (g2 - g1)
        extremum = -g1 * size / (g2 - g1) if size and g2 != g1 else None
        root = pvc + extremum if extremum is not None and 0 <= extremum <= size else None
        curves.append({"pvi_index": index, "pvi_station_m": pvi["station"],
                       "pvc_station_m": pvc, "pvt_station_m": pvt, "length_m": size,
                       "pvc_elevation_m": pvi["elevation"] - g1 * size / 2,
                       "grade_in_pct": 100 * g1, "grade_out_pct": 100 * g2,
                       "grade_change_pct": change_pct,
                       "k_m_per_pct": size / abs(change_pct) if change_pct else None,
                       "kind": "crest" if g2 < g1 else "sag" if g2 > g1 else "constant_grade",
                       "high_point_station_m": root if g2 < g1 else None,
                       "low_point_station_m": root if g2 > g1 else None})
    return rows, curves


def validate(al: dict) -> list[str]:
    """Return geometric/vertical structural errors; an empty list means valid.

    Contract §3.2 requires XY continuity, not tangency: explicit line-line corners
    remain valid. PI-generated spiral joins are tangent and curvature-continuous.
    """
    errors = []
    try:
        _horizontal(al)
    except CivilError as exc:
        errors.append(exc.reason)
    if isinstance(al, dict) and al.get("vertical") is not None:
        try:
            _vertical(al)
        except CivilError as exc:
            errors.append(exc.reason)
    return errors


def build(horizontal: list[dict], vertical: dict | None = None, *, id: str = "alignment",
          name: str = "Alignment", start_station: float = 0.0) -> dict:
    """Build and validate an aicad.civil/1 §3.2 alignment (LandXML 1.2 geometry)."""
    if not isinstance(id, str) or not id or not isinstance(name, str) or not name:
        raise CivilError("invalid_alignment_identity")
    al = {"id": id, "name": name, "start_station": _number(start_station, "start_station"),
          "horizontal": deepcopy(horizontal), "vertical": deepcopy(vertical)}
    _horizontal(al)
    if vertical is not None:
        _vertical(al)
    return al


def from_pis(pis, radii, spiral_lengths=None, *, id: str = "alignment", name: str = "Alignment",
             start_station: float = 0.0) -> dict:
    """Construct tangent/spiral/arc/spiral/tangent geometry from ordered PIs.

    ``radii`` is a positive scalar or one radius per interior PI. Spiral lengths
    are a scalar, one length per PI, or [entry, exit] pairs per PI (metres).
    Absent spirals means circular curves, not an inferred design length.
    Exact spiral endpoint geometry gives the tangent shifts; for symmetric
    transitions it is equivalent to T=(R+p)tan(|delta|/2)+q, with
    p=y_s-R(1-cos(theta_s)), q=x_s-R*sin(theta_s).
    Reference: Euler spiral/standard highway transition geometry; AASHTO 2018.
    """
    if not isinstance(pis, (list, tuple)) or len(pis) < 2:
        raise CivilError("pi_points_required")
    points = [_xy(p, "pi") for p in pis]
    if any(math.dist(a, b) == 0 for a, b in zip(points, points[1:])):
        raise CivilError("duplicate_pi")
    count = len(points) - 2
    if isinstance(radii, (int, float, np.number)) and not isinstance(radii, bool):
        radii = [radii] * count
    if not isinstance(radii, (list, tuple)) or len(radii) != count:
        raise CivilError("pi_radius_count", "Provide one radius per interior PI")
    radii = [_number(r, "radius", positive=True) for r in radii]
    if spiral_lengths is None:
        lengths = [(0.0, 0.0)] * count
    else:
        if isinstance(spiral_lengths, (int, float, np.number)) and not isinstance(spiral_lengths, bool):
            spiral_lengths = [spiral_lengths] * count
        if not isinstance(spiral_lengths, (list, tuple)) or len(spiral_lengths) != count:
            raise CivilError("pi_spiral_count")
        lengths = []
        for value in spiral_lengths:
            pair = value if isinstance(value, (list, tuple)) else (value, value)
            if len(pair) != 2:
                raise CivilError("invalid_spiral_lengths")
            lengths.append(tuple(_number(v, "spiral_length", nonnegative=True) for v in pair))
    curves = []
    for i, (radius, (entry, exit)) in enumerate(zip(radii, lengths), start=1):
        previous, pi, following = points[i - 1:i + 2]
        heading = math.atan2(pi[1] - previous[1], pi[0] - previous[0])
        outgoing = math.atan2(following[1] - pi[1], following[0] - pi[0])
        delta = math.atan2(math.sin(outgoing - heading), math.cos(outgoing - heading))
        if abs(delta) >= math.pi - 1e-9:
            raise CivilError("pi_deflection_invalid", "A reversing PI cannot have a finite tangent curve", pi_index=i)
        if abs(delta) < 1e-12:
            if entry or exit:
                raise CivilError("spiral_without_deflection", pi_index=i)
            curves.append({"start": pi, "end": pi, "incoming": 0.0, "outgoing": 0.0, "elements": []})
            continue
        sign = 1 if delta > 0 else -1
        turn = "ccw" if sign > 0 else "cw"
        k = sign / radius
        beta = abs(delta) - (entry + exit) / (2 * radius)
        if beta < 0:
            raise CivilError("spirals_exceed_deflection", pi_index=i)
        entry_xy = _spiral_local(entry, 0.0, k / entry) if entry else 0j
        entry_angle = k * entry / 2
        arc_len = radius * beta
        arc_xy = _spiral_local(arc_len, k, 0.0) * complex(math.cos(entry_angle), math.sin(entry_angle))
        exit_angle = entry_angle + sign * beta
        exit_xy = (_spiral_local(exit, k, -k / exit) * complex(math.cos(exit_angle), math.sin(exit_angle))) if exit else 0j
        displacement = entry_xy + arc_xy + exit_xy
        tout = displacement.imag / math.sin(delta)
        tin = displacement.real - tout * math.cos(delta)
        if min(tin, tout) < 0:
            raise CivilError("pi_curve_not_constructible", pi_index=i)
        rotation = complex(math.cos(heading), math.sin(heading))
        start_xy = complex(*pi) - tin * rotation
        cursor, theta = start_xy, heading
        elements = []
        if entry:
            elements.append({"type": "spiral", "start": [cursor.real, cursor.imag], "start_heading": theta,
                             "length": entry, "radius_start": None, "radius_end": radius, "dir": turn})
            cursor += rotation * entry_xy
            theta += entry_angle
        if arc_len:
            center = cursor + complex(-math.sin(theta), math.cos(theta)) / k
            endpoint = cursor + complex(math.cos(theta), math.sin(theta)) * _spiral_local(arc_len, k, 0.0)
            elements.append({"type": "arc", "start": [cursor.real, cursor.imag], "end": [endpoint.real, endpoint.imag],
                             "center": [center.real, center.imag], "radius": radius, "dir": turn})
            cursor = endpoint
            theta += sign * beta
        if exit:
            elements.append({"type": "spiral", "start": [cursor.real, cursor.imag], "start_heading": theta,
                             "length": exit, "radius_start": radius, "radius_end": None, "dir": turn})
            cursor += complex(math.cos(theta), math.sin(theta)) * _spiral_local(exit, k, -k / exit)
        curves.append({"start": (start_xy.real, start_xy.imag), "end": (cursor.real, cursor.imag),
                       "incoming": tin, "outgoing": tout, "elements": elements})
    for i in range(len(points) - 1):
        used = (curves[i - 1]["outgoing"] if i else 0.0) + (curves[i]["incoming"] if i < count else 0.0)
        if used > math.dist(points[i], points[i + 1]):
            raise CivilError("pi_curves_overlap", pi_leg_index=i)
    elements, cursor = [], points[0]
    for curve in curves:
        if math.dist(cursor, curve["start"]) > 1e-10:
            elements.append({"type": "line", "start": list(cursor), "end": list(curve["start"])})
        elements.extend(curve["elements"])
        cursor = curve["end"]
    if math.dist(cursor, points[-1]) > 1e-10:
        elements.append({"type": "line", "start": list(cursor), "end": list(points[-1])})
    return build(elements, id=id, name=name, start_station=start_station)


def length(al: dict) -> float:
    """Plan length_m: line length, R*delta and spiral length (LandXML 1.2 CoordGeom)."""
    return sum(s.length for s in _horizontal(al))


def _point(segments: list[_Segment], start_station: float, station: float) -> tuple[float, float, float]:
    total = sum(s.length for s in segments)
    station = _number(station, "station")
    if station < start_station or station > start_station + total:
        raise CivilError("station_outside_alignment", station_m=station,
                         minimum_m=start_station, maximum_m=start_station + total)
    if station == start_station + total:
        return segments[-1].at(segments[-1].length)
    distance = station - start_station
    accumulated = 0.0
    for segment in segments:
        if distance <= accumulated + segment.length:
            return segment.at(distance - accumulated)
        accumulated += segment.length
    return segments[-1].at(segments[-1].length)


def point_at(al: dict, station: float) -> tuple[float, float, float]:
    """Return (x_m,y_m,heading_rad) by LandXML 1.2/Euler geometry, with no extrapolation."""
    return _point(_horizontal(al), float(al["start_station"]), station)


def station_offset(al: dict, x: float, y: float) -> tuple[float, float]:
    """Nearest plan projection: (station_m, signed offset_m), positive to the left.

    All elements compete for the nearest point. Equal-distance/self-intersection
    ties choose the earliest station. At bounded endpoints the offset is the
    normal component, not an extrapolated station or longitudinal residual.
    Spiral distance minimization is subdivided at <= pi/16 heading increments.
    """
    target = _number(x, "x"), _number(y, "y")
    segments = _horizontal(al)
    best = None
    chainage = float(al["start_station"])
    for segment in segments:
        candidates = [0.0, segment.length]
        if segment.kind == "line":
            dx, dy = segment.end[0] - segment.start[0], segment.end[1] - segment.start[1]
            s = ((target[0] - segment.start[0]) * dx + (target[1] - segment.start[1]) * dy) / segment.length
            candidates.append(max(0.0, min(segment.length, s)))
        elif segment.kind == "arc" or segment.k0 == segment.k1:
            if segment.k0 == 0:
                s = ((target[0] - segment.start[0]) * math.cos(segment.heading)
                     + (target[1] - segment.start[1]) * math.sin(segment.heading))
                candidates.append(max(0.0, min(segment.length, s)))
            else:
                k = segment.k0
                center = (segment.start[0] - math.sin(segment.heading) / k,
                          segment.start[1] + math.cos(segment.heading) / k)
                a0 = math.atan2(segment.start[1] - center[1], segment.start[0] - center[0])
                a1 = math.atan2(target[1] - center[1], target[0] - center[0])
                sweep = (math.copysign(1, k) * (a1 - a0)) % math.tau
                s = sweep / abs(k)
                while s <= segment.length:
                    candidates.append(s)
                    s += math.tau / abs(k)
        else:
            n = max(8, math.ceil(segment.length / 50), math.ceil(segment.length * max(abs(segment.k0), abs(segment.k1)) / (math.pi / 16)))
            def squared(s):
                px, py, _ = segment.at(s)
                return (px - target[0]) ** 2 + (py - target[1]) ** 2
            for j in range(n):
                lo, hi = segment.length * j / n, segment.length * (j + 1) / n
                optimum = minimize_scalar(squared, bounds=(lo, hi), method="bounded",
                                          options={"xatol": 1e-11, "maxiter": 100})
                if not optimum.success:
                    raise CivilError("projection_not_converged")
                candidates.extend((lo, hi, float(optimum.x)))
        for s in candidates:
            px, py, theta = segment.at(s)
            distance2 = (target[0] - px) ** 2 + (target[1] - py) ** 2
            station = chainage + s
            offset = -(target[0] - px) * math.sin(theta) + (target[1] - py) * math.cos(theta)
            if best is None or distance2 < best[0] - 1e-12 or (abs(distance2 - best[0]) <= 1e-12 and station < best[1]):
                best = distance2, station, offset
        chainage += segment.length
    return best[1], best[2]


def _stations(start: float, end: float, step: float) -> list[float]:
    step = _number(step, "step", positive=True)
    n = math.floor((end - start) / step)
    values = [start + i * step for i in range(n + 1)]
    if values[-1] < end:
        values.append(end)
    return values


def densify(al: dict, step: float = 10.0) -> dict:
    """Sample LandXML 1.2/Euler plan geometry at <= step metres, including joins."""
    segments = _horizontal(al)
    start = float(al["start_station"])
    total = sum(s.length for s in segments)
    stations = _stations(start, start + total, step)
    joins, distance = [], 0.0
    for segment in segments:
        distance += segment.length
        joins.append(start + distance)
    stations = sorted(set(stations + joins))
    coordinates = [_point(segments, start, s) for s in stations]
    return _result("Horizontal alignment sampling", _GEOMETRY_REFERENCE,
                   stations_m=stations, points=[[x, y] for x, y, _ in coordinates],
                   headings_rad=[h for _, _, h in coordinates], units={"points": "m"})


def vertical_curves(al: dict) -> dict:
    """AASHTO 2018 parabolic parameters, K=L/|A_pct|, and grade-zero stations."""
    _, curves = _vertical(al)
    return _result("Symmetric parabolic vertical curves", _VERTICAL_REFERENCE, curves=curves)


def _vertical_value(rows: list[dict], curves: list[dict], station: float, derivative: bool) -> float:
    s = _number(station, "station")
    if not rows[0]["station"] <= s <= rows[-1]["station"]:
        raise CivilError("station_outside_vertical", station_m=s)
    for curve in curves:
        size = curve["length_m"]
        if size and curve["pvc_station_m"] <= s <= curve["pvt_station_m"]:
            x = s - curve["pvc_station_m"]
            g1, change = curve["grade_in_pct"] / 100, curve["grade_change_pct"] / 100
            if derivative:
                return g1 + change * x / size
            return curve["pvc_elevation_m"] + g1 * x + change * x * x / (2 * size)
    index = min(len(rows) - 2, bisect_right([r["station"] for r in rows], s) - 1)
    first, second = rows[index:index + 2]
    g = (second["elevation"] - first["elevation"]) / (second["station"] - first["station"])
    return g if derivative else first["elevation"] + g * (s - first["station"])


def elevation_at(al: dict, station: float) -> float:
    """TxDOT/AASHTO 2018 elevation_m: z=z_PVC+g1*x+(g2-g1)*x²/(2L), grades decimal."""
    rows, curves = _vertical(al)
    return _vertical_value(rows, curves, station, False)


def grade(al: dict, station: float) -> float:
    """Derivative of the AASHTO 2018 parabola, decimal dz/ds; sharp PVIs use outgoing grade."""
    rows, curves = _vertical(al)
    return _vertical_value(rows, curves, station, True)


def vertical_curve_checks(al: dict, design_speed_kmh: float, criteria: dict | None = None) -> dict:
    """AASHTO 2018 level-road SSD and crest/sag stopping-sight-distance checks.

    SSD=0.278*V*2.5+0.039*V²/3.4. Design SSD rounds up to the next 5 m
    (Green Book Table 3-1: 100 km/h -> 185 m); raw SSD is also reported.
    Crest uses C=658 (h1=1.08 m, h2=0.60 m); sag headlight uses
    C=120+3.5*S (headlight height 0.60 m, beam angle 1 degree).
    The S>L branch is L=2*S-C/A; the S<=L branch is L=A*S²/C,
    with A in %. A nonpositive short-curve requirement is reported as zero.
    Optional criteria: ssd_m, crest_k_m_per_pct, sag_k_m_per_pct,
    references, either flat or in a shared profile's overrides.
    These checks do not assess horizontal obstructions or sag passenger comfort.
    """
    speed = _number(design_speed_kmh, "design_speed_kmh", positive=True)
    vertical_keys = {"ssd_m", "crest_k_m_per_pct", "sag_k_m_per_pct"}
    if criteria is None:
        limits = {}
    elif not isinstance(criteria, dict):
        raise CivilError("invalid_criteria")
    elif "profile" in criteria or "overrides" in criteria:
        from .criteria import resolve
        overrides = criteria.get("overrides", {})
        if not isinstance(overrides, dict) or not isinstance(overrides.get("references", {}), dict):
            raise CivilError("invalid_criteria")
        references = overrides.get("references", {})
        shared = {**criteria, "overrides": {k: v for k, v in overrides.items() if k not in vertical_keys}}
        shared["overrides"]["references"] = {k: v for k, v in references.items() if k not in vertical_keys}
        limits = resolve(shared)
        limits.update({k: v for k, v in overrides.items() if k in vertical_keys})
        limits["references"].update({k: v for k, v in references.items() if k in vertical_keys})
    else:
        unknown = set(criteria) - vertical_keys - {"references"}
        if unknown:
            raise CivilError("unknown_criteria_key", keys=sorted(unknown))
        limits = criteria
    references = limits.get("references", {})
    if not isinstance(references, dict):
        raise CivilError("invalid_criteria", "references must be an object")
    for key in vertical_keys:
        if key in limits:
            _number(limits[key], key, positive=key == "ssd_m", nonnegative=key != "ssd_m")
        if key in references and (not isinstance(references[key], str) or not references[key].strip()):
            raise CivilError("invalid_criteria", key=key)
    _, curves = _vertical(al)
    raw_ssd = 0.278 * speed * 2.5 + 0.039 * speed * speed / 3.4
    ssd = _number(limits["ssd_m"], "ssd_m", positive=True) if "ssd_m" in limits else 5 * math.ceil(raw_ssd / 5)
    rows, checks = [], []
    for curve in curves:
        if curve["kind"] == "constant_grade":
            rows.append({**curve, "required_length_m": 0.0, "required_k_m_per_pct": None})
            continue
        a = abs(curve["grade_change_pct"])
        constant = 658.0 if curve["kind"] == "crest" else 120.0 + 3.5 * ssd
        long_k = ssd * ssd / constant
        key = curve["kind"] + "_k_m_per_pct"
        reference = _VERTICAL_REFERENCE
        if key in limits:
            required_k = _number(limits[key], key, nonnegative=True)
            required_length = a * required_k
            branch = "criteria_override"
            reference = limits.get("references", {}).get(key, "User-supplied vertical alignment criterion")
        elif a * ssd >= constant:
            required_length, required_k, branch = a * long_k, long_k, "S<=L"
        else:
            required_length = max(0.0, 2 * ssd - constant / a)
            required_k, branch = required_length / a, "S>L"
        rows.append({**curve, "required_length_m": required_length,
                     "required_k_m_per_pct": required_k, "long_curve_k_m_per_pct": long_k,
                     "sight_distance_branch": branch})
        checks.append({"check": curve["kind"] + "_stopping_sight_distance", "element_id": al.get("id"),
                       "pvi_station_m": curve["pvi_station_m"], "value": curve["k_m_per_pct"],
                       "limit": required_k, "units": "m/%", "pass": curve["length_m"] + 1e-9 >= required_length,
                       "criterion": key if key in limits else "AASHTO 2018 stopping sight distance",
                       "reference": reference})
    result = _result("Vertical curve stopping sight distance", _VERTICAL_REFERENCE,
                     design_speed_kmh=speed, computed_ssd_m=raw_ssd, stopping_sight_distance_m=ssd,
                     curves=rows, checks=checks)
    result["assumptions"] = ["Level-road braking, reaction time 2.5 s, deceleration 3.4 m/s².",
                             "Crest eye/object heights 1.08/0.60 m; sag headlights 0.60 m and 1 degree.",
                             "Design SSD is equation-based and rounded upward to 5 m unless explicitly overridden."]
    return result


def _ground(z_at: Callable, x: float, y: float, *, required: bool = False) -> float | None:
    if not callable(z_at):
        raise CivilError("surface_sampler_required")
    z = z_at(x, y)
    if z is None:
        if required:
            raise CivilError("surface_outside", "Ground must cover the full cross-section/daylight ray", x_m=x, y_m=y)
        return None
    return _number(z, "ground_elevation")


def existing_ground_profile(al: dict, z_at: Callable, step: float = 10.0) -> dict:
    """Contract §3.2 centreline station sampling; None outside ground, never extrapolate."""
    sampling = densify(al, step)
    rows = [{"station_m": s, "x_m": xy[0], "y_m": xy[1], "ground_elevation_m": _ground(z_at, *xy)}
            for s, xy in zip(sampling["stations_m"], sampling["points"])]
    result = _result("Existing ground centreline profile", "Station sampling of supplied z_at(x,y)", samples=rows)
    missing = sum(row["ground_elevation_m"] is None for row in rows)
    if missing:
        result["warnings"].append(f"{missing} stations are outside the supplied ground surface; elevations are null.")
    return result


def design_vs_existing(al: dict, z_at: Callable, step: float = 10.0) -> dict:
    """TxDOT/AASHTO parabolic design versus sampled ground: cut=ground-design, fill opposite."""
    result = existing_ground_profile(al, z_at, step)
    rows, curves = _vertical(al)
    for sample in result["samples"]:
        design = _vertical_value(rows, curves, sample["station_m"], False)
        ground = sample["ground_elevation_m"]
        sample.update({"design_elevation_m": design,
                       "cut_depth_m": None if ground is None else max(0.0, ground - design),
                       "fill_depth_m": None if ground is None else max(0.0, design - ground)})
    result["method"] = {"name": "Design versus existing centreline profile", "reference": _VERTICAL_REFERENCE}
    return result


def _template(template: dict) -> dict:
    if not isinstance(template, dict):
        raise CivilError("invalid_section_template")
    return {"width_left": _number(template.get("width_left"), "width_left", nonnegative=True),
            "width_right": _number(template.get("width_right"), "width_right", nonnegative=True),
            "cross_slope_pct": _number(template.get("cross_slope_pct"), "cross_slope_pct"),
            "cut_slope": _number(template.get("cut_slope"), "cut_slope", positive=True),
            "fill_slope": _number(template.get("fill_slope"), "fill_slope", positive=True),
            "depth_below_finish": _number(template.get("depth_below_finish", 0.0), "depth_below_finish", nonnegative=True)}


def _areas(offsets: list[float], ground: list[float], design: list[float]) -> tuple[float, float, list, list]:
    """Separate signed strip polygons at every linear ground/design crossing."""
    areas = {"cut": 0.0, "fill": 0.0}
    polygons = {"cut": [], "fill": []}
    for x0, x1, g0, g1, d0, d1 in zip(offsets, offsets[1:], ground, ground[1:], design, design[1:]):
        difference0, difference1 = g0 - d0, g1 - d1
        splits = [0.0, 1.0]
        if difference0 * difference1 < 0:
            splits.insert(1, difference0 / (difference0 - difference1))
        for t0, t1 in zip(splits, splits[1:]):
            xa, xb = x0 + t0 * (x1 - x0), x0 + t1 * (x1 - x0)
            ga, gb = g0 + t0 * (g1 - g0), g0 + t1 * (g1 - g0)
            da, db = d0 + t0 * (d1 - d0), d0 + t1 * (d1 - d0)
            sign = ((ga - da) + (gb - db)) / 2
            if sign == 0:
                continue
            kind = "cut" if sign > 0 else "fill"
            areas[kind] += (xb - xa) * (abs(ga - da) + abs(gb - db)) / 2
            polygons[kind].append([[xa, ga], [xb, gb], [xb, db], [xa, da]])
    return areas["cut"], areas["fill"], polygons["cut"], polygons["fill"]


def _section(segments, start, vertical_rows, curves, z_at, template, station, ground_step, max_daylight, tolerance):
    x, y, theta = _point(segments, start, station)
    finish = _vertical_value(vertical_rows, curves, station, False)
    base = finish - template["depth_below_finish"]
    cross_slope = template["cross_slope_pct"] / 100
    left_x, left_y = -math.sin(theta), math.cos(theta)
    def ground(offset):
        return _ground(z_at, x + offset * left_x, y + offset * left_y, required=True)
    def platform(offset):
        return base - cross_slope * abs(offset)
    sides = []
    for direction, width in ((-1, template["width_right"]), (1, template["width_left"])):
        edge = direction * width
        edge_z = platform(edge)
        edge_difference = ground(edge) - edge_z
        if abs(edge_difference) <= tolerance:
            sides.append((edge, edge, edge_z, 0.0))
            continue
        slope = 1 / template["cut_slope"] if edge_difference > 0 else -1 / template["fill_slope"]
        def difference(distance):
            return ground(edge + direction * distance) - (edge_z + slope * distance)
        low, f_low = 0.0, edge_difference
        found = False
        while low < max_daylight:
            high = min(low + ground_step, max_daylight)
            f_high = difference(high)
            if f_low * f_high <= 0:
                found = True
                break
            low, f_low = high, f_high
        if not found:
            raise CivilError("daylight_not_found", "No daylight intersection inside max_daylight", station_m=station,
                             side="left" if direction > 0 else "right", max_daylight_m=max_daylight)
        for _ in range(100):
            middle = (low + high) / 2
            f_middle = difference(middle)
            if high - low <= tolerance:
                break
            if f_low * f_middle <= 0:
                high = middle
            else:
                low, f_low = middle, f_middle
        else:
            raise CivilError("daylight_not_converged", station_m=station)
        distance = (low + high) / 2
        sides.append((edge + direction * distance, edge, edge_z, slope))
    right, left = sides
    begin, end = right[0], left[0]
    offsets = sorted(set(_stations(begin, end, ground_step) + [right[1], 0.0, left[1]]))
    ground_z = [ground(o) for o in offsets]
    def design(offset):
        if offset < right[1]:
            return right[2] + right[3] * (right[1] - offset)
        if offset > left[1]:
            return left[2] + left[3] * (offset - left[1])
        return platform(offset)
    design_z = [design(o) for o in offsets]
    cut, fill, cut_polygons, fill_polygons = _areas(offsets, ground_z, design_z)
    return {"station_m": float(station), "x_m": x, "y_m": y, "heading_rad": theta,
            "finish_elevation_m": finish, "subgrade_elevation_m": base,
            "daylight_left_offset_m": end, "daylight_right_offset_m": begin,
            "cut_area_m2": cut, "fill_area_m2": fill,
            "ground_points": [[o, z] for o, z in zip(offsets, ground_z)],
            "design_points": [[o, z] for o, z in zip(offsets, design_z)],
            "cut_polygons": cut_polygons, "fill_polygons": fill_polygons,
            "units": {"ground_points": "[offset_m,elevation_m]", "design_points": "[offset_m,elevation_m]",
                      "cut_polygons": "[offset_m,elevation_m]", "fill_polygons": "[offset_m,elevation_m]"}}


def cross_sections(al: dict, z_at: Callable, template: dict, stations, *, ground_step: float = 0.5,
                   max_daylight: float = 10000.0, daylight_tolerance: float = 1e-7) -> dict:
    """Template/ground polygons, cut/fill areas, and independently computed mid-sections.

    Offsets are left-positive; widths and depths are metres. A positive
    cross_slope_pct falls away from the centreline on BOTH sides (a crown).
    cut_slope/fill_slope are horizontal:vertical ratios. Side slopes originate
    at subgrade, finish minus depth_below_finish. First sampled daylight crossing
    on each outward offset ray is refined by bisection. Ground between samples
    is piecewise linear: areas are exact for that sampled profile, not an
    assertion of exact integration of an arbitrary ground callable. Surface
    gaps or missing daylight cause an explicit refusal, never truncated areas.
    Reference: LibreTexts, Fundamentals of Transportation, §7.3 Earthwork;
    polygon strip integration, with linear interpolation between ground samples.
    """
    ground_step = _number(ground_step, "ground_step", positive=True)
    maximum = _number(max_daylight, "max_daylight", positive=True)
    tolerance = _number(daylight_tolerance, "daylight_tolerance", positive=True)
    if not callable(z_at):
        raise CivilError("surface_sampler_required")
    if not isinstance(stations, (list, tuple)) or not stations:
        raise CivilError("section_stations_required")
    positions = [_number(s, "station") for s in stations]
    if any(b <= a for a, b in zip(positions, positions[1:])):
        raise CivilError("section_stations_not_increasing")
    segments, specification = _horizontal(al), _template(template)
    vertical_rows, curves = _vertical(al)
    start = float(al["start_station"])
    def section(station):
        return _section(segments, start, vertical_rows, curves, z_at, specification,
                        station, ground_step, maximum, tolerance)
    sections = [section(s) for s in positions]
    mids = [section((a + b) / 2) for a, b in zip(positions, positions[1:])]
    result = _result("Template daylight and cross-section polygon areas", _EARTHWORK_REFERENCE,
                     sections=sections, mid_sections=mids, ground_sample_step_m=ground_step,
                     daylight_tolerance_m=tolerance, element_ids=[al["id"]] if al.get("id") else [])
    result["assumptions"] = ["Ground is interpolated linearly between sampled offsets.",
                             "Positive cross slope is a symmetric crown; side slopes are H:V.",
                             "Daylight selects the first crossing bracketed by the ground sampling interval."]
    return result


def _section_areas(row: dict) -> tuple[float, float, float]:
    if not isinstance(row, dict):
        raise CivilError("invalid_earthwork_section")
    return (_number(row.get("station_m"), "station_m"),
            _number(row.get("cut_area_m2"), "cut_area_m2", nonnegative=True),
            _number(row.get("fill_area_m2"), "fill_area_m2", nonnegative=True))


def _real_roots(coefficients: list[float]) -> list[float]:
    while len(coefficients) > 1 and coefficients[-1] == 0:
        coefficients = coefficients[:-1]
    if len(coefficients) == 1:
        return []
    return sorted(float(root.real) for root in np.polynomial.polynomial.polyroots(coefficients)
                  if abs(root.imag) <= 1e-8 and 0 <= root.real <= 1)


def mass_haul(earthwork_result: dict) -> dict:
    """Mass-curve zero-balance stations and extrema, on a bank-volume basis.

    Roots integrate each interval's linear (AEA) or quadratic (prismoidal)
    section-area interpolation. Balance points refer to the initial ordinate
    zero, not arbitrary equal-ordinate haul pairs. This is a material balance
    summary, not a haul-cost optimization or a claim of average haul distance.
    Reference: LibreTexts, Fundamentals of Transportation, §7.3 Earthwork.
    """
    intervals = earthwork_result.get("intervals") if isinstance(earthwork_result, dict) else None
    if not isinstance(intervals, list) or not intervals:
        raise CivilError("earthwork_intervals_required")
    previous_end = previous_mass = None
    normalized = []
    for interval in intervals:
        if not isinstance(interval, dict):
            raise CivilError("invalid_earthwork_interval")
        start = _number(interval.get("start_station_m"), "start_station_m")
        end = _number(interval.get("end_station_m"), "end_station_m")
        mass_start = _number(interval.get("mass_start_m3"), "mass_start_m3")
        mass_end = _number(interval.get("mass_end_m3"), "mass_end_m3")
        if previous_end is None and abs(mass_start) > 1e-10:
            raise CivilError("mass_curve_initial_ordinate_not_zero")
        polynomial = interval.get("net_area_polynomial_m2")
        if end <= start or not isinstance(polynomial, list) or len(polynomial) != 3:
            raise CivilError("invalid_earthwork_interval")
        values = [_number(value, "net_area_polynomial_m2") for value in polynomial]
        if previous_end is not None and (abs(start - previous_end) > 1e-8 or abs(mass_start - previous_mass) > 1e-8):
            raise CivilError("mass_curve_discontinuous")
        integrated = (end - start) * (values[0] + values[1] / 2 + values[2] / 3)
        if abs((mass_end - mass_start) - integrated) > max(1e-8, abs(integrated) * 1e-10):
            raise CivilError("mass_curve_volume_mismatch")
        previous_end, previous_mass = end, mass_end
        normalized.append({"start_station_m": start, "end_station_m": end,
                           "mass_start_m3": mass_start, "mass_end_m3": mass_end,
                           "net_area_polynomial_m2": values})
    intervals = normalized
    balance, plateaus, extrema = [intervals[0]["start_station_m"]], [], []
    for interval in intervals:
        start, end = interval["start_station_m"], interval["end_station_m"]
        span = end - start
        a0, a1, a2 = interval["net_area_polynomial_m2"]
        cumulative = interval["mass_start_m3"]
        if abs(cumulative) <= 1e-10:
            balance.append(start)
        if abs(interval["mass_end_m3"]) <= 1e-10:
            balance.append(end)
        coefficients = [cumulative, span * a0, span * a1 / 2, span * a2 / 3]
        if all(abs(c) < 1e-10 for c in coefficients):
            plateaus.append([start, end])
            balance.extend((start, end))
        else:
            balance.extend(start + u * span for u in _real_roots(coefficients))
        for u in _real_roots([a0, a1, a2]):
            if 0 < u < 1:
                mass = cumulative + span * (a0 * u + a1 * u * u / 2 + a2 * u ** 3 / 3)
                extrema.append({"station_m": start + u * span, "mass_ordinate_m3": mass})
    for first, second in zip(intervals, intervals[1:]):
        first_end = sum(first["net_area_polynomial_m2"])
        second_start = second["net_area_polynomial_m2"][0]
        if first_end * second_start < 0:
            extrema.append({"station_m": first["end_station_m"], "mass_ordinate_m3": first["mass_end_m3"]})
    unique = []
    for value in sorted(balance):
        if not unique or value - unique[-1] > 1e-7:
            unique.append(value)
    net = intervals[-1]["mass_end_m3"]
    result = _result("Bank-volume mass haul balance", _EARTHWORK_REFERENCE,
                     balance_points_m=unique, balance_intervals_m=plateaus,
                     turning_points=sorted(extrema, key=lambda row: row["station_m"]),
                     net_bank_m3=net, borrow_bank_m3=max(0.0, -net), waste_bank_m3=max(0.0, net))
    result["assumptions"] = ["All cut is suitable for fill; no haul limits or material classes were supplied.",
                             "Balance datum is the initial cumulative mass ordinate (zero)."]
    return result


def earthwork(sections, method: str = "average_end_area", shrink: float = 1.0, swell: float = 1.0) -> dict:
    """AEA L(A1+A2)/2 or prismoidal L(A1+4Am+A2)/6 earthwork (LibreTexts §7.3).

    ``sections`` is the cross_sections result, or a list of section area rows.
    Prismoidal requires actual midpoint rows in ``mid_sections``; it never
    fabricates midpoint areas from endpoint interpolation. cross_sections
    calculates these directly from alignment/template/ground.
    ``shrink`` is compacted-fill/bank volume and ``swell`` is loose/bank volume.
    Required bank fill = geometric fill/shrink. Mass = bank cut - bank fill;
    swell affects reported loose quantities, not the bank-volume mass curve.
    """
    if method not in ("average_end_area", "prismoidal"):
        raise CivilError("unsupported_earthwork_method")
    shrink = _number(shrink, "shrink", positive=True)
    swell = _number(swell, "swell", positive=True)
    primary = sections.get("sections") if isinstance(sections, dict) else sections
    mids = sections.get("mid_sections", []) if isinstance(sections, dict) else []
    element_ids = sections.get("element_ids", []) if isinstance(sections, dict) else []
    if not isinstance(element_ids, list) or any(not isinstance(value, str) or not value for value in element_ids):
        raise CivilError("invalid_earthwork_element_ids")
    if not isinstance(primary, list) or len(primary) < 2:
        raise CivilError("earthwork_sections_required", "At least two primary cross-sections are required")
    rows = [_section_areas(row) for row in primary]
    if any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise CivilError("section_stations_not_increasing")
    if method == "prismoidal" and (not isinstance(mids, list) or len(mids) != len(rows) - 1):
        raise CivilError("midpoint_section_required", "Supply one computed midpoint section for each interval")
    mid_rows = [_section_areas(row) for row in mids] if method == "prismoidal" else []
    intervals, cumulative = [], 0.0
    total_cut = total_fill = 0.0
    mass_rows = [{"station_m": rows[0][0], "mass_ordinate_m3": 0.0}]
    for index, (first, last) in enumerate(zip(rows, rows[1:])):
        station0, cut0, fill0 = first
        station1, cut1, fill1 = last
        span = station1 - station0
        if method == "average_end_area":
            cut, fill = span * (cut0 + cut1) / 2, span * (fill0 + fill1) / 2
            net0, net1 = cut0 - fill0 / shrink, cut1 - fill1 / shrink
            polynomial = [net0, net1 - net0, 0.0]
        else:
            middle, cutm, fillm = mid_rows[index]
            if abs(middle - (station0 + station1) / 2) > 1e-8:
                raise CivilError("midpoint_station_mismatch", interval_index=index)
            cut, fill = span * (cut0 + 4 * cutm + cut1) / 6, span * (fill0 + 4 * fillm + fill1) / 6
            net0, netm, net1 = cut0 - fill0 / shrink, cutm - fillm / shrink, cut1 - fill1 / shrink
            polynomial = [net0, 4 * netm - 3 * net0 - net1, 2 * net1 + 2 * net0 - 4 * netm]
        fill_bank = fill / shrink
        increment = cut - fill_bank
        intervals.append({"start_station_m": station0, "end_station_m": station1, "length_m": span,
                          "cut_m3": cut, "fill_m3": fill, "fill_bank_m3": fill_bank,
                          "cut_loose_m3": cut * swell, "fill_loose_m3": fill_bank * swell,
                          "net_bank_m3": increment, "mass_start_m3": cumulative,
                          "mass_end_m3": cumulative + increment, "net_area_polynomial_m2": polynomial})
        cumulative += increment
        total_cut += cut
        total_fill += fill
        mass_rows.append({"station_m": station1, "mass_ordinate_m3": cumulative})
    result = _result(method, _EARTHWORK_REFERENCE, intervals=intervals, mass_ordinates=mass_rows,
                     total_cut_m3=total_cut, total_fill_m3=total_fill, total_fill_bank_m3=total_fill / shrink,
                     total_cut_loose_m3=total_cut * swell, total_fill_loose_m3=total_fill / shrink * swell,
                     net_bank_m3=cumulative, shrink=shrink, swell=swell, element_ids=list(element_ids))
    result["mass_haul"] = mass_haul(result)
    result["assumptions"] = ["Shrink is compacted/bank volume; swell is loose/bank volume.",
                             "All cut is reusable fill; unsuitable material is not included."]
    return result

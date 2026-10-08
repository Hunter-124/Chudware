"""Dependency-free IFC 4.3 ADD2 ISO-10303-21 civil writer.

Reference: buildingSMART IFC 4.3.2.0 (ISO 16739-1:2024), alignment design
parameters, distribution systems and swept solids. Positive horizontal radii
turn left (CCW); zero represents an infinite radius. GUIDs use IFC's base64
alphabet, not MIME base64. No IFC runtime or third-party library is required.
"""
from __future__ import annotations

import math
import uuid
from collections import Counter
from pathlib import Path

from .errors import CivilError

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
_REFERENCE = "buildingSMART IFC 4.3.2.0 / IFC4X3_ADD2, ISO 16739-1:2024; ISO 10303-21"


def _guid(identity):
    number = uuid.uuid5(uuid.NAMESPACE_URL, "aicad.civil/1/" + identity).int
    chars = []
    for _ in range(22):
        chars.append(_ALPHABET[number & 63])
        number >>= 6
    return "".join(reversed(chars))


def _text(value):
    if value is None:
        return "$"
    text = str(value)
    if any(ord(c) < 32 or ord(c) > 126 or c == "\\" for c in text):
        wide = any(ord(c) > 0xFFFF for c in text)
        encoding, marker = ("utf-32-be", "X4") if wide else ("utf-16-be", "X2")
        return "'\\" + marker + "\\" + text.encode(encoding).hex().upper() + "\\X0\\'"
    return "'" + text.replace("'", "''") + "'"


def _real(value, field="coordinate"):
    if isinstance(value, bool):
        raise CivilError("invalid_geometry", f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CivilError("invalid_geometry", f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise CivilError("invalid_geometry", f"{field} must be finite")
    text = format(number, ".17g").upper()
    if "." not in text:
        text = text.replace("E", ".E") if "E" in text else text + "."
    return text


def _tuple(values):
    return "(" + ",".join(values) + ")"


class _Spf:
    def __init__(self):
        self.lines = []
        self.counts = Counter()
        self.identities = set()

    def add(self, kind, *args):
        self.counts[kind] += 1
        index = len(self.lines) + 1
        self.lines.append(f"#{index}={kind.upper()}({','.join(args)});")
        return f"#{index}"

    def guid(self, identity):
        if identity in self.identities:
            raise CivilError("duplicate_id", "IFC root identity is not unique", id=identity)
        self.identities.add(identity)
        return _text(_guid(identity))

    def point(self, xyz):
        if len(xyz) not in (2, 3):
            raise CivilError("invalid_geometry", "Cartesian coordinates need two or three components")
        return self.add("IfcCartesianPoint", _tuple([_real(x) for x in xyz]))

    def direction(self, xyz):
        if math.sqrt(sum(float(v) ** 2 for v in xyz)) == 0:
            raise CivilError("invalid_geometry", "Direction vector must be nonzero")
        return self.add("IfcDirection", _tuple([_real(v) for v in xyz]))

    def axis(self, xyz, axis=None, ref=None):
        return self.add("IfcAxis2Placement3D", self.point(xyz),
                        "$" if axis is None else self.direction(axis),
                        "$" if ref is None else self.direction(ref))

    def relationship(self, kind, identity, *args):
        return self.add(kind, self.guid(identity), "$", "$", "$", *args)


def _segment(s):
    kind = s.get("type")
    start = s.get("start")
    if start is None or len(start) != 2:
        raise CivilError("invalid_alignment", "Horizontal segment requires 2D start")
    [_real(v) for v in start]
    if kind == "line":
        end = s.get("end")
        if end is None or len(end) != 2:
            raise CivilError("invalid_alignment", "Line requires 2D end")
        [_real(v) for v in end]
        length = math.dist(start, end)
        heading = math.atan2(end[1] - start[1], end[0] - start[0])
        radii, predefined = (0.0, 0.0), "LINE"
    elif kind == "arc":
        center, end = s.get("center"), s.get("end")
        radius = s.get("radius")
        _real(radius, "radius")
        if center is None or end is None or len(center) != 2 or len(end) != 2 or radius <= 0:
            raise CivilError("invalid_alignment", "Arc requires center/end and positive radius")
        [_real(v) for v in center]
        [_real(v) for v in end]
        if any(not math.isclose(math.dist(p, center), radius, rel_tol=1e-9, abs_tol=1e-6) for p in (start, end)):
            raise CivilError("invalid_alignment", "Arc endpoint disagrees with radius")
        if s.get("dir") not in ("cw", "ccw"):
            raise CivilError("invalid_alignment", "Arc dir must be cw or ccw")
        sign = 1 if s["dir"] == "ccw" else -1
        a = math.atan2(start[1] - center[1], start[0] - center[0])
        b = math.atan2(end[1] - center[1], end[0] - center[0])
        heading = a + sign * math.pi / 2
        length = radius * ((sign * (b - a)) % (2 * math.pi))
        radii, predefined = (sign * radius, sign * radius), "CIRCULARARC"
    elif kind == "spiral":
        heading, length = s.get("start_heading"), s.get("length")
        _real(heading, "start_heading")
        _real(length, "length")
        if s.get("dir") not in ("cw", "ccw"):
            raise CivilError("invalid_alignment", "Spiral dir must be cw or ccw")
        sign = 1 if s["dir"] == "ccw" else -1
        radii = []
        for name in ("radius_start", "radius_end"):
            r = s.get(name)
            if r is not None:
                _real(r, name)
                if r <= 0:
                    raise CivilError("invalid_alignment", "Spiral radii must be positive or None")
            radii.append(0.0 if r is None else sign * r)
        if radii[0] == radii[1]:
            raise CivilError("invalid_alignment", "Clothoid must change curvature")
        predefined = "CLOTHOID"
    else:
        raise CivilError("unsupported_alignment_segment", f"Unsupported segment: {kind}")
    if length <= 0:
        raise CivilError("invalid_alignment", "Horizontal segment length must be positive")
    return heading, length, radii, predefined


def _vertical_segments(a):
    pvis = a["vertical"].get("pvis", [])
    if len(pvis) < 2:
        raise CivilError("invalid_profile", "Vertical alignment requires at least two PVIs")
    for p in pvis:
        for field in ("station", "elevation"):
            _real(p.get(field), field)
        _real(p.get("curve_length", 0), "curve_length")
        if p.get("curve_length", 0) < 0:
            raise CivilError("invalid_profile", "Vertical curve length cannot be negative")
    if pvis[0].get("curve_length", 0) or pvis[-1].get("curve_length", 0):
        raise CivilError("invalid_profile", "End PVIs cannot carry symmetric vertical curves")
    grades = []
    for p, q in zip(pvis, pvis[1:]):
        delta = q["station"] - p["station"]
        if delta <= 0:
            raise CivilError("invalid_profile", "PVI stations must increase")
        if (p.get("curve_length", 0) + q.get("curve_length", 0)) / 2 > delta:
            raise CivilError("overlapping_vertical_curves", "Adjacent vertical curves overlap")
        grades.append((q["elevation"] - p["elevation"]) / delta)
    origin = a.get("start_station", 0.0)
    segments = []
    for i, (p, q) in enumerate(zip(pvis, pvis[1:])):
        start = p["station"] + p.get("curve_length", 0) / 2
        end = q["station"] - q.get("curve_length", 0) / 2
        g = grades[i]
        if end > start:
            segments.append((start - origin, end - start, p["elevation"] + g * (start - p["station"]), g, g, None, "CONSTANTGRADIENT"))
        length = q.get("curve_length", 0)
        if length:
            g2 = grades[i + 1]
            radius = None if g2 == g else length / (g2 - g)
            segments.append((end - origin, length, q["elevation"] - g * length / 2, g, g2, radius, "PARABOLICARC"))
    return segments


def _alignment(spf, a, placement):
    identity = "alignment/" + str(a["id"])
    root = spf.add("IfcAlignment", spf.guid(identity), "$", _text(a.get("name", a["id"])), "$", "$", placement, "$", ".NOTDEFINED.")
    horizontal = spf.add("IfcAlignmentHorizontal", spf.guid(identity + "/horizontal"), "$", _text("Horizontal"), "$", "$", placement, "$")
    children = [horizontal]
    segments = []
    if not a.get("horizontal"):
        raise CivilError("invalid_alignment", "Alignment requires horizontal segments")
    for i, s in enumerate(a["horizontal"]):
        heading, length, radii, kind = _segment(s)
        params = spf.add("IfcAlignmentHorizontalSegment", "$", "$", spf.point(s["start"]), _real(heading),
                         _real(radii[0]), _real(radii[1]), _real(length), "$", f".{kind}.")
        segments.append(spf.add("IfcAlignmentSegment", spf.guid(identity + f"/horizontal/{i}"), "$", _text(f"Horizontal {i + 1}"), "$", "$", placement, "$", params))
    spf.relationship("IfcRelNests", identity + "/nest_horizontal", horizontal, _tuple(segments))
    if a.get("vertical") is not None:
        vertical = spf.add("IfcAlignmentVertical", spf.guid(identity + "/vertical"), "$", _text("Vertical"), "$", "$", placement, "$")
        children.append(vertical)
        segments = []
        for i, (station, length, z, g1, g2, radius, kind) in enumerate(_vertical_segments(a)):
            params = spf.add("IfcAlignmentVerticalSegment", "$", "$", _real(station), _real(length), _real(z), _real(g1), _real(g2), "$" if radius is None else _real(radius), f".{kind}.")
            segments.append(spf.add("IfcAlignmentSegment", spf.guid(identity + f"/vertical/{i}"), "$", _text(f"Vertical {i + 1}"), "$", "$", placement, "$", params))
        spf.relationship("IfcRelNests", identity + "/nest_vertical", vertical, _tuple(segments))
    spf.relationship("IfcRelNests", identity + "/nest_components", root, _tuple(children))
    # Stationing is not the same thing as the vertical segment distance along.
    prop = spf.add("IfcPropertySingleValue", _text("StartStation"), "$", f"IFCLENGTHMEASURE({_real(a.get('start_station', 0))})", "$")
    pset = spf.add("IfcPropertySet", spf.guid(identity + "/stationing"), "$", _text("Civil_Stationing"), "$", _tuple([prop]))
    spf.relationship("IfcRelDefinesByProperties", identity + "/stationing_relation", _tuple([root]), pset)
    return root


def _solid(spf, diameter, start, end):
    _real(diameter, "diameter")
    if diameter <= 0:
        raise CivilError("invalid_geometry", "Circular pipe diameter must be positive")
    vector = [b - a for a, b in zip(start, end)]
    depth = math.sqrt(sum(x * x for x in vector))
    if depth == 0:
        raise CivilError("invalid_geometry", "Pipe reach must have nonzero physical length")
    axis = [v / depth for v in vector]
    norm = math.hypot(axis[0], axis[1])
    ref = [-axis[1] / norm, axis[0] / norm, 0] if norm else [1, 0, 0]
    profile = spf.add("IfcCircleProfileDef", ".AREA.", "$", "$", _real(diameter / 2))
    return spf.add("IfcExtrudedAreaSolid", profile, spf.axis(start, axis, ref), spf.direction([0, 0, 1]), _real(depth))


def _representation(spf, context, solids):
    shape = spf.add("IfcShapeRepresentation", context, _text("Body"), _text("SweptSolid"), _tuple(solids))
    return spf.add("IfcProductDefinitionShape", "$", "$", _tuple([shape]))


def _pipe_points(pipe, nodes, water):
    a, b = nodes[pipe["from"]], nodes[pipe["to"]]
    if water:
        z1, z2 = a.get("elevation"), b.get("elevation")
    else:
        z1, z2 = pipe.get("inlet_invert"), pipe.get("outlet_invert")
        if z1 is None:
            z1 = a.get("invert")
        if z2 is None:
            z2 = b.get("invert")
        if z1 is not None and z2 is not None:
            z1 += pipe["diameter"] / 2
            z2 += pipe["diameter"] / 2
    if z1 is None or z2 is None:
        return None
    _real(z1, "pipe_start_elevation")
    _real(z2, "pipe_end_elevation")
    xy = [[a["x"], a["y"]], *pipe.get("vertices", []), [b["x"], b["y"]]]
    for p in xy:
        if len(p) != 2:
            raise CivilError("invalid_geometry", "Pipe vertices must be two-dimensional")
        [_real(v) for v in p]
    lengths = [math.dist(p, q) for p, q in zip(xy, xy[1:])]
    distance = sum(lengths)
    if distance == 0 and len(xy) == 2 and z1 != z2:
        return [xy[0] + [z1], xy[1] + [z2]]
    if distance == 0 or any(length == 0 for length in lengths):
        raise CivilError("invalid_geometry", "Pipe plan path contains zero-length reaches")
    cumulative, points = 0.0, [xy[0] + [z1]]
    for p, length in zip(xy[1:], lengths):
        cumulative += length
        points.append(p + [z1 + (z2 - z1) * cumulative / distance])
    return points


def _network(spf, network, water, context, placement, site, skipped):
    collection = "water_networks" if water else "gravity_networks"
    identity = collection + "/" + str(network["id"])
    kind = "DOMESTICCOLDWATER" if water else {"sanitary": "SEWAGE", "storm": "STORMWATER", "combined": "SEWAGE"}.get(network.get("kind"))
    if kind is None:
        raise CivilError("unsupported_network", "Unknown distribution network kind")
    system = spf.add("IfcDistributionSystem", spf.guid(identity), "$", _text(network.get("name", network["id"])), "$", "$", "$", f".{kind}.")
    spf.relationship("IfcRelServicesBuildings", identity + "/services_site", system, _tuple([site]))
    node_list = [n for key in (("junctions", "reservoirs", "tanks") if water else ("nodes",)) for n in network.get(key) or []]
    nodes = {n["id"]: n for n in node_list}
    if len(nodes) != len(node_list):
        raise CivilError("duplicate_id", "Duplicate network node ids")
    products = []
    for pipe in network.get("pipes" if water else "conduits") or []:
        if pipe["from"] not in nodes or pipe["to"] not in nodes:
            raise CivilError("not_found", "IFC pipe endpoint does not exist", id=pipe["id"])
        if not water and pipe.get("shape") != "circular":
            skipped.append({"collection": collection, "network_id": network["id"], "id": pipe["id"], "reason": "unsupported_pipe_shape"})
            continue
        _real(pipe.get("diameter"), "diameter")
        points = _pipe_points(pipe, nodes, water)
        if points is None:
            skipped.append({"collection": collection, "network_id": network["id"], "id": pipe["id"], "reason": "missing_pipe_elevation"})
            continue
        solids = [_solid(spf, pipe["diameter"], p, q) for p, q in zip(points, points[1:])]
        representation = _representation(spf, context, solids)
        products.append(spf.add("IfcPipeSegment", spf.guid(identity + "/pipe/" + str(pipe["id"])), "$", _text(pipe["id"]), "$", "$", placement, representation, _text(pipe["id"]), ".RIGIDSEGMENT."))
    if not water:
        for node in node_list:
            if node.get("type") != "manhole":
                skipped.append({"collection": collection, "network_id": network["id"], "id": node["id"], "reason": "unsupported_chamber_type"})
                continue
            representation = "$"
            diameter, rim, invert = node.get("diameter"), node.get("rim"), node.get("invert")
            if all(v is not None for v in (diameter, rim, invert)):
                if rim <= invert:
                    raise CivilError("invalid_geometry", "Manhole rim must be above invert", id=node["id"])
                solid = _solid(spf, diameter, [node["x"], node["y"], invert], [node["x"], node["y"], rim])
                representation = _representation(spf, context, [solid])
            else:
                skipped.append({"collection": collection, "network_id": network["id"], "id": node["id"], "reason": "missing_chamber_dimensions"})
            products.append(spf.add("IfcDistributionChamberElement", spf.guid(identity + "/node/" + str(node["id"])), "$", _text(node["id"]), "$", "$", placement, representation, _text(node["id"]), ".MANHOLE."))
    for kind in ("pumps", "valves") if water else ():
        for item in network.get(kind) or []:
            skipped.append({"collection": collection, "network_id": network["id"], "id": item["id"], "reason": "unsupported_" + kind[:-1]})
    if products:
        spf.relationship("IfcRelAssignsToGroup", identity + "/group", _tuple(products), "$", system)
    return products


def _write_ifc(model, path) -> dict:
    """Write IFC4X3_ADD2 with semantic alignments, georeferencing and pipe solids.

    No terrain mesh or analysis results are exported. Pipe solids are circular
    coordination envelopes, not wall-thickness geometry. Gravity elevations are
    inverts plus radius; water-node elevations are treated as centreline levels.
    Unknown physical elevations (e.g. a reservoir head alone) are reported skipped.
    """
    spf, skipped = _Spf(), []
    metre = spf.add("IfcSIUnit", "*", ".LENGTHUNIT.", "$", ".METRE.")
    area = spf.add("IfcSIUnit", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    volume = spf.add("IfcSIUnit", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    radian = spf.add("IfcSIUnit", "*", ".PLANEANGLEUNIT.", "$", ".RADIAN.")
    units = spf.add("IfcUnitAssignment", _tuple([metre, area, volume, radian]))
    axis = spf.axis([0, 0, 0])
    context = spf.add("IfcGeometricRepresentationContext", "$", _text("Model"), "3", "1.E-9", axis, "$")
    placement = spf.add("IfcLocalPlacement", "$", axis)
    project = spf.add("IfcProject", spf.guid("project/" + str(model.get("id", model.get("name", "Civil")))), "$", _text(model.get("name", "Civil")), "$", "$", "$", "$", _tuple([context]), units)
    site = spf.add("IfcSite", spf.guid("site/" + str(model.get("id", model.get("name", "Civil")))), "$", _text(model.get("name", "Civil")), "$", "$", placement, "$", "$", ".ELEMENT.", "$", "$", "$", "$", "$")
    spf.relationship("IfcRelAggregates", "project/site", project, _tuple([site]))
    crs = model.get("crs")
    if crs and crs.get("horizontal"):
        target = spf.add("IfcProjectedCRS", _text(crs["horizontal"]), _text(crs.get("notes")), "$", _text(crs.get("vertical")), "$", "$", metre)
        local = crs.get("local") or {}
        angle, scale = local.get("rotation_rad", 0), local.get("scale_factor", 1)
        _real(angle, "rotation_rad")
        _real(scale, "scale_factor")
        if scale <= 0:
            raise CivilError("invalid_crs", "Local-grid scale factor must be positive")
        spf.add("IfcMapConversion", context, target, _real(local.get("origin_x", 0)), _real(local.get("origin_y", 0)), "0.", _real(math.cos(angle)), _real(math.sin(angle)), _real(scale))
    else:
        skipped.append({"collection": "crs", "reason": "crs_unknown"})
    products = [_alignment(spf, a, placement) for a in model.get("alignments", [])]
    for collection, water in (("gravity_networks", False), ("water_networks", True)):
        for network in model.get(collection, []):
            products.extend(_network(spf, network, water, context, placement, site, skipped))
    if products:
        spf.relationship("IfcRelContainedInSpatialStructure", "site/containment", _tuple(products), site)
    for collection in ("surfaces", "points", "catchments", "structures", "tie_ins"):
        for element in model.get(collection, []):
            skipped.append({"collection": collection, "id": element.get("id"), "reason": "unsupported_collection"})
    header = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('Civil infrastructure coordination'),'2;1');",
              "FILE_NAME('civil.ifc','',('Chudware'),('Chudware'),'aicad.civil/1','Chudware','');",
              "FILE_SCHEMA(('IFC4X3_ADD2'));", "ENDSEC;", "DATA;"]
    text = "\n".join([*header, *spf.lines, "ENDSEC;", "END-ISO-10303-21;", ""])
    try:
        Path(path).write_text(text, encoding="ascii")
    except (OSError, TypeError) as exc:
        raise CivilError("file_write_failed", str(exc)) from exc
    return {"ok": True, "path": str(path), "entity_counts": dict(spf.counts), "skipped": skipped,
            "warnings": [], "assumptions": ["Pipe/chamber solids are full circular coordination envelopes; wall thickness is not specified.",
                                           "Water-node elevations define pipe centreline levels; gravity pipe elevations use invert plus radius."],
            "method": {"name": "IFC 4.3 ADD2 SPF export", "reference": _REFERENCE}}


def write_ifc(model, path) -> dict:
    """Write IFC 4.3 ADD2 (ISO 16739-1:2024) semantic alignments and pipe solids.

    Coordination envelopes have no wall thickness. Missing physical elevations
    are reported in ``skipped``; reservoir hydraulic head is never used as z.
    """
    if not isinstance(model, dict):
        raise CivilError("invalid_model", "Civil model must be a dictionary")
    try:
        return _write_ifc(model, path)
    except CivilError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CivilError("invalid_model", f"Invalid civil field: {exc}") from exc

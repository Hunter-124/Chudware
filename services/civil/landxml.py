"""LandXML 1.2 civil exchange (http://www.landxml.org/schema/LandXML-1.2/).

Coordinates in LandXML text are NORTH EAST [ELEVATION], not model x/y/z.
The standard Feature/Property extension preserves civil attributes with no native
LandXML equivalent. Its fingerprint prevents stale metadata overriding edited XML.
Missing hydraulic inputs stay None: an imported drawing is not a hydraulic design.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .errors import CivilError


class _LazyEtree:
    """lxml.etree resolved on first use: the MCP server registers civil tools by importing
    this module, and tool registration must not require lxml."""

    def __getattr__(self, name):
        from lxml import etree as _etree
        return getattr(_etree, name)


etree = _LazyEtree()

NS = "http://www.landxml.org/schema/LandXML-1.2"
_PREFIX = "{" + NS + "}"
_COLLECTIONS = ("surfaces", "alignments", "gravity_networks", "water_networks", "points")
_LINEAR = {"meter": 1.0, "millimeter": .001, "centimeter": .01,
           "kilometer": 1000.0, "foot": .3048, "feet": .3048,
           "USSurveyFoot": 1200 / 3937, "inch": .0254,
           "mile": 1609.344, "miles": 1609.344}
_REFERENCE = "LandXML 1.2 schema: Units, Surface/Definition, CoordGeom, ProfAlign, PipeNetwork"


def _tag(node):
    return etree.QName(node).localname


def _children(node, path):
    return node.findall("/".join(_PREFIX + p for p in path.split("/")))


def _child(node, path):
    return node.find("/".join(_PREFIX + p for p in path.split("/")))


def _required(node, key):
    value = node.get(key)
    if value is None or value == "":
        raise CivilError("invalid_landxml", f"{_tag(node)} requires {key}")
    return value


def _number(value, field):
    if isinstance(value, bool):
        raise CivilError("invalid_landxml", f"{field} must be numeric, not boolean")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise CivilError("invalid_landxml", f"{field} must be numeric") from None
    if not math.isfinite(n):
        raise CivilError("invalid_landxml", f"{field} must be finite")
    return n


def _attr(node, key, factor=1.0):
    return None if node.get(key) is None else _number(node.get(key), key) * factor


def _factor(unit):
    if unit not in _LINEAR:
        raise CivilError("unsupported_units", f"Unsupported LandXML length unit: {unit}")
    return _LINEAR[unit]


def _units(root, assumptions):
    units = _child(root, "Units")
    if units is None or len(units) != 1 or _tag(units[0]) not in ("Metric", "Imperial"):
        raise CivilError("units_missing", "LandXML needs one Metric or Imperial unit declaration")
    spec = dict(units[0].attrib)
    linear = _factor(spec.get("linearUnit"))
    if "elevationUnit" not in spec:
        assumptions.append("elevationUnit omitted: elevation text interpreted in declared linearUnit, as civil coordinate triples.")
    if "diameterUnit" not in spec:
        assumptions.append("diameterUnit omitted: pipe/structure diameters interpreted in declared linearUnit.")
    return spec, linear, _factor(spec.get("elevationUnit", spec["linearUnit"])), _factor(spec.get("diameterUnit", spec["linearUnit"]))


def _coord(node, linear, elevation, cgpoints, dims=None):
    if node is None:
        raise CivilError("invalid_landxml", "Required coordinate element missing")
    if node.get("refPnt"):
        ref = node.get("refPnt")
        if ref not in cgpoints:
            raise CivilError("not_found", "Unknown CgPoint reference", id=ref)
        p = cgpoints[ref]
        values = [p["x"], p["y"], p["z"]]
        if dims == 3 and values[2] is None:
            raise CivilError("invalid_landxml", "Referenced CgPoint has no elevation", id=ref)
        return values[:dims] if dims else values
    words = (node.text or "").split()
    if len(words) not in (2, 3) or (dims == 3 and len(words) != 3):
        raise CivilError("invalid_landxml", "Coordinate needs north east [elevation]")
    values = [_number(words[1], "easting") * linear, _number(words[0], "northing") * linear]
    if len(words) == 3:
        values.append(_number(words[2], "elevation") * elevation)
    return values[:dims] if dims else values


def _fingerprint(node, units):
    def native(e):
        return [_tag(e), sorted(e.attrib.items()), (e.text or "").strip(),
                [native(c) for c in e if isinstance(c.tag, str) and _tag(c) != "Feature"]]
    raw = json.dumps([units, native(node)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _metadata(node, element, units):
    feature = etree.SubElement(node, _PREFIX + "Feature", code="aicad.civil/1")
    etree.SubElement(feature, _PREFIX + "Property", label="civil_element",
                     value=json.dumps({"native_sha256": _fingerprint(node, units), "element": element},
                                      allow_nan=False, separators=(",", ":")))


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _finite_metadata(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise CivilError("invalid_landxml_metadata", "Metadata numbers must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _finite_metadata(child)
    elif isinstance(value, list):
        for child in value:
            _finite_metadata(child)


def _restore(node, native, units, warnings):
    for feature in _children(node, "Feature"):
        if feature.get("code") != "aicad.civil/1":
            continue
        for prop in _children(feature, "Property"):
            if prop.get("label") != "civil_element":
                continue
            try:
                record = json.loads(prop.get("value"), parse_constant=_reject_constant)
            except (TypeError, ValueError):
                raise CivilError("invalid_landxml_metadata", "Malformed civil Feature JSON") from None
            if not isinstance(record, dict) or not isinstance(record.get("element"), dict):
                raise CivilError("invalid_landxml_metadata", "Feature needs an element object")
            element = record["element"]
            if not isinstance(element.get("id"), str) or not element["id"]:
                raise CivilError("invalid_landxml_metadata", "Metadata needs a nonempty string element id")
            _finite_metadata(element)
            if record.get("native_sha256") != _fingerprint(node, units):
                warnings.append(f"Ignored stale civil metadata for {native['id']}: native XML or units changed.")
                return native
            return record["element"]
    return native


def _direction(value, unit):
    angle = _number(value, "direction")
    if unit == "decimal degrees":
        angle = math.radians(angle)
    elif unit == "grads":
        angle *= math.pi / 200
    elif unit == "decimal dd.mm.ss":
        deg = math.floor(abs(angle))
        minutes = math.floor((abs(angle) - deg) * 100 + 1e-9)
        seconds = ((abs(angle) - deg) * 100 - minutes) * 100
        if minutes >= 60 or seconds >= 60 + 1e-6:
            raise CivilError("invalid_landxml", "Invalid dd.mm.ss direction")
        angle = math.copysign(math.radians(deg + minutes / 60 + seconds / 3600), angle)
    elif unit != "radians":
        raise CivilError("unsupported_units", f"Unsupported directionUnit: {unit}")
    return math.pi / 2 - angle


def _surface(node, linear, elevation, cgpoints, source, dropped):
    definition = _child(node, "Definition")
    if definition is None or definition.get("surfType") != "TIN":
        raise CivilError("unsupported_surface", "Only LandXML TIN definitions are supported")
    points, ids = [], {}
    for p in _children(definition, "Pnts/P"):
        pid = _required(p, "id")
        if pid in ids:
            raise CivilError("duplicate_id", "Repeated TIN point id", id=pid)
        ids[pid] = len(points)
        points.append(_coord(p, linear, elevation, cgpoints, 3))
    triangles = []
    for face in _children(definition, "Faces/F"):
        if face.get("i") == "1":  # LandXML's invisible/deleted face marker.
            dropped.append({"element": "F", "id": node.get("OID", node.get("name")), "reason": "invisible_face"})
            continue
        refs = (face.text or "").split()
        if len(refs) != 3 or any(ref not in ids for ref in refs):
            raise CivilError("invalid_landxml", "Face must reference three existing P ids")
        tri = [ids[r] for r in refs]
        a, b, c = [points[i] for i in tri]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if cross == 0:
            raise CivilError("degenerate_triangle", "TIN face has zero plan area")
        if cross < 0:
            tri[1], tri[2] = tri[2], tri[1]
        triangles.append(tri)
    if not points or not triangles:
        raise CivilError("invalid_landxml", "TIN requires points and visible triangular faces")
    return {"id": node.get("OID", _required(node, "name")), "name": _required(node, "name"),
            "kind": "tin", "points": points, "triangles": triangles, "breaklines": [],
            "boundary": None, "source": {"kind": "landxml", "ref": source}}


def _alignment(node, units, linear, elevation, cgpoints, dropped):
    from .alignment import point_at, validate
    horizontal = []
    geom = _child(node, "CoordGeom")
    if geom is None:
        raise CivilError("invalid_landxml", "Alignment requires CoordGeom")
    for e in geom:
        if not isinstance(e.tag, str) or _tag(e) == "Feature":
            continue
        kind = _tag(e)
        start = _coord(_child(e, "Start"), linear, elevation, cgpoints, 2)
        if kind == "Line":
            seg = {"type": "line", "start": start, "end": _coord(_child(e, "End"), linear, elevation, cgpoints, 2)}
        elif kind == "Curve":
            seg = {"type": "arc", "start": start, "end": _coord(_child(e, "End"), linear, elevation, cgpoints, 2),
                   "center": _coord(_child(e, "Center"), linear, elevation, cgpoints, 2),
                   "radius": _number(_required(e, "radius"), "radius") * linear, "dir": _required(e, "rot")}
        elif kind == "Spiral":
            if e.get("spiType") != "clothoid":
                raise CivilError("unsupported_spiral", "Only clothoid spirals can be imported")
            if e.get("dirStart") is not None:
                heading = _direction(e.get("dirStart"), units.get("directionUnit", "radians"))
            else:
                pi = _coord(_child(e, "PI"), linear, elevation, cgpoints, 2)
                if pi == start:
                    raise CivilError("invalid_landxml", "Spiral needs dirStart or nonzero Start-PI tangent")
                heading = math.atan2(pi[1] - start[1], pi[0] - start[0])
            def radius(key):
                r = _required(e, key)
                return None if r.upper() == "INF" else _number(r, key) * linear
            seg = {"type": "spiral", "start": start, "start_heading": heading,
                   "length": _number(_required(e, "length"), "length") * linear,
                   "radius_start": radius("radiusStart"), "radius_end": radius("radiusEnd"), "dir": _required(e, "rot")}
            declared_end = _coord(_child(e, "End"), linear, elevation, cgpoints, 2)
            single = {"id": "spiral", "name": "Spiral", "start_station": 0.0,
                      "horizontal": [seg], "vertical": None}
            errors = validate(single)
            if errors:
                raise CivilError("invalid_alignment", "; ".join(errors))
            computed_end = point_at(single, seg["length"])[:2]
            if math.dist(declared_end, computed_end) > 1e-6:
                raise CivilError("inconsistent_spiral_endpoint", "Declared End disagrees with clothoid integration")
        else:
            raise CivilError("unsupported_alignment_segment", f"Unsupported CoordGeom element: {kind}")
        if seg.get("dir", "ccw") not in ("cw", "ccw"):
            raise CivilError("invalid_landxml", "rot must be cw or ccw")
        horizontal.append(seg)
    profiles = _children(node, "Profile/ProfAlign")
    vertical = None
    if profiles:
        pvis = []
        for p in profiles[0]:
            kind = _tag(p)
            if kind == "Feature":
                continue
            if kind not in ("PVI", "ParaCurve"):
                raise CivilError("unsupported_vertical_curve", f"Unsupported profile element: {kind}")
            values = (p.text or "").split()
            if len(values) != 2:
                raise CivilError("invalid_landxml", "Profile PVI text must be station elevation")
            pvis.append({"station": _number(values[0], "station") * linear,
                         "elevation": _number(values[1], "elevation") * elevation,
                         "curve_length": _number(_required(p, "length"), "length") * linear if kind == "ParaCurve" else 0.0})
        vertical = {"pvis": pvis}
        for extra in profiles[1:]:
            dropped.append({"element": "ProfAlign", "id": extra.get("name"), "reason": "multiple_profiles"})
    result = {"id": node.get("oID", _required(node, "name")), "name": _required(node, "name"),
              "start_station": (_attr(node, "staStart", linear) or 0.0), "horizontal": horizontal, "vertical": vertical}
    errors = validate(result)
    if errors:
        raise CivilError("invalid_alignment", "; ".join(errors))
    return result


def _network(node, linear, elevation, diameter, cgpoints, assumptions):
    kind = _required(node, "pipeNetType")
    if kind not in ("sanitary", "storm", "water"):
        raise CivilError("unsupported_network", f"Unsupported pipeNetType: {kind}")
    nodes, by_name, inverts = [], {}, {}
    for s in _children(node, "Structs/Struct"):
        name = _required(s, "name")
        if name in by_name:
            raise CivilError("duplicate_id", "Duplicate Struct name", id=name)
        xy = _coord(_child(s, "Center"), linear, elevation, cgpoints, 2)
        circ = _child(s, "CircStruct")
        inv = {}
        for i in _children(s, "Invert"):
            ref = _required(i, "refPipe")
            if ref in inv:
                raise CivilError("duplicate_id", "Repeated Invert refPipe at one Struct", id=ref)
            inv[ref] = _number(_required(i, "elev"), "elev") * elevation
        inverts[name] = inv
        sump = _attr(s, "elevSump", elevation)
        # The lowest connection invert is a documented geometric derivation, not a guessed sump.
        if sump is None and inv:
            sump = min(inv.values())
            assumptions.append(f"{name}: node invert derived from lowest connected Invert; elevSump absent.")
        n = {"id": s.get("oID", name), "x": xy[0], "y": xy[1]}
        if kind == "water":
            n.update(elevation=sump, demand_m3s=None, pattern=None)
        else:
            n.update(type="inlet" if _child(s, "InletStruct") is not None else "outfall" if _child(s, "OutletStruct") is not None else "manhole" if circ is not None else "junction",
                     rim=_attr(s, "elevRim", elevation), invert=sump,
                     diameter=_attr(circ, "diameter", diameter) if circ is not None else None,
                     inflow_m3s=None, catchment=None, loss_k=None, outfall_tailwater=None)
        nodes.append(n)
        by_name[name] = n
    pipes = []
    seen = set()
    for p in _children(node, "Pipes/Pipe"):
        name, a, b = _required(p, "name"), _required(p, "refStart"), _required(p, "refEnd")
        if name in seen:
            raise CivilError("duplicate_id", "Duplicate Pipe name", id=name)
        seen.add(name)
        if a not in by_name or b not in by_name:
            raise CivilError("not_found", "Pipe endpoints must reference existing Structs", id=name)
        circ = _child(p, "CircPipe")
        if circ is None:
            raise CivilError("unsupported_pipe_shape", "Only CircPipe can be imported", id=name)
        if _child(p, "Center") is not None:
            raise CivilError("unsupported_curved_pipe", "Curved pipe Center cannot be represented as a straight conduit", id=name)
        link = {"id": p.get("oID", name), "from": by_name[a]["id"], "to": by_name[b]["id"],
                "length": _attr(p, "length", linear), "diameter": _number(_required(circ, "diameter"), "diameter") * diameter,
                "material": circ.get("material"), "vertices": []}
        if link["diameter"] <= 0 or (link["length"] is not None and link["length"] <= 0):
            raise CivilError("invalid_geometry", "Pipe diameter and specified length must be positive", id=name)
        if kind == "water":
            link.update(roughness=None, minor_loss=None, status=None)
        else:
            inlet = inverts[a].get(name, by_name[a]["invert"])
            outlet = inverts[b].get(name, by_name[b]["invert"])
            slope = _attr(p, "slope")
            # Preserve a specified slope even when only one invert was supplied.
            length = link["length"]
            if length is None:
                length = math.hypot(by_name[b]["x"] - by_name[a]["x"], by_name[b]["y"] - by_name[a]["y"])
            if slope is not None and inlet is not None and outlet is None:
                outlet = inlet - slope * length
                assumptions.append(f"{name}: outlet invert derived from slope and length.")
            elif slope is not None and inlet is None and outlet is not None:
                inlet = outlet + slope * length
                assumptions.append(f"{name}: inlet invert derived from slope and length.")
            if slope is not None and inlet is not None and outlet is not None and length > 0 and not math.isclose((inlet - outlet) / length, slope, rel_tol=1e-7, abs_tol=1e-9):
                raise CivilError("inconsistent_pipe_slope", "Slope disagrees with connection inverts and length", id=name)
            link.update(shape="circular", width=None, height=None, side_slope=None,
                        n=None, inlet_invert=inlet, outlet_invert=outlet)
        pipes.append(link)
    for inv in inverts.values():
        for ref in inv:
            if ref not in seen:
                raise CivilError("not_found", "Invert refPipe does not reference an existing pipe", id=ref)
    assumptions.append(f"{node.get('name')}: LandXML omits hydraulic coefficients and loads; unspecified values remain None.")
    base = {"id": node.get("oID", _required(node, "name")), "name": _required(node, "name")}
    if kind == "water":
        base.update(headloss=None, junctions=nodes, reservoirs=[], tanks=[], pipes=pipes,
                    pumps=[], valves=[], patterns={}, options={})
    else:
        base.update(kind=kind, nodes=nodes, conduits=pipes)
    return kind, base


def read(path_or_bytes) -> dict:
    """Import the supported LandXML 1.2 entities; unit conversions are recorded.

    Reference: LandXML 1.2 schema documentation, north/east coordinate text and
    north-clockwise direction convention. External entities/DTDs are refused.
    """
    source = "bytes" if isinstance(path_or_bytes, bytes) else str(path_or_bytes)
    try:
        raw = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    except (OSError, TypeError) as exc:
        raise CivilError("file_read_failed", str(exc)) from exc
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             remove_blank_text=True, remove_comments=True)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise CivilError("invalid_xml", str(exc)) from exc
    if root.getroottree().docinfo.doctype:
        raise CivilError("unsupported_xml_dtd", "LandXML DTD/entity declarations are not accepted")
    if root.tag != _PREFIX + "LandXML" or root.get("version") != "1.2":
        raise CivilError("unsupported_landxml_version", "Expected LandXML 1.2 namespace and version")
    warnings, assumptions, dropped = [], [], []
    units, linear, elevation, diameter = _units(root, assumptions)
    fragment = {k: [] for k in _COLLECTIONS}
    cgpoints = {}
    groups = [g for top in _children(root, "CgPoints") for g in top.iter(_PREFIX + "CgPoints")]
    for group in groups:
        native_points = []
        for p in _children(group, "CgPoint"):
            name = _required(p, "name")
            if name in cgpoints:
                raise CivilError("duplicate_id", "Duplicate CgPoint name", id=name)
            xyz = _coord(p, linear, elevation, {}, None)
            point = {"id": p.get("oID", name), "name": name, "x": xyz[0], "y": xyz[1],
                     "z": xyz[2] if len(xyz) == 3 else None, "description": p.get("desc")}
            cgpoints[name] = point
            native_points.append(point)
        native_group = {"id": group.get("name", "civil_points"), "points": native_points}
        restored = _restore(group, native_group, units, warnings)
        if not isinstance(restored.get("points"), list):
            raise CivilError("invalid_landxml_metadata", "CgPoints metadata needs a points list")
        fragment["points"].extend(restored["points"])
    for s in _children(root, "Surfaces/Surface"):
        native = _surface(s, linear, elevation, cgpoints, source, dropped)
        fragment["surfaces"].append(_restore(s, native, units, warnings))
        for extra in s:
            if _tag(extra) not in ("Definition", "Feature"):
                dropped.append({"element": _tag(extra), "id": native["id"], "reason": "unsupported_surface_source_data"})
    for a in _children(root, "Alignments/Alignment"):
        native = _alignment(a, units, linear, elevation, cgpoints, dropped)
        fragment["alignments"].append(_restore(a, native, units, warnings))
    for n in _children(root, "PipeNetworks/PipeNetwork"):
        kind, native = _network(n, linear, elevation, diameter, cgpoints, assumptions)
        fragment["water_networks" if kind == "water" else "gravity_networks"].append(_restore(n, native, units, warnings))
    supported = {"Units", "CgPoints", "Surfaces", "Alignments", "PipeNetworks", "Project", "Application", "CoordinateSystem"}
    for node in root:
        if _tag(node) not in supported:
            dropped.append({"element": _tag(node), "reason": "unsupported_element"})
    for collection, elements in fragment.items():
        ids = [e.get("id") for e in elements]
        if len(ids) != len(set(ids)):
            raise CivilError("duplicate_id", "Duplicate imported element ids", collection=collection)
    provenance = {"source": source, "kind": "landxml", "imported_at": datetime.now(timezone.utc).isoformat(),
                  "crs": None, "units_in": units, "linear_to_m": linear, "elevation_to_m": elevation,
                  "diameter_to_m": diameter, "notes": "LandXML north/east reordered to model x=east, y=north; SI metres."}
    return {"ok": True, "model_fragment": fragment, "provenance": provenance, "dropped": dropped,
            "warnings": warnings, "assumptions": assumptions, "method": {"name": "LandXML 1.2 import", "reference": _REFERENCE}}


def _fmt(number):
    n = _number(number, "export number")
    return format(n, ".17g")


def _sub(parent, tag_name, **attrs):
    return etree.SubElement(parent, _PREFIX + tag_name, **{k: str(v) for k, v in attrs.items() if v is not None})


def _put_coord(parent, name, point):
    if len(point) not in (2, 3):
        raise CivilError("invalid_geometry", "Coordinate must have two or three components")
    e = _sub(parent, name)
    e.text = " ".join(_fmt(v) for v in [point[1], point[0], *point[2:]])
    return e


def _segment_length(s):
    if s["type"] == "line":
        return math.dist(s["start"], s["end"])
    if s["type"] == "spiral":
        return s["length"]
    a = math.atan2(s["start"][1] - s["center"][1], s["start"][0] - s["center"][0])
    b = math.atan2(s["end"][1] - s["center"][1], s["end"][0] - s["center"][0])
    return s["radius"] * ((b - a) % (2 * math.pi) if s["dir"] == "ccw" else (a - b) % (2 * math.pi))


def _export_alignment(parent, a, units):
    from .alignment import point_at, validate
    errors = validate(a)
    if errors:
        raise CivilError("invalid_alignment", "; ".join(errors))
    node = _sub(parent, "Alignment", name=a.get("name", a["id"]), oID=a["id"],
                staStart=_fmt(a.get("start_station", 0)), length=_fmt(sum(_segment_length(s) for s in a["horizontal"])))
    geom = _sub(node, "CoordGeom")
    station = a.get("start_station", 0)
    for s in a["horizontal"]:
        kind = s["type"]
        length = _segment_length(s)
        if kind == "line":
            e = _sub(geom, "Line", length=_fmt(length))
            _put_coord(e, "Start", s["start"])
            _put_coord(e, "End", s["end"])
        elif kind == "arc":
            e = _sub(geom, "Curve", rot=s["dir"], radius=_fmt(s["radius"]), length=_fmt(length))
            _put_coord(e, "Start", s["start"])
            _put_coord(e, "Center", s["center"])
            _put_coord(e, "End", s["end"])
        elif kind == "spiral":
            end_x, end_y, end_h = point_at(a, station + length)
            h = s["start_heading"]
            e = _sub(geom, "Spiral", rot=s["dir"], spiType="clothoid", length=_fmt(length),
                     radiusStart="INF" if s["radius_start"] is None else _fmt(s["radius_start"]),
                     radiusEnd="INF" if s["radius_end"] is None else _fmt(s["radius_end"]),
                     dirStart=_fmt((math.pi / 2 - h) % (2 * math.pi)))
            _put_coord(e, "Start", s["start"])
            # Intersection of the endpoint tangents defines the standard PI.
            dx, dy = end_x - s["start"][0], end_y - s["start"][1]
            den = math.sin(end_h - h)
            if abs(den) < 1e-14:
                raise CivilError("unsupported_spiral_pi", "Parallel endpoint tangents cannot define a finite LandXML spiral PI")
            tangent = (dx * math.sin(end_h) - dy * math.cos(end_h)) / den
            _put_coord(e, "PI", [s["start"][0] + tangent * math.cos(h), s["start"][1] + tangent * math.sin(h)])
            _put_coord(e, "End", [end_x, end_y])
        else:
            raise CivilError("unsupported_alignment_segment", f"Unsupported type: {kind}")
        station += length
    if a.get("vertical") is not None:
        profile = _sub(_sub(node, "Profile", name=a.get("name", a["id"])), "ProfAlign", name=a.get("name", a["id"]))
        for p in a["vertical"]["pvis"]:
            e = _sub(profile, "ParaCurve", length=_fmt(p["curve_length"])) if p.get("curve_length", 0) else _sub(profile, "PVI")
            e.text = _fmt(p["station"]) + " " + _fmt(p["elevation"])
    _metadata(node, a, units)


def _export_network(parent, network, water, units):
    kind = "water" if water else network["kind"]
    if kind not in ("water", "sanitary", "storm"):
        raise CivilError("unsupported_network", "LandXML has no combined pipeNetType")
    e = _sub(parent, "PipeNetwork", name=network.get("name", network["id"]), oID=network["id"], pipeNetType=kind)
    structs, pipes_parent = _sub(e, "Structs"), _sub(e, "Pipes")
    nodes = [n for key in (("junctions", "reservoirs", "tanks") if water else ("nodes",)) for n in network.get(key) or []]
    links = network.get("pipes" if water else "conduits") or []
    by_id = {n["id"]: n for n in nodes}
    if len(by_id) != len(nodes):
        raise CivilError("duplicate_id", "Duplicate network node id")
    scope = kind + "/" + quote(str(network["id"]), safe="") + "/"
    node_names = {n["id"]: scope + "node/" + quote(str(n["id"]), safe="") for n in nodes}
    pipe_names = {p["id"]: scope + "pipe/" + quote(str(p["id"]), safe="") for p in links}
    for n in nodes:
        rim, invert = (None, n.get("elevation")) if water else (n.get("rim"), n.get("invert"))
        s = _sub(structs, "Struct", name=node_names[n["id"]], oID=n["id"], elevRim=None if rim is None else _fmt(rim), elevSump=None if invert is None else _fmt(invert))
        _put_coord(s, "Center", [n["x"], n["y"]])
        if not water and n.get("diameter") is not None:
            _sub(s, "CircStruct", diameter=_fmt(n["diameter"]))
        else:
            _sub(s, "Connection")
        for link in links:
            if n["id"] in (link["from"], link["to"]):
                start = n["id"] == link["from"]
                z = invert if water else link.get("inlet_invert" if start else "outlet_invert")
                if z is None:
                    z = invert
                if z is not None:
                    _sub(s, "Invert", refPipe=pipe_names[link["id"]], elev=_fmt(z), flowDir="out" if start else "in")
    seen = set()
    for link in links:
        if link["id"] in seen:
            raise CivilError("duplicate_id", "Duplicate pipe id")
        seen.add(link["id"])
        if link["from"] not in by_id or link["to"] not in by_id:
            raise CivilError("not_found", "Pipe endpoint does not exist", id=link["id"])
        if not water and link["shape"] != "circular":
            raise CivilError("unsupported_pipe_shape", "LandXML export supports circular conduits")
        length = link.get("length")
        if link["diameter"] <= 0 or (length is not None and length <= 0):
            raise CivilError("invalid_geometry", "Pipe diameter and specified length must be positive", id=link["id"])
        slope = None
        if not water:
            a, b = by_id[link["from"]], by_id[link["to"]]
            inlet, outlet = link.get("inlet_invert"), link.get("outlet_invert")
            inlet = a.get("invert") if inlet is None else inlet
            outlet = b.get("invert") if outlet is None else outlet
            native_length = length if length is not None else math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            if inlet is not None and outlet is not None and native_length > 0:
                slope = (inlet - outlet) / native_length
        p = _sub(pipes_parent, "Pipe", name=pipe_names[link["id"]], oID=link["id"], refStart=node_names[link["from"]], refEnd=node_names[link["to"]], length=None if length is None else _fmt(length))
        if slope is not None:
            p.set("slope", _fmt(slope))
        _sub(p, "CircPipe", diameter=_fmt(link["diameter"]), material=link.get("material"))
    _metadata(e, network, units)


def write(model, path, *, include=("surfaces", "alignments", "gravity_networks", "water_networks")) -> dict:
    """Export SI LandXML 1.2; preserve all selected civil fields in standard Features.

    Reference: LandXML 1.2 schema. CgPoints are always included. Geometry remains
    native LandXML; hydraulic-only fields/breakline constraints use civil Features.
    """
    if not isinstance(model, dict):
        raise CivilError("invalid_model", "Civil model must be a dictionary")
    unknown = set(include) - set(_COLLECTIONS)
    if unknown:
        raise CivilError("unsupported_collection", ", ".join(sorted(unknown)))
    now = datetime.now(timezone.utc)
    root = etree.Element(_PREFIX + "LandXML", nsmap={None: NS}, version="1.2", date=now.date().isoformat(), time=now.time().replace(tzinfo=None).isoformat())
    unit_spec = {"linearUnit": "meter", "areaUnit": "squareMeter", "volumeUnit": "cubicMeter",
                 "temperatureUnit": "celsius", "pressureUnit": "HPA", "diameterUnit": "meter",
                 "elevationUnit": "meter", "angularUnit": "radians", "directionUnit": "radians"}
    _sub(_sub(root, "Units"), "Metric", **unit_spec)
    counts = {k: 0 for k in _COLLECTIONS}
    cg = _sub(root, "CgPoints") if model.get("points") else None
    for p in model.get("points", []):
        e = _put_coord(cg, "CgPoint", [p["x"], p["y"]] + ([] if p.get("z") is None else [p["z"]]))
        e.set("name", str(p["id"]))
        e.set("oID", str(p["id"]))
        if p.get("description") is not None:
            e.set("desc", str(p["description"]))
        counts["points"] += 1
    if cg is not None:
        _metadata(cg, {"id": "civil_points", "points": model["points"]}, unit_spec)
    for collection in include:
        if collection == "points" or not model.get(collection):
            continue
        if collection == "surfaces":
            parent = _sub(root, "Surfaces")
            for s in model[collection]:
                if s.get("kind") != "tin":
                    raise CivilError("unsupported_surface", "Only TIN surfaces can be exported")
                e = _sub(parent, "Surface", name=s.get("name", s["id"]), OID=s["id"])
                definition = _sub(e, "Definition", surfType="TIN")
                pts, faces = _sub(definition, "Pnts"), _sub(definition, "Faces")
                for i, xyz in enumerate(s["points"]):
                    if len(xyz) != 3:
                        raise CivilError("invalid_geometry", "TIN points must have x/y/z")
                    _put_coord(pts, "P", xyz).set("id", str(i + 1))
                for triangle in s["triangles"]:
                    if len(triangle) != 3 or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(s["points"]) for i in triangle):
                        raise CivilError("invalid_geometry", "Triangle must index three surface points")
                    a, b, c = [s["points"][i] for i in triangle]
                    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
                    if cross <= 0:
                        raise CivilError("invalid_geometry", "Model TIN triangles must have positive CCW plan area")
                    _sub(faces, "F").text = " ".join(str(i + 1) for i in triangle)
                _metadata(e, s, unit_spec)
        elif collection == "alignments":
            parent = _sub(root, "Alignments")
            for a in model[collection]:
                _export_alignment(parent, a, unit_spec)
        else:
            parent = _sub(root, "PipeNetworks")
            for n in model[collection]:
                _export_network(parent, n, collection == "water_networks", unit_spec)
        counts[collection] = len(model[collection])
    dropped = [{"collection": k, "reason": "not_supported_in_landxml"} for k in ("catchments", "structures", "tie_ins") if model.get(k)]
    dropped.extend({"collection": k, "reason": "excluded_by_include"} for k in _COLLECTIONS[:-1] if k not in include and model.get(k))
    try:
        destination = Path(path)
    except TypeError as exc:
        raise CivilError("invalid_path", "LandXML destination must be a filesystem path") from exc
    payload = etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)
    try:
        destination.write_bytes(payload)
    except OSError as exc:
        raise CivilError("file_write_failed", str(exc)) from exc
    return {"ok": True, "path": str(path), "counts": counts, "dropped": dropped, "warnings": [],
            "assumptions": ["Non-native civil attributes use LandXML Feature/Property metadata; third-party readers may ignore those attributes."],
            "method": {"name": "LandXML 1.2 export", "reference": _REFERENCE}}



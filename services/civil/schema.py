"""Structural validation and document factories for contracts/civil_schema.md §3.

Errors use JSON-path locators, following services.piping.schema. Geometry and
hydraulic feasibility remain the responsibility of the respective analysis modules.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .errors import CivilError

SCHEMA = "aicad.civil/1"
COLLECTIONS = ("surfaces", "alignments", "water_networks", "gravity_networks",
               "catchments", "structures", "tie_ins")


def new_model(name: str, crs: dict | None = None) -> dict:
    from .crs import normalize
    if not isinstance(name, str) or not name.strip():
        raise CivilError("invalid_name")
    return {"schema": SCHEMA, "name": name, "crs": normalize(crs),
            **{key: [] for key in COLLECTIONS},
            "criteria": {"profile": "ten_states_2014", "overrides": {}}, "provenance": []}


def water_network(id: str, name: str = "Water", *, headloss: str = "H-W", **fields) -> dict:
    """Create an empty network; defaults are explicit EPANET-style model options."""
    return {"id": id, "name": name, "headloss": headloss, "junctions": [],
            "reservoirs": [], "tanks": [], "pipes": [], "pumps": [], "valves": [],
            "patterns": {}, "options": {"duration_s": 0, "hydraulic_step_s": 3600,
            "pattern_step_s": 3600, "demand_model": "DDA", "p_min_m": 0,
            "p_req_m": 20, "pda_exponent": 0.5, "viscosity_m2s": 1.004e-6,
            "accuracy": 1e-6, "max_iter": 200}, **deepcopy(fields)}


def gravity_network(id: str, name: str = "Gravity", *, kind: str = "sanitary", **fields) -> dict:
    return {"id": id, "name": name, "kind": kind, "nodes": [], "conduits": [], **deepcopy(fields)}


def _collection(model, collection):
    if collection not in COLLECTIONS:
        raise CivilError("unknown_collection", collection=collection)
    items = model.get(collection)
    if not isinstance(items, list):
        raise CivilError("invalid_model", collection=collection)
    return items


def find(model: dict, collection: str, id: str) -> dict:
    for item in _collection(model, collection):
        if item.get("id") == id:
            return item
    raise CivilError("not_found", collection=collection, id=id)


def upsert(model: dict, collection: str, element: dict) -> dict:
    if not isinstance(element, dict) or not isinstance(element.get("id"), str) or not element["id"]:
        raise CivilError("invalid_id")
    items = _collection(model, collection)
    for i, item in enumerate(items):
        if item.get("id") == element["id"]:
            items[i] = deepcopy(element)
            return items[i]
    items.append(deepcopy(element))
    return items[-1]


def remove(model: dict, collection: str, id: str):
    items = _collection(model, collection)
    for i, item in enumerate(items):
        if item.get("id") == id:
            del items[i]
            return
    raise CivilError("not_found", collection=collection, id=id)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(model: dict) -> list[str]:
    """Check all §3 collections, references, dimensions, TIN indices and continuity.

    Optional null attributes are equivalent to absent attributes. Nullable exchange
    inputs may remain unknown; required physical dimensions may not be unknown.
    """
    errors = []
    def err(path, message):
        errors.append(f"{path}: {message}")
    def required(obj, fields, path):
        for field in fields:
            if field not in obj:
                err(f"{path}.{field}", "required")
    def numeric(obj, fields, path, positive=False, nullable=False):
        for field in fields:
            value = obj.get(field)
            if nullable and value is None:
                continue
            if not _number(value) or (positive and value <= 0):
                err(f"{path}.{field}", "must be a finite positive number" if positive else "must be finite number")
    def coords(value, dim, path, minimum=0):
        if not isinstance(value, (list, tuple)) or len(value) < minimum:
            err(path, "invalid coordinate array")
            return False
        for i, point in enumerate(value):
            if not isinstance(point, (list, tuple)) or len(point) != dim or not all(_number(v) for v in point):
                err(f"{path}[{i}]", f"must be a finite {dim}D coordinate")
                return False
        return True
    def entries(obj, key, path, optional=False):
        value = obj.get(key)
        if optional and value is None:
            return []
        if not isinstance(value, list):
            err(f"{path}.{key}" if path else key, "required array")
            return []
        seen = set()
        out = []
        for i, item in enumerate(value):
            loc = f"{path + '.' if path else ''}{key}[{i}]"
            if not isinstance(item, dict):
                err(loc, "must be an object")
                continue
            if item.get("name") is not None and not isinstance(item["name"], str):
                err(loc + ".name", "must be a string")
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                err(loc + ".id", "required nonempty string")
            elif identifier in seen:
                err(loc + ".id", f"duplicate id {identifier!r}")
            else:
                seen.add(identifier)
            out.append((item, loc))
        return out
    if not isinstance(model, dict):
        return ["model: must be an object"]
    required(model, ("schema", "name", "crs", "criteria", "provenance", *COLLECTIONS), "model")
    if model.get("schema") != SCHEMA:
        err("schema", f"must be {SCHEMA}")
    if not isinstance(model.get("name"), str) or not model.get("name"):
        err("name", "required nonempty string")
    from .crs import validate as validate_crs
    errors.extend(validate_crs(model.get("crs")))
    from .criteria import resolve
    try:
        resolve(model.get("criteria"))
    except CivilError as exc:
        err("criteria", exc.reason)
    if not isinstance(model.get("provenance"), list):
        err("provenance", "must be an array")
    collections = {key: entries(model, key, "") for key in COLLECTIONS}
    gravity_nodes = set()
    network_nodes = {}
    for key in ("water_networks", "gravity_networks"):
        for net, path in collections[key]:
            if key == "gravity_networks":
                required(net, ("kind",), path)
            else:
                for optional in ("patterns", "options", "curves"):
                    if net.get(optional) is not None and not isinstance(net[optional], dict):
                        err(path + "." + optional, "must be an object")
            water = key == "water_networks"
            if water and net.get("headloss") is not None and net["headloss"] not in ("H-W", "D-W"):
                err(path + ".headloss", "must be H-W or D-W")
            if not water and net.get("kind") not in ("sanitary", "storm", "combined"):
                err(path + ".kind", "invalid gravity network kind")
            node_keys = ("junctions", "reservoirs", "tanks") if water else ("nodes",)
            nodes = {}
            for node_key in node_keys:
                for node, loc in entries(net, node_key, path, optional=True):
                    identifier = node.get("id")
                    if isinstance(identifier, str):
                        if identifier in nodes:
                            err(loc + ".id", "duplicate node id across network node collections")
                        nodes[identifier] = node
                    required(node, ("x", "y"), loc)
                    numeric(node, ("x", "y"), loc)
                    fields = {"junctions": ("elevation",), "reservoirs": ("head",),
                              "tanks": ("elevation", "init_level", "min_level", "max_level", "diameter"),
                              "nodes": ("type", "rim", "invert")}[node_key]
                    required(node, fields, loc)
                    numeric(node, tuple(f for f in fields if f != "type"), loc, nullable=True)
                    if node_key == "junctions":
                        numeric(node, ("demand_m3s",), loc, nullable=True)
                    if "diameter" in node and node["diameter"] is not None:
                        numeric(node, ("diameter",), loc, positive=True)
                    if node_key == "tanks":
                        numeric(node, ("diameter",), loc, positive=True)
                        levels = [node.get(f) for f in ("min_level", "init_level", "max_level")]
                        if all(_number(v) for v in levels) and not levels[0] <= levels[1] <= levels[2]:
                            err(loc, "tank levels must satisfy min <= initial <= max")
                    if node_key == "nodes" and node.get("type") not in ("manhole", "inlet", "junction", "outfall"):
                        err(loc + ".type", "invalid gravity node type")
            network_nodes.setdefault(net.get("id") if isinstance(net.get("id"), str) else None, []).append(set(nodes))
            if not water:
                gravity_nodes.update(nodes)
            link_keys = ("pipes", "pumps", "valves") if water else ("conduits",)
            for link_key in link_keys:
                for link, loc in entries(net, link_key, path, optional=True):
                    required(link, ("from", "to"), loc)
                    for endpoint in ("from", "to"):
                        ref = link.get(endpoint)
                        if not isinstance(ref, str) or ref not in nodes:
                            err(loc + "." + endpoint, "dangling node reference")
                    if link_key == "pipes":
                        required(link, ("length", "diameter"), loc)
                        numeric(link, ("length", "diameter"), loc, positive=True)
                        numeric(link, ("roughness",), loc, nullable=True)
                    elif link_key == "pumps":
                        required(link, ("curve",), loc)
                        coords(link.get("curve"), 2, loc + ".curve", 1)
                    elif link_key == "valves":
                        required(link, ("type", "diameter", "setting"), loc)
                        numeric(link, ("diameter",), loc, positive=True)
                        numeric(link, ("setting",), loc, nullable=True)
                        if link.get("type") not in ("PRV", "PSV", "FCV", "TCV"):
                            err(loc + ".type", "invalid valve type")
                    else:
                        required(link, ("shape",), loc)
                        shape = link.get("shape")
                        dimensions = ({"circular": ("diameter",), "rectangular": ("width", "height"),
                                       "trapezoidal": ("width", "height")}.get(shape)
                                      if isinstance(shape, str) else None)
                        if dimensions is None:
                            err(loc + ".shape", "invalid conduit shape")
                        else:
                            numeric(link, dimensions, loc, positive=True)
                        numeric(link, ("n",), loc, positive=True, nullable=True)
                        numeric(link, ("length",), loc, positive=True, nullable=True)
                        if shape == "trapezoidal" and (not _number(link.get("side_slope")) or link["side_slope"] < 0):
                            err(loc + ".side_slope", "must be finite and nonnegative")
                    if link.get("vertices") is not None:
                        coords(link["vertices"], 2, loc + ".vertices")
    for surface, path in collections["surfaces"]:
        required(surface, ("kind", "points", "triangles"), path)
        if surface.get("kind") != "tin":
            err(path + ".kind", "must be tin")
        points = surface.get("points")
        good_points = coords(points, 3, path + ".points", 3)
        triangles = surface.get("triangles")
        if not isinstance(triangles, list):
            err(path + ".triangles", "required array")
        else:
            for i, triangle in enumerate(triangles):
                loc = f"{path}.triangles[{i}]"
                if (not isinstance(triangle, (list, tuple)) or len(triangle) != 3
                    or not good_points or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 or v >= len(points) for v in triangle)):
                    err(loc, "triangle indices must index points")
                    continue
                a, b, c = [points[j] for j in triangle]
                if (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]) <= 0:
                    err(loc, "triangle must be nondegenerate and CCW")
        if surface.get("boundary") is not None:
            coords(surface["boundary"], 2, path + ".boundary", 3)
        if surface.get("breaklines") is not None and not isinstance(surface["breaklines"], list):
            err(path + ".breaklines", "must be an array")
        for i, line in enumerate(surface.get("breaklines", []) if isinstance(surface.get("breaklines", []), list) else []):
            coords(line, 3, f"{path}.breaklines[{i}]", 2)
    for alignment, path in collections["alignments"]:
        required(alignment, ("start_station", "horizontal"), path)
        numeric(alignment, ("start_station",), path)
        horizontal = alignment.get("horizontal")
        if not isinstance(horizontal, list) or not horizontal:
            err(path + ".horizontal", "required nonempty array")
            continue
        end = None
        for i, element in enumerate(horizontal):
            loc = f"{path}.horizontal[{i}]"
            if not isinstance(element, dict):
                err(loc, "must be an object")
                continue
            start = element.get("start")
            if not coords([start], 2, loc + ".start"):
                end = None
                continue
            if end is not None and math.dist(start, end) > 1e-6:
                err(loc + ".start", "discontinuous alignment (>1e-6 m)")
            kind = element.get("type")
            if kind in ("line", "arc"):
                if coords([element.get("end")], 2, loc + ".end"):
                    end = element["end"]
                    if kind == "line" and math.dist(start, end) <= 0:
                        err(loc, "line length must be positive")
                else:
                    end = None
                if kind == "arc":
                    numeric(element, ("radius",), loc, positive=True)
                    coords([element.get("center")], 2, loc + ".center")
                    if element.get("dir") not in ("cw", "ccw"):
                        err(loc + ".dir", "must be cw or ccw")
            elif kind == "spiral":
                required(element, ("start_heading", "length", "dir"), loc)
                numeric(element, ("length",), loc, positive=True)
                numeric(element, ("start_heading",), loc)
                numeric(element, ("radius_start", "radius_end"), loc, positive=True, nullable=True)
                if element.get("dir") not in ("cw", "ccw"):
                    err(loc + ".dir", "must be cw or ccw")
                values = [element.get("length"), element.get("start_heading")]
                radii = [element.get("radius_start"), element.get("radius_end")]
                if all(_number(v) for v in values) and values[0] > 0 and all(v is None or (_number(v) and v > 0) for v in radii):
                    from scipy.integrate import quad
                    length, heading = values
                    k0, k1 = [0.0 if r is None else (1 if element.get("dir") == "ccw" else -1)/r for r in radii]
                    def angle(s):
                        return heading + k0*s + (k1-k0)*s*s/(2*length)
                    end = [start[0]+quad(lambda s: math.cos(angle(s)), 0, length)[0],
                           start[1]+quad(lambda s: math.sin(angle(s)), 0, length)[0]]
                else:
                    end = None
            else:
                err(loc + ".type", "invalid alignment element")
                end = None
        vertical = alignment.get("vertical")
        if vertical is not None:
            if not isinstance(vertical, dict) or not isinstance(vertical.get("pvis"), list):
                err(path + ".vertical", "must contain pvis array")
            else:
                previous = None
                for i, pvi in enumerate(vertical["pvis"]):
                    loc = f"{path}.vertical.pvis[{i}]"
                    if not isinstance(pvi, dict):
                        err(loc, "must be an object")
                        continue
                    numeric(pvi, ("station", "elevation", "curve_length"), loc)
                    if _number(pvi.get("station")):
                        if previous is not None and pvi["station"] <= previous:
                            err(loc + ".station", "must increase strictly")
                        previous = pvi["station"]
                    if _number(pvi.get("curve_length")) and pvi["curve_length"] < 0:
                        err(loc + ".curve_length", "must be nonnegative")
    for catchment, path in collections["catchments"]:
        required(catchment, ("outlet", "area_m2"), path)
        if not isinstance(catchment.get("outlet"), str) or catchment["outlet"] not in gravity_nodes:
            err(path + ".outlet", "dangling gravity node reference")
        numeric(catchment, ("area_m2",), path, positive=True)
        for field, low, high in (("runoff_coeff", 0, 1), ("cn", 30, 100), ("imperv_pct", 0, 100)):
            value = catchment.get(field)
            if value is not None and (not _number(value) or not low <= value <= high):
                err(path + "." + field, "outside permitted range")
        if catchment.get("polygon") is not None:
            coords(catchment["polygon"], 2, path + ".polygon", 3)
        numeric(catchment, ("tc_s",), path, positive=True, nullable=True)
        if catchment.get("tc_segments") is not None:
            segments = catchment["tc_segments"]
            if not isinstance(segments, list) or any(not isinstance(segment, dict) for segment in segments):
                err(path + ".tc_segments", "must be an array of objects")
    for structure, path in collections["structures"]:
        kind = structure.get("kind")
        fields = ({"detention_pond": ("stage_storage", "outlets", "inflow_node", "outflow_node"),
                   "culvert": (), "thrust_block": ("network", "node", "bearing_kpa"),
                   "pipe_support": ("piping_doc_id", "run_id", "station", "type")}.get(kind)
                  if isinstance(kind, str) else None)
        if fields is None:
            err(path + ".kind", "invalid structure kind")
        else:
            required(structure, fields, path)
        if kind == "detention_pond":
            coords(structure.get("stage_storage"), 2, path + ".stage_storage", 2)
            if not isinstance(structure.get("outlets"), list):
                err(path + ".outlets", "must be an array")
    for tie, path in collections["tie_ins"]:
        required(tie, ("civil_network", "civil_node", "piping_doc_id", "piping_endpoint", "direction"), path)
        net_id, node_id = tie.get("civil_network"), tie.get("civil_node")
        candidates = network_nodes.get(net_id, []) if isinstance(net_id, str) else []
        if len(candidates) != 1 or not isinstance(node_id, str) or node_id not in candidates[0]:
            err(path + ".civil_node", "dangling or ambiguous civil network/node reference")
        if tie.get("direction") not in ("supply", "discharge"):
            err(path + ".direction", "must be supply or discharge")
        endpoint = tie.get("piping_endpoint")
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("run_id"), str) or endpoint.get("end") not in ("start", "end"):
            err(path + ".piping_endpoint", "requires run_id and end=start|end")
        if tie.get("piping_study") is not None and not isinstance(tie["piping_study"], str):
            err(path + ".piping_study", "must be a string")
        numeric(tie, ("elevation_offset_m",), path, nullable=True)
    return errors

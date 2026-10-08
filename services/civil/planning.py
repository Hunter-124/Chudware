"""Traceable infrastructure quantities, user-priced estimates, CPM and MSPDI exchange.

Measurement conventions are explicit, not jurisdictional design rules. CPM uses
activity-on-node difference constraints (PMI, Practice Standard for Scheduling,
3rd ed., 2019). MSPDI follows Microsoft's Project XML Data Interchange schema.
Serialization functions return text; analysis functions return result dictionaries.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import math
import re
from datetime import date, datetime
from xml.etree import ElementTree as ET

from .errors import CivilError


def _result(name, reference, **fields):
    return {"ok": True, "warnings": [], "assumptions": [],
            "method": {"name": name, "reference": reference}, **fields}


def _number(value, name, *, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CivilError("invalid_input", f"{name} must be finite numeric data")
    if (minimum is not None and value < minimum) or (positive and value <= 0):
        raise CivilError("invalid_input", f"{name} is outside its admissible range")
    return float(value)


def _id(value):
    if not isinstance(value, str) or not value:
        raise CivilError("invalid_input", "A nonempty string id is required")
    return value


def _index(elements):
    if not isinstance(elements, list):
        raise CivilError("invalid_input", "Expected a list of element records")
    out = {}
    for element in elements:
        if not isinstance(element, dict):
            raise CivilError("invalid_input", "Element records must be dictionaries")
        key = _id(element.get("id"))
        if key in out:
            raise CivilError("duplicate_id", element_id=key)
        out[key] = element
    return out


def _path(pipe, nodes):
    try:
        a, b = nodes[pipe["from"]], nodes[pipe["to"]]
    except KeyError as exc:
        raise CivilError("not_found", "Pipe endpoint is missing", element_id=pipe.get("id")) from exc
    xy = [[a.get("x"), a.get("y")], *(pipe.get("vertices") or []), [b.get("x"), b.get("y")]]
    points = []
    for vertex in xy:
        if not isinstance(vertex, (list, tuple)) or len(vertex) < 2:
            raise CivilError("invalid_input", "Pipe vertices need x and y")
        p = [_number(vertex[0], "x"), _number(vertex[1], "y")]
        if not points or p != points[-1]:
            points.append(p)
    lengths = [math.dist(p, q) for p, q in zip(points, points[1:])]
    if not lengths or sum(lengths) <= 0:
        raise CivilError("invalid_input", "Pipe has zero plan length", element_id=pipe["id"])
    return a, b, points, lengths


def _paved_area(strips, polygons):
    """Exact union intersection area of simple polygons by vertical decomposition.

    Vertex and edge-intersection abscissae divide the arrangement into slabs.
    Within each slab the union-intersection ordinate length is linear, so the
    midpoint rectangle integrates it exactly; overlapping paving/strips count once.
    """
    clips = []
    for polygon in polygons:
        if len(polygon) < 3:
            raise CivilError("invalid_input", "Paved polygon needs at least three vertices")
        points = [(_number(v[0], "polygon x"), _number(v[1], "polygon y")) for v in polygon]
        clips.append(points)
    if not strips or not clips:
        return 0.0
    all_polygons = strips + clips
    edges = [(a, b) for polygon in all_polygons for a, b in zip(polygon, polygon[1:] + polygon[:1]) if a != b]
    cuts = {v[0] for polygon in all_polygons for v in polygon}
    for i, (a, b) in enumerate(edges):
        dx, dy = b[0] - a[0], b[1] - a[1]
        for c, d in edges[i + 1:]:
            ex, ey = d[0] - c[0], d[1] - c[1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-14:
                continue
            rx, ry = c[0] - a[0], c[1] - a[1]
            t, u = (rx * ey - ry * ex) / den, (rx * dy - ry * dx) / den
            if 0 <= t <= 1 and 0 <= u <= 1:
                cuts.add(a[0] + t * dx)

    def ordinates(polygons, x):
        intervals = []
        for polygon in polygons:
            ys = sorted(a[1] + (x - a[0]) * (b[1] - a[1]) / (b[0] - a[0])
                        for a, b in zip(polygon, polygon[1:] + polygon[:1])
                        if min(a[0], b[0]) < x < max(a[0], b[0]))
            if len(ys) % 2:
                raise CivilError("invalid_polygon", "Paved polygon scanline has odd crossings")
            intervals.extend(zip(ys[::2], ys[1::2]))
        merged = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(hi, merged[-1][1])
            else:
                merged.append([lo, hi])
        return merged

    area = 0.0
    cuts = sorted(cuts)
    for lo, hi in zip(cuts, cuts[1:]):
        x = (lo + hi) / 2
        if not lo < x < hi:
            continue
        subjects, bounds = ordinates(strips, x), ordinates(clips, x)
        length = sum(max(0.0, min(b, d) - max(a, c)) for a, b in subjects for c, d in bounds)
        area += (hi - lo) * length
    return area


def quantity_takeoff(model, *, ground=None, trench=None, surfaces_volume=None):
    """Measure model quantities, integrating trench sections by composite Simpson.

    Trench options: required ``side_clearance_m`` (unless ``width_table`` supplies
    [{max_diameter_m, width_m}]), ``bedding_depth_m``, ``sample_step_m``; optional
    ``side_slope`` (H:V, default vertical), ``paved_polygons`` ([{id, polygon}]).
    OD must be explicit on each pipe as ``outer_diameter_m`` or supplied by
    ``outer_diameter_by_material`` {material: [[ID_m, OD_m], ...]} in trench.
    Inverts are the pipe-bottom measurement datum; bedding is below that datum.
    Haunch is invert to springline minus half the external pipe displacement;
    backfill is all remaining excavation above bedding/haunch, minus the pipe.
    Excavation depth = ground - invert + bedding. Sloped area = w*d + s*d².
    Simpson's prismoidal equation is exact for linearly varying depth.
    """
    if not isinstance(model, dict) or (ground is not None and not callable(ground)):
        raise CivilError("invalid_input", "Model must be a dict and ground callable")
    out = _result("geometric quantity takeoff", "Prismoidal formula V=L(A0+4Am+A1)/6", items=[])
    items = out["items"]
    groups = {}

    def add(item, ids, quantity, unit, basis):
        items.append({"item": item, "element_ids": list(ids), "quantity": quantity, "unit": unit, "basis": basis})

    if trench is not None:
        if not isinstance(trench, dict):
            raise CivilError("invalid_input", "trench must be a dict")
        bedding = _number(trench.get("bedding_depth_m"), "bedding_depth_m", minimum=0)
        step = _number(trench.get("sample_step_m"), "sample_step_m", positive=True)
        slope = _number(trench.get("side_slope", 0), "side_slope", minimum=0)
        out["assumptions"].append("Trench inverts are pipe-bottom datum; bedding below invert; symmetric H:V walls; no shoring, swell, waste or compaction factors.")
        if slope == 0:
            out["assumptions"].append("Vertical trench walls selected (side_slope=0).")
    for collection, node_keys, pipe_key in (("water_networks", ("junctions", "reservoirs", "tanks"), "pipes"),
                                           ("gravity_networks", ("nodes",), "conduits")):
        for network in model.get(collection, []):
            nid = _id(network.get("id"))
            nodes = _index([n for key in node_keys for n in network.get(key) or []])
            for node in nodes.values():
                kind = node.get("type", next((key[:-1] for key in node_keys if node in (network.get(key) or [])), "structure"))
                key = ("structure_count", nid, kind, node.get("diameter"))
                groups.setdefault(key, [[], 0])[0].append(node["id"])
                groups[key][1] += 1
            _index(network.get(pipe_key) or [])
            for pipe in network.get(pipe_key) or []:
                pid = pipe["id"]
                a, b, points, lengths = _path(pipe, nodes)
                length = sum(lengths)
                if pipe.get("length") is not None and not math.isclose(_number(pipe["length"], "length", positive=True), length, rel_tol=1e-6, abs_tol=1e-6):
                    out["warnings"].append(f"{nid}/{pid}: hydraulic length differs from measured plan geometry; QTO uses geometry.")
                diameter = pipe.get("diameter")
                if diameter is not None:
                    diameter = _number(diameter, "diameter", positive=True)
                material = pipe.get("material")
                basis = {"network_id": nid, "material": material, "diameter_m": diameter, "category": "pipe"}
                za = pipe.get("inlet_invert") if collection == "gravity_networks" else pipe.get("start_elevation_m")
                zb = pipe.get("outlet_invert") if collection == "gravity_networks" else pipe.get("end_elevation_m")
                if za is None:
                    za = a.get("invert") if collection == "gravity_networks" else a.get("elevation")
                if zb is None:
                    zb = b.get("invert") if collection == "gravity_networks" else b.get("elevation")
                length3d = None
                if za is not None and zb is not None:
                    za, zb = _number(za, "start elevation"), _number(zb, "end elevation")
                    length3d = math.hypot(length, zb - za)
                else:
                    out["warnings"].append(f"{nid}/{pid}: 3D length unavailable; endpoint elevations missing (reservoir head is not elevation).")
                for name, measured in (("pipe_length_plan", length), ("pipe_length_3d", length3d)):
                    if measured is not None:
                        key = (name, nid, material, diameter)
                        groups.setdefault(key, [[], 0])[0].append(pid)
                        groups[key][1] += measured
                if trench is None:
                    continue
                if za is None or zb is None:
                    raise CivilError("elevation_missing", "Trench requires endpoint inverts/elevations", element_id=pid)
                if pipe.get("shape", "circular") != "circular":
                    raise CivilError("unsupported_shape", "Trench embedment currently requires circular pipe", element_id=pid)
                od = pipe.get("outer_diameter_m")
                if od is None:
                    rows = trench.get("outer_diameter_by_material", {}).get(material, [])
                    for internal, external in rows:
                        if diameter == internal:
                            od = external
                            break
                if od is None:
                    raise CivilError("outer_diameter_missing", "Supply OD; hydraulic diameter is not outside diameter", element_id=pid)
                od = _number(od, "outer_diameter_m", positive=True)
                if diameter is not None and od < diameter:
                    raise CivilError("invalid_input", "OD is smaller than hydraulic diameter", element_id=pid)
                width = None
                for row in sorted(trench.get("width_table", []), key=lambda r: r["max_diameter_m"]):
                    if od <= _number(row.get("max_diameter_m"), "max_diameter_m", positive=True):
                        width = _number(row.get("width_m"), "width_m", positive=True)
                        break
                if width is None:
                    if trench.get("width_table"):
                        raise CivilError("trench_width_missing", "OD exceeds user width table", element_id=pid)
                    width = od + 2 * _number(trench.get("side_clearance_m"), "side_clearance_m", minimum=0)
                if width < od:
                    raise CivilError("invalid_input", "Trench bottom width cannot be less than OD")
                if ground is None:
                    rim_a = _number(a.get("rim"), "from node rim")
                    rim_b = _number(b.get("rim"), "to node rim")
                    out["assumptions"].append(f"{nid}/{pid}: ground interpolated linearly between endpoint rims.")
                volume = 0.0
                pavement_strips = []
                min_depth = math.inf
                station = 0.0

                def depth_at(s, x, y):
                    nonlocal min_depth
                    z = ground(x, y) if ground else rim_a + (rim_b - rim_a) * s / length
                    if z is None:
                        raise CivilError("ground_outside_surface", element_id=pid, x=x, y=y)
                    d = _number(z, "ground elevation") - (za + (zb - za) * s / length) + bedding
                    min_depth = min(min_depth, d)
                    if d < bedding + od:
                        raise CivilError("insufficient_trench_depth", "Pipe crown is above sampled ground", element_id=pid, depth_m=d)
                    return d

                paved = trench.get("paved_polygons", [])
                if not paved:
                    paved = [p for p in [*model.get("catchments", []), *model.get("surfaces", [])]
                             if p.get("paved") and (p.get("polygon") or p.get("boundary"))]
                for p, q, segment in zip(points, points[1:], lengths):
                    panels = max(1, math.ceil(segment / step))
                    for j in range(panels):
                        u, v = j / panels, (j + 1) / panels
                        vals, depths = [], []
                        for t in (u, (u + v) / 2, v):
                            d = depth_at(station + segment * t, p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
                            depths.append(d)
                            vals.append(width * d + slope * d * d)
                        volume += segment / panels * (vals[0] + 4 * vals[1] + vals[2]) / 6
                        if paved:
                            normal = [-(q[1] - p[1]) / segment, (q[0] - p[0]) / segment]
                            for left, right, dl, dr in ((u, (u + v) / 2, depths[0], depths[1]),
                                                       ((u + v) / 2, v, depths[1], depths[2])):
                                ends = [[p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t] for t in (left, right)]
                                radii = [width / 2 + slope * dl, width / 2 + slope * dr]
                                pavement_strips.append([
                                    [ends[k][0] + sign * radii[k] * normal[0], ends[k][1] + sign * radii[k] * normal[1]]
                                    for k, sign in ((0, 1), (1, 1), (1, -1), (0, -1))])
                    station += segment
                bed_volume = (width * bedding + slope * bedding ** 2) * length
                displaced = math.pi * od ** 2 / 4 * length3d
                haunch = (width * od / 2 + slope * ((bedding + od / 2) ** 2 - bedding ** 2)) * length - displaced / 2
                backfill = volume - bed_volume - haunch - displaced
                if haunch < 0 or backfill < -1e-10:
                    raise CivilError("invalid_trench_geometry", "Inclined pipe displacement exceeds available embedment")
                tb = {**basis, "category": "earthwork", "outer_diameter_m": od, "bottom_width_m": width,
                      "bedding_depth_m": bedding, "side_slope": slope, "sample_step_m": step,
                      "minimum_sampled_depth_m": min_depth, "plan_length_m": length, "length_3d_m": length3d}
                for item, qty in (("trench_excavation", volume), ("bedding", bed_volume), ("haunch", haunch),
                                  ("backfill", backfill), ("pipe_displaced", displaced)):
                    add(item, [pid], qty, "m3", tb.copy())
                if paved:
                    paved_area = _paved_area(pavement_strips, [p.get("polygon") or p.get("boundary") or [] for p in paved])
                    add("pavement_restoration", [pid], paved_area, "m2", {**tb, "category": "pavement", "polygon_ids": [p.get("id") for p in paved]})
                    note = "Pavement restoration is union area of sampled top-width strips intersecting paved polygons, including lateral intersections; no end caps or construction overcut."
                    if note not in out["assumptions"]:
                        out["assumptions"].append(note)
    _index(model.get("structures", []))
    for structure in model.get("structures", []):
        key = ("structure_count", None, structure.get("kind"), structure.get("diameter"))
        groups.setdefault(key, [[], 0])[0].append(structure["id"])
        groups[key][1] += 1
    for (item, nid, material_or_type, diameter), (ids, qty) in groups.items():
        structure = item == "structure_count"
        add(item, ids, qty, "each" if structure else "m",
            {"network_id": nid, "type" if structure else "material": material_or_type,
             "diameter_m": diameter, "category": "structures" if structure else "pipe",
             "measurement": "count" if structure else ("linear elevation along plan station" if item == "pipe_length_3d" else "plan polyline")})
    for volume in surfaces_volume or []:
        if volume.get("ok") is False:
            raise CivilError("invalid_input", "Cannot take quantities from failed surface analysis")
        ids = volume.get("element_ids") or volume.get("surface_ids")
        if not ids:
            raise CivilError("quantity_trace_missing", "Earthwork volume needs element_ids or surface_ids")
        for kind in ("cut", "fill"):
            add("earthwork_" + kind, [_id(i) for i in ids], _number(volume.get(kind + "_m3"), kind + "_m3", minimum=0), "m3",
                {"category": "earthwork", "source_method": copy.deepcopy(volume.get("method"))})
    out["assumptions"].append("Plan and 3D pipe quantities are alternative measurement bases, not additive purchases. Hydraulic length is not a measured quantity. No fittings allowance.")
    return out


def cost_estimate(qto, price_book, *, contingency_pct=0, escalation=None):
    """Quantity × user unit price, without bundled rates or currency conversion.

    Price entries keyed by item code require unit/unit_price/currency/source/date;
    optional match={item,material,diameter_m,type,network_id}, category. A direct
    item-code equal to item is a match too. Ambiguous matches are refused.
    Escalation is {factor} or {rate_pct,years}, compounded (1+r/100)**years.
    """
    contingency = _number(contingency_pct, "contingency_pct", minimum=0) / 100
    if not isinstance(price_book, dict) or not isinstance(qto, dict):
        raise CivilError("invalid_input", "qto and price_book must be dicts")
    factor = 1.0
    if escalation is not None:
        if not isinstance(escalation, dict):
            raise CivilError("invalid_input", "escalation must be a dict")
        if set(escalation) == {"factor"}:
            factor = _number(escalation["factor"], "escalation factor", positive=True)
        elif set(escalation) == {"rate_pct", "years"}:
            rate = _number(escalation["rate_pct"], "rate_pct")
            years = _number(escalation["years"], "years", minimum=0)
            if rate <= -100:
                raise CivilError("invalid_input", "Escalation rate must exceed -100%")
            try:
                factor = (1 + rate / 100) ** years
            except OverflowError as exc:
                raise CivilError("invalid_input", "Escalation overflows") from exc
            _number(factor, "escalation factor", positive=True)
        else:
            raise CivilError("invalid_input", "Use factor or rate_pct and years for escalation")
    out = _result("unit-price estimate", "Bill of quantities: quantity × user-supplied rate", items=[], unpriced=[], totals_by_currency={})
    allowed = {"item", "material", "diameter_m", "type", "network_id"}
    for code, entry in price_book.items():
        if not isinstance(entry, dict) or any(k not in entry for k in ("unit", "unit_price", "currency", "source", "date")):
            raise CivilError("invalid_price_book", item_code=code)
        for field in ("unit", "currency", "source", "date"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise CivilError("invalid_price_book", item_code=code, field=field)
        _number(entry["unit_price"], "unit_price", minimum=0)
        match = entry.get("match", {"item": code})
        if not isinstance(match, dict) or not match or not set(match) <= allowed:
            raise CivilError("invalid_price_book", "Unsupported/empty match rule", item_code=code)
    for row in qto.get("items", []):
        qty = _number(row.get("quantity"), "quantity", minimum=0)
        basis = row.get("basis", {})
        attrs = {**basis, "item": row.get("item")}
        candidates = [(code, entry) for code, entry in price_book.items()
                      if all(attrs.get(key) == value for key, value in entry.get("match", {"item": code}).items())]
        if not candidates:
            out["unpriced"].append(copy.deepcopy(row))
            continue
        if len(candidates) > 1:
            raise CivilError("ambiguous_price_match", item=row["item"], item_codes=[c[0] for c in candidates])
        code, entry = candidates[0]
        if row.get("unit") != entry["unit"]:
            raise CivilError("unit_mismatch", item=row["item"], quantity_unit=row.get("unit"), price_unit=entry["unit"])
        amount = qty * entry["unit_price"]
        currency = entry["currency"]
        category = entry.get("category", basis.get("category", row["item"]))
        totals = out["totals_by_currency"].setdefault(currency, {"subtotal": 0.0, "categories": {}})
        totals["subtotal"] += amount
        totals["categories"][category] = totals["categories"].get(category, 0.0) + amount
        out["items"].append({**copy.deepcopy(row), "item_code": code, "category": category, "unit_price": entry["unit_price"],
                             "currency": currency, "source": entry["source"], "date": entry["date"], "amount": amount})
    for totals in out["totals_by_currency"].values():
        totals["escalation"] = totals["subtotal"] * (factor - 1)
        totals["contingency"] = totals["subtotal"] * factor * contingency
        totals["total"] = totals["subtotal"] * factor * (1 + contingency)
        totals["category_totals"] = {c: v * factor * (1 + contingency) for c, v in totals["categories"].items()}
    out["complete"] = not out["unpriced"]
    if not out["complete"]:
        out["warnings"].append("Estimate is incomplete: unpriced quantities are excluded, not assigned a zero price.")
    out["assumptions"] = ["Currencies are totaled separately; no currency conversion or bundled rates.",
                           "Contingency applies after escalation; category totals allocate both factors proportionately."]
    return out


def schedule_cpm(tasks):
    """DAG CPM with FS/SS/FF/SF lags via start-time difference constraints.

    ES(v)>=ES(u)+w: FS w=Du+lag, SS w=lag, FF w=Du-Dv+lag,
    SF w=lag-Dv. Backward bounds LS(u)<=LS(v)-w. Continuous
    workdays, no resource leveling/calendars (PMI Scheduling Standard, 2019).
    """
    if not isinstance(tasks, list):
        raise CivilError("invalid_input", "tasks must be a list")
    indexed = _index(tasks)
    rows = copy.deepcopy(indexed)
    incoming = {key: [] for key in rows}
    outgoing = {key: [] for key in rows}
    for key, row in rows.items():
        row["duration_d"] = _number(row.get("duration_d"), "duration_d", minimum=0)
        row["name"] = row.get("name", key)
        if not isinstance(row["name"], str) or not isinstance(row.get("element_ids", []), list) or not isinstance(row.get("predecessors", []), list):
            raise CivilError("invalid_input", "Task name must be text, element_ids/predecessors lists")
        row["element_ids"] = list(dict.fromkeys(_id(i) for i in row.get("element_ids", [])))
        row["predecessors"] = row.get("predecessors", [])
    for key, row in rows.items():
        seen = set()
        for pred in row["predecessors"]:
            if not isinstance(pred, dict):
                raise CivilError("invalid_input", "Predecessor records must be dictionaries")
            previous = pred.get("id")
            if previous not in rows:
                raise CivilError("not_found", "Predecessor task is missing", task_id=previous)
            kind = pred.get("type", "FS")
            if kind not in ("FS", "SS", "FF", "SF"):
                raise CivilError("unsupported_dependency", dependency_type=kind)
            if (previous, kind) in seen:
                raise CivilError("duplicate_dependency", task_id=key)
            seen.add((previous, kind))
            lag = _number(pred.get("lag_d", 0), "lag_d")
            pred["type"], pred["lag_d"] = kind, lag
            du, dv = rows[previous]["duration_d"], row["duration_d"]
            weight = {"FS": du + lag, "SS": lag, "FF": du - dv + lag, "SF": lag - dv}[kind]
            incoming[key].append((previous, weight))
            outgoing[previous].append((key, weight))
    degree = {key: len(edges) for key, edges in incoming.items()}
    queue = [key for key in rows if degree[key] == 0]
    order = []
    for key in queue:
        order.append(key)
        for successor, _ in outgoing[key]:
            degree[successor] -= 1
            if degree[successor] == 0:
                queue.append(successor)
    if len(order) != len(rows):
        raise CivilError("schedule_cycle", task_ids=[key for key, deg in degree.items() if deg])
    for key in order:
        es = max([0.0, *(rows[p]["es_d"] + w for p, w in incoming[key])])
        rows[key].update(es_d=es, ef_d=es + rows[key]["duration_d"])
    duration = max((r["ef_d"] for r in rows.values()), default=0.0)
    for key in reversed(order):
        row = rows[key]
        ls = min([duration - row["duration_d"], *(rows[s]["ls_d"] - w for s, w in outgoing[key])])
        tf = ls - row["es_d"]
        ff = min([duration - row["ef_d"], *(rows[s]["es_d"] - row["es_d"] - w for s, w in outgoing[key])])
        row.update(ls_d=ls, lf_d=ls + row["duration_d"], total_float_d=tf, free_float_d=ff, critical=abs(tf) < 1e-9)
    critical_path = []
    endpoint = next((k for k in reversed(order) if rows[k]["critical"] and abs(rows[k]["ef_d"] - duration) < 1e-9), None)
    while endpoint is not None:
        critical_path.append(endpoint)
        endpoint = next((p for p, w in incoming[endpoint] if rows[p]["critical"] and
                         abs(rows[p]["es_d"] + w - rows[endpoint]["es_d"]) < 1e-9), None)
    critical_path.reverse()
    return _result("activity-on-node generalized CPM", "PMI Practice Standard for Scheduling, 3rd ed. (2019)",
                   tasks=[rows[key] for key in order], project_duration_d=duration, critical_path=critical_path,
                   critical_tasks=[k for k in order if rows[k]["critical"]],
                   assumptions=["Continuous workdays from day zero; no resource constraints or calendars; critical_path is one driving chain, critical_tasks includes parallel critical chains."])


def _element_ids(model):
    if not isinstance(model, dict):
        raise CivilError("invalid_input", "Model must be a dictionary")
    ids = set()

    def add(value):
        value = _id(value)
        if value in ids:
            raise CivilError("ambiguous_element_id", "4D links require unambiguous IDs across collections/networks", element_id=value)
        ids.add(value)

    for collection in ("surfaces", "alignments", "catchments", "structures", "tie_ins", "water_networks", "gravity_networks"):
        for element in model.get(collection, []):
            add(element.get("id"))
            if collection.endswith("networks"):
                for key in ("nodes", "junctions", "reservoirs", "tanks", "pipes", "conduits", "pumps", "valves"):
                    for child in element.get(key) or []:
                        add(child.get("id"))
    return ids


def _schedule_rows(schedule):
    if not isinstance(schedule, dict) or not isinstance(schedule.get("tasks"), list):
        raise CivilError("invalid_input", "Expected a schedule_cpm result")
    rows = schedule["tasks"]
    _index(rows)
    for row in rows:
        es = _number(row.get("es_d"), "es_d", minimum=0)
        ef = _number(row.get("ef_d"), "ef_d", minimum=0)
        if ef < es:
            raise CivilError("invalid_input", "Task finishes before it starts")
    return rows


def link_4d(schedule, model):
    """Map many-to-many element assignments to early install windows (PMI CPM)."""
    known = _element_ids(model)
    elements = {key: {"task_ids": [], "install_window_d": None, "tasks": []} for key in sorted(known)}
    for row in _schedule_rows(schedule):
        for key in row.get("element_ids", []):
            if key not in known:
                raise CivilError("not_found", "Scheduled model element is missing", element_id=key)
            record = elements[key]
            record["task_ids"].append(row["id"])
            record["tasks"].append({"id": row["id"], "start_d": row["es_d"], "finish_d": row["ef_d"]})
            old = record["install_window_d"]
            record["install_window_d"] = [min(old[0], row["es_d"]), max(old[1], row["ef_d"])] if old else [row["es_d"], row["ef_d"]]
    return _result("4D model-task assignment", "Many-to-many model element assignment to CPM early windows", elements=elements,
                   warnings=[f"{key}: no scheduled task" for key, record in elements.items() if not record["task_ids"]])


def state_at(schedule, day):
    """Planned until first ES, complete when all assigned tasks EF<=day.

    Between disjoint assigned tasks an element stays in-progress, not complete.
    Zero-duration milestones are complete at their exact event day.
    """
    day = _number(day, "day")
    windows = {}
    for row in _schedule_rows(schedule):
        for element in row.get("element_ids", []):
            windows.setdefault(element, []).append(row)
    statuses = {}
    for element, rows in windows.items():
        statuses[element] = "complete" if all(day >= r["ef_d"] for r in rows) else (
            "planned" if all(day < r["es_d"] for r in rows) else "in-progress")
    return _result("4D early-schedule snapshot", "CPM planned start/finish, not actual field progress", day_d=day,
                   elements=statuses, planned=[e for e, s in statuses.items() if s == "planned"],
                   in_progress=[e for e, s in statuses.items() if s == "in-progress"],
                   complete=[e for e, s in statuses.items() if s == "complete"],
                   assumptions=["Snapshot uses planned early dates, not measured construction progress."])


_NS = "http://schemas.microsoft.com/project"
_TYPES = {"FF": 0, "FS": 1, "SF": 2, "SS": 3}
_TEXT1 = "188743731"


def export_msproject_xml(schedule, start_date):
    """Serialize minimal MSPDI; 1 workday=8 h, lag in tenths of minutes.

    Civil task IDs/element assignments persist in MSPDI task Text1. No dates or
    calendar constraints beyond project start are inferred. Reference: Microsoft
    Project XML Data Interchange, Task/PredecessorLink schema (Type 0..3).
    """
    rows = _schedule_rows(schedule)
    try:
        start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
        if isinstance(start, datetime):
            start = start.date()
        if not isinstance(start, date):
            raise ValueError
    except ValueError as exc:
        raise CivilError("invalid_input", "start_date must be ISO YYYY-MM-DD") from exc
    ET.register_namespace("", _NS)
    root = ET.Element(f"{{{_NS}}}Project")

    def put(parent, key, value):
        child = ET.SubElement(parent, f"{{{_NS}}}{key}")
        child.text = str(value)
        return child

    put(root, "Name", "Civil construction schedule")
    put(root, "ScheduleFromStart", 1)
    put(root, "StartDate", start.isoformat() + "T08:00:00")
    put(root, "MinutesPerDay", 480)
    put(root, "MinutesPerWeek", 2400)
    attrs = ET.SubElement(root, f"{{{_NS}}}ExtendedAttributes")
    attr = ET.SubElement(attrs, f"{{{_NS}}}ExtendedAttribute")
    put(attr, "FieldID", _TEXT1)
    put(attr, "FieldName", "Text1")
    put(attr, "Alias", "Civil task identity and elements")
    parent = ET.SubElement(root, f"{{{_NS}}}Tasks")
    uids = {row["id"]: i for i, row in enumerate(rows, 1)}
    for row in rows:
        task = ET.SubElement(parent, f"{{{_NS}}}Task")
        put(task, "UID", uids[row["id"]])
        put(task, "ID", uids[row["id"]])
        put(task, "Name", row["name"])
        put(task, "Duration", f"PT{row['duration_d'] * 8:.15g}H")
        put(task, "DurationFormat", 7)
        put(task, "Milestone", int(row["duration_d"] == 0))
        put(task, "Summary", 0)
        for pred in row["predecessors"]:
            lag = pred["lag_d"] * 4800
            if not math.isclose(lag, round(lag), abs_tol=1e-8):
                raise CivilError("unsupported_schedule_precision", "MSPDI lags must be representable in tenths of minutes")
            link = ET.SubElement(task, f"{{{_NS}}}PredecessorLink")
            put(link, "PredecessorUID", uids[pred["id"]])
            put(link, "Type", _TYPES[pred["type"]])
            put(link, "LinkLag", round(lag))
            put(link, "LagFormat", 7)
        attr = ET.SubElement(task, f"{{{_NS}}}ExtendedAttribute")
        put(attr, "FieldID", _TEXT1)
        put(attr, "Value", json.dumps({"id": row["id"], "element_ids": row["element_ids"]}, separators=(",", ":")))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def import_msproject_xml(text):
    """Read the minimal MSPDI subset and recompute CPM, preserving Text1 IDs.

    Summary tasks, calendars, actual progress, cross-project links and task-date
    constraints are refused rather than silently discarded. Standard 8-hour
    workday durations support ISO PT hours/minutes/seconds.
    """
    if not isinstance(text, str) or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise CivilError("invalid_schedule_xml", "XML document types/entities are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CivilError("invalid_schedule_xml", str(exc)) from exc
    ns = {"p": _NS}
    if root.tag != f"{{{_NS}}}Project":
        raise CivilError("invalid_schedule_xml", "Expected MSPDI Project namespace")

    def val(element, key, default=None):
        return element.findtext("p:" + key, default, ns)

    if root.find("p:Calendars", ns) is not None or val(root, "MinutesPerDay", "480") != "480":
        raise CivilError("unsupported_schedule_calendar", "Only abstract 8-hour workdays are supported")
    if val(root, "ScheduleFromStart", "1") != "1":
        raise CivilError("unsupported_schedule_constraint", "Backward scheduled projects are unsupported")
    records = []
    uid_map = {}
    for task in root.findall("p:Tasks/p:Task", ns):
        uid = val(task, "UID")
        if uid in uid_map or uid is None:
            raise CivilError("invalid_schedule_xml", "Missing or duplicate task UID")
        if val(task, "Summary", "0") != "0":
            raise CivilError("unsupported_schedule_summary", "Flatten summary tasks before import")
        if val(task, "ConstraintType", "0") != "0" or val(task, "CalendarUID", "-1") not in ("-1", "0"):
            raise CivilError("unsupported_schedule_constraint", task_uid=uid)
        if val(task, "PercentComplete", "0") != "0" or task.find("p:ActualStart", ns) is not None:
            raise CivilError("unsupported_schedule_progress", task_uid=uid)
        duration = val(task, "Duration", "")
        match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", duration)
        if match is None or not any(match.groups()):
            raise CivilError("invalid_schedule_xml", "Unsupported duration", duration=duration)
        hours, minutes, seconds = (float(v or 0) for v in match.groups())
        identity = {"id": uid, "element_ids": []}
        for attr in task.findall("p:ExtendedAttribute", ns):
            if val(attr, "FieldID") == _TEXT1:
                value = val(attr, "Value", "")
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue  # Ordinary non-civil Text1 is not an identity record.
                if isinstance(parsed, dict) and "id" in parsed and "element_ids" in parsed:
                    identity = parsed
        uid_map[uid] = _id(identity.get("id"))
        records.append((task, {"id": identity["id"], "name": val(task, "Name", uid),
                               "duration_d": (hours + minutes / 60 + seconds / 3600) / 8,
                               "element_ids": identity.get("element_ids", []), "predecessors": []}))
    for task, row in records:
        for link in task.findall("p:PredecessorLink", ns):
            if val(link, "CrossProject", "0") != "0":
                raise CivilError("unsupported_schedule_dependency", "Cross-project links unsupported")
            uid, kind = val(link, "PredecessorUID"), val(link, "Type", "1")
            if uid not in uid_map:
                raise CivilError("not_found", task_uid=uid)
            if kind not in {str(v) for v in _TYPES.values()}:
                raise CivilError("unsupported_dependency", dependency_type=kind)
            lag_format = val(link, "LagFormat", "7")
            if lag_format not in ("3", "5", "7", "9", "11"):
                raise CivilError("unsupported_schedule_calendar", "Elapsed/percent lag format unsupported")
            try:
                lag = int(val(link, "LinkLag", "0")) / 4800
            except ValueError as exc:
                raise CivilError("invalid_schedule_xml", "Invalid LinkLag") from exc
            row["predecessors"].append({"id": uid_map[uid], "type": next(k for k, v in _TYPES.items() if str(v) == kind), "lag_d": lag})
    out = schedule_cpm([row for _, row in records])
    out["start_date"] = val(root, "StartDate")
    out["method"]["reference"] += "; Microsoft Project XML Data Interchange"
    return out


def export_csv(result):
    """Export QTO/cost lines with JSON-encoded basis and element ID columns."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise CivilError("invalid_input", "Expected QTO or cost result with items")
    columns = ["item", "element_ids", "quantity", "unit", "basis"]
    if "unpriced" in result:
        columns += ["priced", "item_code", "category", "unit_price", "currency", "amount", "source", "date"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for priced, rows in ((True, result["items"]), (False, result.get("unpriced", []))):
        for row in rows:
            record = {**row, "element_ids": json.dumps(row.get("element_ids", [])),
                      "basis": json.dumps(row.get("basis", {}), sort_keys=True), "priced": priced}
            writer.writerow(record)
    return stream.getvalue()


def requirement_evidence(check_rows, requirement_ids_by_check):
    """Map criterion rows to Trace.evidence payloads grouped by requirement ID.

    ``Trace`` (services.requirements.requirements) accepts arbitrary evidence
    dictionaries. study is copied from each row, or null if not supplied; no
    successful requirement status is inferred from one check. Unmapped checks
    are reported explicitly. Values/limits/references are retained unchanged.
    """
    out = _result("criteria-to-requirement evidence", "aicad.civil/1 §2; services.requirements.requirements.Trace", evidence_by_requirement={}, unmapped=[])
    if not isinstance(check_rows, list) or not isinstance(requirement_ids_by_check, dict):
        raise CivilError("invalid_input", "Expected check list and requirement mapping")
    required = {"check", "element_id", "value", "limit", "units", "pass", "criterion", "reference"}
    for row in check_rows:
        if not isinstance(row, dict) or not required <= row.keys() or not isinstance(row["pass"], bool):
            raise CivilError("invalid_check_row", "Check row must match civil contract §2")
        ids = requirement_ids_by_check.get(row["check"], [])
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            raise CivilError("invalid_input", "Requirement IDs must be string or list")
        evidence = {"source": "civil", **{key: copy.deepcopy(row[key]) for key in
                    ("check", "element_id", "value", "limit", "units", "pass", "reference")}, "study": row.get("study")}
        if not ids:
            out["unmapped"].append(copy.deepcopy(row))
        for rid in dict.fromkeys(_id(i) for i in ids):
            out["evidence_by_requirement"].setdefault(rid, []).append(copy.deepcopy(evidence))
    if out["unmapped"]:
        out["warnings"].append("Some check rows have no requirement mapping.")
    return out

"""SWMM 5.2 INP exchange for steady gravity networks and CN catchments.

Grammar/units: EPA *SWMM User's Manual*, version 5.2 (2022), Appendix D.
This adapter does not run SWMM. Unsupported sections/rows are retained in
``dropped`` and explicitly reported; export can re-emit them unchanged. Neither
EPA engine code nor third-party parser code is copied into this module.
"""
from __future__ import annotations

import math
import shlex

from .errors import CivilError

_REFERENCE = "EPA SWMM User's Manual 5.2 (2022), Appendix D, input-file format"
_FLOW = {"CFS": 0.028316846592, "GPM": 0.003785411784 / 60,
         "MGD": 3785.411784 / 86400, "CMS": 1.0, "LPS": 0.001, "MLD": 1000 / 86400}
_US = {"CFS", "GPM", "MGD"}
_SUPPORTED = {"OPTIONS", "JUNCTIONS", "OUTFALLS", "CONDUITS", "XSECTIONS", "SUBCATCHMENTS",
              "SUBAREAS", "INFILTRATION", "COORDINATES", "VERTICES", "POLYGONS", "DWF"}


def _result(name, **values):
    return {"ok": True, **values, "warnings": [], "assumptions": [],
            "method": {"name": name, "reference": _REFERENCE}}


def _numeric(token, field):
    try:
        value = float(token)
    except (ValueError, TypeError):
        raise CivilError("invalid_swmm_number", field=field, token=token) from None
    if not math.isfinite(value):
        raise CivilError("invalid_swmm_number", field=field, token=token)
    return value


def _sections(text):
    if not isinstance(text, str):
        raise CivilError("invalid_swmm_input", "Supply INP file contents as text.")
    sections, current = {}, None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if current is not None:
                sections[current].append((number, raw, []))
            continue
        if line.startswith("["):
            header = line.split(";", 1)[0].strip()
            if not header.endswith("]") or len(header) < 3:
                raise CivilError("invalid_swmm_section", line=number)
            current = header[1:-1].upper()
            sections.setdefault(current, [])
            continue
        if current is None:
            raise CivilError("swmm_data_outside_section", line=number)
        try:
            lexer = shlex.shlex(raw, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ";"
            tokens = list(lexer)
        except ValueError:
            raise CivilError("invalid_swmm_row", line=number) from None
        sections[current].append((number, raw, tokens))
    return sections


def _require(tokens, count, section, line):
    if len(tokens) < count:
        raise CivilError("invalid_swmm_row", section=section, line=line, required_fields=count)


def import_inp(text: str, *, id: str = "swmm", name: str = "SWMM import") -> dict:
    """Import documented SWMM sections; convert foreign quantities to SI.

    SWMM FIXED outfall stages are absolute elevations, whereas civil
    ``outfall_tailwater`` is depth above node invert. Missing plan coordinates
    are explicitly represented as an assumed (0,0), with warnings.
    """
    sections = _sections(text)
    options = {}
    for line, _, tokens in sections.get("OPTIONS", []):
        if not tokens:
            continue
        _require(tokens, 2, "OPTIONS", line)
        options[tokens[0].upper()] = " ".join(tokens[1:])
    if "FLOW_UNITS" not in options:
        raise CivilError("swmm_flow_units_missing")
    units = options["FLOW_UNITS"].upper()
    if units not in _FLOW:
        raise CivilError("unsupported_swmm_flow_units", flow_units=units)
    lf, af, df = (0.3048, 4046.8564224, 25.4) if units in _US else (1.0, 10000.0, 1.0)
    qf = _FLOW[units]
    offsets = options.get("LINK_OFFSETS", "DEPTH").upper()
    if offsets not in ("DEPTH", "ELEVATION"):
        raise CivilError("unsupported_swmm_link_offsets", link_offsets=offsets)
    infiltration = options.get("INFILTRATION", "HORTON").upper()
    dropped = {section: [raw for _, raw, _ in rows] for section, rows in sections.items() if section not in _SUPPORTED}
    warnings, assumptions = [], []
    def preserve(section, raw):
        dropped.setdefault(section, []).append(raw)
    def number(tokens, index, field, factor=1.0, default=None):
        if index >= len(tokens):
            if default is None:
                raise CivilError("invalid_swmm_row", field=field)
            return default
        return _numeric(tokens[index], field) * factor
    nodes, pipes, catchments = {}, {}, {}
    def unique(target, key, section, line):
        if key in target:
            raise CivilError("duplicate_swmm_id", element_id=key, section=section, line=line)
    for line, _, tokens in sections.get("JUNCTIONS", []):
        if not tokens:
            continue
        _require(tokens, 2, "JUNCTIONS", line)
        unique(nodes, tokens[0], "JUNCTIONS", line)
        invert = number(tokens, 1, "invert", lf)
        depth = number(tokens, 2, "maximum_depth", lf, 0.0)
        if depth < 0:
            raise CivilError("invalid_swmm_depth", element_id=tokens[0])
        if depth == 0:
            rim = None
            warnings.append(f"{tokens[0]}: SWMM automatic maximum depth cannot define a civil rim; rim remains unknown.")
        else:
            rim = invert + depth
        nodes[tokens[0]] = {"id": tokens[0], "type": "manhole", "x": 0.0, "y": 0.0,
                            "invert": invert, "rim": rim, "inflow_m3s": 0.0, "loss_k": None,
                            "outfall_tailwater": None,
                            "swmm_initial_depth_m": number(tokens, 3, "initial_depth", lf, 0.0),
                            "swmm_surcharge_depth_m": number(tokens, 4, "surcharge_depth", lf, 0.0),
                            "swmm_ponded_area_m2": number(tokens, 5, "ponded_area", lf * lf, 0.0)}
    for line, _, tokens in sections.get("OUTFALLS", []):
        if not tokens:
            continue
        _require(tokens, 3, "OUTFALLS", line)
        unique(nodes, tokens[0], "OUTFALLS", line)
        invert = number(tokens, 1, "invert", lf)
        kind = tokens[2].upper()
        if kind not in ("FREE", "NORMAL", "FIXED"):
            raise CivilError("unsupported_swmm_outfall", element_id=tokens[0], outfall_type=kind)
        position = 3
        tail = None
        if kind == "FIXED":
            _require(tokens, 4, "OUTFALLS", line)
            tail = number(tokens, 3, "fixed_stage", lf) - invert
            if tail < 0:
                raise CivilError("swmm_tailwater_below_invert", element_id=tokens[0], tailwater_m=tail)
            position = 4
        nodes[tokens[0]] = {"id": tokens[0], "type": "outfall", "x": 0.0, "y": 0.0,
                            "invert": invert, "rim": None, "inflow_m3s": 0.0, "loss_k": None,
                            "outfall_tailwater": tail, "swmm_outfall_type": kind,
                            "swmm_flap_gate": tokens[position].upper() if position < len(tokens) else "NO",
                            "swmm_route_to": tokens[position + 1] if position + 1 < len(tokens) else None}
        if kind == "NORMAL":
            warnings.append(f"{tokens[0]}: SWMM NORMAL outfall is retained for export; civil screening uses max(normal, critical) control.")
    for line, _, tokens in sections.get("CONDUITS", []):
        if not tokens:
            continue
        _require(tokens, 7, "CONDUITS", line)
        unique(pipes, tokens[0], "CONDUITS", line)
        if tokens[1] not in nodes or tokens[2] not in nodes:
            raise CivilError("not_found", element_id=tokens[0], hint="Conduit references an unsupported/missing SWMM node.")
        z1, z2 = number(tokens, 5, "inlet_offset", lf), number(tokens, 6, "outlet_offset", lf)
        if offsets == "DEPTH":
            z1 += nodes[tokens[1]]["invert"]
            z2 += nodes[tokens[2]]["invert"]
        length, n = number(tokens, 3, "length", lf), number(tokens, 4, "n")
        if length <= 0 or n <= 0:
            raise CivilError("invalid_swmm_conduit", element_id=tokens[0])
        pipes[tokens[0]] = {"id": tokens[0], "from": tokens[1], "to": tokens[2], "length": length,
                            "n": n, "inlet_invert": z1, "outlet_invert": z2, "material": None,
                            "vertices": [], "swmm_initial_flow_m3s": number(tokens, 7, "initial_flow", qf, 0.0),
                            "swmm_max_flow_m3s": number(tokens, 8, "maximum_flow", qf, 0.0)}
        if pipes[tokens[0]]["swmm_max_flow_m3s"] != 0:
            warnings.append(f"{tokens[0]}: SWMM maximum flow retained but not imposed by steady gravity analysis.")
    xsections = set()
    for line, _, tokens in sections.get("XSECTIONS", []):
        if not tokens:
            continue
        _require(tokens, 3, "XSECTIONS", line)
        key, shape = tokens[0], tokens[1].upper()
        if key not in pipes:
            raise CivilError("not_found", element_id=key, section="XSECTIONS")
        if key in xsections:
            raise CivilError("duplicate_swmm_id", element_id=key, section="XSECTIONS")
        xsections.add(key)
        if shape not in ("CIRCULAR", "RECT_CLOSED", "RECT_OPEN", "TRAPEZOIDAL"):
            raise CivilError("unsupported_swmm_shape", element_id=key, shape=shape)
        barrels = number(tokens, 6, "barrels", default=1.0)
        if barrels != 1:
            raise CivilError("unsupported_swmm_barrels", element_id=key, barrels=barrels)
        if len(tokens) > 7 and number(tokens, 7, "culvert_code") != 0:
            raise CivilError("unsupported_swmm_culvert", element_id=key)
        height = number(tokens, 2, "geom1", lf)
        if height <= 0:
            raise CivilError("invalid_swmm_geometry", element_id=key)
        pipe = pipes[key]
        if shape == "CIRCULAR":
            pipe.update(shape="circular", diameter=height, width=None, height=None, side_slope=None)
        else:
            _require(tokens, 4 if shape != "TRAPEZOIDAL" else 6, "XSECTIONS", line)
            width = number(tokens, 3, "geom2", lf)
            if width < 0 or (width == 0 and shape != "TRAPEZOIDAL"):
                raise CivilError("invalid_swmm_geometry", element_id=key)
            side = None
            if shape == "TRAPEZOIDAL":
                left, right = number(tokens, 4, "left_slope"), number(tokens, 5, "right_slope")
                if left != right:
                    raise CivilError("unsupported_asymmetric_trapezoid", element_id=key)
                if left < 0 or (left == 0 and width == 0):
                    raise CivilError("invalid_swmm_geometry", element_id=key)
                side = left
            pipe.update(shape="trapezoidal" if shape == "TRAPEZOIDAL" else "rectangular",
                        diameter=None, width=width, height=height, side_slope=side,
                        closed=shape == "RECT_CLOSED")
    for key in pipes:
        if key not in xsections:
            raise CivilError("swmm_xsection_missing", element_id=key)
    for line, _, tokens in sections.get("SUBCATCHMENTS", []):
        if not tokens:
            continue
        _require(tokens, 8, "SUBCATCHMENTS", line)
        unique(catchments, tokens[0], "SUBCATCHMENTS", line)
        if tokens[2] not in nodes:
            raise CivilError("unsupported_subcatchment_routing", element_id=tokens[0], outlet=tokens[2])
        area, imperv = number(tokens, 3, "area", af), number(tokens, 4, "imperv_pct")
        width, slope, curb = number(tokens, 5, "width", lf), number(tokens, 6, "slope_pct"), number(tokens, 7, "curb_length", lf)
        if area <= 0 or not 0 <= imperv <= 100 or width < 0 or slope < 0 or curb < 0:
            raise CivilError("invalid_swmm_subcatchment", element_id=tokens[0])
        catchments[tokens[0]] = {"id": tokens[0], "name": tokens[0], "outlet": tokens[2],
                                 "area_m2": area, "imperv_pct": imperv, "cn": None, "polygon": None,
                                 "swmm_rain_gage": tokens[1], "swmm_width_m": width,
                                 "swmm_slope_pct": slope, "swmm_curb_length_m": curb,
                                 "swmm_snow_pack": tokens[8] if len(tokens) > 8 else None}
    for line, _, tokens in sections.get("SUBAREAS", []):
        if not tokens:
            continue
        _require(tokens, 7, "SUBAREAS", line)
        if tokens[0] not in catchments:
            raise CivilError("not_found", element_id=tokens[0], section="SUBAREAS")
        catchments[tokens[0]]["swmm_subareas"] = {
            "n_imperv": number(tokens, 1, "n_imperv"), "n_perv": number(tokens, 2, "n_perv"),
            "dstore_imperv_mm": number(tokens, 3, "dstore_imperv", df),
            "dstore_perv_mm": number(tokens, 4, "dstore_perv", df), "pct_zero": number(tokens, 5, "pct_zero"),
            "route_to": tokens[6].upper(), "pct_routed": number(tokens, 7, "pct_routed", default=100.0)}
    for line, raw, tokens in sections.get("INFILTRATION", []):
        if not tokens:
            continue
        if infiltration != "CURVE_NUMBER" or (len(tokens) > 4 and tokens[-1].upper() != "CURVE_NUMBER"):
            preserve("INFILTRATION", raw)
            continue
        _require(tokens, 4, "INFILTRATION", line)
        if tokens[0] not in catchments:
            raise CivilError("not_found", element_id=tokens[0], section="INFILTRATION")
        cn = number(tokens, 1, "curve_number")
        conductivity = number(tokens, 2, "conductivity", df)
        drying = number(tokens, 3, "drying_days", 86400.0)
        if not 0 < cn <= 100 or conductivity < 0 or drying < 0:
            raise CivilError("invalid_swmm_infiltration", element_id=tokens[0])
        catchments[tokens[0]].update(cn=cn, swmm_conductivity_mmh=conductivity, swmm_drying_time_s=drying)
    coordinated = set()
    for section, target, key in (("COORDINATES", nodes, None), ("VERTICES", pipes, "vertices"), ("POLYGONS", catchments, "polygon")):
        for line, _, tokens in sections.get(section, []):
            if not tokens:
                continue
            _require(tokens, 3, section, line)
            if tokens[0] not in target:
                raise CivilError("not_found", element_id=tokens[0], section=section)
            xy = [number(tokens, 1, "x", lf), number(tokens, 2, "y", lf)]
            if key is None:
                if tokens[0] in coordinated:
                    raise CivilError("duplicate_swmm_id", element_id=tokens[0], section=section)
                target[tokens[0]].update(x=xy[0], y=xy[1])
                coordinated.add(tokens[0])
            else:
                if target[tokens[0]].get(key) is None:
                    target[tokens[0]][key] = []
                target[tokens[0]][key].append(xy)
    for key in nodes.keys() - coordinated:
        warnings.append(f"{key}: no SWMM coordinate; explicit assumed plan position (0,0) m. Supply coordinates or loss_k before geometric junction analysis.")
    seen_dwf = set()
    for line, raw, tokens in sections.get("DWF", []):
        if not tokens:
            continue
        _require(tokens, 3, "DWF", line)
        if tokens[1].upper() != "FLOW":
            preserve("DWF", raw)
            continue
        if tokens[0] not in nodes:
            raise CivilError("not_found", element_id=tokens[0], section="DWF")
        if tokens[0] in seen_dwf:
            raise CivilError("duplicate_swmm_id", element_id=tokens[0], section="DWF FLOW")
        seen_dwf.add(tokens[0])
        q = number(tokens, 2, "dwf", qf)
        if q < 0:
            raise CivilError("negative_swmm_inflow", element_id=tokens[0])
        nodes[tokens[0]]["inflow_m3s"] = q
        nodes[tokens[0]]["swmm_dwf_patterns"] = tokens[3:]
        if tokens[3:]:
            warnings.append(f"{tokens[0]}: DWF patterns retained; gravity analysis uses the base flow only, not time-varying patterns.")
    if dropped:
        warnings.append("Sections/rows not represented in the civil model are preserved in dropped: " + ", ".join(sorted(dropped)))
    network = {"id": id, "name": name, "kind": "storm", "nodes": list(nodes.values()),
               "conduits": list(pipes.values()), "swmm_options": options}
    provenance = [{"source": "SWMM INP", "kind": "swmm", "units_in": units,
                   "notes": f"Flow factor={qf:.16g} to m3/s; length factor={lf} to m; area factor={af} to m2; depression/conductivity factor={df} to mm."}]
    out = _result("SWMM 5.2 INP import", network=network, catchments=list(catchments.values()), dropped=dropped, provenance=provenance)
    out["warnings"] = warnings
    out["assumptions"] = assumptions + ["SWMM junctions are civil manholes; no structure diameter is invented.",
                                         "SWMM constant DWF FLOW becomes node inflow_m3s; transient routing/options remain exchange metadata.",
                                         "SWMM FIXED stage converted to depth above outfall invert.",
                                         "FREE/NORMAL outfall metadata is retained; civil preliminary analysis uses its documented max(normal, critical, tailwater) boundary, not SWMM boundary semantics."]
    return out


def _fmt(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CivilError("invalid_input", value=value)
    return format(value, ".15g")


def _label(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\n\r;[]"'):
        raise CivilError("invalid_swmm_label", value=value)
    return '"' + value + '"' if any(c.isspace() for c in value) else value


def export_inp(network: dict, catchments: list | None = None, *, flow_units: str = "CMS", dropped: dict | None = None) -> dict:
    """Export the supported subset, with optional exact unsupported-row replay."""
    if not isinstance(flow_units, str) or flow_units.upper() not in _FLOW:
        raise CivilError("unsupported_swmm_flow_units", flow_units=flow_units)
    units = flow_units.upper()
    lf, af, df = (0.3048, 4046.8564224, 25.4) if units in _US else (1.0, 10000.0, 1.0)
    qf = _FLOW[units]
    if not isinstance(network, dict) or any(network.get(key) is not None and not isinstance(network[key], list) for key in ("nodes", "conduits")):
        raise CivilError("invalid_network")
    nodes = {n["id"]: n for n in network.get("nodes") or []}
    if len(nodes) != len(network.get("nodes") or []):
        raise CivilError("duplicate_swmm_id", section="nodes")
    sections = {section: [] for section in _SUPPORTED}
    options = dict(network.get("swmm_options", {}))
    source_units = str(options.get("FLOW_UNITS", units)).upper()
    if source_units not in _FLOW:
        raise CivilError("unsupported_swmm_flow_units", flow_units=source_units)
    source_length = 0.3048 if source_units in _US else 1.0
    for option, power in (("HEAD_TOLERANCE", 1), ("MIN_SURFAREA", 2)):
        if option in options:
            options[option] = _fmt(_numeric(options[option], option) * (source_length / lf) ** power)
    options["FLOW_UNITS"] = units
    options.setdefault("INFILTRATION", "CURVE_NUMBER")
    options.setdefault("FLOW_ROUTING", "KINWAVE")
    offset_kind = options.get("LINK_OFFSETS", "DEPTH").upper()
    if offset_kind not in ("DEPTH", "ELEVATION"):
        raise CivilError("unsupported_swmm_link_offsets", link_offsets=offset_kind)
    sections["OPTIONS"] = [f"{_label(k)} {v}" for k, v in options.items()]
    for node in network.get("nodes") or []:
        key, invert = _label(node["id"]), node["invert"]
        if node.get("type") == "outfall":
            kind = node.get("swmm_outfall_type", "FIXED" if node.get("outfall_tailwater") is not None else "FREE")
            if kind not in ("FREE", "NORMAL", "FIXED"):
                raise CivilError("unsupported_swmm_outfall", element_id=node["id"], outfall_type=kind)
            stage = ""
            if kind == "FIXED":
                tail = node.get("outfall_tailwater")
                if tail is None or tail < 0:
                    raise CivilError("invalid_outfall_tailwater", element_id=node["id"])
                stage = " " + _fmt((invert + tail) / lf)
            gate = _label(node.get("swmm_flap_gate", "NO"))
            route = " " + _label(node["swmm_route_to"]) if node.get("swmm_route_to") else ""
            sections["OUTFALLS"].append(f"{key} {_fmt(invert / lf)} {kind}{stage} {gate}{route}")
        else:
            rim = node.get("rim")
            depth = rim - invert if rim is not None else 0.0
            if depth < 0:
                raise CivilError("invalid_swmm_depth", element_id=node["id"])
            values = [invert / lf, depth / lf, node.get("swmm_initial_depth_m", 0.0) / lf,
                      node.get("swmm_surcharge_depth_m", 0.0) / lf, node.get("swmm_ponded_area_m2", 0.0) / (lf * lf)]
            sections["JUNCTIONS"].append(key + " " + " ".join(_fmt(v) for v in values))
        if node.get("x") is not None and node.get("y") is not None:
            sections["COORDINATES"].append(f"{key} {_fmt(node['x'] / lf)} {_fmt(node['y'] / lf)}")
        q = node.get("inflow_m3s", 0.0)
        if q < 0:
            raise CivilError("negative_swmm_inflow", element_id=node["id"])
        patterns = node.get("swmm_dwf_patterns", [])
        if q != 0 or patterns:
            sections["DWF"].append(f"{key} FLOW {_fmt(q / qf)}" + (" " + " ".join(_label(p) for p in patterns) if patterns else ""))
    seen = set()
    for pipe in network.get("conduits") or []:
        key = _label(pipe["id"])
        if pipe["id"] in seen:
            raise CivilError("duplicate_swmm_id", element_id=pipe["id"])
        seen.add(pipe["id"])
        if pipe.get("from") not in nodes or pipe.get("to") not in nodes:
            raise CivilError("not_found", element_id=pipe["id"])
        start, end = nodes[pipe["from"]], nodes[pipe["to"]]
        z1 = start["invert"] if pipe.get("inlet_invert") is None else pipe["inlet_invert"]
        z2 = end["invert"] if pipe.get("outlet_invert") is None else pipe["outlet_invert"]
        if offset_kind == "DEPTH":
            z1 -= start["invert"]
            z2 -= end["invert"]
        length = pipe.get("length")
        if length is None:
            xy = [[start["x"], start["y"]], *pipe.get("vertices", []), [end["x"], end["y"]]]
            length = sum(math.dist(a, b) for a, b in zip(xy, xy[1:]))
        if length <= 0 or pipe["n"] <= 0:
            raise CivilError("invalid_swmm_conduit", element_id=pipe["id"])
        values = [length / lf, pipe["n"], z1 / lf, z2 / lf,
                  pipe.get("swmm_initial_flow_m3s", 0.0) / qf, pipe.get("swmm_max_flow_m3s", 0.0) / qf]
        sections["CONDUITS"].append(f"{key} {_label(pipe['from'])} {_label(pipe['to'])} " + " ".join(_fmt(v) for v in values))
        if pipe["shape"] == "circular":
            diameter = pipe.get("diameter")
            if diameter is None or diameter <= 0:
                raise CivilError("invalid_swmm_geometry", element_id=pipe["id"])
            shape, geom = "CIRCULAR", [diameter / lf, 0, 0, 0]
        elif pipe["shape"] in ("rectangular", "trapezoidal"):
            height, width = pipe.get("height"), pipe.get("width")
            if height is None or width is None or height <= 0 or width < 0:
                raise CivilError("invalid_swmm_geometry", element_id=pipe["id"])
            if pipe["shape"] == "rectangular":
                if width == 0:
                    raise CivilError("invalid_swmm_geometry", element_id=pipe["id"])
                shape = "RECT_CLOSED" if pipe.get("closed", False) else "RECT_OPEN"
                geom = [height / lf, width / lf, 0, 0]
            else:
                side = pipe.get("side_slope")
                if side is None or side < 0 or (width == 0 and side == 0):
                    raise CivilError("invalid_swmm_geometry", element_id=pipe["id"])
                shape, geom = "TRAPEZOIDAL", [height / lf, width / lf, side, side]
        else:
            raise CivilError("unsupported_swmm_shape", element_id=pipe["id"], shape=pipe["shape"])
        sections["XSECTIONS"].append(f"{key} {shape} " + " ".join(_fmt(v) for v in geom) + " 1")
        for x, y in pipe.get("vertices", []):
            sections["VERTICES"].append(f"{key} {_fmt(x / lf)} {_fmt(y / lf)}")
    for catch in catchments or []:
        if catch.get("outlet") not in nodes:
            raise CivilError("unsupported_subcatchment_routing", element_id=catch.get("id"))
        required = ("id", "swmm_rain_gage", "swmm_width_m", "swmm_slope_pct", "swmm_curb_length_m", "swmm_subareas")
        missing = [field for field in required if catch.get(field) is None]
        if missing:
            raise CivilError("swmm_catchment_fields_missing", element_id=catch.get("id"), fields=missing)
        key = _label(catch["id"])
        values = [catch["area_m2"] / af, catch["imperv_pct"], catch["swmm_width_m"] / lf,
                  catch["swmm_slope_pct"], catch["swmm_curb_length_m"] / lf]
        snow = " " + _label(catch["swmm_snow_pack"]) if catch.get("swmm_snow_pack") else ""
        sections["SUBCATCHMENTS"].append(f"{key} {_label(catch['swmm_rain_gage'])} {_label(catch['outlet'])} " + " ".join(_fmt(v) for v in values) + snow)
        sub = catch["swmm_subareas"]
        values = [sub["n_imperv"], sub["n_perv"], sub["dstore_imperv_mm"] / df,
                  sub["dstore_perv_mm"] / df, sub["pct_zero"]]
        sections["SUBAREAS"].append(key + " " + " ".join(_fmt(v) for v in values) + f" {_label(sub['route_to'])} {_fmt(sub['pct_routed'])}")
        if catch.get("cn") is not None:
            if options["INFILTRATION"].upper() != "CURVE_NUMBER":
                raise CivilError("unsupported_swmm_infiltration", hint="CN export requires INFILTRATION CURVE_NUMBER.")
            if catch.get("swmm_conductivity_mmh") is None or catch.get("swmm_drying_time_s") is None:
                raise CivilError("swmm_catchment_fields_missing", element_id=catch["id"], fields=["swmm_conductivity_mmh", "swmm_drying_time_s"])
            values = [catch["cn"], catch["swmm_conductivity_mmh"] / df, catch["swmm_drying_time_s"] / 86400]
            sections["INFILTRATION"].append(key + " " + " ".join(_fmt(v) for v in values))
        for x, y in catch.get("polygon") or []:
            sections["POLYGONS"].append(f"{key} {_fmt(x / lf)} {_fmt(y / lf)}")
    warnings = []
    if dropped:
        if not isinstance(dropped, dict):
            raise CivilError("invalid_dropped_sections")
        original_units = network.get("swmm_options", {}).get("FLOW_UNITS", units).upper()
        dimensional_sections = set(dropped) - {"TITLE", "REPORT", "TAGS", "MAP", "BACKDROP"}
        if original_units != units and dimensional_sections:
            raise CivilError("dropped_sections_unit_conversion", "Unsupported-section quantities cannot be safely converted; export in original FLOW_UNITS or omit those sections.", sections=sorted(dimensional_sections))
        for section, raw_rows in dropped.items():
            if not isinstance(section, str) or not section or any(c in section for c in "[]\n\r") or not isinstance(raw_rows, list) or not all(isinstance(v, str) and "\n" not in v and "\r" not in v for v in raw_rows):
                raise CivilError("invalid_dropped_sections")
            sections.setdefault(section, []).extend(raw_rows)
        warnings.append("Unsupported sections/rows replayed unchanged; not interpreted or validated by civil analysis.")
    order = ["TITLE", "OPTIONS", "JUNCTIONS", "OUTFALLS", "CONDUITS", "XSECTIONS", "SUBCATCHMENTS",
             "SUBAREAS", "INFILTRATION", "DWF", "COORDINATES", "VERTICES", "POLYGONS"]
    order += sorted(set(sections) - set(order))
    text = "\n\n".join("[" + section + "]\n" + "\n".join(sections[section]) for section in order if sections.get(section)) + "\n"
    out = _result("SWMM 5.2 INP export", inp=text, provenance=[{"source": "civil model", "kind": "swmm_export", "units_in": "SI", "units_out": units,
                 "notes": f"Length divided by {lf}; flow divided by {qf:.16g}; catchment area divided by {af}."}])
    out["warnings"] = warnings
    return out

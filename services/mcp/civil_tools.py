"""Civil site, exchange, terrain, alignment and planning MCP tools.

SI metre models and full engineering results live in the shared civil session and,
when doc_id is supplied, one document civil_site study. Agent replies are compact;
detail=True opts into geometry/section arrays. Registration is deferred to the server.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.civil import alignment, crs as crs_ops, dem, epanet_inp, gis_io, ifc_export
from services.civil import landxml, planning, schema, swmm_inp, terrain
from services.civil.errors import CivilError
from services.mcp import civil_common as cc

_IMPORT_FORMATS = ("landxml", "geojson", "geopackage", "epanet", "swmm", "dem_ascii", "dem_xyz")
_EXPORT_FORMATS = ("landxml", "geojson", "geopackage", "ifc", "epanet", "swmm", "qto_csv", "cost_csv", "msproject")
_SURFACE_FIELDS = ("id", "name", "kind", "points", "triangles", "breaklines", "boundary", "source", "diagnostics")
_NETWORK_CHILDREN = {"water_networks": ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves"),
                     "gravity_networks": ("nodes", "conduits")}


def _value(value):
    return cc.as_obj(value) if isinstance(value, str) else value


def _crs_arg(value):
    if isinstance(value, str) and not value.lstrip().startswith(("{", "[")):
        return value
    return cc.as_obj(value)


def _options(value):
    obj = cc.as_obj(value) if value is not None else {}
    if not isinstance(obj, dict):
        raise CivilError("bad_argument", "options must be an object")
    return obj


def _format(value, allowed):
    if value not in allowed:
        raise CivilError("invalid_format", f"Choose one of {', '.join(allowed)}", formats=list(allowed))
    return value


def _finish(result, *, entry=None, doc_id=None, next_actions=()):
    out = dict(result)
    out.setdefault("ok", True)
    out.setdefault("warnings", [])
    out.setdefault("assumptions", [])
    out.setdefault("method", {"name": "Civil site bookkeeping", "reference": "contracts/civil_schema.md"})
    out["next_actions"] = list(next_actions)
    if entry is not None:
        out.update(cc.persist(doc_id, entry))
    return out


def _surface_summary(surface):
    points = surface.get("points") or []
    out = {k: surface[k] for k in ("id", "name", "kind") if k in surface}
    out.update(point_count=len(points), triangle_count=len(surface.get("triangles") or []),
               breakline_count=len(surface.get("breaklines") or []),
               boundary_vertex_count=len(surface.get("boundary") or []))
    if points:
        out["bounds_m"] = {"min": [min(p[k] for p in points) for k in range(3)],
                           "max": [max(p[k] for p in points) for k in range(3)]}
    return out


def _summary(entry, detail=False):
    model = entry["model"]
    out = {"site_name": model["name"], "schema": model["schema"], "crs": model["crs"],
           "criteria": model["criteria"], "counts": {key: len(model.get(key, [])) for key in schema.COLLECTIONS},
           "surfaces": [_surface_summary(s) for s in model.get("surfaces", [])],
           "alignments": [{"id": a["id"], "name": a["name"], "length_m": alignment.length(a),
                           "horizontal_element_count": len(a["horizontal"])} for a in model.get("alignments", [])],
           "validation_errors": schema.validate(model), "result_keys": sorted(entry.get("results", {})),
           "provenance_count": len(model.get("provenance", []))}
    for key, children in _NETWORK_CHILDREN.items():
        out[key] = [{"id": n["id"], "name": n.get("name"),
                     "counts": {child: len(n.get(child, [])) for child in children}} for n in model.get(key, [])]
    if "points" in model:
        out["counts"]["points"] = len(model["points"])
    if detail:
        out.update(model=model, results=entry.get("results", {}))
    return out


def _compact(result, detail=False):
    if detail:
        return dict(result)
    out = dict(result)
    if result.get("samples"):
        samples = result["samples"]
        elevations = [row.get("z_m", row.get("ground_elevation_m")) for row in samples]
        valid = [z for z in elevations if z is not None]
        out["profile_summary"] = {
            "start_station_m": samples[0]["station_m"], "end_station_m": samples[-1]["station_m"],
            "coverage_gap_count": len(elevations) - len(valid),
            "ground_elevation_range_m": [min(valid), max(valid)] if valid else None}
    for key in ("points", "triangles", "breaklines", "boundary", "samples", "sections", "mid_sections",
                "intervals", "mass_ordinates", "daylight_line", "daylight_lines", "mask", "boundary_rings"):
        if key in out:
            values = out.pop(key)
            out[key + "_count"] = len(values) if values is not None else 0
    if "contours" in out:
        rows = out.pop("contours")
        out["contour_level_count"] = len(rows)
        out["contour_polyline_count"] = sum(len(r["polylines"]) for r in rows)
        out["contour_vertex_count"] = sum(len(p) for r in rows for p in r["polylines"])
        if rows:
            out["contour_elevation_range_m"] = [rows[0]["level_m"], rows[-1]["level_m"]]
    if "surface" in out:
        out["surface"] = _surface_summary(out["surface"])
    if "diagnostics" in out:
        diagnostics = dict(out["diagnostics"])
        for key in ("long_edges", "slivers"):
            if key in diagnostics:
                diagnostics[key + "_count"] = len(diagnostics.pop(key))
        out["diagnostics"] = diagnostics
    return out


def _earthwork(entry, key, result):
    runs = dict(entry.get("results", {}).get("earthwork") or {})
    runs[key] = {k: v for k, v in result.items() if k not in ("ok", "hint", "next_actions")}
    cc.store_result(entry, "earthwork", runs)


def _merge_fragment(model, fragment, replace):
    for key in (*schema.COLLECTIONS, "points"):
        incoming = fragment.get(key, [])
        if not incoming:
            continue
        if key == "points":
            model.setdefault(key, [])
        items = model[key]
        positions = {item["id"]: i for i, item in enumerate(items)}
        for item in incoming:
            eid = item["id"]
            if eid in positions:
                if not replace:
                    raise CivilError("id_collision", "Use options.replace=True to replace an imported element", collection=key, id=eid)
                items[positions[eid]] = copy.deepcopy(item)
            else:
                positions[eid] = len(items)
                items.append(copy.deepcopy(item))


def _provenance(result, source, kind, model):
    records = result.get("provenance") or []
    if isinstance(records, dict):
        records = [records]
    records = copy.deepcopy(records)
    if not records:
        records = [{"source": str(source), "kind": kind, "units_in": "SI metres"}]
    now = datetime.now(timezone.utc).isoformat()
    for record in records:
        record.setdefault("imported_at", now)
        record.setdefault("crs", model["crs"])
        record.setdefault("notes", "Imported through civil_import")
    return records


def _gis_seed(entry):
    seed = schema.new_model(entry["model"]["name"], entry["model"]["crs"])
    for key, children in _NETWORK_CHILDREN.items():
        for network in entry["model"][key]:
            envelope = copy.deepcopy(network)
            for child in children:
                envelope[child] = []
            seed[key].append(envelope)
    return seed


def _read_import(source, fmt, options, entry):
    if fmt == "landxml":
        data = source.encode("utf-8") if isinstance(source, str) and source.lstrip().startswith("<") else source
        result = landxml.read(data)
        return result, result["model_fragment"]
    if fmt in ("geojson", "geopackage"):
        seed = _gis_seed(entry) if entry is not None and options.get("field_mapping") is not None else None
        if fmt == "geojson":
            data = cc.as_obj(source)
            target_crs = entry["model"]["crs"] if entry and entry["model"]["crs"]["horizontal"] is not None else None
            result = gis_io.import_geojson(data, seed, field_mapping=options.get("field_mapping"),
                                          crs=options.get("crs", target_crs))
        else:
            result = gis_io.import_geopackage(source, seed, field_mapping=options.get("field_mapping"))
        fragment = copy.deepcopy(result["model"])
        if seed is not None:
            for key, children in _NETWORK_CHILDREN.items():
                fragment[key] = [n for n in fragment[key] if any(n.get(child) for child in children)]
        return result, fragment
    if fmt == "epanet":
        result = epanet_inp.read(source)
        network = result["network"]
        if options.get("network_id") is not None:
            network["id"] = options["network_id"]
        return result, {"water_networks": [network]}
    if fmt == "swmm":
        text = Path(source).read_text(encoding="utf-8") if isinstance(source, str) and "\n" not in source and Path(source).is_file() else source
        result = swmm_inp.import_inp(text, id=options.get("network_id", "swmm"), name=options.get("network_name", "SWMM import"))
        result["network"]["swmm_dropped"] = copy.deepcopy(result["dropped"])
        return result, {"gravity_networks": [result["network"]], "catchments": result["catchments"]}
    grid = dem.read_ascii_grid(source) if fmt == "dem_ascii" else dem.read_xyz(source, options.get("cell_size"))
    sampled = dem.to_tin_points(grid, stride=options.get("stride", 1))
    sid = options.get("surface_id", Path(source).stem)
    result = terrain.build_tin(sampled["points"], id=sid, name=options.get("surface_name", sid))
    result["source"] = {"kind": "dem", "ref": str(source), "dem": grid}
    result["warnings"] = grid["warnings"] + sampled["warnings"] + result["warnings"]
    result["assumptions"] = grid["assumptions"] + sampled["assumptions"] + result["assumptions"]
    return result, {"surfaces": [{k: result[k] for k in _SURFACE_FIELDS if k in result}]}


def register_civil_tools(mcp):
    @mcp.tool()
    def civil_site_new(name: str, crs: Any = None, criteria: Any = None, doc_id: Optional[str] = None) -> str:
        """Create and activate an editable civil site, optionally persisted as civil_site.
        UNITS: SI m, m², m³, m³/s, kPa; CRS horizontal axes must be projected metres.
        Returns: site counts, CRS, criteria, validation errors and persistence status.
        Refuses invalid_name, invalid CRS or criteria; does not invent survey coordinates.
        next_actions: civil_import or civil_site_edit, then civil_surface_build.
        """
        def body():
            doc = cc.target_doc(doc_id)
            model = schema.new_model(name, _crs_arg(crs))
            if criteria is not None:
                model["criteria"] = cc.as_obj(criteria)
            errors = schema.validate(model)
            if errors:
                raise CivilError("invalid_site", errors=errors)
            entry = cc.remember(model)
            return _finish(_summary(entry), entry=entry, doc_id=doc, next_actions=("civil_import", "civil_site_edit"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_load(doc_id: str, site: Optional[str] = None, detail: bool = False) -> str:
        """Restore a document civil_site model and all stored engineering results.
        UNITS: SI m, m³/s, kPa. Read-only; activates the restored session site.
        Returns: collection counts/result keys; detail=True includes the full model/results.
        Refuses unknown_doc or no_civil_site (missing name or ambiguous studies).
        next_actions: civil_site_summary, then resume analysis or editing.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.load_from_doc(doc, site)
            return _finish({**_summary(entry, detail), "doc_id": doc}, next_actions=("civil_site_summary",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_site_edit(ops: Any, site: Any = None, doc_id: Optional[str] = None) -> str:
        """Apply atomic upsert/remove, set_crs/set_criteria, or rename operations.
        UNITS: SI m, m³/s, kPa; ops=[{op, collection, element|id}, ...].
        Model changes invalidate ALL stale results and persist the updated civil_site.
        Returns: updated counts, validation errors and cleared result keys.
        Refuses no_civil_site, unknown_site, unknown_operation or invalid_site; no partial edits.
        next_actions: rerun affected analyses, civil_quantities and civil_cost_estimate.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            model = cc.model_copy(entry)
            operations = cc.as_obj(ops)
            if not isinstance(operations, list):
                raise CivilError("bad_argument", "ops must be an array")
            old_keys = sorted(entry.get("results", {}))
            for op in operations:
                if not isinstance(op, dict):
                    raise CivilError("bad_argument", "each operation must be an object")
                kind = op.get("op")
                if kind == "upsert":
                    schema.upsert(model, op.get("collection"), op.get("element"))
                elif kind == "remove":
                    schema.remove(model, op.get("collection"), op.get("id"))
                elif kind == "set_crs":
                    model["crs"] = crs_ops.normalize(op.get("crs"))
                elif kind == "set_criteria":
                    model["criteria"] = copy.deepcopy(op.get("criteria"))
                elif kind == "rename":
                    model["name"] = op.get("name")
                else:
                    raise CivilError("unknown_operation", op=kind)
            changed = model != entry["model"]
            cc.set_model(entry, model)
            return _finish({**_summary(entry), "cleared_result_keys": old_keys if changed else []},
                           entry=entry, doc_id=doc, next_actions=("civil_quantities",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_site_summary(site: Any = None, doc_id: Optional[str] = None, detail: bool = False) -> str:
        """Summarize site geometry, network counts, CRS, validation and stored results.
        UNITS: SI m, m³/s, kPa. No model mutation; doc_id also persists the current site.
        Returns: counts and result keys; detail=True returns full model/results arrays.
        Refuses no_civil_site or unknown_site; use civil_load for a saved study by name.
        next_actions: address validation errors, choose an analysis by element id.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            return _finish(_summary(entry, detail), entry=entry, doc_id=doc, next_actions=("civil_surface_analyze", "civil_quantities"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_import(source: Any, format: str, site: Any = None, merge: bool = True,
                     options: Any = None, doc_id: Optional[str] = None) -> str:
        """Import LandXML, GeoJSON/GeoPackage, EPANET/SWMM, or dem_ascii/dem_xyz.
        UNITS: converts foreign coordinates/flows to SI m, m³/s; records provenance.
        source is a path (LandXML/INP may be text; GeoJSON may be an object/JSON).
        options: replace, name, crs, field_mapping, network_id; DEM surface_id/stride/cell_size.
        Merges into active/target site; merge=False replaces it. Model edits clear results.
        Returns: counts, engine dropped fields/rows, provenance and persistence status.
        Refuses invalid_format, id_collision unless replace=True, or invalid/incomplete model.
        next_actions: civil_site_summary, then solve/analyze the imported elements.
        """
        def body():
            fmt = _format(format, _IMPORT_FORMATS)
            opts = _options(options)
            doc = cc.target_doc(doc_id)
            entry = None
            if site is not None:
                entry = cc.resolve_site(site, doc)
            else:
                try:
                    entry = cc.resolve_site(None, doc)
                except CivilError as exc:
                    if exc.reason != "no_civil_site":
                        raise
            result, fragment = _read_import(source, fmt, opts, entry)
            if merge and entry is not None:
                model = cc.model_copy(entry)
            else:
                name = opts.get("name", entry["model"]["name"] if entry else fragment.get("name", "Civil import"))
                record = opts.get("crs", fragment.get("crs", entry["model"]["crs"] if entry else None))
                model = schema.new_model(name, record)
                if entry is not None:
                    model["criteria"] = copy.deepcopy(entry["model"]["criteria"])
                elif fragment.get("criteria") is not None:
                    model["criteria"] = copy.deepcopy(fragment["criteria"])
            if fragment.get("crs") is not None:
                incoming_crs = crs_ops.normalize(fragment["crs"])
                if model["crs"]["horizontal"] is None and not any(model.get(key) for key in (*schema.COLLECTIONS, "points")):
                    model["crs"] = incoming_crs
                elif any(model["crs"][key] != incoming_crs[key] for key in ("horizontal", "vertical", "local")):
                    raise CivilError("crs_mismatch", "Imported geometry and target site must use the same project CRS/local grid; transform before merging",
                                     source_crs=incoming_crs, target_crs=model["crs"])
            _merge_fragment(model, fragment, opts.get("replace") is True)
            provenance = _provenance(result, source if isinstance(source, str) and len(source) < 1024 else fmt, fmt, model)
            model["provenance"].extend(provenance)
            errors = schema.validate(model)
            if errors:
                raise CivilError("invalid_site", "Imported model is incomplete or inconsistent", errors=errors,
                                 dropped=result.get("dropped", result.get("fields_dropped", [])))
            entry = cc.set_model(entry, model) if entry else cc.remember(model)
            metadata = {k: result[k] for k in ("warnings", "assumptions", "method") if k in result}
            if fmt == "epanet":
                metadata.update(warnings=result["provenance"].get("warnings", []),
                                assumptions=result["provenance"].get("assumptions", []),
                                method={"name": "EPANET INP import", "reference": result["provenance"]["reference"]})
            return _finish({**_summary(entry), **metadata, "format": fmt, "provenance": provenance,
                            "dropped": result.get("dropped", result.get("fields_dropped", []))},
                           entry=entry, doc_id=doc, next_actions=("civil_site_summary",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_export(format: str, path: str, site: Any = None, network_id: Optional[str] = None,
                     options: Any = None) -> str:
        """Export landxml, geojson, geopackage, ifc, epanet, swmm, qto_csv, cost_csv or msproject.
        UNITS: site SI m/m³/s; adapters report foreign units and dropped information.
        EPANET/SWMM require network_id selecting exactly one network; SWMM includes its catchments.
        options: spacing_m, LandXML include, SWMM flow_units/dropped, MSP start_date.
        Read-only model/results; writes the requested path, not a new study.
        Returns: path, counts and adapter warnings/dropped rows; never large file payloads.
        Refuses invalid_format, no_civil_site, missing network_id or no_qto/no_cost/no_schedule.
        next_actions: inspect the exported file and resolve any reported omissions.
        """
        def body():
            fmt = _format(format, _EXPORT_FORMATS)
            opts = _options(options)
            entry = cc.resolve_site(site)
            model = entry["model"]
            if fmt == "landxml":
                kwargs = {"include": opts["include"]} if "include" in opts else {}
                result = landxml.write(model, path, **kwargs)
            elif fmt == "geojson":
                result = gis_io.export_geojson(model, spacing_m=opts.get("spacing_m", 10.0))
                payload = result.pop("geojson")
                Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                result["feature_count"] = len(payload["features"])
            elif fmt == "geopackage":
                result = gis_io.export_geopackage(model, path, spacing_m=opts.get("spacing_m", 10.0))
            elif fmt == "ifc":
                result = ifc_export.write_ifc(model, path)
            elif fmt in ("epanet", "swmm"):
                if network_id is None:
                    raise CivilError("network_id_required", "Select exactly one network for INP export")
                collection = "water_networks" if fmt == "epanet" else "gravity_networks"
                network = schema.find(model, collection, network_id)
                if fmt == "epanet":
                    result = epanet_inp.write(network, path)
                else:
                    nodes = {n["id"] for n in network["nodes"]}
                    catchments = [c for c in model["catchments"] if c["outlet"] in nodes]
                    result = swmm_inp.export_inp(network, catchments, flow_units=opts.get("flow_units", network.get("swmm_options", {}).get("FLOW_UNITS", "CMS")),
                                                dropped=opts.get("dropped", network.get("swmm_dropped")))
                    Path(path).write_text(result.pop("inp"), encoding="utf-8")
                result["network_id"] = network_id
            else:
                key = {"qto_csv": "qto", "cost_csv": "cost", "msproject": "schedule"}[fmt]
                stored = entry.get("results", {}).get(key)
                if stored is None:
                    raise CivilError("no_" + key, f"Run civil_{'quantities' if key == 'qto' else 'cost_estimate' if key == 'cost' else 'schedule'} first")
                if fmt == "msproject":
                    start = opts.get("start_date", stored.get("start_date"))
                    if start is None:
                        raise CivilError("start_date_required", "Supply options.start_date or civil_schedule(start_date=...)")
                    text = planning.export_msproject_xml(stored, start)
                else:
                    text = planning.export_csv(stored)
                Path(path).write_text(text, encoding="utf-8")
                result = {"item_count": len(stored.get("items", []))} if fmt != "msproject" else {"task_count": len(stored["tasks"])}
            if "fields_dropped" in result:
                result["dropped"] = result.pop("fields_dropped")
            return _finish({**result, "path": str(path), "format": fmt}, next_actions=("Review export warnings and dropped information",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_crs_transform(points: Any, src: Any, dst: Any, scale_factors: bool = False) -> str:
        """Transform XY/XYZ point arrays between CRS codes or civil CRS/local-grid records.
        UNITS: projected m, geographic degrees; XYZ elevation m is unchanged (2D only).
        scale_factors=True reports destination grid/elevation factors (ellipsoidal height).
        Returns: transformed points, operation provenance and optional factors; no persistence.
        Refuses invalid CRS/coordinates; scale factors require projected metre destination.
        next_actions: verify survey datum before importing transformed geometry.
        """
        def body():
            rows = cc.as_obj(points)
            if not isinstance(rows, list) or not rows:
                raise CivilError("bad_argument", "points must be a nonempty array of XY or XYZ")
            source, destination = _crs_arg(src), _crs_arg(dst)
            transformed, factors, provenance = [], [], None
            for point in rows:
                if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
                    raise CivilError("bad_argument", "each point must have two or three coordinates")
                if len(point) == 3 and (isinstance(point[2], bool) or not isinstance(point[2], (int, float)) or not math.isfinite(point[2])):
                    raise CivilError("invalid_elevation", "XYZ elevation must be a finite SI metre value")
                result = crs_ops.transform(point[:2], source, destination)
                output = result["xy"] + list(point[2:])
                transformed.append(output)
                if provenance is None:
                    provenance = result["provenance"]
                if scale_factors:
                    factors.append(crs_ops.scale_factors(destination, *output[:2], elevation_m=output[2] if len(output) == 3 else 0.0))
            return _finish({"points": transformed, "point_count": len(transformed), "provenance": provenance,
                            **({"scale_factors": factors} if scale_factors else {}),
                            "warnings": result["warnings"], "assumptions": result["assumptions"], "method": result["method"]},
                           next_actions=("Confirm horizontal and vertical survey datums",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_surface_build(surface_id: str, name: Optional[str] = None, points: Any = None,
                            breaklines: Any = None, boundary: Any = None, dem_path: Optional[str] = None,
                            dem_stride: int = 1, site: Any = None, doc_id: Optional[str] = None,
                            detail: bool = False) -> str:
        """Build/update a constrained TIN from XYZ points or an ESRI ASCII DEM path.
        UNITS: SI m for points, breaklines, boundary and raster coordinates/elevations.
        Supply points OR dem_path; dem_stride subsamples valid cell centres.
        Stores full TIN and source DEM in the model, invalidates old results, persists civil_site.
        Returns: geometry counts/bounds/diagnostics; detail=True returns the full TIN.
        Refuses no_civil_site, ambiguous_surface_source or engine geometry/grid errors.
        next_actions: civil_surface_analyze or civil_grade_pad, then civil_quantities.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            if (points is None) == (dem_path is None):
                raise CivilError("ambiguous_surface_source", "Supply exactly one of points or dem_path")
            grid = dem.read_ascii_grid(dem_path) if dem_path is not None else None
            pts = dem.to_tin_points(grid, stride=dem_stride)["points"] if grid is not None else cc.as_obj(points)
            result = terrain.build_tin(pts, cc.as_obj(breaklines), cc.as_obj(boundary), id=surface_id, name=name or surface_id)
            if grid is not None:
                result["source"] = {"kind": "dem", "ref": str(dem_path), "dem": grid}
                result["warnings"] = grid["warnings"] + result["warnings"]
                result["assumptions"] = grid["assumptions"] + result["assumptions"]
            surface = {key: result[key] for key in _SURFACE_FIELDS if key in result}
            model = cc.model_copy(entry)
            schema.upsert(model, "surfaces", surface)
            cc.set_model(entry, model)
            compact = dict(result) if detail else {**_surface_summary(surface), **_compact({k: v for k, v in result.items() if k not in _SURFACE_FIELDS or k == "diagnostics"})}
            return _finish(compact, entry=entry, doc_id=doc, next_actions=("civil_surface_analyze", "civil_grade_pad"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_surface_analyze(kind: str, surface_id: str, other_surface_id: Optional[str] = None,
                              interval: Optional[float] = None, base: float = 0.0, polyline: Any = None,
                              step: float = 10.0, elevation: Optional[float] = None, polygon: Any = None,
                              boundary: Any = None, stages: Any = None, site: Any = None,
                              doc_id: Optional[str] = None, detail: bool = False) -> str:
        """Analyze contours, slope, profile, volume, volume_to_elevation or stage_storage.
        UNITS: SI m; volumes m³, slopes %, contour interval/base/elevation/step in m.
        volume: selected surface is top, other_surface_id bottom; positive difference is fill.
        stage_storage needs polygon+stages; profile needs polyline; contours needs interval.
        Volume runs persist full results under earthwork; read-only arrays recompute from TIN.
        Returns: engineering summary/counts; detail=True includes contour/facet/profile arrays.
        Refuses missing/unknown site/surface, unknown_analysis or invalid engineering inputs.
        next_actions: civil_quantities for volumes; civil_detention_route for stage-storage.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            surface = schema.find(entry["model"], "surfaces", surface_id)
            if kind == "contours":
                result = terrain.contours(surface, interval, base)
            elif kind == "slope":
                result = terrain.slope_aspect(surface)
            elif kind == "profile":
                result = terrain.profile_along(surface, cc.as_obj(polyline), step)
            elif kind == "volume":
                other = schema.find(entry["model"], "surfaces", other_surface_id)
                result = terrain.volume_between(surface, other, cc.as_obj(boundary))
            elif kind == "volume_to_elevation":
                result = terrain.volume_to_elevation(surface, elevation, cc.as_obj(boundary))
            elif kind == "stage_storage":
                result = terrain.stage_storage(surface, cc.as_obj(polygon), cc.as_obj(stages))
                result["surface_ids"] = [surface_id]
            else:
                raise CivilError("unknown_analysis", kind=kind)
            if kind in ("volume", "volume_to_elevation", "stage_storage"):
                _earthwork(entry, kind + ":" + ":".join([surface_id] + ([other_surface_id] if other_surface_id else [])), result)
            return _finish({**_compact(result, detail), "kind": kind, "surface_id": surface_id},
                           entry=entry, doc_id=doc, next_actions=("civil_quantities",) if kind.startswith("volume") else ("civil_surface_analyze",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_grade_pad(surface_id: str, pad_polygon: Any, pad_elevation: float, cut_slope: float,
                        fill_slope: float, graded_surface_id: Optional[str] = None, site: Any = None,
                        doc_id: Optional[str] = None, detail: bool = False) -> str:
        """Grade a pad to existing terrain with cut/fill daylight slopes and exact volumes.
        UNITS: SI m, m³; cut_slope/fill_slope are positive horizontal:vertical ratios.
        Persists full graded TIN in model and new earthwork; invalidates old model results.
        Returns: surface/daylight counts and cut/fill volumes; detail=True includes geometry.
        Refuses missing site/surface, incomplete survey coverage or invalid/crossed slopes.
        next_actions: civil_quantities then civil_cost_estimate for the grading earthwork.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            existing = schema.find(entry["model"], "surfaces", surface_id)
            result = terrain.grade_pad(existing, cc.as_obj(pad_polygon), pad_elevation, cut_slope, fill_slope)
            result["surface"]["id"] = graded_surface_id or result["surface"]["id"]
            if result["surface"]["id"] == surface_id:
                raise CivilError("id_collision", "The graded surface must have a distinct id", id=surface_id)
            result["volumes"]["surface_ids"] = [result["surface"]["id"], surface_id]
            model = cc.model_copy(entry)
            schema.upsert(model, "surfaces", result["surface"])
            cc.set_model(entry, model)
            _earthwork(entry, "grade_pad:" + result["surface"]["id"], result)
            return _finish(_compact(result, detail), entry=entry, doc_id=doc, next_actions=("civil_quantities", "civil_cost_estimate"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_watershed(dem_path: str, outlet_xy: Any, catchment_id: Optional[str] = None,
                        outlet_node: Optional[str] = None, site: Any = None, doc_id: Optional[str] = None,
                        detail: bool = False) -> str:
        """Delineate an ASCII DEM watershed and create/update its routed site catchment.
        UNITS: SI m/m²; outlet_xy is projected XY. Uses depression fill then D8 routing.
        New catchments require outlet_node; updates preserve runoff/CN/Tc parameters.
        Stores exact catchment polygon/area and import provenance; invalidates old results.
        Returns: catchment id, upstream cells/area and geometry counts; detail=True has boundary.
        Refuses invalid outlet, missing gravity node, or holes/multipart unsupported by catchment ring.
        next_actions: civil_site_edit to specify runoff/CN/Tc, then civil_hydrology.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            sid = catchment_id or Path(dem_path).stem + "_watershed"
            old = next((c for c in entry["model"]["catchments"] if c["id"] == sid), None)
            outlet = outlet_node if outlet_node is not None else old.get("outlet") if old else None
            if outlet is None:
                raise CivilError("outlet_node_required", "Catchments must route to an existing gravity node")
            if not any(n["id"] == outlet for net in entry["model"]["gravity_networks"] for n in net["nodes"]):
                raise CivilError("not_found", "Catchment outlet must be a gravity node", id=outlet)
            grid = dem.read_ascii_grid(dem_path)
            filled = dem.fill_depressions(grid)
            directions = dem.flow_direction_d8(filled)
            result = dem.watershed(directions, cc.as_obj(outlet_xy))
            geometry = result["boundary"]
            if geometry["type"] != "Polygon" or len(geometry["coordinates"]) != 1:
                raise CivilError("unsupported_watershed_geometry", "Catchment polygon supports one exterior ring without holes",
                                 geometry_type=geometry["type"], boundary_ring_count=len(result["boundary_rings"]))
            catchment = copy.deepcopy(old) if old else {"id": sid, "name": sid, "runoff_coeff": None, "cn": None,
                                                       "imperv_pct": None, "tc_s": None, "tc_segments": []}
            catchment.update(outlet=outlet, area_m2=result["area_m2"], polygon=geometry["coordinates"][0][:-1])
            model = cc.model_copy(entry)
            schema.upsert(model, "catchments", catchment)
            model["provenance"].extend(_provenance({}, dem_path, "watershed", model))
            cc.set_model(entry, model)
            result["warnings"] = grid["warnings"] + filled["warnings"] + directions["warnings"] + result["warnings"]
            result["assumptions"] = grid["assumptions"] + filled["assumptions"] + directions["assumptions"] + result["assumptions"]
            if not detail:
                result = {k: v for k, v in result.items() if k not in ("mask", "boundary", "boundary_rings")}
                result["polygon_vertex_count"] = len(catchment["polygon"])
            return _finish({**result, "catchment_id": sid, "outlet_node": outlet}, entry=entry, doc_id=doc,
                           next_actions=("civil_site_edit", "civil_hydrology"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_alignment(alignment_id: str, name: Optional[str] = None, pis: Any = None, radii: Any = None,
                        spiral_lengths: Any = None, horizontal: Any = None, vertical: Any = None,
                        start_station: float = 0.0, site: Any = None, doc_id: Optional[str] = None,
                        detail: bool = False) -> str:
        """Create/update a highway alignment from PIs/radii/spirals or explicit horizontal segments.
        UNITS: SI m for stations/elevations/radii; segment headings radians; vertical={pvis:[...]}.
        Supply pis OR horizontal. Persists complete editable geometry and clears stale results.
        Returns: length/station limits and element counts; detail=True includes alignment geometry.
        Refuses missing site, ambiguous alignment source or invalid/noncontiguous geometry.
        next_actions: civil_alignment_analyze for profile, sight_distance, sections or earthwork.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            if (pis is None) == (horizontal is None):
                raise CivilError("ambiguous_alignment_source", "Supply exactly one of pis or horizontal")
            if pis is not None:
                built = alignment.from_pis(cc.as_obj(pis), _value(radii), _value(spiral_lengths), id=alignment_id,
                                           name=name or alignment_id, start_station=start_station)
                al = alignment.build(built["horizontal"], cc.as_obj(vertical), id=alignment_id, name=name or alignment_id,
                                     start_station=start_station)
            else:
                al = alignment.build(cc.as_obj(horizontal), cc.as_obj(vertical), id=alignment_id,
                                     name=name or alignment_id, start_station=start_station)
            model = cc.model_copy(entry)
            schema.upsert(model, "alignments", al)
            cc.set_model(entry, model)
            length = alignment.length(al)
            result = {"alignment_id": alignment_id, "name": al["name"], "length_m": length,
                      "start_station_m": start_station, "end_station_m": start_station + length,
                      "horizontal_element_count": len(al["horizontal"]),
                      "vertical_pvi_count": len((al["vertical"] or {}).get("pvis", []))}
            if detail:
                result["alignment"] = al
            return _finish(result, entry=entry, doc_id=doc, next_actions=("civil_alignment_analyze",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_alignment_analyze(kind: str, alignment_id: str, surface_id: Optional[str] = None,
                                template: Any = None, stations: Any = None, step: float = 10.0,
                                design_speed_kmh: Optional[float] = None, method: str = "average_end_area",
                                shrink: float = 1.0, swell: float = 1.0, site: Any = None,
                                doc_id: Optional[str] = None, detail: bool = False) -> str:
        """Analyze alignment profile/design_vs_existing, sight_distance, sections or earthwork.
        UNITS: SI m, m², m³; speed km/h, cross-slope %, cut/fill slopes H:V.
        Ground analyses require surface_id; sections/earthwork also need a template.
        stations defaults to step sampling. Earthwork method=average_end_area|prismoidal.
        Sections and earthwork persist full arrays under earthwork; other analyses recompute.
        Returns: summary/counts and criterion checks; detail=True includes profiles/sections.
        Refuses missing site/elements, missing vertical design, ground gaps or unknown_analysis.
        next_actions: address sight-distance failures or civil_quantities/civil_cost_estimate.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            al = schema.find(entry["model"], "alignments", alignment_id)
            if kind == "sight_distance":
                result = alignment.vertical_curve_checks(al, design_speed_kmh, entry["model"]["criteria"])
            elif kind in ("profile", "design_vs_existing", "sections", "earthwork"):
                if surface_id is None:
                    raise CivilError("surface_id_required", "Ground analysis requires an existing TIN")
                sampler = terrain.surface_sampler(schema.find(entry["model"], "surfaces", surface_id))
                if kind == "profile":
                    result = alignment.existing_ground_profile(al, sampler, step)
                elif kind == "design_vs_existing":
                    result = alignment.design_vs_existing(al, sampler, step)
                else:
                    positions = cc.as_obj(stations) if stations is not None else alignment.densify(al, step)["stations_m"]
                    sections = alignment.cross_sections(al, sampler, cc.as_obj(template), positions)
                    result = sections if kind == "sections" else alignment.earthwork(sections, method, shrink, swell)
                    if kind == "earthwork":
                        result.update(sections=sections["sections"], mid_sections=sections["mid_sections"])
                    result["surface_ids"] = [surface_id]
                    _earthwork(entry, kind + ":" + alignment_id + ":" + surface_id, result)
            else:
                raise CivilError("unknown_analysis", kind=kind)
            return _finish({**_compact(result, detail), "kind": kind, "alignment_id": alignment_id},
                           entry=entry, doc_id=doc, next_actions=("civil_quantities",) if kind == "earthwork" else ("civil_alignment_analyze",))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_quantities(trench: Any = None, surface_id: Optional[str] = None, site: Any = None,
                         doc_id: Optional[str] = None) -> str:
        """Measure pipe/structure/trench quantities and stored surface/alignment earthwork.
        UNITS: SI m, m², m³; trench requires bedding_depth_m/sample_step_m and width/clearance+OD.
        surface_id samples ground; otherwise trench uses explicit gravity-node rim elevations.
        Persists qto with traced element ids; maps alignment total_cut/total_fill to cut/fill.
        Returns: measured items, units/basis and warnings; quantities are not priced purchases.
        Refuses missing site, missing trench geometry/OD/elevations or incomplete ground coverage.
        next_actions: civil_cost_estimate with your sourced, dated price_book.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            ground = terrain.surface_sampler(schema.find(entry["model"], "surfaces", surface_id)) if surface_id else None
            volumes = []
            for run in (entry.get("results", {}).get("earthwork") or {}).values():
                if not isinstance(run, dict):
                    continue
                volume = run.get("volumes", run)
                if "total_cut_m3" in volume:
                    volume = {**volume, "cut_m3": volume["total_cut_m3"], "fill_m3": volume["total_fill_m3"]}
                if "cut_m3" in volume and "fill_m3" in volume:
                    volumes.append(volume)
            result = planning.quantity_takeoff(entry["model"], ground=ground, trench=cc.as_obj(trench), surfaces_volume=volumes)
            cc.store_result(entry, "qto", result)
            return _finish(result, entry=entry, doc_id=doc, next_actions=("civil_cost_estimate", "civil_export"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_cost_estimate(price_book: Any, contingency_pct: float = 0.0, escalation: Any = None,
                            site: Any = None, doc_id: Optional[str] = None) -> str:
        """Price stored qto with sourced/dated user unit rates, escalation and contingency.
        UNITS: quantity SI m/m²/m³/each; price_book entries unit/unit_price/currency/source/date.
        Currency totals remain separate; unpriced items are listed, never assigned zero prices.
        Persists cost against current qto/model; does not invent rates or currency conversions.
        Returns: priced lines, unpriced quantities, currency totals and completeness.
        Refuses no_civil_site, no_qto, invalid_price_book, ambiguous matches or unit_mismatch.
        next_actions: supply missing prices or civil_export(format='cost_csv', path=...).
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            qto = entry.get("results", {}).get("qto")
            if qto is None:
                raise CivilError("no_qto", "Run civil_quantities for this site first")
            result = planning.cost_estimate(qto, cc.as_obj(price_book), contingency_pct=contingency_pct,
                                            escalation=cc.as_obj(escalation))
            cc.store_result(entry, "cost", result)
            return _finish(result, entry=entry, doc_id=doc, next_actions=("Price the listed unpriced quantities", "civil_export"))
        return cc.run_tool(body)

    @mcp.tool()
    def civil_schedule(tasks: Any, start_date: Optional[str] = None, day: Optional[float] = None,
                       site: Any = None, doc_id: Optional[str] = None) -> str:
        """Compute CPM construction schedule and link tasks to model elements for 4D.
        UNITS: continuous workdays; tasks duration_d and predecessors={id,type:FS|SS|FF|SF,lag_d}.
        Optional start_date is ISO YYYY-MM-DD (MS Project export); day adds planned state.
        Persists schedule with critical path, 4D assignments and optional snapshot.
        Returns: early/late dates, float, driving chain, install windows and unassigned warnings.
        Refuses missing site, cyclic dependencies, unknown element/task ids or invalid inputs.
        next_actions: civil_export(format='msproject', path=...) or revise task dependencies.
        """
        def body():
            doc = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, doc)
            result = planning.schedule_cpm(cc.as_obj(tasks))
            linked = planning.link_4d(result, entry["model"])
            result["links_4d"] = linked
            result["warnings"] = result["warnings"] + linked["warnings"]
            if start_date is not None:
                planning.export_msproject_xml(result, start_date)
                result["start_date"] = start_date
            if day is not None:
                result["state"] = planning.state_at(result, day)
            cc.store_result(entry, "schedule", result)
            return _finish(result, entry=entry, doc_id=doc, next_actions=("civil_export",))
        return cc.run_tool(body)

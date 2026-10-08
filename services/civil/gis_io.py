"""GeoJSON RFC 7946 and GeoPackage OGC 1.3 vector exchange (stdlib SQLite).

Feature properties retain civil attributes; a civil_site foreign member/table
retains collection/network envelopes for lossless own-format round trips. External
GIS imports require explicit field mappings and report unmapped properties.
Geometry support is 2D Point/LineString/single-ring Polygon; multipart geometries,
polygon holes, antimeridian crossing and vertical transforms are refused.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import struct

from . import crs as crs_ops, schema
from .errors import CivilError

GEOJSON_REFERENCE = "RFC 7946 §§3.1,3.1.6,4 (WGS84 longitude, latitude)"
GPKG_REFERENCE = "OGC GeoPackage 1.3, 12-128r18 §§2.1–2.2 (GeoPackageBinary and features)"


def _result(**payload):
    return {"ok": True, "warnings": [], "assumptions": [], "fields_dropped": [], **payload}


def _valid_geometry(geometry):
    if not isinstance(geometry, dict):
        raise CivilError("invalid_gis_geometry")
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        groups = [[coordinates]]
    elif kind == "LineString":
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise CivilError("invalid_gis_geometry")
        groups = [coordinates]
    elif kind == "Polygon":
        if not isinstance(coordinates, list) or len(coordinates) != 1:
            raise CivilError("unsupported_polygon_holes")
        ring = coordinates[0]
        if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
            raise CivilError("invalid_gis_geometry", "Polygon ring must be closed with ≥4 positions")
        groups = [ring]
    else:
        raise CivilError("unsupported_gis_geometry", geometry_type=kind)
    for group in groups:
        for point in group:
            if not isinstance(point, (list, tuple)) or len(point) != 2 or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in point):
                raise CivilError("invalid_gis_geometry", "Only finite 2D geometry is supported")
    return kind, coordinates


def _ring(points):
    points = [list(p[:2]) for p in points]
    if len(points) < 3:
        raise CivilError("invalid_gis_geometry")
    if points[0] != points[-1]:
        points.append(points[0][:])
    return points


def _map_geometry(geometry, convert, *, geographic=False):
    kind, coords = _valid_geometry(geometry)
    if kind == "Point":
        transformed = convert(coords)
    elif kind == "LineString":
        transformed = [convert(p) for p in coords]
    else:
        transformed = [[convert(p) for p in coords[0]]]
    if geographic and kind in ("LineString", "Polygon"):
        points = transformed if kind == "LineString" else transformed[0]
        if any(abs(a[0]-b[0]) > 180 for a, b in zip(points, points[1:])):
            raise CivilError("unsupported_antimeridian", "Split the geometry at the antimeridian before RFC 7946 export")
        if kind == "Polygon":
            signed = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(points, points[1:]))
            if signed < 0:
                transformed[0] = list(reversed(points))
    return {"type": kind, "coordinates": transformed}


def _site_metadata(model):
    site = deepcopy(model)
    for key in schema.COLLECTIONS:
        site[key] = []
    for key in ("water_networks", "gravity_networks"):
        nested = ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves") if key == "water_networks" else ("nodes", "conduits")
        for network in model.get(key, []):
            envelope = deepcopy(network)
            for collection in nested:
                envelope[collection] = []
            site[key].append(envelope)
    return site


def _features(model, spacing_m):
    if not isinstance(spacing_m, (int, float)) or isinstance(spacing_m, bool) or not math.isfinite(spacing_m) or spacing_m <= 0:
        raise CivilError("invalid_spacing")
    features, dropped = [], []
    def add(element, collection, geometry, network_id=None, element_collection=None):
        properties = {"id": element["id"], "type": element_collection or collection,
                      "collection": collection, "attributes": deepcopy(element)}
        if network_id is not None:
            properties.update(network_id=network_id, element_collection=element_collection)
        features.append({"type": "Feature", "id": element["id"], "geometry": geometry, "properties": properties})
    for collection in ("water_networks", "gravity_networks"):
        water = collection == "water_networks"
        for network in model.get(collection, []):
            node_collections = ("junctions", "reservoirs", "tanks") if water else ("nodes",)
            nodes = {n["id"]: n for key in node_collections for n in network.get(key) or []}
            for key in node_collections:
                for node in network.get(key) or []:
                    add(node, collection, {"type": "Point", "coordinates": [node["x"], node["y"]]}, network["id"], key)
            for key in (("pipes", "pumps", "valves") if water else ("conduits",)):
                for link in network.get(key) or []:
                    try:
                        a, b = nodes[link["from"]], nodes[link["to"]]
                    except KeyError as exc:
                        raise CivilError("dangling_node_reference", id=link["id"]) from exc
                    path = [[a["x"], a["y"]], *deepcopy(link.get("vertices", [])), [b["x"], b["y"]]]
                    add(link, collection, {"type": "LineString", "coordinates": path}, network["id"], key)
    for alignment in model.get("alignments", []):
        from .alignment import densify
        path = densify(alignment, step=spacing_m)["points"]
        add(alignment, "alignments", {"type": "LineString", "coordinates": path})
    for key, field in (("catchments", "polygon"), ("surfaces", "boundary")):
        for element in model.get(key, []):
            if element.get(field) is None:
                dropped.append({"collection": key, "id": element["id"], "fields": list(element), "reason": f"{field}_missing"})
                continue
            add(element, key, {"type": "Polygon", "coordinates": [_ring(element[field])]})
    for key in ("structures", "tie_ins"):
        for element in model.get(key, []):
            dropped.append({"collection": key, "id": element["id"], "fields": list(element), "reason": "no_spatial_geometry_in_schema"})
    return features, dropped


def export_geojson(model: dict, *, spacing_m=10.0) -> dict:
    """Export FeatureCollection in lon,lat, CCW exterior rings (RFC 7946)."""
    record = crs_ops.normalize(model.get("crs"))
    if record["horizontal"] is None:
        raise CivilError("crs_unknown")
    features, dropped = _features(model, spacing_m)
    provenance = []
    def convert(point):
        result = crs_ops.to_wgs84(record, point)
        if not provenance:
            provenance.append(result["provenance"])
        return result["xy"]
    for feature in features:
        feature["geometry"] = _map_geometry(feature["geometry"], convert, geographic=True)
    geojson = {"type": "FeatureCollection", "features": features, "civil_site": _site_metadata(model)}
    return _result(geojson=geojson, fields_dropped=dropped, provenance=provenance,
                   assumptions=["Analytic alignments are densified in geometry; original attributes retained in properties", "Elevation is an attribute, not a transformed GeoJSON height"],
                   method={"name": "GeoJSON civil feature export", "reference": GEOJSON_REFERENCE})


def _import_features(features, model, mapping, convert):
    if mapping is not None and not isinstance(mapping, dict):
        raise CivilError("invalid_field_mapping")
    dropped = []
    for i, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise CivilError("invalid_gis_feature", index=i)
        kind, _ = _valid_geometry(feature.get("geometry"))
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise CivilError("invalid_gis_properties", index=i)
        own = mapping is None and "collection" in properties and "attributes" in properties
        if own:
            spec = properties
            element = deepcopy(properties["attributes"])
            if not isinstance(element, dict):
                raise CivilError("invalid_gis_properties")
        else:
            spec = (mapping or {}).get(kind)
            if not isinstance(spec, dict):
                raise CivilError("field_mapping_required", geometry_type=kind)
            fields = spec.get("fields", {})
            if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
                raise CivilError("invalid_field_mapping")
            element = deepcopy(spec.get("defaults", {}))
            if not isinstance(element, dict):
                raise CivilError("invalid_field_mapping", "defaults must be an object")
            for target, source in fields.items():
                if source not in properties:
                    raise CivilError("mapped_field_missing", field=source, index=i)
                element[target] = deepcopy(properties[source])
            unused = sorted(set(properties)-set(fields.values()))
            if unused:
                dropped.append({"feature": i, "fields": unused, "reason": "unmapped_properties"})
        collection = spec.get("collection")
        if collection not in schema.COLLECTIONS:
            raise CivilError("invalid_field_mapping", "Target collection is required")
        if not isinstance(element.get("id"), str) or not element["id"]:
            raise CivilError("invalid_id", index=i)
        geometry = _map_geometry(feature["geometry"], convert)
        _, coords = _valid_geometry(geometry)
        if kind == "Point":
            element.update(x=coords[0], y=coords[1])
        elif kind == "LineString":
            if collection == "alignments":
                if not own:
                    element["horizontal"] = [{"type": "line", "start": a, "end": b} for a, b in zip(coords, coords[1:])]
                    element.setdefault("vertical", None)
                    element.setdefault("start_station", 0.0)
            elif collection in ("water_networks", "gravity_networks"):
                if not own or "vertices" in element or len(coords) > 2:
                    element["vertices"] = coords[1:-1]
            else:
                raise CivilError("invalid_field_mapping", "Lines target alignments or network links")
        elif collection == "catchments":
            original = element.get("polygon")
            closed = own and isinstance(original, list) and len(original) > 1 and original[0] == original[-1]
            element["polygon"] = coords[0] if closed else coords[0][:-1]
        elif collection == "surfaces":
            original = element.get("boundary")
            closed = own and isinstance(original, list) and len(original) > 1 and original[0] == original[-1]
            element["boundary"] = coords[0] if closed else coords[0][:-1]
        else:
            raise CivilError("invalid_field_mapping", "Polygons target catchments or surface boundaries")
        if collection in ("water_networks", "gravity_networks"):
            network_id = spec.get("network_id")
            nested = spec.get("element_collection")
            allowed = ("junctions", "reservoirs", "tanks") if collection == "water_networks" else ("nodes",)
            allowed_links = ("pipes", "pumps", "valves") if collection == "water_networks" else ("conduits",)
            if nested not in (allowed if kind == "Point" else allowed_links):
                raise CivilError("invalid_field_mapping", "Incompatible geometry and network collection")
            network = schema.find(model, collection, network_id)
            items = network.setdefault(nested, [])
            if any(existing.get("id") == element["id"] for existing in items):
                raise CivilError("duplicate_id", id=element["id"])
            items.append(element)
        else:
            if any(existing.get("id") == element["id"] for existing in model[collection]):
                raise CivilError("duplicate_id", id=element["id"])
            schema.upsert(model, collection, element)
    return dropped


def import_geojson(data: dict | str, model: dict | None = None, *, field_mapping=None, crs=None) -> dict:
    """Mapping by geometry type: {Point: {collection, network_id,
    element_collection, fields: {civil_field: source_property}, defaults: {...}}}.
    Destination network envelopes must exist in model. Unknown hydraulic fields
    remain unknown and are exposed as schema_errors, never invented.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise CivilError("invalid_geojson", str(exc)) from exc
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise CivilError("invalid_geojson")
    if "crs" in data:
        raise CivilError("unsupported_geojson_crs", "RFC 7946 input must be WGS84")
    out = deepcopy(model) if model is not None else deepcopy(data.get("civil_site"))
    if out is None:
        out = schema.new_model("GIS import", crs)
    if not isinstance(out, dict):
        raise CivilError("invalid_geojson", "civil_site must be a model object")
    original_crs = crs_ops.normalize(out.get("crs"))
    if crs is not None and field_mapping is None and any(crs_ops.normalize(crs)[k] != original_crs[k] for k in ("horizontal", "local")):
        raise CivilError("unsupported_analytic_crs_change", "Civil attributes must retain their original CRS; use explicit mappings for geometry-only reprojection")
    record = crs_ops.normalize(crs if crs is not None else out.get("crs"))
    if record["horizontal"] is None:
        raise CivilError("crs_unknown")
    out["crs"] = record
    provenance = []
    def convert(point):
        if not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
            raise CivilError("invalid_geographic_coordinates", "RFC 7946 longitude/latitude must lie within [-180,180]/[-90,90]")
        result = crs_ops.from_wgs84(record, point)
        if not provenance:
            provenance.append(result["provenance"])
        return result["xy"]
    dropped = _import_features(data["features"], out, field_mapping, convert)
    out.setdefault("provenance", []).append({"source": "GeoJSON", "kind": "gis", "imported_at": datetime.now(timezone.utc).isoformat(),
                                             "crs": "EPSG:4326", "units_in": "degrees", "notes": "RFC 7946 always_xy transform"})
    return _result(model=out, fields_dropped=dropped, schema_errors=schema.validate(out), provenance=provenance,
                   method={"name": "GeoJSON mapped civil import", "reference": GEOJSON_REFERENCE})


def _wkb(geometry):
    kind, coords = _valid_geometry(geometry)
    number = {"Point": 1, "LineString": 2, "Polygon": 3}[kind]
    out = bytearray(struct.pack("<BI", 1, number))
    def points(items):
        return struct.pack("<I", len(items)) + b"".join(struct.pack("<dd", *p) for p in items)
    if kind == "Point":
        out.extend(struct.pack("<dd", *coords))
    elif kind == "LineString":
        out.extend(points(coords))
    else:
        out.extend(struct.pack("<I", len(coords)))
        for ring in coords:
            out.extend(points(ring))
    return bytes(out)


def _decode_geometry(blob, expected_srs):
    try:
        if not isinstance(blob, bytes) or len(blob) < 8 or blob[:2] != b"GP" or blob[2] != 0:
            raise CivilError("invalid_geopackage_geometry")
        flags = blob[3]
        if flags & 0xE0 or flags & 0x10:
            raise CivilError("unsupported_geopackage_geometry", "Extended or empty geometry")
        endian = "<" if flags & 1 else ">"
        srs = struct.unpack_from(endian+"i", blob, 4)[0]
        if srs != expected_srs:
            raise CivilError("geopackage_srs_mismatch")
        envelope = (flags >> 1) & 7
        sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
        if envelope not in sizes:
            raise CivilError("invalid_geopackage_geometry")
        position = 8 + sizes[envelope]
        order = blob[position]
        if order not in (0, 1):
            raise CivilError("invalid_geopackage_geometry")
        endian = "<" if order else ">"
        number = struct.unpack_from(endian+"I", blob, position+1)[0]
        position += 5
        def read(fmt):
            nonlocal position
            values = struct.unpack_from(endian+fmt, blob, position)
            position += struct.calcsize(endian+fmt)
            return values
        def points():
            count = read("I")[0]
            if count > (len(blob)-position)//16:
                raise CivilError("invalid_geopackage_geometry")
            return [list(read("dd")) for _ in range(count)]
        if number == 1:
            geometry = {"type": "Point", "coordinates": list(read("dd"))}
        elif number == 2:
            geometry = {"type": "LineString", "coordinates": points()}
        elif number == 3:
            count = read("I")[0]
            if count != 1:
                raise CivilError("unsupported_polygon_holes")
            geometry = {"type": "Polygon", "coordinates": [points()]}
        else:
            raise CivilError("unsupported_gis_geometry", wkb_type=number)
        if position != len(blob):
            raise CivilError("invalid_geopackage_geometry", "Trailing geometry bytes")
        _valid_geometry(geometry)
        return geometry
    except (struct.error, IndexError, TypeError) as exc:
        raise CivilError("invalid_geopackage_geometry", str(exc)) from exc


def export_geopackage(model: dict, path, *, spacing_m=10.0) -> dict:
    """Write OGC 1.3 feature tables in the projected EPSG CRS (no local grid)."""
    from pyproj import CRS  # call-time: tool registration imports this module without PROJ
    record = crs_ops.normalize(model.get("crs"))
    if record["horizontal"] is None:
        raise CivilError("crs_unknown")
    parsed = CRS.from_user_input(record["horizontal"])
    authority = parsed.to_authority()
    if authority is None or authority[0] != "EPSG":
        raise CivilError("geopackage_epsg_required")
    srs = int(authority[1])
    path = Path(path)
    if path.exists():
        raise CivilError("output_exists", path=str(path))
    features, dropped = _features(model, spacing_m)
    for feature in features:
        feature["geometry"] = _map_geometry(feature["geometry"], lambda p: crs_ops.local_to_projected(record, p))
    try:
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA application_id=1196444487")
            db.execute("PRAGMA user_version=10300")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY, organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER, FOREIGN KEY(srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, PRIMARY KEY(table_name,column_name), FOREIGN KEY(table_name) REFERENCES gpkg_contents(table_name), FOREIGN KEY(srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE civil_features (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, geom GEOMETRY NOT NULL, properties TEXT NOT NULL);
CREATE TABLE civil_site (model_json TEXT NOT NULL);
""")
            db.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
                ("Undefined Cartesian", -1, "NONE", -1, "undefined", ""),
                ("Undefined Geographic", 0, "NONE", 0, "undefined", ""),
                ("WGS 84", 4326, "EPSG", 4326, CRS.from_epsg(4326).to_wkt(version="WKT1_GDAL"), "")])
            if srs != 4326:
                db.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", (parsed.name, srs, "EPSG", srs, parsed.to_wkt(version="WKT1_GDAL"), "Project horizontal CRS"))
            db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('civil_features','features','Civil features',?)", (srs,))
            db.execute("INSERT INTO gpkg_geometry_columns VALUES ('civil_features','geom','GEOMETRY',?,0,0)", (srs,))
            for feature in features:
                blob = b"GP" + bytes([0, 1]) + struct.pack("<i", srs) + _wkb(feature["geometry"])
                db.execute("INSERT INTO civil_features(geom,properties) VALUES (?,?)", (blob, json.dumps(feature["properties"], allow_nan=False)))
            db.execute("INSERT INTO civil_site VALUES (?)", (json.dumps(_site_metadata(model), allow_nan=False),))
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise CivilError("geopackage_write_failed", str(exc)) from exc
    return _result(path=str(path), feature_count=len(features), srs_id=srs, fields_dropped=dropped,
                   assumptions=["Geometry stored in projected CRS; local grid transformed explicitly", "Civil attributes and network envelopes retained as JSON"],
                   method={"name": "OGC GeoPackage civil feature export", "reference": GPKG_REFERENCE})


def import_geopackage(path, model: dict | None = None, *, field_mapping=None) -> dict:
    """Read 2D feature tables using their declared EPSG CRS; apply explicit mapping."""
    from pyproj import CRS  # call-time: tool registration imports this module without PROJ
    def quote(identifier):
        return '"' + identifier.replace('"', '""') + '"'
    try:
        with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as db:
            rows = db.execute("SELECT g.table_name,g.column_name,g.srs_id,g.z,g.m,s.organization,s.organization_coordsys_id FROM gpkg_geometry_columns g JOIN gpkg_spatial_ref_sys s ON s.srs_id=g.srs_id JOIN gpkg_contents c ON c.table_name=g.table_name WHERE c.data_type='features'").fetchall()
            if not rows:
                raise CivilError("invalid_geopackage", "No feature tables")
            has_site = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='civil_site'").fetchone()
            native_site = json.loads(db.execute("SELECT model_json FROM civil_site").fetchone()[0]) if has_site else None
            out = deepcopy(model) if model is not None else (native_site if native_site is not None else schema.new_model("GeoPackage import", f"EPSG:{rows[0][6]}"))
            dropped, provenance = [], []
            source_units = {}
            for table, geom_column, srs, z, m, organization, code in rows:
                if organization.upper() != "EPSG":
                    raise CivilError("geopackage_epsg_required")
                if z or m:
                    raise CivilError("unsupported_gis_dimensions")
                source = f"EPSG:{code}"
                source_units[table] = [axis.unit_name for axis in CRS.from_user_input(source).axis_info[:2]]
                cursor = db.execute("SELECT * FROM " + quote(table))
                names = [d[0] for d in cursor.description]
                features = []
                for row in cursor:
                    attributes = dict(zip(names, row))
                    geometry = _decode_geometry(attributes.pop(geom_column), srs)
                    if table == "civil_features" and "properties" in attributes:
                        properties = json.loads(attributes["properties"])
                    else:
                        properties = attributes
                    features.append({"type": "Feature", "geometry": geometry, "properties": properties})
                destination = crs_ops.normalize(out.get("crs"))
                if destination["horizontal"] is None:
                    raise CivilError("crs_unknown")
                if native_site is not None and field_mapping is None:
                    native_crs = crs_ops.normalize(native_site.get("crs"))
                    if any(destination[k] != native_crs[k] for k in ("horizontal", "local")) and any(f["properties"].get("collection") in ("alignments", "surfaces") for f in features):
                        raise CivilError("unsupported_analytic_crs_change", "Analytic alignments/TIN attributes cannot be reprojected from their vector footprint; use explicit field mappings")
                def convert(point):
                    if destination["horizontal"] == source:
                        return crs_ops.projected_to_local(destination, point)
                    result = crs_ops.transform(point, source, destination)
                    if not provenance:
                        provenance.append(result["provenance"])
                    return result["xy"]
                dropped.extend(_import_features(features, out, field_mapping, convert))
    except CivilError:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        raise CivilError("invalid_geopackage", str(exc)) from exc
    out.setdefault("provenance", []).append({"source": str(path), "kind": "gis", "imported_at": datetime.now(timezone.utc).isoformat(),
                                             "crs": [f"EPSG:{r[6]}" for r in rows], "units_in": source_units, "notes": "OGC GeoPackage feature import; pyproj converts declared source units to project metres"})
    return _result(model=out, fields_dropped=dropped, schema_errors=schema.validate(out), provenance=provenance,
                   method={"name": "OGC GeoPackage mapped civil import", "reference": GPKG_REFERENCE})

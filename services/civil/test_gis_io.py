"""OGC GeoPackage 1.3 header/WKB and RFC 7946 analytic coordinate fixtures."""
from copy import deepcopy
import json
import math
import sqlite3
import struct
import pytest
from . import gis_io, schema
from .errors import CivilError


def site():
    model = schema.new_model("GIS site", {"horizontal": "EPSG:26915"})
    model["gravity_networks"] = [schema.gravity_network("g", nodes=[
        {"id": "a", "type": "manhole", "x": 500000.125, "y": 0.25, "rim": 4, "invert": 1},
        {"id": "b", "type": "outfall", "x": 500100.5, "y": 0.75, "rim": 3, "invert": 0}],
        conduits=[{"id": "c", "from": "a", "to": "b", "shape": "circular", "diameter": .3,
                   "length": None, "n": .013, "vertices": [[500050.25, 1.125]], "material": "PVC"}])]
    model["catchments"] = [{"id": "lot", "name": "Lot", "outlet": "a", "area_m2": 100,
                            "runoff_coeff": .5, "cn": 80, "imperv_pct": 40, "tc_s": None,
                            "tc_segments": [], "polygon": [[500000, 0], [500000, 10], [500010, 10], [500010, 0]]}]
    return model


def test_geojson_wgs84_lon_lat_right_hand_ring_and_loss():
    model = site()
    model["gravity_networks"][0]["nodes"][0].update(x=500000, y=0)
    model["structures"] = [{"id": "s", "kind": "culvert"}]
    exported = gis_io.export_geojson(model)
    json.dumps(exported, allow_nan=False)
    feature = exported["geojson"]["features"][0]
    # EPSG:26915 central meridian / false easting identity: (-93°,0°).
    assert feature["geometry"]["coordinates"] == pytest.approx([-93, 0], abs=1e-9)
    polygon = exported["geojson"]["features"][-1]["geometry"]["coordinates"][0]
    assert polygon[0] == polygon[-1]
    assert sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(polygon, polygon[1:])) > 0
    assert exported["fields_dropped"] == [{"collection": "structures", "id": "s",
        "fields": ["id", "kind"], "reason": "no_spatial_geometry_in_schema"}]
    imported = gis_io.import_geojson(exported["geojson"])
    assert imported["schema_errors"] == []
    assert imported["model"]["gravity_networks"][0]["nodes"][0]["x"] == pytest.approx(500000, abs=1e-6)


def test_geopackage_tables_headers_and_exact_geometry_roundtrip(tmp_path):
    model = site()
    path = tmp_path / "site.gpkg"
    gis_io.export_geopackage(model, path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0x47504B47
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10300
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"gpkg_spatial_ref_sys", "gpkg_contents", "gpkg_geometry_columns"} <= names
        assert db.execute("SELECT data_type,srs_id FROM gpkg_contents").fetchone() == ("features", 26915)
        assert db.execute("SELECT geometry_type_name,z,m FROM gpkg_geometry_columns").fetchone() == ("GEOMETRY", 0, 0)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        blob = db.execute("SELECT geom FROM civil_features ORDER BY fid LIMIT 1").fetchone()[0]
        # Independent binary inspection, GP flags little-endian, no envelope, WKB Point.
        assert blob[:4] == b"GP\x00\x01"
        assert struct.unpack_from("<i", blob, 4)[0] == 26915
        assert struct.unpack_from("<BI", blob, 8) == (1, 1)
        assert struct.unpack_from("<dd", blob, 13) == (500000.125, .25)
    imported = gis_io.import_geopackage(path)
    assert imported["schema_errors"] == []
    assert imported["fields_dropped"] == []
    assert imported["model"]["gravity_networks"] == model["gravity_networks"]
    assert imported["model"]["catchments"] == model["catchments"]


def test_geopackage_local_grid_stores_projected_not_local_coordinates(tmp_path):
    model = site()
    model["crs"]["local"] = {"origin_x": 500000, "origin_y": 0, "rotation_rad": 0, "scale_factor": 2}
    model["gravity_networks"][0]["nodes"][0].update(x=3, y=4)
    path = tmp_path / "local.gpkg"
    gis_io.export_geopackage(model, path)
    with sqlite3.connect(path) as db:
        blob = db.execute("SELECT geom FROM civil_features ORDER BY fid LIMIT 1").fetchone()[0]
        assert struct.unpack_from("<dd", blob, 13) == (500006, 8)
    result = gis_io.import_geopackage(path)
    assert result["model"]["gravity_networks"][0]["nodes"][0]["x"] == 3
    assert result["model"]["gravity_networks"][0]["nodes"][0]["y"] == 4


def test_alignment_arc_is_densified_not_endpoint_chord():
    model = schema.new_model("Arc", {"horizontal": "EPSG:26915"})
    model["alignments"] = [{"id": "arc", "name": "Arc", "start_station": 0, "vertical": None,
        "horizontal": [{"type": "arc", "start": [500010, 0], "end": [500000, 10],
                        "center": [500000, 0], "radius": 10, "dir": "ccw"}]}]
    exported = gis_io.export_geojson(model, spacing_m=2)
    geometry = exported["geojson"]["features"][0]["geometry"]
    from .crs import from_wgs84
    points = [from_wgs84(model["crs"], p)["xy"] for p in geometry["coordinates"]]
    assert len(points) >= 9  # Arc length 5*pi m, max spacing 2 m.
    for x, y in points:
        assert math.hypot(x-500000, y) == pytest.approx(10, abs=1e-6)


def test_external_geojson_point_line_polygon_field_mapping_and_explicit_loss():
    model = schema.new_model("Mapped", {"horizontal": "EPSG:26915"})
    model["gravity_networks"] = [schema.gravity_network("g")]
    mapping = {
        "Point": {"collection": "gravity_networks", "network_id": "g", "element_collection": "nodes",
                  "fields": {"id": "asset_id"}, "defaults": {"type": "outfall", "rim": 2, "invert": 0}},
        "LineString": {"collection": "alignments", "fields": {"id": "asset_id", "name": "label"}},
        "Polygon": {"collection": "catchments", "fields": {"id": "asset_id"},
                    "defaults": {"name": "Area", "outlet": "node", "area_m2": 10, "runoff_coeff": .5,
                                 "cn": 80, "imperv_pct": 40, "tc_s": None, "tc_segments": []}},
    }
    data = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"asset_id": "node", "unused": "reported"},
         "geometry": {"type": "Point", "coordinates": [-93, 0]}},
        {"type": "Feature", "properties": {"asset_id": "line", "label": "Line"},
         "geometry": {"type": "LineString", "coordinates": [[-93, 0], [-92.9999, 0]]}},
        {"type": "Feature", "properties": {"asset_id": "poly"},
         "geometry": {"type": "Polygon", "coordinates": [[[-93, 0], [-92.9999, 0], [-92.9999, .0001], [-93, 0]]]}},
    ]}
    result = gis_io.import_geojson(data, model, field_mapping=mapping)
    assert result["schema_errors"] == []
    assert result["model"]["gravity_networks"][0]["nodes"][0]["x"] == pytest.approx(500000, abs=1e-6)
    assert result["model"]["alignments"][0]["horizontal"][0]["end"][0] > 500000
    assert len(result["model"]["catchments"][0]["polygon"]) == 3
    assert result["fields_dropped"] == [{"feature": 0, "fields": ["unused"], "reason": "unmapped_properties"}]
    assert model["gravity_networks"][0]["nodes"] == []  # Input model is not mutated.


@pytest.mark.parametrize("data,reason", [
    ({"type": "FeatureCollection", "crs": {}, "features": []}, "unsupported_geojson_crs"),
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
       "geometry": {"type": "Point", "coordinates": [0, 0, 1]}}]}, "invalid_gis_geometry"),
    ({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
       "geometry": {"type": "Point", "coordinates": [0, 0]}}]}, "field_mapping_required"),
])
def test_import_refusals(data, reason):
    with pytest.raises(CivilError) as exc:
        gis_io.import_geojson(data, schema.new_model("A", {"horizontal": "EPSG:26915"}))
    assert exc.value.reason == reason


def test_export_unknown_crs_and_existing_file_refuse(tmp_path):
    with pytest.raises(CivilError) as exc:
        gis_io.export_geojson(schema.new_model("Unknown"))
    assert exc.value.reason == "crs_unknown"
    path = tmp_path / "existing.gpkg"
    path.write_bytes(b"user file")
    with pytest.raises(CivilError) as exc:
        gis_io.export_geopackage(site(), path)
    assert exc.value.reason == "output_exists"
    assert path.read_bytes() == b"user file"


def test_missing_mapping_refuses_before_coordinate_operation(monkeypatch):
    def forbidden_transform(*args, **kwargs):
        raise AssertionError("Coordinate operation must not precede field-mapping validation")
    monkeypatch.setattr(gis_io.crs_ops, "from_wgs84", forbidden_transform)
    data = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [0, 0]}}]}
    with pytest.raises(CivilError) as exc:
        gis_io.import_geojson(data, schema.new_model("A", {"horizontal": "EPSG:26915"}))
    assert exc.value.reason == "field_mapping_required"

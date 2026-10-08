"""Metre-based project CRS and local grids using pyproj/PROJ (MIT).

Local grid equations: contracts/civil_schema.md §3. Scale factors: PROJ
proj_factors; combined factor k R/(R+h), using the ellipsoid Gaussian radius
sqrt(M*N) at the point. No vertical datum transformation is implied.
"""
from __future__ import annotations

import math
from copy import deepcopy
import pyproj
from pyproj import CRS, Proj, Transformer
from .errors import CivilError

REFERENCE = "PROJ coordinate operations / proj_factors; contracts/civil_schema.md §3"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse(code, projected=False):
    try:
        parsed = CRS.from_user_input(code)
    except (pyproj.exceptions.CRSError, TypeError, ValueError) as exc:
        raise CivilError("invalid_crs", str(exc)) from exc
    if projected and not parsed.is_projected:
        raise CivilError("crs_not_projected", "Project coordinates require a projected metre-based CRS")
    if projected and any(abs(axis.unit_conversion_factor - 1.0) > 1e-12 for axis in parsed.axis_info[:2]):
        raise CivilError("crs_not_metres", "Convert foreign units before using a project CRS")
    return parsed


def normalize(record: dict | str | None = None) -> dict:
    """Normalize a project CRS, refusing geographic/non-metre horizontal axes."""
    if isinstance(record, str):
        record = {"horizontal": record}
    if record is None:
        record = {}
    if not isinstance(record, dict):
        raise CivilError("invalid_crs")
    out = {"horizontal": None, "vertical": None, "local": None, "notes": "", **deepcopy(record)}
    for key in ("horizontal", "vertical"):
        if out[key] is not None:
            parsed = _parse(out[key], projected=key == "horizontal")
            authority = parsed.to_authority()
            if authority is None:
                raise CivilError("crs_authority_required", "Persist an authority:code CRS identifier")
            if key == "vertical" and not parsed.is_vertical:
                raise CivilError("crs_not_vertical")
            out[key] = ":".join(authority)
    local = out["local"]
    if local is not None:
        if not isinstance(local, dict) or any(not _finite(local.get(k)) for k in ("origin_x", "origin_y", "rotation_rad", "scale_factor")):
            raise CivilError("invalid_local_grid", "Local grid requires finite origin, rotation and scale")
        if local["scale_factor"] <= 0:
            raise CivilError("invalid_local_grid", "Scale factor must be positive")
    if not isinstance(out["notes"], str):
        raise CivilError("invalid_crs", "notes must be a string")
    return out


def validate(record) -> list[str]:
    try:
        normalize(record)
    except CivilError as exc:
        return [f"crs: {exc.reason}"]
    return []


def _xy(xy):
    if not isinstance(xy, (list, tuple)) or len(xy) != 2 or not all(_finite(v) for v in xy):
        raise CivilError("invalid_coordinates", "Supply two finite coordinates")
    return tuple(xy)


def local_to_projected(record, xy) -> list[float]:
    record = normalize(record)
    x, y = _xy(xy)
    local = record["local"]
    if local is None:
        return [x, y]
    c, s = math.cos(local["rotation_rad"]), math.sin(local["rotation_rad"])
    scale = local["scale_factor"]
    return [local["origin_x"] + scale*(x*c-y*s), local["origin_y"] + scale*(x*s+y*c)]


def projected_to_local(record, xy) -> list[float]:
    record = normalize(record)
    x, y = _xy(xy)
    local = record["local"]
    if local is None:
        return [x, y]
    x, y = (x-local["origin_x"])/local["scale_factor"], (y-local["origin_y"])/local["scale_factor"]
    c, s = math.cos(local["rotation_rad"]), math.sin(local["rotation_rad"])
    return [x*c+y*s, -x*s+y*c]


def _operation_crs(value):
    if isinstance(value, dict) or value is None:
        record = normalize(value)
        if record["horizontal"] is None:
            raise CivilError("crs_unknown", "Set the project horizontal CRS before geodetic operations")
        return _parse(record["horizontal"], projected=True), record
    return _parse(value), None


def transform(xy, src, dst) -> dict:
    """Transform with always_xy=True; standalone geographic CRS strings are valid.

    Model CRS records are projected metres. Their local grid is applied before
    source transformation and inverted after destination transformation. Vertical
    CRS is recorded only: inputs/outputs are strictly two-dimensional.
    """
    source, source_record = _operation_crs(src)
    destination, destination_record = _operation_crs(dst)
    x, y = _xy(xy)
    if source_record:
        x, y = local_to_projected(source_record, [x, y])
    try:
        transformer = Transformer.from_crs(source, destination, always_xy=True)
        x, y = transformer.transform(x, y, errcheck=True)
        if not all(math.isfinite(v) for v in (x, y)):
            raise CivilError("crs_transform_failed")
    except pyproj.exceptions.ProjError as exc:
        raise CivilError("crs_transform_failed", str(exc)) from exc
    out = projected_to_local(destination_record, [x, y]) if destination_record else [x, y]
    try:
        operation = transformer.get_last_used_operation()
    except pyproj.exceptions.ProjError:
        operation = transformer
    return {"ok": True, "xy": out, "warnings": [],
            "assumptions": ["2D horizontal operation only; elevation and vertical datum unchanged"],
            "method": {"name": "PROJ coordinate transformation", "reference": REFERENCE},
            "provenance": {"pyproj_version": pyproj.__version__, "proj_version": pyproj.proj_version_str,
            "source_crs": source.to_string(), "destination_crs": destination.to_string(),
            "source_local": source_record.get("local") if source_record else None,
            "units_in": [axis.unit_name for axis in source.axis_info[:2]],
            "units_out": [axis.unit_name for axis in destination.axis_info[:2]],
            "destination_local": destination_record.get("local") if destination_record else None,
            "always_xy": True, "operation": operation.description,
            "accuracy_m": operation.accuracy if operation.accuracy >= 0 else None,
            "definition": operation.definition}}


def to_wgs84(model_crs, xy) -> dict:
    return transform(xy, normalize(model_crs), "EPSG:4326")


def from_wgs84(model_crs, xy) -> dict:
    return transform(xy, "EPSG:4326", normalize(model_crs))


def scale_factors(crs, x, y, elevation_m=0.0) -> dict:
    """Grid/elevation/combined factors; h is ellipsoidal height, not orthometric."""
    record = normalize(crs)
    if record["horizontal"] is None:
        raise CivilError("crs_unknown")
    if not _finite(elevation_m):
        raise CivilError("invalid_elevation")
    projected = _parse(record["horizontal"], projected=True)
    X, Y = local_to_projected(record, [x, y])
    try:
        lon, lat = Transformer.from_crs(projected, projected.geodetic_crs, always_xy=True).transform(X, Y, errcheck=True)
        factors = Proj(projected).get_factors(lon, lat, errcheck=True)
    except pyproj.exceptions.ProjError as exc:
        raise CivilError("crs_transform_failed", str(exc)) from exc
    a = projected.ellipsoid.semi_major_metre
    b = projected.ellipsoid.semi_minor_metre
    e2 = 1 - (b/a)**2
    denominator = 1 - e2*math.sin(math.radians(lat))**2
    M = a*(1-e2)/denominator**1.5
    N = a/math.sqrt(denominator)
    R = math.sqrt(M*N)
    if R + elevation_m <= 0:
        raise CivilError("invalid_elevation", "Elevation must exceed negative ellipsoid radius")
    elevation_factor = R/(R+elevation_m)
    grid_factor = math.sqrt(factors.meridional_scale*factors.parallel_scale)
    local_scale = record["local"]["scale_factor"] if record["local"] else 1.0
    return {"ok": True, "grid_factor": grid_factor, "meridional_factor": factors.meridional_scale,
            "parallel_factor": factors.parallel_scale, "convergence_deg": factors.meridian_convergence,
            "elevation_factor": elevation_factor, "combined_factor": grid_factor*elevation_factor,
            "local_to_ground_factor": local_scale/(grid_factor*elevation_factor),
            "ellipsoid_radius_m": R, "elevation_m": elevation_m, "warnings": [],
            "assumptions": ["Elevation is ellipsoidal height", "Grid factor is geometric mean of meridional and parallel scales; nonconformal projections are direction-dependent"],
            "method": {"name": "PROJ factors and Gaussian ellipsoid radius", "reference": REFERENCE},
            "provenance": {"pyproj_version": pyproj.__version__, "proj_version": pyproj.proj_version_str,
                           "crs": record["horizontal"]}}

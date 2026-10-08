"""Analytic Transverse Mercator identities and local-grid matrix inversion.

NAD83 / UTM zone 15N EPSG:26915 central meridian -93°, false easting
500000 m, scale .9996, latitude origin 0° (EPSG coordinate operation 16015).
"""
import math
import pytest
from . import crs
from .errors import CivilError


def test_utm_central_meridian_false_easting_identity_and_axis_order():
    result = crs.from_wgs84({"horizontal": "EPSG:26915"}, [-93, 0])
    assert result["xy"] == pytest.approx([500000, 0], abs=1e-6)
    inverse = crs.to_wgs84({"horizontal": "EPSG:26915"}, [500000, 0])
    assert inverse["xy"] == pytest.approx([-93, 0], abs=1e-9)
    assert result["provenance"]["always_xy"] is True
    assert result["provenance"]["pyproj_version"]
    assert result["provenance"]["proj_version"]
    assert "accuracy_m" in result["provenance"]


def test_local_grid_forward_hand_calculation_and_inverse():
    record = {"horizontal": "EPSG:26915", "local": {
        "origin_x": 500000, "origin_y": 1000000, "rotation_rad": math.pi/2, "scale_factor": 2}}
    # Rotation 90° maps [3,4] to [-4,3], scale 2 -> [-8,6].
    assert crs.local_to_projected(record, [3, 4]) == pytest.approx([499992, 1000006])
    assert crs.projected_to_local(record, [499992, 1000006]) == pytest.approx([3, 4])
    geographic = crs.to_wgs84(record, [3, 4])
    assert crs.from_wgs84(record, geographic["xy"])["xy"] == pytest.approx([3, 4], abs=1e-7)


def test_transform_local_to_local_applies_both_grid_maps():
    src = {"horizontal": "EPSG:26915", "local": {"origin_x": 500000, "origin_y": 0,
           "rotation_rad": 0, "scale_factor": 2}}
    dst = {"horizontal": "EPSG:26915", "local": {"origin_x": 499000, "origin_y": 0,
           "rotation_rad": 0, "scale_factor": 1}}
    assert crs.transform([10, 20], src, dst)["xy"] == pytest.approx([1020, 40])


def test_central_meridian_scale_and_elevation_factor():
    factors = crs.scale_factors("EPSG:26915", 500000, 0, elevation_m=1000)
    assert factors["grid_factor"] == pytest.approx(.9996, abs=1e-8)
    assert factors["convergence_deg"] == pytest.approx(0, abs=1e-9)
    # At equator Gaussian radius = semi-minor axis b for the GRS80 ellipsoid.
    radius = 6356752.314140356
    expected = radius/(radius+1000)
    assert factors["ellipsoid_radius_m"] == pytest.approx(radius, abs=.001)
    assert factors["elevation_factor"] == pytest.approx(expected, abs=1e-12)
    assert factors["combined_factor"] == pytest.approx(.9996*expected, abs=1e-8)


@pytest.mark.parametrize("operation,reason", [
    (lambda: crs.to_wgs84({"horizontal": None}, [1, 2]), "crs_unknown"),
    (lambda: crs.normalize("EPSG:4326"), "crs_not_projected"),
    (lambda: crs.normalize("EPSG:2227"), "crs_not_metres"),
    (lambda: crs.normalize("EPSG:not-real"), "invalid_crs"),
    (lambda: crs.normalize({"local": {"origin_x": 0, "origin_y": 0, "rotation_rad": 0, "scale_factor": 0}}), "invalid_local_grid"),
    (lambda: crs.transform([float("nan"), 0], "EPSG:26915", "EPSG:4326"), "invalid_coordinates"),
])
def test_crs_refusals(operation, reason):
    with pytest.raises(CivilError) as exc:
        operation()
    assert exc.value.reason == reason

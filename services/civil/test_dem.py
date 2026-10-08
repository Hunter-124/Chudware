"""Offline DEM V&V from exact planes, cell counts and raster geometry.

No reference-engine dependency: fixtures below are closed-form constructions.
Routing uses O'Callaghan & Mark (1984) steepest D8; filling uses the spill-level
definition in Barnes et al. (2014); slope uses Horn's (1981) 3x3 stencil.
"""
import json

import numpy as np
import pytest

from . import dem
from .errors import CivilError


def grid(z, *, h=1.0, x=0.0, y=0.0, nodata=None):
    z = np.asarray(z, dtype=float)
    return dict(origin_x=x, origin_y=y, cell_size=h,
                nrows=z.shape[0], ncols=z.shape[1], z=z, nodata=nodata)


def center(g, r, c):
    return [g["origin_x"] + (c + 0.5) * g["cell_size"],
            g["origin_y"] + (g["nrows"] - r - 0.5) * g["cell_size"]]


def plane(rows=5, cols=6, *, a=-2.0, b=0.0, h=1.0):
    # z=a*x+b*y+20; cell centres, not corners.
    x = (np.arange(cols) + 0.5) * h
    y = (rows - np.arange(rows) - 0.5) * h
    return grid(20 + a * x[None, :] + b * y[:, None], h=h)


def valley():
    # Side gradient 2, longitudinal southward gradient 1. Every valid cell
    # reaches the bottom-centre outlet: 5*5=25 cells, 100 m2 at h=2 m.
    return grid(np.arange(4, -1, -1)[:, None] + 2 * np.abs(np.arange(5) - 2), h=2)


def ring_area(ring):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) / 2


def geometry_area(geometry):
    polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                else geometry["coordinates"])
    return sum(sum(ring_area(ring) for ring in polygon) for polygon in polygons)


@pytest.mark.parametrize("a,b,code", [(-2, 0, 1), (0, 2, 4), (-1, 1, 2),
                                      (2, 0, 16), (0, -2, 64), (1, -1, 32)])
def test_tilted_plane_d8_closed_form(a, b, code):
    directions = np.asarray(dem.flow_direction_d8(plane(a=a, b=b))["directions"])
    # Orthogonal fall / h versus diagonal fall / (sqrt(2)*h).
    assert np.all(directions[1:-1, 1:-1] == code)
    if code == 1:
        assert np.all(directions[:, :-1] == 1)
        assert np.all(directions[:, -1] == 0)


def test_valley_conserves_cells_area_and_optional_weights():
    g = valley()
    fdir = dem.flow_direction_d8(g)
    weights = np.arange(1, 26).reshape(5, 5)
    acc = dem.flow_accumulation(fdir, weights=weights)
    assert acc["cells"][4][2] == 25
    assert acc["area_m2"][4][2] == 100
    # Independent integer-series sum, 25*26/2=325.
    assert acc["weighted_accumulation"][4][2] == 325
    directions = np.asarray(fdir["directions"])
    assert np.count_nonzero(directions == 0) == 1
    assert acc["cells"][0][0] == 1  # own cell is included


def test_priority_flood_exact_spill_and_volume_no_input_mutation():
    z = np.full((5, 5), 9.0)
    z[2, 2], z[2, 3], z[2, 4] = 1, 5, 5
    g = grid(z, h=3)
    filled = dem.fill_depressions(g)
    assert filled["z"][2][2] == 5  # 5 m spill, not rim elevation 9 m
    assert filled["filled_cells"] == 1
    assert filled["fill_volume_m3"] == 36  # (5-1)*3^2
    assert g["z"][2, 2] == 1
    assert filled["z"][2][3] == 5
    assert np.array_equal(dem.fill_depressions(filled)["z"], filled["z"])


def test_priority_flood_epsilon_is_explicit_and_nodata_is_open():
    z = np.full((5, 5), 9.0)
    z[2, 2], z[2, 3], z[2, 4] = 1, 5, 5
    filled = dem.fill_depressions(grid(z), epsilon=0.25)
    assert filled["z"][2][3] == 5.25
    assert filled["z"][2][2] == 5.5
    z[2, 3] = -9999
    open_pit = dem.fill_depressions(grid(z, nodata=-9999))
    assert open_pit["z"][2][2] == 1
    assert open_pit["z"][2][3] == -9999


def test_two_basins_split_at_ridge_with_exact_cell_counts():
    # Five rows; left 3 columns drain to col 1; right 3 columns plus ridge
    # drain to col 5. The exactly tied ridge drains east under declared ties.
    g = grid(np.arange(4, -1, -1)[:, None] + np.array([2, 0, 2, 10, 2, 0, 2]), h=2)
    directions = dem.flow_direction_d8(g)
    left = dem.watershed(directions, center(g, 4, 1))
    right = dem.watershed(directions, center(g, 4, 5))
    assert left["cells"] == 15
    assert right["cells"] == 20
    assert left["area_m2"] == 60
    assert right["area_m2"] == 80
    lm, rm = np.asarray(left["mask"]), np.asarray(right["mask"])
    assert not np.any(lm & rm)
    assert np.all(lm | rm)
    assert np.all(lm[:, :3]) and not np.any(lm[:, 3:])
    assert geometry_area(left["boundary"]) == 60
    assert geometry_area(right["boundary"]) == 80


def test_flat_towards_lower_edge_has_no_cycle_and_full_accumulation():
    z = np.full((7, 7), 10.0)
    z[6, 3] = 9
    fdir = dem.flow_direction_d8(grid(z))
    assert fdir["closed_depressions"] == 0
    acc = dem.flow_accumulation(fdir)
    assert acc["cells"][6][3] == 49
    assert dem.watershed(fdir, [3.5, 0.5])["cells"] == 49


def test_unfilled_closed_pit_reported_and_not_changed():
    z = np.full((5, 5), 10.0)
    z[2, 2] = 1
    fdir = dem.flow_direction_d8(grid(z))
    assert fdir["closed_depressions"] == 1
    assert "unfilled_closed_depressions:1" in fdir["warnings"]
    assert fdir["directions"][2][2] == 0
    assert z[2, 2] == 1


def test_cell_edge_boundary_preserves_hole():
    # A clockwise routing tree around a nodata centre. All eight cells reach
    # bottom-middle. Cell-union area is 9-1=8, not convex-hull area 9.
    g = grid([[1, 1, 1], [1, np.nan, 1], [1, 1, 1]], h=2, x=10, y=20)
    fdir = {k: g[k] for k in ("origin_x", "origin_y", "cell_size", "nrows", "ncols")}
    fdir["directions"] = [[4, 1, 4], [4, -1, 4], [1, 0, 16]]
    result = dem.watershed(fdir, center(g, 2, 1))
    assert result["cells"] == 8
    assert result["area_m2"] == 32
    assert result["boundary"]["type"] == "Polygon"
    assert len(result["boundary"]["coordinates"]) == 2
    assert geometry_area(result["boundary"]) == 32
    assert sorted(ring_area(r) for r in result["boundary_rings"]) == [-4, 36]
    assert all(r[0] == r[-1] for r in result["boundary_rings"])


def test_corner_connected_watershed_preserves_two_polygons():
    # Only two diagonally touching cells; D8 allows connection, polygon area
    # must stay 2 cells, not a self-crossing bow-tie or bounding rectangle.
    fdir = dict(origin_x=0, origin_y=0, cell_size=1, nrows=2, ncols=2,
                directions=[[2, -1], [-1, 0]])
    result = dem.watershed(fdir, [1.5, 0.5])
    assert result["cells"] == 2
    assert result["boundary"]["type"] == "MultiPolygon"
    assert len(result["boundary"]["coordinates"]) == 2
    assert geometry_area(result["boundary"]) == 2


def test_horn_slope_matches_plane_gradient_and_does_not_pad_edges():
    # dz/dx=0.03, dz/dy=-0.04 -> slope 100*sqrt(.03^2+.04^2)=5%.
    g = plane(a=0.03, b=-0.04, h=7)
    result = dem.slope_percent(g)
    slopes = np.asarray(result["slope_pct"], dtype=float)
    assert np.allclose(slopes[1:-1, 1:-1], 5, atol=2e-13, rtol=0)
    assert np.all(np.isnan(slopes[0])) and np.all(np.isnan(slopes[-1]))
    assert np.all(np.isnan(slopes[:, 0])) and np.all(np.isnan(slopes[:, -1]))
    g["z"][2, 2] = np.nan
    slopes = np.asarray(dem.slope_percent(g)["slope_pct"], dtype=float)
    assert np.all(np.isnan(slopes[1:4, 1:4]))


def test_ascii_roundtrip_nodata_and_north_up(tmp_path):
    g = grid([[1.123456789012345, -9999, 3], [40, 50, 60]], h=2.5,
             x=100.25, y=-50.125, nodata=-9999)
    path = tmp_path / "terrain.asc"
    dem.write_ascii_grid(g, path)
    read = dem.read_ascii_grid(path)
    assert read["origin_x"] == 100.25
    assert read["origin_y"] == -50.125
    assert read["cell_size"] == 2.5
    assert read["nodata"] == -9999
    assert np.array_equal(read["z"], g["z"])
    assert dem.sample(read, *center(g, 1, 1))["elevation_m"] == 50
    # Contract forbids numpy arrays/nonfinite numbers in returned results.
    json.dumps(read, allow_nan=False)


def test_ascii_xllcenter_yllcenter_variant(tmp_path):
    path = tmp_path / "centres.asc"
    path.write_text("NCOLS 2\nNROWS 2\nXLLCENTER 101\nYLLCENTER 201\n"
                    "CELLSIZE 2\nNODATA_value -9999\n10 20\n30 -9999\n")
    read = dem.read_ascii_grid(path)
    assert (read["origin_x"], read["origin_y"]) == (100, 200)
    assert dem.sample(read, 101, 203)["elevation_m"] == 10
    assert dem.sample(read, 101, 201)["elevation_m"] == 30
    assert dem.sample(read, 103, 201)["elevation_m"] is None


def test_ascii_null_nodata_export_avoids_valid_sentinel_collision(tmp_path):
    g = grid([[-9999, np.nan], [-10000, 4]])
    path = tmp_path / "null.asc"
    out = dem.write_ascii_grid(g, path)
    assert out["nodata"] == -10001
    read = dem.read_ascii_grid(path)
    assert read["z"][0][0] == -9999
    assert read["z"][0][1] == -10001
    assert dem.to_tin_points(read)["points"] == [[0.5, 1.5, -9999.0],
                                                 [0.5, 0.5, -10000.0], [1.5, 0.5, 4.0]]


def test_xyz_regular_centres_unordered_and_tin_stride(tmp_path):
    path = tmp_path / "regular.xyz"
    # z=x+2*y; intentionally unordered.
    path.write_text("3 1 5\n1 3 7\n3 3 9\n1 1 3\n")
    read = dem.read_xyz(path, 2)
    assert (read["origin_x"], read["origin_y"]) == (0, 0)
    assert read["z"] == [[7, 9], [3, 5]]
    assert dem.to_tin_points(read)["points"] == [[1, 3, 7], [3, 3, 9], [1, 1, 3], [3, 1, 5]]
    assert dem.to_tin_points(read, stride=2)["points"] == [[1, 3, 7]]
    assert dem.sample(read, 2, 2)["elevation_m"] == 6


def test_bilinear_no_nodata_blending_or_extrapolation():
    g = grid([[1, 2], [3, np.nan]])
    assert dem.sample(g, 1, 1)["elevation_m"] is None
    assert dem.sample(g, 0.5, 1.5)["elevation_m"] == 1  # zero-weight nodata ignored
    assert dem.sample(g, 0, 1)["elevation_m"] is None
    assert dem.sample(grid([[8]]), 0.5, 0.5)["elevation_m"] == 8


def test_stream_reaches_follow_routing_and_preserve_confluences():
    # Two source arms merge at [1,1], then reach [2,1]. Selected cells >=2:
    # top corners feed top-middle and middle-left, so both arms carry 2 cells.
    fdir = dict(origin_x=0, origin_y=0, cell_size=2, nrows=3, ncols=3,
                directions=[[1, 4, -1], [1, 4, -1], [64, 0, -1]])
    acc = dem.flow_accumulation(fdir)
    assert acc["cells"] == [[1, 2, 0], [2, 5, 0], [1, 6, 0]]
    result = dem.streams(acc, 2)
    expected = [[[3, 5], [3, 3]], [[1, 3], [3, 3]], [[3, 3], [3, 1]]]
    assert sorted(result["polylines"]) == sorted(expected)
    isolated = dem.streams(acc, 6)
    assert isolated["polylines"] == []
    assert isolated["isolated_points"] == [[3, 1]]


@pytest.mark.parametrize("directions,reason", [([[1, 16]], "flow_direction_cycle"),
                                              ([[1, -1]], "flow_into_nodata"),
                                              ([[3, 0]], "invalid_d8_code")])
def test_d8_named_refusals(directions, reason):
    fdir = dict(origin_x=0, origin_y=0, cell_size=1, nrows=1, ncols=2, directions=directions)
    with pytest.raises(CivilError) as caught:
        dem.flow_accumulation(fdir)
    assert caught.value.reason == reason


@pytest.mark.parametrize("content,reason", [
    ("ncols 2\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\n1\n", "ascii_cell_count_mismatch"),
    ("ncols 1\nnrows 1\nxllcorner 0\nxllcenter .5\nyllcorner 0\ncellsize 1\n1\n", "invalid_ascii_origin"),
    ("ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 0\n1\n", "invalid_cell_size"),
])
def test_ascii_named_refusals(tmp_path, content, reason):
    path = tmp_path / "bad.asc"
    path.write_text(content)
    with pytest.raises(CivilError) as caught:
        dem.read_ascii_grid(path)
    assert caught.value.reason == reason


@pytest.mark.parametrize("text,reason", [("0 0 1\n2 0 2\n", "irregular_xyz_grid"),
                                         ("0 0 1\n0 0 2\n", "duplicate_xyz_cell"),
                                         ("0 0 1\n1 0 2\n0 1 3\n", "incomplete_xyz_grid")])
def test_xyz_named_refusals(tmp_path, text, reason):
    path = tmp_path / "bad.xyz"
    path.write_text(text)
    with pytest.raises(CivilError) as caught:
        dem.read_xyz(path, 1)
    assert caught.value.reason == reason


def test_invalid_geometry_weights_and_outlets_have_stable_reasons():
    g = grid([[1, 0], [1, 0]])
    fdir = dem.flow_direction_d8(g)
    cases = [(lambda: dem.fill_depressions(g, epsilon=-1), "invalid_epsilon"),
             (lambda: dem.to_tin_points(g, 0), "invalid_stride"),
             (lambda: dem.flow_accumulation(fdir, [[-1, 1], [1, 1]]), "invalid_flow_weights"),
             (lambda: dem.watershed(fdir, [2, 0]), "outlet_outside_grid"),
             (lambda: dem.slope_percent(g), "grid_too_small_for_horn"),
             (lambda: dem.streams(dem.flow_accumulation(fdir), 0), "invalid_stream_threshold")]
    for call, reason in cases:
        with pytest.raises(CivilError) as caught:
            call()
        assert caught.value.reason == reason


def test_all_analysis_results_are_strict_json():
    g = valley()
    direction = dem.flow_direction_d8(g)
    accumulation = dem.flow_accumulation(direction)
    outputs = [dem.fill_depressions(g), direction, accumulation,
               dem.watershed(direction, center(g, 4, 2)), dem.streams(accumulation, 3),
               dem.slope_percent(g), dem.to_tin_points(g), dem.sample(g, *center(g, 1, 1))]
    for result in outputs:
        assert result["ok"] is True
        json.dumps(result, allow_nan=False)

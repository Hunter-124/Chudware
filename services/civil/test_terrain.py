"""Offline terrain V&V against affine integrals and prismoidal geometry.

Expected numbers are closed-form, not computed by the implementation under test.
Method references: Sloan (1993), Computers & Structures 47(3), 441–450;
Sutherland–Hodgman (1974), CACM 17(1), 32–42; LibreTexts, Fundamentals
of Transportation §7.03 Earthwork (prismoidal formula).
"""
import json
import math

import pytest
from scipy.spatial import Delaunay

from .errors import CivilError
from .terrain import (
    build_tin, contours, grade_pad, profile_along, slope_aspect,
    stage_storage, surface_sampler, volume_between, volume_to_elevation,
)


def plane(a=0.0, b=0.0, c=0.0, *, x0=0.0, y0=0.0, size=10.0, id="plane", diagonal="ac"):
    xy = [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]
    return {"id": id, "name": id, "kind": "tin",
            "points": [[x, y, a * (x - x0) + b * (y - y0) + c] for x, y in xy],
            "triangles": [[0, 1, 2], [0, 2, 3]] if diagonal == "ac" else [[0, 1, 3], [1, 2, 3]],
            "breaklines": [], "boundary": None, "source": {"kind": "points", "ref": "analytic plane"}}


def edge_set(surface):
    return {tuple(sorted((a, b))) for tri in surface["triangles"]
            for a, b in zip(tri, tri[1:] + tri[:1])}


def index_xy(surface, x, y):
    return next(i for i, p in enumerate(surface["points"])
                if p[:2] == pytest.approx([x, y], abs=1e-10))


def test_affine_tin_interpolation_exact_including_edges_and_utm_coordinates():
    # Analytic plane z=2u-3v+7 in a large projected-coordinate frame.
    x0, y0 = 500000.0, 4500000.0
    xy = [(0, 0), (10, 0), (10, 10), (0, 10), (3, 2), (7, 8)]
    tin = build_tin([[x0 + x, y0 + y, 2 * x - 3 * y + 7] for x, y in xy], id="eg", name="Survey")
    z_at = surface_sampler(tin)
    for x, y in [(0, 0), (10, 10), (5, 5), (2.2, 6.7), (0, 4), (10, 3), (3, 2)]:
        assert z_at(x0 + x, y0 + y) == pytest.approx(2 * x - 3 * y + 7, abs=2e-8)
    assert z_at(x0 - 0.01, y0 + 5) is None
    assert z_at(x0 + 5, y0 + 10.01) is None
    for a, b, c in tin["triangles"]:
        p, q, r = (tin["points"][j] for j in (a, b, c))
        assert (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]) > 0


def test_breakline_flips_the_actual_unconstrained_diagonal_without_densifying():
    # Non-cocircular quadrilateral; the independently obtained Qhull diagonal
    # is deliberately replaced by the other diagonal (Sloan edge recovery).
    points = [[0, 0, 0], [4, 0, 4], [3, 3, 6], [0, 2, 2]]
    base = Delaunay([p[:2] for p in points])
    counts = {}
    for tri in base.simplices:
        for a, b in zip(tri, list(tri[1:]) + [tri[0]]):
            edge = tuple(sorted((int(a), int(b))))
            counts[edge] = counts.get(edge, 0) + 1
    old = next(edge for edge, count in counts.items() if count == 2)
    other = tuple(i for i in range(4) if i not in old)
    tin = build_tin(points, [[points[other[0]], points[other[1]]]], id="constraint", name="Constraint")
    assert len(tin["points"]) == 4  # This cannot pass through mere densification.
    assert tuple(sorted(other)) in edge_set(tin)
    assert old not in edge_set(tin)
    assert surface_sampler(tin)(2, 1) == pytest.approx(3)


def test_inserted_ridge_is_a_hard_constraint_not_smeared_by_point_delaunay():
    corners = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]
    tin = build_tin(corners, [[[5, 0, 10], [5, 10, 10]]], id="ridge", name="Ridge")
    ridge = tuple(sorted((index_xy(tin, 5, 0), index_xy(tin, 5, 10))))
    assert ridge in edge_set(tin)
    z_at = surface_sampler(tin)
    assert z_at(5, 4) == pytest.approx(10)
    assert z_at(2.5, 4) == pytest.approx(5)
    assert z_at(7.5, 4) == pytest.approx(5)


def test_crossing_breaklines_insert_shared_vertex_and_recover_all_subedges():
    p = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]
    tin = build_tin(p, [[p[0], p[2]], [p[1], p[3]]], id="cross", name="Crossing constraints")
    center = index_xy(tin, 5, 5)
    assert len(tin["points"]) == 5
    assert all(tuple(sorted((center, i))) in edge_set(tin) for i in range(4))
    assert surface_sampler(tin)(5, 5) == 0


def test_breakline_is_split_at_an_existing_collinear_vertex():
    p = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [5, 5, 0]]
    tin = build_tin(p, [[p[0], p[2]]], id="split", name="Split constraint")
    assert (0, 4) in edge_set(tin)
    assert (2, 4) in edge_set(tin)
    assert len(tin["breaklines"][0]) == 3


def test_boundary_uses_the_documented_centroid_rule():
    points = [[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0]]
    # Centroids of a square split into two triangles have x=4/3 and 8/3.
    tin = build_tin(points, boundary=[[0, 0], [2, 0], [2, 4], [0, 4]], id="clipped", name="Centroid clip")
    assert len(tin["triangles"]) == 1
    for tri in tin["triangles"]:
        assert sum(tin["points"][j][0] for j in tri) / 3 < 2
    assert "centroid" in tin["assumptions"][0]


def test_duplicate_xy_merges_only_when_elevations_agree():
    points = [[0, 0, 0], [1, 0, 1], [0, 1, 1], [0.5e-6, 0, 0.5e-6]]
    tin = build_tin(points, id="dedupe", name="Dedupe")
    assert len(tin["points"]) == 3
    assert surface_sampler(tin)(0.2, 0.3) == pytest.approx(0.5)
    with pytest.raises(CivilError) as exc:
        build_tin(points + [[0, 0, 0.01]], id="bad", name="Bad Z")
    assert exc.value.reason == "conflicting_duplicate_points"


@pytest.mark.parametrize("points", [[], [[0, 0, 0]], [[0, 0, 0], [1, 1, 2]],
                                    [[0, 0, 0], [1, 1, 2], [2, 2, 7]],
                                    [[0, 0, 0], [0, 0, 0], [1, 0, 0]]])
def test_noncollinear_minimum_point_set_refusal(points):
    with pytest.raises(CivilError) as exc:
        build_tin(points, id="bad", name="Degenerate")
    assert exc.value.reason == "degenerate_point_set"


def test_inconsistent_crossing_breakline_z_refuses_instead_of_averaging():
    corners = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]
    with pytest.raises(CivilError) as exc:
        build_tin(corners, [[[0, 5, 0], [10, 5, 0]], [[5, 0, 1], [5, 10, 1]]], id="bad", name="Crossing Z")
    assert exc.value.reason == "conflicting_breakline_elevations"


def test_plane_contour_is_one_joined_straight_line_at_the_correct_offset():
    tin = plane(1, 2, 0)
    result = contours(tin, 10)
    at10 = next(row for row in result["contours"] if row["level_m"] == 10)
    assert len(at10["polylines"]) == 1
    line = at10["polylines"][0]
    # z=x+2y=10 meets the domain at (0,5) and (10,0), length sqrt(125).
    assert sorted([line[0][:2], line[-1][:2]]) == [[0, 5], [10, 0]]
    assert all(p[0] + 2 * p[1] == pytest.approx(10) for p in line)
    assert sum(math.dist(a[:2], b[:2]) for a, b in zip(line, line[1:])) == pytest.approx(math.sqrt(125))
    z_at = surface_sampler(tin)
    assert all(z_at(*p[:2]) == pytest.approx(10) for p in line)


def test_exact_level_vertex_and_edge_are_not_duplicated():
    # z=x-y has its zero isoline on the existing diagonal AC.
    result = contours(plane(1, -1), 10)
    zero = next(row for row in result["contours"] if row["level_m"] == 0)
    assert len(zero["polylines"]) == 1
    assert len(zero["polylines"][0]) == 2
    assert math.dist(*[p[:2] for p in zero["polylines"][0]]) == pytest.approx(math.sqrt(200))


def test_plateau_contour_has_perimeter_not_an_internal_diagonal():
    result = contours(plane(c=2), 1)
    line = result["contours"][0]["polylines"][0]
    assert line[0] == line[-1]
    assert sum(math.dist(a[:2], b[:2]) for a, b in zip(line, line[1:])) == pytest.approx(40)
    assert len(line) == 5


def test_slope_aspect_is_downhill_ccw_math_angle_not_compass_bearing():
    result = slope_aspect(plane(3, 4))
    assert result["summary"]["mean_slope_pct"] == pytest.approx(500)
    assert result["summary"]["area_m2"] == 100
    for row in result["triangles"]:
        assert row["slope_pct"] == pytest.approx(500)
        assert row["aspect_rad"] == pytest.approx(math.atan2(-4, -3) % (2 * math.pi))
    assert all(row["aspect_rad"] is None for row in slope_aspect(plane())["triangles"])


def test_two_plane_volume_is_exact_even_with_opposite_tin_diagonals():
    # Integral over 10x10 of (0.1x+0.2y+1)=100*(0.5+1+1)=250 m3.
    top = plane(0.1, 0.2, 1, id="proposed", diagonal="ac")
    bottom = plane(id="existing", diagonal="bd")
    result = volume_between(top, bottom)
    assert result["fill_m3"] == pytest.approx(250, abs=1e-9)
    assert result["cut_m3"] == pytest.approx(0)
    assert result["net_m3"] == pytest.approx(250)
    assert result["area_m2"] == pytest.approx(100)
    assert result["excluded_area_m2"] == 0
    assert result["surface_ids"] == ["proposed", "existing"]
    reverse = volume_between(bottom, top)
    assert reverse["cut_m3"] == pytest.approx(250)
    assert reverse["fill_m3"] == pytest.approx(0)


def test_crossing_planes_report_separate_cut_and_fill_not_zero_gross_volume():
    # On each half of a 10x10 square, wedge volume = 10*(5*5/2)=125.
    result = volume_between(plane(1, 0, -5, diagonal="bd"), plane())
    assert result["cut_m3"] == pytest.approx(125, abs=1e-9)
    assert result["fill_m3"] == pytest.approx(125, abs=1e-9)
    assert result["net_m3"] == pytest.approx(0, abs=1e-9)


def test_nonplanar_surface_volume_uses_facet_heights_not_a_best_fit_plane():
    # Unit square, AC diagonal, heights [0,2,0,2]: both facets have area 1/2
    # and vertex-average height 2/3, hence V=2/3 m3 (not 1 m3).
    top = plane(size=1)
    top["points"][1][2] = top["points"][3][2] = 2
    result = volume_between(top, plane(size=1, diagonal="bd"))
    assert result["fill_m3"] == pytest.approx(2 / 3)
    assert result["cut_m3"] == pytest.approx(0)


def test_concave_boundary_volume_is_clipped_without_filling_the_notch():
    polygon = [[0, 0], [10, 0], [10, 4], [4, 4], [4, 10], [0, 10]]
    result = volume_between(plane(c=1), plane(diagonal="bd"), polygon)
    assert result["area_m2"] == pytest.approx(64)
    assert result["fill_m3"] == pytest.approx(64)
    assert result["excluded_area_m2"] == 0


def test_partial_common_coverage_reports_the_missing_area():
    top, bottom = plane(c=2), plane(x0=5)
    result = volume_between(top, bottom)
    assert result["area_m2"] == pytest.approx(50)
    assert result["fill_m3"] == pytest.approx(100)
    assert result["excluded_area_m2"] == pytest.approx(100)  # Union 150 minus common 50.
    bounded = volume_between(top, bottom, [[0, 0], [10, 0], [10, 10], [0, 10]])
    assert bounded["excluded_area_m2"] == pytest.approx(50)
    assert bounded["warnings"] == ["excluded_area"]


def test_volume_to_elevation_has_explicit_datum_cut_fill_convention():
    result = volume_to_elevation(plane(c=1), 3)
    assert result["cut_m3"] == pytest.approx(200)
    assert result["fill_m3"] == pytest.approx(0)
    assert result["net_m3"] == pytest.approx(-200)


def test_pyramid_stage_storage_equals_prismoidal_formula_and_is_monotone():
    # Inverted square pyramid: rim square 2x2 at z=2, apex at z=0.
    # Area(h)=h², so V(h)=h³/3 for 0<=h<=2. Above rim add 4*(h-2).
    pond = {"id": "pyramid", "kind": "tin", "points": [[-1, -1, 2], [1, -1, 2], [1, 1, 2], [-1, 1, 2], [0, 0, 0]],
            "triangles": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]]}
    result = stage_storage(pond, [[-1, -1], [1, -1], [1, 1], [-1, 1]], [-1, 0, 1, 2, 3])
    assert [r[1] for r in result["stage_storage"]] == pytest.approx([0, 0, 1 / 3, 8 / 3, 20 / 3])
    assert all(b[1] >= a[1] for a, b in zip(result["stage_storage"], result["stage_storage"][1:]))


def test_frustum_stage_storage_equals_closed_form_cross_section_integral():
    # Flat bottom square 2x2 at z=0; top square 4x4 at z=2, straight sides.
    # A(h)=(2+h)^2, V(h)=4h+2h²+h³/3; V(2)=56/3 by prismoidal formula.
    inner = [[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]]
    outer = [[-2, -2, 2], [2, -2, 2], [2, 2, 2], [-2, 2, 2]]
    pond = build_tin(inner + outer, [inner + inner[:1]], id="frustum", name="Frustum pond")
    result = stage_storage(pond, [p[:2] for p in outer], [0, 1, 2, 3])
    assert [r[1] for r in result["stage_storage"]] == pytest.approx([0, 19 / 3, 56 / 3, 104 / 3], abs=1e-9)


@pytest.mark.parametrize("stages", [[], [1, 1], [2, 1]])
def test_stage_storage_rejects_invalid_stage_order(stages):
    with pytest.raises(CivilError) as exc:
        stage_storage(plane(), [[0, 0], [1, 0], [1, 1], [0, 1]], stages)
    assert exc.value.reason == "invalid_stages"


def test_stage_storage_refuses_missing_pond_ground_instead_of_understating_storage():
    with pytest.raises(CivilError) as exc:
        stage_storage(plane(), [[-1, 0], [10, 0], [10, 10], [-1, 10]], [1, 2])
    assert exc.value.reason == "incomplete_surface_coverage"


@pytest.mark.parametrize("pad_z, expected_kind", [(2, "fill_m3"), (-2, "cut_m3")])
def test_flat_ground_pad_grading_equals_closed_form_frustum_and_keeps_outer_ground(pad_z, expected_kind):
    # 10x10 pad, height/depth 2 m, 2H:1V: daylight square side 18 m.
    # V=h/3*(A1+A2+sqrt(A1*A2))=2/3*(100+324+180)=1208/3 m3.
    ground = plane(x0=-10, y0=-10, size=30, id="eg")
    result = grade_pad(ground, [[0, 0], [10, 0], [10, 10], [0, 10]], pad_z, 2, 2, step=5)
    assert result["volumes"][expected_kind] == pytest.approx(1208 / 3, rel=1e-7)
    other = "cut_m3" if expected_kind == "fill_m3" else "fill_m3"
    assert result["volumes"][other] == pytest.approx(0, abs=1e-8)
    assert result["volumes"]["area_m2"] == pytest.approx(900, abs=1e-7)
    assert result["volumes"]["excluded_area_m2"] == 0
    line = result["daylight_line"]
    assert line[0] == line[-1]
    assert min(p[0] for p in line) == pytest.approx(-4)
    assert max(p[0] for p in line) == pytest.approx(14)
    assert min(p[1] for p in line) == pytest.approx(-4)
    assert max(p[1] for p in line) == pytest.approx(14)
    assert all(p[2] == pytest.approx(0, abs=1e-8) for p in line)
    z_at = surface_sampler(result["surface"])
    assert z_at(5, 5) == pytest.approx(pad_z)
    assert z_at(5, -2) == pytest.approx(pad_z / 2)
    assert z_at(-8, -8) == pytest.approx(0)
    assert z_at(25, 25) is None
    # Grading feature lines must be actual edge chains, not just annotations.
    edges = edge_set(result["surface"])
    for feature in result["surface"]["breaklines"]:
        for p, q in zip(feature, feature[1:]):
            a, b = index_xy(result["surface"], *p[:2]), index_xy(result["surface"], *q[:2])
            if tuple(sorted((a, b))) not in edges:
                # A long feature segment can be split by existing ground edges.
                along = [i for i, r in enumerate(result["surface"]["points"])
                         if abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) < 1e-7
                         and (r[0] - p[0]) * (r[0] - q[0]) + (r[1] - p[1]) * (r[1] - q[1]) <= 1e-8]
                along.sort(key=lambda i: math.dist(p[:2], result["surface"]["points"][i][:2]))
                assert all(tuple(sorted((u, v))) in edges for u, v in zip(along, along[1:]))


def test_pad_daylight_follows_ground_facet_changes_without_an_incompatible_seam():
    # Existing ridge: z=0.1*abs(x) on a surveyed square. A 4x4 pad at z=2
    # crosses both ground planes. Every daylight segment must lie on ground;
    # unchanged coverage and exact pad elevations must survive the merge.
    ground = build_tin([[-10, -10, 1], [10, -10, 1], [10, 10, 1], [-10, 10, 1]],
                       [[[0, -10, 0], [0, 10, 0]]], id="ridge", name="Two-facet ground")
    result = grade_pad(ground, [[-2, -2], [2, -2], [2, 2], [-2, 2]], 2, 2, 2, step=2)
    z_at = surface_sampler(ground)
    for a, b in zip(result["daylight_line"], result["daylight_line"][1:]):
        assert z_at(a[0], a[1]) == pytest.approx(a[2], abs=1e-7)
        midpoint = [(a[k] + b[k]) / 2 for k in range(3)]
        assert z_at(*midpoint[:2]) == pytest.approx(midpoint[2], abs=1e-7)
    graded_at = surface_sampler(result["surface"])
    assert graded_at(0, 0) == pytest.approx(2)
    assert graded_at(9, 9) == pytest.approx(0.9)
    assert result["volumes"]["area_m2"] == pytest.approx(400, abs=1e-7)
    assert result["volumes"]["excluded_area_m2"] == 0


def test_pad_cannot_fake_daylight_beyond_survey_coverage():
    with pytest.raises(CivilError) as exc:
        grade_pad(plane(size=10), [[1, 1], [9, 1], [9, 9], [1, 9]], 5, 2, 2, step=4)
    assert exc.value.reason == "daylight_not_found"


def test_profile_stationing_preserves_real_gaps_and_polyline_corners():
    first = plane(1, size=1)
    second = plane(1, x0=3, size=1)
    second["points"] = [[p[0], p[1], p[2] + 3] for p in second["points"]]
    surface = {"kind": "tin", "points": first["points"] + second["points"],
               "triangles": first["triangles"] + [[j + 4 for j in tri] for tri in second["triangles"]]}
    result = profile_along(surface, [[-1, 0.5], [4, 0.5], [4, 1]], 1)
    rows = result["samples"]
    assert [r["station_m"] for r in rows] == [0, 1, 2, 3, 4, 5, 5.5]
    assert [r["z_m"] for r in rows] == [None, 0, 1, None, 3, 4, 4]
    assert result["length_m"] == 5.5
    assert result["warnings"] == ["surface_coverage_gaps"]


def test_public_analysis_outputs_serialize_finite_numbers_and_nulls():
    tin = plane(1, -1)
    outputs = [contours(tin, 2), slope_aspect(tin), volume_between(tin, plane()),
               volume_to_elevation(tin, 1), stage_storage(tin, [[0, 0], [1, 0], [1, 1], [0, 1]], [0, 1]),
               profile_along(tin, [[-1, 0], [1, 0]], 0.5)]
    for output in outputs:
        json.dumps(output, allow_nan=False)
        assert output["ok"] is True
        assert {"warnings", "assumptions", "method"} <= output.keys()


@pytest.mark.parametrize("function,args,reason", [
    (contours, (plane(), 0), "invalid_contour_interval"),
    (profile_along, (plane(), [[0, 0], [1, 0]], -1), "invalid_profile_step"),
    (volume_to_elevation, (plane(), float("nan")), "invalid_elevation"),
    (volume_between, (plane(), plane(), [[0, 0], [2, 2], [0, 2], [2, 0]]), "invalid_boundary"),
    (grade_pad, (plane(), [[1, 1], [2, 1], [2, 2], [1, 2]], 1, 0, 2), "invalid_grading_slope"),
])
def test_invalid_geometry_or_parameters_have_named_refusals(function, args, reason):
    with pytest.raises(CivilError) as exc:
        function(*args)
    assert exc.value.reason == reason


def test_mesh_diagnostics_identify_gap_edges_and_skinny_facets_with_explicit_thresholds():
    # Two rows 0.1 m apart, with a 19.9 m unsupported span: the long-gap
    # triangles have atan(0.1/19.9)=0.005025 rad minimum angle (<1 degree).
    points = [[x, y, 0] for x in (0, 0.1, 20) for y in (0, 0.1)]
    tin = build_tin(points, id="gap", name="Survey gap")
    diagnostics = tin["diagnostics"]
    assert max(row["length_m"] for row in diagnostics["long_edges"]) >= 19.9
    assert min(row["min_angle_rad"] for row in diagnostics["slivers"]) < math.atan(0.1 / 19)
    assert diagnostics["sliver_angle_threshold_rad"] == pytest.approx(math.pi / 180)
    assert {"long_edges", "slivers"} <= set(tin["warnings"])


def test_boundary_does_not_persist_a_breakline_whose_triangles_were_removed():
    points = [[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0]]
    tin = build_tin(points, [[points[1], points[2]]],
                    boundary=[[0, 0], [2, 0], [2, 4], [0, 4]], id="clip", name="Clip")
    assert tin["breaklines"] == []
    assert "breaklines_clipped_by_boundary" in tin["warnings"]
    assert len(tin["triangles"]) == 1


def test_degenerate_breakline_and_noniterable_triangles_have_named_refusals():
    with pytest.raises(CivilError) as exc:
        build_tin([[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                  [[[0, 0, 0], [0, 0, 0]]], id="bad", name="Bad line")
    assert exc.value.reason == "invalid_breakline"
    with pytest.raises(CivilError) as exc:
        surface_sampler({"kind": "tin", "points": [[0, 0, 0]], "triangles": None})
    assert exc.value.reason == "invalid_triangle"

"""Offline, independent-oracle civil alignment V&V (no daemon/reference engine).

Sources: Euler spiral integrals (Levien, UCB/EECS-2008-111, 2008); AASHTO
Green Book 2018 ch.3/Table 3-1; TxDOT Vertical Alignment; LibreTexts §7.3.
Numbers below are hand/closed-form calculations, not fixtures from this engine.
"""

import json
import math

import pytest

from services.civil import alignment as align
from services.civil.errors import CivilError


def _flat_alignment(size=100.0, elevation=2.0, end_elevation=None):
    end_elevation = elevation if end_elevation is None else end_elevation
    return align.build([{"type": "line", "start": [0, 0], "end": [size, 0]}],
                       {"pvis": [{"station": 0, "elevation": elevation, "curve_length": 0},
                                 {"station": size, "elevation": end_elevation, "curve_length": 0}]})


def _vertical_example(sag=False, curve_length=200):
    # g1=+3%, g2=-2%, PVC=100/z=103; reverse grades/elevations for a sag.
    elevations = [100, 94, 98] if sag else [100, 106, 102]
    return align.build([{"type": "line", "start": [0, 0], "end": [400, 0]}],
                       {"pvis": [{"station": s, "elevation": z, "curve_length": curve_length if s == 200 else 0}
                                 for s, z in zip([0, 200, 400], elevations)]})


def _template(width=3.0, cross_slope=0.0, slope=2.0, depth=0.0):
    return {"width_left": width, "width_right": width, "cross_slope_pct": cross_slope,
            "cut_slope": slope, "fill_slope": slope, "depth_below_finish": depth}


def _clothoid_series(length, radius, terms=6):
    # Integrate the Taylor series of cos(s²/(2RL)) and sin(s²/(2RL)).
    # Six terms; independent of scipy.special.fresnel and the implementation.
    angle = length / (2 * radius)
    x = length * sum((-1) ** n * angle ** (2 * n) / ((4 * n + 1) * math.factorial(2 * n))
                     for n in range(terms))
    y = length * sum((-1) ** n * angle ** (2 * n + 1) / ((4 * n + 3) * math.factorial(2 * n + 1))
                     for n in range(terms))
    return x, y


def _general_spiral_series(length, k0, k1):
    # exp(i*k0*s+i*a*s²/2): n*c_n=i*k0*c_(n-1)+i*a*c_(n-2).
    # Integrate its 40-term power series; parameters here have total turn <0.4 rad.
    a = (k1 - k0) / length
    coefficients = [1 + 0j]
    for n in range(1, 40):
        coefficients.append((1j * k0 * coefficients[n - 1]
                             + (1j * a * coefficients[n - 2] if n >= 2 else 0)) / n)
    return sum(c * length ** (n + 1) / (n + 1) for n, c in enumerate(coefficients))


def _element_lengths(al):
    # Each individual contract element is independently usable as an alignment.
    return [align.length({"start_station": 0.0, "horizontal": [element], "vertical": None})
            for element in al["horizontal"]]


def test_circular_pi_curve_tangent_and_arc_length():
    # Highway curve T=R*tan(delta/2), L=R*delta; 90-degree CCW turn, R=50 m.
    al = align.from_pis([[0, 0], [200, 0], [200, 200]], [50])
    sizes = _element_lengths(al)
    assert sizes == pytest.approx([150, 25 * math.pi, 150], abs=1e-10)
    assert al["horizontal"][1]["start"] == pytest.approx([150, 0])
    assert al["horizontal"][1]["end"] == pytest.approx([200, 50])
    assert al["horizontal"][1]["center"] == pytest.approx([150, 50])
    assert align.length(al) == pytest.approx(300 + 25 * math.pi)
    assert align.point_at(al, 150 + 25 * math.pi) == pytest.approx([200, 50, math.pi / 2])


@pytest.mark.parametrize("direction, sign", [("ccw", 1), ("cw", -1)])
def test_clothoid_fresnel_endpoint_against_six_term_series(direction, sign):
    al = align.build([{"type": "spiral", "start": [0, 0], "start_heading": 0,
                       "length": 100, "radius_start": None, "radius_end": 200, "dir": direction}])
    expected_x, expected_y = _clothoid_series(100, 200)
    # The rough x≈99.75/y≈8.33 estimate neglects non-negligible Taylor terms.
    # The six-term integral gives x≈99.3768 m and |y|≈8.2962 m.
    assert expected_x == pytest.approx(99.3768, abs=0.0001)
    assert expected_y == pytest.approx(8.2962, abs=0.0001)
    assert align.point_at(al, 100) == pytest.approx([expected_x, sign * expected_y, sign * 0.25], abs=1e-10)


@pytest.mark.parametrize("r0,r1", [(200, 100), (100, 200), (200, 200), (200, 200.000001)])
def test_general_spiral_nonzero_initial_curvature_and_near_equal_radii(r0, r1):
    length, heading = 30.0, 0.4
    al = align.build([{"type": "spiral", "start": [10, -7], "start_heading": heading,
                       "length": length, "radius_start": r0, "radius_end": r1, "dir": "ccw"}])
    local = _general_spiral_series(length, 1 / r0, 1 / r1)
    expected = complex(10, -7) + local * complex(math.cos(heading), math.sin(heading))
    assert align.point_at(al, length) == pytest.approx(
        [expected.real, expected.imag, heading + length * (1 / r0 + 1 / r1) / 2], abs=1e-9)
    px, py, theta = align.point_at(al, 17.5)
    assert align.station_offset(al, px - 2 * math.sin(theta), py + 2 * math.cos(theta)) == pytest.approx([17.5, 2], abs=2e-6)


@pytest.mark.parametrize("sign, spirals", [(1, [40]), (-1, [40]), (1, [[25, 55]]), (-1, [[25, 55]])])
def test_pi_spiral_tangent_shifts_and_all_join_continuities(sign, spirals):
    al = align.from_pis([[0, 0], [500, 0], [500, sign * 500]], [200], spirals)
    assert [e["type"] for e in al["horizontal"]] == ["line", "spiral", "arc", "spiral", "line"]
    assert align.point_at(al, align.length(al)) == pytest.approx([500, sign * 500, sign * math.pi / 2], abs=1e-9)
    if spirals == [40]:
        xs, ys = _clothoid_series(40, 200)
        theta_s = 40 / (2 * 200)
        p = ys - 200 * (1 - math.cos(theta_s))
        q = xs - 200 * math.sin(theta_s)
        tangent = (200 + p) * math.tan(math.pi / 4) + q
        assert al["horizontal"][0]["end"][0] == pytest.approx(500 - tangent, abs=1e-9)
        assert al["horizontal"][-1]["start"][1] == pytest.approx(sign * tangent, abs=1e-9)
    station = 0.0
    epsilon = 1e-4
    for size in _element_lengths(al)[:-1]:
        station += size
        before = align.point_at(al, station - epsilon)
        at = align.point_at(al, station)
        after = align.point_at(al, station + epsilon)
        assert math.dist(before[:2], after[:2]) == pytest.approx(2 * epsilon, abs=1e-8)
        assert before[2] == pytest.approx(after[2], abs=2e-6)
        left_k = (at[2] - before[2]) / epsilon
        right_k = (after[2] - at[2]) / epsilon
        assert left_k == pytest.approx(right_k, abs=1e-7)
    assert align.validate(al) == []


@pytest.mark.parametrize("station,offset", [(20, 7), (305, -3), (340, 4), (460, 5), (650, -2)])
def test_station_offset_inverse_across_tangent_spiral_arc(station, offset):
    al = align.from_pis([[0, 0], [500, 0], [500, 500]], [200], [40])
    x, y, theta = align.point_at(al, station)
    assert align.station_offset(al, x - offset * math.sin(theta), y + offset * math.cos(theta)) == pytest.approx(
        [station, offset], abs=1e-5)


def test_projection_global_nearest_and_earliest_self_intersection():
    al = align.build([{"type": "line", "start": [-10, -10], "end": [10, 10]},
                      {"type": "line", "start": [10, 10], "end": [-10, 10]},
                      {"type": "line", "start": [-10, 10], "end": [10, -10]}], start_station=1000)
    assert align.station_offset(al, 0, 0) == pytest.approx([1000 + math.sqrt(200), 0])
    assert align.station_offset(al, 0, 9) == pytest.approx([1000 + math.sqrt(800) + 10, 1])


def test_nonzero_station_origin_and_exact_endpoints_in_densification():
    al = align.from_pis([[0, 0], [200, 0], [200, 200]], [50], start_station=1_000_000)
    result = align.densify(al, step=37)
    assert result["points"][0] == [0, 0]
    assert result["points"][-1] == pytest.approx([200, 200], abs=1e-9)
    assert result["stations_m"][-1] == 1_000_000 + align.length(al)
    assert max(b - a for a, b in zip(result["stations_m"], result["stations_m"][1:])) <= 37
    assert 1_000_150 in result["stations_m"]
    assert align.point_at(al, result["stations_m"][-1])[:2] == pytest.approx([200, 200])


def test_multiple_pi_curves_use_shared_tangent_not_original_pi_polyline():
    al = align.from_pis([[0, 0], [300, 0], [300, 300], [600, 300]], [50, 75], [20, 30])
    assert align.validate(al) == []
    assert align.point_at(al, align.length(al))[:2] == pytest.approx([600, 300])
    middle_line = [e for e in al["horizontal"] if e["type"] == "line"][1]
    assert middle_line["start"][0] == pytest.approx(300)
    assert middle_line["end"][0] == pytest.approx(300)
    assert middle_line["end"][1] > middle_line["start"][1]


@pytest.mark.parametrize("sag,expected_z,kind", [(False, 104.8, "high"), (True, 95.2, "low")])
def test_textbook_vertical_curve_extremum_and_closed_form_elevation(sag, expected_z, kind):
    al = _vertical_example(sag)
    curve = align.vertical_curves(al)["curves"][0]
    assert curve["k_m_per_pct"] == pytest.approx(200 / 5)
    assert curve[f"{kind}_point_station_m"] == pytest.approx(100 + 0.03 * 200 / (0.03 + 0.02))
    assert align.grade(al, 220) == pytest.approx(0, abs=1e-14)
    assert align.elevation_at(al, 220) == pytest.approx(expected_z)
    if not sag:
        assert align.elevation_at(al, 200) == pytest.approx(103 + 0.03 * 100 - 0.05 * 100 ** 2 / 400)
        assert align.elevation_at(al, 300) == pytest.approx(104)
        assert align.grade(al, 100) == pytest.approx(0.03)
        assert align.grade(al, 300) == pytest.approx(-0.02)


def test_vertical_tangent_elevation_and_grade_continuity_at_pvc_pvt():
    al = _vertical_example()
    for station in (100, 300):
        epsilon = 1e-5
        left = align.elevation_at(al, station - epsilon)
        right = align.elevation_at(al, station + epsilon)
        assert (right - left) / (2 * epsilon) == pytest.approx(align.grade(al, station), abs=2e-8)
        assert align.grade(al, station - epsilon) == pytest.approx(align.grade(al, station + epsilon), abs=1e-8)


def test_ssd_at_100_kmh_green_book_table_3_1_and_crest_sag_k():
    crest = align.vertical_curve_checks(_vertical_example(), 100)
    # Green Book 2018 SI Table 3-1 design SSD 185 m; unrounded equation 184.205882 m.
    assert crest["computed_ssd_m"] == pytest.approx(0.278 * 100 * 2.5 + 0.039 * 10000 / 3.4)
    assert crest["stopping_sight_distance_m"] == 185
    assert crest["curves"][0]["required_k_m_per_pct"] == pytest.approx(185 ** 2 / 658)
    assert crest["curves"][0]["required_length_m"] == pytest.approx(5 * 185 ** 2 / 658)
    assert crest["checks"][0]["pass"] is False
    sag = align.vertical_curve_checks(_vertical_example(True), 100)
    assert sag["curves"][0]["required_k_m_per_pct"] == pytest.approx(185 ** 2 / (120 + 3.5 * 185))
    assert sag["checks"][0]["pass"] is False


def test_short_crest_sight_distance_branch_not_long_curve_k_only():
    al = align.build([{"type": "line", "start": [0, 0], "end": [200, 0]}],
                     {"pvis": [{"station": 0, "elevation": 0, "curve_length": 0},
                               {"station": 100, "elevation": 1, "curve_length": 50},
                               {"station": 200, "elevation": 0, "curve_length": 0}]})
    result = align.vertical_curve_checks(al, 100)
    # A=2%, S=185 > L: Lmin=2*185-658/2=41, Kmin=20.5; actual K=25.
    assert result["curves"][0]["sight_distance_branch"] == "S>L"
    assert result["curves"][0]["required_length_m"] == pytest.approx(41)
    assert result["checks"][0]["pass"] is True


def test_explicit_vertical_k_criteria_override_changes_check():
    result = align.vertical_curve_checks(_vertical_example(), 100,
                                         {"crest_k_m_per_pct": 35, "references": {"crest_k_m_per_pct": "Explicit project criterion"}})
    assert result["checks"][0]["pass"] is True
    assert result["checks"][0]["limit"] == 35
    assert result["checks"][0]["reference"] == "Explicit project criterion"


def test_ground_profile_missing_coverage_does_not_invent_elevations():
    al = _flat_alignment(20, 2)
    ground = lambda x, y: 3 if x < 15 else None
    result = align.design_vs_existing(al, ground, step=10)
    assert [r["station_m"] for r in result["samples"]] == [0, 10, 20]
    assert result["samples"][0]["cut_depth_m"] == 1
    assert result["samples"][0]["fill_depth_m"] == 0
    assert result["samples"][-1]["ground_elevation_m"] is None
    assert result["samples"][-1]["cut_depth_m"] is None
    assert result["warnings"]


def test_earthwork_uniformly_sloped_ground_closed_form_trapezoid():
    result = align.cross_sections(_flat_alignment(10, 2), lambda x, y: 0.1 * y, _template(), [0, 10])
    # Platform integral: 6*2=12. Left/right side triangles: h²/(2*(slope-ground_grade)).
    expected_fill = 12 + 1.7 ** 2 / (2 * 0.6) + 2.3 ** 2 / (2 * 0.4)
    for section in result["sections"] + result["mid_sections"]:
        assert section["cut_area_m2"] == pytest.approx(0, abs=1e-8)
        assert section["fill_area_m2"] == pytest.approx(expected_fill, abs=1e-7)
        assert section["daylight_left_offset_m"] == pytest.approx(3 + 1.7 / 0.6, abs=1e-7)
        assert section["daylight_right_offset_m"] == pytest.approx(-3 - 2.3 / 0.4, abs=1e-7)
    volumes = align.earthwork(result)
    assert volumes["total_fill_m3"] == pytest.approx(10 * expected_fill, abs=1e-6)
    assert volumes["total_cut_m3"] == pytest.approx(0, abs=1e-8)


def test_sections_mixed_cut_and_fill_are_not_cancelled():
    result = align.cross_sections(_flat_alignment(10, 0), lambda x, y: 0.5 * y,
                                 _template(width=2, slope=1), [0, 10])
    # Each side: inner triangle 0.5*2*1 + daylight triangle 0.5*2*1 = 2 m².
    section = result["sections"][0]
    assert section["daylight_left_offset_m"] == pytest.approx(4, abs=1e-7)
    assert section["daylight_right_offset_m"] == pytest.approx(-4, abs=1e-7)
    assert section["cut_area_m2"] == pytest.approx(2, abs=1e-7)
    assert section["fill_area_m2"] == pytest.approx(2, abs=1e-7)
    volumes = align.earthwork(result)
    assert volumes["total_cut_m3"] == pytest.approx(20, abs=1e-6)
    assert volumes["total_fill_m3"] == pytest.approx(20, abs=1e-6)
    assert volumes["net_bank_m3"] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("elevation,ground,depth,expected_cut,expected_fill", [
    (0, 2, 0, 20, 0), (2, 0, 1, 0, 8), (2, 0, 0, 0, 20)])
def test_flat_ground_section_trapezoids_and_subgrade_depth(elevation, ground, depth, expected_cut, expected_fill):
    section = align.cross_sections(_flat_alignment(5, elevation), lambda x, y: ground,
                                   _template(depth=depth), [0, 5])["sections"][0]
    assert section["cut_area_m2"] == pytest.approx(expected_cut, abs=1e-7)
    assert section["fill_area_m2"] == pytest.approx(expected_fill, abs=1e-7)


def test_positive_crown_cross_slope_lowers_both_platform_edges():
    section = align.cross_sections(_flat_alignment(5, 2), lambda x, y: 0,
                                   _template(cross_slope=10), [0, 5])["sections"][0]
    # Six-metre crown mean height 1.85 -> 11.1; two triangular slopes total 2*1.7²=5.78.
    assert section["fill_area_m2"] == pytest.approx(11.1 + 5.78, abs=1e-7)
    assert section["daylight_left_offset_m"] == pytest.approx(6.4, abs=1e-7)
    assert section["daylight_right_offset_m"] == pytest.approx(-6.4, abs=1e-7)


def test_pyramid_prismoidal_exact_and_average_end_area_fifty_percent_overestimate():
    # Inverted-V embankment on flat ground: half-width=3*h, h=2*(1-x/3).
    # A(x)=3*h²=12*(1-x/3)², volume=12*3/3=12 m³ (a pyramid).
    sections = align.cross_sections(_flat_alignment(3, 2, 0), lambda x, y: 0,
                                    _template(width=0, slope=3), [0, 3])
    assert sections["sections"][0]["fill_area_m2"] == pytest.approx(12, abs=1e-7)
    assert sections["mid_sections"][0]["fill_area_m2"] == pytest.approx(3, abs=1e-7)
    assert sections["sections"][1]["fill_area_m2"] == 0
    aea = align.earthwork(sections)
    prism = align.earthwork(sections, "prismoidal")
    assert aea["total_fill_m3"] == pytest.approx(18, abs=1e-7)
    assert prism["total_fill_m3"] == pytest.approx(12, abs=1e-7)
    assert aea["total_fill_m3"] / prism["total_fill_m3"] == pytest.approx(1.5)


def test_material_shrink_and_swell_bank_mass_conservation():
    sections = {"sections": [{"station_m": 0, "cut_area_m2": 3, "fill_area_m2": 1},
                              {"station_m": 10, "cut_area_m2": 3, "fill_area_m2": 1}]}
    result = align.earthwork(sections, shrink=0.8, swell=1.2)
    assert result["total_cut_m3"] == 30
    assert result["total_fill_m3"] == 10
    assert result["total_fill_bank_m3"] == 12.5
    assert result["total_cut_loose_m3"] == 36
    assert result["total_fill_loose_m3"] == 15
    assert result["net_bank_m3"] == 17.5
    assert result["mass_haul"]["waste_bank_m3"] == 17.5
    assert result["mass_ordinates"][-1]["mass_ordinate_m3"] == 17.5


def test_mass_haul_finds_within_interval_balance_and_extremum():
    sections = [{"station_m": 0, "cut_area_m2": 4, "fill_area_m2": 0},
                {"station_m": 10, "cut_area_m2": 0, "fill_area_m2": 8}]
    result = align.earthwork(sections)
    # a(x)=4-1.2*x; M(x)=4*x-0.6*x², root 20/3 and maximum at 10/3.
    mass = result["mass_haul"]
    assert mass["balance_points_m"] == pytest.approx([0, 20 / 3])
    assert mass["turning_points"][0]["station_m"] == pytest.approx(10 / 3)
    assert mass["turning_points"][0]["mass_ordinate_m3"] == pytest.approx(20 / 3)
    assert mass["borrow_bank_m3"] == 20


def test_mass_haul_repeated_zero_balances_and_nonzero_internal_lobes():
    sections = [{"station_m": 0, "cut_area_m2": 4, "fill_area_m2": 0},
                {"station_m": 10, "cut_area_m2": 0, "fill_area_m2": 4},
                {"station_m": 20, "cut_area_m2": 4, "fill_area_m2": 0}]
    mass = align.earthwork(sections)["mass_haul"]
    assert mass["balance_points_m"] == pytest.approx([0, 10, 20])
    assert [row["station_m"] for row in mass["turning_points"]] == pytest.approx([5, 15])
    assert [row["mass_ordinate_m3"] for row in mass["turning_points"]] == pytest.approx([10, -10])


def test_analysis_results_are_json_serializable_with_cited_numerical_values():
    al = _flat_alignment(3, 2, 0)
    sections = align.cross_sections(al, lambda x, y: 0, _template(width=0, slope=3), [0, 3])
    quantities = align.earthwork(sections, "prismoidal")
    assert quantities["total_fill_m3"] == pytest.approx(12, abs=1e-7)
    restored = json.loads(json.dumps({"sections": sections, "quantities": quantities}, allow_nan=False))
    assert restored["quantities"]["intervals"][0]["fill_m3"] == pytest.approx(12, abs=1e-7)
    assert restored["quantities"]["method"]["reference"].startswith("LibreTexts")


@pytest.mark.parametrize("pis,radii,spirals,reason", [
    ([[0, 0], [10, 0], [10, 10]], [20], None, "pi_curves_overlap"),
    ([[0, 0], [10, 0], [0, 0]], [2], None, "pi_deflection_invalid"),
    ([[0, 0], [100, 0], [100, 100]], [20], [100], "spirals_exceed_deflection"),
    ([[0, 0], [10, 0], [20, 0]], [2], [1], "spiral_without_deflection"),
    ([[0, 0], [10, 0], [10, 10]], [], None, "pi_radius_count"),
    ([[0, 0], [0, 0]], [], None, "duplicate_pi")])
def test_pi_construction_refusals_have_exact_reasons(pis, radii, spirals, reason):
    with pytest.raises(CivilError) as failure:
        align.from_pis(pis, radii, spirals)
    assert failure.value.reason == reason


def test_contract_contiguity_and_radius_mismatch_refusals():
    bad = {"start_station": 0, "horizontal": [{"type": "line", "start": [0, 0], "end": [1, 0]},
                                              {"type": "line", "start": [2, 0], "end": [3, 0]}]}
    assert align.validate(bad) == ["horizontal_not_contiguous"]
    with pytest.raises(CivilError) as failure:
        align.build([{"type": "arc", "start": [1, 0], "end": [0, 2], "center": [0, 0], "radius": 1, "dir": "ccw"}])
    assert failure.value.reason == "arc_radius_mismatch"


def test_spiral_explicit_endpoint_is_checked_against_integral():
    with pytest.raises(CivilError) as failure:
        align.build([{"type": "spiral", "start": [0, 0], "end": [100, 0], "start_heading": 0,
                      "length": 100, "radius_start": None, "radius_end": 200, "dir": "ccw"}])
    assert failure.value.reason == "spiral_endpoint_mismatch"


def test_vertical_curve_overlap_and_endpoint_curve_refusals():
    horizontal = [{"type": "line", "start": [0, 0], "end": [400, 0]}]
    pvis = [{"station": 0, "elevation": 0, "curve_length": 0},
            {"station": 100, "elevation": 3, "curve_length": 180},
            {"station": 200, "elevation": 1, "curve_length": 180},
            {"station": 400, "elevation": 2, "curve_length": 0}]
    with pytest.raises(CivilError) as failure:
        align.build(horizontal, {"pvis": pvis})
    assert failure.value.reason == "vertical_curves_overlap"
    pvis[0]["curve_length"] = 1
    with pytest.raises(CivilError) as failure:
        align.build(horizontal, {"pvis": pvis})
    assert failure.value.reason == "endpoint_vertical_curve"


@pytest.mark.parametrize("function,reason", [(lambda al: align.point_at(al, -0.001), "station_outside_alignment"),
                                             (lambda al: align.elevation_at(al, 101), "station_outside_vertical"),
                                             (lambda al: align.densify(al, 0), "invalid_number"),
                                             (lambda al: align.grade(al, math.nan), "invalid_number")])
def test_station_bounds_and_nonfinite_input_refusals(function, reason):
    with pytest.raises(CivilError) as failure:
        function(_flat_alignment())
    assert failure.value.reason == reason


def test_cross_sections_refuse_no_daylight_and_missing_surface_coverage():
    al = _flat_alignment(10, 1)
    with pytest.raises(CivilError) as failure:
        align.cross_sections(al, lambda x, y: -2 * abs(y), _template(width=1), [0, 10], max_daylight=5)
    assert failure.value.reason == "daylight_not_found"
    with pytest.raises(CivilError) as failure:
        align.cross_sections(al, lambda x, y: 0 if abs(y) <= 2 else None, _template(width=1), [0, 10])
    assert failure.value.reason == "surface_outside"


def test_prismoidal_refuses_invented_or_wrong_midpoint_sections():
    sections = [{"station_m": 0, "cut_area_m2": 0, "fill_area_m2": 12},
                {"station_m": 3, "cut_area_m2": 0, "fill_area_m2": 0}]
    with pytest.raises(CivilError) as failure:
        align.earthwork(sections, "prismoidal")
    assert failure.value.reason == "midpoint_section_required"
    with pytest.raises(CivilError) as failure:
        align.earthwork({"sections": sections, "mid_sections": [{"station_m": 1, "cut_area_m2": 0, "fill_area_m2": 3}]}, "prismoidal")
    assert failure.value.reason == "midpoint_station_mismatch"


@pytest.mark.parametrize("kwargs,reason", [({"shrink": 0}, "invalid_number"),
                                          ({"swell": -1}, "invalid_number"),
                                          ({"method": "approximate_midpoint"}, "unsupported_earthwork_method")])
def test_earthwork_material_and_method_refusals(kwargs, reason):
    sections = [{"station_m": 0, "cut_area_m2": 1, "fill_area_m2": 0},
                {"station_m": 3, "cut_area_m2": 1, "fill_area_m2": 0}]
    with pytest.raises(CivilError) as failure:
        align.earthwork(sections, **kwargs)
    assert failure.value.reason == reason


def test_vertical_k_override_inside_shared_profile_preserves_standard_envelope():
    result = align.vertical_curve_checks(
        _vertical_example(), 100,
        {"profile": "ten_states_2014", "overrides": {
            "crest_k_m_per_pct": 35, "references": {"crest_k_m_per_pct": "Project geometric design basis"}}})
    assert result["curves"][0]["required_length_m"] == 175
    assert result["checks"][0]["pass"] is True
    assert result["checks"][0]["reference"] == "Project geometric design basis"


@pytest.mark.parametrize("criteria,reason", [
    ({"crest_k_m_per_pct": -1}, "invalid_number"),
    ({"ssd_m": 0}, "invalid_number"),
    ({"unrecognized_k": 10}, "unknown_criteria_key"),
    ({"references": []}, "invalid_criteria")])
def test_vertical_criteria_refuses_invalid_or_unrecognized_limits(criteria, reason):
    with pytest.raises(CivilError) as failure:
        align.vertical_curve_checks(_vertical_example(), 100, criteria)
    assert failure.value.reason == reason


def test_mass_haul_refuses_a_nonconserving_external_interval():
    interval = {"start_station_m": 0, "end_station_m": 10,
                "mass_start_m3": 0, "mass_end_m3": 20,
                "net_area_polynomial_m2": [1, 0, 0]}
    with pytest.raises(CivilError) as failure:
        align.mass_haul({"intervals": [interval]})
    assert failure.value.reason == "mass_curve_volume_mismatch"


def test_section_quantities_retain_alignment_element_trace():
    al = _flat_alignment(3, 2, 0)
    al["id"] = "road-centreline"
    sections = align.cross_sections(al, lambda x, y: 0, _template(width=0, slope=3), [0, 3])
    result = align.earthwork(sections, "prismoidal")
    assert result["total_fill_m3"] == pytest.approx(12, abs=1e-7)
    assert result["element_ids"] == ["road-centreline"]


def test_mass_curve_requires_its_documented_zero_initial_ordinate():
    interval = {"start_station_m": 0, "end_station_m": 10,
                "mass_start_m3": 5, "mass_end_m3": 15,
                "net_area_polynomial_m2": [1, 0, 0]}
    with pytest.raises(CivilError) as failure:
        align.mass_haul({"intervals": [interval]})
    assert failure.value.reason == "mass_curve_initial_ordinate_not_zero"

"""Deterministic hand-equation and conservation V&V; no external engine daemon.

Numerical sources are cited beside each independent expectation. These tests
compare units, physical invariants and named refusals, not just result wiring.
"""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from services.civil.errors import CivilError
from services.civil import hydrology as h


def refused(reason, function, *args, **kwargs):
    with pytest.raises(CivilError) as error:
        function(*args, **kwargs)
    assert error.value.reason == reason


def test_rational_ten_hectare_si_conversion():
    # HDS-2 §5.3, Q=C i A: 10 ha=100000 m², mm/h→m/s divides by 3.6e6.
    result = h.rational_peak(0.5, 50, 100000)
    assert result["flow_m3s"] == pytest.approx(0.6944444444444444, rel=1e-12)
    assert h.rational_peak(0, 50, 100000)["flow_m3s"] == 0
    refused("invalid_runoff_coefficient", h.rational_peak, 1.01, 50, 100000)
    refused("invalid_parameter", h.rational_peak, 0.5, float("nan"), 100000)


def test_area_weighted_composites_not_unweighted_means():
    assert h.composite_c([{"area_m2": 100, "c": 0.2}, {"area_m2": 300, "c": 0.8}])["c"] == pytest.approx(0.65)
    assert h.composite_cn([{"area_m2": 100, "cn": 50}, {"area_m2": 300, "cn": 90}])["cn"] == 80
    refused("invalid_composite", h.composite_cn, [{"area_m2": 1, "cn": 0}])
    refused("invalid_composite", h.composite_c, [])


def test_cn_closed_form_initial_abstraction_and_impervious_boundary():
    # TR-55 chapter 2 eqs. 2-1/2-2. The assignment's ≈69.6 mm is a typo;
    # P=125,CN=80,Ia/S=.2 actually gives 71.73657565415243 mm.
    storage = 25400 / 80 - 254
    expected = (125 - 0.2 * storage) ** 2 / (125 + 0.8 * storage)
    result = h.scs_runoff_depth(125, 80)
    assert result["storage_mm"] == 63.5
    assert result["runoff_mm"] == pytest.approx(expected, rel=1e-12)
    assert result["runoff_mm"] == pytest.approx(71.73657565415243)
    assert h.scs_runoff_depth(12.7, 80)["runoff_mm"] == 0
    assert h.scs_runoff_depth(0, 100)["runoff_mm"] == 0
    assert h.scs_runoff_depth(125, 100)["runoff_mm"] == 125
    alternate = h.scs_runoff_depth(125, 80, ia_ratio=0.05)
    assert alternate["runoff_mm"] == pytest.approx((125 - 0.05 * storage) ** 2 / (125 + 0.95 * storage))
    assert alternate["runoff_mm"] > expected
    refused("invalid_curve_number", h.scs_runoff_depth, 125, 101)
    refused("invalid_parameter", h.scs_runoff_depth, 125, 0)


def test_idf_two_dimensional_log_interpolation_and_no_extrapolation():
    table = {"durations_s": [300, 1200], "return_periods_yr": [2, 8],
             "intensities_mm_h": [[100, 200], [50, 100]]}
    # Both coordinates are geometric midpoints; intensity geometric mean=100.
    assert h.idf_intensity(table, 600, 4)["intensity_mm_h"] == pytest.approx(100)
    assert h.idf_intensity(table, 300, 2)["intensity_mm_h"] == pytest.approx(100)
    assert h.idf_intensity(table, 1200, 8)["intensity_mm_h"] == pytest.approx(100)
    refused("outside_idf_table", h.idf_intensity, table, 299, 4)
    refused("outside_idf_table", h.idf_intensity, table, 600, 8.1)
    refused("invalid_idf_table", h.idf_intensity, {**table, "durations_s": [1200, 300]}, 600, 4)


def test_noaa_pfds_csv_depth_intensity_and_si_provenance():
    # NOAA Atlas 14 vol.2 v3, PDS, 38.90°N 77.04°W: 5-min estimates
    # .355/.425 inch at ARI1/2; official CSV endpoint provenance and original
    # format are recorded in hydrology_noaa_pfds_reference.json.
    # 60-day row from the same NOAA response: 11.0/13.0 inch.
    text = "NOAA Atlas 14 Volume 2 Version 3\nPrecipitation frequency estimates (inches)\nAverage recurrence interval (years)\nDuration,1,2\n5-min,0.355,0.425\n60-day,11.0,13.0\n\nUpper confidence limits\n5-min,0.390,0.467\n"
    table = h.idf_from_csv(text)
    assert table["durations_s"] == [300, 5184000]
    assert table["return_periods_yr"] == [1, 2]
    assert table["intensities_mm_h"][0][0] == pytest.approx(0.355 * 25.4 * 12)
    assert table["intensities_mm_h"][1][1] == pytest.approx(13 * 25.4 / (60 * 24))
    assert table["provenance"]["units_in"] == "inches"
    intensity = h.idf_from_csv("Precipitation intensity (inches/hour)\nDuration,2,10\n5-min,1.0,2.0\n1-hr,0.5,1.0\n")
    assert intensity["intensities_mm_h"] == [[25.4, 50.8], [12.7, 25.4]]
    metric = h.idf_from_csv("Precipitation depth (mm)\nDuration,2,10\n5-min,10,20\n1-hr,30,60\n")
    assert metric["intensities_mm_h"] == [[120, 240], [30, 60]]
    refused("idf_units_unknown", h.idf_from_csv, "Duration,2,10\n5-min,10,20")
    refused("invalid_idf_csv", h.idf_from_csv, "Depth in mm\nDuration,2,10\n5-min,10")
    refused("unsupported_idf_frequency", h.idf_from_csv, "Depth in mm\nAnnual exceedance probability\nDuration,2,10\n5-min,10,20")


def test_original_noaa_csv_header_duration_colons_and_rounded_intensities():
    fixture = json.loads(Path(__file__).with_name("data").joinpath("hydrology_noaa_pfds_reference.json").read_text())
    depth = h.idf_from_csv(fixture["depth_csv"])
    assert depth["durations_s"] == [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
                                     172800, 259200, 345600, 604800, 864000, 1728000, 2592000, 3888000, 5184000]
    assert depth["return_periods_yr"] == [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000]
    assert h.idf_intensity(depth, 86400, 100)["intensity_mm_h"] == pytest.approx(8.32 * 25.4 / 24)
    assert h.idf_intensity(depth, 5184000, 1000)["intensity_mm_h"] == pytest.approx(25.6 * 25.4 / 1440)
    intensity = h.idf_from_csv(fixture["intensity_csv"])
    assert intensity["intensities_mm_h"][0] == [108, 130, 154, 172, 195, 212, 229, 245, 265, 282]
    assert intensity["intensities_mm_h"][-1] == [0] * 10
    assert "rounded_zero_intensities_require_depth_export_for_log_interpolation" in intensity["warnings"]
    # Positive short-duration values remain usable even when an unrelated
    # long-duration cell is rounded to zero. Never invent a nonzero replacement.
    assert h.idf_intensity(intensity, 300, 2)["intensity_mm_h"] == pytest.approx(130)
    refused("nonpositive_idf_intensity", h.idf_intensity, intensity, 5184000, 2)


def test_tr55_travel_times_consistent_us_to_si_conversion():
    # TR-55 eq.3-3 in customary units; 100ft, n=.15, P2=3in, slope=.01.
    expected_sheet = 0.007 * (0.15 * 100) ** 0.8 / (3 ** 0.5 * 0.01 ** 0.4) * 3600
    sheet = h.sheet_flow_time(30.48, 0.01, 0.15, 76.2)
    assert sheet["travel_time_s"] == pytest.approx(expected_sheet)
    assert sheet["warnings"] == []
    assert h.sheet_flow_time(30.4801, 0.01, 0.15, 76.2)["warnings"] == ["sheet_flow_length_exceeds_100_ft"]
    paved = h.shallow_flow_time(30.48, 0.01, paved=True)
    unpaved = h.shallow_flow_time(30.48, 0.01, paved=False)
    assert paved["travel_time_s"] == pytest.approx(100 / (20.3282 * 0.1))
    assert unpaved["travel_time_s"] == pytest.approx(100 / (16.1345 * 0.1))
    # SI Manning, R=1m,n=.02,S=.01 -> V=5m/s, L=100m ->20s.
    channel = h.channel_flow_time(100, 0.01, 0.02, 2, 2)
    assert channel["velocity_ms"] == 5
    assert channel["travel_time_s"] == 20
    segments = [{"type": "sheet", "length": 30.48, "slope": 0.01, "n": 0.15, "p2_mm": 76.2},
                {"type": "shallow", "length_m": 30.48, "slope": 0.01, "paved": True},
                {"type": "channel", "length_m": 100, "slope": 0.01, "n": 0.02, "area_m2": 2, "wetted_perimeter_m": 2}]
    assert h.time_of_concentration(segments)["tc_s"] == pytest.approx(expected_sheet + paved["travel_time_s"] + 20)
    refused("invalid_parameter", h.sheet_flow_time, 20, 0, 0.15, 76.2)
    refused("unsupported_tc_segment", h.time_of_concentration, [{"type": "magic"}])


@pytest.mark.parametrize("prf", [100, 300, 484, 600])
def test_unit_hydrograph_unit_volume_and_full_tail(prf):
    result = h.nrcs_unit_hydrograph(1e6, 3600, 60, peak_rate_factor=prf)
    # One millimeter over 1km² is 1000m³, independently integrated.
    volume = float(np.trapezoid(result["q_m3s"], result["t_s"]))
    assert volume == pytest.approx(1000, rel=0.005)
    assert result["q_m3s"][0] == result["q_m3s"][-1] == 0
    assert min(result["q_m3s"]) >= 0
    assert result["lag_s"] == 2160
    assert result["time_to_peak_s"] == 2190
    # NEH 16A: qp=PRF A_mi² depth_in / Tp_hr, converted to m³/s.
    expected_peak = prf * (1e6 / 2589988.110336) / 25.4 / (2190 / 3600) * 0.028316846592
    assert result["peak_m3s"] == pytest.approx(expected_peak, rel=0.01)
    assert json.loads(json.dumps(result))["volume_m3"] == pytest.approx(1000)


def test_unit_hydrograph_has_published_484_shape_not_a_triangle():
    # Choose Tc=570s,dt=36s -> Tp=360s, so all Table16-1 tenths land on knots.
    result = h.nrcs_unit_hydrograph(10000, 570, 36)
    q = result["q_m3s"]
    assert q[5] / q[10] == pytest.approx(0.47)
    assert q[17] / q[10] == pytest.approx(0.46)
    assert q[30] / q[10] == pytest.approx(0.055)
    assert q[45] / q[10] == pytest.approx(0.005)
    refused("unsupported_peak_rate_factor", h.nrcs_unit_hydrograph, 10000, 3600, 60, 700)


@pytest.mark.parametrize("kind,half_fraction", [("scs_type_i", 0.682), ("scs_type_ia", 0.664),
                                                   ("scs_type_ii", 0.663), ("scs_type_iii", 0.5)])
def test_design_storm_published_midpoint_and_total(kind, half_fraction):
    # SCS table transcriptions with provenance in hydrology_nrcs_tables.json.
    storm = h.design_storm(kind, 125, 86400, 1800)
    assert storm["cumulative_mm"][24] == pytest.approx(125 * half_fraction)
    assert sum(storm["depth_mm"]) == pytest.approx(125)
    assert min(storm["depth_mm"]) >= 0
    assert storm["t_s"][-1] == 86400
    refused("unsupported_scs_duration", h.design_storm, kind, 125, 43200, 1800)


def test_alternating_blocks_preserve_idf_incremental_depths():
    # DDF sqrt(duration) curve: 10,20,30,40mm at 300,600,900,1200s.
    # Constant increments here cannot detect block ordering, so use 10,16,20,22.
    depths = [10, 16, 20, 22]
    durations = [300, 600, 900, 1200]
    table = {"durations_s": durations, "return_periods_yr": [10],
             "intensities_mm_h": [[d * 3600 / t] for d, t in zip(depths, durations)]}
    storm = h.design_storm("idf_alternating_block", 22, 1200, 300,
                           idf={"table": table, "return_period_yr": 10})
    assert storm["depth_mm"] == pytest.approx([4, 10, 6, 2])
    assert storm["total_depth_mm"] == pytest.approx(22)
    bad = {**table, "intensities_mm_h": [[120], [30], [20], [15]]}
    refused("nonmonotonic_idf_depth", h.design_storm, "idf_alternating_block", 10, 1200, 300,
            idf={"table": bad, "return_period_yr": 10})


def test_cn_runoff_convolution_conserves_volume_and_counts_ia_once():
    area = 100000
    storm = h.design_storm("scs_type_ii", 125, 86400, 300)
    result = h.runoff_hydrograph({"area_m2": area, "cn": 80, "tc_s": 3600}, storm, 300)
    expected_depth = (125 - 12.7) ** 2 / (125 - 12.7 + 63.5)
    expected_volume = area * expected_depth / 1000
    assert sum(result["excess_depth_mm"]) == pytest.approx(expected_depth)
    assert np.trapezoid(result["q_m3s"], result["t_s"]) == pytest.approx(expected_volume, rel=0.005)
    assert abs(result["mass_balance_error_pct"]) < 0.5
    assert result["t_s"][-1] > 86400  # Must include runoff recession after storm.
    assert result["q_m3s"][0] == result["q_m3s"][-1] == 0
    dry = h.runoff_hydrograph({"area_m2": area, "cn": 80, "tc_s": 3600}, h.design_storm("scs_type_i", 1, 86400, 300), 300)
    assert dry["volume_m3"] == dry["peak_m3s"] == 0
    refused("time_grid_mismatch", h.runoff_hydrograph, {"area_m2": area, "cn": 80, "tc_s": 3600}, storm, 600)


def test_modified_puls_linear_reservoir_against_exponential_decay():
    # dS/dt=-kS -> S=S0 exp(-kt). S=1000*stage, Q=kS, k=.0002/s.
    # Exact supplied linear rating permits independent analytic V&V, not a mock.
    k, initial_storage, duration = 0.0002, 1000, 3600
    rating = {"type": "rating_curve", "stage_discharge": [[0, 0], [10, k * 10000]]}
    result = h.modified_puls({"t_s": [0, duration], "q_m3s": [0, 0]}, [[0, 0], [10, 10000]], [rating], 1, 60)
    expected = initial_storage * math.exp(-k * duration)
    assert result["final_storage_m3"] == pytest.approx(expected, rel=0.0001)
    assert result["outflow_hydrograph"]["q_m3s"][-1] == pytest.approx(k * expected, rel=0.0001)
    assert result["outflow_volume_m3"] + result["final_storage_m3"] == pytest.approx(initial_storage, abs=1e-7)
    assert abs(result["mass_balance_error_pct"]) < 0.5


def test_modified_puls_attenuates_peak_and_delays_it_to_falling_limb():
    inflow = {"t_s": [0, 600, 1200, 1800, 7200], "q_m3s": [0, 1, 2, 1, 0]}
    # Linear reservoir k=.001/s, storage=1000stage. Analytical linear-system
    # convolution guarantees attenuation for zero initial storage and one peak.
    rating = {"type": "rating_curve", "stage_discharge": [[0, 0], [20, 20]]}
    result = h.modified_puls(inflow, [[0, 0], [20, 20000]], [rating], 0, 60)
    assert result["peak_outflow_m3s"] < result["peak_inflow_m3s"]
    assert result["peak_outflow_time_s"] > result["peak_inflow_time_s"]
    assert result["peak_attenuation_pct"] > 0
    assert abs(result["mass_balance_error_pct"]) < 0.5
    assert result["inflow_volume_m3"] == pytest.approx(np.trapezoid(inflow["q_m3s"], inflow["t_s"]))
    assert result["initial_storage_m3"] + result["inflow_volume_m3"] == pytest.approx(result["outflow_volume_m3"] + result["final_storage_m3"], abs=1e-7)


def test_puls_retains_irregular_inflow_knots_and_refuses_overflow():
    inflow = {"t_s": [0, 137, 467, 1000], "q_m3s": [0, 1, 0.5, 0]}
    result = h.modified_puls(inflow, [[0, 0], [10, 10000]], [], 0, 60)
    expected = np.trapezoid(inflow["q_m3s"], inflow["t_s"])
    assert result["final_storage_m3"] == pytest.approx(expected)
    assert result["inflow_volume_m3"] == pytest.approx(expected)
    assert 137 in result["outflow_hydrograph"]["t_s"]
    assert result["peak_outflow_m3s"] == 0
    refused("stage_storage_exceeded", h.modified_puls, inflow, [[0, 0], [1, 10]], [], 0, 60)
    fast_rating = {"type": "rating_curve", "stage_discharge": [[0, 0], [10, 1000]]}
    refused("routing_timestep_too_large", h.modified_puls, {"t_s": [0, 100], "q_m3s": [0, 0]},
            [[0, 0], [10, 10000]], [fast_rating], 1, 100)


def test_outlet_weirs_riser_and_submerged_orifice_closed_forms():
    g, cd = 9.80665, 0.62
    # Sharp-crested free-flow equations from HEC-22 outlet-structure methods.
    rectangular = {"type": "weir", "crest_m": 0, "width_m": 2, "cd": cd}
    assert h.outlet_flow(rectangular, 0.5)["flow_m3s"] == pytest.approx((2 / 3) * cd * math.sqrt(2 * g) * 2 * 0.5 ** 1.5)
    notch = {"type": "weir", "shape": "v_notch", "crest_m": 0, "angle_deg": 90, "cd": cd}
    assert h.outlet_flow(notch, 0.5)["flow_m3s"] == pytest.approx((8 / 15) * cd * math.sqrt(2 * g) * 0.5 ** 2.5)
    # Entire aperture submerged on both sides: Δhead is constant, so integral
    # must equal exact Cd*A*sqrt(2gΔhead), not a free-discharge approximation.
    orifice = {"type": "orifice", "invert_m": 0, "diameter_m": 1, "cd": cd, "tailwater_m": 1}
    assert h.outlet_flow(orifice, 1.5)["flow_m3s"] == pytest.approx(cd * math.pi / 4 * math.sqrt(2 * g * 0.5), rel=1e-8)
    riser = {"type": "riser", "crest_m": 0, "diameter_m": 1, "cd": cd, "orifice_cd": 0.6}
    for head in [0.001, 0.1, 1, 2]:
        expected = min((2 / 3) * cd * math.sqrt(2 * g) * math.pi * head ** 1.5,
                       0.6 * math.pi / 4 * math.sqrt(2 * g * head))
        assert h.outlet_flow(riser, head)["flow_m3s"] == pytest.approx(expected)
    refused("submerged_weir_unsupported", h.outlet_flow, {**rectangular, "tailwater_m": 0.1}, 0.5)
    refused("reverse_outlet_flow_unsupported", h.outlet_flow, orifice, 0.5)


def test_partial_orifice_flow_is_continuous_at_midpoint_and_crown():
    orifice = {"type": "orifice", "invert_m": 0, "diameter_m": 1, "cd": 0.62}
    assert h.outlet_flow(orifice, -0.01)["flow_m3s"] == 0
    previous = 0
    for head in [0.001, 0.1, 0.25, 0.5, 0.75, 1, 2]:
        q = h.outlet_flow(orifice, head)["flow_m3s"]
        assert q > previous
        previous = q
    for boundary in [0.5, 1]:
        below = h.outlet_flow(orifice, boundary - 1e-6)["flow_m3s"]
        above = h.outlet_flow(orifice, boundary + 1e-6)["flow_m3s"]
        assert above == pytest.approx(below, rel=2e-5)

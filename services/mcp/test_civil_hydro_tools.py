"""Offline consumer-level V&V for civil MCP hydraulics, runoff and pond routing.

The equations are real services.civil calls; only the native document study store
is replaced. Run centrally with .venv-mcp/bin/python -m pytest this file.
"""
from __future__ import annotations

import copy
import json
import math

import pytest

from services.civil import culvert, gravity, hydrology, schema, terrain
from services.mcp import civil_common as cc
from services.mcp import civil_hydro_tools as hydro_tools
from services.mcp import core_bridge as cb


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Store:
    def __init__(self):
        self.studies = []
        self.next_id = 0

    def call(self, method, params=None):
        params = params or {}
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(self.studies)}
        if method == "sim_study_add":
            self.next_id += 1
            record = copy.deepcopy(params["study"])
            record["id"] = f"study-{self.next_id}"
            self.studies.append(record)
            return {"study_id": record["id"]}
        if method == "sim_study_edit":
            for index, record in enumerate(self.studies):
                if record["id"] == params["study_id"]:
                    self.studies[index] = copy.deepcopy(params["study"])
                    return {"ok": True}
            raise RuntimeError("no such study")
        raise AssertionError(f"Offline tools attempted unexpected RPC: {method}")

    def site(self):
        (study,) = self.studies
        assert study["type"] == "civil_site"
        return study


@pytest.fixture(autouse=True)
def _isolated_session():
    saved_sites = copy.deepcopy(cc.SITES)
    saved_active = dict(cc.ACTIVE)
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    yield
    cc.SITES.clear()
    cc.SITES.update(saved_sites)
    cc.ACTIVE.clear()
    cc.ACTIVE.update(saved_active)


@pytest.fixture
def store(monkeypatch):
    fake = _Store()
    monkeypatch.setattr(cb, "call", fake.call)

    def doc(doc_id):
        if doc_id != "doc-1":
            raise ValueError("unknown document")
        return doc_id

    monkeypatch.setattr(cc, "_doc", doc)
    return fake


@pytest.fixture
def tools(store):
    from services.mcp.civil_tools import register_civil_tools

    mcp = _StubMCP()
    register_civil_tools(mcp)
    hydro_tools.register_civil_hydro_tools(mcp)
    return mcp.tools


def _call(tools, name, **kwargs):
    return json.loads(tools[name](**kwargs))


def _ok(tools, name, **kwargs):
    result = _call(tools, name, **kwargs)
    assert result["ok"] is True, result
    return result


def _water_network():
    network = schema.water_network("w", "Reservoir supply")
    network["reservoirs"] = [{"id": "R", "x": 0, "y": 0, "head": 80}]
    network["junctions"] = [{"id": "J", "x": 500, "y": 0, "elevation": 10, "demand_m3s": 0.02}]
    network["pipes"] = [{"id": "WP", "from": "R", "to": "J", "length": 500,
                         "diameter": 0.2, "roughness": 130, "minor_loss": 0}]
    return network


def _gravity_network():
    network = schema.gravity_network("g", "Three-reach sewer")
    inverts = [11, 10.6, 10.5, 10.1]
    inflows = [0.002, 0.001, 0.001, 0]
    ids = ["A", "B", "C", "O"]
    network["nodes"] = [{"id": node_id, "type": "outfall" if node_id == "O" else "manhole",
                         "x": 100 * i, "y": 0, "invert": inverts[i], "rim": 15,
                         "inflow_m3s": inflows[i], "loss_k": 0}
                        for i, node_id in enumerate(ids)]
    network["conduits"] = [{"id": f"P{i + 1}", "from": a, "to": b, "shape": "circular",
                            "diameter": 0.3, "length": 100, "n": 0.013, "vertices": []}
                           for i, (a, b) in enumerate(zip(ids, ids[1:]))]
    return network


def _site():
    model = schema.new_model("Engineering site")
    model["water_networks"] = [_water_network()]
    model["gravity_networks"] = [_gravity_network()]
    model["catchments"] = [{"id": "lot", "name": "Lot", "outlet": "A", "area_m2": 1000,
                            "runoff_coeff": 0.6, "cn": 80, "imperv_pct": 40,
                            "polygon": None, "tc_s": 600, "tc_segments": []}]
    model["criteria"]["overrides"] = {"water_min_pressure_m": 10, "water_max_pressure_m": 40,
                                        "fire_residual_min_m": 20}
    return model


def _hw_resistance(network):
    pipe = network["pipes"][0]
    # EPANET 4.727 US coefficient converted independently using ft=0.3048 m.
    coefficient_si = 4.727 * 0.3048 ** (4.871 - 3 * 1.852)
    return coefficient_si * pipe["length"] / (pipe["roughness"] ** 1.852 * pipe["diameter"] ** 4.871)


def _pyramid_surface():
    # Square inverted pyramid: inundated area=100*z², V=(100/3)*z³ for 0<=z<=4.
    return {"id": "pond-ground", "name": "Pyramid basin", "kind": "tin",
            "points": [[-20, -20, 4], [20, -20, 4], [20, 20, 4], [-20, 20, 4], [0, 0, 0]],
            "triangles": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]],
            "breaklines": [], "boundary": [[-20, -20], [20, -20], [20, 20], [-20, 20]]}


def _culvert():
    return {"id": "road-pipe", "kind": "culvert", "shape": "circular", "diameter_m": 1.2,
            "length_m": 30, "slope": 0.01, "n": 0.013,
            "inlet": "concrete_pipe_square_edge_headwall", "inlet_invert_m": 100,
            "road_crest_m": 103, "tailwater_m": 1.2, "flow_m3s": 3}


def test_water_closed_form_summary_persistence_and_load(tools, store):
    model = _site()
    network = model["water_networks"][0]
    full = _ok(tools, "civil_water_solve", network_id="w", site=model, doc_id="doc-1", detail=True)
    head = 80 - _hw_resistance(network) * 0.02 ** 1.852
    junction = full["timesteps"][0]["nodes"]["J"]
    assert full["converged"] is True
    assert junction["head_m"] == pytest.approx(head, abs=2e-5)
    assert full["summary"]["min_pressure_m"] == pytest.approx(head - 10, abs=2e-5)
    assert full["summary"]["min_pressure_node_id"] == full["summary"]["max_pressure_node_id"] == "J"
    assert full["summary"]["max_pressure_kpa"] == pytest.approx((head - 10) * 9.80665, abs=2e-4)
    assert full["summary"]["max_velocity_ms"] == pytest.approx(0.02 / (math.pi * 0.2 ** 2 / 4))
    assert full["checks"][0]["check"] == "maximum_pressure"
    assert full["checks"][0]["limit"] == 40 and not full["checks"][0]["pass"]
    compact = _ok(tools, "civil_water_solve", network_id="w", doc_id="doc-1")
    assert "timesteps" not in compact
    stored = copy.deepcopy(store.site())
    assert stored["results"]["water"]["timesteps"][0]["nodes"]["J"]["head_m"] == pytest.approx(head, abs=2e-5)
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    _ok(tools, "civil_load", doc_id="doc-1")
    restored = cc.resolve_site(None)
    assert restored["model"] == stored["params"]["model"]
    assert restored["results"] == stored["results"]
    assert _ok(tools, "civil_water_solve", network_id="w")["summary"] == compact["summary"]


def test_fire_flow_limit_reaches_closed_form_residual_and_fixed_flow_fails(tools, store):
    model = _site()
    network = model["water_networks"][0]
    result = _ok(tools, "civil_fire_flow", network_id="w", node_id="J", site=model, doc_id="doc-1")
    # H_R-z_J-R*(base+fire)^1.852 = residual_min.
    expected = ((80 - 10 - 20) / _hw_resistance(network)) ** (1 / 1.852) - 0.02
    assert result["available_flow_m3s"] == pytest.approx(expected, abs=2e-7)
    assert result["minimum_pressure_m"] == pytest.approx(20, abs=2e-4)
    assert result["residual_min_m"] == 20 and result["limiting_node_id"] == "J"
    assert result["pass"] is True
    assert "timesteps" not in result["hydraulics"]
    assert "timesteps" in store.site()["results"]["fire_flow"]["hydraulics"]
    fixed = _ok(tools, "civil_fire_flow", network_id="w", node_id="J", flow_m3s=expected * 1.05,
                residual_min_m=20, detail=True)
    assert fixed["pass"] is False and fixed["minimum_pressure_m"] < 20
    fire_check = next(row for row in fixed["checks"] if row["check"] == "fire_residual_pressure")
    assert fire_check["limit"] == 20 and fire_check["pass"] is False
    # Both residual pressure and velocity fail; only the failure/pass partition is promised.
    verdicts = [row["pass"] for row in fixed["checks"]]
    assert verdicts == sorted(verdicts)
    velocity_check = next(row for row in fixed["checks"] if row["check"] == "maximum_velocity")
    expected_velocity = (0.02 + expected * 1.05) / (math.pi * 0.2 ** 2 / 4)
    assert velocity_check["value"] == pytest.approx(expected_velocity)
    assert velocity_check["limit"] == 3 and velocity_check["pass"] is False
    assert fixed["summary"]["checks_failed"] == 2
    assert fixed["hydraulics"]["timesteps"][0]["links"]["WP"]["flow_m3s"] == pytest.approx(0.02 + expected * 1.05)
    assert cc.resolve_site(None)["model"]["water_networks"][0]["junctions"][0]["demand_m3s"] == 0.02


def test_gravity_accumulates_three_reaches_and_flags_ten_states_slope(tools, store):
    model = _site()
    result = _ok(tools, "civil_gravity_analyze", network_id="g", site=model, doc_id="doc-1", detail=True)
    assert [result["conduits"][pid]["flow_m3s"] for pid in ("P1", "P2", "P3")] == pytest.approx([0.002, 0.003, 0.004])
    assert result["nodes"]["O"]["flow_m3s"] == pytest.approx(0.004)
    assert result["total_inflow_m3s"] == pytest.approx(result["total_outflow_m3s"])
    slope_check = next(row for row in result["checks"] if row["check"] == "minimum_slope" and row["element_id"] == "P2")
    assert slope_check["value"] == pytest.approx(0.001)
    assert slope_check["limit"] == pytest.approx(0.0028) and not slope_check["pass"]
    assert "GLUMRB" in slope_check["reference"]
    passes = [row["pass"] for row in result["checks"]]
    assert passes == sorted(passes)
    pressure_id = result["summary"]["max_pressure_node_id"]
    invert = next(node["invert"] for node in model["gravity_networks"][0]["nodes"] if node["id"] == pressure_id)
    assert result["summary"]["max_pressure_m"] == pytest.approx(result["nodes"][pressure_id]["hgl_m"] - invert)
    expected = gravity.analyze(model["gravity_networks"][0], criteria=model["criteria"])
    assert result["conduits"]["P2"]["hgl_upstream_m"] == pytest.approx(expected["conduits"]["P2"]["hgl_upstream_m"])
    compact = _ok(tools, "civil_gravity_analyze", network_id="g")
    assert "nodes" not in compact and "conduits" not in compact
    assert len(store.site()["results"]["gravity"]["conduits"]) == 3


@pytest.mark.parametrize("event,intensity", [
    ({"intensity_mm_h": 36}, 36),
    ({"idf_table": {"durations_s": [300, 1200], "return_periods_yr": [2, 10],
                     "intensities_mm_h": [[80, 160], [20, 40]]}, "return_period_yr": 2}, 40),
])
def test_catchment_rational_peaks_are_added_at_outlet_and_propagate(tools, event, intensity):
    model = _site()
    result = _ok(tools, "civil_gravity_analyze", network_id="g", site=model,
                 flows={"A": 0.001}, catchment_peaks=True, storm=event, detail=True)
    peak = 0.6 * intensity * 1000 / 3.6e6
    assert result["catchment_peaks"]["lot"]["flow_m3s"] == pytest.approx(peak)
    assert result["catchment_peaks"]["lot"]["outlet_node_id"] == "A"
    assert [result["conduits"][pid]["flow_m3s"] for pid in ("P1", "P2", "P3")] == pytest.approx([0.003 + peak, 0.004 + peak, 0.005 + peak])
    assert result["total_outflow_m3s"] == pytest.approx(0.005 + peak)
    assert model["gravity_networks"][0]["nodes"][0]["inflow_m3s"] == 0.002


def test_cover_uses_ground_surface_not_endpoint_rim(tools):
    model = _site()
    model["surfaces"] = [{"id": "ground", "name": "Ground", "kind": "tin",
                          "points": [[0, -10, 11.6], [300, -10, 11.6], [300, 10, 11.6], [0, 10, 11.6]],
                          "triangles": [[0, 1, 2], [0, 2, 3]], "breaklines": []}]
    result = _ok(tools, "civil_gravity_analyze", network_id="g", surface_id="ground", site=model)
    cover = next(row for row in result["checks"] if row["check"] == "minimum_cover" and row["element_id"] == "P1")
    assert cover["value"] == pytest.approx(11.6 - 11 - 0.3)
    assert cover["pass"] is False  # rims alone would pass, at 15 m.


def test_gravity_design_apply_writes_diameters_and_invalidates_results(tools, store):
    model = _site()
    network = model["gravity_networks"][0]
    for i, node in enumerate(network["nodes"]):
        node["invert"] = 11.2 - 0.4 * i
        node["inflow_m3s"] = 0.025 if i == 0 else 0
    for pipe in network["conduits"]:
        pipe["diameter"] = 0.2
    model["criteria"]["overrides"]["gravity_max_depth_ratio"] = 0.5
    _ok(tools, "civil_water_solve", network_id="w", site=model, doc_id="doc-1")
    _ok(tools, "civil_gravity_analyze", network_id="g", doc_id="doc-1")
    expected = gravity.design_sizes(network, {}, model["criteria"], [0.2, 0.25, 0.3, 0.4])
    preview = _ok(tools, "civil_gravity_design", network_id="g", available_diameters=[0.4, 0.3, 0.2, 0.25], apply=False)
    assert preview["sizes"] == expected["sizes"]
    assert all(pipe["diameter"] == 0.2 for pipe in cc.resolve_site(None)["model"]["gravity_networks"][0]["conduits"])
    applied = _ok(tools, "civil_gravity_design", network_id="g", available_diameters=[0.2, 0.25, 0.3, 0.4],
                  apply=True, doc_id="doc-1")
    assert applied["applied"] is True
    assert applied["diameter_changes"] == [{"conduit_id": f"P{i}", "old_diameter_m": 0.2, "new_diameter_m": 0.3} for i in range(1, 4)]
    persisted = store.site()
    assert [pipe["diameter"] for pipe in persisted["params"]["model"]["gravity_networks"][0]["conduits"]] == [0.3] * 3
    assert set(persisted["results"]) == {"gravity"}
    assert "conduits" not in persisted["results"]["gravity"]  # the stale analysis is gone, replaced by the design.
    # A subsequent public model edit must invalidate this design result too.
    _ok(tools, "civil_site_edit", ops=[{"op": "set_criteria", "criteria": {"profile": "ten_states_2014", "overrides": {}}}], doc_id="doc-1")
    assert store.site()["results"] == {} and cc.resolve_site(None)["results"] == {}


def test_pyramid_pond_route_conserves_mass_attenuates_and_persists_structure(tools, store):
    model = _site()
    surface = _pyramid_surface()
    model["surfaces"] = [surface]
    polygon = surface["boundary"]
    stages = [i / 10 for i in range(41)]
    inflow = {"t_s": [0, 600, 1200, 1800, 7200], "q_m3s": [0, 0.1, 0.2, 0.1, 0]}
    outlets = [{"type": "rating_curve", "stage_discharge": [[0, 0], [4, 0.08]]}]
    result = _ok(tools, "civil_detention_route", structure_id="pond", inflow=inflow, surface_id="pond-ground",
                 polygon=polygon, stages=stages, outlets=outlets, initial_stage=0, dt_s=60, site=model, doc_id="doc-1")
    assert result["stage_storage"][10][1] == pytest.approx(100 / 3)  # z=1 m exact pyramid volume.
    assert result["stage_storage"][-1][1] == pytest.approx(100 * 4 ** 3 / 3)
    expected_storage = terrain.stage_storage(surface, polygon, stages)["stage_storage"]
    expected = hydrology.modified_puls(inflow, expected_storage, outlets, 0, 60)
    assert result["max_stage_m"] == pytest.approx(expected["max_stage_m"])
    assert result["peak_outflow_m3s"] < result["peak_inflow_m3s"]
    assert result["peak_attenuation_pct"] > 0
    assert abs(result["mass_balance_error_pct"]) < 0.5 and result["mass_balance_pass"] is True
    assert result["initial_storage_m3"] + result["inflow_volume_m3"] == pytest.approx(result["final_storage_m3"] + result["outflow_volume_m3"], abs=1e-7)
    pond = store.site()["params"]["model"]["structures"][0]
    assert pond["kind"] == "detention_pond" and pond["stage_storage"] == result["stage_storage"]
    assert pond["outlets"] == outlets
    persisted = copy.deepcopy(store.site())
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    _ok(tools, "civil_load", doc_id="doc-1")
    assert cc.resolve_site(None)["results"] == persisted["results"]
    assert cc.resolve_site(None)["model"]["structures"][0] == pond
    rerun = _ok(tools, "civil_detention_route", structure_id="pond", inflow=inflow)
    assert rerun["outflow_hydrograph"] == result["outflow_hydrograph"]


def test_detention_generates_catchment_runoff_and_retains_ending_storage(tools, store):
    model = _site()
    storm = {"t_s": [0, 60, 120, 180], "depth_mm": [10, 15, 10]}
    outlets = [{"type": "rating_curve", "stage_discharge": [[0, 0], [10, 0.1]]}]
    result = _ok(tools, "civil_detention_route", structure_id="runoff-pond", catchment_id="lot", storm=storm,
                 stage_storage=[[0, 0], [10, 10000]], outlets=outlets, initial_stage=0, dt_s=60, site=model, doc_id="doc-1")
    runoff = hydrology.runoff_hydrograph(model["catchments"][0], storm, 60)
    assert result["inflow_volume_m3"] == pytest.approx(runoff["volume_m3"])
    assert result["runoff_hydrograph"]["peak_m3s"] == pytest.approx(runoff["peak_m3s"])
    assert result["final_storage_m3"] > 0 and abs(result["mass_balance_error_pct"]) < 0.5
    assert store.site()["params"]["model"]["structures"][0]["inflow_node"] == "A"


def test_culvert_hds5_pipe_fixture_and_performance_curve(tools, store):
    model = _site()
    fixture = _culvert()
    model["structures"] = [fixture]
    result = _ok(tools, "civil_culvert", structure_id="road-pipe", site=model, doc_id="doc-1")
    expected = culvert.analyze(fixture, 3, 1.2)
    assert result["inlet_control_hw_m"] == pytest.approx(expected["inlet_control_hw_m"])
    assert result["inlet_control_hw_m"] == pytest.approx(1.716454687966524, rel=1e-12)
    assert result["controlling"] == "inlet" and result["overtopping"] is False
    assert store.site()["results"]["culvert"]["headwater_m"] == pytest.approx(expected["headwater_m"])
    flows = [1, 2, 3]
    curve = _ok(tools, "civil_culvert", culvert=json.dumps(fixture), flows=flows, tailwater_m=1.2)
    expected_curve = culvert.performance_curve(fixture, flows)
    assert [point["headwater_m"] for point in curve["points"]] == pytest.approx([point["headwater_m"] for point in expected_curve["points"]])
    assert [point["flow_m3s"] for point in curve["points"]] == flows


def test_hydrology_kinds_use_catchment_inputs_and_real_equations(tools, store):
    model = _site()
    rational = _ok(tools, "civil_hydrology", kind="rational", catchment_id="lot", intensity_mm_h=36, site=model, doc_id="doc-1")
    assert rational["flow_m3s"] == pytest.approx(0.6 * 36 * 1000 / 3.6e6)
    cn_runoff = _ok(tools, "civil_hydrology", kind="scs_runoff", catchment_id="lot", p_mm=50)
    storage = 25400 / 80 - 254
    assert cn_runoff["runoff_mm"] == pytest.approx((50 - 0.2 * storage) ** 2 / (50 + 0.8 * storage))
    segments = [{"type": "channel", "length_m": 100, "slope": 0.01, "n": 0.02,
                 "area_m2": 2, "wetted_perimeter_m": 4}]
    tc = _ok(tools, "civil_hydrology", kind="tc", segments=segments)
    assert tc["tc_s"] == pytest.approx(100 / (0.5 ** (2 / 3) * math.sqrt(0.01) / 0.02))
    unit = _ok(tools, "civil_hydrology", kind="unit_hydrograph", catchment_id="lot", dt_s=60)
    assert unit["volume_m3"] == pytest.approx(1000 * 0.001)
    design = _ok(tools, "civil_hydrology", kind="design_storm", depth_mm=50, dt_s=300)
    assert sum(design["depth_mm"]) == pytest.approx(50)
    runoff = _ok(tools, "civil_hydrology", kind="runoff_hydrograph", catchment_id="lot", storm=design, dt_s=300)
    expected = hydrology.runoff_hydrograph(model["catchments"][0], design, 300)
    assert runoff["peak_m3s"] == pytest.approx(expected["peak_m3s"])
    assert runoff["volume_m3"] == pytest.approx(cn_runoff["runoff_mm"] * 1000 / 1000, rel=0.005)
    table = {"durations_s": [300, 1200], "return_periods_yr": [2, 10], "intensities_mm_h": [[80, 160], [20, 40]]}
    idf = _ok(tools, "civil_hydrology", kind="idf", idf_table=table, duration_s=600, return_period_yr=2, doc_id="doc-1")
    assert idf["intensity_mm_h"] == pytest.approx(40)
    assert store.site()["results"]["hydrology"]["intensity_mm_h"] == pytest.approx(40)
    imported = _ok(tools, "civil_hydrology", kind="idf",
                   idf_csv="NOAA PFDS rainfall depths in mm\nDuration,2,10\n5-min,5,10\n10-min,8,16\n")
    assert imported["durations_s"] == [300, 600]
    assert imported["intensities_mm_h"] == [[60, 120], [48, 96]]


def test_hydrology_preserves_catchment_abstraction_and_peak_rate_factor(tools):
    model = _site()
    catchment = model["catchments"][0]
    catchment.update(ia_ratio=0.05, peak_rate_factor=300)
    event = {"t_s": [0, 60, 120, 180], "depth_mm": [10, 15, 10]}
    result = _ok(tools, "civil_hydrology", kind="runoff_hydrograph", catchment_id="lot",
                 storm=event, dt_s=60, site=model)
    expected = hydrology.runoff_hydrograph(catchment, event, 60)
    assert result["peak_m3s"] == pytest.approx(expected["peak_m3s"])
    assert result["runoff_depth_mm"] == pytest.approx(expected["runoff_depth_mm"])
    unit = _ok(tools, "civil_hydrology", kind="unit_hydrograph", catchment_id="lot", dt_s=60)
    assert unit["peak_rate_factor"] == 300 and unit["volume_m3"] == pytest.approx(1)


def test_open_channel_full_capacity_matches_manning_closed_form(tools, store):
    result = _ok(tools, "civil_open_channel", kind="capacity", shape={"shape": "circular", "diameter": 0.3},
                 flow_m3s=0, n=0.013, slope=0.004)
    expected = math.pi * 0.3 ** 2 / 4 * (0.3 / 4) ** (2 / 3) * math.sqrt(0.004) / 0.013
    assert result["flow_m3s"] == pytest.approx(expected)
    assert store.studies == [] and cc.SITES == {}



def test_detention_accepts_idf_design_storm_specification(tools):
    model = _site()
    table = {"durations_s": [60, 120, 180], "return_periods_yr": [2],
             "intensities_mm_h": [[60], [48], [40]]}
    spec = {"kind": "idf_alternating_block", "depth_mm": 35, "duration_s": 180,
            "dt_s": 60, "idf_table": table, "return_period_yr": 2}
    result = _ok(tools, "civil_detention_route", catchment_id="lot", storm=spec,
                 stage_storage=[[0, 0], [10, 10000]], outlets=[], dt_s=60, site=model)
    event = hydrology.design_storm("idf_alternating_block", 35, 180, 60,
                                  {"table": table, "return_period_yr": 2})
    expected = hydrology.runoff_hydrograph(model["catchments"][0], event, 60)
    assert result["final_storage_m3"] == pytest.approx(expected["volume_m3"])
    assert result["inflow_volume_m3"] == pytest.approx(expected["volume_m3"])
    assert result["peak_outflow_m3s"] == 0 and abs(result["mass_balance_error_pct"]) < 0.5

@pytest.mark.parametrize("kind,kwargs,engine", [
    ("normal_depth", {"flow_m3s": 4, "n": 0.02, "slope": 0.001}, lambda: gravity.normal_depth({"shape": "rectangular", "width": 2}, 4, 0.001, 0.02)),
    ("critical_depth", {"flow_m3s": 4}, lambda: gravity.critical_depth({"shape": "rectangular", "width": 2}, 4)),
    ("hydraulic_jump", {"flow_m3s": 2, "y1_m": 0.2}, lambda: gravity.hydraulic_jump({"shape": "rectangular", "width": 2}, 2, 0.2)),
    ("capacity", {"flow_m3s": 0, "n": 0.02, "slope": 0.001, "depth_m": 1}, lambda: gravity.manning_flow({"shape": "rectangular", "width": 2}, 1, 0.001, 0.02)),
    ("standard_step", {"flow_m3s": 2, "n": 0.02, "slope": 0.001, "length_m": 50, "control_depth_m": 2, "steps": 10}, lambda: gravity.standard_step({"shape": "rectangular", "width": 2}, 2, 0.02, 0.001, 50, 2, "upstream", 10)),
])
def test_open_channel_kinds_are_stateless_and_match_engine(tools, store, kind, kwargs, engine):
    result = _ok(tools, "civil_open_channel", kind=kind, shape={"shape": "rectangular", "width": 2}, **kwargs)
    expected = engine()
    for field in ("depth_m", "conjugate_depth_m", "energy_loss_m", "flow_m3s", "normal_depth_m"):
        if field in expected:
            assert result[field] == pytest.approx(expected[field])
    if kind == "critical_depth":
        assert result["depth_m"] == pytest.approx((4 ** 2 / (9.80665 * 2 ** 2)) ** (1 / 3))
    if kind == "standard_step":
        assert result["profile"] == expected["profile"] and result["converged"] is True
    assert store.studies == [] and cc.SITES == {}


@pytest.mark.parametrize("tool,kwargs,reason", [
    ("civil_water_solve", {"network_id": "absent"}, "not_found"),
    ("civil_fire_flow", {"network_id": "w", "node_id": "R"}, "not_found"),
    ("civil_fire_flow", {"network_id": "w", "node_id": "J", "flow_m3s": -0.1}, "invalid_fire_flow"),
    ("civil_fire_flow", {"network_id": "w", "node_id": "J", "residual_min_m": -1}, "invalid_residual_pressure"),
    ("civil_gravity_analyze", {"network_id": "g", "flows": []}, "invalid_flows"),
    ("civil_gravity_analyze", {"network_id": "g", "catchment_peaks": True}, "storm_required"),
    ("civil_gravity_design", {"network_id": "g"}, "invalid_available_diameters"),
    ("civil_hydrology", {"kind": "unknown"}, "unsupported_hydrology_kind"),
    ("civil_hydrology", {"kind": "idf", "idf_table": {"durations_s": [300, 600], "return_periods_yr": [2], "intensities_mm_h": [[40], [20]]}, "duration_s": 1200, "return_period_yr": 2}, "outside_idf_table"),
    ("civil_detention_route", {}, "inflow_required"),
    ("civil_detention_route", {"inflow": {"t_s": [0, 60], "q_m3s": [0, 0]}}, "stage_storage_required"),
    ("civil_detention_route", {"inflow": {"t_s": [0, 60], "q_m3s": [0, 1]}, "stage_storage": [[0, 0], [1, 1]], "outlets": []}, "stage_storage_exceeded"),
    ("civil_culvert", {}, "culvert_required"),
    ("civil_culvert", {"culvert": _culvert(), "flows": [3, 2]}, "invalid_performance_flows"),
])
def test_named_refusals_do_not_persist_results(tools, store, tool, kwargs, reason):
    result = _call(tools, tool, site=_site(), doc_id="doc-1", **kwargs)
    assert result["ok"] is False and result["reason"] == reason
    assert store.studies == [] and cc.resolve_site(None)["results"] == {}


def test_site_document_and_channel_refusals(tools):
    assert _call(tools, "civil_water_solve", network_id="w")["reason"] == "no_civil_site"
    assert _call(tools, "civil_water_solve", network_id="w", site="missing")["reason"] == "unknown_site"
    assert _call(tools, "civil_water_solve", network_id="w", site=_site(), doc_id="bad-doc")["reason"] == "unknown_doc"
    assert _call(tools, "civil_open_channel", kind="bad", shape={}, flow_m3s=1)["reason"] == "unsupported_open_channel_kind"
    assert _call(tools, "civil_open_channel", kind="hydraulic_jump", shape={"shape": "rectangular", "width": 2}, flow_m3s=0.1, y1_m=1)["reason"] == "jump_requires_supercritical_flow"


def test_disconnected_gravity_cycles_terrain_gaps_and_no_feasible_size_refuse(tools):
    model = _site()
    model["water_networks"][0]["pipes"][0]["status"] = "closed"
    assert _call(tools, "civil_water_solve", network_id="w", site=model)["reason"] == "disconnected_nodes"
    model = _site()
    model["gravity_networks"][0]["conduits"].append({"id": "back", "from": "O", "to": "A", "shape": "circular", "diameter": 0.3, "n": 0.013})
    assert _call(tools, "civil_gravity_analyze", network_id="g", site=model)["reason"] == "cycle_in_gravity_network"
    model = _site()
    model["surfaces"] = [_pyramid_surface()]
    assert _call(tools, "civil_gravity_analyze", network_id="g", surface_id="pond-ground", site=model)["reason"] == "terrain_outside_surface"
    assert _call(tools, "civil_gravity_design", network_id="g", available_diameters=[0.1], site=_site())["reason"] == "design_no_feasible_size"

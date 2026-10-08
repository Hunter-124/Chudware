"""Offline civil MCP engineering flows, persisted studies and refusal contracts.

The native plant in coupling tests is an independent q=k*sqrt(delta_p) law.
The civil feeder remains the real water engine; the oracle is the laminar
Darcy-Weisbach closed form, not a canned civil result. No daemon is required.
"""
from __future__ import annotations

import copy
import json
import math
import os

import pytest

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

from services.civil import buried, schema, supports  # noqa: E402
from services.mcp import civil_common as cc, civil_link_tools as links, core_bridge as cb  # noqa: E402
from services.mcp import piping_tools as pt, simhandoff as sh  # noqa: E402

_G = 9.80665
_NU = 1.004e-6
_K = 5e-7
_D = 0.01
_L = 1000.0
_MATERIAL = {"elastic_modulus_gpa": 200.0, "allowable_stress_mpa": 100.0,
             "pipe_density_kg_m3": 7850.0}


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def _tools():
    from services.mcp.civil_tools import register_civil_tools
    from services.mcp.civil_hydro_tools import register_civil_hydro_tools
    mcp = _StubMCP()
    register_civil_tools(mcp)
    register_civil_hydro_tools(mcp)
    links.register_civil_link_tools(mcp)
    return mcp.tools


def _j(value):
    return json.loads(value)


class _Store:
    def __init__(self):
        self.documents = {}
        self.calls = []
        self.plant = None
        self.next_id = 0

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "piping_solve":
            assert self.plant is not None, "tests must explicitly choose a plant behavior"
            return self.plant(params)
        doc = params["doc_id"]
        studies = self.documents.setdefault(doc, [])
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(studies)}
        if method == "sim_study_add":
            self.next_id += 1
            record = copy.deepcopy(params["study"])
            record["id"] = f"study-{self.next_id}"
            studies.append(record)
            return {"study_id": record["id"]}
        if method == "sim_study_edit":
            for index, record in enumerate(studies):
                if record["id"] == params["study_id"]:
                    replacement = copy.deepcopy(params["study"])
                    replacement["id"] = record["id"]
                    studies[index] = replacement
                    return {"ok": True}
            raise AssertionError("study to edit does not exist")
        raise AssertionError(f"unexpected native method {method}")

    def civil(self, doc_id="doc-1"):
        records = [s for s in self.documents[doc_id] if s["type"] == "civil_site"]
        assert len(records) == 1
        return records[0]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    old_sites, old_active = copy.deepcopy(cc.SITES), dict(cc.ACTIVE)
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    monkeypatch.setattr(pt, "_NETWORKS", {})
    monkeypatch.setattr(pt, "_PIPING", {"doc": None, "network": None})
    yield
    cc.SITES.clear()
    cc.SITES.update(old_sites)
    cc.ACTIVE.clear()
    cc.ACTIVE.update(old_active)


@pytest.fixture
def store(monkeypatch):
    value = _Store()
    monkeypatch.setattr(cb, "call", value.call)
    monkeypatch.setattr(cc, "_doc", lambda doc_id: doc_id)
    return value


def _buried_inputs():
    return {"cover_m": 2.0, "od_m": 0.6, "diameter_m": 0.6, "gamma_kn_m3": 18.0,
            "wheel_load_kn": 80.0, "contact_w_m": 0.2, "contact_l_m": 0.4,
            "impact_factor": 1.25, "ring_stiffness_kpa": 1000.0, "ring_ei_kn_m": 27.0,
            "e_prime_kpa": 2000.0, "applied_pressure_kpa": 50.0,
            "empty_weight_kn_m": 0.5, "gw_depth_m": 0.25,
            "gamma_dry_kn_m3": 18.0, "gamma_sat_kn_m3": 20.0, "fs_required": 1.5,
            "material": "PVC", "criteria": {"allowable_deflection_pct_by_material": {"PVC": 0.01}}}


def _piping():
    return {"schema": "aicad.piping/1", "id": "plant-network", "name": "two runs",
            "units": {"system": "MMGS"}, "material": "A106-B",
            "runs": [{"id": "r1", "path": [[0, 0, 0], [6000, 0, 0], [6000, 4000, 0]],
                      "nps": 2.0, "schedule": "40"},
                     {"id": "r2", "path": [[3000, 0, 0], [3000, -5000, 0]],
                      "nps": 2.0, "schedule": "40"}],
            "fittings": [{"id": "elbow", "type": "elbow", "run_id": "r1",
                          "origin": [6000, 0, 0], "params": {"angle": 90.0}}]}


def _coupling_inputs():
    model = schema.new_model("Coupled site")
    model["water_networks"] = [schema.water_network(
        "w1", "Laminar feeder", headloss="D-W",
        junctions=[{"id": "j1", "x": _L, "y": 0.0, "elevation": 10.0,
                    "demand_m3s": 0.0, "pattern": None}],
        reservoirs=[{"id": "res", "x": 0.0, "y": 0.0, "head": 40.0, "pattern": None}],
        pipes=[{"id": "feeder", "from": "res", "to": "j1", "length": _L,
                "diameter": _D, "roughness": 0.0, "minor_loss": 0.0,
                "status": "open", "material": None, "vertices": []}],
        options={"duration_s": 0, "demand_model": "DDA", "viscosity_m2s": _NU,
                 "accuracy": 1e-11, "max_iter": 200})]
    model["tie_ins"] = [{"id": "t1", "civil_network": "w1", "civil_node": "j1",
                         "piping_doc_id": "plant", "piping_study": "piping_network",
                         "piping_endpoint": {"run_id": "r1", "end": "start"},
                         "direction": "supply", "elevation_offset_m": 0.0}]
    plant = {"schema": "aicad.piping/1", "id": "fixed-resistance", "name": "Plant",
             "units": {"system": "MMGS"}, "material": "A106-B",
             "runs": [{"id": "r1", "path": [[0, 0, 10000], [1000, 0, 10000]],
                       "nps": 0.5, "schedule": "40"}], "fittings": []}
    return model, plant, [{"run": "r1", "end": "end", "pressure": 0.0}], {"rho": 1000.0, "mu": 1000*_NU}


def _plant(params):
    pressures = {row["end"]: row["pressure"] for row in params["boundary"]}
    dp = pressures["start"]-pressures["end"]
    flow = math.copysign(_K*math.sqrt(abs(dp)), dp)
    run = params["doc"]["runs"][0]
    return {"ok": True, "converged": True,
            "nodes": [{"id": "n0", "xyz": run["path"][0], "pressure_kpa": pressures["start"]},
                      {"id": "n1", "xyz": run["path"][-1], "pressure_kpa": pressures["end"]}],
            "links": [{"id": "r1", "from": "n0", "to": "n1", "flow_m3h": flow*3600}],
            "warnings": []}


@pytest.mark.parametrize("trench_width", [None, 1.2])
def test_buried_composition_matches_direct_engine_and_persists(store, trench_width):
    tools = _tools()
    data = _buried_inputs()
    if trench_width:
        data["trench_width_m"] = trench_width
    result = _j(tools["civil_buried_pipe_check"](json.dumps(data), site=schema.new_model("Buried site"), doc_id="doc-1"))
    assert result["ok"] and result["saved_to_study"]
    if trench_width:
        earth = buried.marston_trench_load(18, 2, trench_width, 0.6)
        earth_pressure = earth["flexible_pressure_kpa"]
    else:
        earth = buried.prism_load(18, 2, 0.6)
        earth_pressure = 36.0  # gamma H = 18*2 kPa.
    live = buried.live_load_pressure(80, 0.2, 0.4, 2, 1.25)
    deflection = buried.iowa_deflection(earth_pressure, live["pressure_kpa"], 1000, 0.6,
                                        e_prime_kpa=2000, material="PVC", criteria=data["criteria"],
                                        earth_load_basis="marston" if trench_width else "prism")
    buckling = buried.ring_buckling(0.6, 27, 2000, 2, 1.75, 50)
    flotation = buried.flotation(0.6, 0.5, 2, 0.25, 18, 20, 1.5)
    for name, expected in (("earth", earth), ("live", live), ("deflection", deflection),
                           ("buckling", buckling), ("flotation", flotation)):
        assert result[name] == expected
    assert result["pressure_kpa"] == pytest.approx(earth_pressure+live["pressure_kpa"])
    assert store.civil()["results"]["buried"]["checks"] == result["checks"]
    handoff = _j(tools["civil_fea_handoff"](source="buried", face_selector="crown"))
    assert handoff["ok"] and sh.validate_envelope(handoff) == []
    assert handoff["conditions"][0]["value"] == pytest.approx(result["pressure_kpa"]*1000)
    assert handoff["conditions"][0]["units"] == "Pa"


def test_cover_uses_surface_minus_conduit_crown_and_invert_overrides(store):
    model = schema.new_model("Surface cover")
    model["surfaces"] = [{"id": "s1", "name": "Sloping grade", "kind": "tin",
                          "points": [[0, -5, 10], [20, -5, 12], [20, 5, 12], [0, 5, 10]],
                          "triangles": [[0, 1, 2], [0, 2, 3]]}]
    model["gravity_networks"] = [schema.gravity_network(
        "g1", nodes=[{"id": "a", "type": "manhole", "x": 2, "y": 0, "rim": 10.2, "invert": 6},
                     {"id": "b", "type": "outfall", "x": 18, "y": 0, "rim": 11.8, "invert": 6}],
        conduits=[{"id": "c1", "from": "a", "to": "b", "shape": "circular", "diameter": 0.6,
                   "n": 0.013, "inlet_invert": 7.0, "outlet_invert": 7.4, "vertices": []}])]
    data = _buried_inputs()
    data.pop("cover_m")
    result = _j(_tools()["civil_buried_pipe_check"](data, conduit_id="c1", network_id="g1",
                                                  surface_id="s1", site=model, doc_id="doc-1"))
    assert result["ok"]
    # Mid-station at x=10: grade 11; invert 7.2; crown 7.8; cover 3.2m.
    assert result["cover_source"]["xy_m"] == pytest.approx([10, 0])
    assert result["cover_source"]["crown_elevation_m"] == pytest.approx(7.8)
    assert result["cover_m"] == pytest.approx(3.2)
    assert result["earth"]["pressure_kpa"] == pytest.approx(18*3.2)
    assert "cover_m" not in data  # tool does not rewrite caller inputs.


def test_thrust_water_head_closed_form_then_fea_round_trip_and_edit_invalidates(store):
    tools = _tools()
    model, _, _, _ = _coupling_inputs()
    before = _j(tools["civil_fea_handoff"](site=model))
    assert before["reason"] == "no_thrust_result"
    solved = _j(tools["civil_water_solve"]("w1", detail=True, doc_id="doc-1"))
    assert solved["ok"] and solved["converged"]
    result = _j(tools["civil_thrust_restraint"](fitting={"kind": "bend", "direction": [1, 0, 0]},
                                               angle_deg=90, diameter_m=0.3,
                                               network_id="w1", node_id="j1", doc_id="doc-1"))
    assert result["ok"] and result["saved_to_study"]
    pressure = 1000*_G*(solved["timesteps"][0]["nodes"]["j1"]["head_m"]-10)/1000
    expected = math.sqrt(2)*pressure*(math.pi*0.3**2/4)
    assert result["thrust_kn"] == pytest.approx(expected)
    assert result["force_vector_n"] == pytest.approx([1000*expected, 0, 0])
    record = copy.deepcopy(store.civil())
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    loaded = _j(tools["civil_load"]("doc-1"))
    assert loaded["ok"]
    entry = cc.resolve_site(None)
    assert entry["model"] == record["params"]["model"]
    assert entry["results"] == record["results"]
    handoff = _j(tools["civil_fea_handoff"](target_doc_id="block", face_selector="bearing"))
    assert handoff["ok"] and sh.validate_envelope(handoff) == []
    assert handoff["conditions"][0]["value"] == pytest.approx([1000*expected, 0, 0])
    assert handoff["target"]["tool"] == "run_fea_static"
    changed_network = copy.deepcopy(model["water_networks"][0])
    changed_network["pipes"][0]["length"] *= 2
    edited = _j(tools["civil_site_edit"]([{"op": "upsert", "collection": "water_networks",
                                         "element": changed_network}], doc_id="doc-1"))
    assert edited["ok"] and cc.resolve_site(None)["results"] == {}
    assert store.civil()["results"] == {}
    assert _j(tools["civil_fea_handoff"]())["reason"] == "no_thrust_result"
    assert _j(tools["civil_requirement_evidence"]({}, source_key="water"))["reason"] == "no_water_result"


def test_thrust_block_and_dipra_restraint_match_real_engine(store):
    restrained = {"od_m": 0.32, "cover_m": 1.5, "gamma_kn_m3": 18,
                  "pipe_weight_kn_m": 0.2, "contents_weight_kn_m": 0.7,
                  "cohesion_kpa": 10, "friction_angle_rad": math.radians(30),
                  "adhesion_factor": 0.5, "friction_angle_factor": 0.8,
                  "bearing_reduction_factor": 0.7}
    result = _j(_tools()["civil_thrust_restraint"](
        pressure_kpa=500, fitting={"kind": "bend", "block": {"depth_m": 2, "height_max_m": 1}},
        angle_deg=90, diameter_m=0.3, bearing_kpa=150, restrained=restrained,
        site=schema.new_model("Block design"), doc_id="doc-1"))
    assert result["ok"]
    area = math.pi*0.3**2/4
    force = buried.thrust_force(500, area, "bend", math.pi/2)
    assert result["block"] == buried.thrust_block(force["thrust_kn"], 150, depth=2, height_max=1)
    assert result["restraint"] == buried.restrained_length(500, area, math.pi/2, **restrained)
    assert _j(_tools()["civil_fea_handoff"]())["reason"] == "force_direction_required"


@pytest.mark.parametrize("source", ["inline", "session", "saved"])
def test_supports_bend_reactions_and_read_only_piping_resolution(store, source):
    doc = _piping()
    args = {"material": _MATERIAL, "fitting_weights_n": {"elbow": 50},
            "site": schema.new_model("Rack site"), "doc_id": "doc-1"}
    if source == "inline":
        args["piping"] = doc
    elif source == "session":
        pt._NETWORKS["rack"] = {"doc": doc}
        args["network"] = "rack"
    else:
        store.documents["doc-1"] = [{"id": "saved-plant", "type": "piping_network", "name": "rack",
                                     "params": {"piping": doc}, "results": {"solve": {"old": True}}}]
        args["network"] = "rack"
    old_networks, old_active = copy.deepcopy(pt._NETWORKS), copy.deepcopy(pt._PIPING)
    result = _j(_tools()["civil_pipe_supports"](**args))
    assert result["ok"] and result["saved_to_study"]
    assert pt._NETWORKS == old_networks and pt._PIPING == old_active
    assert any(s["run_id"] == "r1" and s["location_mm"] == [6000, 0, 0] for s in result["supports"])
    # ASME B36.10M NPS2 Sch40: OD=.0603m, wall=.00391m. Total run length=15m.
    od, inside = 0.0603, 0.0603-2*0.00391
    per_m = _G*(7850*math.pi*(od**2-inside**2)/4+1000*math.pi*inside**2/4)
    assert result["total_weight_n"] == pytest.approx(15*per_m+50)
    assert result["total_reaction_n"] == pytest.approx(result["total_weight_n"])
    direct = supports.supports_for_piping(doc, **_MATERIAL, fitting_weights_n={"elbow": 50})
    assert result["supports"] == direct["supports"]
    assert store.civil()["results"]["supports"]["total_reaction_n"] == pytest.approx(15*per_m+50)


@pytest.mark.parametrize("packaged", [False, True])
def test_coupling_native_adapter_matches_analytic_intersection_and_saves_handoffs(store, packaged):
    tools = _tools()
    model, doc, boundary, fluid = _coupling_inputs()
    store.plant = _plant
    before_model, before_doc = copy.deepcopy(model), copy.deepcopy(doc)
    args = {"piping": {"doc": doc, "boundary": boundary, "fluid": fluid}} if packaged else {
        "piping": doc, "boundary": json.dumps(boundary), "fluid": json.dumps(fluid)}
    result = _j(tools["civil_piping_couple"]("w1", **args, tie_in_ids=["t1"],
                                            tol_rel=1e-7, max_iter=100, site=model, doc_id="doc-1"))
    assert result["ok"] and result["converged"] and result["saved_to_study"]
    # Laminar h_loss=aQ and plant Q=k sqrt(rho*g*(30-aQ)/1000).
    a = 128*_NU*_L/(math.pi*_G*_D**4)
    c = 1000*_G/1000
    beta = _K**2*c*a
    expected = (-beta+math.sqrt(beta**2+4*_K**2*c*30))/2
    assert 4*expected/(math.pi*_D*_NU) < 2000
    assert result["tie_ins"]["t1"]["flow_m3s"] == pytest.approx(expected, rel=3e-7)
    assert result["tie_ins"]["t1"]["head_m"] == pytest.approx(40-a*expected, abs=1e-5)
    assert result["simhandoff"]["verify"]["pass"]
    assert model == before_model and doc == before_doc
    native_calls = [params for method, params in store.calls if method == "piping_solve"]
    assert len(native_calls) == result["iterations"]
    assert all(params["fluid"] == fluid for params in native_calls)
    saved = store.civil()["results"]["tie_ins"]
    assert saved["tie_ins"] == result["tie_ins"]
    for envelope in saved["simhandoff"].values():
        assert sh.validate_envelope(envelope) == []
    evidence = _j(tools["civil_requirement_evidence"]({"min_pressure": "REQ-PRESSURE"}, source_key="tie_ins"))
    assert evidence["ok"]  # Civil checks are usable; simhandoff agreement rows are not recast.
    cc.SITES.clear()
    cc.ACTIVE["name"] = None
    assert _j(tools["civil_load"]("doc-1"))["ok"]
    assert cc.resolve_site(None)["results"]["tie_ins"] == saved


@pytest.mark.parametrize("reply,reason", [({"ok": False, "reason": "no_pressure_boundary"}, "no_pressure_boundary"),
                                           ({"ok": True, "converged": False}, "not_converged")])
def test_piping_refusal_becomes_named_civil_refusal_and_is_not_saved(store, reply, reason):
    model, doc, boundary, fluid = _coupling_inputs()
    store.plant = lambda params: reply
    result = _j(_tools()["civil_piping_couple"]("w1", doc, boundary, fluid, site=model, doc_id="doc-1"))
    assert not result["ok"] and result["reason"] == "piping_solver_failed"
    assert result["solver_reason"] == reason
    assert "tie_ins" not in cc.resolve_site(None)["results"]
    assert not any(s["type"] == "civil_site" for s in store.documents.get("doc-1", []))


def test_requirement_evidence_keeps_failing_check_and_unmapped_rows(store):
    tools = _tools()
    result = _j(tools["civil_buried_pipe_check"](_buried_inputs(), site=schema.new_model("Criteria"), doc_id="doc-1"))
    assert result["ok"]
    evidence = _j(tools["civil_requirement_evidence"]({"vertical_deflection": ["REQ-DEFLECT", "REQ-INSTALL"]}, source_key="buried"))
    assert evidence["ok"]
    check = next(row for row in result["checks"] if row["check"] == "vertical_deflection")
    assert check["pass"] is False
    for requirement in ("REQ-DEFLECT", "REQ-INSTALL"):
        row, = evidence["evidence_by_requirement"][requirement]
        assert row["pass"] is False and row["value"] == check["value"]
        assert row["limit"] == check["limit"] and row["reference"] == check["reference"]
        assert row["units"] == check["units"]
    assert {row["check"] for row in evidence["unmapped"]} == {"ring_buckling", "flotation"}
    assert store.civil()["results"]["buried"]["checks"] == result["checks"]


@pytest.mark.parametrize("tool,kwargs,reason", [
    ("civil_buried_pipe_check", {"inputs": {}}, "missing_input"),
    ("civil_thrust_restraint", {"node_id": "j1", "diameter_m": 0.3}, "no_water_result"),
    ("civil_thrust_restraint", {"pressure_kpa": 100, "diameter_m": 0.3, "angle_deg": 90,
                                "bearing_kpa": 100}, "block_geometry_required"),
    ("civil_pipe_supports", {"piping": _piping(), "material": {}}, "missing_input"),
    ("civil_fea_handoff", {"source": "buried"}, "no_buried_result"),
    ("civil_requirement_evidence", {"requirement_ids_by_check": {}, "source_key": "gravity"}, "no_gravity_result"),
])
def test_missing_prerequisites_are_exact_named_refusals(store, tool, kwargs, reason):
    result = _j(_tools()[tool](**kwargs, site=schema.new_model("Refusal site")))
    assert result["ok"] is False and result["reason"] == reason
    assert cc.resolve_site(None)["results"] == {}

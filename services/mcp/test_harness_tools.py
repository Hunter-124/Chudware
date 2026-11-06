"""Behavioral coverage for harness studies, reports, and engine routing."""
from __future__ import annotations

import copy
import json
import uuid
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from services.harness import model
from services.mcp import core_bridge as cb


enginetest = pytest.mark.skipif(not cb.available(), reason="libchudware not built/staged")


class ToolCollector:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return register


@pytest.fixture
def tools():
    from services.mcp.harness_tools import register_harness_tools

    collector = ToolCollector()
    register_harness_tools(collector)
    return collector.tools


def invoke(tools, tool, **kwargs):
    return json.loads(tools[tool](**kwargs))


def test_harness_category_and_registered_risks(monkeypatch):
    from services.agent import tool_categories
    from services.mcp import tool_registry
    from services.mcp.harness_tools import register_harness_tools
    from services.mcp.harness_electrical_tools import register_harness_electrical_tools

    monkeypatch.setattr(tool_registry, "_CATALOG", {})
    monkeypatch.setattr(tool_registry, "warm_catalog", lambda: None)
    collector = ToolCollector()
    register_harness_tools(tool_registry.view(collector, "ecad"))
    register_harness_electrical_tools(tool_registry.view(collector, "ecad"))
    names = set(collector.tools)
    assert {"harness_new", "harness_route", "harness_check", "harness_report",
            "harness_get", "harness_build_geometry", "harness_formboard",
            "harness_electrical_check", "harness_bus_check"} <= names
    menu = dict((name, set(items)) for name, _, items in tool_categories.tool_menu_groups())
    assert names <= menu["harness"]
    resolved, unknown = tool_categories.resolve_tool_requests(["harness"])
    assert not unknown and names <= resolved
    assert {"harness_electrical_check", "harness_bus_check",
            "harness_cable_model", "harness_to_bus_links"} <= resolved
    catalog = tool_registry.catalog()
    assert all(catalog[name]["family"] == "ecad" for name in names)
    for name in ("harness_check", "harness_report", "harness_get",
                 "harness_electrical_check", "harness_bus_check"):
        assert catalog[name]["risk"] == "read"
    for name in ("harness_new", "harness_add_connector", "harness_add_wire",
                 "harness_route", "harness_build_geometry", "harness_formboard", "harness_edit"):
        assert catalog[name]["risk"] == "write"


@pytest.fixture
def study_rpc(monkeypatch):
    studies = {}
    calls = []

    def call(method, params):
        calls.append((method, copy.deepcopy(params)))
        doc = params["doc_id"]
        rows = studies.setdefault(doc, [])
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(rows)}
        if method == "sim_study_add":
            record = copy.deepcopy(params["study"])
            record["id"] = f"study-{len(rows) + 1}"
            rows.append(record)
            return {"study_id": record["id"], "revision": len(calls)}
        if method == "sim_study_edit":
            index = next(i for i, row in enumerate(rows) if row["id"] == params["study_id"])
            rows[index] = copy.deepcopy(params["study"])
            return {"study_id": params["study_id"], "revision": len(calls)}
        if method == "sim_study_delete":
            rows[:] = [row for row in rows if row["id"] != params["study_id"]]
            return {}
        raise AssertionError(f"unexpected RPC {method}")

    monkeypatch.setattr(cb, "call", call)
    return studies, calls


def routed_harness():
    h = model.new_harness("Control harness", harness_id="control")
    for ref, pos in (("J1", [0, 0, 0]), ("J2", [30, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame(pos, [1, 0, 0]))
    model.add_wire(h, from_="J1:1", to="J2:1", insulation_od_mm=2,
                   gauge_awg=20, service_loop_mm=12, strip_mm=6,
                   min_bend_radius_mm=8)
    model.add_segment(h, "J1", "J2", [[0, 0, 0], [30, 40, 0]])
    return h


def test_study_persistence_roundtrip_and_update(study_rpc):
    studies, calls = study_rpc
    h = routed_harness()
    model.recompute_derived(h)
    saved = model.save_harness("doc-a", h)
    assert saved["saved_to_study"] and saved["study_id"] == "study-1"
    assert studies["doc-a"][0]["type"] == "harness"
    assert model.load_harness("doc-a", "control") == h
    assert model.list_harnesses("doc-a") == [{"id": "control", "name": "Control harness",
                                                "connectors": 2, "wires": 1, "segments": 1}]
    loaded = model.load_harness("doc-a")
    loaded["name"] = "Updated harness"
    assert model.load_harness("doc-a")["name"] == "Control harness"
    model.save_harness("doc-a", loaded)
    assert len(studies["doc-a"]) == 1
    assert model.load_harness("doc-a")["name"] == "Updated harness"
    assert [method for method, _ in calls].count("sim_study_add") == 1
    assert [method for method, _ in calls].count("sim_study_edit") == 1
    assert model.list_harnesses("doc-b") == []


def test_tools_save_and_read_study_with_mock_core_rpc(study_rpc, tools):
    studies, _ = study_rpc
    created = invoke(tools, "harness_new", doc_id="doc-a", name="Control",
                     harness_id="control")
    assert created["ok"] and created["saved_to_study"], created
    for ref, pos in (("J1", [0, 0, 0]), ("J2", [30, 40, 0])):
        result = invoke(tools, "harness_add_connector", doc_id="doc-a", harness_id="control",
                        ref=ref, family="generic", pins=2,
                        frame={"origin": pos, "normal": [1, 0, 0]})
        assert result["ok"], result
    wire = invoke(tools, "harness_add_wire", doc_id="doc-a", harness_id="control",
                  from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
                  service_loop_mm=12, strip_mm=6, min_bend_radius_mm=8)
    assert wire["ok"], wire
    reloaded = invoke(tools, "harness_get", doc_id="doc-a", harness_id="control")
    assert reloaded["ok"] and len(reloaded["harness"]["connectors"]) == 2
    assert reloaded["harness"]["wires"][0]["id"] == wire["wire"]["id"]
    assert len(studies["doc-a"]) == 1
    h = model.load_harness("doc-a", "control")
    model.add_segment(h, "J1", "J2", [[0, 0, 0], [30, 40, 0]])
    model.save_harness("doc-a", h)
    reported = invoke(tools, "harness_report", doc_id="doc-a", harness_id="control")
    assert reported["ok"], reported
    row, = reported["report"]["wires"]
    assert (row["routed_length_mm"], row["service_loop_mm"], row["strip_mm"],
            row["cut_length_mm"]) == pytest.approx((50, 12, 6, 68))


def test_add_clamp_splits_route_and_appears_on_formboard(study_rpc, tools, tmp_path, monkeypatch):
    from services.mcp import harness_tools

    model.save_harness("doc-a", routed_harness())
    result = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                    action="add_clamp", segment_id="s1", distance_mm=20)
    assert result["ok"] and result["saved_to_study"], result
    clamp = result["clamp"]
    assert clamp["kind"] == "clamp"
    assert clamp["pos"] == pytest.approx([12, 16, 0])
    assert len(result["segments"]) == 2
    h = model.load_harness("doc-a", "control")
    model.recompute_derived(h)
    adjacent = [model.get_segment(h, sid) for sid in result["segments"]]
    assert all(clamp["id"] in (seg["a"], seg["b"]) for seg in adjacent)
    assert sorted(model.seg_length(seg) for seg in adjacent) == pytest.approx([20, 30])
    assert h["wires"][0]["length_mm"] == pytest.approx(50)

    output = tmp_path / "formboard.svg"
    monkeypatch.setattr(harness_tools, "_sanitize_output_path", lambda path: str(output))
    formboard = invoke(tools, "harness_formboard", doc_id="doc-a", harness_id="control",
                       format="svg", path=str(output))
    assert formboard["ok"], formboard
    assert formboard["verification"]["segments_checked"] == 2
    root = ET.parse(output).getroot()
    assert root.find(f".//{{http://www.w3.org/2000/svg}}circle[@data-clamp='{clamp['id']}']") is not None


def test_report_cut_length_includes_route_service_and_strip():
    report = model.build_report(routed_harness())
    assert report["unrouted_wires"] == []
    wire, = report["wires"]
    assert wire["routed_length_mm"] == pytest.approx(50)
    assert wire["service_loop_mm"] == pytest.approx(12)
    assert wire["strip_mm"] == pytest.approx(6)
    assert wire["cut_length_mm"] == pytest.approx(68)
    assert report["cut_list"][0]["total_cut_length_mm"] == pytest.approx(68)
    assert report["summary"]["total_routed_wire_length_mm"] == pytest.approx(50)


def test_route_sourced_defaults_and_overrides(study_rpc, tools, monkeypatch):
    from services.mcp import harness_tools

    h = model.new_harness("Route defaults", harness_id="route")
    for ref, x in (("J1", 0), ("J2", 100)):
        model.add_connector(h, ref=ref, family="generic", pins=3,
                            frame=model.make_frame([x, 0, 0], [1, 0, 0]))
    for pin, kind in enumerate(("wire", "shielded", "coax"), 1):
        wire, _ = model.add_wire(h, from_=f"J1:{pin}", to=f"J2:{pin}",
                                 insulation_od_mm=2, gauge_awg=20, kind=kind)
        assert wire["min_bend_radius_mm"] > 0
        assert wire["bend_radius_basis"]
    model.save_harness("doc-a", h)
    calls = []

    def route_rpc(method, params):
        calls.append((method, copy.deepcopy(params)))
        if method == "harness_route":
            return {"path": [params["start"]["pos"], params["end"]["pos"]]}
        assert method == "harness_path_check"
        return {"bend_violations": [], "clearance_violations": []}

    monkeypatch.setattr(harness_tools, "_rpc", route_rpc)
    result = invoke(tools, "harness_route", doc_id="doc-a", harness_id="route")
    assert result["ok"], result
    assert result["assumptions"]
    routed = next(params for method, params in calls if method == "harness_route")
    assert routed["od_mm"] == pytest.approx(model.bundle_od_mm([2, 2, 2]))
    assert routed["min_bend_radius_mm"] > 0

    edit = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="route",
                  action="edit_wire", wire_id="w1", min_bend_radius_mm=37)
    assert edit["ok"] and edit["wire"]["min_bend_radius_mm"] == 37
    assert edit["wire"]["bend_radius_confidence"] == "explicit"
    edit = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="route",
                  action="edit_segment", segment_id="s1", segment_od_mm=11,
                  segment_min_bend_radius_mm=45, segment_packing_factor=0.5)
    assert edit["ok"], edit
    assert edit["segment"]["od_mm"] == 11
    assert edit["segment"]["min_bend_radius_mm"] == 45
    calls.clear()
    checked = invoke(tools, "harness_check", doc_id="doc-a", harness_id="route")
    assert checked["ok"] and checked["assumptions"], checked
    assert any(p["od_mm"] == 11 and p["min_bend_radius_mm"] == 45
               for method, p in calls if method == "harness_path_check")
    calls.clear()
    rerouted = invoke(tools, "harness_route", doc_id="doc-a", harness_id="route",
                      packing_factor=0.6)
    assert rerouted["ok"], rerouted
    assert any(p["od_mm"] == 11 and p["min_bend_radius_mm"] == 45
               for method, p in calls if method == "harness_path_check")
    persisted = model.load_harness("doc-a", "route")
    assert persisted["defaults"]["packing_factor"] == 0.6
    assert persisted["segments"][0]["override_packing_factor"] == 0.5
    packing_only = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="route",
                          action="edit_segment", segment_id="s1", clear_segment_overrides=["od_mm"])
    assert packing_only["ok"], packing_only
    assert packing_only["segment"]["od_mm"] == pytest.approx(model.bundle_od_mm([2, 2, 2], 0.5), abs=1e-3)
    cleared = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="route",
                     action="edit_segment", segment_id="s1",
                     clear_segment_overrides=["od_mm", "min_bend_radius_mm", "packing_factor"])
    assert cleared["ok"], cleared
    assert "override_od_mm" not in cleared["segment"]
    assert cleared["segment"]["od_mm"] != 11
    assert cleared["segment"]["min_bend_radius_mm"] != 45


@pytest.mark.parametrize("material", ["PVC", "PTFE", "FEP", "ETFE", "XLPE", "silicone", "TPE"])
def test_wire_insulation_create_and_edit(study_rpc, tools, material):
    h = model.new_harness("Materials", harness_id="materials")
    for ref, x in (("J1", 0), ("J2", 100)):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame([x, 0, 0], [1, 0, 0]))
    model.save_harness("doc-a", h)
    added = invoke(tools, "harness_add_wire", doc_id="doc-a", harness_id="materials",
                   from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
                   insulation=material)
    assert added["ok"] and added["wire"]["insulation"] == material, added
    connected = invoke(tools, "harness_connect", doc_id="doc-a", harness_id="materials",
                       from_="J1:2", to="J2:2", insulation_od_mm=2, gauge_awg=20,
                       insulation=material)
    assert connected["ok"] and connected["wire"]["insulation"] == material, connected
    edited = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="materials",
                    action="edit_wire", wire_id=added["wire"]["id"], insulation="PTFE")
    assert edited["ok"] and edited["wire"]["insulation"] == "PTFE", edited
    assert model.load_harness("doc-a", "materials")["wires"][0]["insulation"] == "PTFE"


def test_ribbon_and_netlist_inputs(study_rpc, tools, monkeypatch):
    from services.mcp import ecad_common

    h = model.new_harness("Ribbon", harness_id="ribbon")
    for ref, x in (("J1", 0), ("J2", 100)):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame([x, 0, 0], [1, 0, 0]))
    model.save_harness("doc-a", h)
    added = invoke(tools, "harness_add_wire", doc_id="doc-a", harness_id="ribbon",
                   from_="J1:1", to="J2:1", kind="ribbon", gauge_awg=24,
                   ribbon_width_mm=10, ribbon_thickness_mm=1, insulation="PVC")
    assert added["ok"], added
    assert added["wire"]["insulation_od_mm"] == pytest.approx(101 ** 0.5, abs=1e-3)
    assert added["wire"]["min_bend_radius_mm"] > 0
    monkeypatch.setattr(ecad_common, "_get_sch", lambda _: SimpleNamespace(doc={}))
    monkeypatch.setattr(ecad_common.netlist_mod, "build_netlist", lambda _: {
        "nets": {"DATA": [{"ref": "A", "pin": "1"}, {"ref": "B", "pin": "1"}]}})
    imported = invoke(tools, "harness_from_netlist", doc_id="doc-a", harness_id="ribbon",
                      schematic_id="schematic", mapping={"A:1": "J1:2", "B:1": "J2:2"},
                      wire_spec={"kind": "ribbon", "gauge_awg": 24, "insulation": "FEP",
                                 "ribbon_width_mm": 8, "ribbon_thickness_mm": 1})
    assert imported["ok"], imported
    wire = model.load_harness("doc-a", "ribbon")["wires"][1]
    assert wire["insulation"] == "FEP" and wire["ribbon_width_mm"] == 8


def test_hyphenated_shielded_drain_is_canonical_on_create_and_edit(study_rpc, tools):
    h = model.new_harness("Drain", harness_id="drain")
    for ref, x in (("J1", 0), ("J2", 100)):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame([x, 0, 0], [1, 0, 0]))
    model.save_harness("doc-a", h)
    added = invoke(tools, "harness_add_wire", doc_id="doc-a", harness_id="drain",
                   from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
                   signal_class="shielded-drain", shield_ground_end="from")
    assert added["ok"] and added["wire"]["signal_class"] == "shielded_drain", added
    connected = invoke(tools, "harness_connect", doc_id="doc-a", harness_id="drain",
                       from_="J1:2", to="J2:2", insulation_od_mm=2, gauge_awg=20)
    assert connected["ok"], connected
    edited = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="drain",
                    action="edit_wire", wire_id=connected["wire"]["id"],
                    signal_class="shielded-drain", shield_ground_end="to")
    assert edited["ok"] and edited["wire"]["signal_class"] == "shielded_drain", edited
    assert model.load_harness("doc-a", "drain")["wires"][1]["signal_class"] == "shielded_drain"


def test_controls_findings_and_connector_report(study_rpc, tools, monkeypatch):
    from services.mcp import harness_tools

    h = model.new_harness("Controls", harness_id="controls")
    for ref, x in (("J1", 0), ("J2", 100)):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame([x, 0, 0], [1, 0, 0]))
    model.add_wire(h, from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
                   signal_class="power", signal="supply")
    model.add_wire(h, from_="J1:2", to="J2:2", insulation_od_mm=2, gauge_awg=20,
                   signal_class="data", kind="shielded", shield_ground_end="none")
    model.add_segment(h, "J1", "J2", [[0, 0, 0], [100, 0, 0]])
    model.save_harness("doc-a", h)
    monkeypatch.setattr(harness_tools, "_rpc", lambda method, params: {
        "bend_violations": [], "clearance_violations": []})
    checked = invoke(tools, "harness_check", doc_id="doc-a", harness_id="controls")
    assert checked["ok"], checked
    assert {"segregation", "shield_termination"} <= {row["type"] for row in checked["issues"]}
    report = invoke(tools, "harness_report", doc_id="doc-a", harness_id="controls")
    assert report["ok"], report
    connector = next(c for c in report["report"]["connectors"] if c["ref"] == "J1")
    assert any(p["pin"] == "1" and p["to_connector"] == "J2" and p["to_pin"] == "1"
               for p in connector["pinout"])
    assert any(row["to"] == "J2" and len(row["wires"]) == 2 for row in connector["from_to"])


@pytest.fixture
def engine_doc(monkeypatch):
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    doc = cb.call("doc_new", {"name": "harness_" + uuid.uuid4().hex, "kind": "part"})["doc_id"]
    try:
        yield doc
    finally:
        cb.call("doc_close", {"doc_id": doc})


@enginetest
def test_path_check_detects_tight_bend(engine_doc):
    params = {"doc_id": engine_doc, "od_mm": 2, "min_bend_radius_mm": 10,
              "clearance_mm": 0}
    straight = cb.call("harness_path_check", {
        **params, "path": [[0, 0, 0], [10, 0, 0], [20, 0, 0]]})
    assert straight["ok"] and straight["bend_violations"] == [], straight
    tight = cb.call("harness_path_check", {
        **params, "path": [[0, 0, 0], [5, 0, 0], [5, 5, 0]]})
    assert tight["ok"], tight
    assert tight["bend_violations"], tight
    assert tight["bend_violations"][0]["radius_mm"] < 10


@enginetest
def test_engine_routes_around_box_and_checks_clearance(engine_doc, tools):
    seed = cb.call("doc_apply_ops", {"doc_id": engine_doc, "ops": [{
        "op": "add_feature", "feature": {"type": "box", "name": "Obstacle",
            "params": {"length": 20, "width": 20, "height": 20}}}]})
    assert all(item["ok"] for item in seed["rebuild"]["feature_status"]), seed
    params = {"doc_id": engine_doc, "od_mm": 1, "min_bend_radius_mm": 2,
              "clearance_mm": 1}
    start, end = [-25, 5, 5], [45, 5, 5]
    blocked = cb.call("harness_path_check", {**params, "path": [start, end]})
    assert blocked["ok"] and blocked["clearance_violations"], blocked
    route = cb.call("harness_route", {**params, "start": {"pos": start},
                    "end": {"pos": end}, "grid_mm": 3})
    assert route["ok"], route
    assert route["path"][0] == pytest.approx(start)
    assert route["path"][-1] == pytest.approx(end)
    assert route["length_mm"] > 70
    checked = cb.call("harness_path_check", {**params, "path": route["path"]})
    assert checked["ok"] and checked["clearance_violations"] == [], checked
    h = model.new_harness("Routed harness", harness_id="routed")
    for ref, pos in (("J1", start), ("J2", end)):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame(pos, [1, 0, 0]))
    model.add_wire(h, from_="J1:1", to="J2:1", insulation_od_mm=1,
                   gauge_awg=24, min_bend_radius_mm=2)
    model.add_segment(h, "J1", "J2", route["path"])
    model.recompute_derived(h)
    model.save_harness(engine_doc, h)
    before = cb.call("eval_mass", {"doc_id": engine_doc})["volume_mm3"]
    built = invoke(tools, "harness_build_geometry", doc_id=engine_doc, harness_id="routed")
    assert built["ok"], built
    ids = built["geometry"]["feature_ids"]
    assert len(ids) == 4 and len(built["geometry"]["sweep_ids"]) == 1
    tree = cb.call("doc_tree", {"doc_id": engine_doc})["tree"]
    assert set(ids) <= {feature["id"] for feature in tree["features"]}
    assert cb.call("eval_mass", {"doc_id": engine_doc})["volume_mm3"] > before
    rebuilt = invoke(tools, "harness_build_geometry", doc_id=engine_doc, harness_id="routed")
    assert rebuilt["ok"] and rebuilt["unchanged"], rebuilt
    assert rebuilt["geometry"]["feature_ids"] == ids

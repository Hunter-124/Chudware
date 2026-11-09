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
    assert routed["od_mm"] == pytest.approx(model.bundle_od_mm([2, 2, 2]), abs=1e-4)
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


def unrouted_harness():
    h = model.new_harness("Unrouted", harness_id="unrouted")
    for ref, pos in (("J1", [0, 0, 0]), ("J2", [30, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame(pos, [1, 0, 0]))
    model.add_wire(h, from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
                   min_bend_radius_mm=8)
    return h


def shared_harness():
    """w1 and w2 both run J1 -> J2 on the single segment s1."""
    h = model.new_harness("Shared", harness_id="shared")
    for ref, pos in (("J1", [0, 0, 0]), ("J2", [30, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=3,
                            frame=model.make_frame(pos, [1, 0, 0]))
    for pin in (1, 2):
        model.add_wire(h, from_=f"J1:{pin}", to=f"J2:{pin}", insulation_od_mm=2,
                       gauge_awg=20, min_bend_radius_mm=8)
    model.add_segment(h, "J1", "J2", [[0, 0, 0], [30, 40, 0]])
    return h


@pytest.fixture
def route_calls(monkeypatch):
    """Engine RPCs replaced: routes take a fixed detour; checks pass unless the path is listed."""
    from services.mcp import harness_tools

    state = {"calls": [], "bad_paths": []}

    def rpc(method, params):
        state["calls"].append((method, copy.deepcopy(params)))
        if method == "harness_route":
            start, end = params["start"]["pos"], params["end"]["pos"]
            detour = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2 + 15, 0]
            return {"path": [start, detour, end]}
        bad = params["path"] in state["bad_paths"]
        return {"bend_violations": [{"index": 1, "radius_mm": 1.0}] if bad else [],
                "clearance_violations": []}

    monkeypatch.setattr(harness_tools, "_rpc", rpc)
    return state


def test_segment_geometry_keeps_full_precision_and_ends_equal_nodes():
    h = model.new_harness("Precise", harness_id="precise")
    for ref, pos in (("J1", [0, 0, 0]), ("J2", [30, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame(pos, [1, 0, 0]))
    third = 1 / 3
    seg = model.add_segment(h, "J1", "J2", [[0, 0, 0], [third, 2 * third, 0.123456789], [30, 40, 0]])
    assert seg["path"][1] == [third, 2 * third, 0.123456789]
    node_id = model.split_segment_at(h, seg["id"], [10 / 7, 25 / 7, 0.3])
    node = model.get_node(h, node_id)
    first, second = h["segments"]
    assert first["path"][-1] == node["pos"] == second["path"][0]
    assert first["path"][0] == model.get_node(h, "J1")["pos"]
    assert second["path"][-1] == model.get_node(h, "J2")["pos"]


def branched_harness():
    h = model.new_harness("Branch", harness_id="branch")
    for ref, pos in (("A", [0, 0, 0]), ("B", [100, 0, 0]), ("C", [50, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2,
                            frame=model.make_frame(pos, [1, 0, 0]))
    # Recorded as a split of the A-B trunk, exactly as split_segment_at leaves it.
    h["nodes"].append({"id": "n1", "kind": "breakout", "pos": [50, 0, 0], "through": ["s1", "s2"]})
    model.add_segment(h, "A", "n1", [[0, 0, 0], [50, 0, 0]])
    model.add_segment(h, "n1", "B", [[50, 0, 0], [100, 0, 0]])
    model.add_segment(h, "n1", "C", [[50, 0, 0], [50, 40, 0]])
    model.add_wire(h, from_="A:1", to="B:1", insulation_od_mm=2, gauge_awg=20)
    model.add_wire(h, from_="C:1", to="A:2", insulation_od_mm=2, gauge_awg=20)
    model.recompute_derived(h)
    return h


def test_removing_a_branch_wire_prunes_it_and_merges_the_trunk():
    h = branched_harness()
    report = model.remove_wire(h, "w2")
    assert report["removed_segments"] == ["s3"]
    assert report["merged_segments"] == [{"node": "n1", "kept": "s1", "dropped": "s2"}]
    trunk, = h["segments"]
    assert (trunk["a"], trunk["b"]) == ("A", "B")
    assert trunk["path"] == [[0, 0, 0], [50, 0, 0], [100, 0, 0]]
    assert trunk["wires"] == ["w1"]
    assert [n["id"] for n in h["nodes"]] == ["A", "B", "C"]


def test_breakout_halves_with_different_overrides_are_not_merged():
    h = branched_harness()
    h["segments"][1]["override_od_mm"] = 3
    report = model.remove_wire(h, "w2")
    assert report["merged_segments"] == []
    assert [s["id"] for s in h["segments"]] == ["s1", "s2"]
    assert model.get_node(h, "n1")["kind"] == "breakout"


def test_remove_wire_prunes_stale_segment_and_next_route_repaths(study_rpc, tools, route_calls):
    model.save_harness("doc-a", routed_harness())
    removed = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                     action="remove_wire", wire_id="w1")
    assert removed["ok"] and removed["removed_segments"] == ["s1"], removed
    assert model.load_harness("doc-a", "control")["segments"] == []
    added = invoke(tools, "harness_add_wire", doc_id="doc-a", harness_id="control",
                   from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=8)
    assert added["ok"], added
    assert model.load_harness("doc-a", "control")["wires"][0]["length_mm"] is None
    routed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="control")
    assert routed["ok"], routed
    assert [method for method, _ in route_calls["calls"]].count("harness_route") == 1
    h = model.load_harness("doc-a", "control")
    segment, = h["segments"]
    assert len(segment["path"]) == 3
    assert h["wires"][0]["length_mm"] > 50


def test_reroute_refuses_a_segment_shared_with_unselected_wires(study_rpc, tools, route_calls):
    model.save_harness("doc-a", shared_harness())
    refused = invoke(tools, "harness_route", doc_id="doc-a", harness_id="shared",
                     wire_ids=["w1"], reroute=True)
    assert not refused["ok"] and refused["code"] == "reroute_shared_segment", refused
    assert refused["wires"] == ["w2"] and "wire_ids" in refused["hint"]
    assert not route_calls["calls"]
    assert model.load_harness("doc-a", "shared")["segments"][0]["path"] == [[0, 0, 0], [30, 40, 0]]
    done = invoke(tools, "harness_route", doc_id="doc-a", harness_id="shared",
                  wire_ids=["w1", "w2"], reroute=True)
    assert done["ok"] and done["removed_segments"] == ["s1"], done
    segment, = model.load_harness("doc-a", "shared")["segments"]
    assert len(segment["path"]) == 3 and segment["wires"] == ["w1", "w2"]


def test_final_check_failure_on_an_existing_segment_hints_reroute(study_rpc, tools, route_calls):
    model.save_harness("doc-a", shared_harness())
    route_calls["bad_paths"].append([[0, 0, 0], [30, 40, 0]])
    failed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="shared", wire_ids=["w1"])
    assert failed["code"] == "route_check_failed" and failed["segment"] == "s1", failed
    assert failed["predates_call"] is True and failed["wires"] == ["w1", "w2"]
    assert "reroute=True" in failed["hint"] and "w1" in failed["hint"] and "w2" in failed["hint"]
    assert failed["detail"]["bend_violations"]


def test_edit_connector_unroutes_touching_segments_and_reports_wires(study_rpc, tools):
    model.save_harness("doc-a", routed_harness())
    same = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                  action="edit_connector", connector_id="J1", frame={"origin": [0, 0, 0]})
    assert same["ok"] and same["changed"] is False and same["unrouted_wires"] == [], same
    assert len(model.load_harness("doc-a", "control")["segments"]) == 1
    for bad_args, code in (({}, "nothing_to_edit"),
                           ({"frame": {"origin": [0, 0, 0], "normal": [0, 0, 0]}}, "zero_vector"),
                           ({"frame": {"centre": [0, 0, 0]}}, "bad_frame")):
        refused = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                         action="edit_connector", connector_id="J1", **bad_args)
        assert not refused["ok"] and refused["code"] == code, refused
    edited = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                    action="edit_connector", connector_id="J2",
                    frame={"origin": [30, 50, 0], "normal": [0, 1, 0]}, exit_dir=[0, 0, 2])
    assert edited["ok"] and edited["changed"], edited
    assert edited["unrouted_wires"] == ["w1"] and edited["removed_segments"] == ["s1"]
    assert edited["connector"]["frame"]["origin"] == [30, 50, 0]
    assert edited["connector"]["exit_dir"] == [0, 0, 1]
    h = model.load_harness("doc-a", "control")
    assert h["segments"] == [] and h["wires"][0]["length_mm"] is None
    assert model.get_node(h, "J2")["pos"] == [30, 50, 0]


def test_a_new_connector_frame_drops_its_stale_face_placement():
    h = model.new_harness("Attach", harness_id="attach")
    conn, _ = model.add_connector(h, ref="J1", family="generic", pins=2,
                                  frame=model.make_frame([0, 0, 0], [1, 0, 0]), attach={"sid": "face-1"})
    model.edit_connector(h, "J1", exit_dir=[0, 1, 0])
    assert conn["attach"] == {"sid": "face-1"}  # re-aiming the exit leaves the placement alone
    model.edit_connector(h, "J1", frame={"origin": [5, 0, 0]})
    assert "attach" not in conn and conn["frame"]["origin"] == [5, 0, 0]


def test_remove_connector_refuses_while_a_wire_uses_it(study_rpc, tools):
    model.save_harness("doc-a", routed_harness())
    refused = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                     action="remove_connector", connector_id="J2")
    assert refused["code"] == "connector_in_use" and refused["wires"] == ["w1"], refused
    assert len(model.load_harness("doc-a", "control")["connectors"]) == 2
    invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
           action="remove_wire", wire_id="w1")
    removed = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                     action="remove_connector", connector_id="J2")
    assert removed["ok"] and removed["removed_connector"] == "J2", removed
    h = model.load_harness("doc-a", "control")
    assert [c["id"] for c in h["connectors"]] == ["J1"] and model.get_node(h, "J2") is None


def test_remove_segment_and_delete_harness(study_rpc, tools):
    studies, _ = study_rpc
    model.save_harness("doc-a", routed_harness())
    unknown = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                     action="remove_segment", segment_id="s9")
    assert unknown["code"] == "unknown_segment", unknown
    gone = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                  action="remove_segment", segment_id="s1")
    assert gone["ok"] and gone["unrouted_wires"] == ["w1"] and gone["removed_segments"] == ["s1"], gone
    h = model.load_harness("doc-a", "control")
    assert h["segments"] == [] and h["wires"][0]["length_mm"] is None
    deleted = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                     action="delete_harness")
    assert deleted["ok"] and deleted["deleted_harness"] == "control", deleted
    assert studies["doc-a"] == [] and model.list_harnesses("doc-a") == []
    again = invoke(tools, "harness_edit", doc_id="doc-a", harness_id="control",
                   action="delete_harness")
    assert again["code"] == "unknown_harness", again


@pytest.mark.parametrize("reply,hint_parts", [
    ({"ok": False, "code": "start_blocked", "error": "start is inside an obstacle or its clearance envelope",
      "endpoint": "start", "pos": [0, 0, 0], "obstacle": "bracket (#3)", "distance_mm": 0.4,
      "required_mm": 1.25, "inside_solid": False},
     ("start J1", "bracket (#3)", "0.4 mm", "1.25 mm", "edit_connector")),
    ({"ok": False, "code": "exit_blocked", "error": "exit stub intersects an obstacle",
      "endpoint": "end", "dir": [0, -1, 0], "stub_mm": 16.0, "free_mm": 3.2, "obstacle": "keepout:ecu"},
     ("end J2", "keepout:ecu", "16 mm", "3.2 mm", "edit_connector")),
    ({"ok": False, "code": "no_path", "error": "no route satisfies min_bend_radius_mm", "reason": "turn_infeasible",
      "hint": "an endpoint direction may point away from the target"},
     ("an endpoint direction may point away from the target",)),
])
def test_route_failure_names_wire_endpoints_and_passes_engine_detail(study_rpc, tools, monkeypatch,
                                                                      reply, hint_parts):
    model.save_harness("doc-a", unrouted_harness())
    base = cb.call
    monkeypatch.setattr(cb, "call", lambda method, params: reply if method == "harness_route"
                        else base(method, params))
    failed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="unrouted")
    assert not failed["ok"] and failed["code"] == reply["code"], failed
    assert failed["wire"] == "w1"
    assert failed["start"] == {"node": "J1", "kind": "connector", "pos": [0, 0, 0], "connector": "J1"}
    assert failed["end"]["node"] == "J2" and failed["end"]["connector"] == "J2"
    assert failed["detail"] == reply
    for part in hint_parts:
        assert part in failed["hint"], failed["hint"]


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


def add_engine_connector(tools, doc, harness, ref, origin, normal, pins=2):
    added = invoke(tools, "harness_add_connector", doc_id=doc, harness_id=harness, ref=ref,
                   family="generic", pins=pins, frame={"origin": origin, "normal": normal})
    assert added["ok"], added


def add_engine_wire(tools, doc, harness, from_, to):
    wire = invoke(tools, "harness_add_wire", doc_id=doc, harness_id=harness, from_=from_, to=to,
                  insulation_od_mm=1.9, gauge_awg=20, min_bend_radius_mm=5)
    assert wire["ok"], wire


def bend_issues(checked):
    return [i for i in checked["issues"] if i["type"] in ("bend", "wire_bend")]


@enginetest
def test_engine_bent_route_meets_its_bend_requirement_through_the_tools(engine_doc, tools):
    assert invoke(tools, "harness_new", doc_id=engine_doc, name="Bend", harness_id="bend")["ok"]
    add_engine_connector(tools, engine_doc, "bend", "A", [0, 0, 0], [1, 0, 0])
    add_engine_connector(tools, engine_doc, "bend", "B", [80, 40, 0], [0, -1, 0])
    add_engine_wire(tools, engine_doc, "bend", "A:1", "B:1")
    routed = invoke(tools, "harness_route", doc_id=engine_doc, harness_id="bend", clearance_mm=0.3)
    assert routed["ok"], routed
    checked = invoke(tools, "harness_check", doc_id=engine_doc, harness_id="bend", clearance_mm=0.3)
    assert checked["ok"], checked
    assert bend_issues(checked) == [], checked["issues"]
    h = model.load_harness(engine_doc, "bend")
    segment, = h["segments"]
    assert segment["path"][0] == model.get_node(h, "A")["pos"]
    assert segment["path"][-1] == model.get_node(h, "B")["pos"]
    assert model.seg_length(segment) > model.vdist([0, 0, 0], [80, 40, 0])


@enginetest
def test_engine_branch_leaves_the_trunk_with_its_own_bend_radius(engine_doc, tools):
    """A side branch makes a real corner at the breakout; the wire is judged there as a fillet."""
    assert invoke(tools, "harness_new", doc_id=engine_doc, name="Branch", harness_id="branch")["ok"]
    add_engine_connector(tools, engine_doc, "branch", "A", [0, 0, 0], [1, 0, 0])
    add_engine_connector(tools, engine_doc, "branch", "B", [120, 0, 0], [-1, 0, 0])
    add_engine_connector(tools, engine_doc, "branch", "C", [60, 30, 0], [0, -1, 0])
    add_engine_wire(tools, engine_doc, "branch", "A:1", "B:1")
    add_engine_wire(tools, engine_doc, "branch", "C:1", "A:2")
    trunk = invoke(tools, "harness_route", doc_id=engine_doc, harness_id="branch",
                   wire_ids=["w1"], clearance_mm=0.3)
    assert trunk["ok"], trunk
    branch = invoke(tools, "harness_route", doc_id=engine_doc, harness_id="branch",
                    wire_ids=["w2"], clearance_mm=0.3)
    assert branch["ok"], branch
    h = model.load_harness(engine_doc, "branch")
    junction, = [n for n in h["nodes"] if n["kind"] == "breakout"]
    ends = [seg["path"][i] for seg in h["segments"]
            for end, i in (("a", 0), ("b", -1)) if seg[end] == junction["id"]]
    assert len(ends) == 3 and all(p == junction["pos"] for p in ends)
    w2 = next(w for w in h["wires"] if w["id"] == "w2")
    points, joints = model.wire_route(h, w2)
    assert len(joints) == 1 and points[joints[0]] == junction["pos"]
    j = joints[0]
    turn = model.angle_deg(model.vsub(points[j], points[j - 1]), model.vsub(points[j + 1], points[j]))
    assert turn > 30, turn  # the junction is a real corner, not a collinear vertex
    checked = invoke(tools, "harness_check", doc_id=engine_doc, harness_id="branch", clearance_mm=0.3)
    assert checked["ok"], checked
    assert bend_issues(checked) == [], checked["issues"]


def growing_branch_scene(tools, doc):
    """A -> B trunk; C0's wires go to both ends, so its branch taps the trunk and enlarges it."""
    assert invoke(tools, "harness_new", doc_id=doc, name="Grow", harness_id="grow")["ok"]
    add_engine_connector(tools, doc, "grow", "A", [0, 0, 0], [1, 0, 0], pins=4)
    add_engine_connector(tools, doc, "grow", "B", [80, 40, 0], [0, -1, 0], pins=4)
    add_engine_connector(tools, doc, "grow", "C0", [60, 30, 0], [1, 0, 0], pins=4)
    for from_, to in (("A:1", "B:1"), ("C0:1", "B:3"), ("C0:2", "B:4"), ("C0:3", "A:3")):
        add_engine_wire(tools, doc, "grow", from_, to)


@enginetest
def test_engine_bundle_that_grows_as_wires_join_is_routed_for_its_final_wires(engine_doc, tools):
    growing_branch_scene(tools, engine_doc)
    routed = invoke(tools, "harness_route", doc_id=engine_doc, harness_id="grow", clearance_mm=0.3)
    assert routed["ok"], routed
    assert routed["repathed_segments"], routed  # the trunk outgrew its first sizing and was routed again
    checked = invoke(tools, "harness_check", doc_id=engine_doc, harness_id="grow", clearance_mm=0.3)
    assert checked["issues"] == [], checked["issues"]
    h = model.load_harness(engine_doc, "grow")
    for node in (n for n in h["nodes"] if n["kind"] != "connector"):
        for conn in h["connectors"]:
            stub = 2 * max(s["min_bend_radius_mm"] for s in h["segments"] if conn["id"] in (s["a"], s["b"]))
            offset = model.vsub(node["pos"], conn["frame"]["origin"])
            along = model.vdot(offset, model.connector_exit_dir(conn))
            aside = model.vnorm(model.vsub(offset, model.vscale(model.connector_exit_dir(conn), along)))
            assert not (0 < along < stub and aside < 1), (node, conn["id"], stub)
    for wire in h["wires"]:
        points, _joints = model.wire_route(h, wire)
        turns = [model.angle_deg(model.vsub(b, a), model.vsub(c, b))
                 for a, b, c in zip(points, points[1:], points[2:]) if a != b != c]
        assert max(turns) < 175, (wire["id"], max(turns))  # no wire doubles back on itself


@enginetest
def test_engine_growth_that_leaves_no_room_fails_naming_the_bundle(engine_doc, tools):
    growing_branch_scene(tools, engine_doc)
    wall = [{"name": "wall", "aabb": [[20, -30, -30], [24, 22, 30]]}]  # clears A's 10 mm stub, not its grown one
    failed = invoke(tools, "harness_route", doc_id=engine_doc, harness_id="grow", clearance_mm=0.3, keepouts=wall)
    assert not failed["ok"] and failed["code"] == "exit_blocked", failed
    assert failed["start"]["connector"] == "A" and failed["detail"]["endpoint"] == "start", failed
    assert "w1" in failed["bundle_wires"] and len(failed["bundle_wires"]) >= 2, failed
    assert failed["min_bend_radius_mm"] > 5 and failed["route_passes"] == 2 and failed["hint"], failed
    assert model.load_harness(engine_doc, "grow")["segments"] == []


def test_segment_checks_borrow_context_only_from_a_through_continuation():
    h, n = through_branch_harness()
    first, second, branch = (seg_between(h, a, b) for a, b in (("A", n), (n, "B"), ("C", n)))
    # The two split halves continue through the breakout and lend each other their far points;
    # the branch has no continuation, so it is checked alone (no range).
    assert model.segment_check_path(h, first) == ([[0, 0, 0], [50, 0, 0], [100, 0, 0]], [0, 1])
    assert model.segment_check_path(h, second) == ([[0, 0, 0], [50, 0, 0], [100, 0, 0]], [1, 2])
    assert model.segment_check_path(h, branch) == ([[50, 40, 0], [50, 0, 0]], None)


def arc_trunk_harness():
    """One sampled arc A -> B, split by a breakout inside a leg (not at a vertex)."""
    h = model.new_harness("Arc", harness_id="arc")
    for ref, pos in (("A", [0, 0, 0]), ("B", [46, 30, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, [1, 0, 0]))
    model.add_wire(h, from_="A:1", to="B:1", insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    seg = model.add_segment(h, "A", "B", [[0, 0, 0], [10, 0, 0], [20, 2, 0], [30, 6, 0],
                                          [38, 12, 0], [44, 20, 0], [46, 30, 0]])
    model.split_segment_at(h, seg["id"], [34, 9, 0])
    return h


def test_halves_of_a_split_arc_are_checked_with_the_other_half_as_context():
    h = arc_trunk_harness()
    model.recompute_derived(h)
    p = [[0, 0, 0], [10, 0, 0], [20, 2, 0], [30, 6, 0], [38, 12, 0], [44, 20, 0], [46, 30, 0]]
    split = [34, 9, 0]
    first, second = h["segments"]
    # Context runs pi x 5 mm = 15.7 mm past the breakout: three points on one side, two on the other.
    assert model.segment_check_path(h, first) == ([*p[:4], split, *p[4:]], [0, 4])
    assert model.segment_check_path(h, second) == ([p[2], p[3], split, *p[4:]], [2, 5])


def test_join_point_is_measured_from_the_connector_departure_point(study_rpc, tools, route_calls):
    h = model.new_harness("Departure", harness_id="departure")
    for ref, pos, normal in (("A", [0, 0, 0], [1, 0, 0]), ("B", [100, 0, 0], [-1, 0, 0]),
                             ("C", [60, 30, 0], [1, 0, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, normal))
    for from_, to in (("A:1", "B:1"), ("C:1", "A:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    model.add_segment(h, "A", "B", [[0, 0, 0], [100, 0, 0]])
    model.save_harness("doc-a", h)
    routed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="departure", wire_ids=["w2"])
    assert routed["ok"], routed
    junction, = [n for n in model.load_harness("doc-a", "departure")["nodes"] if n["kind"] == "breakout"]
    # The exit stub runs to (70, 30, 0); the trunk is joined nearest that, not nearest the connector.
    assert junction["pos"] == pytest.approx([70, 0, 0])


def test_join_point_keeps_fillet_room_on_a_straight_leg(study_rpc, tools, route_calls):
    """A junction on an arc chord (or at a corner) leaves a turning wire no room for its fillet."""
    h = model.new_harness("Room", harness_id="room")
    for ref, pos, normal in (("A", [0, 0, 0], [1, 0, 0]), ("B", [42.5, 40, 0], [0, -1, 0]),
                             ("C", [41, -20, 0], [0, 1, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, normal))
    for from_, to in (("A:1", "B:1"), ("C:1", "A:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    # A straight leg, a bend sampled by short chords, then a second straight leg up to B.
    model.add_segment(h, "A", "B", [[0, 0, 0], [40, 0, 0], [41, 0.5, 0], [42, 1.5, 0],
                                    [42.5, 3, 0], [42.5, 40, 0]])
    model.save_harness("doc-a", h)
    assert invoke(tools, "harness_route", doc_id="doc-a", harness_id="room", wire_ids=["w2"])["ok"]
    junction, = [n for n in model.load_harness("doc-a", "room")["nodes"] if n["kind"] == "breakout"]
    # Departure (41, -10, 0) is nearest the bend's chords; the junction instead lands on the first
    # leg, a full bend radius (5 mm) short of its corner at (40, 0, 0).
    assert junction["pos"] == pytest.approx([35, 0, 0])


def trunk_with_two_way_branch(length):
    """A straight A-B trunk and a connector C whose wires go to both ends, so it must tap the trunk."""
    h = model.new_harness("Tap", harness_id="tap")
    for ref, pos, normal in (("A", [0, 0, 0], [1, 0, 0]), ("B", [length, 0, 0], [-1, 0, 0]),
                             ("C", [4, 30, 0], [0, -1, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=3, frame=model.make_frame(pos, normal))
    for from_, to in (("A:1", "B:1"), ("C:1", "A:2"), ("C:2", "B:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    model.add_segment(h, "A", "B", [[0, 0, 0], [length, 0, 0]])
    return h


def test_a_junction_never_lands_inside_a_connector_exit_stub(study_rpc, tools, route_calls):
    model.save_harness("doc-a", trunk_with_two_way_branch(100))
    assert invoke(tools, "harness_route", doc_id="doc-a", harness_id="tap", wire_ids=["w2", "w3"])["ok"]
    junction, = [n for n in model.load_harness("doc-a", "tap")["nodes"] if n["kind"] == "breakout"]
    # Departure (4, 2, 0) is nearest x = 4, but A's exit stub (2 x 5 mm) runs to x = 10.
    assert junction["pos"][0] >= 10 - 1e-6


def test_no_join_point_is_reported_when_no_leg_clears_the_exit_stubs(study_rpc, tools, route_calls):
    model.save_harness("doc-a", trunk_with_two_way_branch(15))  # both 10 mm stubs leave no free run
    failed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="tap", wire_ids=["w2", "w3"])
    assert not failed["ok"] and failed["code"] == "no_join_point", failed
    assert failed["wire"] == "w2" and failed["wires"] == ["w2", "w3"], failed
    assert [n for n in model.load_harness("doc-a", "tap")["nodes"] if n["kind"] != "connector"] == []


@pytest.fixture
def capped_router(monkeypatch):
    """Mock engine: a route realises the bend radius it was planned for, up to ``max_radius``, and
    refuses (no_path) to plan one above ``route_limit``."""
    from services.mcp import harness_tools

    state = {"calls": [], "routes": [], "max_radius": float("inf"), "route_limit": float("inf")}

    def rpc(method, params):
        state["calls"].append((method, copy.deepcopy(params)))
        if method == "harness_route":
            if params["min_bend_radius_mm"] > state["route_limit"]:
                raise model.HarnessError("no_path", "no feasible rounded path found in the bounded search domain",
                                         detail={"ok": False, "code": "no_path"})
            start, end = params["start"]["pos"], params["end"]["pos"]
            path = [start, [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2, 0], end]
            state["routes"].append((path, min(params["min_bend_radius_mm"], state["max_radius"])))
            return {"path": path}
        if "junctions" in params:  # full-wire fillet checks are not under test here
            return {"bend_violations": [], "clearance_violations": []}
        realised = [radius for path, radius in state["routes"]
                    if all(model.closest_on_polyline(p, path)[0] < 1e-6 for p in params["path"])]
        short = bool(realised) and params["min_bend_radius_mm"] > realised[-1] + 1e-9
        return {"bend_violations": [{"index": 1, "radius_mm": realised[-1]}] if short else [],
                "clearance_violations": []}

    monkeypatch.setattr(harness_tools, "_rpc", rpc)
    return state


def growing_harness():
    """w1 trunks A -> B; w2 from C joins it, turning the half it shares into a 2-wire bundle."""
    h = model.new_harness("Growing", harness_id="growing")
    for ref, pos, normal in (("A", [0, 0, 0], [1, 0, 0]), ("B", [100, 0, 0], [-1, 0, 0]),
                             ("C", [50, 40, 0], [0, -1, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, normal))
    for from_, to in (("A:1", "B:1"), ("C:1", "A:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=8)
    return h


def test_a_bundle_that_outgrows_its_sizing_is_repathed_for_the_grown_requirement(study_rpc, tools, capped_router):
    model.save_harness("doc-a", growing_harness())
    result = invoke(tools, "harness_route", doc_id="doc-a", harness_id="growing")
    # The call passes its own final check, so every bundle's route realises the radius it needs.
    assert result["ok"], result
    assert result["route_passes"] == 2 and len(result["repathed_segments"]) == 3, result
    h = model.load_harness("doc-a", "growing")
    assert sorted(len(s["wires"]) for s in h["segments"]) == [1, 1, 2]
    assert all(w["length_mm"] is not None for w in h["wires"])


def test_growth_that_the_engine_cannot_realise_fails_with_the_bundle(study_rpc, tools, capped_router):
    capped_router["max_radius"] = 8
    model.save_harness("doc-a", growing_harness())
    failed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="growing")
    assert not failed["ok"] and failed["code"] == "route_check_failed", failed
    assert failed["wires"] == ["w1", "w2"] and failed["route_passes"] == 2, failed
    assert failed["min_bend_radius_mm"] == 14 and failed["od_mm"] == 4, failed
    assert model.load_harness("doc-a", "growing")["segments"] == []


def test_a_bundle_with_no_room_at_its_final_size_fails_at_that_route_naming_its_wires(study_rpc, tools, capped_router):
    capped_router["route_limit"] = 8
    model.save_harness("doc-a", growing_harness())
    failed = invoke(tools, "harness_route", doc_id="doc-a", harness_id="growing")
    assert not failed["ok"] and failed["code"] == "no_path", failed
    assert failed["bundle_wires"] == ["w1", "w2"] and failed["min_bend_radius_mm"] == 14, failed
    assert failed["wire"] == "w1" and failed["route_passes"] == 2 and failed["hint"], failed
    assert model.load_harness("doc-a", "growing")["segments"] == []


def seg_between(h, a, b):
    return next(s for s in h["segments"] if {s["a"], s["b"]} == {a, b})


def assert_through_valid(h):
    """Every recorded ``through`` names exactly two segments that both touch its node."""
    for node in h["nodes"]:
        if "through" not in node:
            continue
        touching = {s["id"] for s in h["segments"] if node["id"] in (s["a"], s["b"])}
        assert len(set(node["through"])) == 2 and set(node["through"]) <= touching, (node, touching)


def through_branch_harness():
    """A trunk A-B split by a breakout N (recorded as a split), with a branch C-N."""
    h = model.new_harness("Through", harness_id="through")
    for ref, pos in (("A", [0, 0, 0]), ("B", [100, 0, 0]), ("C", [50, 40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, [1, 0, 0]))
    trunk = model.add_segment(h, "A", "B", [[0, 0, 0], [100, 0, 0]])
    n = model.split_segment_at(h, trunk["id"], [50, 0, 0])
    model.add_segment(h, "C", n, [[50, 40, 0], [50, 0, 0]])
    for from_, to in (("A:1", "B:1"), ("C:1", "A:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    model.recompute_derived(h)
    return h, n


def test_a_wire_straight_through_a_split_breakout_is_not_at_a_junction_but_the_branch_wire_is(
        study_rpc, tools, route_calls):
    h, _n = through_branch_harness()
    w1, w2 = h["wires"]
    assert model.wire_route(h, w1)[1] == []
    assert model.wire_route(h, w2) == ([[50, 40, 0], [50, 0, 0], [0, 0, 0]], [1])
    model.save_harness("doc-a", h)
    assert invoke(tools, "harness_check", doc_id="doc-a", harness_id="through")["ok"]
    joined = [(p["path"], p["junctions"]) for method, p in route_calls["calls"]
              if method == "harness_path_check" and "junctions" in p]
    assert joined == [([[50, 40, 0], [50, 0, 0], [0, 0, 0]], [1])]


def test_through_follows_repeated_splits_even_when_a_split_segment_id_is_reused():
    h = model.new_harness("Chain", harness_id="chain")
    for ref, pos in (("A", [0, 0, 0]), ("B", [100, 0, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, [1, 0, 0]))
    trunk = model.add_segment(h, "A", "B", [[0, 0, 0], [100, 0, 0]])
    n2 = model.split_segment_at(h, trunk["id"], [75, 0, 0])
    n1 = model.split_segment_at(h, seg_between(h, "A", n2)["id"], [25, 0, 0])
    middle = seg_between(h, n1, n2)
    n3 = model.split_segment_at(h, middle["id"], [50, 0, 0])
    # The middle segment held the highest id, so its first half reuses it: n2 must not keep pointing at it.
    assert seg_between(h, n1, n3)["id"] == middle["id"]
    assert_through_valid(h)
    for node, (left, right) in {n1: ("A", n3), n3: (n1, n2), n2: (n3, "B")}.items():
        assert set(model.get_node(h, node)["through"]) == {
            seg_between(h, left, node)["id"], seg_between(h, node, right)["id"]}


def test_through_follows_a_prune_merge():
    h, n = through_branch_harness()
    m = model.split_segment_at(h, seg_between(h, "A", n)["id"], [25, 0, 0])  # no branch of its own
    assert_through_valid(h)
    report = model.prune_graph(h)  # m keeps exactly two segments with identical fields: merged away
    assert [x["node"] for x in report["merged_segments"]] == [m] and model.get_node(h, m) is None
    assert_through_valid(h)
    assert set(model.get_node(h, n)["through"]) == {
        seg_between(h, "A", n)["id"], seg_between(h, n, "B")["id"]}
    w1, w2 = h["wires"]
    assert model.wire_route(h, w1)[1] == [] and model.wire_route(h, w2)[1] == [1]


def test_removing_a_through_segment_drops_the_stale_provenance():
    h = model.new_harness("Star", harness_id="star")
    for ref, pos in (("A", [0, 0, 0]), ("B", [100, 0, 0]), ("C", [50, 40, 0]), ("D", [50, -40, 0])):
        model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, [1, 0, 0]))
    trunk = model.add_segment(h, "A", "B", [[0, 0, 0], [100, 0, 0]])
    n = model.split_segment_at(h, trunk["id"], [50, 0, 0])
    model.add_segment(h, "C", n, [[50, 40, 0], [50, 0, 0]])
    model.add_segment(h, "D", n, [[50, -40, 0], [50, 0, 0]])
    for from_, to in (("A:1", "B:1"), ("C:1", "D:1"), ("C:2", "B:2")):
        model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
    model.recompute_derived(h)
    assert_through_valid(h)
    # Only w1 uses the A-N half; with it gone N keeps three segments, so it survives without provenance.
    report = model.remove_segments(h, [seg_between(h, "A", n)["id"]])
    assert report["unrouted_wires"] == ["w1"]
    assert "through" not in model.get_node(h, n)
    assert_through_valid(h)
    assert model.wire_route(h, h["wires"][2])[1] == [1]


def test_arc_context_comes_only_from_the_recorded_split_partner():
    def build(split: bool):
        h = model.new_harness("Shallow", harness_id="shallow")
        for ref, pos in (("A", [0, 0, 0]), ("B", [100, 0, 0]), ("C", [90, 14.5, 0])):
            model.add_connector(h, ref=ref, family="generic", pins=2, frame=model.make_frame(pos, [1, 0, 0]))
        if split:
            n = model.split_segment_at(h, model.add_segment(h, "A", "B", [[0, 0, 0], [100, 0, 0]])["id"],
                                       [50, 0, 0])
        else:  # no provenance: the breakout was not made by a split
            n = "n1"
            h["nodes"].append({"id": n, "kind": "breakout", "pos": [50, 0, 0]})
            model.add_segment(h, "A", n, [[0, 0, 0], [50, 0, 0]])
            model.add_segment(h, n, "B", [[50, 0, 0], [100, 0, 0]])
        # Leaves N about 20 degrees off the trunk line: nearly tangent, still a branch.
        branch = model.add_segment(h, "C", n, [[90, 14.5, 0], [50, 0, 0]])
        for from_, to in (("A:1", "B:1"), ("C:1", "A:2")):
            model.add_wire(h, from_=from_, to=to, insulation_od_mm=2, gauge_awg=20, min_bend_radius_mm=5)
        model.recompute_derived(h)
        return h, branch

    h, branch = build(split=True)
    assert model.segment_check_path(h, branch)[1] is None  # a branch is not a continuation
    trunk_half = next(s for s in h["segments"] if s["a"] == "A")
    assert model.segment_check_path(h, trunk_half)[1] == [0, 1]  # the split partner lends context
    h, branch = build(split=False)
    assert model.segment_check_path(h, branch)[1] is None  # no guessing from leg angles

"""Hydraulic fluid-power design MCP tools (services/mcp/hydraulic_tools.py + piping_pump_drive).

Groups:
  1. Registration + pure helpers — the new tools register beside the existing hydraulic
     tools on a stub FastMCP; the payload compaction keeps ≤400 aligned samples per series;
     the rating check flags a component / hose whose peak node pressure passes its rating;
     sizing results merge per kind. No engine.
  2. Persistence (engine-less) — the ``hydraulic_circuit`` study merges simulate / sizing /
     bom of one circuit, drops simulate / bom (keeps sizing) when the circuit changes, and
     ``hydraulic_load`` restores it into the session; ``piping_pump_drive`` reads the solved
     pump duty point and saves ``results.pump_drive.<run>`` in the ``piping_network`` study.
     A fake study store + fake native replies stand in for ``sim_study_*`` and the RPCs.
  3. Live engine — pump → relief → 4/3 tandem valve → double-acting cylinder extending
     against a constant load: steady velocity = Q·η_v / A_bore and stroke time =
     stroke / velocity; hydraulic-motor sizing and pump-drive envelopes against hand
     calculations; the whole flow persisted into a real document and restored with
     ``hydraulic_load``. Skipped cleanly when libchudware is not staged or the build lacks
     the method (``unknown method``), like test_hydraulic_handoff / test_piping_design_tools.

Physics anchors (formula cited at each assertion):
  * Pump delivery Q = D·n·η_v; cylinder speed v = Q / (π/4·d²).
  * Motor displacement D = 2π·T / (Δp·η_m); flow Q = D·n / η_v; P_out = T·ω; P_in = Δp·Q.
  * Hydraulic power Q·Δp (or ρ·g·Q·H); shaft = hydraulic / η_pump; motor ≥ shaft × SF,
    rounded up the IEC 60072 kW ladder (… 22, 30, 37 …) / NEMA MG-1 hp ladder (… 15, 20, 25 …).

Run: CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest \
         services/mcp/test_hydraulic_design_tools.py -v
"""
from __future__ import annotations

import copy
import json
import math
import os
import uuid

import pytest

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp import hydraulic_tools as ht  # noqa: E402
from services.mcp import piping_tools as pt  # noqa: E402

_HAVE_CORE = cb.available()

_NEW_TOOLS = ("hydraulic_templates", "hydraulic_simulate", "hydraulic_size_motor", "pump_drive_size",
              "hydraulic_load", "hydraulic_layout_3d")


# ── fixtures ──────────────────────────────────────────────────────────────────

class _StubMCP:
    """Collects tool functions the way FastMCP registration would."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools() -> dict:
    m = _StubMCP()
    ht.register_hydraulic_tools(m)
    return m.tools


def _piping_tools() -> dict:
    m = _StubMCP()
    pt.register_piping_tools(m)
    return m.tools


@pytest.fixture(autouse=True)
def _isolated_session(tmp_path, monkeypatch):
    """Fresh hydraulic + piping session state; series files land in tmp_path."""
    saved_hyd = dict(ht._HYD)
    saved_networks = copy.deepcopy(pt._NETWORKS)
    saved_piping = dict(pt._PIPING)
    ht._HYD.clear()
    ht._HYD["doc"] = None
    pt._NETWORKS.clear()
    pt._PIPING.update({"doc": None, "result": None, "boundary": None, "fluid": None,
                       "pumps": None, "network": None})
    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    yield
    ht._HYD.clear()
    ht._HYD.update(saved_hyd)
    pt._NETWORKS.clear()
    pt._NETWORKS.update(saved_networks)
    pt._PIPING.clear()
    pt._PIPING.update(saved_piping)


def _j(raw: str) -> dict:
    return json.loads(raw)


def _live(res: dict) -> dict:
    """Skip when the staged build has no such native method; otherwise pass through."""
    text = f"{res.get('reason', '')} {res.get('hint', '')}".lower()
    if not res.get("ok") and ("unknown method" in text or "cannot dispatch" in text):
        pytest.skip(f"build lacks the native method: {res.get('hint')}")
    return res


def _component(cid, type, **params) -> dict:
    return {"id": cid, "type": type, "name": type, "position": [0.0, 0.0], "angle": 0.0,
            "ports": [], "params": params,
            "sourcing": {"mpn": None, "manufacturer": None, "description": None,
                         "quantity": 1, "unit": "ea", "category": None}}


def _circuit(name: str = "Press") -> dict:
    """Pump → relief → 4/3 tandem valve → double-acting cylinder → tank (by hand)."""
    return {
        "schema": "aicad.hydraulic/1", "id": "hyd-test", "name": name,
        "units": {"pressure": "bar", "flow": "L/min", "force": "kN", "position": "mm"},
        "components": [
            _component("c1", "pump", displacement_cc_rev=10.0, speed_rpm=1500.0,
                       max_pressure_bar=250.0),
            _component("c2", "valve_relief", set_pressure_bar=160.0),
            _component("c3", "valve_directional", center_condition="tandem",
                       rated_flow_lpm=30.0, rated_dp_bar=5.0),
            _component("c4", "cylinder_linear", bore_mm=50.0, rod_mm=28.0, stroke_mm=300.0,
                       max_pressure_bar=100.0),
            _component("c5", "reservoir"),
        ],
        "hoses": [
            {"id": "h1", "net_name": "P", "max_pressure_bar": 120.0,
             "connections": [{"component_id": "c1", "port": "P"},
                             {"component_id": "c2", "port": "P"},
                             {"component_id": "c3", "port": "P"}]},
            {"id": "h2", "net_name": "A", "connections": [{"component_id": "c3", "port": "A"},
                                                          {"component_id": "c4", "port": "A"}]},
            {"id": "h3", "net_name": "B", "connections": [{"component_id": "c3", "port": "B"},
                                                          {"component_id": "c4", "port": "B"}]},
            {"id": "h4", "net_name": "T", "connections": [{"component_id": "c2", "port": "T"},
                                                          {"component_id": "c3", "port": "T"},
                                                          {"component_id": "c5", "port": "T"}]},
        ],
        "annotations": [],
    }


def _fake_sim(n: int = 2001, peak_bar: float = 140.0) -> dict:
    """A synthetic native hydraulic_simulate reply with n samples per series."""
    t = [i * 0.001 for i in range(n)]
    p = [peak_bar * math.sin(math.pi * i / (n - 1)) for i in range(n)]
    return {
        "ok": True, "time_s": t,
        "nodes": {
            "n1": {"pressure_bar": p, "members": ["c1.P", "c2.P", "c3.P"]},
            "n2": {"pressure_bar": [0.5 * v for v in p], "members": ["c3.A", "c4.A"]},
        },
        "components": {
            "c4": {"kind": "cylinder_linear",
                   "series": {"position_mm": [0.15 * i for i in range(n)],
                              "velocity_mms": [150.0] * n}},
        },
        "summary": {"peak_pressure_bar": peak_bar, "pump_energy_kj": 4.0,
                    "useful_work_kj": 3.0, "heat_kj": 1.0, "efficiency": 0.75,
                    "relief_open_time_s": 0.0,
                    "cylinders": {"c4": {"stroke_time_s": 2.0, "max_velocity_mms": 150.0,
                                         "end_reached": True}}},
        "solver": {"method": "rosenbrock", "steps": 900, "rejected_steps": 3},
        "warnings": [], "hint": "x", "next_actions": ["y"],
    }


# ── group 1: registration + pure helpers ──────────────────────────────────────

def test_new_tools_register_with_the_existing_hydraulic_tools():
    tools = _tools()
    for name in _NEW_TOOLS + ("hydraulic_new", "hydraulic_add_component",
                              "hydraulic_add_hose", "hydraulic_size_cylinder",
                              "hydraulic_size_pump", "hydraulic_bom",
                              "hydraulic_piping_handoff", "hydraulic_fea_handoff"):
        assert name in tools, f"tool {name!r} not registered"
        assert (tools[name].__doc__ or "").strip(), f"{name} has no description"
    for name in _NEW_TOOLS:
        assert len(tools[name].__doc__.strip().splitlines()[0]) <= 140
    assert "hydraulic_fmu_simulate" not in tools, "the retired FMU stub must stay gone"
    assert "piping_pump_drive" in _piping_tools()


def test_compaction_keeps_aligned_endpoints_and_at_most_400_points():
    raw = _fake_sim(n=2001)
    out = ht.compact_simulation(raw, max_points=10_000)  # the cap still binds at 400
    n = len(out["time_s"])
    assert n <= 400 and out["downsampled"] == {"points": n, "of": 2001,
                                               "full_resolution": False}
    assert out["time_s"][0] == raw["time_s"][0] and out["time_s"][-1] == raw["time_s"][-1]
    pos = out["components"]["c4"]["series"]["position_mm"]
    assert len(pos) == n and len(out["nodes"]["n1"]["pressure_bar"]) == n
    # one shared index set: position stays 150·t at every kept sample
    for t, x in zip(out["time_s"], pos):
        assert x == pytest.approx(150.0 * t, abs=1e-9)
    assert out["nodes"]["n1"]["members"] == raw["nodes"]["n1"]["members"]
    assert out["summary"] == raw["summary"]
    small = ht.compact_simulation(_fake_sim(n=50))
    assert small["downsampled"]["full_resolution"] is True and len(small["time_s"]) == 50


def test_rating_check_flags_components_and_hoses_over_their_rating():
    res = ht.rating_check(_circuit(), _fake_sim(peak_bar=140.0))
    by_item = {r["item"].split(" ")[0]: r for r in res["checked"]}
    # pump on n1 (peak 140) rated 250 → pass; hose h1 on n1 rated 120 → exceeded;
    # cylinder on n2 (peak 70) rated 100 → pass
    assert by_item["c1"]["pass"] is True and by_item["c1"]["peak_bar"] == pytest.approx(140.0)
    assert by_item["h1"]["pass"] is False and by_item["h1"]["utilization"] == pytest.approx(
        140.0 / 120.0, rel=1e-3)
    assert by_item["c4"]["peak_bar"] == pytest.approx(70.0)
    assert [r["item"].split(" ")[0] for r in res["exceeded"]] == ["h1"]
    unrated = ht.rating_check({"components": [], "hoses": []}, _fake_sim())
    assert unrated["checked"] == [] and unrated["note"]


def test_sizing_results_merge_per_kind_and_key():
    old = {"sizing": {"pumps": {"c1": {"a": 1}}, "motors": {"c6": {"b": 2}}}, "bom": {"x": 1}}
    new = {"sizing": {"pumps": {"c7": {"a": 3}}}}
    got = ht._merge_results(old, new)
    assert got["sizing"]["pumps"] == {"c1": {"a": 1}, "c7": {"a": 3}}
    assert got["sizing"]["motors"] == {"c6": {"b": 2}} and got["bom"] == {"x": 1}


# ── group 2: persistence (engine-less study store) ────────────────────────────

class _Store:
    """In-memory sim_study_* on one document + canned native replies for other methods."""

    def __init__(self):
        self.studies: list = []
        self.calls: list = []
        self.replies: dict = {}
        self._next = 0

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(self.studies)}
        if method == "sim_study_add":
            self._next += 1
            rec = copy.deepcopy(params["study"])
            rec.setdefault("id", f"st{self._next}")
            self.studies.append(rec)
            return {"study_id": rec["id"]}
        if method == "sim_study_edit":
            for i, s in enumerate(self.studies):
                if s["id"] == params["study_id"]:
                    rec = copy.deepcopy(params["study"])
                    rec["id"] = params["study_id"]
                    self.studies[i] = rec
                    return {"ok": True}
            raise RuntimeError("invalid_op: no such study")
        if method in self.replies:
            reply = self.replies[method]
            return copy.deepcopy(reply(params) if callable(reply) else reply)
        raise RuntimeError(f"unknown method {method}")

    def study(self, stype: str, name: str) -> dict:
        (st,) = [s for s in self.studies if s["type"] == stype and s["name"] == name]
        return st


@pytest.fixture()
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(cb, "call", s.call)
    monkeypatch.setattr(ht, "_doc", lambda doc_id: "doc-1")
    monkeypatch.setattr(pt, "_doc", lambda doc_id: "doc-1")
    return s


def test_hydraulic_new_template_persists_native_circuit_and_simulation_setup(store):
    tools = _tools()
    circuit = _circuit("Cylinder study")
    circuit["id"] = "native-template"
    setup = {
        "duration_s": 3.0, "output_dt_s": 0.01, "max_step_s": 0.01,
        "valve_schedule": [{"t_s": 0.0, "component_id": "c3", "position": "a"},
                           {"t_s": 1.5, "component_id": "c3", "position": "b"}],
        "loads": {"c4": {"force_n": 10000.0, "mass_kg": 50.0}},
        "fluid": {"density_kgm3": 870.0, "viscosity_cst": 32.0},
        "initial": {"c4": {"position_mm": 0.0}},
    }
    # The wrapper must persist the native reply, not recreate any template in Python.
    store.replies["hydraulic_new"] = {
        "ok": True, "doc": circuit, "template": "cylinder_circuit", "simulation": setup,
        "warnings": [], "hint": "native template", "next_actions": [],
    }
    ht._persist_circuit("doc-1", "Cylinder study", _circuit("Cylinder study"),
                        params={"duration_s": 99.0, "loads": {"old": {"force_n": 1.0}}},
                        results={"simulate": {"summary": {}}, "sizing": {"cylinders": {"old": {}}}})
    res = _j(tools["hydraulic_new"](name="Cylinder study", template="cylinder_circuit",
                                    doc_id="doc-1"))
    assert res["ok"] is True and res["saved_to_study"] is True, res
    assert res["template"] == "cylinder_circuit" and res["simulation"] == setup
    sent = next(p for m, p in store.calls if m == "hydraulic_new")
    assert sent == {"name": "Cylinder study", "template": "cylinder_circuit"}
    assert ht._HYD["doc"] == circuit
    st = store.study("hydraulic_circuit", "Cylinder study")
    assert st["params"]["circuit"]["components"] == circuit["components"]
    assert st["params"]["circuit"]["hoses"] == circuit["hoses"]
    assert {k: st["params"][k] for k in setup} == setup
    assert "simulate" not in st["results"] and st["results"]["sizing"]
    assert "simulation" not in st["params"]  # Same flat setup keys the GUI / simulator use.
    ht._HYD["doc"] = None
    loaded = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert loaded["circuit"] == circuit and loaded["simulation_setup"] == setup


def test_hydraulic_new_defaults_to_blank_template(store):
    circuit = _circuit("Hydraulic Circuit")
    circuit.update({"components": [], "hoses": []})
    store.replies["hydraulic_new"] = {
        "ok": True, "doc": circuit, "template": "blank",
        "simulation": {"duration_s": 5.0, "output_dt_s": 0.01,
                       "valve_schedule": [], "loads": {}, "initial": {}, "fluid": {}},
    }
    res = _j(_tools()["hydraulic_new"]())
    assert res["ok"] is True and res["template"] == "blank"
    assert res["doc"]["components"] == [] and res["doc"]["hoses"] == []
    assert store.calls == [("hydraulic_new", {"name": "Hydraulic Circuit", "template": "blank"})]


def test_hydraulic_new_unknown_template_surfaces_refusal_without_persisting(store):
    prior = _circuit()
    ht._HYD["doc"] = prior
    refusal = {"ok": False, "reason": "unknown_template",
               "hint": "Valid templates: blank, power_unit, cylinder_circuit, motor_circuit."}
    store.replies["hydraulic_new"] = refusal
    res = _j(_tools()["hydraulic_new"](template="not_a_template", doc_id="doc-1"))
    assert res == refusal
    assert ht._HYD["doc"] == prior and store.studies == []
    assert store.calls == [("hydraulic_new",
                            {"name": "Hydraulic Circuit", "template": "not_a_template"})]


def test_hydraulic_templates_returns_native_catalog(store):
    catalog = {"ok": True, "templates": [
        {"id": "cylinder_circuit", "name": "Native cylinder", "description": "Native description",
         "components": 6, "hoses": 6},
    ]}
    store.replies["hydraulic_templates"] = catalog
    assert _j(_tools()["hydraulic_templates"]()) == catalog
    assert store.calls == [("hydraulic_templates", {})] and store.studies == []


def test_simulate_persists_compact_run_and_writes_full_series(store, tmp_path):
    tools = _tools()
    circuit = _circuit()
    ht._HYD["doc"] = circuit
    store.replies["hydraulic_simulate"] = _fake_sim(n=2001)
    sched = [{"t_s": 0.1, "component_id": "c3", "position": "a"}]
    loads = {"c4": {"force_n": 10000.0, "mass_kg": 200.0}}
    res = _j(tools["hydraulic_simulate"](duration_s=2.0, valve_schedule=sched, loads=loads,
                                         doc_id="doc-1"))
    assert res["ok"] is True and res["saved_to_study"] is True, res
    sent = next(p for m, p in store.calls if m == "hydraulic_simulate")
    assert sent["doc"] == circuit and sent["valve_schedule"] == sched
    assert sent["loads"] == loads and sent["duration_s"] == 2.0
    assert len(res["time_s"]) <= 400
    assert res["ratings"]["exceeded"][0]["item"].startswith("h1")
    assert any("h1" in w for w in res["warnings"])
    # the full-resolution run is on disk, inside the project's .chudware dir
    path = res["series_path"]
    assert path.startswith(str(tmp_path / ".chudware" / "hydraulic"))
    with open(path, encoding="utf-8") as fh:
        full = json.load(fh)
    assert len(full["time_s"]) == 2001
    st = store.study("hydraulic_circuit", "Press")
    assert st["engine"] == "cpp-hydraulic" and st["params"]["circuit"] == circuit
    assert st["params"]["valve_schedule"] == sched and st["params"]["loads"] == loads
    sim = st["results"]["simulate"]
    assert sim["summary"]["peak_pressure_bar"] == 140.0 and sim["series_path"] == path
    assert len(sim["time_s"]) <= 400 and "hint" not in sim and "next_actions" not in sim


def test_circuit_study_merges_sizing_and_drops_stale_analyses(store):
    tools = _tools()
    circuit = _circuit()
    ht._HYD["doc"] = circuit
    store.replies.update({
        "hydraulic_simulate": _fake_sim(n=300),
        "hydraulic_size_motor": {"ok": True, "displacement_cc_rev": 69.81,
                                 "standard_displacement_cc_rev": 80.0, "flow_lpm": 73.5,
                                 "warnings": [], "hint": "h", "next_actions": []},
        "pump_drive_size": {"ok": True, "motor_rating_kw": 30.0, "poles": 4,
                            "warnings": [], "hint": "h", "next_actions": []},
        "hydraulic_bom": {"ok": True, "lines": [], "summary": {"missing_mpn": []}},
    })
    sim = _j(tools["hydraulic_simulate"](doc_id="doc-1"))
    mot = _j(tools["hydraulic_size_motor"](torque_nm=100.0, speed_rpm=1000.0,
                                           pressure_bar=100.0, component_id="c6",
                                           doc_id="doc-1"))
    drv = _j(tools["pump_drive_size"](kind="hydraulic", flow_lpm=60.0, pressure_bar=200.0,
                                      component_id="c1", doc_id="doc-1"))
    bom = _j(tools["hydraulic_bom"](doc_id="doc-1"))
    assert sim["study_id"] == mot["study_id"] == drv["study_id"] == bom["study_id"]
    assert len([s for s in store.studies if s["type"] == "hydraulic_circuit"]) == 1
    st = store.study("hydraulic_circuit", "Press")
    assert set(st["results"]) == {"simulate", "sizing", "bom"}
    assert st["results"]["sizing"]["motors"]["c6"]["standard_displacement_cc_rev"] == 80.0
    assert st["results"]["sizing"]["drives"]["c1"]["motor_rating_kw"] == 30.0

    # a centrifugal drive belongs to a piping network, not this circuit
    cen = _j(tools["pump_drive_size"](kind="centrifugal", flow_m3h=100.0, head_m=30.0,
                                      doc_id="doc-1"))
    assert cen["ok"] is True and cen["saved_to_study"] is False
    assert "piping_pump_drive" in cen["study_not_saved_reason"]

    # editing the circuit invalidates simulate/bom (and a GUI-written piping_handoff)
    st["results"]["piping_handoff"] = {"verify": {"pass": True}}
    store.studies[0] = st
    changed = copy.deepcopy(circuit)
    changed["components"][3]["params"]["bore_mm"] = 63.0
    ht._persist_circuit("doc-1", "Press", changed)
    st = store.study("hydraulic_circuit", "Press")
    assert set(st["results"]) == {"sizing"} and st["params"]["circuit"] == changed
    # the setup of the dropped run stays as the circuit's last simulation setup
    assert st["params"]["duration_s"] == 5.0


def test_hydraulic_load_restores_a_saved_circuit(store):
    tools = _tools()
    circuit = _circuit()
    sched = [{"t_s": 0.0, "component_id": "c3", "position": "b"}]
    ht._persist_circuit("doc-1", "Press", circuit,
                        params={"valve_schedule": sched, "duration_s": 3.0},
                        results={"simulate": {"summary": {"peak_pressure_bar": 90.0}},
                                 "sizing": {"pumps": {"c1": {"displacement_cc_rev": 10.0}}}})
    assert ht._HYD["doc"] is None

    res = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert res["ok"] is True and res["name"] == "Press" and res["circuit"] == circuit
    assert res["components"] == 5 and res["hoses"] == 4
    assert res["restored_results"] == ["simulate", "sizing"]
    assert res["simulation_setup"] == {"valve_schedule": sched, "duration_s": 3.0}
    assert ht._HYD["doc"] == circuit, "the restored circuit is the active session circuit"

    # by name: unknown → refused with the saved names; several → ambiguous
    res = _j(tools["hydraulic_load"](doc_id="doc-1", name="nope"))
    assert res["ok"] is False and res["reason"] == "circuit_not_found"
    assert res["circuits"] == ["Press"]
    ht._persist_circuit("doc-1", "Lift", _circuit("Lift"))
    res = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert res["ok"] is False and res["reason"] == "circuit_ambiguous"
    res = _j(tools["hydraulic_load"](doc_id="doc-1", name="Lift"))
    assert res["ok"] is True and ht._HYD["doc"]["name"] == "Lift"


def test_hydraulic_layout_3d_merges_persisted_positions_without_dropping_results(store):
    tools = _tools()
    circuit = _circuit()
    circuit["components"][1]["position"] = [25.0, -50.0]
    original_result = {"summary": {"peak_pressure_bar": 140.0}}
    ht._persist_circuit("doc-1", "Press", circuit,
                        params={"layout3d": {"c1": [10.0, 20.0, 30.0]}, "duration_s": 3.0},
                        results={"simulate": original_result})
    restored = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert restored["layout3d"]["c2"] == [50.0, -100.0, 0.0]
    res = _j(tools["hydraulic_layout_3d"]({"c2": [100.0, 200.0, 300.0]}, doc_id="doc-1"))
    assert res["ok"] and res["saved_to_study"]
    assert res["layout3d"]["c1"] == [10.0, 20.0, 30.0]
    assert res["layout3d"]["c2"] == [100.0, 200.0, 300.0]
    assert len(res["layout3d"]) == len(circuit["components"])
    saved = store.study("hydraulic_circuit", "Press")
    assert saved["params"]["layout3d"] == res["layout3d"]
    assert saved["params"]["circuit"] == circuit
    assert saved["params"]["duration_s"] == 3.0
    assert saved["results"]["simulate"] == original_result
    loaded = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert loaded["layout3d"] == res["layout3d"]
    again = _j(tools["hydraulic_layout_3d"]({"c3": [-5, 6, 7]}, circuit="Press", doc_id="doc-1"))
    assert again["layout3d"]["c2"] == [100.0, 200.0, 300.0]
    assert again["layout3d"]["c3"] == [-5.0, 6.0, 7.0]
    bad = _j(tools["hydraulic_layout_3d"]({"c1": [0, 0, 0], "absent": [1, 2, 3]}, doc_id="doc-1"))
    assert not bad["ok"] and bad["reason"] == "unknown_component"
    assert store.study("hydraulic_circuit", "Press")["params"]["layout3d"] == again["layout3d"]
    bad = _j(tools["hydraulic_layout_3d"]({"c1": [float("nan"), 0, 0]}, doc_id="doc-1"))
    assert not bad["ok"] and bad["reason"] == "bad_layout"


def test_a_doc_with_no_circuit_refuses_and_sizing_alone_is_not_a_circuit(store):
    tools = _tools()
    store.replies["hydraulic_size_motor"] = {"ok": True, "displacement_cc_rev": 62.83}
    _j(tools["hydraulic_size_motor"](torque_nm=100.0, speed_rpm=1000.0, pressure_bar=100.0,
                                     doc_id="doc-1"))
    (st,) = store.studies
    assert st["params"]["circuit"] is None and st["name"] == "Hydraulic Circuit"
    assert list(st["results"]["sizing"]["motors"]) == ["100Nm@1000rpm@100bar"]
    res = _j(tools["hydraulic_load"](doc_id="doc-1"))
    assert res["ok"] is False and res["reason"] == "no_hydraulic_circuit"
    assert res["sizing_only_studies"] == ["Hydraulic Circuit"]
    # a later hydraulic_new of the default name adopts the sizing study
    store.replies["hydraulic_new"] = {"ok": True, "doc": _circuit("Hydraulic Circuit")}
    new = _j(tools["hydraulic_new"](doc_id="doc-1"))
    assert new["saved_to_study"] is True and len(store.studies) == 1
    assert store.studies[0]["results"]["sizing"]["motors"]


def test_saved_circuit_is_found_by_tools_without_a_session(store):
    """The save/reopen path: a tool given doc_id restores the saved circuit itself."""
    circuit = _circuit()
    ht._persist_circuit("doc-1", "Press", circuit)
    d, refusal = ht._circuit_input(None, "doc-1")
    assert refusal is None and d == circuit and ht._HYD["doc"] == circuit
    ht._HYD["doc"] = None
    d, refusal = ht._circuit_input(None, None)
    assert d is None and refusal["reason"] == "no_circuit" and refusal["hint"]


def _piping_doc() -> dict:
    return {"schema": "aicad.piping/1", "id": "net", "name": "plant",
            "units": {"system": "MMGS"}, "material": "A106-B",
            "runs": [{"id": "r1", "path": [[0.0, 0.0, 0.0], [5000.0, 0.0, 0.0]], "nps": 4,
                      "schedule": "40", "end_type": "BW"},
                     {"id": "r2", "path": [[5000.0, 0.0, 0.0], [9000.0, 0.0, 0.0]],
                      "nps": 4, "schedule": "40", "end_type": "BW"}],
            "fittings": []}


def test_piping_pump_drive_sizes_from_the_solved_duty_point_and_persists(store):
    tools = _piping_tools()
    doc = _piping_doc()
    solve = {"ok": True, "converged": True, "fluid": {"name": "water", "rho": 998.2},
             "links": [{"id": "r1", "flow_m3h": 100.0, "pump_head_m": 30.0},
                       {"id": "r2", "flow_m3h": 100.0, "pump_head_m": 0.0}]}
    pt._remember("plant", doc, result=solve, pumps=[{"run": "r1", "curve": "flow,head"}])
    store.replies["pump_drive_size"] = lambda p: {
        "ok": True, "hydraulic_power_kw": 8.16, "motor_rating_kw": 15.0,
        "echo": p, "warnings": [], "hint": "h", "next_actions": []}

    res = _j(tools["piping_pump_drive"](network="plant", doc_id="doc-1"))
    assert res["ok"] is True and res["run_id"] == "r1", res
    sent = next(p for m, p in store.calls if m == "pump_drive_size")
    assert sent["kind"] == "centrifugal" and sent["flow_m3h"] == 100.0
    assert sent["head_m"] == 30.0 and sent["fluid_density_kgm3"] == 998.2
    assert res["operating_point"]["source"] == "piping_solve"
    st = store.study("piping_network", "plant")
    drive = st["results"]["pump_drive"]["r1"]
    assert drive["motor_rating_kw"] == 15.0 and drive["operating_point"]["head_m"] == 30.0
    assert "hint" not in drive
    assert pt._NETWORKS["plant"]["pump_drive"]["r1"]["motor_rating_kw"] == 15.0

    # an explicit duty point (e.g. piping_operating_point) overrides the solve
    res = _j(tools["piping_pump_drive"](network="plant", flow_m3h=80.0, head_m=35.0,
                                        doc_id="doc-1"))
    assert res["operating_point"] == {"flow_m3h": 80.0, "head_m": 35.0,
                                      "fluid_density_kgm3": 998.2, "source": "explicit"}


def test_piping_pump_drive_refuses_without_a_unique_solved_pump(store):
    tools = _piping_tools()
    doc = _piping_doc()
    pt._remember("plant", doc)
    res = _j(tools["piping_pump_drive"](network="plant"))
    assert res["ok"] is False and res["reason"] == "no_solve" and res["hint"]
    two = {"ok": True, "fluid": {"rho": 998.2},
           "links": [{"id": "r1", "flow_m3h": 50.0, "pump_head_m": 20.0},
                     {"id": "r2", "flow_m3h": 50.0, "pump_head_m": 18.0}]}
    pt._remember("plant", doc, result=two)
    res = _j(tools["piping_pump_drive"](network="plant"))
    assert res["ok"] is False and res["reason"] == "pump_ambiguous"
    assert res["pump_runs"] == ["r1", "r2"]
    none = {"ok": True, "links": [{"id": "r1", "flow_m3h": 50.0, "pump_head_m": 0.0}]}
    pt._remember("plant", doc, result=none, pumps=[])
    res = _j(tools["piping_pump_drive"](network="plant"))
    assert res["ok"] is False and res["reason"] == "no_pump"


# ── group 3: live engine ──────────────────────────────────────────────────────

live = pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — tools are core RPCs")

_SCHEDULE = [{"t_s": 0.1, "component_id": "c3", "position": "a"}]
_LOADS = {"c4": {"force_n": 10000.0, "mass_kg": 200.0}}


def _build_press(tools, doc_id=None) -> dict:
    """The cylinder circuit built through the tools (the real core mints ids c1..c5)."""
    assert _j(tools["hydraulic_new"](name="Press", doc_id=doc_id))["ok"]
    parts = [
        ("pump", {"displacement_cc_rev": 10.0, "speed_rpm": 1500.0}),
        ("valve_relief", {"set_pressure_bar": 160.0}),
        ("valve_directional", {"center_condition": "tandem", "rated_flow_lpm": 30.0,
                               "rated_dp_bar": 5.0}),
        ("cylinder_linear", {"bore_mm": 50.0, "rod_mm": 28.0, "stroke_mm": 300.0}),
        ("reservoir", {}),
    ]
    for t, params in parts:
        r = _j(tools["hydraulic_add_component"](type=t, params=params, doc_id=doc_id))
        assert r["ok"], r
    for net, conns in (("P", [("c1", "P"), ("c2", "P"), ("c3", "P")]),
                       ("A", [("c3", "A"), ("c4", "A")]),
                       ("B", [("c3", "B"), ("c4", "B")]),
                       ("T", [("c2", "T"), ("c3", "T"), ("c5", "T")])):
        r = _j(tools["hydraulic_add_hose"](
            net_name=net, connections=[{"component_id": c, "port": p} for c, p in conns],
            doc_id=doc_id))
        assert r["ok"], r
    return ht._HYD["doc"]


@live
def test_cylinder_circuit_steady_velocity_matches_q_over_a():
    tools = _tools()
    _build_press(tools)
    res = _live(_j(tools["hydraulic_simulate"](duration_s=4.0, valve_schedule=_SCHEDULE,
                                               loads=_LOADS, output_dt_s=0.005)))
    assert res["ok"] is True, res
    # Q = D·n·η_v = 10 cc × 1500 rpm × 0.95 = 14.25 L/min; v = Q / (π/4·50²)
    q_mm3_s = 10.0e3 * 1500.0 / 60.0 * 0.95
    v_expect = q_mm3_s / (math.pi / 4.0 * 50.0 ** 2)          # ≈ 120.96 mm/s
    with open(res["series_path"], encoding="utf-8") as fh:
        full = json.load(fh)
    t = full["time_s"]
    vel = full["components"]["c4"]["series"]["velocity_mms"]
    # steady mid-stroke window: after the 0.1 s spool shift + acceleration, before 300 mm
    mid = [v for ti, v in zip(t, vel) if 1.0 <= ti <= 2.0]
    assert mid, "no samples in the steady window"
    v_mean = sum(mid) / len(mid)
    assert v_mean == pytest.approx(v_expect, rel=0.03), (v_mean, v_expect)
    cyl = res["summary"]["cylinders"]["c4"]
    assert cyl["end_reached"] is True
    # stroke time ≈ stroke / v; the slack covers the 0.1 s schedule delay (whether or not
    # the core counts from t=0) and the short acceleration of the 200 kg load
    assert cyl["stroke_time_s"] == pytest.approx(300.0 / v_expect, abs=0.25)
    # payload discipline: ≤400 returned samples, full resolution only in the file
    assert len(res["time_s"]) <= 400 and len(t) > 400
    assert len(res["components"]["c4"]["series"]["velocity_mms"]) == len(res["time_s"])
    # past the end stop the pump deadheads into the relief: peak ≥ cracking pressure
    assert res["summary"]["peak_pressure_bar"] >= 160.0


@live
def test_simulate_refuses_an_unconnected_port_by_name():
    tools = _tools()
    _build_press(tools)
    circuit = copy.deepcopy(ht._HYD["doc"])
    circuit["hoses"] = [h for h in circuit["hoses"] if h["net_name"] != "B"]
    res = _live(_j(tools["hydraulic_simulate"](doc=circuit, valve_schedule=_SCHEDULE)))
    assert res["ok"] is False and res["reason"] == "unconnected_port" and res["hint"]


@live
def test_motor_sizing_envelope_matches_the_hand_calc():
    tools = _tools()
    ideal = _live(_j(tools["hydraulic_size_motor"](torque_nm=100.0, speed_rpm=1000.0,
                                                   pressure_bar=100.0, mech_efficiency=1.0,
                                                   vol_efficiency=1.0)))
    assert ideal["ok"] is True, ideal
    # D = 2π·T/Δp = 2π·100 / 100e5 m³ = 62.83 cc/rev; Q = D·n = 62.83 L/min
    assert ideal["displacement_cc_rev"] == pytest.approx(62.83, rel=1e-3)
    assert ideal["flow_lpm"] == pytest.approx(62.83, rel=1e-3)
    # P_out = T·ω = 100 × 2π·1000/60 = 10.47 kW = P_in (lossless)
    assert ideal["output_power_kw"] == pytest.approx(10.472, rel=1e-3)
    assert ideal["input_hydraulic_power_kw"] == pytest.approx(10.472, rel=1e-3)
    assert ideal["standard_displacement_cc_rev"] >= ideal["displacement_cc_rev"]

    real = _live(_j(tools["hydraulic_size_motor"](torque_nm=100.0, speed_rpm=1000.0,
                                                  pressure_bar=100.0, type="piston")))
    # D = 2π·100 / (100e5 · 0.9) = 69.81 cc; Q = 69.81 × 1000 / 0.95 / 1000 = 73.49 L/min
    assert real["displacement_cc_rev"] == pytest.approx(69.81, rel=1e-3)
    assert real["flow_lpm"] == pytest.approx(73.49, rel=1e-3)
    assert real["standard_displacement_cc_rev"] >= real["displacement_cc_rev"]


@live
def test_pump_drive_envelopes_iec_and_nema():
    tools = _tools()
    iec = _live(_j(tools["pump_drive_size"](kind="hydraulic", flow_lpm=60.0,
                                            pressure_bar=200.0)))
    assert iec["ok"] is True, iec
    # P_h = Q·Δp = (60/60000 m³/s)(200e5 Pa) = 20 kW; shaft = 20/0.88 = 22.73 kW
    assert iec["hydraulic_power_kw"] == pytest.approx(20.0, rel=1e-3)
    assert iec["shaft_power_kw"] == pytest.approx(20.0 / 0.88, rel=1e-3)
    # × SF 1.15 = 26.14 kW → next IEC 60072 step 30 kW; 1500 rpm @ 50 Hz → 4 poles
    assert iec["required_motor_kw"] == pytest.approx(20.0 / 0.88 * 1.15, rel=1e-3)
    assert iec["motor_rating_kw"] == pytest.approx(30.0)
    assert iec["poles"] == 4 and iec["sync_speed_rpm"] == pytest.approx(1500.0)
    assert 0.0 < iec["motor_efficiency"] < 1.0
    assert iec["input_power_kw"] > iec["shaft_power_kw"] and iec["full_load_current_a"] > 0.0

    nema = _live(_j(tools["pump_drive_size"](kind="centrifugal", flow_m3h=100.0, head_m=30.0,
                                             standard="NEMA", voltage_v=460.0,
                                             frequency_hz=60.0, speed_rpm=1800.0)))
    assert nema["ok"] is True, nema
    # P_h = ρgQH = 998 × 9.81 × (100/3600) × 30 = 8.157 kW; shaft /0.7 = 11.65 kW
    assert nema["hydraulic_power_kw"] == pytest.approx(8.157, rel=2e-3)
    # × 1.15 = 13.40 kW = 17.97 hp → NEMA MG-1 step 20 hp
    assert nema["motor_rating_hp"] == pytest.approx(20.0)
    assert nema["poles"] == 4

    bad = _live(_j(tools["pump_drive_size"](kind="hydraulic", pressure_bar=200.0)))
    assert bad["ok"] is False and bad["reason"] and bad["hint"]


@live
def test_persist_then_hydraulic_load_round_trip_on_a_live_doc():
    tools = _tools()
    doc = cb.call("doc_new", {"kind": "part",
                              "name": f"hyd_persist_{uuid.uuid4().hex[:8]}"})["doc_id"]
    try:
        circuit = _build_press(tools, doc_id=doc)
        sim = _live(_j(tools["hydraulic_simulate"](duration_s=3.0, valve_schedule=_SCHEDULE,
                                                   loads=_LOADS, doc_id=doc)))
        assert sim["ok"] is True and sim["saved_to_study"] is True, sim
        mot = _live(_j(tools["hydraulic_size_motor"](torque_nm=50.0, speed_rpm=500.0,
                                                     pressure_bar=120.0, component_id="c9",
                                                     doc_id=doc)))
        assert mot["study_id"] == sim["study_id"]

        studies = [s for s in cb.call("sim_study_list", {"doc_id": doc})["studies"]
                   if s.get("type") == "hydraulic_circuit"]
        assert len(studies) == 1
        assert set(studies[0]["results"]) >= {"simulate", "sizing"}

        # forget the session (a fresh process after save/reopen) and restore from the doc
        ht._HYD["doc"] = None
        loaded = _j(tools["hydraulic_load"](doc_id=doc))
        assert loaded["ok"] is True and loaded["name"] == "Press"
        assert loaded["circuit"] == circuit
        assert {"simulate", "sizing"} <= set(loaded["restored_results"])
        assert loaded["simulation_setup"]["valve_schedule"] == _SCHEDULE
        assert ht._HYD["doc"] == circuit
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass


@live
def test_native_cylinder_template_persists_and_restores_setup():
    tools = _tools()
    doc = cb.call("doc_new", {"kind": "part",
                              "name": f"hyd_template_{uuid.uuid4().hex[:8]}"})["doc_id"]
    try:
        created = _live(_j(tools["hydraulic_new"](name="Template cylinder",
                                                  template="cylinder_circuit", doc_id=doc)))
        assert created["ok"] is True and created["saved_to_study"] is True, created
        assert created["template"] == "cylinder_circuit"
        circuit, setup = created["doc"], created["simulation"]
        assert len(circuit["components"]) == 6 and len(circuit["hoses"]) == 6
        assert len(setup["valve_schedule"]) == 3 and setup["loads"]
        studies = [s for s in cb.call("sim_study_list", {"doc_id": doc})["studies"]
                   if s.get("type") == "hydraulic_circuit"]
        assert len(studies) == 1
        assert studies[0]["params"]["circuit"] == circuit
        assert {k: studies[0]["params"][k] for k in setup} == setup
        ht._HYD["doc"] = None
        loaded = _j(tools["hydraulic_load"](doc_id=doc))
        assert loaded["ok"] is True and loaded["circuit"] == circuit
        assert loaded["simulation_setup"] == setup
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass

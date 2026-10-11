"""Piping design + industrial sizing + internal pipe-flow MCP tools (services/mcp/piping_tools.py).

Groups:
  1. Registration + pure helpers — every new tool registers on a stub FastMCP; session
     networks resolve by name; pipe-run inlet/outlet planes point into the bore; the
     ``sim_run_pipe_flow`` job payload validator refuses malformed setups. No engine.
  2. Persistence (engine-less) — the ``piping_network`` study merges analyses of one
     network, drops them when the network changes, and ``piping_load`` restores it into
     the session; the pipe-flow collector persists one ``pipe_flow`` study. A fake study
     store stands in for ``sim_study_*``.
  3. Live engine — route around an obstacle → B31.3 spec check → BOM on the result; the
     same flow persisted into a real document and restored with ``piping_load``; vessel and
     duct envelopes against hand calculations; ``run_pipe_flow`` on a straight round duct
     against Hagen–Poiseuille. Skipped cleanly when libchudware is not staged or the build
     lacks the method (``unknown method``), like test_hydraulic_handoff / test_cfd_lbm_live.

Physics anchors (formula cited at each assertion):
  * ASME B31.3 eq. (3a): t = P·D / (2·(S·E·W + P·Y)), Y = 0.4.
  * ASME VIII-1 UG-27(c)(1): t = P·R / (S·E − 0.6·P); UG-32(d) 2:1 ellipsoidal head
    t = P·D / (2·S·E − 0.2·P); 2:1 ellipsoidal head volume π·D³/24.
  * Huebscher rectangular-duct equivalent diameter 1.30·(a·b)^0.625 / (a+b)^0.25.
  * Hagen–Poiseuille: Δp = 32·μ·L·V / D² (fully developed laminar flow in a round pipe).

Run: CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest \
         services/mcp/test_piping_design_tools.py -v
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
from services.mcp import piping_tools as pt  # noqa: E402
from services.sim import jobs as simjobs  # noqa: E402

_HAVE_CORE = cb.available()

_NEW_TOOLS = ("piping_route", "piping_edit", "piping_fitting_k", "piping_fitting_k_cfd",
              "piping_spec_check", "piping_bom", "piping_load",
              "vessel_size", "duct_size", "run_pipe_flow")


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
    pt.register_piping_tools(m)
    return m.tools


@pytest.fixture(autouse=True)
def _isolated_session():
    """Every test starts with an empty network table and restores the module state after."""
    saved_networks = copy.deepcopy(pt._NETWORKS)
    saved_piping = dict(pt._PIPING)
    pt._NETWORKS.clear()
    pt._PIPING.update({"doc": None, "result": None, "boundary": None, "fluid": None,
                       "pumps": None, "network": None})
    yield
    pt._NETWORKS.clear()
    pt._NETWORKS.update(saved_networks)
    pt._PIPING.clear()
    pt._PIPING.update(saved_piping)


def _straight_doc(length_mm: float = 1000.0, nps: float = 2.0) -> dict:
    return {
        "schema": "aicad.piping/1", "id": "net-test", "name": "test", "units": {"system": "MMGS"},
        "material": "A106-B",
        "runs": [{"id": "r1", "name": "line", "path": [[0.0, 0.0, 0.0], [length_mm, 0.0, 0.0]],
                  "nps": nps, "schedule": "40", "material": None, "bend_radius": None,
                  "end_type": "BW"}],
        "fittings": [],
    }


def _j(raw: str) -> dict:
    return json.loads(raw)


def _live(res: dict) -> dict:
    """Skip when the staged build has no such native method; otherwise pass through."""
    text = f"{res.get('reason', '')} {res.get('hint', '')}".lower()
    if not res.get("ok") and ("unknown method" in text or "cannot dispatch" in text):
        pytest.skip(f"build lacks the native method: {res.get('hint')}")
    return res


# ── group 1: registration + pure helpers ──────────────────────────────────────

def test_new_tools_register_with_the_existing_piping_tools():
    tools = _tools()
    for name in _NEW_TOOLS + ("piping_solve", "piping_report", "piping_internal_volume",
                              "piping_cfd_handoff", "piping_verify_cfd"):
        assert name in tools, f"tool {name!r} not registered"
        assert (tools[name].__doc__ or "").strip(), f"{name} has no description"
    # first docstring line is the tool-panel description: one crisp line
    for name in _NEW_TOOLS:
        assert len(tools[name].__doc__.strip().splitlines()[0]) <= 140


def test_run_flow_planes_point_into_the_bore():
    run = {"id": "r1", "path": [[0, 0, 0], [1000, 0, 0], [1000, 500, 0]]}
    inlet, outlet = pt.run_flow_planes(run)
    assert inlet == {"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
    # outlet sits on the last point, normal reversed from the last segment (into the bore)
    assert outlet == {"point": [1000.0, 500.0, 0.0], "normal": [0.0, -1.0, 0.0]}
    # coincident leading points are skipped, a zero-length run is refused
    inlet, _ = pt.run_flow_planes({"id": "r2", "path": [[0, 0, 0], [0, 0, 0], [0, 0, 50]]})
    assert inlet["normal"] == [0.0, 0.0, 1.0]
    with pytest.raises(ValueError):
        pt.run_flow_planes({"id": "r3", "path": [[1, 1, 1], [1, 1, 1]]})


def test_session_networks_resolve_by_name():
    doc = _straight_doc()
    d, name, refusal = pt._resolve_piping(None, None)
    assert d is None and refusal["reason"] == "no_piping"
    d, name, refusal = pt._resolve_piping("plant", None)
    assert refusal["reason"] == "unknown_network" and refusal["hint"]

    pt._remember("plant", doc)
    d, name, refusal = pt._resolve_piping("plant", None)
    assert refusal is None and d == doc and name == "plant"
    d, name, refusal = pt._resolve_piping(None, None)  # the last network used
    assert refusal is None and d == doc and name == "plant"
    # a whole route/load result is accepted for its `piping` doc
    d, name, refusal = pt._resolve_piping({"ok": True, "piping": doc}, "other")
    assert refusal is None and d == doc and name == "other"

    # a changed doc under the same name drops the stale analyses
    pt._remember("plant", doc, spec_check={"all_pass": True})
    assert pt._NETWORKS["plant"]["spec_check"] == {"all_pass": True}
    changed = copy.deepcopy(doc)
    changed["runs"][0]["path"][1] = [2000.0, 0.0, 0.0]
    pt._remember("plant", changed)
    assert "spec_check" not in pt._NETWORKS["plant"]


def _stl_file(tmp_path) -> str:
    p = tmp_path / "duct.stl"
    p.write_text("solid d\nendsolid d\n", encoding="utf-8")
    return str(p)


def test_pipe_flow_job_payload_validation(tmp_path):
    stl = _stl_file(tmp_path)
    good = {"stl_path": stl, "inlet": {"point": [0, 0, 0], "normal": [1, 0, 0]},
            "outlet": {"point": [80, 0, 0], "normal": [-1, 0, 0]}, "fluid": "water",
            "mean_velocity_ms": 0.5}
    simjobs._validate_pipe_flow_payload({}, dict(good))  # accepted

    bad_cases = [
        ({}, {k: v for k, v in good.items() if k != "stl_path"}, "geometry source"),
        ({"open_step": stl}, dict(good), "geometry source"),
        ({}, {**good, "mean_velocity_ms": None}, "mean_velocity_ms or flow_m3s"),
        ({}, {**good, "flow_m3s": 1e-4}, "mean_velocity_ms or flow_m3s"),
        ({}, {**good, "mean_velocity_ms": -1.0}, "must be > 0"),
        ({}, {**good, "inlet": {"point": [0, 0, 0], "normal": [0, 0, 0]}}, "non-zero"),
        ({}, {**good, "outlet": {"point": [0, 0], "normal": [1, 0, 0]}}, "3-vector"),
        ({}, {**good, "density_kgm3": 1000.0}, "go together"),
        ({}, {**good, "fluid": None}, "fluid name"),
        ({}, {**good, "max_steps": 0}, "max_steps"),
        ({}, {**good, "doc_id": "doc-1"}, "do not accept"),
    ]
    for payload, params, needle in bad_cases:
        params = {k: v for k, v in params.items() if v is not None}
        with pytest.raises(ValueError, match=needle):
            simjobs._validate_pipe_flow_payload(payload, params)
    assert "sim_run_pipe_flow" in simjobs.CORE_RPC_METHODS


def test_run_pipe_flow_refuses_incomplete_setups(tmp_path):
    tools = _tools()
    stl = _stl_file(tmp_path)
    res = _j(tools["run_pipe_flow"](stl_path=stl, mean_velocity_ms=0.5))
    assert res["ok"] is False and res["reason"] == "missing_boundary_planes"
    res = _j(tools["run_pipe_flow"](
        stl_path=stl, inlet={"point": [0, 0, 0], "normal": [1, 0, 0]},
        outlet={"point": [80, 0, 0], "normal": [-1, 0, 0]}))
    assert res["ok"] is False and res["reason"] == "missing_flow_rate"
    res = _j(tools["run_pipe_flow"](stl_path=stl, piping=_straight_doc()))
    assert res["ok"] is False and res["reason"] == "conflicting_geometry"
    res = _j(tools["run_pipe_flow"](run_id="r1"))  # no session network yet
    assert res["ok"] is False and res["reason"] == "no_piping"


# ── group 2: persistence (engine-less study store) ────────────────────────────

class _StudyStore:
    """In-memory stand-in for the engine's sim_study_list/add/edit on one document."""

    def __init__(self):
        self.studies: list = []
        self._next = 0

    def call(self, method, params=None):
        params = params or {}
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
        raise RuntimeError(f"unknown method {method}")


@pytest.fixture()
def study_store(monkeypatch):
    store = _StudyStore()
    monkeypatch.setattr(cb, "call", store.call)
    monkeypatch.setattr(pt, "_doc", lambda doc_id: "doc-1")
    return store


def test_network_study_merges_and_invalidates(study_store):
    doc = _straight_doc()
    r1 = pt._persist_network("doc-1", "plant", doc, params={"design": {"p": 1}},
                             results={"spec_check": {"all_pass": True},
                                      "routes": {"r1": {"bends": 0}}})
    r2 = pt._persist_network("doc-1", "plant", doc, results={"bom": {"totals": {}}})
    assert r1["saved_to_study"] and r2["study_id"] == r1["study_id"]
    nets = [s for s in study_store.studies if s["type"] == "piping_network"]
    assert len(nets) == 1, "one study per network, never a duplicate per analysis"
    st = nets[0]
    assert st["engine"] == "cpp-piping" and st["params"]["piping"] == doc
    assert st["params"]["design"] == {"p": 1}
    assert set(st["results"]) == {"spec_check", "bom", "routes"}

    changed = copy.deepcopy(doc)
    changed["runs"][0]["path"][1] = [1500.0, 0.0, 0.0]
    pt._persist_network("doc-1", "plant", changed, results={"bom": {"totals": {"x": 1}}})
    st = study_store.studies[0]
    # analyses of the old network are dropped; routed-run records accumulate
    assert set(st["results"]) == {"bom", "routes"}
    assert st["params"]["piping"] == changed


def test_piping_load_restores_a_saved_network(study_store):
    tools = _tools()
    doc = _straight_doc()
    solve = {"ok": True, "converged": True, "links": [{"id": "r1", "velocity_ms": 1.2}],
             "fluid": {"name": "water", "rho": 998.2, "mu": 1.0e-3}}
    pt._persist_network("doc-1", "plant", doc,
                        params={"boundary": [{"run": "r1", "end": "start", "pressure": 300}],
                                "design": {"design_pressure_kpa": 1000}},
                        results={"solve": solve, "spec_check": {"all_pass": True}})
    pt._persist_record("doc-1", "vessel", "V-101", "cpp-industrial", {}, {"volume_m3": 6.2})

    res = _j(tools["piping_load"](doc_id="doc-1"))
    assert res["ok"] is True and res["network"] == "plant" and res["piping"] == doc
    assert res["restored_results"] == ["solve", "spec_check"]
    assert [s["name"] for s in res["industrial_studies"]] == ["V-101"]
    # the session now addresses it by name, and piping_report has its solve back
    assert pt._NETWORKS["plant"]["doc"] == doc
    assert pt._PIPING["result"] == solve and pt._PIPING["network"] == "plant"
    assert pt._solve_defaults(pt._NETWORKS["plant"]["result"], "r1") == (solve["fluid"], 1.2)

    # by name: unknown -> refused with the saved names; several -> ambiguous
    res = _j(tools["piping_load"](doc_id="doc-1", network="nope"))
    assert res["ok"] is False and res["reason"] == "network_not_found"
    assert res["networks"] == ["plant"]
    pt._persist_network("doc-1", "utility", doc)
    res = _j(tools["piping_load"](doc_id="doc-1"))
    assert res["ok"] is False and res["reason"] == "network_ambiguous"


def test_saved_network_is_found_by_tools_without_a_session(study_store):
    """A tool given doc_id restores the saved network itself (the save/reopen path)."""
    doc = _straight_doc()
    pt._persist_network("doc-1", "piping", doc)
    d, name, refusal = pt._piping_input(None, None, "doc-1")
    assert refusal is None and d == doc and name == "piping"
    assert pt._NETWORKS["piping"]["doc"] == doc


def test_pipe_flow_collector_persists_one_study(study_store):
    meta = {"analysis": "pipe_flow", "doc": "doc-1", "name": "Pipe flow (r1)",
            "source": {"piping_network": "plant", "run_id": "r1"},
            "params": {"mean_velocity_ms": 0.5, "fluid": "water"}}
    raw = {"ok": True, "dp_pa": 120.0, "reynolds": 900.0, "regime": "laminar",
           "converged": True, "caveats": [], "warnings": [], "hint": "x", "next_actions": []}
    out = pt._collect_pipe_flow({"kind": "pipe_flow", "meta": meta}, raw)
    assert out["ok"] is True and out["saved_to_study"] is True
    assert out["dp_pa"] == 120.0 and out["source"]["run_id"] == "r1"
    (st,) = study_store.studies
    assert st["type"] == "pipe_flow" and st["engine"] == "cpp-lbm-internal"
    assert st["results"]["dp_pa"] == 120.0 and "hint" not in st["results"]
    assert st["params"]["source"] == meta["source"]
    # a re-collect of the same study name replaces it instead of piling up
    pt._collect_pipe_flow({"kind": "pipe_flow", "meta": meta}, raw)
    assert len(study_store.studies) == 1

    bad = pt._collect_pipe_flow({"kind": "pipe_flow", "meta": meta},
                                {"ok": False, "reason": "no_fluid_path", "hint": "check planes"})
    assert bad["ok"] is False and bad["reason"] == "no_fluid_path"
    assert len(study_store.studies) == 1


# ── group 3: live engine ──────────────────────────────────────────────────────

live = pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — tools are core RPCs")

_OBSTACLE = {"min": [800.0, -300.0, -300.0], "max": [1200.0, 300.0, 300.0], "name": "tank"}


def _segment_hits_box(a, b, lo, hi) -> bool:
    """Slab test: does the straight segment a→b pass through the open box (lo, hi)?"""
    t0, t1 = 0.0, 1.0
    for i in range(3):
        d = b[i] - a[i]
        if abs(d) < 1e-12:
            if not (lo[i] < a[i] < hi[i]):
                return False
            continue
        ta, tb = (lo[i] - a[i]) / d, (hi[i] - a[i]) / d
        t0, t1 = max(t0, min(ta, tb)), min(t1, max(ta, tb))
        if t0 >= t1:
            return False
    return True


def _route(tools, **kw):
    kw.setdefault("start", {"point": [0.0, 0.0, 0.0], "dir": [1.0, 0.0, 0.0]})
    kw.setdefault("end", {"point": [2000.0, 0.0, 0.0], "dir": [1.0, 0.0, 0.0]})
    kw.setdefault("nps", 2.0)
    kw.setdefault("material", "A106-B")
    kw.setdefault("obstacles", [_OBSTACLE])
    kw.setdefault("clearance_mm", 50.0)
    return _live(_j(tools["piping_route"](**kw)))


@live
def test_route_around_obstacle_then_spec_check_then_bom():
    tools = _tools()
    res = _route(tools, network="plant", fresh=True)
    assert res["ok"] is True, res
    path = res["path"]
    assert path[0] == pytest.approx([0.0, 0.0, 0.0]) and path[-1] == pytest.approx(
        [2000.0, 0.0, 0.0])
    # orthogonal route: every segment moves along exactly one axis
    for a, b in zip(path, path[1:]):
        assert sum(1 for i in range(3) if abs(b[i] - a[i]) > 1e-6) == 1, (a, b)
        assert not _segment_hits_box(a, b, _OBSTACLE["min"], _OBSTACLE["max"]), (a, b)
    assert res["bends"] >= 2 and res["length_mm"] > 2000.0
    seg_len = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
    assert res["length_mm"] == pytest.approx(seg_len, rel=1e-6)
    assert res["min_clearance_mm"] > 0.0
    net_doc = res["piping"]
    elbows = [f for f in net_doc["fittings"] if f.get("type") == "elbow"]
    assert len(elbows) == res["bends"], "an LR elbow at every bend"
    assert res["network"] == "plant" and pt._NETWORKS["plant"]["doc"] == net_doc

    # B31.3 on the routed network, addressed by session name
    spec = _live(_j(tools["piping_spec_check"](design_pressure_kpa=1000.0, piping="plant")))
    assert spec["ok"] is True and spec["code"] == "ASME B31.3"
    (row,) = spec["runs"]
    od = row["od_mm"]
    assert od == pytest.approx(60.3, abs=0.1)            # NPS 2 OD (B36.10M)
    # eq. (3a) pressure thickness with S(A106-B, 40 °C) = 137.9 MPa, E = W = 1, Y = 0.4
    t_p = 1.0 * od / (2.0 * (137.9 + 0.4 * 1.0))
    assert row["t_pressure_mm"] == pytest.approx(t_p, rel=0.01)
    # eq. (2) + CA, then grossed up over the 12.5 % mill under-tolerance
    assert row["t_required_mm"] == pytest.approx((t_p + 1.5) / (1.0 - 0.125), rel=0.01)
    assert row["t_nominal_mm"] == pytest.approx(3.91, abs=0.02)  # NPS 2 Sch 40 wall
    assert row["pass"] is True and spec["all_pass"] is True
    assert row["mawp_kpa"] > 1000.0 and 0.0 < row["utilization"] < 1.0
    assert row["support_span_m"] > 0.0
    assert pt._NETWORKS["plant"]["spec_check"]["all_pass"] is True

    # BOM of the same network (session default = the last network used)
    bom = _live(_j(tools["piping_bom"]()))
    assert bom["ok"] is True and bom["network"] == "plant"
    (line,) = bom["line_list"]
    assert line["length_mm"] == pytest.approx(res["length_mm"], abs=0.1)
    elbow_qty = sum(i["qty"] for i in bom["items"] if i["kind"] == "elbow")
    assert elbow_qty == res["bends"]
    pipe_m = bom["totals"]["pipe_length_m"]
    assert 0.0 < pipe_m <= res["length_mm"] / 1000.0 + 1e-6
    # NPS 2 Sch 40 steel pipe weighs 5.44 kg/m (B36.10M)
    pipe_mass = sum(i["mass_kg"] for i in bom["items"] if i["kind"] == "pipe")
    assert pipe_mass == pytest.approx(5.44 * pipe_m, rel=0.03)
    assert bom["totals"]["mass_kg"] >= pipe_mass
    assert bom["spools"] and all(s["length_mm"] <= 12000.0 for s in bom["spools"])


@live
def test_route_refuses_an_end_inside_an_obstacle():
    tools = _tools()
    res = _route(tools, fresh=True, end={"point": [1000.0, 0.0, 0.0]})
    assert res["ok"] is False and res["reason"] == "end_inside_obstacle" and res["hint"]


@live
def test_persist_then_piping_load_round_trip_on_a_live_doc():
    tools = _tools()
    doc = cb.call("doc_new", {"kind": "part", "name": f"piping_persist_{uuid.uuid4().hex[:8]}"})[
        "doc_id"]
    try:
        res = _route(tools, network="plant", fresh=True, doc_id=doc)
        assert res["ok"] is True and res["saved_to_study"] is True, res
        spec = _live(_j(tools["piping_spec_check"](design_pressure_kpa=1000.0, network="plant",
                                                   doc_id=doc)))
        assert spec["ok"] is True and spec["study_id"] == res["study_id"]
        bom = _live(_j(tools["piping_bom"](network="plant", doc_id=doc)))
        assert bom["study_id"] == res["study_id"]
        vessel = _live(_j(tools["vessel_size"](inner_diameter_mm=1500.0, tan_tan_length_mm=3000.0,
                                               design_pressure_kpa=1000.0, doc_id=doc,
                                               name="V-101")))
        assert vessel["ok"] is True and vessel["saved_to_study"] is True

        studies = cb.call("sim_study_list", {"doc_id": doc})["studies"]
        nets = [s for s in studies if s.get("type") == "piping_network"]
        assert len(nets) == 1
        assert set(nets[0]["results"]) >= {"routes", "spec_check", "bom"}
        assert any(s.get("type") == "vessel" and s.get("name") == "V-101" for s in studies)

        # forget the session (a fresh process after save/reopen) and restore from the doc
        pt._NETWORKS.clear()
        pt._PIPING.update({"doc": None, "result": None, "network": None})
        loaded = _j(tools["piping_load"](doc_id=doc))
        assert loaded["ok"] is True and loaded["network"] == "plant"
        assert loaded["piping"] == res["piping"]
        assert {"routes", "spec_check", "bom"} <= set(loaded["restored_results"])
        assert loaded["results"]["spec_check"]["all_pass"] is True
        assert [s["name"] for s in loaded["industrial_studies"]] == ["V-101"]
        again = _live(_j(tools["piping_bom"](piping="plant")))
        assert again["totals"] == bom["totals"]
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass


@live
def test_tee_per_path_constant_k_solves_reports_and_round_trips_the_study():
    """F2 cooling-water shape: tee run K 0.38 / branch K 1.14 + K 5 valves, constant K.

    Each fitting dP must equal K·ρ·v²/2 on the velocity of the pipe that carries it, and the
    tee overrides must survive the persisted piping_network study (piping_load)."""
    tools = _tools()
    doc = cb.call("doc_new", {"kind": "part", "name": f"piping_tee_k_{uuid.uuid4().hex[:8]}"})[
        "doc_id"]
    spec = {"nps": 2, "schedule": "40", "material": "A106-B"}
    try:
        built = _live(_j(tools["piping_edit"](ops=[
            {"op": "add_run", "path": [[0.0, 0.0, 0.0], [10_000.0, 0.0, 0.0]], **spec},
            {"op": "add_run", "path": [[10_000.0, 0.0, 0.0], [25_000.0, 0.0, 0.0]], **spec},
            {"op": "add_run", "path": [[10_000.0, 0.0, 0.0], [10_000.0, 20_000.0, 0.0]], **spec},
        ], network="CW-1", doc_id=doc)))
        assert built["ok"] is True, built
        run_a, run_b = built["piping"]["runs"][1]["id"], built["piping"]["runs"][2]["id"]
        (tee,) = [f for f in built["piping"]["fittings"] if f["type"] == "tee"]
        assert tee["params"]["run_ids"][1] == run_a and tee["params"]["branch_run"] == run_b

        # The benchmark model's first guesses are refused by name, listing the real keys.
        refused = _j(tools["piping_edit"](ops=[{"op": "update_fitting", "id": tee["id"],
                                                 "params": {"K_run": 0.38, "K_branch": 1.14}}],
                                          network="CW-1", doc_id=doc))
        assert refused["ok"] is False and refused["reason"] == "unknown_param", refused
        assert "'K_run'" in refused["hint"] and "'K_branch'" in refused["hint"]
        assert "k_model_run" in refused["hint"] and "k_model_branch" in refused["hint"]

        valve = {"k_model": {"kind": "constant", "K": 5.0}}
        edited = _live(_j(tools["piping_edit"](ops=[
            {"op": "add_fitting", "type": "custom", "at": [24_000.0, 0.0, 0.0], "run_id": run_a,
             "params": {**valve, "label": "BV-A"}},
            {"op": "add_fitting", "type": "custom", "at": [10_000.0, 19_000.0, 0.0],
             "run_id": run_b, "params": {**valve, "label": "BV-B"}},
            {"op": "update_fitting", "id": tee["id"], "params": {
                "k_model_run": {"kind": "constant", "K": 0.38},
                "k_model_branch": {"kind": "constant", "K": 1.14}}},
        ], network="CW-1", doc_id=doc)))
        assert edited["ok"] is True and edited["saved_to_study"] is True, edited

        rho, mu = 998.2, 0.001002
        boundary = [{"at": [0.0, 0.0, 0.0], "pressure": 250.0},
                    {"at": [25_000.0, 0.0, 0.0], "demand": 6.0},
                    {"at": [10_000.0, 20_000.0, 0.0], "demand": 6.0}]
        solved = _live(_j(tools["piping_solve"](doc="CW-1", boundary=boundary,
                                                fluid={"rho": rho, "mu": mu},
                                                network="CW-1", doc_id=doc)))
        assert solved["ok"] is True and solved["converged"] is True, solved
        links = {lk["id"]: lk for lk in solved["links"]}
        assert links[run_a]["k_total"] == pytest.approx(5.38)
        assert links[run_b]["k_total"] == pytest.approx(6.14)

        bore_m = links[run_a]["id_mm"] / 1000.0
        v = 6.0 / 3600.0 / (math.pi * bore_m ** 2 / 4.0)
        q = 0.5 * rho * v * v                              # dynamic pressure, Pa
        report = _live(_j(tools["piping_report"]()))
        assert report["ok"] is True, report
        rows = {seg["id"]: {b.get("fitting"): b for b in seg["breakdown"] if b.get("fitting")}
                for seg in report["segments"]}
        tee_a, tee_b = rows[run_a][tee["id"]], rows[run_b][tee["id"]]
        assert tee_a["method"] == "k_model_run(constant)"
        assert tee_b["method"] == "k_model_branch(constant)"
        assert tee_a["dp_kpa"] * 1000.0 == pytest.approx(0.38 * q, rel=1e-3)
        assert tee_b["dp_kpa"] * 1000.0 == pytest.approx(1.14 * q, rel=1e-3)
        for run in (run_a, run_b):
            (bv,) = [row for fid, row in rows[run].items() if fid != tee["id"]]
            assert bv["dp_kpa"] * 1000.0 == pytest.approx(5.0 * q, rel=1e-3)

        preview = _live(_j(tools["piping_fitting_k"](fitting=tee | {
            "params": {**tee["params"], "k_model_run": {"kind": "constant", "K": 0.38},
                       "k_model_branch": {"kind": "constant", "K": 1.14}}}, path="branch")))
        assert preview["K"] == pytest.approx(1.14)
        assert preview["paths"]["run"]["K"] == pytest.approx(0.38)

        # save/reopen: the persisted network keeps the overrides and solves identically
        pt._NETWORKS.clear()
        pt._PIPING.update({"doc": None, "result": None, "network": None})
        loaded = _j(tools["piping_load"](doc_id=doc, network="CW-1"))
        assert loaded["ok"] is True, loaded
        (saved_tee,) = [f for f in loaded["piping"]["fittings"] if f["id"] == tee["id"]]
        assert saved_tee["params"]["k_model_run"] == {"kind": "constant", "K": 0.38}
        assert saved_tee["params"]["k_model_branch"] == {"kind": "constant", "K": 1.14}
        again = _live(_j(tools["piping_solve"](doc="CW-1", boundary=boundary,
                                               fluid={"rho": rho, "mu": mu}, network="CW-1")))
        assert {lk["id"]: lk["k_total"] for lk in again["links"]} == {
            lid: lk["k_total"] for lid, lk in links.items()}
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass


@live
def test_vessel_size_envelope_matches_asme_viii_hand_calc():
    tools = _tools()
    res = _live(_j(tools["vessel_size"](inner_diameter_mm=1500.0, tan_tan_length_mm=3000.0,
                                        design_pressure_kpa=1000.0)))
    assert res["ok"] is True and res["code"] == "ASME VIII-1"
    p, s, e, ca = 1.0, 138.0, 0.85, 3.0                  # MPa, SA-516-70 @ 40 °C, E, mm
    # UG-27(c)(1) on the corroded inside radius R = ID/2 + CA; t_required includes CA
    t_shell = p * (750.0 + ca) / (s * e - 0.6 * p) + ca
    shell = res["shell"]
    assert shell["t_required_mm"] == pytest.approx(t_shell, rel=0.03)
    assert shell["t_nominal_mm"] >= shell["t_required_mm"]
    assert shell["mawp_kpa"] >= 1000.0
    heads = res["heads"]
    # UG-32(d) 2:1 ellipsoidal head on the corroded diameter, + CA
    t_head = p * (1500.0 + 2.0 * ca) / (2.0 * s * e - 0.2 * p) + ca
    assert heads["type"] == "ellipsoidal_2_1"
    assert heads["t_required_mm"] == pytest.approx(t_head, rel=0.03)
    assert heads["t_nominal_mm"] >= heads["t_required_mm"]
    assert heads["depth_mm"] == pytest.approx(1500.0 / 4.0, rel=0.02)
    # UG-99(b): 1.3 × MAWP × stress ratio (≥ 1)
    assert res["hydrotest_kpa"] >= 1.3 * min(shell["mawp_kpa"], heads["mawp_kpa"]) * 0.999
    # shell π/4·D²·L + two 2:1 heads π·D³/24
    vol = math.pi / 4.0 * 1.5 ** 2 * 3.0 + 2.0 * math.pi * 1.5 ** 3 / 24.0
    assert res["volume_m3"] == pytest.approx(vol, rel=0.03)
    assert 0.0 < res["mass_empty_kg"] < res["mass_operating_kg"] < res["mass_full_water_kg"]
    assert res["mass_full_water_kg"] - res["mass_empty_kg"] == pytest.approx(vol * 1000.0,
                                                                            rel=0.05)

    # a required volume solves the tan-tan length back
    inv = _live(_j(tools["vessel_size"](inner_diameter_mm=1500.0, volume_m3=res["volume_m3"],
                                        design_pressure_kpa=1000.0)))
    assert inv["ok"] is True
    assert inv["tan_tan_length_mm"] == pytest.approx(3000.0, rel=0.03)

    bad = _live(_j(tools["vessel_size"](inner_diameter_mm=-1.0, tan_tan_length_mm=3000.0,
                                        design_pressure_kpa=1000.0)))
    assert bad["ok"] is False and bad["reason"] == "invalid_inner_diameter" and bad["hint"]


@live
def test_duct_size_envelope_equal_friction_tree():
    tools = _tools()
    segments = [
        {"id": "main", "airflow_m3s": 1.0, "length_m": 10.0, "k_fittings": 0.5,
         "shape": "round"},
        {"id": "a", "parent": "main", "airflow_m3s": 0.6, "length_m": 8.0, "k_fittings": 0.3,
         "shape": "round"},
        {"id": "b", "parent": "main", "airflow_m3s": 0.4, "length_m": 15.0, "k_fittings": 0.3,
         "shape": "rect", "aspect_ratio": 2.0},
    ]
    res = _live(_j(tools["duct_size"](segments=segments)))
    assert res["ok"] is True, res
    rows = {r["id"]: r for r in res["segments"]}
    assert set(rows) == {"main", "a", "b"}
    air = res["air"]
    nu_air = air["viscosity_pas"] / air["density_kgm3"]
    assert nu_air == pytest.approx(1.516e-5, rel=0.03)  # m²/s, dry air at 20 °C
    flows = {s["id"]: s["airflow_m3s"] for s in segments}
    for sid, r in rows.items():
        de = r["equivalent_diameter_mm"] / 1000.0
        assert r["velocity_ms"] <= 10.0 + 1e-6
        # each term is rounded to 1e-3 Pa on its own
        assert r["dp_total_pa"] == pytest.approx(r["dp_friction_pa"] + r["dp_fittings_pa"],
                                                 abs=2e-3)
        # Huebscher: a rect duct carries the friction of its equal-flow round equivalent De,
        # so Re uses that duct's velocity Q/(pi De^2/4), not the rect face velocity.
        v_re = (r["velocity_ms"] if sid != "b"
                else flows[sid] / (math.pi / 4.0 * de * de))
        assert r["reynolds"] == pytest.approx(v_re * de / nu_air, rel=0.01)
        if sid != "b":
            d = r["diameter_mm"] / 1000.0
            assert r["equivalent_diameter_mm"] == pytest.approx(r["diameter_mm"], abs=0.05)
            assert r["velocity_ms"] == pytest.approx(flows[sid] / (math.pi / 4.0 * d * d),
                                                     rel=0.01)
            # sizes round UP from the exact equal-friction diameter: never above the rate
            length = next(s["length_m"] for s in segments if s["id"] == sid)
            assert r["dp_friction_pa"] / length <= 1.0 + 1e-6
    b = rows["b"]
    w, h = b["width_mm"], b["height_mm"]
    assert b["equivalent_diameter_mm"] == pytest.approx(
        1.30 * (w * h) ** 0.625 / (w + h) ** 0.25, rel=0.01)
    # path sums through the parent tree; the worst path is the fan static
    paths = {"a": rows["main"]["dp_total_pa"] + rows["a"]["dp_total_pa"],
             "b": rows["main"]["dp_total_pa"] + rows["b"]["dp_total_pa"]}
    worst = max(paths, key=paths.get)
    assert res["critical_path"] == ["main", worst]
    assert res["fan_static_pa"] == pytest.approx(paths[worst], abs=0.01)

    bad = _live(_j(tools["duct_size"](segments=[])))
    assert bad["ok"] is False and bad["reason"] == "missing_segments" and bad["hint"]


def test_sizing_without_doc_id_says_it_was_not_saved_and_how(monkeypatch):
    """2026-10-08 demo runs: both models sized the vessel and the ducts with no doc_id and
    delivered numbers nobody could reopen; the engine's next_actions pointed at the raw
    sim_study_add RPC this wrapper already performs when given a doc_id."""
    from services.mcp import piping_tools as pt

    def fake_rpc(method, params):
        return {"ok": True, "warnings": [], "fan_static_pa": 1.66, "next_actions": [
            f"persist the sizing with sim_study_add {{type:'{method}'}}", "keep this"]}

    monkeypatch.setattr(pt, "_rpc", fake_rpc)
    tools = _tools()
    vessel = _j(tools["vessel_size"](inner_diameter_mm=1000.0, design_pressure_kpa=1400.0,
                                     volume_m3=2.0))
    assert vessel["saved_to_study"] is False
    assert "new_part" in vessel["study_not_saved_reason"]
    assert vessel["next_actions"] == ["keep this"]

    stubs = [{"id": "main", "airflow_m3s": 3.0, "length_m": 1.0},
             {"id": "b1", "parent": "main", "airflow_m3s": 1.2, "length_m": 1.0}]
    duct = _j(tools["duct_size"](segments=stubs))
    assert duct["saved_to_study"] is False
    assert any("fan_static_pa" in w for w in duct["warnings"])
    real = [{**s, "length_m": 15.0, "k_fittings": 1.5} for s in stubs]
    assert not _j(tools["duct_size"](segments=real))["warnings"]



def _write_cylinder_stl(path: str, d_mm: float, l_mm: float, n: int = 96) -> None:
    """Closed, outward-oriented round duct along +X from x=0 to x=l_mm (ASCII STL)."""
    r = d_mm / 2.0
    ring = [(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n))
            for k in range(n)]
    tris = []
    for k in range(n):
        (y0, z0), (y1, z1) = ring[k], ring[(k + 1) % n]
        a0, a1 = (0.0, y0, z0), (0.0, y1, z1)
        b0, b1 = (l_mm, y0, z0), (l_mm, y1, z1)
        tris += [(a0, b1, b0), (a0, a1, b1),                 # wall, normal radially out
                 ((0.0, 0.0, 0.0), a1, a0),                  # inlet cap, normal −X
                 ((l_mm, 0.0, 0.0), b0, b1)]                 # outlet cap, normal +X
    lines = ["solid duct"]
    for p, q, s in tris:
        u = [q[i] - p[i] for i in range(3)]
        v = [s[i] - p[i] for i in range(3)]
        nrm = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        m = math.sqrt(sum(c * c for c in nrm)) or 1.0
        lines.append("  facet normal %.9g %.9g %.9g" % tuple(c / m for c in nrm))
        lines.append("    outer loop")
        for vert in (p, q, s):
            lines.append("      vertex %.9g %.9g %.9g" % vert)
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid duct")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


@live
def test_run_pipe_flow_straight_duct_matches_hagen_poiseuille(tmp_path, monkeypatch):
    """Laminar flow in a straight round duct: Δp = 32·μ·L·V / D² within 15 %.

    The durable job's SOLVE + COLLECT stages run in-process — the very stage function and
    collector the child/parent use, with the payload validated (and the backend probed)
    exactly as ``simjobs.start`` would — so the test needs no spawned worker.
    """
    def _inline(kind, stage, payload, meta, *, background, doc_id):
        payload = simjobs._resolve_tokens(payload, str(tmp_path / "progress.json"))
        simjobs._validate_start(kind, stage, payload, meta)
        raw = simjobs.STAGES[stage](payload, lambda snapshot: None)
        return pt._collect_pipe_flow({"kind": kind, "meta": meta}, raw)

    monkeypatch.setattr(pt, "_run_solve", _inline)
    monkeypatch.setattr(pt, "_autosave_all_open", lambda: None)
    d_mm, l_mm = 10.0, 80.0
    rho, mu, v = 1260.0, 1.41, 0.5                      # glycerol-like: Re ≈ 4.5 (laminar)
    stl = str(tmp_path / "duct.stl")
    _write_cylinder_stl(stl, d_mm, l_mm)
    res = _live(_j(_tools()["run_pipe_flow"](
        stl_path=stl,
        inlet={"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]},
        outlet={"point": [l_mm, 0.0, 0.0], "normal": [-1.0, 0.0, 0.0]},
        density_kgm3=rho, viscosity_pas=mu, mean_velocity_ms=v, voxel_mm=0.5)))
    assert res["ok"] is True, res
    d, length = d_mm / 1000.0, l_mm / 1000.0
    dp_hp = 32.0 * mu * length * v / (d * d)
    re = rho * v * d / mu
    assert res["regime"] == "laminar"
    assert res["reynolds"] == pytest.approx(re, rel=0.10)
    assert res["hydraulic_diameter_mm"] == pytest.approx(d_mm, rel=0.10)
    assert abs(res["dp_pa"] - dp_hp) <= 0.15 * dp_hp, (res["dp_pa"], dp_hp)
    assert res["provenance"]["engine"] == "cpp-lbm-internal"
    assert res["pressure_profile"] and res["source"] == {"stl_path": stl}


def test_piping_edit_starts_and_remembers_network(monkeypatch):
    calls = []
    doc = _straight_doc()
    ops = [{"op": "add_run", "path": [[0, 0, 0], [1000, 0, 0]], "nps": 2}]
    def rpc(method, params):
        calls.append((method, params))
        return {"ok": True, "piping": doc, "created": ["r1"], "removed": [], "changed": []}
    monkeypatch.setattr(pt, "_rpc", rpc)
    result = _j(_tools()["piping_edit"](json.dumps(ops), network="plant", tol_mm=0.5))
    assert result["ok"] and result["created"] == ["r1"] and result["removed"] == []
    assert calls == [("piping_edit", {"ops": ops, "tol_mm": 0.5})]
    assert pt._NETWORKS["plant"]["doc"] == doc
    assert result["next_actions"]


def test_piping_edit_persists_and_refusal_does_not_change_session(monkeypatch, study_store):
    doc = _straight_doc()
    pt._remember("plant", doc)
    updated = copy.deepcopy(doc)
    updated["runs"][0]["name"] = "renamed"
    monkeypatch.setattr(pt, "_rpc", lambda method, params: {
        "ok": True, "piping": updated, "created": [], "removed": [], "changed": ["r1"]})
    result = _j(_tools()["piping_edit"]([{"op": "set_run", "id": "r1", "name": "renamed"}],
                                       network="plant", doc_id="doc-1"))
    assert result["saved_to_study"]
    assert study_store.studies[0]["params"]["piping"] == updated
    monkeypatch.setattr(pt, "_rpc", lambda *args: {"ok": False, "reason": "unknown_run", "hint": "missing"})
    refused = _j(_tools()["piping_edit"]([{"op": "remove_run", "id": "missing"}], network="plant"))
    assert not refused["ok"] and pt._NETWORKS["plant"]["doc"] == updated


def test_fitting_k_wrapper_forwards_native_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(pt, "_rpc", lambda method, params: calls.append((method, params)) or {
        "ok": True, "K": 0.2, "method": "Crane"})
    fitting = {"type": "valve", "params": {"valve_type": "ball"}}
    result = _j(_tools()["piping_fitting_k"](json.dumps(fitting), nps=2, schedule="80",
                                          reynolds=1e5, velocity_ms=1, fluid="water"))
    assert result["K"] == 0.2
    assert calls == [("piping_fitting_k", {"fitting": fitting, "nps": 2, "schedule": "80",
                                         "reynolds": 1e5, "velocity_ms": 1, "fluid": "water"})]


def test_fitting_cfd_resolves_run_spec_and_background_apply(monkeypatch):
    doc = _straight_doc()
    doc["runs"][0]["schedule"] = "80"
    doc["fittings"] = [{"id": "f1", "type": "elbow", "run_id": "r1", "params": {"angle": 90}}]
    pt._remember("plant", doc)
    calls = []
    monkeypatch.setattr(pt, "_fitting_cfd_rpc", lambda method, params: calls.append((method, params)) or {
        "ok": True, "job_id": "fitting-k-test"})
    monkeypatch.setattr(pt, "_doc", lambda doc: "d1")
    result = _j(_tools()["piping_fitting_k_cfd"](fitting_id="f1", network="plant", doc_id="d1",
                                               apply=True, background=True))
    assert result["job_id"] == "fitting-k-test" and result["state"] == "running"
    method, request = calls[0]
    assert method == "piping_fitting_k_cfd_start"
    assert request["fitting"]["nps"] == 2 and request["schedule"] == "80"
    assert request["apply_context"] == {"piping": doc, "fitting_id": "f1", "network": "plant", "doc_id": "d1"}
    assert result["next_actions"]


def test_fitting_cfd_poll_remembers_applied_result_and_cancel(monkeypatch):
    doc = _straight_doc()
    model = {"kind": "table", "source": "cfd", "points": [{"re": 1e4, "K": 0.2}]}
    calls = []
    monkeypatch.setattr(pt, "_fitting_cfd_rpc", lambda method, params: calls.append(method) or {
        "ok": True, "job_id": "fitting-k-test", "state": "done", "result": {
            "ok": True, "k_model": model, "applied": True, "piping": doc, "network": "plant"}})
    result = _j(_tools()["piping_fitting_k_cfd"](job_id="fitting-k-test"))
    assert result["k_model"] == model and pt._NETWORKS["plant"]["doc"] == doc
    _tools()["piping_fitting_k_cfd"](job_id="fitting-k-test", cancel=True)
    assert calls == ["piping_fitting_k_cfd_status", "piping_fitting_k_cfd_cancel"]

"""Tests for hydraulic→piping and hydraulic→FEA couplings on the
aicad.simhandoff/1 seam (contracts/sim_handoff.md).

Groups:
  1. Translators — the hoses→piping network with exact hose bores (driven through
     the flipped tool: the translator lives in the C++ core, so these
     need libchudware and skip cleanly without it); estimate_line_dp physics sanity.
  2. Envelopes — handoff/verify envelopes for both couplings validate structurally
     and cross-checks fire correctly on mock solve results (the pure-Python
     envelope compositions in hydraulic_tools, mirroring test_simhandoff.py).
  3. Pump-margin feedback — real line losses change the pump margin.
  4. Tools — driven against a stub FastMCP; the C++ core always runs the live
     solve (the old fluids-absent "solve_pending" stage is gone).
  5. piping_solve consumer — network_from_handoff extracts doc/boundary/fluid.

The tool-driven groups need libchudware (the tool bodies are core_bridge wrappers
now); the envelope/margin/estimate groups stay pure Python.

Run: pytest services/mcp/test_hydraulic_handoff.py -q
"""

from __future__ import annotations

import json
import math

import pytest

from services.mcp import core_bridge as cb
from services.mcp import hydraulic_tools as ht
from services.mcp import piping_tools as pt
from services.mcp import simhandoff as sh


_HAVE_CORE = cb.available()


# ── fixtures ──────────────────────────────────────────────────────────────────

def _component(cid, type, name, **params) -> dict:
    return {"id": cid, "type": type, "name": name, "position": [0.0, 0.0],
            "angle": 0.0, "ports": [], "params": params,
            "sourcing": {"mpn": None, "manufacturer": None, "description": None,
                         "quantity": 1, "unit": "ea", "category": None}}


def _circuit() -> dict:
    """Two-hose circuit: pump → cylinder pressure line + return line (inline
    aicad.hydraulic/1 — the engine schema factories are deleted with the oracle)."""
    doc = {
        "schema": "aicad.hydraulic/1",
        "id": "test-circuit",
        "name": "Press circuit",
        "units": {"pressure": "bar", "flow": "L/min", "force": "kN", "position": "mm"},
        "components": [
            _component("c1", "pump", "pump"),
            _component("c2", "cylinder_linear", "cyl", bore_mm=50.0, rod_mm=28.0),
        ],
        "hoses": [
            {"id": "h1", "net_name": "PRESSURE_LINE",
             "connections": [{"component_id": "c1", "port": "pressure"},
                             {"component_id": "c2", "port": "work_a"}]},
            {"id": "h2", "net_name": "RETURN_LINE",
             "connections": [{"component_id": "c2", "port": "work_b"},
                             {"component_id": "c1", "port": "tank"}]},
        ],
        "annotations": [],
    }
    # hose-carried geometry (the translator reads these fields)
    doc["hoses"][0]["id_mm"] = 12.7      # -8 hose
    doc["hoses"][0]["length_mm"] = 2500.0
    doc["hoses"][1]["id_mm"] = 19.0      # -12 hose
    doc["hoses"][1]["length_mm"] = 3000.0
    return doc


def _empty_circuit() -> dict:
    return {"schema": "aicad.hydraulic/1", "id": "empty", "name": "empty",
            "units": {"pressure": "bar", "flow": "L/min", "force": "kN",
                      "position": "mm"},
            "components": [], "hoses": [], "annotations": []}


def _net(doc=None, **kw):
    """The hoses→piping translation, driven through the flipped tool — the
    translator lives in the C++ core (core/hydraulic/handoff.cpp) and
    hydraulic_piping_handoff's base payload IS the net."""
    if not _HAVE_CORE:
        pytest.skip("libchudware not staged — hoses_to_piping lives in the C++ core")
    kw.setdefault("flow_lpm", 30.0)
    kw.setdefault("supply_pressure_bar", 200.0)
    tools = _tools()
    res = json.loads(tools["hydraulic_piping_handoff"](
        doc=json.dumps(doc or _circuit()), **kw))
    if not res.get("ok"):
        return res
    return {k: res[k] for k in
            ("ok", "piping_doc", "boundary", "fluid", "lines", "estimates", "warnings")}


class _StubMCP:
    """Collects tool functions the way FastMCP registration would."""
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools():
    m = _StubMCP()
    ht.register_hydraulic_tools(m)
    return m.tools


# ── group 1: translators ──────────────────────────────────────────────────────

def test_hoses_to_piping_valid_doc_exact_bores():
    net = _net()
    assert net["ok"]
    pdoc = net["piping_doc"]  # the core validated the doc it generated (ok attests)
    assert [r["bore_mm"] for r in pdoc["runs"]] == [12.7, 19.0]
    assert [r["name"] for r in pdoc["runs"]] == ["PRESSURE_LINE", "RETURN_LINE"]
    # each run gets a fixed supply pressure at start + demand at end
    assert len(net["boundary"]) == 4
    pres = [b for b in net["boundary"] if "pressure" in b]
    dems = [b for b in net["boundary"] if "demand" in b]
    assert len(pres) == 2 and all(b["pressure"] == 20000.0 for b in pres)
    assert len(dems) == 2 and all(abs(b["demand"] - 1.8) < 1e-9 for b in dems)
    # runs are spaced so endpoint snapping cannot merge lines
    ys = {r["path"][0][1] for r in pdoc["runs"]}
    assert len(ys) == 2
    assert pdoc["runs"][0]["connections"] == _circuit()["hoses"][0]["connections"]


def test_hoses_to_piping_line_overrides_beat_hose_fields():
    net = _net(lines={"h1": {"id_mm": 9.5, "flow_lpm": 12.0}})
    row = next(r for r in net["lines"] if r["hose_id"] == "h1")
    assert row["id_mm"] == 9.5 and row["flow_lpm"] == 12.0
    assert row["length_mm"] == 2500.0  # hose field still used where not overridden


def test_hoses_to_piping_no_hoses_soft_fails():
    net = _net(_empty_circuit())
    assert not net["ok"] and "no hoses" in net["reason"]


def test_estimate_line_dp_laminar_hand_calc():
    # Q=10 L/min through 12.7 mm bore, ISO VG32: v = Q/A, laminar check
    est = ht.estimate_line_dp(flow_lpm=10.0, id_mm=12.7, length_mm=1000.0)
    assert est["ok"]
    d = 0.0127
    v = (10.0 / 60000.0) / (math.pi / 4 * d * d)
    assert abs(est["velocity_ms"] - v) < 1e-3
    re = 870.0 * v * d / 0.0278
    assert abs(est["reynolds"] - re) < 1.0
    assert est["regime"] == "laminar"
    # laminar dp = 32 μ L v / d²  (Hagen–Poiseuille)
    dp_kpa = 32 * 0.0278 * 1.0 * v / d ** 2 / 1000.0
    assert abs(est["dp_kpa"] - dp_kpa) / dp_kpa < 0.01


def test_estimate_line_dp_turbulent_and_bad_inputs():
    est = ht.estimate_line_dp(flow_lpm=200.0, id_mm=12.7, length_mm=1000.0,
                              rho=998.0, mu=1.0e-3)  # water → turbulent
    assert est["ok"] and est["reynolds"] > 4000 and est["friction"] < 0.06
    assert not ht.estimate_line_dp(flow_lpm=0, id_mm=12.7, length_mm=1)["ok"]
    assert not ht.estimate_line_dp(
        flow_lpm=10, id_mm=12.7, length_mm=1, rho=math.nan)["ok"]
    assert not ht.estimate_line_dp(
        flow_lpm=10, id_mm=12.7, length_mm=1, mu=0)["ok"]
    assert not ht.estimate_line_dp(
        flow_lpm=float("1e308"), id_mm=12.7, length_mm=1)["ok"]


# ── group 2: envelopes ────────────────────────────────────────────────────────

def test_piping_handoff_envelope_valid_and_inline_doc():
    doc = _circuit()
    net = _net(doc)
    env = ht._piping_handoff_envelope(doc, net, 200.0)
    assert sh.validate_envelope(env) == []
    assert env["coupling"] == "hydraulic_to_piping"
    g = env["geometry"][0]
    assert g["kind"] == "doc" and g["inline"]["schema"] == "aicad.piping/1"
    names = [c["name"] for c in env["conditions"]]
    assert "supply_pressure" in names and "r1_flow" in names and "r2_flow" in names
    assert {e["quantity"] for e in env["expectations"]} == \
        {"pressure_drop:r1", "pressure_drop:r2"}
    assert env["meta"]["boundary"] == net["boundary"]
    assert "independent" in env["meta"]["topology_model"]


def _mock_solve_from(net, factor=1.0):
    """A piping-solve-shaped result whose link dps = factor × the estimates."""
    return {"ok": True, "converged": True,
            "links": [{"id": rid, "dp_kpa": est["dp_kpa"] * factor}
                      for rid, est in net["estimates"].items()]}


def test_verify_piping_envelope_pass_and_fail():
    doc = _circuit()
    net = _net(doc)
    env = ht._piping_handoff_envelope(doc, net, 200.0)

    ok_env, checks = ht._verify_piping_envelope(env, _mock_solve_from(net, 1.1), 30.0)
    assert sh.validate_envelope(ok_env) == []
    assert ok_env["pass"] and len(checks) == 2
    assert ok_env["target"]["handoff_id"] == env["id"]

    bad_env, checks = ht._verify_piping_envelope(env, _mock_solve_from(net, 3.0), 30.0)
    assert sh.validate_envelope(bad_env) == []
    assert not bad_env["pass"] and all(not c["pass"] for c in checks)
    assert bad_env["verdict"].startswith("FAIL")

    partial = _mock_solve_from(net)
    partial["links"].pop()
    partial_env, partial_checks = ht._verify_piping_envelope(env, partial, 30.0)
    assert not partial_env["pass"]
    assert len(partial_checks) == 2 and any(not c["pass"] for c in partial_checks)
    assert partial_env["warnings"]

    edge_factor = 1.0 / (1.0 - 0.30004)
    edge_env, edge_checks = ht._verify_piping_envelope(
        env, _mock_solve_from(net, edge_factor), 30.0)
    assert edge_checks[0]["deviation_pct"] == 30.0
    assert edge_checks[0]["pass"] and edge_env["pass"]


def test_fea_handoff_envelope_forces_and_cases():
    # Hand calc: 50 mm bore @ 200 bar → 39 270 N extend (engine.hydraulic docstring)
    env = ht._fea_handoff_envelope(bore_mm=50.0, rod_mm=28.0, max_pressure_bar=200.0,
                                   axis=[0, 0, 1], efficiency=1.0,
                                   load_face="the mount face", fixture_face="the base",
                                   target_doc_id="d9")
    assert sh.validate_envelope(env) == []
    assert env["coupling"] == "hydraulic_to_fea"
    ext = next(c for c in env["conditions"] if c["case"] == "extend_max_pressure")
    ret = next(c for c in env["conditions"] if c["case"] == "retract_max_pressure")
    assert abs(ext["value"][2] + 39269.9) < 1.0      # into the mount = −axis
    a_ann = math.pi / 4 * (0.05 ** 2 - 0.028 ** 2)
    assert abs(ret["value"][2] - a_ann * 200e5) < 1.0  # +axis
    fix = next(c for c in env["conditions"] if c["kind"] == "fixture")
    assert fix["applied_to"] == "the base"
    assert env["target"]["doc_id"] == "d9"
    assert {e["quantity"] for e in env["expectations"]} == \
        {"mount_force:extend_max_pressure", "mount_force:retract_max_pressure"}


def test_verify_fea_envelope_pass_direction_warning_and_fail():
    env = ht._fea_handoff_envelope(bore_mm=50.0, rod_mm=0.0, max_pressure_bar=200.0,
                                   axis=[1, 0, 0], efficiency=0.95,
                                   load_face="f", fixture_face="b")
    v = ht._verify_fea_envelope(env, 0.5)
    assert sh.validate_envelope(v) == []
    assert v["pass"] and len(v["checks"]) == 2 and not v.get("warnings")

    # corrupt one force → magnitude check fails + colinearity warning
    bad = json.loads(json.dumps(env))
    for c in bad["conditions"]:
        if c.get("case") == "extend_max_pressure":
            c["value"] = [c["value"][0] * 0.8, 8000.0, 0.0]
    v2 = ht._verify_fea_envelope(bad, 0.5)
    assert sh.validate_envelope(v2) == []
    assert not v2["pass"]
    assert any("not colinear" in w for w in v2.get("warnings", []))

    # fea_result rides as a non-gating advisory
    v3 = ht._verify_fea_envelope(env, 0.5,
                                 fea_result={"summary": {"min_fos": 2.4}})
    assert v3["pass"]
    assert v3["advisories"][0]["detail"]["min_fos"] == 2.4

    missing = json.loads(json.dumps(env))
    missing["conditions"] = [
        c for c in missing["conditions"]
        if c.get("case") != "retract_max_pressure"
    ]
    assert not ht._verify_fea_envelope(missing, 0.5)["pass"]

    duplicate = json.loads(json.dumps(env))
    duplicate["conditions"].append(json.loads(json.dumps(duplicate["conditions"][0])))
    assert not ht._verify_fea_envelope(duplicate, 0.5)["pass"]

    missing_axis = json.loads(json.dumps(env))
    missing_axis["meta"].pop("axis")
    assert not ht._verify_fea_envelope(missing_axis, 0.5)["pass"]

    rounded_edge = json.loads(json.dumps(env))
    for condition in rounded_edge["conditions"]:
        if condition.get("case") == "extend_max_pressure":
            condition["value"] = [
                component * 1.005004 for component in condition["value"]]
    edge_verify = ht._verify_fea_envelope(rounded_edge, 0.5)
    assert edge_verify["checks"][0]["deviation_pct"] == 0.5
    assert edge_verify["checks"][0]["pass"]


# ── group 3: pump-margin feedback ─────────────────────────────────────────────

def test_margin_changes_when_losses_are_real():
    # estimate says 3 bar of hose loss; the solve says 8 bar → margin shrinks
    m = ht._margin_update(200.0, 180.0, 3.0, 8.0)
    assert m["pump_margin_estimate_bar"] == 17.0
    assert m["pump_margin_solved_bar"] == 12.0
    assert m["pump_margin_solved_bar"] < m["pump_margin_estimate_bar"]
    assert m["pump_margin_solved_pct"] == 6.0
    # no load pressure → losses reported, margins omitted
    m2 = ht._margin_update(200.0, None, 3.0, 8.0)
    assert "pump_margin_solved_bar" not in m2


# ── group 4: tools against a stub MCP ─────────────────────────────────────────

def test_tool_piping_handoff_engine_less_or_live():
    tools = _tools()
    doc = _circuit()
    res = json.loads(tools["hydraulic_piping_handoff"](
        supply_pressure_bar=200.0, flow_lpm=30.0, load_pressure_bar=180.0,
        doc=json.dumps(doc)))
    if not _HAVE_CORE:
        # no libchudware staged: the wrapper degrades contract-shaped, never raises
        assert res["ok"] is False and "core engine unavailable" in res["reason"]
        return
    assert res["ok"]
    env = res["simhandoff"]
    assert sh.validate_envelope(env) == []
    assert env["coupling"] == "hydraulic_to_piping"
    # the C++ core IS the solver: the live solve always runs (no solve_pending stage)
    assert res["stage"] == "solved"
    assert res["solve"]["ok"] and len(res["per_line_dp_kpa"]) == 2
    assert "pump_margin_solved_bar" in res["margin"]
    # the whole point: real losses move the margin off the estimate
    assert res["verify"]["checks"]


@pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — tool bodies are core RPCs")
def test_tool_verify_piping_with_mock_solve():
    tools = _tools()
    doc = _circuit()
    handoff_res = json.loads(tools["hydraulic_piping_handoff"](
        supply_pressure_bar=200.0, flow_lpm=30.0, doc=json.dumps(doc)))
    net = {"estimates": {q.split(":")[1]: {"dp_kpa": e["value"]}
                         for q, e in ((e["quantity"], e) for e in
                                      handoff_res["simhandoff"]["expectations"])}}
    mock_solve = _mock_solve_from(net, 1.05)
    res = json.loads(tools["hydraulic_verify_piping"](
        handoff=handoff_res["simhandoff"], solve_result=mock_solve,
        supply_pressure_bar=200.0, load_pressure_bar=180.0, tolerance_pct=25.0))
    assert res["ok"] and res["pass"]
    assert sh.validate_envelope(res["simhandoff"]) == []
    assert res["simhandoff"]["kind"] == "verify"
    assert res["margin"]["pump_margin_solved_bar"] is None
    assert all(v["pump_margin_solved_bar"] is not None
               for v in res["margin"]["per_line"].values())

    mock_solve["converged"] = False
    rejected = json.loads(tools["hydraulic_verify_piping"](
        handoff=handoff_res["simhandoff"], solve_result=mock_solve))
    assert not rejected["ok"]


@pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — tool bodies are core RPCs")
def test_tool_fea_handoff_and_verify_roundtrip():
    tools = _tools()
    res = json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=50.0, max_pressure_bar=200.0, rod_mm=28.0,
        axis=[0, 0, -1], target_doc_id="d3"))
    assert res["ok"]
    assert abs(res["extend_force_n"] - 39269.908) < 0.01
    assert set(res["fea_args"]) == {"extend_max_pressure", "retract_max_pressure"}
    ld = res["fea_args"]["extend_max_pressure"]["loads"][0]
    assert ld["force"][2] > 0  # −(−z axis) = +z into the mount
    assert res["fea_args"]["extend_max_pressure"]["doc_id"] == "d3"
    assert sh.validate_envelope(res["simhandoff"]) == []

    # verify from session state (no args)
    v = json.loads(tools["hydraulic_verify_fea"]())
    assert v["ok"] and v["pass"]
    assert v["simhandoff"]["handoff_id"] == res["simhandoff"]["id"]


@pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — tool bodies are core RPCs")
def test_tool_fea_handoff_input_guards():
    tools = _tools()
    assert not json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=0, max_pressure_bar=200.0))["ok"]
    assert not json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=50.0, max_pressure_bar=200.0, rod_mm=60.0))["ok"]
    assert not json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=50.0, max_pressure_bar=200.0, axis=[0, 0, 0]))["ok"]
    assert not json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=50.0, max_pressure_bar=200.0,
        load_face="same", fixture_face="same"))["ok"]
    assert not json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=50.0, max_pressure_bar=200.0, target_doc_id=42))["ok"]


# ── group 5: piping_solve consumes the handoff ────────────────────────────────

def test_network_from_handoff_extracts_doc_boundary_fluid():
    doc = _circuit()
    net = _net(doc)
    env = ht._piping_handoff_envelope(doc, net, 200.0)
    pdoc, bc, fl, errs = pt.network_from_handoff(env)
    assert errs == []
    assert pdoc == net["piping_doc"] and bc == net["boundary"]
    assert fl["name"] == "hydraulic_oil_iso_vg32"
    # also accepts the whole producer result wrapper
    pdoc2, _, _, errs2 = pt.network_from_handoff({"ok": True, "simhandoff": env})
    assert errs2 == [] and pdoc2 == pdoc


def test_network_from_handoff_rejects_bad_envelopes():
    assert pt.network_from_handoff("nope")[3]
    assert pt.network_from_handoff({"schema": "wrong"})[3]
    doc = _circuit()
    net = _net(doc)
    env = ht._piping_handoff_envelope(doc, net, 200.0)
    env2 = json.loads(json.dumps(env))
    env2["geometry"] = []
    env2.pop("meta")
    _, _, _, errs = pt.network_from_handoff(env2)
    assert any("inline piping doc" in e for e in errs)
    assert any("boundary missing" in e for e in errs)

    for key, value in (
            ("coupling", "piping_to_cfd"),
            ("source", {"domain": "motion", "tool": "motion_fea_handoff"}),
            ("target", {"domain": "fea", "tool": "run_fea_static"})):
        wrong = json.loads(json.dumps(env))
        wrong[key] = value
        assert pt.network_from_handoff(wrong)[3]


@pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — live 1D solve skipped")
def test_live_solve_estimate_vs_solve_within_tolerance():
    """With fluids installed the whole loop runs live: solve the generated network
    and require the per-hose Darcy estimate to agree with the solver within 30%."""
    tools = _tools()
    res = json.loads(tools["hydraulic_piping_handoff"](
        supply_pressure_bar=200.0, flow_lpm=30.0, load_pressure_bar=180.0,
        doc=json.dumps(_circuit()), tolerance_pct=30.0))
    assert res["stage"] == "solved"
    assert res["verify"]["pass"], res["verify"]


# ----------------------------------------- guide-verbatim dialect (measured 2026-07-30)
# Driving cad_guide("hydraulic")'s own workflow verbatim refused half its steps: the guide
# said "Add pump, valve, cylinder, reservoir, etc." and the schema's frozen enum rejects
# `cylinder`, `motor`, `check_valve`, `relief_valve`; and hydraulic_fea_handoff(axis="z")
# died in JSON parsing with "could not parse inputs" for a word every other sim tool takes.


@pytest.mark.parametrize("supplied,canon", [
    ("cylinder", "cylinder_linear"),
    ("linear_cylinder", "cylinder_linear"),
    ("actuator", "cylinder_linear"),
    ("rotary_actuator", "cylinder_rotary"),
    ("motor", "motor_hydraulic"),
    ("check_valve", "valve_check"),
    ("relief_valve", "valve_relief"),
    ("directional_valve", "valve_directional"),
    ("flow_control_valve", "valve_flow_control"),
    ("tank", "reservoir"),
    ("heat exchanger", "heat_exchanger"),
])
def test_component_type_spellings_fold_onto_the_frozen_enum(supplied, canon):
    got, note = ht._resolve_component_type(supplied)
    assert got == canon, (supplied, got)
    assert note and supplied in note and canon in note, note


def test_a_canonical_component_type_is_returned_unchanged_with_no_note():
    """Identity for the shape that already worked — a note on a no-op would be noise."""
    for t in ("pump", "cylinder_linear", "valve_relief", "reservoir"):
        got, note = ht._resolve_component_type(t)
        assert (got, note) == (t, None), (t, got, note)


def test_a_bare_valve_stays_refused_because_four_valve_types_exist():
    """Guessing which valve a circuit needs would change what the circuit DOES, so this is
    the one generic noun that must NOT be mapped."""
    with pytest.raises(ValueError) as exc:
        ht._resolve_component_type("valve")
    msg = str(exc.value)
    assert "ambiguous" in msg
    for member in ("valve_directional", "valve_check", "valve_relief", "valve_flow_control"):
        assert member in msg, msg


def test_an_unknown_component_type_is_passed_through_for_the_core_to_reject():
    """The enum lives in the core; the normalizer only fixes SPELLINGS and must not start
    inventing its own vocabulary errors."""
    assert ht._resolve_component_type("flux_capacitor") == ("flux_capacitor", None)


@pytest.mark.parametrize("word,vec", [
    ("z", [0.0, 0.0, 1.0]), ("+z", [0.0, 0.0, 1.0]), ("-z", [0.0, 0.0, -1.0]),
    ("x", [1.0, 0.0, 0.0]), ("-x", [-1.0, 0.0, 0.0]), ("Y", [0.0, 1.0, 0.0]),
    ("up", [0.0, 0.0, 1.0]), ("down", [0.0, 0.0, -1.0]),
])
def test_axis_words_resolve_to_the_z_up_vector(word, vec):
    assert ht._axis_arg(word) == vec


def test_an_explicit_axis_vector_is_untouched_and_json_still_parses():
    assert ht._axis_arg([0, 1, 0]) == [0, 1, 0]
    assert ht._axis_arg("[0, 1, 0]") == [0, 1, 0]
    assert ht._axis_arg(None) is None


def test_a_word_axis_reaches_the_mount_force_math_unchanged():
    """The point of the fix: axis="z" must give the same envelope as axis=[0,0,1], and the
    extend force must be the hand-calc pi/4 d^2 P (50 mm bore @ 200 bar -> 39269.9 N)."""
    def _extend(axis):
        env = ht._fea_handoff_envelope(bore_mm=50.0, rod_mm=28.0, max_pressure_bar=200.0,
                                       axis=axis, efficiency=1.0,
                                       load_face="the mount", fixture_face="the base")
        assert sh.validate_envelope(env) == []
        return next(c for c in env["conditions"]
                    if c.get("case") == "extend_max_pressure")["value"]

    word, vec = _extend(ht._axis_arg("z")), _extend([0.0, 0.0, 1.0])
    assert word == vec, (word, vec)
    assert word[2] == pytest.approx(-math.pi / 4.0 * 0.05 ** 2 * 200e5, rel=1e-4)


# ------------------------------- unaddressable piping nodes (measured 2026-07-30)
# Node ids are DERIVED by snapping run endpoints and published only by a successful solve's
# node_menu, so a first boundary can only guess one — and the refusal named the alternative
# shapes without naming a single addressable node.

def _one_run_doc():
    return {"schema": "aicad.piping/1", "id": "d", "name": "skid",
            "units": {"system": "MMGS"},
            "runs": [{"id": "r1", "path": [[0, 0, 0], [3000, 0, 0], [3000, 2000, 0]],
                      "nps": 2, "schedule": "40", "end_type": "BW"}],
            "fittings": []}


def test_a_node_resolution_refusal_now_publishes_the_run_endpoints():
    res = pt._with_run_endpoints(
        {"ok": False, "reason": "boundary reference {'node': 'n2'} did not resolve to a node",
         "hint": "use {'node':'n0'}"}, _one_run_doc())
    assert res["addressable_endpoints"] == [
        {"run": "r1", "end": "start", "xyz": [0, 0, 0]},
        {"run": "r1", "end": "end", "xyz": [3000, 2000, 0]}]
    assert "Node ids only exist after a solve" in res["hint"]
    assert "use {'node':'n0'}" in res["hint"]  # the core's own hint is kept


def test_a_suppressed_or_degenerate_run_is_not_offered_as_addressable():
    doc = _one_run_doc()
    doc["runs"].append({"id": "r2", "suppressed": True, "path": [[0, 0, 0], [1, 0, 0]]})
    doc["runs"].append({"id": "r3", "path": [[9, 9, 9]]})  # <2 points: no endpoints
    res = pt._with_run_endpoints(
        {"ok": False, "reason": "did not resolve to a node"}, doc)
    assert {e["run"] for e in res["addressable_endpoints"]} == {"r1"}


def test_a_successful_solve_and_an_unrelated_failure_are_left_alone():
    """The enrichment must be scoped to the one refusal it explains — otherwise it becomes
    noise on every reply."""
    ok = {"ok": True, "links": []}
    assert pt._with_run_endpoints(dict(ok), _one_run_doc()) == ok
    other = {"ok": False, "reason": "piping document failed schema validation"}
    assert pt._with_run_endpoints(dict(other), _one_run_doc()) == other
    assert pt._with_run_endpoints({"ok": False, "reason": "did not resolve to a node"},
                                  None) == {"ok": False,
                                            "reason": "did not resolve to a node"}

"""ECAD verification/physics pack regression tests on ONE reference board.

A small MCU-ish reference board is hand-built through the ecad session layer (the same
fixture style as test_ecad_tools.py's board tests): 40x30 mm closed outline, a +5V power
net (connector J1 -> regulator U1 -> MCU U2 over a 1.0 mm trace), a GND zone, a 50-ohm
candidate CLK trace, an edge-coupled CLK_P/CLK_N pair and a deliberately short SHORTNET
for the length-match rule. Every pre-fab / physics pack is then driven against it:

  ecad_run_pdn            IR drop vs the hand-computed trace resistance (Pouillet's law)
  ecad_run_sicheck        length-match group deltas (hand-checked medians)
  ecad_check_impedance    Z0 vs the IPC-2141A surface-microstrip closed form
  ecad_check_diff_impedance  Z_diff vs the IPC/National edge-coupling de-rating formula
                          + the reversed-track-digitisation regression (gap inference)
  ecad_run_hostcheck      clean board + seeded sequencing finding
  ecad_fpga_check         iCE40 config-pin violation + empty-sheet guard
  ecad_stackup_solve      solved geometry reads BACK at target through ecad_check_impedance
  ecad_thermal_handoff / ecad_verify_thermal   dry-run envelope + live coarse solve
  heatsink_select         re-solve with library candidates after the thermal handoff
  ecad_airflow_handoff / ecad_verify_airflow   dry-run envelope; verify teaches w/o a solve
  ecad_vibe_check         dry-run mass model + mount-hole fixtures + no-holes guard
  power_budget            battery life vs the hand calc life_h = capacity_ah / i_avg_a

Physics/3D packs skip cleanly when build123d/OCP is unavailable.

Run: CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_ecad_packs.py -q
"""
from __future__ import annotations

import json
import math

import pytest

from services.mcp.ecad_tools import register_ecad_tools, _ECAD
from services.mcp.ecad_thermal import (register_ecad_thermal_tools,
                                     register_ecad_airflow_tools)
from services.mcp.ecad_reliability import register_ecad_reliability_tools
from services.mcp.heatsink import register_heatsink_tools
from services.mcp.power_budget import register_power_budget_tools
from services.mcp.ecad_common import ecad_session


def _has_3d() -> bool:
    try:
        import build123d  # noqa: F401
        return True
    except ImportError:
        return False


needs_3d = pytest.mark.skipif(not _has_3d(), reason="build123d/OCP not installed")


# ---------------------------------------------------------------------------- stub mcp

class _StubMCP:
    """Collects @mcp.tool()-decorated functions by name (no FastMCP needed)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _call(_tools: dict, _name: str, **kw) -> dict:
    out = _tools[_name](**kw)
    assert isinstance(out, str), f"{_name} must return a JSON string, got {type(out)}"
    return json.loads(out)


def _assert_context(payload: dict, name: str) -> None:
    """Rich-context contract: every payload teaches (hint + next_actions + warnings)."""
    assert "hint" in payload and payload["hint"], f"{name}: missing/empty hint"
    assert isinstance(payload.get("next_actions"), list), f"{name}: next_actions not a list"
    assert "warnings" in payload, f"{name}: missing warnings list"


# ---------------------------------------------------------------------------- reference board

# Rail metadata for the two active parts (aicad.boardphys/1 shapes).
REG_ENG = {"power_w": {"typ": 2.0, "max": 3.0},
           "theta_c_per_w": {"ja": 40.0, "jc": 5.0},
           "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
           "supply": {"rails": [{"name": "+5V", "voltage_v_min": 4.75,
                                 "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                 "current_a_active": 1.0, "current_a_sleep": None}],
                      "voltage_domains": ["+5V"], "sequencing": []},
           "sources": ["test fixture: 2 W regulator"]}
MCU_ENG = {"power_w": {"typ": 0.3, "max": 0.5},
           "theta_c_per_w": {"ja": 60.0, "jc": None},
           "limits_c": {"tj_max": 105.0, "ta_max": 85.0},
           "supply": {"rails": [{"name": "+5V", "voltage_v_min": 4.75,
                                 "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                 "current_a_active": 1.0, "current_a_sleep": 1e-5}],
                      "voltage_domains": ["+5V"], "sequencing": []},
           "sources": ["test fixture: MCU"]}

# +5V trace geometry (the PDN hand-calc leans on these — keep in sync with asserts).
PWR_TRACE_W_MM = 1.0
J1_X, U1_X, U2_X, PWR_Y = 5.0, 15.0, 35.0, 15.0


def _pad(net: str, n: str = "1", size: float = 1.0) -> dict:
    return {"id": f"p{n}", "number": n, "pin": n, "pos": [0.0, 0.0], "angle": 0.0,
            "shape": "rect", "size": [size, size], "type": "smd",
            "layers": ["F.Cu"], "net": net}


def build_reference_board(tools: dict, name: str = "ecad_packs_ref") -> str:
    """Hand-build the reference MCU board on a fresh pcb doc; returns the doc_id.

    Also imported by benchmarks/simsuite/ecad_packs_bench.py — keep it dependency-light.
    """
    _call(tools, "ecad_new_pcb", name=name)
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc

    pts = [(0, 0), (40, 0), (40, 30), (0, 30)]
    doc["outline"] = {"closed": True, "shapes": [
        {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4])}
        for i in range(4)]}

    for net in ("+5V", "GND", "CLK", "CLK_P", "CLK_N", "SHORTNET"):
        doc["nets"].append({"name": net, "class": "default"})

    doc["footprints"].extend([
        {"id": "fp1", "ref": "J1", "fpid": "Test:PAD1", "pos": [J1_X, PWR_Y],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V")]},
        {"id": "fp2", "ref": "U1", "fpid": "Test:REG", "pos": [U1_X, PWR_Y],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V", size=4.0)],
         "fields": {"height": 4.5}, "eng_overrides": REG_ENG},
        {"id": "fp3", "ref": "U2", "fpid": "Test:MCU", "pos": [U2_X, PWR_Y],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V")],
         "fields": {"height": 1.6}, "eng_overrides": MCU_ENG},
        {"id": "fp4", "ref": "C1", "fpid": "Test:C", "pos": [20.0, 5.0],
         "angle": 0.0, "side": "top", "pads": [_pad("GND")]},
    ])

    # +5V power trace J1 -> U1 -> U2, one straight 30 mm x 1.0 mm segment.
    doc["tracks"].append({"id": "t1", "kind": "segment", "p1": [J1_X, PWR_Y],
                          "p2": [U2_X, PWR_Y], "width": PWR_TRACE_W_MM,
                          "net": "+5V", "layer": "F.Cu"})
    # 20 mm x 0.3 mm CLK trace (controlled-impedance candidate).
    doc["tracks"].append({"id": "t2", "kind": "segment", "p1": [5.0, 25.0],
                          "p2": [25.0, 25.0], "width": 0.3, "net": "CLK",
                          "layer": "F.Cu"})
    # Edge-coupled pair: 0.25 mm wide, centres 0.45 mm apart -> 0.20 mm gap.
    doc["tracks"].append({"id": "t3", "kind": "segment", "p1": [5.0, 27.0],
                          "p2": [25.0, 27.0], "width": 0.25, "net": "CLK_P",
                          "layer": "F.Cu"})
    doc["tracks"].append({"id": "t4", "kind": "segment", "p1": [5.0, 27.45],
                          "p2": [25.0, 27.45], "width": 0.25, "net": "CLK_N",
                          "layer": "F.Cu"})
    # Deliberately short net for the length-match rule (5 mm vs CLK's 20 mm).
    doc["tracks"].append({"id": "t5", "kind": "segment", "p1": [5.0, 29.0],
                          "p2": [10.0, 29.0], "width": 0.3, "net": "SHORTNET",
                          "layer": "F.Cu"})

    # The doc is a client copy of the C++ session doc — write the fixture back before
    # the tools read board state (outline for the zone polygon, copper for PDN/SI checks).
    _sess = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(_sess, doc)
    doc_id = _sess.doc_id

    # GND zone filled over the board (the ground pour the packs expect to see).
    z = _call(tools, "ecad_add_zone", net="GND", doc_id=doc_id)
    assert z["ok"], f"add_zone failed: {z.get('error')}"
    fz = _call(tools, "ecad_fill_zones", doc_id=doc_id)
    assert fz["ok"], f"fill_zones failed: {fz.get('error')}"
    return doc_id


def register_pack_tools() -> dict:
    """All pack tools on ONE stub (thermal/heatsink share ecad session state)."""
    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_ecad_airflow_tools(stub)
    register_ecad_reliability_tools(stub)
    register_heatsink_tools(stub)
    register_power_budget_tools(stub)
    return stub.tools


# ---------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def tools() -> dict:
    return register_pack_tools()


@pytest.fixture(scope="module")
def board(tools) -> str:
    return build_reference_board(tools)


# ---------------------------------------------------------------------------- registration

def test_all_pack_tools_registered(tools):
    expected = {"ecad_run_pdn", "ecad_run_sicheck", "ecad_set_net_class",
                "ecad_check_impedance", "ecad_check_diff_impedance",
                "ecad_run_hostcheck", "ecad_fpga_check", "ecad_stackup_solve",
                "ecad_thermal_handoff", "ecad_verify_thermal",
                "ecad_airflow_handoff", "ecad_verify_airflow",
                "ecad_vibe_check", "heatsink_select", "power_budget"}
    missing = expected - set(tools)
    assert not missing, f"pack tools not registered: {missing}"


# ---------------------------------------------------------------------------- PDN

def test_pdn_ir_drop_matches_trace_resistance(tools, board):
    """IR drop vs hand calc. Pouillet's law: R = rho * L / (w * t) per trace segment;
    superposed drops: J1->U1 carries I_U1+I_U2 = 2 A over 10 mm, U1->U2 carries 1 A over
    20 mm, so drop(U2) = 2A*R(10mm) + 1A*R(20mm) and drop(U1) = 2A*R(10mm).
    rho and t come from the SAME constants the solver uses (engine.ecad.pdn), so the
    comparison isolates the rasterised network solve, not the material data."""
    from services.ecad.pdn import RHO_CU_OHM_M, DEFAULT_COPPER_T_MM

    r = _call(tools, "ecad_run_pdn", net="+5V", source_refs="J1", doc_id=board)
    _assert_context(r, "ecad_run_pdn")
    assert r["ok"], f"pdn failed: {r.get('error')}"
    net = r["nets"][0]
    assert net["vsrc"] == 5.0, "vsrc must come from the rail metadata"
    drops = {d["ref"]: d for d in net["drops"]}
    assert drops["U1"]["i_a"] == 1.0 and drops["U2"]["i_a"] == 1.0

    # R per mm of the 1.0 mm x 35 um trace (Pouillet: R = rho*L/(w*t)).
    r_per_mm = RHO_CU_OHM_M * 1e-3 / (PWR_TRACE_W_MM * 1e-3 * DEFAULT_COPPER_T_MM * 1e-3)
    drop_u1_ref = 2.0 * (U1_X - J1_X) * r_per_mm            # 2 A over 10 mm
    drop_u2_ref = drop_u1_ref + 1.0 * (U2_X - U1_X) * r_per_mm  # + 1 A over 20 mm
    for ref, want in (("U1", drop_u1_ref), ("U2", drop_u2_ref)):
        got = drops[ref]["drop_v"]
        rel = abs(got - want) / want
        # 25% band: pads are finite (1 mm source pad, 4 mm U1 tab) and the solve is a
        # rasterised 2.5D grid, so a ~10% shortening vs the centreline calc is expected.
        assert rel < 0.25, f"{ref}: drop {got*1e3:.2f} mV vs hand {want*1e3:.2f} mV (rel {rel:.2f})"
        assert drops[ref]["within_tolerance"] is True, drops[ref]

    # 2 A through 1.0 mm x 35 um copper -> J = I/(w*t) = 57 A/mm2 > the 30 A/mm2
    # default ceiling: the current-density violation MUST fire.
    j_hand = 2.0 / (PWR_TRACE_W_MM * DEFAULT_COPPER_T_MM)
    assert j_hand > net["j_limit_a_mm2"]
    assert net["hotspots"], "57 A/mm2 must produce hotspots"
    assert any("current density" in v for v in r["violations"]), r["violations"]
    assert 0.5 * j_hand < net["max_j_a_mm2"] < 1.5 * j_hand, \
        f"max J {net['max_j_a_mm2']} vs hand {j_hand:.1f} A/mm2"


def test_pdn_all_failed_nets_is_a_real_error(tools, board):
    """Regression: a run where EVERY requested net fails must return an 'error' (it used
    to return ok=False with error=None and a 'PDN clean on 0 net(s)' verdict)."""
    r = _call(tools, "ecad_run_pdn", net="NO_SUCH_NET", doc_id=board)
    assert not r["ok"]
    assert r.get("error"), "all-failed PDN run must carry a top-level error"
    assert "NO_SUCH_NET" in r["error"]
    assert r.get("failed_nets") and r["failed_nets"][0]["net"] == "NO_SUCH_NET"
    _assert_context(r, "ecad_run_pdn(all-failed)")


# ---------------------------------------------------------------------------- SI check

def test_sicheck_length_match_group(tools, board):
    """CLK (20 mm) + SHORTNET (5 mm) in one 0.5 mm match group: median 12.5 mm, both
    nets +/-7.5 mm out -> two si_match_length findings with those exact deltas."""
    r = _call(tools, "ecad_set_net_class", name="clk_grp",
              params={"match_tolerance_mm": 0.5}, nets=["CLK", "SHORTNET"],
              doc_id=board)
    assert r["ok"], r.get("error")
    r = _call(tools, "ecad_run_sicheck", doc_id=board)
    _assert_context(r, "ecad_run_sicheck")
    assert r["ok"]
    assert "clk_grp" in r["si_classes"]
    assert r["limitations"], "sicheck must state its honest limits"
    grp = r["match_report"]["clk_grp"]
    # median of {20, 5} = 12.5 mm; deltas exactly +/-7.5 mm
    assert abs(grp["reference_mm"] - 12.5) < 1e-9
    assert abs(grp["nets"]["CLK"]["delta_mm"] - 7.5) < 1e-9
    assert abs(grp["nets"]["SHORTNET"]["delta_mm"] + 7.5) < 1e-9
    assert not grp["nets"]["CLK"]["ok"] and not grp["nets"]["SHORTNET"]["ok"]
    rules = {g["rule"] for g in r["by_rule"]}
    assert "si_match_length" in rules, r["by_rule"]


# ---------------------------------------------------------------------------- impedance

def test_check_impedance_matches_ipc2141a(tools, board):
    """Tool Z0 must equal the IPC-2141A surface-microstrip closed form
    Z0 = 87/sqrt(er+1.41) * ln(5.98*h / (0.8*w + t)) evaluated on the geometry the
    tool itself reports (w/h/t/er) — a formula-level differential check."""
    r = _call(tools, "ecad_check_impedance", net_or_ref="CLK", target=50.0, doc_id=board)
    _assert_context(r, "ecad_check_impedance")
    assert r["ok"] and r["kind"] == "microstrip"
    z0_ref = 87.0 / math.sqrt(r["er"] + 1.41) * math.log(
        5.98 * r["h"] / (0.8 * r["width"] + r["t"]))
    assert abs(r["z0"] - z0_ref) / z0_ref < 1e-3, f"{r['z0']} vs IPC {z0_ref}"
    # 0.3 mm over a 1.53 mm core is way above 50 ohm -> must be flagged.
    assert r["within_tolerance"] is False and r["pct_error"] > 10.0
    assert abs(r["length_mm"] - 20.0) < 1e-6
    # delay sanity: eeff between 1 and er -> 3.34..3.34*sqrt(er) ps/mm (delay = sqrt(eeff)/c)
    assert 3.3 < r["delay_ps_per_mm"] < 3.34 * math.sqrt(r["er"]) + 0.1


def test_check_impedance_scan_mode(tools, board):
    """No net given -> whole-board scan summarising every routed net."""
    r = _call(tools, "ecad_check_impedance", target=50.0, doc_id=board)
    assert r["ok"] and r["nets_analysed"] >= 4  # +5V, CLK, CLK_P/N, SHORTNET
    assert r["out_of_tolerance"] >= 1 and r["worst"], r


def test_check_diff_impedance_matches_coupling_formula(tools, board):
    """Z_diff must equal 2*Z0_se*(1 - 0.48*exp(-0.96*s/h)) (IPC/National edge-coupled
    microstrip de-rating) on the reported gap/height, and always < 2*Z0_se."""
    r = _call(tools, "ecad_check_diff_impedance", net_p="CLK_P", net_n="CLK_N",
              target=100.0, doc_id=board)
    _assert_context(r, "ecad_check_diff_impedance")
    assert r["ok"] and r["kind"] == "microstrip"
    # geometric gap: centres 0.45 mm apart, both 0.25 mm wide -> 0.20 mm edge gap
    assert abs(r["gap_mm"] - 0.20) < 1e-6, r["gap_mm"]
    zd_ref = 2.0 * r["z0_se"] * (1.0 - 0.48 * math.exp(-0.96 * r["gap_mm"] / r["h"]))
    assert abs(r["z_diff"] - zd_ref) / zd_ref < 1e-3, f"{r['z_diff']} vs {zd_ref}"
    assert r["z_diff"] < 2.0 * r["z0_se"], "coupling must LOWER Z_diff below 2*Z0_se"
    assert abs(r["skew_mm"]) < 1e-9, "equal-length pair must report zero skew"


def test_check_diff_impedance_reversed_digitisation(tools):
    """Regression for the gap-inference fix: the SAME pair with the N track digitised
    in the reverse direction (p1<->p2 swapped) must read the same gap and Z_diff —
    it used to infer a ~20 mm 'gap' (the track length) and blow up Z_diff."""
    tools2 = tools  # same registry; fresh board so the reference fixture stays pristine
    _call(tools2, "ecad_new_pcb", name="ecad_packs_revpair")
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc
    for net in ("FWD_P", "FWD_N", "REV_P", "REV_N"):
        doc["nets"].append({"name": net, "class": "default"})
    doc["tracks"] += [
        {"id": "t1", "kind": "segment", "p1": [5.0, 10.0], "p2": [25.0, 10.0],
         "width": 0.25, "net": "FWD_P", "layer": "F.Cu"},
        {"id": "t2", "kind": "segment", "p1": [5.0, 10.45], "p2": [25.0, 10.45],
         "width": 0.25, "net": "FWD_N", "layer": "F.Cu"},
        {"id": "t3", "kind": "segment", "p1": [5.0, 20.0], "p2": [25.0, 20.0],
         "width": 0.25, "net": "REV_P", "layer": "F.Cu"},
        # identical geometry, reversed digitisation direction
        {"id": "t4", "kind": "segment", "p1": [25.0, 20.45], "p2": [5.0, 20.45],
         "width": 0.25, "net": "REV_N", "layer": "F.Cu"},
    ]
    _s = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(_s, doc)  # write the client-copy fixture back
    doc_id = _s.doc_id
    fwd = _call(tools2, "ecad_check_diff_impedance", net_p="FWD_P", net_n="FWD_N",
                doc_id=doc_id)
    rev = _call(tools2, "ecad_check_diff_impedance", net_p="REV_P", net_n="REV_N",
                doc_id=doc_id)
    assert fwd["ok"] and rev["ok"]
    assert abs(fwd["gap_mm"] - rev["gap_mm"]) < 1e-9, (fwd["gap_mm"], rev["gap_mm"])
    assert abs(fwd["z_diff"] - rev["z_diff"]) < 1e-6, (fwd["z_diff"], rev["z_diff"])


# ---------------------------------------------------------------------------- hostcheck

def test_hostcheck_reference_board_clean(tools, board):
    r = _call(tools, "ecad_run_hostcheck", doc_id=board)
    _assert_context(r, "ecad_run_hostcheck")
    assert r["ok"] and r["errors"] == 0, r


def test_hostcheck_seeded_sequencing_finding(tools):
    """A rail whose metadata demands 'VCORE before VIO' with nobody sourcing VIO must
    raise host_sequencing_unverifiable with a fix hint (metadata-driven, no part DB)."""
    _call(tools, "ecad_new_pcb", name="ecad_packs_seq")
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc
    # C++ schema validation (core/ecad/schema.cpp): a pad net must exist in board nets.
    doc["nets"].append({"name": "3V3", "class": "default"})
    doc["footprints"].append({
        "id": "fp1", "fpid": "Test:FP", "libid": "", "ref": "U1", "value": "MCU",
        "pos": [0.0, 0.0], "angle": 0.0, "side": "top", "graphics": [],
        "model_3d": None, "attr": "smd", "linked_symbol": None,
        "pads": [_pad("3V3")],
        "eng_overrides": {"supply": {"rails": [{"name": "3V3", "voltage_v_nom": 3.3}],
                                     "sequencing": ["VCORE before VIO"]}},
    })
    ecad_session.swap_doc(ecad_session.get_session(doc_id), doc)  # write-back
    r = _call(tools, "ecad_run_hostcheck", doc_id=doc_id)
    assert r["ok"], r
    rules = {g["rule"] for g in r["by_rule"]}
    assert "host_sequencing_unverifiable" in rules, r["by_rule"]
    assert r["fix_hints"], "findings must carry actionable fix hints"


# ---------------------------------------------------------------------------- FPGA

def test_fpga_check_and_empty_sheet_guard(tools):
    s = _call(tools, "ecad_new_schematic", name="ecad_packs_fpga")
    sid = s["doc_id"]
    e = _call(tools, "ecad_fpga_check", doc_id=sid)
    assert not e["ok"] and "FPGA" in (e.get("error") or e.get("hint") or "")
    _assert_context(e, "ecad_fpga_check(empty)")
    p = _call(tools, "ecad_place_symbol", libid="FPGA:iCE40UP5K-SG48", x=100, y=100,
              doc_id=sid)
    assert p["ok"], p
    r = _call(tools, "ecad_fpga_check", doc_id=sid)
    _assert_context(r, "ecad_fpga_check")
    assert r["devices"] and r["devices"][0]["device"] == "iCE40UP5K-SG48"
    # bare FPGA: the required CRESET_B config pin is unwired -> config violation
    assert not r["ok"] and any(v["check"] == "config" for v in r["violations"]), r


# ---------------------------------------------------------------------------- stackup

def test_stackup_solve_roundtrips_through_impedance_check(tools):
    """Solve a 4-layer 50-ohm/100-ohm stackup, apply it, route a trace at the solved
    width, and confirm ecad_check_impedance reads it back within the +/-10% fab
    tolerance — an end-to-end forward/inverse oracle check of the shared IPC model."""
    _call(tools, "ecad_new_pcb", name="ecad_packs_stackup")
    doc_id = _ECAD["current_pcb"]
    r = _call(tools, "ecad_stackup_solve", copper_layers=4, doc_id=doc_id)
    _assert_context(r, "ecad_stackup_solve")
    assert r["ok"] and r["applied"], r
    geo = r["geometries"]["se_50"]
    w50 = geo["track_width"]
    assert w50 > 0.05, geo
    assert "se_50" in r["net_classes_written"], r["net_classes_written"]
    # the solved diff geometry must carry both width and gap
    dg = r["geometries"]["diff_100"]
    assert dg["type"] == "diff" and dg.get("diff_gap", dg["layers"][r["signal_layers"][0]].get("gap"))
    # route a track at the solved width and read it back
    doc = ecad_session.get_session(doc_id).doc
    doc["nets"].append({"name": "Z50", "class": "default"})
    doc["tracks"].append({"id": "t1", "kind": "segment", "p1": [5.0, 5.0],
                          "p2": [25.0, 5.0], "width": w50, "net": "Z50",
                          "layer": "F.Cu"})
    _s = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(_s, doc)  # write the client-copy fixture back
    doc_id = _s.doc_id
    chk = _call(tools, "ecad_check_impedance", net_or_ref="Z50", target=50.0,
                doc_id=doc_id)
    assert chk["ok"] and chk["within_tolerance"] is True, chk
    assert abs(chk["z0"] - 50.0) / 50.0 < 0.02, \
        f"solver/checker round trip drifted: {chk['z0']} ohm"


def test_stackup_solve_bad_profile_teaches(tools):
    r = _call(tools, "ecad_stackup_solve", copper_layers=4, profile="not_a_profile",
              apply=False)
    assert not r["ok"] and "profile" in r["error"]
    assert "jlc_4layer_7628" in (r.get("hint") or ""), r


# ---------------------------------------------------------------------------- thermal

@needs_3d
def test_thermal_handoff_dry_run_envelope(tools, board):
    r = _call(tools, "ecad_thermal_handoff", doc_id=board, dry_run=True)
    _assert_context(r, "ecad_thermal_handoff")
    assert r["ok"] and r["dry_run"] and not r["solved"]
    cov = r["coverage"]
    assert cov["n_heat_sources"] == 2, cov          # U1 + U2 have power_w
    assert sorted(cov["unpowered_refs"]) == ["C1", "J1"], cov  # honesty: no invented watts
    sh = r["simhandoff"]
    assert sh and sh.get("schema") == "aicad.simhandoff/1", sh
    assert r["assumptions"], "homogenised material assumptions must be listed"


@needs_3d
def test_thermal_live_solve_verify_and_heatsink(tools, board):
    """Coarse live solve: 2.3 W on a 40x30 board in still air. Lumped sanity bound:
    board rise >= P_total/(h*A_two_sides) = 2.3/(10 * 2*0.04*0.03) = 95.8 C above
    ambient for convection-only heat removal (Newton cooling Q = h*A*dT), so U1's
    Tj = Tcase + P*theta_jc must bust its 125 C limit -> verify FAILS by design.
    Then heatsink_select must find a library candidate meeting Tj <= 85 C."""
    r = _call(tools, "ecad_thermal_handoff", doc_id=board, mesh_fineness=0.35)
    assert r["ok"] and r["solved"], f"live solve failed: {r.get('error')}"
    comps = {c["ref"]: c for c in r["components"]}
    u1 = comps["U1"]
    assert u1["power_w"] == 2.0 and u1["theta_jc"] == 5.0
    assert u1["tj_c"] == pytest.approx(u1["t_case_c"] + 2.0 * 5.0, abs=1e-6), \
        "Tj must be Tcase + P*theta_jc"
    # convection-limited lower bound (see docstring): the board must sit well above
    # ambient; use a slack factor 0.5 for the coarse mesh + component surface area.
    t_amb = r["environment"]["t_amb_c"]
    assert u1["t_case_c"] - t_amb > 0.5 * 95.8, u1
    # honesty: U2 has no theta_jc -> tj_c must be null, never invented
    assert comps["U2"]["tj_c"] is None

    v = _call(tools, "ecad_verify_thermal")
    assert v["ok"] and v["pass"] is False, v.get("verdict")
    row = next(t for t in v["table"] if t["ref"] == "U1")
    assert row["pass"] is False
    assert row["margin_c"] == pytest.approx(row["tj_max_c"] - row["tj_used_c"], abs=0.01)

    h = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=85.0)
    assert h["ok"], h.get("error")
    assert h["table"], "candidate table must not be empty"
    assert h["target_met"] is True and h["winner"], h
    assert h["winner"]["achieved_tj_c"] <= 85.0 + 1e-6, h["winner"]
    # every FEA-resolved candidate must report a physical achieved temperature
    for row in h["table"]:
        if row.get("achieved_tj_c") is not None:
            assert row["achieved_tj_c"] > t_amb, row


def test_verify_thermal_without_solve_teaches(tools):
    """With no cached solve, verify must return a teaching error (the cache lives in
    the module-global _THERMAL, so save/clear/restore it around the check)."""
    from services.mcp import ecad_thermal as et  # same module identity as the tools (double-import hazard)
    saved = et._THERMAL.get("last_handoff")
    et._THERMAL["last_handoff"] = None
    try:
        r = _call(tools, "ecad_verify_thermal")
        assert not r["ok"] and "handoff" in r["error"], r
    finally:
        et._THERMAL["last_handoff"] = saved


# ---------------------------------------------------------------------------- airflow

@needs_3d
def test_airflow_handoff_dry_run_and_verify_guard(tools, board):
    r = _call(tools, "ecad_airflow_handoff", doc_id=board, dry_run=True)
    _assert_context(r, "ecad_airflow_handoff")
    assert r["ok"] and not r["solved"] and r["skipped"], r
    sh = r["simhandoff"]
    assert sh and sh.get("schema") == "aicad.simhandoff/1", sh
    # only POWERED components become solid regions (honesty rule): board + c_U1 + c_U2
    names = {s["name"] for s in r["solids"]}
    assert names == {"board", "c_U1", "c_U2"}, names
    powers = {s["name"]: s["power_w"] for s in r["solids"]}
    assert powers["c_U1"] == 2.0 and powers["c_U2"] == 0.3, powers
    from services.mcp import ecad_thermal as et  # same module identity as the tools (double-import hazard)
    saved = et._AIRFLOW.get("last")
    et._AIRFLOW["last"] = None
    try:
        v = _call(tools, "ecad_verify_airflow")
        assert not v["ok"] and "airflow" in v["error"], v
    finally:
        et._AIRFLOW["last"] = saved


# ---------------------------------------------------------------------------- vibration

@needs_3d
def test_vibe_check_dry_run_mass_model(tools, board):
    r = _call(tools, "ecad_vibe_check", doc_id=board, dry_run=True,
              mount_holes=[[3, 3], [37, 3], [37, 27], [3, 27]])
    _assert_context(r, "ecad_vibe_check")
    assert r["ok"] and not r["solved"], r
    assert len(r["mount_holes"]) == 4
    mm = r["mass_model"]
    # FR4 board 40x30x1.6 mm at ~1.85-2.6 g/cm3 (homogenised with copper): 1.92 cm3
    # -> ~3.5-5 g. A wildly different number means broken volume/density bookkeeping.
    assert 2.0 < mm["board_mass_g"] < 8.0, mm["board_mass_g"]
    refs = {c["ref"] for c in mm["component_masses"]}
    assert {"U1", "U2"} <= refs, mm
    assert r["simhandoff"].get("schema") == "aicad.simhandoff/1"
    assert r["assumptions"], "damping/PSD assumptions must be echoed"


@needs_3d
def test_vibe_check_no_mounts_teaches(tools):
    _call(tools, "ecad_new_pcb", name="ecad_packs_nomount")
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc
    pts = [(0, 0), (20, 0), (20, 20), (0, 20)]
    doc["outline"] = {"closed": True, "shapes": [
        {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4])}
        for i in range(4)]}
    r = _call(tools, "ecad_vibe_check", doc_id=doc_id, dry_run=True)
    assert not r["ok"] and "mount" in r["error"], r


# ---------------------------------------------------------------------------- power budget

def test_power_budget_battery_life_hand_calc(tools):
    """Degraded (no firmware profile) budget: both parts always-active at 1 A each on
    +5V fed straight from the battery. life_h = capacity_ah / i_avg_a = 2.0/2.0 = 1.0 h
    exactly (simple charge bookkeeping — the tool's own documented model)."""
    bp = {"schema": "aicad.boardphys/1", "board": {"name": "ecad_packs_ref"},
          "components": [{"ref": "U1", "supply": REG_ENG["supply"]},
                         {"ref": "U2", "supply": MCU_ENG["supply"]}]}
    r = _call(tools, "power_budget",
              source={"kind": "battery", "name": "+5V", "voltage_v": 5.0,
                      "capacity_mah": 2000.0},
              board=bp)
    assert r["ok"], r.get("error")
    assert r["degraded"] is True, "no firmware profile -> must be LABELLED degraded"
    b = r["battery"]
    assert b["i_source_avg_a"] == pytest.approx(2.0, abs=1e-9)
    assert b["life_h"] == pytest.approx(2.0 / 2.0, rel=1e-9)   # capacity_ah / i_avg
    assert r["verdict"] == "ok"
    rail = next(x for x in r["rails"] if x["name"] == "+5V")
    assert rail["i_avg_a"] == pytest.approx(2.0, abs=1e-9)
    assert all("degraded" in p for p in rail["provenance"]), rail["provenance"]

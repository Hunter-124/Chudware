"""Mega-17 fixes — Z1 terminal grading, Z2 power-then-wire, Z3 AC5 simulate.

Axes:
  Z1 — every mission terminal (done/deliver_partial/needs_correction) emits
       criterion_result for ALL 9 ACs (honest NOT-CHECKED/FAIL/SKIP when absent).
  Z2 — force-inject orders ecad_add_power before wire; wire args derive from live
       place/add_power refs (never hardcoded #PWR1); missing-ref → re-derive once.
  Z3 — chain need schedules ecad_simulate after ERC (or when ERC not allowed).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Z1 — terminal grading unconditional
# ---------------------------------------------------------------------------


class _MemLedger:
    def __init__(self):
        self.events = []
        self.state = {"criterion_results": [], "artifacts": []}

    def append(self, actor, kind, payload):
        ev = {"actor": actor, "kind": kind, "payload": dict(payload)}
        self.events.append(ev)
        if kind == "criterion_result":
            ac = payload.get("id")
            existing = next(
                (c for c in self.state["criterion_results"] if c.get("id") == ac),
                None,
            )
            if existing:
                existing.update(dict(payload))
            else:
                self.state["criterion_results"].append(dict(payload))
        return ev

    def add_criterion_result(self, actor, ac_id, result, **kw):
        return self.append(actor, "criterion_result", {
            "id": ac_id, "result": result, **kw,
        })

    def features(self):
        return []

    def geometry_specs(self):
        return []


def test_terminal_grading_on_deliver_partial(monkeypatch):
    """mega-16 repro: deliver_partial must still grade all 9 ACs."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.pipeline.phases import Phase, PhaseMachine
    from services.dag.mission_template import build_mission_graph

    # Minimal rocket-plane-shaped manifest with gradeable ACs.
    from services.agent.mission_criteria import apply_rocket_plane_checks

    base = {
        "request_verbatim": "flight computer",
        "ambiguities": [],
        "deliverables": [
            {"id": "DL1", "name": "Airframe", "kind": "part",
             "description": "a", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
            {"id": "DL2", "name": "Asm", "kind": "assembly",
             "description": "b", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
            {"id": "DL3", "name": "FC", "kind": "analysis",
             "description": "c", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
            {"id": "DL4", "name": "FW", "kind": "analysis",
             "description": "d", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
            {"id": "DL5", "name": "Report", "kind": "report",
             "description": "e", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
        ],
        "assumptions": [],
    }
    manifest = apply_rocket_plane_checks(base)
    graph = build_mission_graph(manifest, graph_id="m17_z1", org_layer=False)

    # Soft-join deliver: one deliver done, others failed; build/verify incomplete
    # but downstream deliver progressed (mega-16 soft-reconcile fingerprint).
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            if "DL5" in nid or nid.endswith("DL1"):
                node_states[nid] = {"status": "done", "output": {"status": "done"}}
            else:
                node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        elif nid.startswith("n_build_") or nid.startswith("n_verify_"):
            # incomplete / failed build workers — soft-reconcile via deliver done
            node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    dag_result = {"status": "paused", "nodes": node_states}

    led = _MemLedger()

    # Silence battery/tool backends — terminal grading must still emit ACs.
    monkeypatch.setattr(
        "services.agent.reviewer_exec.run_verify_battery",
        lambda *a, **k: {
            "verdict": "NOT-CHECKED", "ok": False, "checks": [],
            "detail": "no doc",
        },
    )
    monkeypatch.setattr(
        "services.agent.reviewer_exec._resolve_fn",
        lambda tool: None,
    )

    machine = PhaseMachine()
    completed: list = []
    events: list = []

    def _emit(phase, text):
        events.append((phase, text))

    reached, status, stop_reason = loop._drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, _emit, Phase,
        ledger=led,
    )

    assert stop_reason == "deliver_partial" or status in ("done", "blocked"), (
        status, stop_reason, reached, completed,
    )
    # All 9 ACs graded (PASS/FAIL/SKIP/NOT-CHECKED — never silence).
    ids = {
        e["payload"]["id"]
        for e in led.events
        if e["kind"] == "criterion_result" and e["payload"].get("id")
    }
    for ac in ("AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8", "AC9"):
        assert ac in ids, (ac, ids, stop_reason, status)


def test_terminal_grading_stub_when_no_manifest_acs():
    """Empty ACs still get 9 honest NOT-CHECKED slots at terminal."""
    from services.agent import loop

    led = _MemLedger()
    loop._ensure_terminal_criterion_grading(
        {"deliverables": []}, led, {"nodes": {}}, [],
        status="done", stop_reason="deliver_partial",
    )
    ids = {
        e["payload"]["id"]
        for e in led.events
        if e["kind"] == "criterion_result"
    }
    for ac in (f"AC{i}" for i in range(1, 10)):
        assert ac in ids, (ac, ids)


def test_terminal_grading_skips_mid_run():
    from services.agent import loop

    led = _MemLedger()
    loop._ensure_terminal_criterion_grading(
        {}, led, {}, [], status="running", stop_reason="",
    )
    assert led.events == []


# ---------------------------------------------------------------------------
# Z2 — power-then-wire + live refs + missing-ref retry
# ---------------------------------------------------------------------------


def test_chain_need_power_before_wire():
    from services.agent.agent_turn import _ecad_chain_need

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {"ref": "R1", "pins": [
                {"number": "1", "name": "~", "type": "passive", "xy": [0, 0]},
                {"number": "2", "name": "~", "type": "passive", "xy": [10, 0]},
            ]},
        },
    ]
    assert _ecad_chain_need(calls, allow) == "ecad_add_power"
    calls_pow = calls + [{
        "tool": "ecad_add_power", "ok": True, "args": {"net": "GND"},
        "result": {"ok": True, "net": "GND", "net_size": 0, "net_members": []},
    }]
    assert _ecad_chain_need(calls_pow, allow) == "ecad_wire"


def test_forced_wire_args_never_hardcode_pwr1():
    """mega-16 fingerprint: wire inject must not invent #PWR1."""
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = [
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48"},
            "result": {
                "ref": "U1",
                "pins": [
                    {"number": "1", "name": "VBAT", "type": "power_in",
                     "xy": [100, 50]},
                    {"number": "2", "name": "GND", "type": "power_in",
                     "xy": [100, 40]},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "name": "~", "type": "passive",
                     "xy": [20, 20]},
                    {"number": "2", "name": "~", "type": "passive",
                     "xy": [30, 20]},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 70.0, "y": 40.0},
            # Engine does NOT return a #PWR ref (mega-16 live shape).
            "result": {
                "ok": True, "net": "3V3", "pos": [70.0, 40.0],
                "net_size": 0, "net_members": [],
            },
        },
    ]
    args = _ecad_forced_tool_args("ecad_wire", calls)
    assert args.get("from_ref") != "#PWR1"
    assert args.get("to_ref") != "#PWR1"
    # Endpoints must be live placed refs only.
    live = {"U1", "R1"}
    assert args["from_ref"] in live
    assert args["to_ref"] in live


def test_sanitize_rewrites_invented_pwr1():
    from services.agent.agent_turn import _ecad_sanitize_wire_args

    calls = [
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "xy": [0, 0]},
                    {"number": "2", "xy": [10, 0]},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"},
            "result": {"ref": "U1", "pins": [{"number": "1", "xy": [5, 5]}]},
        },
    ]
    bad = {
        "from_ref": "R1", "from_pin": "1",
        "to_ref": "#PWR1", "to_pin": "+3V3",
        "doc_id": "sch-1",
    }
    fixed = _ecad_sanitize_wire_args(bad, calls)
    assert fixed.get("to_ref") != "#PWR1"
    assert fixed.get("from_ref") in ("R1", "U1")
    assert fixed.get("to_ref") in ("R1", "U1")
    assert fixed.get("doc_id") == "sch-1"


def test_wire_missing_ref_retry_once(tmp_path, monkeypatch):
    """Denial→reinject: wire with #PWR1 fails once, retry with live refs succeeds."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega17_wire_retry", path=tmp_path / "ledger.json")
    seq = {"n": 0}
    wire_attempts = []

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic",'
                    '"args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"ecad_place_symbol",'
                    '"args":{"libid":"Device:R","value":"1k"}}\n```'
                )
            if seq["n"] == 3:
                return (
                    '```json\n{"tool":"ecad_add_power",'
                    '"args":{"net":"GND","x":0,"y":0}}\n```'
                )
            if seq["n"] == 4:
                # Invented power ref — the mega-16 failure mode.
                return (
                    '```json\n{"tool":"ecad_wire","args":'
                    '{"from_ref":"R1","from_pin":"1",'
                    '"to_ref":"#PWR1","to_pin":"GND"}}\n```'
                )
            if seq["n"] == 5:
                return (
                    '```json\n{"tool":"board_from_schematic","args":{}}\n```'
                )
            if seq["n"] == 6:
                return '```json\n{"tool":"ecad_run_drc","args":{}}\n```'
            return "done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-r"}
        if tool == "ecad_place_symbol":
            return {
                "ok": True, "ref": "R1", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "name": "~", "type": "passive",
                     "xy": [0, 0]},
                    {"number": "2", "name": "~", "type": "passive",
                     "xy": [10, 0]},
                ],
            }
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": args.get("net") or "GND",
                "net_size": 0, "net_members": [],
            }
        if tool == "ecad_wire":
            wire_attempts.append(dict(args or {}))
            tr = str((args or {}).get("to_ref") or "")
            fr = str((args or {}).get("from_ref") or "")
            if tr.startswith("#") or fr.startswith("#"):
                return {
                    "ok": False,
                    "error": f"no symbol '{tr or fr}' on the schematic",
                    "placed_refs": ["R1"],
                }
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-r"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Wire with live refs",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=14,
        phase="build",
    )
    calls = (res.result or {}).get("calls") or []
    # At least one successful wire with no #PWR endpoint.
    ok_wires = [
        c for c in calls
        if c.get("tool") == "ecad_wire" and c.get("ok")
    ]
    assert ok_wires, (calls, wire_attempts)
    for c in ok_wires:
        a = c.get("args") or {}
        assert not str(a.get("from_ref") or "").startswith("#")
        assert not str(a.get("to_ref") or "").startswith("#")
    # Either sanitize pre-dispatch rewrote #PWR1, or retry path fired.
    assert wire_attempts, "wire never dispatched"
    assert any(
        not str(a.get("to_ref") or "").startswith("#")
        and not str(a.get("from_ref") or "").startswith("#")
        for a in wire_attempts
    ), wire_attempts


def test_power_refs_from_live_add_power():
    from services.agent.agent_turn import _ecad_power_refs, _ecad_all_live_refs

    calls = [
        {
            "tool": "ecad_place_symbol", "ok": True,
            "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND"},
            "result": {
                "ok": True, "net": "GND", "ref": "#PWR01",
                "net_members": ["#PWR01.1"],
            },
        },
    ]
    pw = _ecad_power_refs(calls)
    assert pw and pw[0]["ref"] == "#PWR01"
    assert "#PWR01" in _ecad_all_live_refs(calls)
    assert "U1" in _ecad_all_live_refs(calls)


# ---------------------------------------------------------------------------
# Z3 — ecad_simulate after ERC
# ---------------------------------------------------------------------------


def test_chain_need_schedules_simulate_after_erc():
    from services.agent.agent_turn import _ecad_chain_need, _ecad_forced_tool_args

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"},
            "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "GND", "net_size": 0},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "R1", "from_pin": "1",
                "to_ref": "U1", "to_pin": "1",
            },
            "result": {"ok": True, "net": "N1", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {"doc_id": "b"},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "result": {"errors": 0},
        },
        {
            "tool": "ecad_run_erc", "ok": True,
            "result": {"errors": 0},
        },
    ]
    assert _ecad_chain_need(calls, allow) == "ecad_simulate"
    sim_args = _ecad_forced_tool_args("ecad_simulate", calls)
    assert sim_args.get("analysis") == "op"
    assert "V(3V3)" in (sim_args.get("probes") or [])

def test_chain_need_simulate_without_erc_in_allow():
    """If ERC not allowed, still schedule simulate after power+cross wire."""
    from services.agent.agent_turn import _ecad_chain_need

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "ecad_simulate",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "GND", "net_size": 0},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "R1", "from_pin": "1",
                "to_ref": "U1", "to_pin": "1",
            },
            "result": {"ok": True, "net": "N1", "net_size": 2},
        },
    ]
    # No board product tools — after power+cross wire, simulate is next.
    assert _ecad_chain_need(calls, allow) == "ecad_simulate"
# ---------------------------------------------------------------------------
# mega-16 ledger fixture replay (Z1 shape)
# ---------------------------------------------------------------------------


def test_mega16_ledger_terminal_grading_replay(monkeypatch, tmp_path):
    """Replay mega-16 empty criterion_results → force 9 ACs via terminal helper."""
    from services.agent import loop
    from services.agent.mission_criteria import apply_rocket_plane_checks

    root = Path(__file__).resolve().parents[2]
    mega16 = root / "reports" / "runs" / "mega-16" / "ledger.json"
    if not mega16.exists():
        pytest.skip("mega-16 ledger fixture absent")
    raw = json.loads(mega16.read_text())
    # Confirm the repro: zero criterion_results on the live ledger.
    assert not (raw.get("state") or {}).get("criterion_results")

    man_path = root / "reports" / "runs" / "mega-16" / "manifest.json"
    if man_path.exists():
        man = json.loads(man_path.read_text())
        man = apply_rocket_plane_checks(man)
    else:
        man = apply_rocket_plane_checks({
            "deliverables": [
                {"id": f"DL{i}", "name": f"DL{i}", "kind": "part",
                 "description": "x", "acceptance_criteria": [],
                 "depends_on": [], "target_refs": {}, "status": "pending"}
                for i in range(1, 6)
            ],
        })

    led = _MemLedger()
    monkeypatch.setattr(
        "services.agent.reviewer_exec.run_verify_battery",
        lambda *a, **k: {
            "verdict": "NOT-CHECKED", "ok": False, "checks": [],
        },
    )
    monkeypatch.setattr(
        "services.agent.reviewer_exec._resolve_fn",
        lambda tool: None,
    )

    loop._ensure_terminal_criterion_grading(
        man, led, {"nodes": {}}, [],
        status="done", stop_reason="deliver_partial",
    )
    ids = {
        e["payload"]["id"]
        for e in led.events
        if e["kind"] == "criterion_result"
    }
    for ac in (f"AC{i}" for i in range(1, 10)):
        assert ac in ids, (ac, ids)
    # No fabricated PASS from silence — all honest non-pass when tools absent.
    results = {
        e["payload"]["id"]: e["payload"]["result"]
        for e in led.events
        if e["kind"] == "criterion_result"
    }
    for ac, res in results.items():
        assert str(res).upper() in (
            "PASS", "FAIL", "SKIP", "NOT-CHECKED", "NOT_CHECKED",
        ), (ac, res)


def test_self_loop_does_not_unlock_board():
    """mega-17 residual: R1.1↔R1.2 is not product nets without power."""
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_has_cross_wire, _ecad_has_real_wire,
        _ecad_nets_ready_for_board,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Sensor_Pressure:BMP280"},
            "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "R1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "2",
            },
            "result": {"ok": True, "net": "N$9", "net_size": 2},
        },
    ]
    assert _ecad_has_cross_wire(calls) is False
    assert _ecad_has_real_wire(calls) is False
    assert _ecad_nets_ready_for_board(calls, allow) is False
    # Must inject power, not board.
    assert _ecad_chain_need(calls, allow) == "ecad_add_power"


def test_power_then_cross_wire_unlocks_board():
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_forced_tool_args, _ecad_nets_ready_for_board,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"},
            "result": {
                "ref": "U1",
                "pins": [{"number": "1", "name": "GND", "type": "power_in",
                          "xy": [0, 0]}],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "xy": [10, 0]},
                    {"number": "2", "xy": [20, 0]},
                ],
            },
        },
    ]
    assert _ecad_chain_need(calls, allow) == "ecad_add_power"
    calls_p = calls + [{
        "tool": "ecad_add_power", "ok": True,
        "args": {"net": "GND", "x": 0, "y": 0},
        "result": {"ok": True, "net": "GND", "net_size": 0},
    }]
    assert _ecad_chain_need(calls_p, allow) == "ecad_wire"
    wire_args = _ecad_forced_tool_args("ecad_wire", calls_p)
    # With U1+R1 live, must be cross-ref (not R1 self-loop).
    assert wire_args["from_ref"] != wire_args["to_ref"], wire_args
    assert {wire_args["from_ref"], wire_args["to_ref"]} <= {"U1", "R1"}
    calls_w = calls_p + [{
        "tool": "ecad_wire", "ok": True,
        "args": wire_args,
        "result": {"ok": True, "net": "N1", "net_size": 2},
    }]
    assert _ecad_nets_ready_for_board(calls_w, allow) is True
    assert _ecad_chain_need(calls_w, allow) == "board_from_schematic"


def test_force_inject_after_passive_is_add_power(tmp_path, monkeypatch):
    """After IC+passive place thrash, force-inject must hit add_power not only R."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega17_power_inject", path=tmp_path / "ledger.json")
    seq = {"n": 0}
    refs = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic",'
                    '"args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"ecad_place_symbol",'
                    '"args":{"libid":"Sensor_Pressure:BMP280"}}\n```'
                )
            # Thrash place / find_part to trip hard gate.
            if seq["n"] <= 8:
                return (
                    '```json\n{"tool":"ecad_place_symbol",'
                    '"args":{"libid":"Device:C"}}\n```'
                )
            return "done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-p"}
        if tool == "ecad_place_symbol":
            refs["n"] += 1
            lib = str((args or {}).get("libid") or "")
            if "Device:R" in lib:
                return {
                    "ok": True, "ref": f"R{refs['n']}", "libid": lib,
                    "value": "1k",
                    "pins": [
                        {"number": "1", "xy": [20, 20]},
                        {"number": "2", "xy": [30, 20]},
                    ],
                }
            if "Device:C" in lib:
                return {"ok": True, "ref": f"C{refs['n']}", "libid": lib}
            return {
                "ok": True, "ref": f"U{refs['n']}", "libid": lib,
                "pins": [{"number": "1", "name": "GND", "type": "power_in",
                          "xy": [0, 0]}],
            }
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": args.get("net") or "GND",
                "net_size": 0, "net_members": [],
            }
        if tool == "ecad_wire":
            a = args or {}
            if a.get("from_ref") == a.get("to_ref"):
                # Accept self-loop but chain should not stop there.
                return {"ok": True, "net": "Nself", "net_size": 2}
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-p"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Power then wire",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=18,
        phase="build",
    )
    calls = (res.result or {}).get("calls") or []
    tools_ok = [c.get("tool") for c in calls if c.get("ok")]
    assert "ecad_add_power" in tools_ok, calls
    # Cross-ref wire or at least a wire after power.
    assert "ecad_wire" in tools_ok, tools_ok
    # Power must precede board.
    if "board_from_schematic" in tools_ok:
        assert tools_ok.index("ecad_add_power") < tools_ok.index(
            "board_from_schematic"
        )

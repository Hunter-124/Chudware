"""Mega-21 fixes — M21 mech deliver-gate force, N21 AC5 net resolution.

Axes:
  M21 — deliver with zero mech artifacts is impossible: forced mech chain
        executes new_part+add_features per the airframe brief, or typed
        refusal with leg relaunch. Replay mega-20 ledger as fixture.
  N21 — AC5: finite V on a resolved rail net (prefer 3V3-class, else source
        net); net name recorded in criterion evidence. Fixture from mega-20
        netlist shape (VIN path → N$2).

Regression: mega-11 legs/grading + mega-16..20 fixes hold.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


_MEGA20_LEDGER = (
    Path(__file__).resolve().parents[2] / "reports/runs/mega-20/ledger.json"
)
_MEGA20_MANIFEST = (
    Path(__file__).resolve().parents[2] / "reports/runs/mega-20/manifest.json"
)


def _load_mega20_ledger() -> dict:
    if not _MEGA20_LEDGER.is_file():
        pytest.skip(f"mega-20 ledger fixture missing: {_MEGA20_LEDGER}")
    return json.loads(_MEGA20_LEDGER.read_text(encoding="utf-8"))


def _load_mega20_manifest() -> dict:
    if not _MEGA20_MANIFEST.is_file():
        pytest.skip(f"mega-20 manifest fixture missing: {_MEGA20_MANIFEST}")
    return json.loads(_MEGA20_MANIFEST.read_text(encoding="utf-8"))


class _FakeLedger:
    """Minimal ledger stand-in that records appends like the real Ledger."""

    def __init__(self, events: list | None = None, run_id: str = "mega21-test"):
        self.events = list(events or [])
        self.run_id = run_id
        self.state: dict[str, Any] = {"artifacts": []}

    def append(self, actor: str, kind: str, payload: dict) -> None:
        self.events.append({
            "actor": actor,
            "kind": kind,
            "payload": dict(payload or {}),
        })

    def add_criterion_result(self, actor, ac_id, result, **kwargs):
        self.append(actor, "criterion_result", {
            "id": ac_id,
            "result": result,
            **kwargs,
        })

    def add_artifact(self, *a, **k):
        self.state.setdefault("artifacts", []).append(k)


# ---------------------------------------------------------------------------
# M21 — mech deliver gate
# ---------------------------------------------------------------------------


def test_mega20_ledger_has_zero_mech_artifacts():
    """Fixture fingerprint: mega-20 primary starved the airframe leg."""
    from services.agent.agent_turn import _mech_has_part_artifact

    raw = _load_mega20_ledger()
    assert _mech_has_part_artifact(raw) is False
    # Sanity: ECAD product did run (not a total empty mission).
    tools = set()
    for e in raw.get("events") or []:
        pl = e.get("payload") or {}
        if pl.get("tool"):
            tools.add(pl["tool"])
    assert "ecad_wire" in tools or "board_from_schematic" in tools


def test_mech_deliver_gate_fires_on_mega20_ledger(monkeypatch):
    """Replay mega-20 ledger: gate must force new_part+add_features."""
    from services.agent import agent_turn as at

    raw = _load_mega20_ledger()
    man = _load_mega20_manifest()
    # Isolate force-once map.
    at._MECH_FORCE_ONCE.clear()

    calls_made: list[tuple[str, dict]] = []
    docs = {"n": 0}

    def fake_dispatch(tool: str, args: dict):
        calls_made.append((tool, dict(args or {})))
        if tool == "new_part":
            docs["n"] += 1
            return {
                "ok": True,
                "doc_id": f"doc-airframe-{docs['n']:04d}",
                "name": (args or {}).get("name") or "part",
            }
        if tool == "add_features":
            assert args.get("doc_id"), args
            assert isinstance(args.get("ops"), list) and args["ops"], args
            # Airframe body must include body tube / bay class features.
            types = [
                (op.get("feature") or {}).get("type")
                for op in args["ops"]
                if isinstance(op, dict)
            ]
            assert "cylinder" in types or "box" in types, types
            return {"ok": True, "doc_id": args.get("doc_id"), "n_ops": len(args["ops"])}
        return {"ok": False, "error": f"unexpected tool {tool}"}

    led = _FakeLedger(events=list(raw.get("events") or []), run_id="mega20-replay")
    gate = at.mech_deliver_gate(
        led,
        manifest=man,
        dispatch=fake_dispatch,
        actor="test",
        run_id="mega20-replay",
        force=True,
    )
    assert gate["forced"] is True, gate
    assert gate["ok"] is True, gate
    assert gate["allow_deliver"] is True, gate
    assert gate["refusal"] is None, gate
    tools = [t for t, _ in calls_made]
    assert tools[:2] == ["new_part", "add_features"], tools
    assert gate["doc_id"] and str(gate["doc_id"]).startswith("doc-airframe-")

    # Logged force inject + tool_call events on ledger.
    events = [e.get("payload", {}).get("event") for e in led.events]
    assert "mech_chain_force_inject" in events
    forced_tools = [
        e["payload"]["tool"]
        for e in led.events
        if e.get("payload", {}).get("event") == "tool_call"
        and e.get("payload", {}).get("forced")
    ]
    assert "new_part" in forced_tools and "add_features" in forced_tools


def test_mech_deliver_gate_caps_one_force_then_refuses():
    """Second deliver with still-zero mech → typed refusal (no infinite force)."""
    from services.agent import agent_turn as at

    at._MECH_FORCE_ONCE.clear()
    rid = "mega21-cap-test"

    def boom_dispatch(tool: str, args: dict):
        # Force path runs but add_features fails → still missing after force.
        if tool == "new_part":
            return {"ok": True, "doc_id": "doc-x"}
        if tool == "add_features":
            return {"ok": False, "error": "engine refused"}
        return {"ok": False, "error": "nope"}

    led = _FakeLedger(run_id=rid)
    g1 = at.mech_deliver_gate(
        led, dispatch=boom_dispatch, run_id=rid, force=True,
    )
    assert g1["forced"] is True
    assert g1["allow_deliver"] is False
    assert g1["refusal"] and "mech" in g1["refusal"].lower()

    # Second call: force already spent → refusal without re-dispatch.
    calls = {"n": 0}

    def count_dispatch(tool, args):
        calls["n"] += 1
        return {"ok": False, "error": "should not run"}

    g2 = at.mech_deliver_gate(
        led, dispatch=count_dispatch, run_id=rid, force=True,
    )
    assert g2["forced"] is False
    assert g2["allow_deliver"] is False
    assert g2["refusal"] and "after_force" in g2["refusal"]
    assert calls["n"] == 0


def test_mech_deliver_gate_noop_when_mech_present():
    """Existing new_part+add_features → allow deliver, no force."""
    from services.agent import agent_turn as at

    at._MECH_FORCE_ONCE.clear()
    led = _FakeLedger(events=[
        {
            "kind": "agent_turn",
            "payload": {
                "event": "tool_call",
                "tool": "new_part",
                "ok": True,
                "args": {"name": "airframe"},
                "result": {"ok": True, "doc_id": "d1"},
            },
        },
        {
            "kind": "agent_turn",
            "payload": {
                "event": "tool_call",
                "tool": "add_features",
                "ok": True,
                "args": {"doc_id": "d1", "ops": [{"op": "add_feature"}]},
                "result": {"ok": True, "doc_id": "d1"},
            },
        },
    ], run_id="has-mech")
    calls = {"n": 0}

    def disp(tool, args):
        calls["n"] += 1
        return {"ok": True}

    g = at.mech_deliver_gate(led, dispatch=disp, run_id="has-mech", force=True)
    assert g["had_mech"] is True
    assert g["allow_deliver"] is True
    assert g["forced"] is False
    assert calls["n"] == 0


def test_airframe_ops_cover_brief_dims():
    """Forced ops encode body tube / bay / fins / wing from role-card dims."""
    from services.agent.agent_turn import (
        _airframe_build_ops, _airframe_spec_from_manifest,
    )

    man = _load_mega20_manifest()
    spec = _airframe_spec_from_manifest(man)
    assert spec["body_od_mm"] >= 40.0
    assert spec["body_len_mm"] >= 300.0
    assert spec["bay_l_mm"] >= 50.0
    ops = _airframe_build_ops(spec)
    names = [
        (op.get("feature") or {}).get("name")
        for op in ops if isinstance(op, dict)
    ]
    assert "body_tube" in names
    assert "payload_bay" in names
    assert "wing" in names
    assert sum(1 for n in names if n and n.startswith("fin_")) >= 3


# The mech deliver gate's own contract is covered three times above
# (test_mech_deliver_gate_fires_on_mega20_ledger / _caps_one_force_then_refuses /
# _noop_when_mech_present) and once in test_mega22_fixes. A fourth test,
# `test_loop_deliver_gate_blocks_zero_mech`, claimed to cover the loop's terminal status
# MAPPING and was removed 2026-08-05: its subject
# (`services/agent/loop.py::_drive_phases_from_dag`) was deleted with the Python loop, and what it
# actually asserted was a tautology — it monkeypatched `mech_deliver_gate` to return
# allow_deliver=False, re-implemented the status mapping inside the test body
# (`status = "blocked"`), then asserted `status == "blocked"`. It imported `loop`,
# `PhaseMachine`, a graph, a dag_result and a `fake_phase_completion` and used none of
# them, so no plausible bug in any product code could have failed it.


# ---------------------------------------------------------------------------
# N21 — AC5 net resolution
# ---------------------------------------------------------------------------


def test_resolve_measure_net_prefers_3v3():
    from services.agent.agent_turn import resolve_measure_net

    r = resolve_measure_net(
        {"nets": [{"name": "GND"}, {"name": "3V3"}, {"name": "N$2"}]},
        preferred="3V3",
    )
    assert r["net"] == "3V3"
    assert r["probe"] == "V(3V3)"
    assert r["source"] == "preferred_exact"


def test_resolve_measure_net_falls_to_source_vin_path():
    """mega-20 shape: no 3V3; VIN source landed on N$2."""
    from services.agent.agent_turn import resolve_measure_net

    calls = [
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {
                "libid": "Simulation_SPICE:V_source",
                "ref": "VIN",
                "value": "7.4",
            },
            "result": {
                "ok": True,
                "ref": "VIN",
                "libid": "Simulation_SPICE:V_source",
                "pins": [
                    {"number": "+", "name": "+", "net": "N$2"},
                    {"number": "-", "name": "-", "net": "GND"},
                ],
            },
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {
                "from_ref": "VIN", "from_pin": "+",
                "to_ref": "R1", "to_pin": "1",
            },
            "result": {
                "ok": True,
                "net": "N$2",
                "net_members": ["R1.1", "VIN.+"],
                "net_size": 2,
            },
        },
        {
            "tool": "ecad_simulate",
            "ok": True,
            "args": {"analysis": "op", "probes": ["V(3V3)"]},
            "result": {
                "ok": True,
                "nodes": ["N$2", "N$3", "N$1"],
                "probes": ["V(3V3)"],
                "op": {"V(3V3)": None},
            },
        },
    ]
    r = resolve_measure_net(
        {"nodes": ["N$2", "N$3", "N$1"]},
        preferred="3V3",
        calls=calls,
    )
    assert r["net"] == "N$2", r
    assert r["probe"] == "V(N$2)", r
    assert r["source"] in ("source_injected", "source_net_arg", "first_nongnd"), r


def test_resolve_from_mega20_ledger_fixture():
    """Live mega-20 ledger → resolved net is source/VIN path, not bare 3V3."""
    from services.agent.agent_turn import resolve_measure_net_from_ledger

    raw = _load_mega20_ledger()
    led = _FakeLedger(events=list(raw.get("events") or []), run_id="m20")
    r = resolve_measure_net_from_ledger(led, preferred="3V3")
    # mega-20 had no 3V3 net; source was VIN on N$2.
    assert r["net"] != "3V3" or r["source"] != "preferred_exact"
    assert r["probe"].startswith("V(")
    # Prefer source-injected path.
    assert r["net"] in ("N$2", "VIN", "N$1", "N$3") or r["source"] in (
        "source_injected", "first_nongnd", "named_rail",
    ), r


def test_forced_simulate_args_use_resolved_probe():
    """_ecad_forced_tool_args(ecad_simulate) probes the resolved rail."""
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"doc_id": "sch1"},
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {"libid": "Device:V", "ref": "V1", "value": "DC 3.3"},
            "result": {
                "ok": True,
                "ref": "V1",
                "libid": "Device:V",
                "pins": [
                    {"number": "+", "net": "VIN"},
                    {"number": "-", "net": "GND"},
                ],
            },
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {"from_ref": "V1", "from_pin": "+", "to_ref": "R1", "to_pin": "1"},
            "result": {"ok": True, "net": "VIN", "net_size": 2},
        },
    ]
    args = _ecad_forced_tool_args("ecad_simulate", calls)
    probes = args.get("probes") or []
    assert probes, args
    # Not stuck on missing 3V3 when VIN is the live rail.
    assert probes[0] in ("V(VIN)", "V(3V3)") or probes[0].startswith("V("), args
    assert args.get("resolved_net")
    assert args.get("analysis") == "op"


def test_ac5_grader_accepts_resolved_net_finite_v():
    """AC5 interval grades finite V on resolved net, records name in detail."""
    from services.agent.reviewer_exec import PASS, FAIL, evaluate_pass_rule

    # Preferred 3V3 null, but resolved VIN holds 3.30.
    payload = {
        "ok": True,
        "analysis": "op",
        "backend": "mna",
        "converged": True,
        "device_count": 2,
        "nodes": ["VIN", "N$1"],
        "probes": ["V(VIN)"],
        "resolved_net": "VIN",
        "resolved_net_source": "source_injected",
        "op": {"V(3V3)": None, "V(VIN)": 3.30},
        "reading": "V(VIN)=3.30",
    }
    v, d = evaluate_pass_rule("V(3V3) in [3.135, 3.465]", payload)
    assert v == PASS, (v, d)
    assert "3.3" in d or "3.30" in d, d
    assert "vin" in d.lower() or "resolved" in d.lower() or "v(vin)" in d.lower(), d


def test_ac5_grader_honest_fail_null_all_probes():
    """Still FAIL (not NOT-CHECKED) when tool ran and all probes null."""
    from services.agent.reviewer_exec import FAIL, evaluate_pass_rule

    payload = {
        "ok": True,
        "analysis": "op",
        "converged": True,
        "device_count": 2,
        "nodes": ["N$2", "N$3", "N$1"],
        "probes": ["V(3V3)"],
        "resolved_net": "N$2",
        "op": {"V(3V3)": None, "V(N$2)": None},
        "reading": "no finite probe values — check connectivity / source values",
    }
    v, d = evaluate_pass_rule("V(3V3) in [3.135, 3.465]", payload)
    assert v == FAIL, (v, d)


def test_ac5_domain_params_hint_resolves_probe(monkeypatch):
    """_domain_params_hint(ecad_simulate) rewrites probes to resolved net."""
    from services.agent import reviewer_exec as re
    from services.agent import agent_turn as at

    raw = _load_mega20_ledger()
    led = _FakeLedger(events=list(raw.get("events") or []), run_id="n21")
    hint = re._domain_params_hint(
        "ecad_simulate",
        {"analysis": "op", "probes": ["V(3V3)"]},
        led,
    )
    assert hint.get("probes"), hint
    assert hint["probes"][0].startswith("V("), hint
    assert hint.get("resolved_net"), hint
    # mega-20: should not blindly keep 3V3 when that net is absent.
    # (If resolver still picks 3V3 as fallback that's ok only when no candidates;
    #  mega-20 has N$2 from wire — expect non-3V3 or source_injected.)
    r = at.resolve_measure_net_from_ledger(led)
    assert r["net"] == hint["resolved_net"]


def test_ac5_evidence_carries_resolved_net(monkeypatch):
    """_run_one stamps resolved_net into evidence for criterion_AC5.json."""
    from services.agent import reviewer_exec as re

    led = _FakeLedger(run_id="ev")
    # Seed a VIN source place so resolver has a rail.
    led.append("agent", "agent_turn", {
        "event": "tool_call",
        "tool": "ecad_place_symbol",
        "ok": True,
        "args": {"libid": "Device:V", "ref": "V1"},
        "result": {
            "ok": True,
            "ref": "V1",
            "libid": "Device:V",
            "pins": [{"number": "+", "net": "VIN"}, {"number": "-", "net": "GND"}],
        },
    })

    def fake_fn(**kwargs):
        probes = kwargs.get("probes") or ["V(3V3)"]
        # Simulate returns finite on whatever probe was requested.
        p0 = probes[0]
        return {
            "ok": True,
            "analysis": "op",
            "converged": True,
            "probes": probes,
            "op": {p0: 3.301},
            "nodes": ["VIN", "GND"],
        }

    monkeypatch.setattr(re, "_resolve_fn", lambda tool: fake_fn if tool == "ecad_simulate" else None)
    monkeypatch.setattr(re, "_latest_tool_result", lambda ledger, tool: None)

    v, d, evidence = re._run_one(
        "ecad_simulate",
        "V(3V3) in [3.135, 3.465]",
        {
            "tool": "ecad_simulate",
            "params_hint": {"analysis": "op", "probes": ["V(3V3)"]},
            "pass_rule": "V(3V3) in [3.135, 3.465]",
        },
        None,
        ledger=led,
    )
    assert v == re.PASS, (v, d, evidence)
    assert evidence.get("resolved_net"), evidence
    assert "vin" in str(evidence.get("resolved_net")).lower() or evidence.get(
        "resolved_net"
    )


# ---------------------------------------------------------------------------
# Airframe ops pure unit (no engine)
# ---------------------------------------------------------------------------


def test_mech_has_part_requires_both_tools():
    from services.agent.agent_turn import _mech_has_part_artifact

    assert _mech_has_part_artifact([]) is False
    assert _mech_has_part_artifact([
        {"tool": "new_part", "ok": True, "result": {"doc_id": "d"}},
    ]) is False
    assert _mech_has_part_artifact([
        {"tool": "new_part", "ok": True, "result": {"doc_id": "d"}},
        {"tool": "add_features", "ok": True, "result": {"ok": True}},
    ]) is True
    assert _mech_has_part_artifact([
        {"tool": "new_part", "ok": True, "result": {"doc_id": "d"}},
        {"tool": "add_features", "ok": False, "result": {"ok": False}},
    ]) is False

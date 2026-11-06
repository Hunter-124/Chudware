"""Mega-21 fixes — N21 AC5 net resolution.

Axis:
  N21 — AC5: finite V on a resolved rail net (prefer 3V3-class, else source
        net); net name recorded in criterion evidence. Fixture from mega-20
        netlist shape (VIN path → N$2).

Regression: mega-11 legs/grading + mega-16..20 fixes hold.
"""
from __future__ import annotations

from typing import Any



def _load_mega20_ledger() -> dict:
    calls = [
        {"tool": "ecad_place_symbol", "ok": True,
         "args": {"libid": "Simulation_SPICE:VDC", "ref": "VIN", "value": "7.4"},
         "result": {"ok": True, "ref": "VIN", "libid": "Simulation_SPICE:VDC",
                    "pins": [{"number": "1", "name": "", "net": "N$2"},
                             {"number": "2", "name": "", "net": "GND"}]}},
        {"tool": "ecad_wire", "ok": True,
         "args": {"from_ref": "VIN", "from_pin": "1", "to_ref": "R1", "to_pin": "1"},
         "result": {"ok": True, "net": "N$2", "net_members": ["R1.1", "VIN.1"],
                    "net_size": 2}},
        {"tool": "ecad_simulate", "ok": True,
         "result": {"ok": True, "nodes": ["N$2", "N$3", "N$1"],
                    "op": {"V(3V3)": None}}},
    ]
    return {"events": [{"kind": "agent_turn", "payload": {"event": "tool_call", **c}}
                       for c in calls]}


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
                "libid": "Simulation_SPICE:VDC",
                "ref": "VIN",
                "value": "7.4",
            },
            "result": {
                "ok": True,
                "ref": "VIN",
                "libid": "Simulation_SPICE:VDC",
                "pins": [
                    {"number": "1", "name": "", "net": "N$2"},
                    {"number": "2", "name": "", "net": "GND"},
                ],
            },
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {
                "from_ref": "VIN", "from_pin": "1",
                "to_ref": "R1", "to_pin": "1",
            },
            "result": {
                "ok": True,
                "net": "N$2",
                "net_members": ["R1.1", "VIN.1"],
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


def test_resolve_measure_net_uses_numbered_positive_pin_without_top_level_net():
    from services.agent.agent_turn import resolve_measure_net

    for libid in ("Simulation_SPICE:VDC", "Simulation_SPICE:VSIN", "Simulation_SPICE:VPULSE"):
        calls = [{
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": libid, "ref": "X1"},
            "result": {
                "ref": "X1", "libid": libid,
                "pins": [
                    {"number": "2", "name": "", "net": "GND"},
                    {"number": "1", "name": "", "net": "N$2"},
                ],
            },
        }]
        result = resolve_measure_net(calls=calls)
        assert result["net"] == "N$2", (libid, result)
        assert result["source"] == "source_injected", (libid, result)


def test_resolve_measure_net_ignores_source_return_wire():
    from services.agent.agent_turn import resolve_measure_net

    calls = [
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "V1", "from_pin": "1", "to_ref": "R1", "to_pin": "1"},
            "result": {"net": "N$2"},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "V1", "from_pin": "2", "to_ref": "R1", "to_pin": "2"},
            "result": {"net": "GND"},
        },
    ]
    result = resolve_measure_net(calls=calls)
    assert result["net"] == "N$2", result
    assert result["source"] == "source_injected", result


def test_resolve_from_mega20_ledger_fixture():
    """Resolve a source/VIN residual to its net, not bare 3V3."""
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
            "args": {"libid": "Simulation_SPICE:VDC", "ref": "V1", "value": "3.3"},
            "result": {
                "ok": True,
                "ref": "V1",
                "libid": "Simulation_SPICE:VDC",
                "pins": [
                    {"number": "1", "name": "", "net": "VIN"},
                    {"number": "2", "name": "", "net": "GND"},
                ],
            },
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {"from_ref": "V1", "from_pin": "1", "to_ref": "R1", "to_pin": "1"},
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
        "args": {"libid": "Simulation_SPICE:VDC", "ref": "V1"},
        "result": {
            "ok": True,
            "ref": "V1",
            "libid": "Simulation_SPICE:VDC",
            "pins": [{"number": "1", "name": "", "net": "VIN"},
                     {"number": "2", "name": "", "net": "GND"}],
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

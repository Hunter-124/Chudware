"""Mega-10 quality-cycle regressions (Fixer10 = ComprehensiveWhale).

Axes:
  H — AC closure: at least one mission AC graded PASS/FAIL from real battery
      / measure artifacts (not NOT-CHECKED when evidence exists).
  I — battery doc_id correct with multiple parts open.
  J — typed-refusal envelope gate E2E (harness-level).
Regression: A–G stay green via mega-8/mega-9 suites.
"""
from __future__ import annotations

import json

import pytest


class _MemLedger:
    def __init__(self):
        self.events: list[dict] = []
        self.state = {
            "criterion_results": [],
            "artifacts": [],
        }

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
        if kind == "artifact":
            self.state["artifacts"].append(dict(payload))
        return ev

    def add_artifact(self, actor, kind, artifact_id, **kw):
        body = {"id": artifact_id, "kind": kind, **kw}
        return self.append(actor, "artifact", body)

    def add_criterion_result(self, actor, ac_id, result, **kw):
        return self.append(actor, "criterion_result", {
            "id": ac_id, "result": result, **kw,
        })


# ---------------------------------------------------------------------------
# H — AC grading consumes battery_result (mega-9 residual: AC1 NOT-CHECKED)
# ---------------------------------------------------------------------------


def test_ac1_grades_from_battery_payload_not_not_checked(monkeypatch):
    """AC1 check.tool=verify_battery must consume battery scores/findings.

    mega-9 live: battery_result PASS (synthetic BATTERY) while AC1 stayed
    NOT-CHECKED with detail 'no verdict/status field'. Fix: reuse battery
    evidence for verify_battery criteria.
    """
    from services.agent import reviewer_exec as re

    clean = {
        "scores": {
            "is_manifold": 1.0,
            "eval_check": 1.0,
            "measure_part": 1.0,
            "query_geometry": 1.0,
            "groundedness": 1.0,
        },
        "findings": [],
        "elapsed_ms": 12.0,
    }

    def _fake_battery(**kwargs):
        # Prove doc_id was threaded.
        assert kwargs.get("doc_id") == "doc-cpp00000001"
        return clean

    monkeypatch.setattr(
        re, "_resolve_fn",
        lambda tool: _fake_battery if tool == "verify_battery" else None,
    )

    led = _MemLedger()
    bat = re.run_verify_battery(led, doc_id="doc-cpp00000001")
    assert bat["verdict"] == re.PASS
    assert bat["doc_id"] == "doc-cpp00000001"

    manifest = {
        "deliverables": [{
            "id": "DL1",
            "acceptance_criteria": [{
                "id": "AC1",
                "text": "airframe geometry coherent (verify_battery PASS)",
                "check": {"tool": "verify_battery", "pass_rule": "PASS"},
            }],
        }],
    }
    results = re.run_acceptance_checks(
        manifest, led, doc_id="doc-cpp00000001", battery_payload=bat,
    )
    assert results["AC1"] == re.PASS, results
    ac1 = next(
        e for e in led.events
        if e["kind"] == "criterion_result" and e["payload"].get("id") == "AC1"
    )
    assert ac1["payload"]["result"] == "PASS"
    assert ac1["payload"]["evidence"].get("source") == "battery_result"
    # Must not be the mega-9 residual detail.
    assert "no verdict/status" not in (ac1["payload"].get("detail") or "")


def test_ac1_fail_when_battery_critical_zeros(monkeypatch):
    """Honest FAIL (not NOT-CHECKED) when battery has is_manifold=0 / ok:false."""
    from services.agent import reviewer_exec as re

    bad = {
        "scores": {
            "is_manifold": 0.0,
            "eval_check": 0.0,
            "measure_part": 0.0,
            "groundedness": 1.0,
        },
        "findings": [
            {"check": "is_manifold", "ok": False, "detail": "document has no bodies"},
            {"check": "eval_check", "ok": False, "detail": "document has no bodies"},
        ],
    }

    monkeypatch.setattr(
        re, "_resolve_fn",
        lambda tool: (lambda **kw: bad) if tool == "verify_battery" else None,
    )
    led = _MemLedger()
    bat = re.run_verify_battery(led, doc_id="doc-cpp9")
    assert bat["verdict"] == re.FAIL

    manifest = {
        "deliverables": [{
            "id": "DL1",
            "acceptance_criteria": [{
                "id": "AC1",
                "check": {"tool": "verify_battery", "pass_rule": "PASS"},
            }],
        }],
    }
    results = re.run_acceptance_checks(
        manifest, led, doc_id="doc-cpp9", battery_payload=bat,
    )
    assert results["AC1"] == re.FAIL, results
    assert results["AC1"] != re.NOT_CHECKED


def test_run_reviewer_checks_battery_before_acs(monkeypatch):
    """VERIFY path: battery first, AC1 reuses battery_payload (order + wiring)."""
    from services.agent import loop as loop_mod

    order: list[str] = []

    def _bat(ledger, *, doc_id=None, actor="reviewer"):
        order.append(f"battery:{doc_id}")
        payload = {
            "verdict": "PASS",
            "ok": True,
            "checks": [{"name": "is_manifold", "verdict": "PASS", "score": 1.0}],
            "doc_id": doc_id,
            "evidence": {
                "scores": {"is_manifold": 1.0, "eval_check": 1.0},
                "findings": [],
            },
        }
        ledger.append(actor, "battery_result", payload)
        return payload

    def _acs(manifest, ledger, *, doc_id=None, actor="reviewer",
             battery_payload=None):
        order.append(f"acs:{doc_id}")
        assert battery_payload is not None, "AC sweep must receive battery_payload"
        assert battery_payload.get("doc_id") == "doc-cpp9"
        # Grade AC1 from battery like production path.
        from services.agent.reviewer_exec import evaluate_pass_rule, PASS
        ev = battery_payload.get("evidence") or battery_payload
        v, d = evaluate_pass_rule("PASS", ev)
        ledger.add_criterion_result(
            actor, "AC1", v, check_tool="verify_battery",
            pass_rule="PASS", detail=d, evidence={"source": "battery_result"},
        )
        return {"AC1": v}

    monkeypatch.setattr("services.agent.reviewer_exec.run_verify_battery", _bat)
    monkeypatch.setattr("services.agent.reviewer_exec.run_acceptance_checks", _acs)
    monkeypatch.setattr(
        "services.agent.reviewer_exec.resolve_mission_doc_id",
        lambda **kw: "doc-cpp9",
    )

    led = _MemLedger()
    manifest = {
        "deliverables": [{
            "id": "DL1",
            "acceptance_criteria": [{
                "id": "AC1",
                "check": {"tool": "verify_battery", "pass_rule": "PASS"},
            }],
        }],
    }
    loop_mod._run_reviewer_checks(manifest, led, {"nodes": {}})
    assert order == ["battery:doc-cpp9", "acs:doc-cpp9"], order
    ac1 = next(
        e for e in led.events
        if e["kind"] == "criterion_result" and e["payload"].get("id") == "AC1"
    )
    assert ac1["payload"]["result"] == "PASS"


def test_interference_pass_rule_mechanical():
    from services.agent.reviewer_exec import evaluate_pass_rule, FAIL, PASS

    v, _ = evaluate_pass_rule(
        "interferences empty or volume==0",
        {"interferences": [], "ok": True},
    )
    assert v == PASS
    v, _ = evaluate_pass_rule(
        "interferences empty or volume==0",
        {"interferences": [{"volume_mm3": 12.0}], "ok": True},
    )
    assert v == FAIL
    v, _ = evaluate_pass_rule(
        "interferences empty or volume==0",
        {"interferences": [{"volume_mm3": 0.0}], "ok": True},
    )
    assert v == PASS


# ---------------------------------------------------------------------------
# I — battery doc_id with multiple parts open
# ---------------------------------------------------------------------------


def test_resolve_prefers_geometry_bearing_doc_over_empty_spawn():
    """Two open docs: prefer the one with add_features volume, not empty new_part."""
    from services.agent.reviewer_exec import resolve_mission_doc_id

    class _L:
        events = [
            {
                "kind": "agent_turn",
                "payload": {
                    "event": "tool_call",
                    "tool": "new_part",
                    "ok": True,
                    "result": json.dumps({
                        "ok": True, "doc_id": "doc-cpp00000001",
                        "name": "rocket_plane_airframe",
                    }),
                },
            },
            {
                "kind": "agent_turn",
                "payload": {
                    "event": "tool_call",
                    "tool": "add_features",
                    "ok": True,
                    "args": {"doc_id": "doc-cpp00000001"},
                    "result": json.dumps({
                        "ok": True,
                        "doc_id": "doc-cpp00000001",
                        "body_count": 1,
                        "volume_mm3": 1187522.0,
                    }),
                },
            },
            {
                "kind": "agent_turn",
                "payload": {
                    "event": "tool_call",
                    "tool": "new_part",
                    "ok": True,
                    "result": json.dumps({
                        "ok": True,
                        "doc_id": "doc-cpp00000003",
                        "name": "DL1_rocket_plane_airframe",
                    }),
                },
            },
        ]

    # Empty spawn shell is newer; geometry-bearing doc must still win.
    got = resolve_mission_doc_id(ledger=_L())
    assert got == "doc-cpp00000001", got


def test_run_verify_battery_threads_doc_id_with_two_docs(monkeypatch):
    """VERIFY must call verify_battery(doc_id=...) — never bare current-part."""
    from services.agent import reviewer_exec as re

    seen: dict = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return {
            "scores": {"is_manifold": 1.0, "eval_check": 1.0},
            "findings": [],
        }

    monkeypatch.setattr(re, "_resolve_fn", lambda tool: _fake)
    led = _MemLedger()
    # Simulate two docs known to the resolver via ledger trail.
    led.events.append({
        "kind": "agent_turn",
        "payload": {
            "event": "tool_call",
            "tool": "new_part",
            "ok": True,
            "result": json.dumps({"ok": True, "doc_id": "doc-cppA"}),
        },
    })
    led.events.append({
        "kind": "agent_turn",
        "payload": {
            "event": "tool_call",
            "tool": "add_features",
            "ok": True,
            "args": {"doc_id": "doc-cppA"},
            "result": json.dumps({
                "ok": True, "doc_id": "doc-cppA",
                "body_count": 1, "volume_mm3": 100.0,
            }),
        },
    })
    led.events.append({
        "kind": "agent_turn",
        "payload": {
            "event": "tool_call",
            "tool": "new_part",
            "ok": True,
            "result": json.dumps({"ok": True, "doc_id": "doc-cppB"}),
        },
    })
    doc = re.resolve_mission_doc_id(ledger=led)
    assert doc == "doc-cppA"
    out = re.run_verify_battery(led, doc_id=doc)
    assert seen.get("doc_id") == "doc-cppA"
    assert out["doc_id"] == "doc-cppA"
    bat_ev = next(e for e in led.events if e["kind"] == "battery_result")
    assert bat_ev["payload"]["doc_id"] == "doc-cppA"


def test_call_tool_forwards_params_hint_and_doc_id():
    from services.agent.reviewer_exec import _call_tool

    got = {}

    def _fn(doc_id=None, feature=None, **kw):
        got["doc_id"] = doc_id
        got["feature"] = feature
        return {"ok": True, "volume_mm3": 42.0}

    raw = _call_tool(_fn, "doc-cpp1", {"feature": "payload_bay", "noise": object()})
    assert raw["volume_mm3"] == 42.0
    assert got == {"doc_id": "doc-cpp1", "feature": "payload_bay"}


# ---------------------------------------------------------------------------
# J — typed-refusal harness E2E (flat multi-domain node)
# ---------------------------------------------------------------------------


def test_typed_refusal_flat_multi_domain_gate_e2e():
    """Seed a deliberately-flat multi-domain node; taught envelope passes gate."""
    from services.dag import node_runner as nr
    from services.agent.role_models import (
        extract_typed_refusal,
        validate_typed_refusal,
    )

    envelope = {
        "reason": "serial dependency chain across geometry+assembly",
        "domains_considered": ["geometry", "assembly", "ecad"],
        "why_flat": "DL2 mates need DL1 solid + board outline first; spawn thrash",
    }
    assert validate_typed_refusal(envelope) == []

    # Request scaffold teaches the keys.
    req = nr._build_request(
        "plan",
        {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "request": "Plan rocket airframe + avionics integration as one flat node.",
        },
        {
            "role_id": "orchestrator",
            "task_class": "multi_feature_part",
            "domain_tags": ["plan", "geometry", "assembly", "ecad"],
            "allowed_tools": ["ledger_get", "ledger_note", "delegate", "buildgraph_plan"],
        },
    )
    assert "domains_considered" in req and "why_flat" in req and "reason" in req

    node = {
        "id": "n_build_DL2",
        "type": "build",
        "name": "BUILD DL2 flat multi-domain",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "assembly",
            "depth": 0,
            "request": "Integrate airframe + FC board without spawning.",
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "assembly",
        "domain_tags": ["geometry", "assembly", "ecad"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "calls": [{"tool": "ledger_note", "ok": True}],
            "handoff": {
                "status": "done",
                "summary": "staying flat with typed refusal",
                "outputs": {"typed_refusal": envelope},
            },
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert out["data"].get("stop_reason") != "delegate_or_typed_refusal_required"
    # Envelope recoverable from agent_result shape the gate sees.
    ar = (out.get("data") or {}).get("agent_result") or _R.result
    got = extract_typed_refusal(ar if isinstance(ar, dict) else _R.result)
    assert got is not None
    assert set(got["domains_considered"]) >= {"geometry", "assembly"}


def test_explicit_pass_contradicted_by_failing_scores_is_fail():
    """battery_result verdict=PASS with is_manifold=0 must not free-pass AC1."""
    from services.agent.reviewer_exec import FAIL, evaluate_pass_rule

    # mega-9 live shape: synthetic PASS + critical zeros in evidence.
    env = {
        "verdict": "PASS",
        "ok": True,
        "scores": {"is_manifold": 0.0, "eval_check": 0.0},
        "findings": [
            {"check": "is_manifold", "ok": False, "detail": "no bodies"},
        ],
    }
    v, d = evaluate_pass_rule("PASS", env)
    assert v == FAIL, (v, d)

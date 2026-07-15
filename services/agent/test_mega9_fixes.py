"""Mega-9 quality-cycle regressions (Fixer9 = ReadySparrow).

Axes:
  F — terminal stop_reason clean string classes (no raw dag_error blobs)
  E — verify_battery wiring: battery runs / per-check results in ledger
  G — typed-refusal envelope validates end-to-end + prompts teach schema
Regression: A–D stay green via existing mega-8 suite (not re-run here).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# F — Fix11 residual: clean dag_error stop_reason classes
# ---------------------------------------------------------------------------


def test_classify_dag_error_blob_too_big():
    from services.agent.loop import classify_dag_error

    assert classify_dag_error("string or blob too big") == "dag_error:checkpoint_blob"
    assert classify_dag_error(Exception("string or blob too big")) == "dag_error:checkpoint_blob"
    # Never embed the raw exception body in the class token.
    out = classify_dag_error("string or blob too big\n" + ("x" * 5000))
    assert out == "dag_error:checkpoint_blob"
    assert "xxxx" not in out
    assert len(out) < 64


def test_classify_dag_error_concurrent_update():
    from services.agent.loop import classify_dag_error

    msg = (
        "At key '__root__': Can receive only one value per step. "
        "Use an Annotated key to handle multiple values.\n"
        "INVALID_CONCURRENT_GRAPH_UPDATE"
    )
    assert classify_dag_error(msg) == "dag_error:concurrent_update"


def test_classify_dag_error_unknown_is_stable_class():
    from services.agent.loop import classify_dag_error

    out = classify_dag_error(RuntimeError("something novel exploded with payload " + "Z" * 200))
    assert out == "dag_error:execution"
    assert "Z" not in out
    assert "something novel" not in out


def test_classify_dag_error_sqlite_variants():
    from services.agent.loop import classify_dag_error

    assert classify_dag_error("database is locked") == "dag_error:checkpoint_locked"
    assert classify_dag_error("sqlite3.OperationalError: disk I/O error") in (
        "dag_error:io",
        "dag_error:checkpoint",
    )


def test_compact_agent_result_drops_fat_payloads():
    """Fix11 prevention: fat tool results never survive into node_states."""
    from services.dag import node_runner as nr

    fat = {
        "stop_reason": "confirmed",
        "success": True,
        "handoff": {
            "status": "done",
            "summary": "built",
            "outputs": {
                "typed_refusal": None,
                "big": "Y" * 50_000,
                "productive_tools": ["add_features", "verify_battery"],
            },
        },
        "calls": [
            {"tool": "add_features", "ok": True, "result": {"mesh": "M" * 20_000}},
            {"tool": "verify_battery", "ok": True, "result": {"findings": [{"x": "F" * 5000}]}},
        ],
    }
    compact = nr._compact_agent_result(fat)
    blob = __import__("json").dumps(compact, default=str)
    assert len(blob) < 12_000
    assert "YYYY" not in blob
    assert "MMMM" not in blob
    assert compact.get("calls")
    assert all("result" not in c for c in compact["calls"])


# ---------------------------------------------------------------------------
# E — verify_battery wiring / ledger evidence
# ---------------------------------------------------------------------------


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


def test_run_verify_battery_logs_honest_not_checked_when_unresolved(monkeypatch):
    """Battery always leaves a ledger trail — even when tool cannot resolve."""
    from services.agent import reviewer_exec as re

    monkeypatch.setattr(re, "_resolve_fn", lambda tool: None)
    led = _MemLedger()
    out = re.run_verify_battery(led, doc_id="doc-test")
    assert out["verdict"] == re.NOT_CHECKED
    kinds = [e["kind"] for e in led.events]
    assert "battery_result" in kinds
    assert "artifact" in kinds
    assert "criterion_result" in kinds
    bat = next(e for e in led.events if e["kind"] == "battery_result")
    assert bat["payload"]["verdict"] == "NOT-CHECKED"
    crit = next(e for e in led.events if e["kind"] == "criterion_result")
    assert crit["payload"]["id"] == "BATTERY"
    assert crit["payload"]["check_tool"] == "verify_battery"


def test_run_verify_battery_logs_pass_and_per_check(monkeypatch):
    from services.agent import reviewer_exec as re

    def _fake_battery(**kwargs):
        return {
            "ok": True,
            "verdict": "PASS",
            "findings": [],
            "checks": [
                {"name": "solid_valid", "verdict": "PASS"},
                {"name": "manifold", "verdict": "PASS"},
            ],
        }

    monkeypatch.setattr(re, "_resolve_fn", lambda tool: _fake_battery if tool == "verify_battery" else None)
    led = _MemLedger()
    out = re.run_verify_battery(led, doc_id="doc-cpp1")
    assert out["verdict"] == "PASS"
    assert out["ok"] is True
    assert len(out["checks"]) == 2
    bat = next(e for e in led.events if e["kind"] == "battery_result")
    assert bat["payload"]["checks"][0]["name"] == "solid_valid"
    art = next(e for e in led.events if e["kind"] == "artifact")
    assert art["payload"].get("verdict") == "PASS"


def test_run_verify_battery_logs_fail_honestly(monkeypatch):
    from services.agent import reviewer_exec as re

    def _fake_battery(**kwargs):
        return {
            "ok": True,
            "findings": [
                {"name": "open_shell", "severity": "fail", "message": "non-manifold edge"},
            ],
            "checks": [{"name": "manifold", "verdict": "FAIL"}],
            "scores": {"is_manifold": 0.0},
        }

    monkeypatch.setattr(re, "_resolve_fn", lambda tool: _fake_battery)
    led = _MemLedger()
    out = re.run_verify_battery(led, doc_id="doc-bad")
    assert out["verdict"] == "FAIL"
    assert out["ok"] is False
    # FAIL is still logged — axis E: honest geometry verdicts.
    bat = next(e for e in led.events if e["kind"] == "battery_result")
    assert bat["payload"]["verdict"] == "FAIL"

def test_interpret_battery_fails_on_zero_manifold():
    """a2 residual: is_manifold 0.0 / ok:false findings must not be overall PASS."""
    from services.agent.reviewer_exec import FAIL, PASS, evaluate_pass_rule, interpret_battery_result

    raw = {
        "scores": {
            "is_manifold": 0.0,
            "groundedness": 1.0,
            "eval_check": 0.0,
        },
        "findings": [
            {"check": "is_manifold", "ok": False, "detail": "document has no bodies"},
            {"check": "eval_check", "ok": False, "detail": "document has no bodies"},
        ],
    }
    v, detail = interpret_battery_result(raw)
    assert v == FAIL, (v, detail)
    # AC1 pass_rule PASS must match the same shape (not NOT-CHECKED).
    ac_v, ac_d = evaluate_pass_rule("PASS", raw)
    assert ac_v == FAIL, (ac_v, ac_d)
    assert "manifold" in ac_d.lower() or "failing" in ac_d.lower()


def test_evaluate_pass_rule_pass_on_clean_battery_scores():
    from services.agent.reviewer_exec import PASS, evaluate_pass_rule

    raw = {
        "scores": {"is_manifold": 1.0, "eval_check": 1.0, "groundedness": 1.0},
        "findings": [],
    }
    v, d = evaluate_pass_rule("PASS", raw)
    assert v == PASS, (v, d)


def test_resolve_mission_doc_id_prefers_build_and_ledger():
    from services.agent.reviewer_exec import resolve_mission_doc_id

    class _L:
        events = [
            {
                "kind": "agent_turn",
                "payload": {
                    "event": "tool_call",
                    "tool": "new_part",
                    "ok": True,
                    "result": '{"ok": true, "doc_id": "doc-cpp00000003"}',
                },
            }
        ]

    dag = {
        "nodes": {
            "n_build_DL1": {
                "status": "done",
                "doc_id": "doc-cpp00000003",
                "output": {"doc_id": "doc-cpp00000003"},
            },
            "n_verify_DL1": {"status": "done"},
        }
    }
    assert resolve_mission_doc_id(dag_result=dag, ledger=_L()) == "doc-cpp00000003"
    assert resolve_mission_doc_id(prefer="doc-explicit") == "doc-explicit"
    assert resolve_mission_doc_id(dag_result={}, ledger=_L()) == "doc-cpp00000003"


def test_extract_doc_id_from_agent_result():
    from services.dag import node_runner as nr

    ar = {
        "calls": [
            {
                "tool": "new_part",
                "ok": True,
                "result": {"ok": True, "doc_id": "doc-cpp9"},
            }
        ]
    }
    assert nr._extract_doc_id_from_agent_result(ar) == "doc-cpp9"


def test_typed_refusal_salvaged_from_ledger_note_text():
    from services.agent.role_models import extract_typed_refusal

    env = {
        "reason": "serial chain",
        "domains_considered": ["geometry", "assembly"],
        "why_flat": "DL2 needs DL1 solid",
    }
    payload = {
        "calls": [
            {
                "tool": "ledger_note",
                "ok": True,
                "args": {
                    "kind": "decision",
                    "text": __import__("json").dumps(env),
                },
            }
        ],
        "handoff": {"status": "done", "summary": "flat", "outputs": {}},
    }
    got = extract_typed_refusal(payload)
    assert got is not None
    assert got["reason"] == "serial chain"



def test_run_reviewer_checks_invokes_battery(monkeypatch):
    """VERIFY entry path must call run_verify_battery (axis E wiring).

    mega-10: battery runs BEFORE AC sweep so AC1 can consume battery_payload.
    """
    from services.agent import loop as loop_mod

    calls = {"battery": 0, "acs": 0}
    order: list[str] = []

    def _acs(manifest, ledger, *, doc_id=None, actor="reviewer",
             battery_payload=None):
        calls["acs"] += 1
        order.append("acs")
        return {}

    def _bat(ledger, *, doc_id=None, actor="reviewer"):
        calls["battery"] += 1
        order.append("battery")
        ledger.append(actor, "battery_result", {
            "verdict": "NOT-CHECKED", "ok": False, "checks": [], "doc_id": doc_id,
        })
        return {"verdict": "NOT-CHECKED", "doc_id": doc_id}

    monkeypatch.setattr("services.agent.reviewer_exec.run_acceptance_checks", _acs)
    monkeypatch.setattr("services.agent.reviewer_exec.run_verify_battery", _bat)

    led = _MemLedger()
    dag_result = {
        "nodes": {
            "n_build_DL1": {"status": "done", "doc_id": "doc-cpp9", "output": {"doc_id": "doc-cpp9"}},
        }
    }
    loop_mod._run_reviewer_checks({"deliverables": []}, led, dag_result)
    assert calls["acs"] == 1
    assert calls["battery"] == 1
    assert order == ["battery", "acs"], order
    assert any(e["kind"] == "battery_result" for e in led.events)



def test_feature_modeler_has_verify_battery_tool():
    from services.agent.roles import load_role

    card = load_role("feature_modeler")
    assert "verify_battery" in card.concrete_tools
    assert "add_features" in card.concrete_tools


def test_build_and_verify_requests_mention_battery():
    from services.dag.mission_template import build_mission_graph

    manifest = {
        "schema": "aicad.manifest/1",
        "id": "m9",
        "name": "m9",
        "deliverables": [
            {
                "id": "DL1",
                "name": "bracket",
                "kind": "part",
                "acceptance_criteria": [
                    {"id": "AC1", "text": "solid", "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
                ],
                "depends_on": [],
            }
        ],
    }
    g = build_mission_graph(manifest)
    by_id = {n["id"]: n for n in g["nodes"]}
    build_req = by_id["n_build_DL1"]["inputs"]["request"]
    verify_req = by_id["n_verify_DL1"]["inputs"]["request"]
    assert "verify_battery" in build_req
    assert "verify_battery" in verify_req


# ---------------------------------------------------------------------------
# G — typed-refusal schema taught + envelope validates end-to-end
# ---------------------------------------------------------------------------


def test_typed_refusal_envelope_validates_end_to_end():
    """Canonical envelope (the one prompts teach) validates + extracts."""
    from services.agent.role_models import (
        extract_typed_refusal,
        validate_typed_refusal,
    )

    envelope = {
        "reason": "single integrated solid",
        "domains_considered": ["geometry", "assembly"],
        "why_flat": "mates share one doc; spawn thrash",
    }
    assert validate_typed_refusal(envelope) == []
    # Handoff path used by node_runner gate.
    extracted = extract_typed_refusal({
        "handoff": {
            "status": "done",
            "summary": "flat with typed refusal",
            "outputs": {"typed_refusal": envelope},
        }
    })
    assert extracted is not None
    assert extracted["reason"] == envelope["reason"]
    assert extracted["domains_considered"] == ["geometry", "assembly"]
    assert extracted["why_flat"] == envelope["why_flat"]


def test_typed_refusal_gate_accepts_taught_envelope():
    """Node runner DONE when multi-domain PLAN returns the taught schema."""
    from services.dag import node_runner as nr

    refusal = {
        "reason": "serial dependency chain",
        "domains_considered": ["geometry", "assembly"],
        "why_flat": "DL2 mates need DL1 solid first",
    }
    node = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "depth": 0,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "multi_feature_part",
        "domain_tags": ["plan", "geometry", "assembly"],
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
                "summary": "flat with typed refusal",
                "outputs": {"typed_refusal": refusal},
            },
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert out["data"].get("stop_reason") != "delegate_or_typed_refusal_required"


def test_multi_domain_request_teaches_exact_schema_keys():
    """F9 residual: scaffold must spell reason/domains_considered/why_flat + example."""
    from services.dag import node_runner as nr

    req = nr._build_request(
        "plan",
        {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "request": "Plan the rocket mission DAG.",
        },
        {
            "role_id": "orchestrator",
            "task_class": "multi_feature_part",
            "domain_tags": ["plan", "geometry", "assembly"],
            "allowed_tools": ["ledger_get", "ledger_note", "delegate", "buildgraph_plan"],
        },
    )
    assert "typed_refusal" in req
    assert "reason" in req
    assert "domains_considered" in req
    assert "why_flat" in req
    # Example envelope from the enriched scaffold.
    assert "single integrated solid" in req or "serial dependency" in req or '"reason"' in req
    assert "non-empty" in req.lower() or "MUST" in req


def test_role_cards_teach_typed_refusal_schema():
    from services.agent.roles import load_role

    for rid in ("layout_engineer", "orchestrator"):
        card = load_role(rid)
        prompt = card.system_prompt or ""
        assert "domains_considered" in prompt, rid
        assert "why_flat" in prompt, rid
        assert "reason" in prompt, rid

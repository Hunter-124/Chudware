"""Mega-28 terminal classification — all-AC-PASS => done/mission_complete.

Live mega-28 (reports/runs/mega-28/) graded 9/9 PASS yet the mission terminal
reported ``status=blocked, phase=deliver, stop_reason=deliver_partial`` because
soft-join deliver treated incomplete sibling deliver nodes as partial even
after every acceptance criterion passed.

Contract:
  * all declared ACs gate-ok (PASS|SKIP|waived) + soft-join deliver_partial
    → status=done, stop_reason=mission_complete
  * any FAIL / NOT-CHECKED → keep deliver_partial semantics (mega-8)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MEGA28 = _ROOT / "reports/runs/mega-28"
_MEGA28_STATUS = _MEGA28 / "status_report.json"
_MEGA28_MISSION = _MEGA28 / "mission.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


class _MemLedger:
    def __init__(self):
        self.events: list[dict] = []
        self.state = {"criterion_results": [], "artifacts": []}

    def append(self, actor, kind, payload):
        ev = {"actor": actor, "kind": kind, "payload": payload}
        self.events.append(ev)
        return ev

    def log_stage(self, *a, **k):
        return None

    def features(self):
        return []

    def geometry_specs(self):
        return []

    def add_criterion_result(self, actor, ac_id, result, **kw):
        return self.append(actor, "criterion_result", {
            "id": ac_id, "result": result, **kw,
        })


def _rocket_manifest():
    from services.agent.mission_criteria import apply_rocket_plane_checks

    base = {
        "request_verbatim": "flight computer rocket plane",
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
    return apply_rocket_plane_checks(base)


def _soft_join_dag(manifest, graph, *, dag_status: str = "blocked"):
    """Replay mega-28 shape: all non-deliver done; soft-join deliver partial."""
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            # One deliver report done, siblings failed/skipped (soft-join).
            if "DL1" in nid or nid.endswith("DL5"):
                node_states[nid] = {"status": "done", "output": {"status": "done"}}
            else:
                node_states[nid] = {
                    "status": "failed", "output": {"status": "failed"},
                }
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    return {"status": dag_status, "nodes": node_states}


def _seed_all_ac_pass(led: _MemLedger, ac_ids: list[str] | None = None) -> None:
    ids = ac_ids or [f"AC{i}" for i in range(1, 10)]
    for ac in ids:
        led.add_criterion_result(
            "reviewer", ac, "PASS",
            check_tool="fixture",
            pass_rule="mega-28 all-pass fixture",
            detail=f"seeded PASS for {ac}",
            evidence={"source": "mega28_fixture"},
        )


def _drive(manifest, graph, dag_result, ledger, monkeypatch):
    from services.agent import loop
    from services.agent.pipeline.phases import Phase, PhaseMachine

    # Keep terminal grading from re-running live tools; seeded PASS must stick.
    monkeypatch.setattr(
        "services.agent.reviewer_exec.run_acceptance_checks",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "services.agent.reviewer_exec.run_verify_battery",
        lambda *a, **k: {
            "verdict": "PASS", "ok": True, "checks": [], "detail": "fixture",
        },
    )
    # Product gates must not hard-block the classification fixture.
    monkeypatch.setattr(
        "services.agent.agent_turn.ecad_product_gate",
        lambda *a, **k: {"forced": False, "ok": True},
    )
    monkeypatch.setattr(
        "services.agent.agent_turn.mech_deliver_gate",
        lambda *a, **k: {"allow_deliver": True, "forced": False, "ok": True},
    )
    monkeypatch.setattr(
        "services.agent.loop._terminal_autosave_close_docs",
        lambda **k: {"ok": True, "saved": [], "closed": []},
    )

    machine = PhaseMachine()
    completed: list = []
    events: list = []

    def _emit(phase, text):
        events.append((getattr(phase, "value", phase), text))

    reached, status, stop_reason = loop._drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, _emit, Phase,
        ledger=ledger, run_id="mega-28-terminal-fixture",
    )
    return reached, status, stop_reason, completed, events


# ---------------------------------------------------------------------------
# Live fingerprint (pre-fix terminal disagreeing with 9/9 grade)
# ---------------------------------------------------------------------------


def test_mega28_live_fingerprint_all_ac_pass_but_deliver_partial():
    """Document the bug shape from reports/runs/mega-28 (grading vs terminal)."""
    report = _load_json(_MEGA28_STATUS)
    mission = _load_json(_MEGA28_MISSION)
    term = report.get("terminal") or {}
    assert term.get("status") == "blocked" or mission.get("status") == "blocked"
    assert (
        term.get("stop_reason") == "deliver_partial"
        or mission.get("stop_reason") == "deliver_partial"
    )
    assert term.get("phase") == "deliver" or mission.get("phase") == "deliver"
    # Grading authoritative: all 9 ACs PASS in the live run.
    live = report.get("live_pass") or {}
    assert int(live.get("count") or 0) == 9 or report.get("milestone_9_of_9") is True
    for i in range(1, 10):
        ac = _load_json(_MEGA28 / f"criterion_AC{i}.json")
        assert ac.get("result") == "PASS", (i, ac.get("result"))


# ---------------------------------------------------------------------------
# Fix: all-AC-PASS soft-join deliver => done / mission_complete
# ---------------------------------------------------------------------------


def test_all_ac_pass_soft_join_promotes_to_mission_complete(monkeypatch):
    """Replay mega-28 shape: soft-join deliver_partial + 9× PASS → mission_complete."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.dag.mission_template import build_mission_graph

    manifest = _rocket_manifest()
    graph = build_mission_graph(manifest, graph_id="m28_term", org_layer=False)
    dag_result = _soft_join_dag(manifest, graph, dag_status="blocked")
    led = _MemLedger()
    _seed_all_ac_pass(led)

    reached, status, stop_reason, completed, _ = _drive(
        manifest, graph, dag_result, led, monkeypatch,
    )

    assert "deliver" in completed or reached == "deliver", (completed, reached)
    assert status == "done", (status, stop_reason, reached, completed)
    assert stop_reason == "mission_complete", (status, stop_reason, reached)
    assert stop_reason != "deliver_partial"
    assert status != "blocked"


def test_partial_ac_fail_keeps_deliver_partial(monkeypatch):
    """Partial verdicts must keep soft-join deliver_partial semantics.

    Soft-reconcile past incomplete build/verify (mega-16/17 shape) so the
    VERIFY gate does not route to needs_correction — the residual is the
    soft-join deliver_partial terminal with honest FAIL on AC4.
    """
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.dag.mission_template import build_mission_graph

    manifest = _rocket_manifest()
    graph = build_mission_graph(manifest, graph_id="m28_partial", org_layer=False)
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            if "DL1" in nid or nid.endswith("DL5"):
                node_states[nid] = {"status": "done", "output": {"status": "done"}}
            else:
                node_states[nid] = {
                    "status": "failed", "output": {"status": "failed"},
                }
        elif nid.startswith("n_build_") or nid.startswith("n_verify_"):
            # Incomplete workers — soft-reconcile via deliver done.
            node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    dag_result = {"status": "paused", "nodes": node_states}
    led = _MemLedger()
    # 8 PASS + 1 FAIL → still partial (do NOT promote).
    for i in range(1, 10):
        led.add_criterion_result(
            "reviewer", f"AC{i}", "PASS" if i != 4 else "FAIL",
            check_tool="fixture",
            detail="partial fixture",
        )

    reached, status, stop_reason, completed, _ = _drive(
        manifest, graph, dag_result, led, monkeypatch,
    )

    assert stop_reason == "deliver_partial", (status, stop_reason, reached)
    assert status in ("done", "blocked"), (status, stop_reason)
    assert status != "running"
    assert stop_reason != "mission_complete"


def test_empty_ac_soft_join_stays_deliver_partial(monkeypatch):
    """No declared ACs: soft-join still deliver_partial (mega-8 contract)."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.pipeline.phases import Phase, PhaseMachine
    from services.dag.mission_template import build_mission_graph

    manifest = {
        "request_verbatim": "Design a rocket plane.",
        "ambiguities": [],
        "deliverables": [
            {"id": "DL1", "name": "Airframe", "kind": "part",
             "description": "primary", "acceptance_criteria": []},
            {"id": "DL2", "name": "Assembly", "kind": "assembly",
             "description": "mate", "acceptance_criteria": []},
            {"id": "DL3", "name": "Report", "kind": "report",
             "description": "report", "acceptance_criteria": []},
            {"id": "DL4", "name": "Drawing", "kind": "drawing",
             "description": "drawing", "acceptance_criteria": []},
            {"id": "DL5", "name": "Note", "kind": "report",
             "description": "note", "acceptance_criteria": []},
        ],
    }
    graph = build_mission_graph(manifest, graph_id="m28_empty", org_layer=False)
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            if nid.endswith("DL2"):
                node_states[nid] = {"status": "done", "output": {"status": "done"}}
            else:
                node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    dag_result = {"status": "paused", "nodes": node_states}

    monkeypatch.setattr(
        "services.agent.agent_turn.ecad_product_gate",
        lambda *a, **k: {"forced": False, "ok": True},
    )
    monkeypatch.setattr(
        "services.agent.agent_turn.mech_deliver_gate",
        lambda *a, **k: {"allow_deliver": True, "forced": False, "ok": True},
    )
    monkeypatch.setattr(
        "services.agent.loop._terminal_autosave_close_docs",
        lambda **k: {"ok": True, "saved": [], "closed": []},
    )

    machine = PhaseMachine()
    completed: list = []
    reached, status, stop_reason = loop._drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, lambda *a: None, Phase,
        ledger=None,
    )
    assert stop_reason == "deliver_partial", (status, stop_reason, reached)
    assert status in ("done", "blocked")

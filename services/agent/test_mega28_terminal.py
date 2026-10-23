"""Mega-28 live fingerprint: the run graded 9/9 PASS yet reported a blocked terminal.

Live mega-28 graded 9/9 PASS while the mission terminal said
``status=blocked, phase=deliver, stop_reason=deliver_partial`` — soft-join deliver
treated incomplete sibling deliver nodes as partial even after every acceptance
criterion passed. This file pins that FINGERPRINT against the archived run artifacts, so
the historical shape stays readable and a future reader can tell the grade from the
terminal.

It does NOT test the terminal classifier. The Python mission loop was deleted
(``services/agent/loop.py`` ``run``/``run_mission``/``_drive_phases_from_dag``) and the
C++ ``agent_mission_v1`` is the only live mission entry, so the classifier is no longer
reachable from Python. A ``_drive`` helper here used to monkeypatch those deleted
functions and was itself never called — dead scaffolding that read as coverage for a
mechanism nothing exercised. Removed 2026-08-05; the classification contract belongs to
``core/agent/`` and needs a ctest, not a Python fixture.
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


# ---------------------------------------------------------------------------
# Live fingerprint (pre-fix terminal disagreeing with 9/9 grade)
# ---------------------------------------------------------------------------


def test_mega28_live_fingerprint_all_ac_pass_but_deliver_partial():
    """Document the bug shape from the mega-28 run (grading vs terminal)."""
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

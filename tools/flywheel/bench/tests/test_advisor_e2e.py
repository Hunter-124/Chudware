"""advisor_e2e grading: fixed mandatory denominator, agent-produced study only (P1-4/P1-5)."""
from __future__ import annotations

import pytest

from tools.flywheel.bench.benches import advisor_e2e as A

BORE_FACE = {"of": "face", "surf": "round", "pick": "inner"}
GOOD_STUDY = {
    "id": "study-1", "type": "fea_static", "name": "Static stress (aluminium)",
    "material": {"name": "Aluminium 6061-T6", "E_mpa": 68900.0, "yield_mpa": 276.0},
    "fixtures": [{"of": "face", "side": "bottom"}],
    "loads": [{"face": BORE_FACE, "force": [500.0, 0.0, 0.0]}],
    "results": {"min_fos": 3.2, "max_von_mises_mpa": 86.0, "max_disp_mm": 0.01},
}


@pytest.fixture
def engine(monkeypatch):
    """Stub the two reads score_fea may make; any solve request fails the test."""
    state = {"studies": [GOOD_STUDY]}

    def dispatch(tool, args):
        if tool.startswith("run_fea"):
            pytest.fail(f"the grader must not run {tool} on the agent's behalf")
        assert tool == "query_geometry", tool
        return {"matches": [{"kind": "face", "concavity": "bore", "axis": [0, 1, 0],
                             "radius": 10.02}]}

    monkeypatch.setattr(A, "_dispatch", dispatch)
    monkeypatch.setattr(A, "_study_list", lambda _doc: state["studies"])
    return state


def _with(**over) -> dict:
    return {**GOOD_STUDY, **over}


def test_saved_aluminium_side_load_study_passes(engine):
    res = A.score_fea("doc-1")
    assert res["passed"] is True, res
    assert res["study_id"] == "study-1"


def test_missing_study_fails_and_is_never_solved_by_the_grader(engine):
    engine["studies"] = [{"type": "motion", "name": "M"}]
    res = A.score_fea("doc-1")
    assert res["passed"] is False
    assert "no saved static FEA study" in res["reason"]


@pytest.mark.parametrize("override, why", [
    ({"material": {"name": "Mild steel", "E_mpa": 200000.0}}, "not aluminium"),
    ({"loads": [{"face": BORE_FACE, "force": [0.0, 500.0, 0.0]}]}, "along the bore axis"),
    ({"loads": [{"face": BORE_FACE, "force": [250.0, 0.0, 0.0]}]}, "!= 500 N"),
    ({"loads": [{"face": BORE_FACE, "pressure": 2.0}]}, "not a force vector"),
    ({"results": {"min_fos": 0.8}}, "<= 1"),
    ({"results": {}}, "no finite min_fos"),
])
def test_wrong_study_setup_or_result_fails(engine, override, why):
    engine["studies"] = [_with(**override)]
    res = A.score_fea("doc-1")
    assert res["passed"] is False
    assert any(why in f for f in res["faults"]), res["faults"]


def test_last_study_decides(engine):
    engine["studies"] = [GOOD_STUDY, _with(id="study-2", results={"min_fos": 0.5})]
    res = A.score_fea("doc-1")
    assert res["passed"] is False and res["study_id"] == "study-2"


def test_unreadable_document_is_a_grader_error(monkeypatch):
    def boom(_doc):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(A, "_study_list", boom)
    res = A.score_fea("doc-1")
    assert res["passed"] is None and "engine gone" in res["error"]


def test_unmeasurable_criterion_stays_in_the_denominator(monkeypatch):
    monkeypatch.setattr(A, "score_bore_fit", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_hole_pattern", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_manifold", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_fea", lambda d: {"passed": None, "error": "solver down"})
    s = A.score_doc("doc-1")
    assert s["graded_count"] == 4 == len(A.MANDATORY_CRITERIA)
    assert s["passed_count"] == 3 and s["fraction"] == 0.75
    assert s["status"] == "error"


def test_a_failed_criterion_fails_the_document(monkeypatch):
    monkeypatch.setattr(A, "score_bore_fit", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_hole_pattern", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_manifold", lambda d: {"passed": True})
    monkeypatch.setattr(A, "score_fea", lambda d: {"passed": False})
    assert A.score_doc("doc-1")["status"] == "fail"


def test_leg_passes_only_when_completed_and_every_criterion_passed():
    leg = A.LegResult(loop="baseline", tier="t", model="m", seed=1, run_id="r")
    leg.scores = {"status": "pass", "fraction": 1.0}
    leg.ok = False
    assert A.example_passed(leg) is False
    leg.ok = True
    assert A.example_passed(leg) is True
    leg.scores = {"status": "error", "fraction": 0.75}
    assert A.example_passed(leg) is False
    by = A._by_loop([leg])
    assert by["baseline"]["passed"] == 0 and by["baseline"]["grader_errors"] == 1

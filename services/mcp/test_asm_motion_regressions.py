"""Regressions for assembly motion input, source leases and study persistence."""
import json

import pytest

from services.mcp import cad_asm_tools as asm
from services.mcp import core_bridge as cb


@pytest.mark.parametrize("at,axis,angle", [
    (None, None, 20), (None, [0, 0, 1], 0), (None, [0, 0, 0], 45),
])
def test_component_pose_rejects_ineffective_or_invalid_rotation(at, axis, angle):
    with pytest.raises(ValueError):
        asm._component_pose(at, axis, angle)


def test_mate_types_and_required_parameters():
    for kind, value, count in [("gear", {"ratio": 2}, 2),
                                ("screw", {"pitch": 1.5}, 2),
                                ("width", None, 3), ("symmetric", None, 3),
                                ("angle_limit", {"min": 0, "max": 90}, 2)]:
        assert asm._check_mate_request(kind, value, count) == kind
    for kind, value, count in [("not_a_mate", None, 2), ("gear", None, 2),
                                ("width", None, 2), ("screw", {}, 2)]:
        with pytest.raises(ValueError):
            asm._check_mate_request(kind, value, count)


def test_missing_solve_verdict_is_not_converged():
    report = asm._solve_report({})
    assert report["converged"] is False
    assert report["reason"] == "solve_verdict_missing"


def test_internal_source_open_is_closed_even_on_failure(monkeypatch, tmp_path):
    path = str(tmp_path / "part.step+")
    events = []

    def call(method, args):
        events.append((method, dict(args)))
        if method == "doc_list":
            return {"docs": []}
        if method == "doc_open":
            return {"doc_id": "private"}
        if method == "doc_close":
            return {"ok": True}
        raise AssertionError(method)

    monkeypatch.setattr(asm.cb, "call", call)
    with pytest.raises(RuntimeError):
        with asm.open_component_part(path) as doc:
            assert doc == "private"
            raise RuntimeError("failure during source use")
    assert events[1] == ("doc_open", {"path": path, "internal": True, "detach_path": True})
    assert events[2] == ("doc_close", {"doc_id": "private"})


def test_mate_source_prefers_open_unsaved_document(monkeypatch, tmp_path):
    path = str(tmp_path / "part.step+")
    monkeypatch.setattr(asm, "_open_docs_by_path", lambda: {
        path: {"doc_id": "live", "path": path, "dirty": True}})
    monkeypatch.setattr(asm.cb, "call", lambda *args: pytest.fail("unexpected source open"))
    with asm._asm_component_doc(path) as doc:
        assert doc == "live"


def test_motion_conflicting_drivers_and_duration_are_refused(monkeypatch):
    monkeypatch.setattr(asm, "_doc", lambda doc: doc)
    monkeypatch.setattr(asm, "_asm_mates", lambda doc: [
        {"id": "m1", "type": "distance", "value": {"value": 0}}])
    with pytest.raises(ValueError):
        asm.run_motion_sweep("d", mate_id="m1", stop=1,
                             drivers=[{"mate_id": "m1", "start": 0, "stop": 1}])
    with pytest.raises(ValueError):
        asm.run_motion_sweep("d", mate_id="m1", start=0, stop=1, steps=3,
                             dynamics={"duration_s": 0})


def test_motion_output_limit_reports_completed_sweep_and_persisted_study():
    result = asm._shape_motion_output({
        "ok": True, "doc_id": "d", "trajectory": [],
        "motion_study": {"study_id": "s1"},
        "oversized": "x" * (asm._MOTION_MAX_REPLY_CHARS + 1),
    }, "full")
    assert result["ok"] is False and result["reason"] == "motion_output_limit"
    assert result["sweep_completed"] is True
    assert result["motion_study"]["study_id"] == "s1"


def test_motion_study_loop_and_persistence_failure_are_reported(monkeypatch):
    monkeypatch.setattr(asm, "_doc", lambda doc: doc)
    monkeypatch.setattr(asm, "_asm_mates", lambda doc: [
        {"id": "m1", "type": "distance", "value": {"value": 0}}])
    monkeypatch.setattr(asm.cb, "call", lambda method, args: {
        "ok": True, "trajectory": [{"i": 0, "bodies": []}, {"i": 1, "bodies": []}]})
    records = []
    monkeypatch.setattr(asm, "_persist_study", lambda doc, record, **kw: records.append(record) or "s1")
    out = asm.run_motion_sweep("d", mate_id="m1", start=0, stop=1, steps=2,
                               persist_study=True, loop=False)
    assert out["motion_study"]["study_id"] == "s1"
    assert records[0]["motion"]["loop"] is False
    asm.run_motion_sweep("d", mate_id="m1", start=0, stop=1, steps=2,
                         persist_study=False)
    assert len(records) == 1
    direct = json.loads(asm.simulate_motion(doc_id="d", mate_id="m1", start=0, stop=1,
                                            steps=2))
    assert direct["ok"] is True and "motion_study" not in direct
    assert len(records) == 1
    def failed(*args, **kwargs):
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(asm, "_persist_study", failed)
    out = asm.run_motion_sweep("d", mate_id="m1", start=0, stop=1, steps=2,
                               persist_study=True)
    assert out["ok"] is True
    assert "storage unavailable" in out["motion_study_error"]


@pytest.mark.skipif(not cb.available(), reason="CAD engine unavailable")
def test_motion_dynamics_uses_live_component_material(tmp_path):
    from services.mcp.test_cad_tools import _build_sliding_block_assembly, _call

    assembly, mate_id, moving = _build_sliding_block_assembly(tmp_path)
    tree = cb.call("doc_tree", {"doc_id": assembly})["tree"]
    source = next(c["source"]["path"] for c in tree["components"] if c["id"] == moving)
    part = cb.call("doc_open", {"path": source, "internal": True})["doc_id"]
    try:
        changed, _ = _call("set_material", {"doc_id": part, "material": "6061-T6"})
        assert changed["ok"], changed
        result, _ = _call("simulate_motion", {
            "doc_id": assembly, "mate_id": mate_id, "start": 10, "stop": 20,
            "steps": 3, "check_interference": False, "persist_study": False,
            "dynamics": {"default_density_g_cm3": 7.85}})
        body = next(b for b in result["dynamics"]["bodies"] if b["component"] == moving)
        assert body["density_source"] == "component_material"
        assert body["density_g_cm3"] == pytest.approx(2.7, rel=0.02)
        assert body["mass_kg"] == pytest.approx(0.0027, rel=0.02)
    finally:
        cb.call("doc_close", {"doc_id": assembly})
        cb.call("doc_close", {"doc_id": part})

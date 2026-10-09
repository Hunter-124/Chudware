"""C1-C3 graders against REAL deliverables built offline with the real CAD/FEA tools.

Each task gets a correct deliverable (direct tool calls, no model) that must PASS every
mandatory check, and adversarial deliverables that must FAIL — never ERROR, never PASS:
C1 15 holes / shifted hole / Ø5.5 holes / blind holes; C2 untouched input / flange
changed / bolt holes lost / bore enlarged by a stacked cut; C3 wrong load direction /
missing study / wrong material / fixture on the wrong face; plus missing, empty and
wrong-document artifacts. Graders open each file in the in-process engine.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from benchmarks.artifact_validity.evidence import build_evidence, sha256_file
from benchmarks.artifact_validity.fixtures import build_c2_bushing
from benchmarks.artifact_validity.tasks import core
from benchmarks.artifact_validity.tasks.schema import (CheckResult, Status, Task, check_ids,
                                                       declared_id, task_status)


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _feat(ftype: str, name: str, params: dict, refs: dict | None = None, **extra) -> dict:
    f = {"type": ftype, "name": name, "params": params, **extra}
    if refs is not None:
        f["refs"] = refs
    return {"op": "add_feature", "feature": f}


@pytest.fixture(scope="module")
def save_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("core_tasks")
    mp = pytest.MonkeyPatch()
    mp.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(d))
    yield d
    mp.undo()


@pytest.fixture(scope="module")
def make(cad_tools, save_dir):
    """make(name, ops, *, then=None) -> Path: new_part + add_features (+ an optional
    callback on the live doc, e.g. a static study) + save_part to <save_dir>/<name>/."""

    def _make(name: str, ops: list[dict], then=None) -> Path:
        doc = json.loads(cad_tools["new_part"](name=f"core_{name}", fresh=True))["doc_id"]
        built = json.loads(cad_tools["add_features"](doc_id=doc, ops=ops))
        assert built.get("ok"), built
        if then is not None:
            then(doc)
        out = save_dir / name / "deliverable.step+"
        out.parent.mkdir(parents=True, exist_ok=True)
        saved = json.loads(cad_tools["save_part"](path=str(out), doc_id=doc))
        assert saved.get("ok"), saved
        cad_tools["close_part"](doc_id=doc, discard=True)
        return out

    return _make


def grade(task: Task, path: Path) -> dict[str, CheckResult]:
    ev = build_evidence(path)
    out: dict[str, CheckResult] = {}
    for i, fn in enumerate(task.checks):
        res = fn(ev)
        assert res.id == declared_id(fn, i)
        out[res.id] = res
    assert list(out) == check_ids(task)
    return out


def _status(task: Task, results: dict[str, CheckResult], path: Path) -> Status:
    return task_status(Status.PASS, path.is_file(), list(results.values()), check_ids(task))


def assert_pass(task: Task, path: Path) -> None:
    res = grade(task, path)
    bad = {k: r.to_json() for k, r in res.items() if r.status is not Status.PASS}
    assert not bad, json.dumps(bad, indent=1, default=str)
    assert _status(task, res, path) is Status.PASS


def assert_fails(task: Task, path: Path, *must_fail: str) -> dict[str, CheckResult]:
    res = grade(task, path)
    errors = {k: r.to_json() for k, r in res.items() if r.status is Status.ERROR}
    assert not errors, json.dumps(errors, indent=1, default=str)
    for cid in must_fail:
        assert res[cid].status is Status.FAIL, (cid, res[cid].to_json())
    assert _status(task, res, path) is Status.FAIL
    return res


# ============================================================================ C1

def _plate_ops(positions, diameter: float = 6.0, depth: float | None = None) -> list[dict]:
    hole = {"style": "simple", "diameter": _dim(diameter), "positions": positions}
    hole.update({"end": "blind", "depth": _dim(depth)} if depth else {"end": "through_all"})
    return [_feat("box", "Plate", {"length": _dim(100), "width": _dim(100),
                                   "height": _dim(10)}),
            _feat("hole", "Grid", hole, {"face": {"of": "face", "side": "top"}})]


C1_GRID = [[x, y] for x in (-30, -10, 10, 30) for y in (-30, -10, 10, 30)]


@pytest.fixture(scope="module")
def c1_good(make):
    return make("c1_good", _plate_ops(C1_GRID))


def test_c1_correct_plate_passes(c1_good):
    assert_pass(core.C1, c1_good)


def test_c1_fifteen_holes_fail_although_volume_passes(make):
    res = assert_fails(core.C1, make("c1_15", _plate_ops(C1_GRID[:-1])),
                       "C1.hole_count", "C1.hole_positions")
    # The old volume-only criterion would have accepted this part.
    assert res["C1.volume"].status is Status.PASS


def test_c1_shifted_hole_fails(make):
    grid = [list(p) for p in C1_GRID]
    grid[-1] = [32, 30]
    res = assert_fails(core.C1, make("c1_shift", _plate_ops(grid)), "C1.hole_positions")
    assert res["C1.hole_count"].status is Status.PASS


def test_c1_undersized_holes_fail(make):
    assert_fails(core.C1, make("c1_d55", _plate_ops(C1_GRID, diameter=5.5)),
                 "C1.hole_diameter")


def test_c1_blind_holes_are_not_through_holes(make):
    assert_fails(core.C1, make("c1_blind", _plate_ops(C1_GRID, depth=5.0)), "C1.hole_count")


def test_c1_missing_empty_and_wrong_document_fail(tmp_path, c1_good):
    missing = tmp_path / "nothing" / "deliverable.step+"
    res = assert_fails(core.C1, missing, *check_ids(core.C1))
    assert all(r.measured == "missing" for r in res.values())

    empty = tmp_path / "empty" / "deliverable.step+"
    empty.parent.mkdir()
    empty.write_bytes(b"")
    assert_fails(core.C1, empty, *check_ids(core.C1))

    wrong = tmp_path / "wrong" / "deliverable.step+"
    wrong.parent.mkdir()
    shutil.copyfile(core.FIXTURES_DIR / core.C2_FIXTURE, wrong)
    assert_fails(core.C1, wrong, "C1.plate_size", "C1.hole_count", "C1.hole_positions")


def test_c1_turn_failure_is_never_a_pass(c1_good):
    """A complete-looking artifact left behind by a failed/aborted turn never PASSes."""
    res = grade(core.C1, c1_good)
    ids = check_ids(core.C1)
    assert task_status(Status.FAIL, True, list(res.values()), ids) is Status.FAIL
    assert task_status(Status.ERROR, True, list(res.values()), ids) is Status.ERROR
    # A dropped check is never a smaller denominator.
    assert task_status(Status.PASS, True, list(res.values())[1:], ids) is Status.ERROR


# ============================================================================ C2

def test_c2_fixture_is_the_declared_bytes():
    assert sha256_file(core.FIXTURES_DIR / core.C2_FIXTURE) == core.C2_FIXTURE_SHA256
    assert core.C2.fixtures == (core.C2_FIXTURE,)


@pytest.fixture(scope="module")
def c2_edit(cad_tools, save_dir):
    """edit(name, ops) -> Path: open a COPY of the fixture (as an agent would find it in
    its workspace), apply ops to the live doc, save as <name>/deliverable.step+."""
    from services.mcp import core_bridge as cb

    def _edit(name: str, ops: list[dict]) -> Path:
        work = save_dir / name
        work.mkdir(parents=True, exist_ok=True)
        src = work / core.C2_FIXTURE
        shutil.copyfile(core.FIXTURES_DIR / core.C2_FIXTURE, src)
        doc = cb.call("doc_open", {"path": str(src)})["doc_id"]
        if ops:
            r = json.loads(cad_tools["add_features"](doc_id=doc, ops=ops))
            assert r.get("ok"), r
        out = work / "deliverable.step+"
        saved = json.loads(cad_tools["save_part"](path=str(out), doc_id=doc))
        assert saved.get("ok"), saved
        cad_tools["close_part"](doc_id=doc, discard=True)
        return out

    return _edit


REBORE = {"op": "edit_feature", "id": "f5", "params": {"diameter": _dim(25.0)}}


def test_c2_rebored_in_place_passes(c2_edit):
    assert_pass(core.C2, c2_edit("c2_good", [REBORE]))


def test_c2_untouched_input_fails(c2_edit):
    res = assert_fails(core.C2, c2_edit("c2_stale", []), "C2.bore", "C2.feature_readback",
                       "C2.volume")
    assert res["C2.flange_hub"].status is Status.PASS


def test_c2_changed_flange_fails(c2_edit):
    assert_fails(core.C2, c2_edit("c2_flange", [
        REBORE, {"op": "edit_feature", "id": "f2", "params": {"depth": _dim(10.0)}}]),
        "C2.flange_hub", "C2.feature_readback", "C2.bolt_circle")


def test_c2_lost_bolt_holes_fail(c2_edit):
    assert_fails(core.C2, c2_edit("c2_noholes", [
        REBORE, {"op": "delete_feature", "id": "f6", "cascade": True}]),
        "C2.bolt_circle", "C2.feature_readback")


def test_c2_stacked_cut_instead_of_edit_fails_readback_only(c2_edit):
    """Right geometry, wrong intent: f5 still says Ø20 and an extra feature carries Ø25."""
    res = assert_fails(core.C2, c2_edit("c2_stacked", [
        _feat("hole", "Rebore", {"style": "simple", "end": "through_all",
                                 "diameter": _dim(25.0)},
              {"face": {"of": "face", "side": "top", "pick": "smallest"}})]),
        "C2.feature_readback")
    assert res["C2.bore"].status is Status.PASS


def test_c2_rebuilt_without_the_fixture_history_fails(make):
    """The same final geometry from primitives (no sketches/relations, new feature types)."""
    path = make("c2_prims", [
        _feat("cylinder", "Flange", {"radius": _dim(35), "height": _dim(8)},
              at=[0, 0, 0], centered=False),
        _feat("cylinder", "Hub", {"radius": _dim(18), "height": _dim(22)},
              at=[0, 0, 8], centered=False),
        _feat("hole", "Bore", {"style": "simple", "end": "through_all", "diameter": _dim(25)},
              {"face": {"of": "face", "side": "top", "pick": "smallest"}}),
        _feat("bolt_circle", "Bolts", {"style": "simple", "end": "through_all",
                                       "diameter": _dim(6.6), "count": 4, "pcd": _dim(52),
                                       "start_angle": 0},
              {"face": {"of": "face", "side": "top", "pick": "largest"}})])
    res = assert_fails(core.C2, path, "C2.feature_readback")
    assert res["C2.bore"].status is Status.PASS


def test_c2_fixture_builder_reproduces_the_reference_geometry(cad_tools, tmp_path):
    """The builder that made the hashed fixture still builds a part the C2 graders read
    as the un-edited bushing (bore Ø20, everything else per the oracle)."""
    path = build_c2_bushing.build(cad_tools, tmp_path / "deliverable.step+")
    res = grade(core.C2, path)
    assert res["C2.flange_hub"].status is Status.PASS
    assert res["C2.bolt_circle"].status is Status.PASS
    assert res["C2.bore"].status is Status.FAIL  # still the Ø20 input


# ============================================================================ C3

BAR = [_feat("box", "Bar", {"length": _dim(120), "width": _dim(20), "height": _dim(8)},
             at=[0, 0, 0], centered=False)]
STEEL = {"name": "structural steel", "E_mpa": 200000.0, "nu": 0.3, "yield_mpa": 250.0,
         "density_g_cm3": 7.85}


@pytest.fixture(scope="module")
def static(cad_tools):
    def _solve(fixtures, loads, material=STEEL):
        def run(doc: str) -> None:
            r = json.loads(cad_tools["run_fea_static"](fixtures=fixtures, loads=loads,
                                                       material=material, doc_id=doc))
            assert r.get("ok") and r.get("saved_to_study"), r
        return run
    return _solve


END0 = [{"of": "face", "side": "-x"}]
END120 = {"of": "face", "side": "+x"}


def test_c3_correct_study_passes(make, static):
    assert_pass(core.C3, make("c3_good", BAR, static(END0, [{"face": END120,
                                                             "force": [0, 0, -500]}])))


def test_c3_wrong_load_direction_fails(make, static):
    assert_fails(core.C3, make("c3_dir", BAR, static(END0, [{"face": END120,
                                                             "force": [0, -500, 0]}])),
                 "C3.load", "C3.tip_deflection", "C3.reactions", "C3.root_stress")


def test_c3_missing_study_fails(make):
    res = assert_fails(core.C3, make("c3_nostudy", BAR), "C3.study_saved", "C3.material",
                       "C3.fixture", "C3.load", "C3.tip_deflection", "C3.reactions",
                       "C3.root_stress", "C3.mesh_evidence")
    assert res["C3.bar_geometry"].status is Status.PASS


def test_c3_wrong_material_fails(make, static):
    assert_fails(core.C3, make("c3_alu", BAR, static(END0, [{"face": END120,
                                                             "force": [0, 0, -500]}],
                                                     material="aluminum")),
                 "C3.material", "C3.tip_deflection")


def test_c3_fixture_on_wrong_face_fails(make, static):
    assert_fails(core.C3, make("c3_fixbottom", BAR, static(
        [{"of": "face", "side": "bottom"}], [{"face": END120, "force": [0, 0, -500]}])),
        "C3.fixture", "C3.tip_deflection")


def test_registry_carries_core_tasks():
    from benchmarks.artifact_validity.tasks import REGISTRY

    for t in core.TASKS:
        assert REGISTRY[t.id] is t
        assert t.final_artifact in t.prompt
        assert t.domain in ("cad", "sim")

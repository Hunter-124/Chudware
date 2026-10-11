"""H1-H6 graders against REAL deliverables built with the CAD/FEA/assembly tools: the
reference deliverable for each task passes every check; adversarial ones (wrong bend radius,
thin wall, solid tube, capped duct, under-/over-sized arm, misplaced hole, missing or wrong
study, interfering/mis-axed/un-driven hinge, loose pin, recreated or un-chamfered flange,
thick or sharp-cornered shell, short pilots, moved bosses) fail exactly the checks they
target."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from benchmarks.artifact_validity.evidence import sha256_file
from benchmarks.artifact_validity.fixtures import build_h5_flange
from benchmarks.artifact_validity.scoring import grade
from benchmarks.artifact_validity.tasks import hard
from benchmarks.artifact_validity.tasks.hard import H1, H2, H3, H4, H5, H6


def _f(typ, name, params, **kw):
    return {"op": "add_feature", "feature": {"type": typ, "name": name, "params": params, **kw}}


def _statuses(task, path: Path) -> dict[str, str]:
    g = grade(task, path.parent)
    return {c["id"]: c["status"] for c in g["checks"]}


def _assert_failing(task, got: dict[str, str], failing: set[str]) -> None:
    assert set(got) == {fn.check_id for fn in task.checks}
    assert {k for k, v in got.items() if v != "pass"} == failing, got
    assert all(v in ("pass", "fail") for v in got.values()), got


# ---------------------------------------------------------------------------- H1

def _tube(r=50.0, od=25.0, id_=21.0):
    """Sweep an annulus (or a disc when id_ is 0) along the XZ centreline with bend r."""
    path = [
        {"type": "line", "p1": [0, 0], "p2": [300 - r, 0]},
        {"type": "arc", "center": [300 - r, r], "radius": r, "start": -90, "end": 0,
         "ccw": True},
        {"type": "line", "p1": [300, r], "p2": [300, 200 - r]},
        {"type": "arc", "center": [300 + r, 200 - r], "radius": r, "start": 180, "end": 90,
         "ccw": False},
        {"type": "line", "p1": [300 + r, 200], "p2": [500, 200]},
    ]
    section = [{"type": "circle", "center": [0, 0], "radius": od / 2}]
    if id_:
        section.append({"type": "circle", "center": [0, 0], "radius": id_ / 2})
    return [_f("sketch", "Path", {"entities": path}, refs={"plane": "plane.front"}),
            _f("sketch", "Section", {"entities": section}, refs={"plane": "plane.right"}),
            _f("sweep", "Tube", {"mode": "boss"},
               refs={"profile": [{"kind": "region", "sketch": "$1"}], "path": "$0"})]


# ---------------------------------------------------------------------------- H2

def _rect(w, h, off=None):
    p = {"entities": [{"type": "rectangle", "center": [0, 0], "width": w, "height": h}]}
    if off is not None:
        p["offset"] = off
    return p


def _circ(r, off=None):
    p = {"entities": [{"type": "circle", "center": [0, 0], "radius": r}]}
    if off is not None:
        p["offset"] = off
    return p


def _reg(i):
    return {"kind": "region", "sketch": f"${i}"}


def _duct(t=2.0, rect_bore=True):
    """Outer collar + loft + collar, then the same three as cuts ``t`` inside (the wall built
    into the profiles). Without ``rect_bore`` the rectangular collar stays solid: a capped
    bottom."""
    ops = [
        _f("sketch", "RectBase", _rect(100, 60), refs={"plane": "plane.top"}),
        _f("extrude", "RectCollar", {"mode": "boss", "end": "blind", "depth": 10},
           refs={"profile": [_reg(0)]}),
        _f("sketch", "RectTop", _rect(100, 60, 10), refs={"plane": "plane.top"}),
        _f("sketch", "RoundEnd", _circ(40, 110), refs={"plane": "plane.top"}),
        _f("loft", "Transition", {"mode": "boss"}, refs={"sections": [_reg(2), _reg(3)]}),
        _f("extrude", "RoundCollar", {"mode": "boss", "end": "blind", "depth": 10},
           refs={"profile": [_reg(3)]}),
    ]
    if rect_bore:
        ops += [_f("sketch", "InRectBase", _rect(100 - 2 * t, 60 - 2 * t),
                   refs={"plane": "plane.top"}),
                _f("extrude", "InRectCut", {"mode": "cut", "end": "blind", "depth": 10},
                   refs={"profile": [_reg(len(ops))]})]
    n = len(ops)
    ops += [
        _f("sketch", "InRectTop", _rect(100 - 2 * t, 60 - 2 * t, 10), refs={"plane": "plane.top"}),
        _f("sketch", "InRound", _circ(40 - t, 110), refs={"plane": "plane.top"}),
        _f("loft", "InTransition", {"mode": "cut"}, refs={"sections": [_reg(n), _reg(n + 1)]}),
        _f("extrude", "InRoundCut", {"mode": "cut", "end": "blind", "depth": 10},
           refs={"profile": [_reg(n + 1)]}),
    ]
    return ops


def _shelled_duct(t=2.0):
    """Outer collar + loft + collar hollowed by ONE shell feature, both ends opened: the
    loft's crease-to-tangent seams make OCCT's offset refuse, so this is the sectional shell."""
    return _duct(rect_bore=False)[:6] + [
        _f("shell", "Hollow", {"thickness": t},
           refs={"remove": [{"of": "face", "side": "top"}, {"of": "face", "side": "bottom"}]})]


# ---------------------------------------------------------------------------- H6

def _enclosure(t=2.5, fillet_first=True, pilot_depth=25.0, boss_dx=50.0):
    box = _f("box", "Body", {"length": 120, "width": 80, "height": 40}, at=[0, 0, 0],
             centered=False)
    rounds = _f("fillet", "CornerRounds", {"radius": 5},
                refs={"edges": {"of": "edge", "surf": "line", "parallel": "z"}})
    shell = _f("shell", "Shell", {"thickness": t}, refs={"remove": {"of": "face", "side": "top"}})
    if fillet_first:
        ops = [box, rounds, shell]
    else:
        # shell first, then round only the OUTER vertical edges: inside corners stay sharp
        ops = [box, shell] + [
            _f("fillet", f"Round{i}", {"radius": 5},
               refs={"edges": {"of": "edge", "surf": "line", "parallel": "z",
                               "near": [x, y, 20], "count": 1}})
            for i, (x, y) in enumerate(((0, 0), (120, 0), (0, 80), (120, 80)))]
    pts = [(60 + sx * boss_dx, 40 + sy * 30) for sx in (1, -1) for sy in (1, -1)]
    n = len(ops)
    ops += [
        _f("sketch", "BossSk", {"entities": [{"type": "circle", "center": [x, y], "radius": 3}
                                             for x, y in pts], "offset": t},
           refs={"plane": "plane.top"}),
        _f("extrude", "Bosses", {"mode": "boss", "end": "blind", "depth": 30},
           refs={"profile": [{"kind": "region", "sketch": f"${n}"}]}),
    ]
    top = t + 30
    ops += [_f("hole", f"Pilot{i}", {"style": "simple", "diameter": 2.5, "end": "blind",
                                     "depth": pilot_depth, "world_points": [[x, y, top]]},
               refs={"face": {"of": "face", "surf": "flat", "near": [x, y, top], "count": 1}})
            for i, (x, y) in enumerate(pts)]
    return ops


MODEL_CASES = [
    # task, ops, expected failing checks (all others must pass)
    (H1, _tube(), set()),
    (H1, _tube(r=40.0), {"H1.straights", "H1.bends", "H1.volume"}),
    (H1, _tube(id_=22.0), {"H1.straights", "H1.bends", "H1.wall", "H1.open_ends",
                           "H1.volume"}),
    (H1, _tube(id_=0.0), {"H1.straights", "H1.bends", "H1.wall", "H1.open_ends",
                          "H1.volume"}),
    (H2, _duct(), set()),
    (H2, _duct(t=1.5), {"H2.rect_collar", "H2.round_collar", "H2.open_ends", "H2.wall"}),
    (H2, _duct(rect_bore=False), {"H2.rect_collar", "H2.open_ends", "H2.wall"}),
    (H2, _shelled_duct(), set()),
    (H2, _shelled_duct(t=1.5), {"H2.rect_collar", "H2.round_collar", "H2.open_ends", "H2.wall"}),
    (H6, _enclosure(), set()),
    # sharp inside corners are only +0.3 % volume: walls/rim/corners catch it, volume cannot
    (H6, _enclosure(fillet_first=False), {"H6.walls", "H6.floor_and_top", "H6.corners"}),
    (H6, _enclosure(pilot_depth=20.0), {"H6.pilots"}),
    (H6, _enclosure(boss_dx=45.0), {"H6.bosses", "H6.pilots"}),
]


@pytest.mark.parametrize("task, ops, failing", MODEL_CASES,
                         ids=[f"{c[0].id}-{i}" for i, c in enumerate(MODEL_CASES)])
def test_hard_part_graders(build_part, task, ops, failing):
    _assert_failing(task, _statuses(task, build_part(ops, name=f"{task.id.lower()}_case")),
                    failing)


# ---------------------------------------------------------------------------- H3

AL6061 = {"name": "aluminium 6061-T6", "E_mpa": 68900.0, "nu": 0.33, "yield_mpa": 276.0,
          "density_g_cm3": 2.70}
STEEL = {"name": "structural steel", "E_mpa": 200000.0, "nu": 0.3, "yield_mpa": 250.0,
         "density_g_cm3": 7.85}


@pytest.fixture
def arm(cad_tools, tmp_path, monkeypatch):
    """arm(b, h, hole_x=140, material=AL6061|None) -> deliverable with a solved study."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def _make(b, h, hole_x=140.0, material=AL6061):
        doc = json.loads(cad_tools["new_part"](name="h3_arm", fresh=True))["doc_id"]
        ops = [_f("box", "Arm", {"length": 150, "width": b, "height": h},
                  at=[0, -b / 2, 0], centered=False),
               _f("hole", "M5Clear", {"style": "simple", "diameter": 5.5, "end": "through_all",
                                      "world_points": [[hole_x, 0, h]]},
                  refs={"face": {"of": "face", "side": "top"}})]
        assert json.loads(cad_tools["add_features"](doc_id=doc, ops=ops)).get("ok")
        if material is not None:
            r = json.loads(cad_tools["run_fea_static"](
                fixtures=[{"of": "face", "side": "-x"}],
                loads=[{"face": {"of": "face", "side": "+x"}, "force": [0, 0, -400]}],
                material=material, doc_id=doc))
            assert r.get("ok") and r.get("saved_to_study"), r
        out = tmp_path / "deliverable.step+"
        assert json.loads(cad_tools["save_part"](path=str(out), doc_id=doc)).get("ok")
        cad_tools["close_part"](doc_id=doc, discard=True)
        return out

    return _make


H3_STUDY = {"H3.study_saved", "H3.material", "H3.fixture", "H3.load", "H3.fea_stress"}
H3_CASES = [
    ((10.0, 25.2), {}, set()),                                    # A 252 = 1.17 × A_min
    ((10.0, 24.0), {}, {"H3.deflection_limit"}),                  # delta 0.567 mm
    ((16.0, 25.2), {}, {"H3.mass"}),                              # A 403 = 1.87 × A_min
    ((10.0, 25.2), {"hole_x": 130.0}, {"H3.hole"}),
    ((10.0, 25.2), {"material": None}, H3_STUDY),
    ((10.0, 25.2), {"material": STEEL}, {"H3.material"}),         # stress is E-independent
]


@pytest.mark.parametrize("bh, kw, failing", H3_CASES,
                         ids=[f"H3-{i}" for i in range(len(H3_CASES))])
def test_h3_graders(arm, bh, kw, failing):
    _assert_failing(H3, _statuses(H3, arm(*bh, **kw)), failing)


def test_h3_minimum_section_oracle():
    """b = 8 is optimal and deflection governs: the lightest feasible arm sits on both."""
    h = hard.H3_H_DEFL
    assert hard.H3_B_MIN == 8.0 and h > hard.H3_H_STRESS
    assert math.isclose(hard._h3_delta(8.0, h), 0.5, rel_tol=1e-9)
    assert hard._h3_sigma(8.0, h) < 80.0
    assert math.isclose(hard.H3_A_MIN, 8.0 * h)
    for b in (8.5, 10.0, 14.0):   # any wider section needs more area
        hb = max(math.sqrt(4500.0 / b), (156748.9 / b) ** (1 / 3))
        assert b * hb > hard.H3_A_MIN


# ---------------------------------------------------------------------------- H4

def _leaf(sign, knuckles, reliefs, bore_r=2.1):
    """A 40×30×3 plate on the +Y (sign 1) / -Y side, relieved |y| < 6 over ``reliefs``,
    with Ø10 knuckles on the X axis over ``knuckles`` and a pin bore."""
    ops = [_f("box", "Plate", {"length": 40, "width": 30, "height": 3},
              at=[0, 0 if sign > 0 else -30, -1.5], centered=False)]
    ops += [_f("box", f"Relief{i}", {"length": 10, "width": 6, "height": 5},
               at=[x, 0 if sign > 0 else -6, -2.5], centered=False, mode="cut")
            for i, x in enumerate(reliefs)]
    ops += [_f("cylinder", f"Knuckle{i}", {"radius": 5, "height": 10}, at=[x, 0, 0],
               axis="+x", centered=False) for i, x in enumerate(knuckles)]
    ops.append(_f("cylinder", "PinBore", {"radius": bore_r, "height": 40}, at=[0, 0, 0],
                  axis="+x", centered=False, mode="cut"))
    return ops


@pytest.fixture
def hinge(cad_tools, tmp_path, monkeypatch):
    """hinge(...) -> assembly deliverable whose components are sibling part files."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def _part(name, ops):
        doc = json.loads(cad_tools["new_part"](name=name, fresh=True))["doc_id"]
        assert json.loads(cad_tools["add_features"](doc_id=doc, ops=ops)).get("ok")
        path = tmp_path / f"{name}.step+"
        assert json.loads(cad_tools["save_part"](path=str(path), doc_id=doc)).get("ok")
        cad_tools["close_part"](doc_id=doc, discard=True)
        return str(path)

    def _make(relief_b=True, pin_r=2.0, mate_axis=(1, 0, 0), mate=True):
        a = _part("leaf_a", _leaf(1, (0, 20), (10, 30)))
        b = _part("leaf_b", _leaf(-1, (10, 30), (0, 20) if relief_b else ()))
        p = _part("pin", [_f("cylinder", "Pin", {"radius": pin_r, "height": 40},
                             at=[0, 0, 0], axis="+x", centered=False)])
        # A unique name: new_assembly RESUMES an open assembly of the same name.
        asm = json.loads(cad_tools["new_assembly"](name=f"hinge_{tmp_path.name}"))["doc_id"]
        ka = json.loads(cad_tools["asm_add_component"](path=a, name="LeafA", fixed=True,
                                                       doc_id=asm))["component"]
        json.loads(cad_tools["asm_add_component"](path=p, name="Pin", fixed=True, doc_id=asm))
        kb = json.loads(cad_tools["asm_add_component"](path=b, name="LeafB", fixed=not mate,
                                                       doc_id=asm))["component"]
        if mate:
            face = {"of": "face", "side": "top", "pick": "largest"}
            m = json.loads(cad_tools["asm_mate"](
                mate_type="angle", a={"component": ka, "query": face},
                b={"component": kb, "query": face},
                value={"value": 0, "axis": list(mate_axis), "axis_point": [0, 0, 0]},
                doc_id=asm))
            assert m.get("ok"), m
        out = tmp_path / "deliverable.step+"
        assert json.loads(cad_tools["save_part"](path=str(out), doc_id=asm)).get("ok")
        cad_tools["close_part"](doc_id=asm, discard=True)
        # Grade a moved copy, as the runner does after bundling the live workspace.
        moved = tmp_path / "moved"
        shutil.copytree(tmp_path, moved, ignore=shutil.ignore_patterns("moved"))
        return moved / "deliverable.step+"

    return _make


H4_CASES = [
    ({}, set()),
    ({"relief_b": False}, {"H4.leaves", "H4.clearance"}),
    ({"mate_axis": (0, 0, 1)}, {"H4.revolute", "H4.clearance"}),
    ({"pin_r": 1.8}, {"H4.pin", "H4.pin_fit"}),
    ({"mate": False}, {"H4.revolute", "H4.clearance"}),
]


@pytest.mark.parametrize("kw, failing", H4_CASES, ids=[f"H4-{i}" for i in range(len(H4_CASES))])
def test_h4_graders(hinge, kw, failing):
    _assert_failing(H4, _statuses(H4, hinge(**kw)), failing)


def test_h4_leaf_volume_oracle(build_part):
    """The closed-form leaf volume is the one the CAD kernel builds."""
    from benchmarks.artifact_validity.evidence import build_evidence

    ev = build_evidence(build_part(_leaf(1, (0, 20), (10, 30)), name="h4_leaf"))
    assert math.isclose(ev.rpc("eval_mass")["volume_mm3"], hard.H4_LEAF_VOLUME, rel_tol=1e-6)


# ---------------------------------------------------------------------------- H5

def test_h5_fixture_is_the_declared_bytes():
    assert sha256_file(hard.FIXTURES_DIR / hard.H5_FIXTURE) == hard.H5_FIXTURE_SHA256
    assert H5.fixtures == (hard.H5_FIXTURE,)


@pytest.fixture
def h5_edit(cad_tools, tmp_path, monkeypatch):
    """edit(ops) -> Path: open a COPY of the fixture, apply ops, save deliverable.step+."""
    from services.mcp import core_bridge as cb

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def _edit(ops: list[dict]) -> Path:
        src = tmp_path / hard.H5_FIXTURE
        shutil.copyfile(hard.FIXTURES_DIR / hard.H5_FIXTURE, src)
        doc = cb.call("doc_open", {"path": str(src)})["doc_id"]
        if ops:
            r = json.loads(cad_tools["add_features"](doc_id=doc, ops=ops))
            assert r.get("ok"), r
        out = tmp_path / "deliverable.step+"
        assert json.loads(cad_tools["save_part"](path=str(out), doc_id=doc)).get("ok")
        cad_tools["close_part"](doc_id=doc, discard=True)
        return out

    return _edit


OD = {"op": "edit_feature", "id": "f1", "params": {"radius": 55}}
BOLTS = {"op": "edit_feature", "id": "f3", "params": {"count": 8, "pcd": 80}}
CHAMFER = _f("chamfer", "EdgeBreak", {"length": 1},
             refs={"edges": [{"of": "edge", "surf": "circle", "pick": "outer"}]})
NEW_BOLTS = _f("bolt_circle", "Bolts8", {"style": "simple", "diameter": 9, "count": 8,
                                         "pcd": 80, "start_angle": 0, "end": "through_all"},
               refs={"face": {"of": "face", "side": "top"}})
#: Live gpt-6-luna H5: `near` is a point ON the rim. Ranked by centroid distance it chose the +X
#: bolt-hole edge (centroid (40,0,z), 15 mm away) over the rim (centroid on the axis, 55 mm).
NEAR_CHAMFERS = [
    _f("chamfer", f"{side}Break", {"length": 1},
       refs={"edges": {"of": "edge", "side": axis, "surf": "circle", "near": [55, 0, z],
                       "count": 1}})
    for side, axis, z in (("Top", "+z", 12), ("Bottom", "-z", 0))]
H5_CASES = [
    ([OD, BOLTS, CHAMFER], set()),
    ([], {"H5.envelope", "H5.holes", "H5.pcd", "H5.spacing", "H5.chamfers",
          "H5.history_kept", "H5.volume"}),
    # the chamfers are only -0.37 % volume: H5.chamfers is the verdict
    ([OD, BOLTS], {"H5.chamfers", "H5.history_kept"}),
    ([OD, {"op": "delete_feature", "id": "f3", "cascade": True}, NEW_BOLTS, CHAMFER],
     {"H5.history_kept"}),
    ([OD, {"op": "edit_feature", "id": "f3", "params": {"count": 8}}, CHAMFER],
     {"H5.pcd", "H5.history_kept"}),
    ([OD, BOLTS, *NEAR_CHAMFERS], set()),
]


@pytest.mark.parametrize("ops, failing", H5_CASES, ids=[f"H5-{i}" for i in range(len(H5_CASES))])
def test_h5_graders(h5_edit, ops, failing):
    _assert_failing(H5, _statuses(H5, h5_edit(ops)), failing)


def test_h5_fixture_builder_reproduces_the_reference_geometry(cad_tools, tmp_path):
    """The builder that made the hashed fixture still builds the un-revised M2 flange."""
    path = build_h5_flange.build(cad_tools, tmp_path / "deliverable.step+")
    got = _statuses(H5, path)
    assert got["H5.bore"] == "pass" and got["H5.solid"] == "pass"
    assert got["H5.holes"] == "fail" and got["H5.envelope"] == "fail"


# ---------------------------------------------------------------------------- shared

def test_missing_deliverable_fails_every_hard_check(tmp_path):
    for task in (H1, H2, H3, H4, H5, H6):
        g = grade(task, tmp_path)
        assert {c["status"] for c in g["checks"]} == {"fail"}, task.id
        assert len(g["checks"]) == len(task.checks)


def test_registry_carries_hard_tasks():
    from benchmarks.artifact_validity.tasks import REGISTRY

    for t in (H1, H2, H3, H4, H5, H6):
        assert REGISTRY[t.id] is t
        assert t.final_artifact == "deliverable.step+"
        assert 1200 <= t.budget_s <= 1800

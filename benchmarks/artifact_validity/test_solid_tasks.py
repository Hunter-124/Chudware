"""S1-S3 graders against REAL parts built with the CAD tools: correct deliverables pass,
adversarial ones (wrong location, wrong face, wrong size, blind hole) fail."""
from __future__ import annotations

import pytest

from benchmarks.artifact_validity.scoring import grade
from benchmarks.artifact_validity.tasks.solids import S1, S2, S3


def _f(typ, name, params, **kw):
    return {"op": "add_feature", "feature": {"type": typ, "name": name, "params": params, **kw}}


def _box(x, y, z):
    return _f("box", "Box", {"length": x, "width": y, "height": z})


def _hole(d, pos, end="through_all", **params):
    return _f("hole", "Hole", {"style": "simple", "diameter": d, "end": end,
                               "positions": [pos], **params},
              refs={"face": {"of": "face", "side": "top"}})


def _boss(d, h, at):
    return _f("cylinder", "Boss", {"radius": d / 2, "height": h}, at=at, centered=False)


def _shell(t, side="top"):
    return _f("shell", "Hollow", {"thickness": t}, refs={"remove": {"of": "face", "side": side}})


def _statuses(task, path):
    g = grade(task, path.parent)
    return {c["id"]: c["status"] for c in g["checks"]}


CASES = [
    # task, ops, expected failing checks (all others must pass)
    (S1, [_box(90, 50, 12), _hole(10, [0, 0])], set()),
    (S1, [_box(90, 50, 12), _hole(10, [5, 0])], {"S1.hole_centre"}),
    (S1, [_box(90, 50, 12), _hole(10, [0, 0], end="blind", depth=6)],
     {"S1.hole", "S1.hole_centre", "S1.volume"}),
    # Ø9 is only 0.34 % lighter: volume stays inside its secondary 0.5 % band.
    (S1, [_box(90, 50, 12), _hole(9, [0, 0])], {"S1.hole"}),
    (S2, [_box(40, 40, 40), _boss(12, 15, [0, 0, 20])], set()),
    (S2, [_box(40, 40, 40), _boss(12, 15, [8, 0, 20])], {"S2.boss_centre"}),
    (S2, [_box(40, 40, 40), _boss(10, 15, [0, 0, 20])], {"S2.boss", "S2.top_face",
                                                          "S2.volume"}),
    (S2, [_box(40, 40, 40), _boss(12, 15, [0, 0, -35])], {"S2.boss", "S2.top_face"}),
    (S3, [_box(50, 50, 50), _shell(3)], set()),
    (S3, [_box(50, 50, 50), _shell(3, side="bottom")], {"S3.open_top", "S3.wall_thickness"}),
    (S3, [_box(50, 50, 50), _shell(2)], {"S3.wall_thickness", "S3.open_top", "S3.volume"}),
]


@pytest.mark.parametrize("task, ops, failing", CASES,
                         ids=[f"{c[0].id}-{i}" for i, c in enumerate(CASES)])
def test_solid_task_graders(build_part, task, ops, failing):
    got = _statuses(task, build_part(ops, name=f"{task.id.lower()}_case"))
    assert set(got) == {fn.check_id for fn in task.checks}
    assert {k for k, v in got.items() if v != "pass"} == failing, got
    assert all(v in ("pass", "fail") for v in got.values()), got


def test_missing_deliverable_fails_every_solid_check(tmp_path):
    for task in (S1, S2, S3):
        g = grade(task, tmp_path)
        assert {c["status"] for c in g["checks"]} == {"fail"}
        assert len(g["checks"]) == len(task.checks)

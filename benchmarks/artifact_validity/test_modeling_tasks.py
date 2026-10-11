"""M1-M5 graders against REAL parts built with the CAD tools: the reference part for each
task passes every check; adversarial parts (wrong step, clocking, count, PCD, bend radius,
thickness, hole position, counterbore face/depth, hex rotation/size, missing revolve) fail
exactly the checks they target."""
from __future__ import annotations

import math

import pytest

from benchmarks.artifact_validity.scoring import grade
from benchmarks.artifact_validity.tasks.modeling import M1, M2, M3, M4, M5


def _f(typ, name, params, **kw):
    return {"op": "add_feature", "feature": {"type": typ, "name": name, "params": params, **kw}}


def _lines(points):
    n = len(points)
    return [{"kind": "line", "p1": list(points[i]), "p2": list(points[(i + 1) % n])}
            for i in range(n)]


# ---------------------------------------------------------------------------- M1

def _shaft(step=40.0, d2=20.0, chamfer=1.0):
    """Revolved stepped shaft: Ø30 over x 0..step, Ø(d2) to x 100, end chamfer."""
    r2 = d2 / 2
    prof = [(0, 0), (0, 15), (step, 15), (step, r2), (100, r2), (100, 0)]
    ops = [_f("sketch", "Profile", {"entities": _lines(prof)}, refs={"plane": "plane.front"}),
           _f("revolve", "Shaft", {"angle": 360},
              refs={"profile": [{"kind": "region", "sketch": "$0"}], "axis": "axis.x"})]
    if chamfer:
        ops.append(_f("chamfer", "EndChamfer", {"length": chamfer},
                      refs={"edges": [{"of": "edge", "surf": "circle", "side": "+x"}]}))
    return ops


def _stacked_shaft():
    """Same geometry from two X cylinders: right part, no revolve."""
    return [_f("cylinder", "Big", {"radius": 15, "height": 40}, at=[0, 0, 0], axis="+x",
               centered=False),
            _f("cylinder", "Small", {"radius": 10, "height": 60}, at=[40, 0, 0], axis="+x",
               centered=False),
            _f("chamfer", "EndChamfer", {"length": 1},
               refs={"edges": [{"of": "edge", "surf": "circle", "side": "+x"}]})]


# ---------------------------------------------------------------------------- M2

def _flange(count=6, pcd=75.0, start=0.0, bore=40.0):
    return [_f("cylinder", "Disk", {"radius": 50, "height": 12}),
            _f("hole", "Bore", {"style": "simple", "diameter": bore, "end": "through_all"},
               refs={"face": {"of": "face", "side": "top"}}),
            _f("bolt_circle", "Bolts", {"style": "simple", "diameter": 9, "count": count,
                                        "pcd": pcd, "start_angle": start,
                                        "end": "through_all"},
               refs={"face": {"of": "face", "side": "top"}})]


# ---------------------------------------------------------------------------- M3

def _bracket(t=3.0, ri=5.0, ro=8.0, base_hole_x=30.0):
    """Two boxes, inner/outer bend fillets, a hole per leg. Hole positions are FACE-LOCAL
    from each flat's centre: base underside (u = +X) spans x ro..60, upright outer face
    (v = -Z) spans z ro..50."""
    return [
        _f("box", "Base", {"length": 60, "width": 40, "height": t}, at=[0, 0, 0],
           centered=False),
        _f("box", "Upright", {"length": t, "width": 40, "height": 50}, at=[0, 0, 0],
           centered=False),
        _f("fillet", "InnerBend", {"radius": ri},
           refs={"edges": [{"of": "edge", "surf": "line", "parallel": "y",
                            "near": [t, 20, t], "count": 1}]}),
        _f("fillet", "OuterBend", {"radius": ro},
           refs={"edges": [{"of": "edge", "surf": "line", "parallel": "y",
                            "near": [0, 20, 0], "count": 1}]}),
        _f("hole", "BaseHole", {"style": "simple", "diameter": 6.5, "end": "through_all",
                                "positions": [[base_hole_x - (ro + 60) / 2, 0]]},
           refs={"face": {"of": "face", "side": "bottom"}}),
        _f("hole", "UprightHole", {"style": "simple", "diameter": 6.5, "end": "through_all",
                                   "positions": [[0, (ro + 50) / 2 - 25]]},
           refs={"face": {"of": "face", "side": "-x"}}),
    ]


# ---------------------------------------------------------------------------- M4

def _cbore_plate(dy=20.0, depth=6.5, face="top", style="cbore"):
    params = {"style": style, "diameter": 6.6, "end": "through_all",
              "positions": [[sx * 30, sy * dy] for sx in (1, -1) for sy in (1, -1)]}
    if style == "cbore":
        params.update(cbore_d=11, cbore_depth=depth)
    return [_f("box", "Plate", {"length": 80, "width": 60, "height": 10}),
            _f("hole", "CBores", params, refs={"face": {"of": "face", "side": face}})]


# ---------------------------------------------------------------------------- M5

def _hex(af=10.0, flat_normal_deg=90.0, bore=5.0):
    """Regular hexagon, one flat normal at ``flat_normal_deg``, extruded 25 along Z."""
    r = af / math.sqrt(3.0)
    pts = [(r * math.cos(math.radians(flat_normal_deg + 30 + 60 * k)),
            r * math.sin(math.radians(flat_normal_deg + 30 + 60 * k))) for k in range(6)]
    return [_f("sketch", "Hex", {"entities": _lines(pts)}, refs={"plane": "plane.top"}),
            _f("extrude", "Prism", {"mode": "boss", "end": "blind", "depth": 25},
               refs={"profile": [{"kind": "region", "sketch": "$0"}]}),
            _f("hole", "Bore", {"style": "simple", "diameter": bore, "end": "through_all"},
               refs={"face": {"of": "face", "side": "top"}})]


def _statuses(task, path):
    g = grade(task, path.parent)
    return {c["id"]: c["status"] for c in g["checks"]}


CASES = [
    # task, ops, expected failing checks (all others must pass)
    (M1, _shaft(), set()),
    (M1, _shaft(chamfer=0), {"M1.chamfer"}),
    (M1, _shaft(step=45.0), {"M1.steps", "M1.volume"}),
    (M1, _stacked_shaft(), {"M1.revolved"}),
    (M2, _flange(), set()),
    (M2, _flange(start=30.0), {"M2.clocking"}),
    (M2, _flange(count=5), {"M2.holes", "M2.spacing", "M2.volume"}),
    (M2, _flange(pcd=80.0), {"M2.pcd"}),
    (M2, _flange(bore=35.0), {"M2.bore", "M2.volume"}),
    (M3, _bracket(), set()),
    (M3, _bracket(ro=5.0), {"M3.bend", "M3.volume"}),
    (M3, _bracket(base_hole_x=34.0), {"M3.hole_positions"}),
    (M3, _bracket(t=4.0, ro=9.0), {"M3.thickness", "M3.bend", "M3.holes", "M3.volume"}),
    (M4, _cbore_plate(), set()),
    (M4, _cbore_plate(dy=15.0), {"M4.positions"}),
    (M4, _cbore_plate(face="bottom"), {"M4.through", "M4.counterbores", "M4.floors"}),
    (M4, _cbore_plate(depth=5.0), {"M4.through", "M4.counterbores", "M4.floors",
                                   "M4.volume"}),
    (M4, _cbore_plate(style="simple"), {"M4.through", "M4.counterbores", "M4.floors",
                                        "M4.volume"}),
    (M5, _hex(), set()),
    (M5, _hex(flat_normal_deg=0.0), {"M5.envelope", "M5.flat_orientation"}),
    (M5, _hex(af=11.0), {"M5.envelope", "M5.across_flats", "M5.volume"}),
    (M5, _hex(bore=6.0), {"M5.bore", "M5.volume"}),
]


@pytest.mark.parametrize("task, ops, failing", CASES,
                         ids=[f"{c[0].id}-{i}" for i, c in enumerate(CASES)])
def test_modeling_task_graders(build_part, task, ops, failing):
    got = _statuses(task, build_part(ops, name=f"{task.id.lower()}_case"))
    assert set(got) == {fn.check_id for fn in task.checks}
    assert {k for k, v in got.items() if v != "pass"} == failing, got
    assert all(v in ("pass", "fail") for v in got.values()), got


def test_missing_deliverable_fails_every_modeling_check(tmp_path):
    for task in (M1, M2, M3, M4, M5):
        g = grade(task, tmp_path)
        assert {c["status"] for c in g["checks"]} == {"fail"}
        assert len(g["checks"]) == len(task.checks)


def test_registry_carries_modeling_tasks():
    from benchmarks.artifact_validity.tasks import REGISTRY

    for t in (M1, M2, M3, M4, M5):
        assert REGISTRY[t.id] is t
        assert t.domain == "cad" and t.final_artifact == "deliverable.step+"

"""Every common feature type still applies, through the real dispatch path.

This is the cheap canary the 2026-08-04 basic-modeling sweep was built around: it
drives the ordinary modelling repertoire — primitives, holes, counterbores,
fillet, chamfer, shell, draft, revolve, loft, linear/circular patterns, bolt
circles, material, edit/rollback, export — through
``agent_turn._dispatch_tool``, which is the path the agent actually uses
(``_bind_tool`` skips ``_reconcile_kwargs`` and hides argument bugs).

Run before spending a token on a live model: three of that sweep's findings were
visible here for free — a tool advertised but unbindable, ``linear_pattern``
rejecting ``direction1``, and ``bolt_circle`` reachable only through the literal
key ``pcd``.

Assertions are on geometry, not on "the call returned": a feature that reports
``ok`` while removing no material is exactly the failure this is meant to catch.
"""
from __future__ import annotations

import json
import math

import pytest

from services.agent import agent_turn as at


def _call(tool: str, /, **kwargs) -> dict:
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve in the MCP registry"
    bound, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    assert err is None, f"{tool} rejected {sorted(kwargs)}: {err}"
    out = fn(**bound)
    return json.loads(out) if isinstance(out, str) else out


def _apply(doc: str, *features: dict) -> dict:
    out = _call("add_features", doc_id=doc,
                ops=[{"op": "add_feature", "feature": f} for f in features])
    assert out.get("ok") is True, out
    for f in out.get("features") or []:
        assert str(f.get("status") or "ok") in ("ok", "warning"), out
    return out


def _volume(doc: str) -> float:
    mp = _call("measure_part", doc_id=doc)
    vol = (mp.get("mass") or {}).get("volume_mm3")
    assert isinstance(vol, (int, float)), mp
    return float(vol)


def _manifold(doc: str) -> float:
    bat = _call("verify_battery", doc_id=doc, checks=["is_manifold"])
    return (bat.get("scores") or {}).get("is_manifold")


def _new(name: str) -> str:
    return _call("new_part", name=name, fresh=True)["doc_id"]


def test_a_plate_survives_hole_counterbore_fillet_and_chamfer():
    doc = _new("rep_plate")
    _apply(doc, {"type": "box", "name": "Plate",
                 "params": {"length": 80, "width": 60, "height": 10}})
    assert _volume(doc) == pytest.approx(48000.0, rel=1e-6)

    _apply(doc, {"type": "hole", "name": "Bore",
                 "params": {"diameter": 12, "end": "through_all"},
                 "refs": {"face": {"of": "face", "side": "top"}}})
    # pi * 6^2 * 10 of material gone, exactly.
    assert _volume(doc) == pytest.approx(48000.0 - math.pi * 36 * 10, rel=1e-4)

    before = _volume(doc)
    _apply(doc, {"type": "hole", "name": "CBore",
                 "params": {"diameter": 6, "style": "cbore", "cbore_d": 11,
                            "cbore_depth": 4, "end": "through_all"},
                 "refs": {"face": {"of": "face", "side": "top"}},
                 "at": [25, 0, 0]})
    assert _volume(doc) < before, "the counterbore removed nothing"

    before = _volume(doc)
    _apply(doc, {"type": "fillet", "name": "Round", "params": {"radius": 3},
                 "refs": {"edges": {"of": "edge", "surf": "line", "parallel": "z"}}})
    _apply(doc, {"type": "chamfer", "name": "Break", "params": {"length": 1},
                 "refs": {"edges": {"of": "edge", "surf": "line", "parallel": "x"}}})
    assert _volume(doc) < before
    assert _manifold(doc) == 1.0

    for fmt in ("step", "stl"):
        out = _call("export_part", doc_id=doc, fmt=fmt)
        assert out.get("path"), (fmt, out)


def test_a_revolve_builds_the_ring_its_profile_describes():
    doc = _new("rep_revolve")
    _apply(doc,
           {"type": "sketch", "name": "Prof", "refs": {"plane": "plane.front"},
            "params": {"entities": [
                {"kind": "line", "p1": [10, 0], "p2": [25, 0]},
                {"kind": "line", "p1": [25, 0], "p2": [25, 8]},
                {"kind": "line", "p1": [25, 8], "p2": [10, 8]},
                {"kind": "line", "p1": [10, 8], "p2": [10, 0]}]}},
           {"type": "revolve", "name": "Ring", "params": {"angle": 360},
            "refs": {"profile": [{"kind": "region", "sketch": "$0"}],
                     "axis": "axis.z"}})
    # A 15 x 8 rectangle from r=10 to r=25, swept: pi * (25^2 - 10^2) * 8.
    assert _volume(doc) == pytest.approx(math.pi * (625 - 100) * 8, rel=1e-3)
    assert _manifold(doc) == 1.0


def test_a_loft_between_two_circles_is_a_frustum():
    doc = _new("rep_loft")
    _apply(doc,
           {"type": "sketch", "name": "S1", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"kind": "circle", "center": [0, 0], "radius": 20}]}},
           {"type": "sketch", "name": "S2", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"kind": "circle", "center": [0, 0], "radius": 8}],
                       "offset": 40}},
           {"type": "loft", "name": "Cone",
            "refs": {"sections": [{"kind": "region", "sketch": "$0"},
                                  {"kind": "region", "sketch": "$1"}]}})
    # Frustum: pi*h/3 * (R^2 + R*r + r^2).
    assert _volume(doc) == pytest.approx(math.pi * 40 / 3 * (400 + 160 + 64), rel=1e-2)
    assert _manifold(doc) == 1.0


def test_a_shell_leaves_the_wall_it_was_asked_for():
    doc = _new("rep_shell")
    _apply(doc, {"type": "box", "name": "Body",
                 "params": {"length": 60, "width": 40, "height": 30}})
    _apply(doc, {"type": "shell", "name": "Hollow", "params": {"thickness": 2},
                 "refs": {"remove": {"of": "face", "side": "top"}}})
    # 60x40x30 hollowed to 2 mm with the top open: outer minus the 56x36x28 void.
    assert _volume(doc) == pytest.approx(72000 - 56 * 36 * 28, rel=1e-3)
    assert _manifold(doc) == 1.0
    wt = _call("wall_thickness", doc_id=doc)
    assert wt.get("ok") is not False, wt


def test_a_draft_tapers_the_face_it_names():
    doc = _new("rep_draft")
    _apply(doc, {"type": "box", "name": "Body",
                 "params": {"length": 60, "width": 40, "height": 30}})
    before = _volume(doc)
    _apply(doc, {"type": "draft", "name": "Draft", "params": {"angle": 2},
                 "refs": {"faces": {"of": "face", "side": "front"},
                          "neutral": {"of": "face", "side": "bottom"}}})
    assert _volume(doc) != pytest.approx(before, rel=1e-6), "the draft changed nothing"
    assert _manifold(doc) == 1.0


def test_a_two_way_linear_pattern_drills_every_instance():
    doc = _new("rep_pattern")
    _apply(doc, {"type": "box", "name": "Base",
                 "params": {"length": 120, "width": 120, "height": 8}})
    _apply(doc, {"type": "hole", "name": "Seed",
                 "params": {"diameter": 5, "end": "through_all"},
                 "refs": {"face": {"of": "face", "side": "top"}},
                 "at": [-40, -40, 0]})
    _apply(doc, {"type": "linear_pattern", "name": "Grid",
                 "params": {"count1": 3, "spacing1": 35,
                            "count2": 2, "spacing2": 40,
                            "direction1": [1, 0, 0], "direction2": [0, 1, 0]},
                 "refs": {"features": ["Seed"]}})
    bores = _call("query_geometry", doc_id=doc, query={"of": "face", "surf": "cylinder"})
    assert len(bores.get("matches") or []) == 6, bores
    xs = sorted({round(m["centroid"][0], 3) for m in bores["matches"]})
    ys = sorted({round(m["centroid"][1], 3) for m in bores["matches"]})
    assert xs == [-40.0, -5.0, 30.0], xs
    assert ys == [-40.0, 0.0], ys


def test_a_bolt_circle_lands_its_holes_on_the_pitch_circle():
    doc = _new("rep_bolts")
    _apply(doc, {"type": "cylinder", "name": "Disc",
                 "params": {"radius": 50, "height": 10}})
    _apply(doc, {"type": "bolt_circle", "name": "BC",
                 "params": {"diameter": 6, "count": 6, "pcd": 60,
                            "end": "through_all"},
                 "refs": {"face": {"of": "face", "side": "top"}}})
    bores = [m for m in _call("query_geometry", doc_id=doc,
                              query={"of": "face", "surf": "cylinder"})["matches"]
             if abs(m["radius"] - 3.0) < 0.05]
    assert len(bores) == 6, bores
    for m in bores:
        cx, cy, _cz = m["centroid"]
        assert math.hypot(cx, cy) == pytest.approx(30.0, abs=1e-2), m


def test_mass_follows_the_material_you_name_not_the_one_you_coloured():
    """set_material is the VISUAL material — it does NOT change what the part weighs.

    measure_part used to refuse material= with "use set_material instead", which is a
    round trip back to a tool that cannot answer: an aluminium-coloured bar still
    reported a steel mass, silently.
    """
    doc = _new("rep_material")
    _apply(doc, {"type": "box", "name": "Bar",
                 "params": {"length": 120, "width": 20, "height": 10}})  # 24 cm^3
    _call("set_material", doc_id=doc, material="aluminum")
    steel = _call("measure_part", doc_id=doc)["mass"]
    assert steel["mass_g"] == pytest.approx(24.0 * 7.85, rel=1e-3), steel

    alu = _call("measure_part", doc_id=doc, material="aluminum")["mass"]
    assert alu["material"]["density_g_cm3"] == pytest.approx(2.7, rel=1e-2), alu
    assert alu["mass_g"] == pytest.approx(24.0 * 2.7, rel=1e-2), alu

    # An explicit number still beats a name.
    both = _call("measure_part", doc_id=doc, material="aluminum", density_g_cm3=7.85)["mass"]
    assert both["mass_g"] == pytest.approx(24.0 * 7.85, rel=1e-3), both


def test_the_readback_tools_answer_on_a_real_body():
    doc = _new("rep_readback")
    _apply(doc, {"type": "box", "name": "Blank",
                 "params": {"length": 50, "width": 50, "height": 20}})
    assert _call("describe_part", doc_id=doc).get("ok") is not False
    assert _call("check_geometry_health", doc_id=doc)["watertight"] is True
    assert _call("render_ascii", doc_id=doc).get("ok") is not False
    assert _call("save_part", doc_id=doc).get("ok") is not False
    # inspect_part / measure return [image, caption] for vision and non-vision models.
    shot = _call("inspect_part", doc_id=doc, target="top")
    assert isinstance(shot, list) and len(shot) == 2, shot

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


def test_a_plate_survives_hole_counterbore_fillet_and_chamfer(monkeypatch):
    from services.agent.providers.base import ProviderUnreachable
    from services.mcp import core_bridge as cb
    from services.mcp import regen_tools

    # Exercise native rejection without starting a paid/live helper turn.
    def no_provider(*_args):
        raise ProviderUnreachable("provider intentionally unavailable")

    monkeypatch.setattr(regen_tools, "_resolve_provider", no_provider)
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
    tree_before = cb.call("doc_tree", {"doc_id": doc})
    # The broad vertical-line query includes cylindrical hole seams. An explicit
    # seam is unsupported, so no part of this selection may be rounded.
    broad = _call("add_features", doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "fillet", "name": "Broad Round", "params": {"radius": 3},
        "refs": {"edges": {"of": "edge", "surf": "line", "parallel": "z"}},
    }}])
    assert broad["ok"] is False and broad["rolled_back"] is True, broad
    failed = next(f for f in broad["failed_features"] if f["name"] == "Broad Round")
    assert failed["error"] and "seam" in failed["error"].lower()
    assert cb.call("doc_tree", {"doc_id": doc}) == tree_before
    assert _volume(doc) == pytest.approx(before, rel=1e-12)

    # The intended rounds are the four OUTSIDE vertical plate corners, not the
    # seam edges inside either hole. Verify each position before selecting all four.
    corners = [(x, y, 0) for x in (-40, 40) for y in (-30, 30)]
    edges = [{"of": "edge", "surf": "line", "parallel": "z",
              "near": list(corner), "count": 1} for corner in corners]
    for corner, query in zip(corners, edges):
        match = _call("query_geometry", doc_id=doc, query=query)
        assert match["ok"] and match["count"] == 1, match
        assert match["matches"][0]["centroid"] == pytest.approx(corner, abs=1e-6)
    rounded = _apply(doc, {"type": "fillet", "name": "Round", "params": {"radius": 3},
                          "refs": {"edges": edges}})
    report = next(f for f in rounded["features"] if f["name"] == "Round")["data"]["fillet"]
    assert report["requested"]["radius"] == report["effective"]["radius"] == 3
    assert report["selected_edges"] == report["covered_edges"] == len(corners)
    round_volume = _volume(doc)
    assert round_volume == pytest.approx(
        before - 4 * (1 - math.pi / 4) * 3**2 * 10, rel=1e-6)

    _apply(doc, {"type": "chamfer", "name": "Break", "params": {"length": 1},
                 "refs": {"edges": {"of": "edge", "surf": "line", "parallel": "x"}}})
    assert _volume(doc) < round_volume
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
    wt = _call("wall_thickness", doc_id=doc, image=False)
    assert wt.get("ok") is not False, wt


def _count(doc: str, query: dict) -> int:
    return len(_call("query_geometry", doc_id=doc, query=query).get("matches") or [])


@pytest.mark.parametrize("extra", [{}, {"kind": "face"}], ids=["query", "kind_face_query"])
def test_a_shell_opened_top_and_bottom_is_a_tube(extra):
    """refs.remove takes a LIST: each query element resolves like the bare form.

    A two-query list used to reach the topology-selector scorer ("remove[0]: no candidates
    of matching kind"); with "kind":"face" added it leaked a raw
    "[json.exception.out_of_range.403] key 'centroid' not found".
    """
    doc = _new("rep_tube")
    _apply(doc, {"type": "box", "name": "Body",
                 "params": {"length": 60, "width": 40, "height": 30}})
    _apply(doc, {"type": "shell", "name": "Tube", "params": {"thickness": 2},
                 "refs": {"remove": [{**extra, "of": "face", "side": "top"},
                                     {**extra, "of": "face", "side": "bottom"}]}})
    # Open both ends: 60x40 outer minus the 56x36 bore, full 30 mm height.
    assert _volume(doc) == pytest.approx(60 * 40 * 30 - 56 * 36 * 30, rel=1e-6)
    assert _manifold(doc) == 1.0
    # Four walls, each an outer + an inner face; the only z-facing faces are the two rims.
    assert _count(doc, {"of": "face", "surf": "plane", "perpendicular": "z"}) == 8
    assert _count(doc, {"of": "face", "parallel": "z"}) == 2


def _spline_cup(doc: str) -> None:
    _apply(doc,
           {"type": "sketch", "name": "Prof", "refs": {"plane": "plane.front"},
            "params": {"entities": [
                {"type": "line", "p1": [0, 0], "p2": [30, 0]},
                {"type": "spline", "points": [[30, 0], [38, 20], [34, 40], [40, 60]]},
                {"type": "line", "p1": [40, 60], "p2": [0, 60]},
                {"type": "line", "p1": [0, 60], "p2": [0, 0]}]}},
           {"type": "revolve", "name": "Cup", "params": {"angle": 360, "mode": "boss"},
            "refs": {"profile": [{"kind": "region", "sketch": "$0"}], "axis": "axis.z"}})


def test_a_revolved_freeform_cup_shells_when_the_offset_validates():
    """A spline-revolve (SurfaceOfRevolution) body is freeform, yet its offset validates —
    so it shells instead of hitting the freeform refusal."""
    doc = _new("rep_cup")
    _spline_cup(doc)
    faces = _call("query_geometry", doc_id=doc, query={"of": "face"})["matches"]
    assert any(m["surf"] not in ("plane", "cylinder", "cone") for m in faces), faces
    top = _call("query_geometry", doc_id=doc, query={"of": "face", "side": "top"})["matches"]
    kept_area = sum(m["area_or_len"] for m in faces) - sum(m["area_or_len"] for m in top)
    _apply(doc, {"type": "shell", "name": "Hollow", "params": {"thickness": 2},
                 "refs": {"remove": [{"of": "face", "side": "top"}]}})
    assert _manifold(doc) == 1.0
    # A parallel inward offset of this (mostly convex) skin: the wall volume lies between
    # t x (inner wall area) and t x (outer kept area) — a no-op, a solid block or a
    # lost face all land outside that bracket.
    shelled = _call("query_geometry", doc_id=doc, query={"of": "face"})["matches"]
    rim = _call("query_geometry", doc_id=doc, query={"of": "face", "side": "top"})["matches"]
    inner_area = (sum(m["area_or_len"] for m in shelled)
                  - sum(m["area_or_len"] for m in rim) - kept_area)
    assert 0 < 2 * inner_area < _volume(doc) < 2 * kept_area, (inner_area, kept_area)


def _loft_duct(name: str) -> str:
    doc = _new(name)
    _apply(doc,
           {"type": "sketch", "name": "Rect", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"type": "rect", "center": [0, 0],
                                     "width": 100, "height": 60}]}},
           {"type": "extrude", "name": "Base", "params": {"distance": 10},
            "refs": {"profile": [{"kind": "region", "sketch": "$0"}]}},
           {"type": "sketch", "name": "RectTop", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"type": "rect", "center": [0, 0],
                                     "width": 100, "height": 60}], "offset": 10}},
           {"type": "sketch", "name": "Round", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"type": "circle", "center": [0, 0], "radius": 40}],
                       "offset": 130}},
           {"type": "loft", "name": "Duct",
            "refs": {"sections": [{"kind": "region", "sketch": "$2"},
                                  {"kind": "region", "sketch": "$3"}]}},
           {"type": "extrude", "name": "Collar", "params": {"distance": 10},
            "refs": {"profile": [{"kind": "region", "sketch": "$3"}]}})
    return doc


_OPEN_ENDS = [{"of": "face", "side": "top"}, {"of": "face", "side": "bottom"}]


def test_a_rect_to_circle_loft_duct_shells_open_at_both_ends():
    """OCCT's offset refuses this body outright: a rectangle-to-circle loft's corner seams run
    from a 90° crease at the rectangle to G1 tangency at the circle
    (BRepOffset_MixedConnectivity, every join type and tolerance). The shell still builds —
    the sectional builder hollows the duct — and the wall it leaves is the requested one."""
    doc = _loft_duct("rep_duct")
    faces = _call("query_geometry", doc_id=doc, query={"of": "face"})["matches"]
    ends = sum(m["area_or_len"] for q in _OPEN_ENDS
               for m in _call("query_geometry", doc_id=doc, query=q)["matches"])
    kept_area = sum(m["area_or_len"] for m in faces) - ends
    _apply(doc, {"type": "shell", "name": "Hollow", "params": {"thickness": 2},
                 "refs": {"remove": _OPEN_ENDS}})
    assert _manifold(doc) == 1.0
    # Both ends open: the only faces across the axis are the two 2 mm rims, 100x60 - 96x56
    # at the bottom and the Ø80/Ø76 annulus at the top.
    rims = sorted(m["area_or_len"] for m in _call(
        "query_geometry", doc_id=doc, query={"of": "face", "surf": "plane", "parallel": "z"}
    )["matches"])
    assert rims == pytest.approx([math.pi * (40 ** 2 - 38 ** 2), 100 * 60 - 96 * 56], rel=1e-3)
    # A 2 mm wall on (mostly convex) skin: between t x inner area and t x outer kept area.
    shelled = _call("query_geometry", doc_id=doc, query={"of": "face"})["matches"]
    inner_area = sum(m["area_or_len"] for m in shelled) - sum(rims) - kept_area
    assert 0 < 2 * inner_area < _volume(doc) < 2 * kept_area, (inner_area, kept_area)


def test_an_outward_shell_of_the_loft_duct_still_refuses_by_name():
    """The sectional builder hollows inward only; an outward shell keeps the named kernel
    refusal, locates the seams, and leaves the duct intact — never an invalid body."""
    doc = _loft_duct("rep_duct_out")
    before = _volume(doc)
    out = _call("add_features", doc_id=doc, ops=[{"op": "add_feature", "feature":
        {"type": "shell", "name": "Hollow", "params": {"thickness": 2, "outward": True},
         "refs": {"remove": _OPEN_ENDS}}}])
    assert out.get("ok") is False, out
    err = out.get("error") or ""
    assert "shell is not supported on this lofted" in err, err
    assert "BRepOffset_MixedConnectivity" in err, err
    assert "4 edge(s) run from a crease into tangency" in err, err
    assert _volume(doc) == pytest.approx(before, rel=1e-9)
    assert _manifold(doc) == 1.0


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


def test_mass_uses_the_material_applied_to_the_model_unless_one_is_passed():
    """The applied material sets mass; a passed name or density overrides it for the readout."""
    doc = _new("rep_material")
    _apply(doc, {"type": "box", "name": "Bar",
                 "params": {"length": 120, "width": 20, "height": 10}})  # 24 cm^3
    _call("set_material", doc_id=doc, material="PLA")
    pla = _call("measure_part", doc_id=doc)["mass"]
    assert pla["material"]["source"] == "document_material", pla
    assert pla["mass_g"] == pytest.approx(24.0 * 1.24, rel=1e-3), pla

    alu = _call("measure_part", doc_id=doc, material="aluminum")["mass"]
    assert alu["material"]["source"] == "explicit", alu
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


def _dxf_polyline_layers(path):
    """Count actual DXF entities, not just the layer table declarations."""
    lines = path.read_text(encoding="utf-8").splitlines()
    tags = [(lines[i].strip(), lines[i + 1].strip())
            for i in range(0, len(lines) - 1, 2)]
    counts = {}
    for i, (code, value) in enumerate(tags):
        if code == "0" and value in ("POLYLINE", "LWPOLYLINE"):
            for c, layer in tags[i + 1:]:
                if c == "0":
                    break
                if c == "8":
                    counts[layer] = counts.get(layer, 0) + 1
                    break
    return counts

def _offline_regen(monkeypatch):
    from services.agent.providers.base import ProviderUnreachable
    from services.mcp import regen_tools

    def no_provider(*_args):
        raise ProviderUnreachable("provider intentionally unavailable")

    monkeypatch.setattr(regen_tools, "_resolve_provider", no_provider)



def test_dogfood_rib_and_sheetmetal_development(tmp_path, monkeypatch):
    """Exercise the reported gusset and Z bracket through the real registry and cache."""
    from services.mcp import core_bridge as cb
    _offline_regen(monkeypatch)

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    docs = []
    try:
        rib_doc = _new("dogfood_rib")
        docs.append(rib_doc)
        points = [(0, 0), (35, 0), (35, 3), (3, 3), (3, 38), (0, 38)]
        profile = [{"id": f"e{i + 1}", "type": "line",
                    "p1": list(points[i]), "p2": list(points[(i + 1) % len(points)])}
                   for i in range(len(points))]
        _apply(rib_doc,
               {"type": "sketch", "name": "L profile",
                "refs": {"plane": "plane.right"},
                "params": {"offset": -36, "entities": profile}},
               {"type": "extrude", "name": "L bracket",
                "params": {"mode": "new", "end": "blind", "depth": 72},
                "refs": {"profile": "$0"}})
        sketch = _apply(rib_doc, {"type": "sketch", "name": "Open rib",
                                  "refs": {"plane": "plane.right"},
                                  "params": {"offset": 31, "entities": [
                                      {"type": "line", "p1": [3, 20], "p2": [18, 3]}]}})
        sketch_id = sketch["id_map"]["$0"]
        before_tree = cb.call("doc_tree", {"doc_id": rib_doc})
        before_bbox = cb.call("doc_bbox", {"doc_id": rib_doc})
        rib = _call("add_features", doc_id=rib_doc, ops=[
            {"op": "add_feature", "feature": {"type": "rib", "name": "Gusset",
             "params": {"thickness": 3}, "refs": {"profile": sketch_id}}}])
        if rib.get("ok"):
            after_bbox = cb.call("doc_bbox", {"doc_id": rib_doc})
            for axis in range(3):
                assert after_bbox["min"][axis] >= before_bbox["min"][axis] - 1e-4
                assert after_bbox["max"][axis] <= before_bbox["max"][axis] + 1e-4
            health = _call("check_geometry_health", doc_id=rib_doc)
            assert health["sound"] and health["body_count"] == 1, health
            assert _call("measure_part", doc_id=rib_doc)["mass"]["body_count"] == 1
            attachment = cb.call("eval_feature_attachment",
                                 {"doc_id": rib_doc, "features": [rib["id_map"]["$0"]]})
            assert attachment["ok"] and not attachment["flagged"], attachment
        else:
            after_tree = cb.call("doc_tree", {"doc_id": rib_doc})
            assert after_tree["revision"] == before_tree["revision"]
            assert after_tree["tree"] == before_tree["tree"]
            assert "rib" in str(rib).lower()

        doc = _new("dogfood_z_bracket")
        docs.append(doc)
        _apply(doc,
               {"type": "sketch", "name": "Base profile", "refs": {"plane": "plane.top"},
                "params": {"entities": [{"type": "rectangle", "center": [0, 0],
                                           "width": 60, "height": 40}]}},
               {"type": "sm_base_flange", "name": "Base",
                "params": {"thickness": 1.5, "default_bend_radius": 1.5},
                "refs": {"profile": "$0"}})
        no_flat = tmp_path / "no-flat.dxf"
        refusal = _call("export_flat_dxf", doc_id=doc, path=str(no_flat))
        assert not refusal["ok"] and "flat pattern" in str(refusal).lower(), refusal
        assert not no_flat.exists()

        for name, near, length in (("Web", [30, 0, 1.5], 25),
                                   ("Top", [30, 0, 28], 30)):
            matches = _call("query_geometry", doc_id=doc, query={
                "of": "edge", "surf": "line", "near": near, "count": 1})["matches"]
            assert len(matches) == 1, matches
            _apply(doc, {"type": "sm_edge_flange", "name": name,
                         "params": {"length": length, "angle": 90, "radius": 1.5},
                         "refs": {"edge": matches[0]}})
        cylinders = _call("query_geometry", doc_id=doc,
                          query={"of": "face", "surf": "cylinder"})["matches"]
        bends = [face for face in cylinders if face.get("radius") is not None
                 and face["radius"] == pytest.approx(1.5, abs=1e-5)]
        assert len(bends) >= 2, cylinders
        base_face = _call("query_geometry", doc_id=doc, query={
            "of": "face", "surf": "flat", "near": [0, 0, 1.5], "count": 1})["matches"][0]
        _apply(doc, {"type": "hole", "name": "Four holes",
                     "params": {"style": "simple", "standard": "custom", "diameter": 5,
                                "end": "through_all",
                                "world_points": [[-20, -12, 1.5], [-20, 12, 1.5],
                                                 [15, -12, 1.5], [15, 12, 1.5]]},
                     "refs": {"face": base_face}})

        # Develop an otherwise identical copy without the relief as a control.
        control_path = tmp_path / "without-relief.step+"
        cb.call("doc_save", {"doc_id": doc, "path": str(control_path)})
        control = cb.call("doc_open", {"path": str(control_path), "detach_path": True})
        control_doc = control["doc_id"]
        docs.append(control_doc)
        assert control["cache"]["features_executed"] == 0, control
        _apply(control_doc, {"type": "sm_flat_pattern", "name": "Flatten"})
        control_export = _call("export_flat_dxf", doc_id=control_doc,
                               path=str(tmp_path / "control.dxf"))
        assert control_export["ok"], control_export

        _apply(doc, {"type": "box", "name": "Bend relief",
                     "params": {"length": 6, "width": 4, "height": 6},
                     "at": [29, 19, 2], "mode": "cut"})
        folded_path = tmp_path / "folded.step+"
        saved = cb.call("doc_save", {"doc_id": doc, "path": str(folded_path)})
        assert saved["cache"]["checkpoints"]["full_result"], saved
        folded_bbox = cb.call("doc_bbox", {"doc_id": doc})
        # add_features refreshes the bound project file. Move the live document
        # to a separate working file so the saved folded checkpoint stays folded.
        cb.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "working.step+")})
        _apply(doc, {"type": "sm_flat_pattern", "name": "Flatten"})
        bbox = cb.call("doc_bbox", {"doc_id": doc})
        allowance = math.pi / 2 * (1.5 + 0.45 * 1.5)
        dimensions = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        assert dimensions == pytest.approx([60 + 25 + 30 + 2 * (allowance - 3),
                                            40, 1.5], abs=1e-3)
        flat_path = tmp_path / "bracket-flat.dxf"
        exported = _call("export_flat_dxf", doc_id=doc, path=str(flat_path))
        assert exported["ok"] and flat_path.stat().st_size > 0, exported
        layers = _dxf_polyline_layers(flat_path)
        assert layers.get("OUTLINE") == 1 and layers.get("BEND", 0) >= 4, layers
        assert layers.get("CUTS") == 4, layers
        assert exported["flat_area"] < control_export["flat_area"] - 1.0, (
            exported, control_export)
        # A document saved *after* flatten must export directly from its saved
        # flat metadata and shape; it must not re-execute the feature tree.
        saved_flat_path = tmp_path / "saved-flat.step+"
        saved_flat = cb.call("doc_save", {"doc_id": doc, "path": str(saved_flat_path)})
        assert saved_flat["cache"]["checkpoints"]["full_result"], saved_flat
        reopened_flat = cb.call("doc_open", {"path": str(saved_flat_path), "detach_path": True})
        docs.append(reopened_flat["doc_id"])
        assert reopened_flat["cache"]["features_executed"] == 0, reopened_flat
        restored_path = tmp_path / "saved-flat.dxf"
        executed = cb.call("prof_contention", {})["compile_cache"]["features_executed"]
        restored = _call("export_flat_dxf", doc_id=reopened_flat["doc_id"],
                         path=str(restored_path))
        assert cb.call("prof_contention", {})["compile_cache"]["features_executed"] == executed
        assert restored["ok"] and restored["flat_area"] == pytest.approx(exported["flat_area"], abs=1e-5)
        assert _dxf_polyline_layers(restored_path).get("CUTS") == 4
        stl = _call("export_part", doc_id=doc, fmt="stl", path=str(tmp_path / "flat.stl"))
        assert stl["ok"] and stl["validated"] is False, stl
        stages = stl["validation_stages"]
        assert stages["serialized_facet_collapse"]["status"] == "passed"
        assert stages["source_solid_facet_incidence_and_orientation"]["status"] == "passed"
        assert stages["brep_round_trip"]["status"] == "skipped"
        assert "no B-rep round-trip" in stages["brep_round_trip"]["reason"]

        reopened = cb.call("doc_open", {"path": str(folded_path), "detach_path": True})
        docs.append(reopened["doc_id"])
        assert reopened["cache"]["features_executed"] == 0, reopened
        restored_tree = cb.call("doc_tree", {"doc_id": reopened["doc_id"]})["tree"]
        assert not any(f["type"] == "sm_flat_pattern" for f in restored_tree["features"])
        restored_bbox = cb.call("doc_bbox", {"doc_id": reopened["doc_id"]})
        assert restored_bbox["min"] == pytest.approx(folded_bbox["min"], abs=1e-5)
        assert restored_bbox["max"] == pytest.approx(folded_bbox["max"], abs=1e-5)
        _apply(reopened["doc_id"], {"type": "sm_flat_pattern", "name": "Flatten"})
        new_bbox = cb.call("doc_bbox", {"doc_id": reopened["doc_id"]})
        assert new_bbox["min"] == pytest.approx(bbox["min"], abs=1e-5)
        assert new_bbox["max"] == pytest.approx(bbox["max"], abs=1e-5)
        second_path = tmp_path / "reopened-flat.dxf"
        executed_before = cb.call("prof_contention", {})["compile_cache"]["features_executed"]
        second = _call("export_flat_dxf", doc_id=reopened["doc_id"], path=str(second_path))
        executed_after = cb.call("prof_contention", {})["compile_cache"]["features_executed"]
        assert executed_after == executed_before, "flat export re-resolved the restored part"
        assert second["ok"] and second["flat_area"] == pytest.approx(exported["flat_area"], abs=1e-5)
        assert _dxf_polyline_layers(second_path).get("CUTS") == 4
    finally:
        for doc_id in reversed(docs):
            cb.call("doc_close", {"doc_id": doc_id})


@pytest.mark.parametrize("spacing,overlaps", [(12, False), (4, True)])
def test_pattern_overlap_excludes_only_its_seed(spacing, overlaps, tmp_path):
    """A pattern must not intersect itself, but real copy/seed overlap is retained."""
    from services.mcp import core_bridge as cb

    doc = _new(f"pattern_overlap_{spacing}")
    reopened_id = None
    try:
        _apply(doc, {"type": "box", "name": "Plate",
                     "params": {"length": 60, "width": 40, "height": 4}})
        seed = _apply(doc, {"type": "hole", "name": "Seed",
                            "params": {"diameter": 8, "end": "through_all"},
                            "refs": {"face": {"of": "face", "side": "top"}}})["id_map"]["$0"]
        pattern = _apply(doc, {"type": "linear_pattern", "name": "Copies",
                               "params": {"count1": 2, "spacing1": spacing,
                                          "direction1": [1, 0, 0]},
                               "refs": {"features": [seed]}})["id_map"]["$0"]
        result = cb.call("eval_feature_overlap", {"doc_id": doc, "features": [pattern]})
        assert result["complete"] and result["basis"] == "pre_boolean_tool_intersection", result
        pairs = [pair for pair in result["pairs"]
                 if {pair["a"], pair["b"]} == {seed, pattern}]
        assert bool(pairs) is overlaps, result
        if pairs:
            assert pairs[0]["tool_kind"] == "cut_tool_overlap"
        path = tmp_path / "pattern.step+"
        cb.call("doc_save", {"doc_id": doc, "path": str(path)})
        reopened = cb.call("doc_open", {"path": str(path), "detach_path": True})
        reopened_id = reopened["doc_id"]
        assert reopened["cache"]["features_executed"] == 0, reopened
        restored = cb.call("eval_feature_overlap", {"doc_id": reopened_id,
                                                     "features": [pattern]})
        assert restored["complete"], restored
        restored_pairs = [pair for pair in restored["pairs"]
                          if {pair["a"], pair["b"]} == {seed, pattern}]
        assert bool(restored_pairs) is overlaps, restored
    finally:
        if reopened_id:
            cb.call("doc_close", {"doc_id": reopened_id})
        cb.call("doc_close", {"doc_id": doc})


@pytest.mark.parametrize("angle,overlaps", [(360, False), (40, True)])
def test_circular_pattern_overlap_excludes_only_its_seed(angle, overlaps):
    from services.mcp import core_bridge as cb

    doc = _new(f"circular_pattern_overlap_{angle}")
    try:
        _apply(doc, {"type": "box", "name": "Plate",
                     "params": {"length": 60, "width": 40, "height": 4}})
        seed = _apply(doc, {"type": "hole", "name": "Seed",
                            "params": {"diameter": 8, "end": "through_all",
                                       "positions": [[10, 0]]},
                            "refs": {"face": {"of": "face", "side": "top"}}})["id_map"]["$0"]
        pattern = _apply(doc, {"type": "circular_pattern", "name": "Copies",
                               "params": {"count": 2, "angle": angle},
                               "refs": {"axis": "axis.z", "features": [seed]}})["id_map"]["$0"]
        result = cb.call("eval_feature_overlap", {"doc_id": doc, "features": [pattern]})
        assert result["complete"] and result["basis"] == "pre_boolean_tool_intersection", result
        pairs = [pair for pair in result["pairs"]
                 if {pair["a"], pair["b"]} == {seed, pattern}]
        assert bool(pairs) is overlaps, result
        if pairs:
            assert pairs[0]["tool_kind"] == "cut_tool_overlap"
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_flat_development_preserves_a_hole_in_the_bent_wall(tmp_path, monkeypatch):
    from services.mcp import core_bridge as cb
    _offline_regen(monkeypatch)

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = _new("bent_wall_hole")
    try:
        _apply(doc,
               {"type": "sketch", "name": "Base profile", "refs": {"plane": "plane.top"},
                "params": {"entities": [{"type": "rectangle", "center": [0, 0],
                                           "width": 60, "height": 40}]}},
               {"type": "sm_base_flange", "name": "Base",
                "params": {"thickness": 1.5, "default_bend_radius": 1.5},
                "refs": {"profile": "$0"}})
        edge = _call("query_geometry", doc_id=doc, query={
            "of": "edge", "surf": "line", "near": [30, 0, 1.5], "count": 1})["matches"][0]
        _apply(doc, {"type": "sm_edge_flange", "name": "Wall",
                     "params": {"length": 25, "angle": 90, "radius": 1.5},
                     "refs": {"edge": edge}})
        base_face = _call("query_geometry", doc_id=doc, query={
            "of": "face", "surf": "plane", "near": [0, 0, 1.5], "count": 1})["matches"][0]
        blind = _apply(doc, {"type": "hole", "name": "Blind pocket",
                             "params": {"style": "simple", "standard": "custom",
                                        "diameter": 4, "end": "blind", "depth": 0.5,
                                        "world_points": [[0, 0, 1.5]]},
                             "refs": {"face": base_face}})["id_map"]["$0"]
        before = cb.call("doc_tree", {"doc_id": doc})
        refused = _call("add_features", doc_id=doc, ops=[
            {"op": "add_feature", "feature": {"type": "sm_flat_pattern", "name": "Flatten"}}])
        assert not refused["ok"] and "partial-depth" in str(refused), refused
        after = cb.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == before["revision"] and after["tree"] == before["tree"]
        removed = _call("add_features", doc_id=doc,
                        ops=[{"op": "delete_feature", "id": blind}])
        assert removed["ok"], removed
        face = _call("query_geometry", doc_id=doc, query={
            "of": "face", "surf": "plane", "near": [30, 0, 15], "count": 1})["matches"][0]
        _apply(doc, {"type": "hole", "name": "Wall hole",
                     "params": {"style": "simple", "standard": "custom", "diameter": 4,
                                "end": "through_all", "world_points": [[30, 0, 15]]},
                     "refs": {"face": face}})
        _apply(doc, {"type": "sm_flat_pattern", "name": "Flatten"})
        path = tmp_path / "wall-hole-flat.dxf"
        result = _call("export_flat_dxf", doc_id=doc, path=str(path))
        assert result["ok"], result
        layers = _dxf_polyline_layers(path)
        assert layers.get("OUTLINE") == 1 and layers.get("CUTS") == 1, layers
        assert layers.get("BEND", 0) >= 2, layers
    finally:
        cb.call("doc_close", {"doc_id": doc})

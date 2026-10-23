"""Unit tests for close-up inspection helpers (inspect_part / look_around /
render_views / inspect_feature / emphasis palette).

Monkeypatches the render/query seams — no live engine / paid APIs required.
Run:  python3 -m pytest services/mcp/test_inspect_tools.py -q
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)



def _extract_palette(caption: str) -> dict:
    """Pull the embedded ``[palette_json=...]`` block out of an inspect caption."""
    marker = "palette_json="
    i = caption.find(marker)
    assert i >= 0, f"no palette_json in caption: {caption!r}"
    j = caption.find("{", i)
    assert j >= 0
    # Bracket-match the JSON object (entries array may contain nested braces).
    depth = 0
    for k in range(j, len(caption)):
        ch = caption[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(caption[j : k + 1])
    raise AssertionError(f"unbalanced palette_json in caption: {caption!r}")

from services.mcp import cad_tools as ct  # noqa: E402
from services.mcp import cad_common as cc  # noqa: E402  _resolve_highlight_specs/_feature_roster
from services.mcp import cad_render_tools as ct_render  # noqa: E402  inspect/render tools live here
from services.mcp.test_shot_planner import (  # noqa: E402  the planner's engine fake
    faces_from_matches,
    fake_view_metrics,
)


# --------------------------------------------------------------------------- fixtures / fakes


def _fake_png(path: str, size=(512, 400), color=(180, 190, 200)) -> str:
    from PIL import Image as PILImage
    PILImage.new("RGB", size, color).save(path, format="PNG")
    return path


@pytest.fixture
def part_bbox():
    return {
        "min": [-20.0, -15.0, -10.0],
        "max": [20.0, 15.0, 10.0],
        "center": [0.0, 0.0, 0.0],
        "size": [40.0, 30.0, 20.0],
    }


@pytest.fixture
def top_face_match():
    return {
        "kind": "face",
        "surf": "plane",
        "side": "top",
        "sid": "Ftop",
        "centroid": [0.0, 0.0, 10.0],
        "normal": [0.0, 0.0, 1.0],
        "axis": [0.0, 0.0, 1.0],
        "area_or_len": 1200.0,
        "radius": None,
    }


@pytest.fixture
def fillet_face_match():
    return {
        "kind": "face",
        "surf": "cylinder",
        "side": None,
        "sid": "Ffillet",
        "centroid": [-18.9, -13.9, 0.0],
        "normal": [-0.707, -0.707, 0.0],
        "axis": [0.0, 0.0, 1.0],
        "area_or_len": 94.25,
        "radius": 3.0,
    }


@pytest.fixture
def boss_doc():
    """A fake cb.call for a plate+boss document: two live features on body 2 plus one
    consumed feature whose topology only exists on the superseded body 1."""
    faces = [
        {"kind": "face", "surf": "plane", "side": "top", "sid": "Fdaab8d",
         "centroid": [0.0, 0.0, 7.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 9600.0},
        {"kind": "face", "surf": "cylinder", "side": None, "sid": "F2474b8",
         "centroid": [0.0, 0.0, 17.5], "axis": [0.0, 0.0, 1.0], "area_or_len": 2200.0,
         "radius": 18.0},
    ]
    tree = {
        "owner_map": {
            "f1": {"faces": [[2, 1]], "edges": []},
            "f2": {"faces": [[2, 2]], "edges": []},
            "f3": {"faces": [[1, 5]], "edges": []},
        },
        "tree": {"features": [
            {"id": "f1", "name": "Plate", "type": "box"},
            {"id": "f2", "name": "Boss", "type": "cylinder"},
            {"id": "f3", "name": "Ghost", "type": "cylinder"},
        ]},
    }

    def call(method, params):
        if method == "doc_tree":
            return tree
        if method == "doc_query":
            return {"matches": faces if params["query"].get("of") == "face" else []}
        raise AssertionError(method)

    return call


# --------------------------------------------------------------------------- parse / resolve


class TestParseTarget:
    def test_side_word(self):
        assert ct._parse_inspect_target("top") == {"of": "face", "side": "top"}
        assert ct._parse_inspect_target("the top face") == {"of": "face", "side": "top"}

    def test_feature_id(self):
        assert ct._parse_inspect_target("f2") == {"feature_id": "f2"}
        assert ct._parse_inspect_target("F12") == {"feature_id": "f12"}
        assert ct._parse_inspect_target({"feature_id": "f3"}) == {"feature_id": "f3"}

    def test_filleted_phrase(self):
        assert ct._parse_inspect_target("the filleted edge") == {
            "of": "edge", "surf": "circle"}
        assert ct._parse_inspect_target("fillet faces") == {
            "of": "face", "surf": "round"}

    def test_geom_query_passthrough(self):
        q = {"of": "edge", "parallel": "z", "pick": "largest"}
        assert ct._parse_inspect_target(q) == q

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            ct._parse_inspect_target("  ")


class TestResolveTarget:
    def test_unmatched_query_raises(self, monkeypatch):
        monkeypatch.setattr(ct.cb, "call", lambda method, params: {"matches": []})
        with pytest.raises(ValueError, match="No geometry matched"):
            ct._resolve_inspect_target("doc-1", {"of": "face", "side": "top"})

    def test_face_query_resolves(self, monkeypatch, top_face_match):
        def fake_call(method, params):
            assert method == "doc_query"
            return {"matches": [top_face_match]}
        monkeypatch.setattr(ct.cb, "call", fake_call)
        r = ct._resolve_inspect_target("doc-1", "top")
        assert r["kind"] == "face"
        assert r["matches"][0]["sid"] == "Ftop"
        assert "top" in r["label"]

    def test_feature_id_joins_owner_map(self, monkeypatch, fillet_face_match):
        faces = [fillet_face_match]
        owner_map = {
            "f2": {"bodies": [1], "faces": [[1, 1]], "edges": []},
        }
        tree = {
            "owner_map": owner_map,
            "tree": {"features": [
                {"id": "f1", "name": "Box", "type": "box"},
                {"id": "f2", "name": "Round", "type": "fillet"},
            ]},
        }

        def fake_call(method, params):
            if method == "doc_tree":
                return tree
            if method == "doc_query":
                assert params["query"]["of"] == "face"
                return {"matches": faces}
            raise AssertionError(method)

        monkeypatch.setattr(ct.cb, "call", fake_call)
        r = ct._resolve_inspect_target("doc-1", "f2")
        assert r["kind"] == "feature"
        assert r["feature_id"] == "f2"
        assert len(r["matches"]) == 1
        assert r["matches"][0]["sid"] == "Ffillet"

    def test_unknown_feature_raises(self, monkeypatch):
        monkeypatch.setattr(
            ct.cb, "call",
            lambda method, params: {"owner_map": {}, "tree": {"features": []}},
        )
        with pytest.raises(ValueError, match="No feature"):
            ct._resolve_inspect_target("doc-1", "f99")

    def test_feature_name_resolves(self, monkeypatch, boss_doc):
        """A feature NAME is a first-class target — it used to fall into the engine's side
        parser and die with `geom query: bad 'side' 'Boss'`."""
        monkeypatch.setattr(ct.cb, "call", boss_doc)
        for target in ("Boss", "boss", "  BOSS  ", "the Boss feature"):
            r = ct._resolve_inspect_target("doc-1", target)
            assert r["kind"] == "feature", target
            assert r["feature_id"] == "f2"
            assert r["feature_name"] == "Boss"
            assert [m["sid"] for m in r["matches"]] == ["F2474b8"]
            assert "Boss" in r["label"]

    def test_sid_resolves_to_one_element(self, monkeypatch, boss_doc):
        monkeypatch.setattr(ct.cb, "call", boss_doc)
        r = ct._resolve_inspect_target("doc-1", "Fdaab8d")
        assert r["kind"] == "face"
        assert r["label"] == "Fdaab8d"
        assert len(r["matches"]) == 1 and r["matches"][0]["sid"] == "Fdaab8d"

    def test_unknown_sid_lists_features(self, monkeypatch, boss_doc):
        monkeypatch.setattr(ct.cb, "call", boss_doc)
        with pytest.raises(ValueError, match="No face/edge with sid") as e:
            ct._resolve_inspect_target("doc-1", "Fdead99")
        assert 'f2="Boss"' in str(e.value)

    def test_unknown_name_teaches_the_roster(self, monkeypatch, boss_doc):
        monkeypatch.setattr(ct.cb, "call", boss_doc)
        with pytest.raises(ValueError) as e:
            ct._resolve_inspect_target("doc-1", "Bracket")
        msg = str(e.value)
        assert "bad 'side'" not in msg          # never the old dead end
        assert 'f1="Plate"' in msg and 'f2="Boss"' in msg
        assert "describe_part" in msg

    def test_ambiguous_name_lists_candidates(self, monkeypatch, fillet_face_match):
        tree = {"owner_map": {"f2": {"faces": [[1, 1]]}, "f3": {"faces": [[1, 1]]}},
                "tree": {"features": [{"id": "f2", "name": "Pin", "type": "cylinder"},
                                      {"id": "f3", "name": "pin", "type": "cylinder"}]}}
        monkeypatch.setattr(
            ct.cb, "call",
            lambda method, params: tree if method == "doc_tree" else {"matches": [fillet_face_match]})
        with pytest.raises(ValueError, match="ambiguous") as e:
            ct._resolve_inspect_target("doc-1", "Pin")
        assert 'f2="Pin"' in str(e.value) and 'f3="pin"' in str(e.value)

    def test_consumed_feature_says_so(self, monkeypatch, boss_doc):
        """f3 exists in the tree but owns topology only on a superseded body."""
        monkeypatch.setattr(ct.cb, "call", boss_doc)
        with pytest.raises(ValueError, match="owns no faces/edges on the current body"):
            ct._resolve_inspect_target("doc-1", "Ghost")


# ------------------------------------------------- what a CUT actually created (vs re-cut)
# A boolean CUT re-creates every face its tool passes through and OCCT credits those faces to
# the CUTTING feature, so doc_tree's owner_map hands an 8 mm bore the plate's whole top and
# bottom face. Every number below is a live engine capture of a 120x80x15 plate + r18 boss +
# r8 through-bore at x=-30 (doc_query face order == owner_map topo_id - 1).


def _rect_points(z, x0, x1, y0, y1):
    return [[x, y, z] for x in (x0, (x0 + x1) * 0.5, x1) for y in (y0, (y0 + y1) * 0.5, y1)]


def _cyl_points(cx, cy, r, z0, z1, n=16):
    return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n), z]
            for i in range(n) for z in (z0, (z0 + z1) * 0.5, z1)]


def _disc_points(cx, cy, r, z, n=16):
    return [[cx + r * f * math.cos(2 * math.pi * i / n),
             cy + r * f * math.sin(2 * math.pi * i / n), z]
            for i in range(n) for f in (0.0, 0.5, 1.0)]


def _fake_doc(faces, owner_map, features, points_by_sid):
    """A cb.call stand-in serving doc_tree / doc_query(face) / doc_face_points."""
    def call(method, params):
        if method == "doc_tree":
            return {"owner_map": owner_map, "tree": {"features": features}}
        if method == "doc_query":
            return {"matches": faces if params["query"].get("of") == "face" else []}
        if method == "doc_face_points":
            return {"points": points_by_sid.get((params.get("selector") or {}).get("sid"), [])}
        raise AssertionError(method)
    return call


def _plate_boss_bore(points_by_sid=None):
    faces = [
        {"kind": "face", "surf": "plane", "side": "left", "sid": "F8665dc",
         "centroid": [-60.0, 0.0, 0.0], "normal": [-1.0, 0.0, 0.0], "area_or_len": 1200.0},
        {"kind": "face", "surf": "plane", "side": "front", "sid": "F647760",
         "centroid": [0.0, -40.0, 0.0], "normal": [0.0, -1.0, 0.0], "area_or_len": 1800.0},
        {"kind": "face", "surf": "plane", "side": "top", "sid": "F742d7a",
         "centroid": [0.719701, 0.0, 7.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 8381.06205},
        {"kind": "face", "surf": "plane", "side": "back", "sid": "Fce09e7",
         "centroid": [0.0, 40.0, 0.0], "normal": [0.0, 1.0, 0.0], "area_or_len": 1800.0},
        {"kind": "face", "surf": "plane", "side": "bottom", "sid": "Fd83900",
         "centroid": [0.64176, 0.0, -7.5], "normal": [0.0, 0.0, -1.0], "area_or_len": 9398.93807},
        {"kind": "face", "surf": "plane", "side": "right", "sid": "Ffdb76d",
         "centroid": [60.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0], "area_or_len": 1200.0},
        {"kind": "face", "surf": "cylinder", "side": "top", "sid": "F704662",
         "centroid": [-30.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0], "normal": [0.0, 0.0, 1.0],
         "radius": 8.0, "area_or_len": 753.982237, "concavity": "bore"},
        {"kind": "face", "surf": "cylinder", "side": "top", "sid": "Fddd31d",
         "centroid": [0.0, 0.0, 12.5], "axis": [0.0, 0.0, 1.0], "normal": [0.0, 0.0, 1.0],
         "radius": 18.0, "area_or_len": 1130.973355, "concavity": "boss"},
        {"kind": "face", "surf": "plane", "side": "top", "sid": "Fe8aa5d",
         "centroid": [0.0, 0.0, 17.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 1017.87602},
    ]
    owner_map = {
        "f1": {"bodies": [1], "faces": [[1, 1], [1, 2], [1, 4], [1, 6]], "edges": []},
        "f2": {"bodies": [1], "faces": [[1, 8], [1, 9]], "edges": []},
        "f3": {"bodies": [1], "faces": [[1, 3], [1, 5], [1, 7]], "edges": []},
    }
    features = [
        {"id": "f1", "name": "Plate", "type": "box",
         "params": {"length": 120, "width": 80, "height": 15}},
        {"id": "f2", "name": "Boss", "type": "cylinder",
         "params": {"radius": 18, "height": 10, "mode": "boss", "at": [0, 0, 12.5]}},
        {"id": "f3", "name": "Bore", "type": "cylinder",
         "params": {"radius": 8, "height": 60, "mode": "cut", "at": [-30, 0, 0]}},
    ]
    if points_by_sid is None:
        points_by_sid = {
            "F8665dc": _rect_points(0.0, -60.0, -60.0, -40.0, 40.0),
            "F647760": _rect_points(0.0, -60.0, 60.0, -40.0, -40.0),
            "F742d7a": _rect_points(7.5, -60.0, 60.0, -40.0, 40.0),
            "Fce09e7": _rect_points(0.0, -60.0, 60.0, 40.0, 40.0),
            "Fd83900": _rect_points(-7.5, -60.0, 60.0, -40.0, 40.0),
            "Ffdb76d": _rect_points(0.0, 60.0, 60.0, -40.0, 40.0),
            "F704662": _cyl_points(-30.0, 0.0, 8.0, -7.5, 7.5),
            "Fddd31d": _cyl_points(0.0, 0.0, 18.0, 7.5, 17.5),
            "Fe8aa5d": _disc_points(0.0, 0.0, 18.0, 17.5),
        }
    return _fake_doc(faces, owner_map, features, points_by_sid)


class TestFeatureCreatedFaces:
    def test_through_bore_keeps_only_its_wall(self, monkeypatch):
        """The r8 bore owns the plate's 8381 mm2 top and 9399 mm2 bottom in owner_map. It
        CREATED neither — it only pierced them. Its own face is the 754 mm2 wall."""
        monkeypatch.setattr(ct.cb, "call", _plate_boss_bore())
        ms = ct._feature_owned_matches("doc-1", "Bore")
        assert [m["sid"] for m in ms] == ["F704662"]
        assert ms[0]["area_or_len"] == pytest.approx(753.98, abs=0.01)

    def test_additive_boss_is_untouched(self, monkeypatch):
        """A boss ADDS its faces, so owner_map is already right — the filter must not fire."""
        monkeypatch.setattr(ct.cb, "call", _plate_boss_bore())
        ms = ct._feature_owned_matches("doc-1", "Boss")
        assert [m["sid"] for m in ms] == ["Fddd31d", "Fe8aa5d"]

    def test_render_highlight_masks_only_the_bore_wall(self, monkeypatch):
        """render_part(highlight=["Boss","f3"]) used to tint the whole plate violet."""
        monkeypatch.setattr(ct.cb, "call", _plate_boss_bore())
        specs = cc._resolve_highlight_specs("doc-1", ["Boss", "f3"])
        assert [s["error"] for s in specs] == [None, None]
        assert [m["sid"] for m in specs[0]["matches"]] == ["Fddd31d", "Fe8aa5d"]
        assert [m["sid"] for m in specs[1]["matches"]] == ["F704662"]

    def test_callout_roster_drops_the_pierced_faces(self, monkeypatch):
        monkeypatch.setattr(ct.cb, "call", _plate_boss_bore())
        roster = {r["id"]: sorted(r["face_topos"]) for r in cc._feature_roster("doc-1")}
        assert roster["f3"] == [7]              # was [3, 5, 7]
        assert roster["f2"] == [8, 9]
        assert roster["f1"] == [1, 2, 4, 6]

    def test_attribution_does_not_depend_on_tessellation(self, monkeypatch):
        """STRENGTHENED contract (was "no face points -> leave the owner_map join alone").

        De-smearing now runs the params-based TOOL SCALE test first, and that test needs no
        geometry at all: an r8 bore's tool cannot have authored an 8381 or 9399 mm2 plane, so
        both plate faces go back to the plate with the tessellation stubbed out. Only the
        second, geometric footprint test degrades when doc_face_points is unavailable, and it
        can only refine what the scale test already decided. The old expectation froze the
        FALLBACK (keep everything) as if it were the contract."""
        monkeypatch.setattr(ct.cb, "call", _plate_boss_bore(points_by_sid={}))
        ms = ct._feature_owned_matches("doc-1", "Bore")
        assert [m["sid"] for m in ms] == ["F704662"]

    def test_blind_hole_keeps_its_floor(self, monkeypatch):
        """The floor fits INSIDE the wall footprint, so it is the tool's own face."""
        faces = [
            {"kind": "face", "surf": "plane", "side": "top", "sid": "Ftop",
             "centroid": [0.0, 0.0, 7.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 9398.9},
            {"kind": "face", "surf": "cylinder", "side": "top", "sid": "Fwall",
             "centroid": [-30.0, 0.0, 3.5], "axis": [0.0, 0.0, 1.0], "normal": [0.0, 0.0, 1.0],
             "radius": 8.0, "area_or_len": 402.12, "concavity": "bore"},
            {"kind": "face", "surf": "plane", "side": "top", "sid": "Ffloor",
             "centroid": [-30.0, 0.0, -0.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 201.06},
        ]
        call = _fake_doc(
            faces,
            {"f1": {"faces": [[1, 1]], "edges": []},
             "f2": {"faces": [[1, 1], [1, 2], [1, 3]], "edges": []}},
            [{"id": "f1", "name": "Plate", "type": "box", "params": {}},
             {"id": "f2", "name": "Blind", "type": "hole",
              "params": {"style": "simple", "end": "blind"}}],
            {"Ftop": _rect_points(7.5, -60.0, 60.0, -40.0, 40.0),
             "Fwall": _cyl_points(-30.0, 0.0, 8.0, -0.5, 7.5),
             "Ffloor": _disc_points(-30.0, 0.0, 8.0, -0.5)})
        monkeypatch.setattr(ct.cb, "call", call)
        assert [m["sid"] for m in ct._feature_owned_matches("doc-1", "Blind")] == \
            ["Fwall", "Ffloor"]

    def test_rectangular_pocket_drops_only_the_host_face(self, monkeypatch):
        """An all-planar cut has no curved wall to state the tool axis — it is recovered from
        the wall normals, and the reading that explains the most regenerated area wins."""
        faces = [
            {"kind": "face", "surf": "plane", "side": "top", "sid": "Fhost",
             "centroid": [0.0, 0.0, 7.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 9000.0},
            {"kind": "face", "surf": "plane", "side": "left", "sid": "Fwx0",
             "centroid": [-10.0, 0.0, 5.0], "normal": [1.0, 0.0, 0.0], "area_or_len": 150.0},
            {"kind": "face", "surf": "plane", "side": "right", "sid": "Fwx1",
             "centroid": [10.0, 0.0, 5.0], "normal": [-1.0, 0.0, 0.0], "area_or_len": 150.0},
            {"kind": "face", "surf": "plane", "side": "front", "sid": "Fwy0",
             "centroid": [0.0, -15.0, 5.0], "normal": [0.0, 1.0, 0.0], "area_or_len": 100.0},
            {"kind": "face", "surf": "plane", "side": "back", "sid": "Fwy1",
             "centroid": [0.0, 15.0, 5.0], "normal": [0.0, -1.0, 0.0], "area_or_len": 100.0},
            {"kind": "face", "surf": "plane", "side": "top", "sid": "Ffloor",
             "centroid": [0.0, 0.0, 2.5], "normal": [0.0, 0.0, 1.0], "area_or_len": 600.0},
        ]
        call = _fake_doc(
            faces,
            {"f1": {"faces": [[1, 1]], "edges": []},
             "f2": {"faces": [[1, i] for i in range(1, 7)], "edges": []}},
            [{"id": "f1", "name": "Plate", "type": "box", "params": {}},
             {"id": "f2", "name": "Pocket", "type": "box",
              "params": {"length": 20, "width": 30, "height": 10, "mode": "cut"}}],
            {"Fhost": _rect_points(7.5, -60.0, 60.0, -40.0, 40.0),
             "Fwx0": _rect_points(5.0, -10.0, -10.0, -15.0, 15.0),
             "Fwx1": _rect_points(5.0, 10.0, 10.0, -15.0, 15.0),
             "Fwy0": _rect_points(5.0, -10.0, 10.0, -15.0, -15.0),
             "Fwy1": _rect_points(5.0, -10.0, 10.0, 15.0, 15.0),
             "Ffloor": _rect_points(2.5, -10.0, 10.0, -15.0, 15.0)})
        monkeypatch.setattr(ct.cb, "call", call)
        assert [m["sid"] for m in ct._feature_owned_matches("doc-1", "Pocket")] == \
            ["Fwx0", "Fwx1", "Fwy0", "Fwy1", "Ffloor"]

    def test_subtractive_role_follows_a_pattern_seed(self):
        feats = [
            {"id": "f1", "name": "Plate", "type": "box", "params": {}},
            {"id": "f2", "name": "Bore", "type": "hole", "params": {"style": "simple"}},
            {"id": "f3", "name": "Boss", "type": "cylinder", "params": {"mode": "boss"}},
            {"id": "f4", "name": "Row", "type": "linear_pattern",
             "params": {"count1": 3}, "refs": {"features": ["f2"]}},
            {"id": "f5", "name": "Bosses", "type": "linear_pattern",
             "params": {"count1": 3}, "refs": {"features": ["f3"]}},
        ]
        by_id = {f["id"]: f for f in feats}
        assert ct._feature_is_subtractive(by_id["f2"], feats)
        assert ct._feature_is_subtractive(by_id["f4"], feats)   # pattern of a cut
        assert not ct._feature_is_subtractive(by_id["f1"], feats)
        assert not ct._feature_is_subtractive(by_id["f3"], feats)
        assert not ct._feature_is_subtractive(by_id["f5"], feats)
        assert ct._feature_is_subtractive(
            {"id": "f6", "type": "boolean", "params": {"op": "cut"}}, feats)


# --------------------------------------------------------------------------- camera / crop math


class TestCameraAndCrop:
    def test_face_normal_auto_view(self, top_face_match):
        d = ct._camera_dir_for_target([top_face_match], "auto")
        assert d[2] == pytest.approx(1.0)
        assert abs(d[0]) < 1e-9 and abs(d[1]) < 1e-9

    def test_named_and_dir_views(self):
        assert ct._camera_dir_for_target([], "front") == (0.0, -1.0, 0.0)
        d = ct._camera_dir_for_target([], {"dir": [3, 0, 0]})
        assert d[0] == pytest.approx(1.0)

    def test_crop_rect_frames_target(self, part_bbox):
        # Target sits on the +Z top-front-left corner of the part.
        target = {
            "min": [-20.0, -15.0, 8.0],
            "max": [-10.0, -5.0, 10.0],
            "center": [-15.0, -10.0, 9.0],
            "size": [10.0, 10.0, 2.0],
        }
        vd = (0.0, 0.0, 1.0)  # top view
        rect = ct._crop_rect_for_target(part_bbox, target, vd,
                                       margin=0.15, zoom_factor=1.5)
        assert rect["method"] == "crop"
        assert 0 <= rect["x0"] < rect["x1"] <= 512
        assert 0 <= rect["y0"] < rect["y1"] <= 400
        # Crop should be smaller than the full image (true close-up).
        assert (rect["x1"] - rect["x0"]) < 512
        assert (rect["y1"] - rect["y0"]) < 400
        # Higher zoom → smaller crop window.
        loose = ct._crop_rect_for_target(part_bbox, target, vd,
                                        margin=0.15, zoom_factor=1.0)
        tight = ct._crop_rect_for_target(part_bbox, target, vd,
                                        margin=0.15, zoom_factor=3.0)
        assert (tight["x1"] - tight["x0"]) <= (loose["x1"] - loose["x0"])
        assert (tight["y1"] - tight["y0"]) <= (loose["y1"] - loose["y0"])

    def test_crop_png_upscales(self, tmp_path):
        src = str(tmp_path / "full.png")
        _fake_png(src, size=(512, 400), color=(10, 20, 30))
        # Paint a bright block in the crop region so we can assert content survived.
        from PIL import Image as PILImage, ImageDraw
        with PILImage.open(src) as im:
            draw = ImageDraw.Draw(im)
            draw.rectangle([100, 100, 150, 150], fill=(255, 0, 0))
            im.save(src)
        rect = {"x0": 100, "y0": 100, "x1": 150, "y1": 150, "method": "crop"}
        out = str(tmp_path / "crop.png")
        ct._crop_png_to_rect(src, rect, out, out_size=(512, 400))
        assert os.path.exists(out)
        with PILImage.open(out) as im:
            assert im.size == (512, 400)
            # Center of upscaled crop should be near the bright red fill.
            px = im.getpixel((256, 200))
            assert px[0] > 200 and px[1] < 50 and px[2] < 50

    def test_orbit_dirs_count_and_unit(self):
        dirs = ct._orbit_view_dirs((0.0, 0.0, 1.0), n=4)
        assert len(dirs) == 4
        for d in dirs:
            n = math.sqrt(sum(c * c for c in d))
            assert n == pytest.approx(1.0, abs=1e-9)
        # First equals base.
        assert dirs[0][2] == pytest.approx(1.0)


# --------------------------------------------------------------------------- caption


class TestCaption:
    def test_caption_content(self, monkeypatch, top_face_match, part_bbox):
        def fake_call(method, params):
            if method == "eval_measure":
                return {"kind": "area", "area": 1200.0}
            raise AssertionError(method)

        monkeypatch.setattr(ct.cb, "call", fake_call)
        resolved = {
            "matches": [top_face_match],
            "label": "top",
            "kind": "face",
        }
        target_bb = {
            "min": [-20, -15, 10], "max": [20, 15, 10],
            "size": [40, 30, 0.5], "center": [0, 0, 10],
        }
        cap = ct._inspect_caption("doc-1", resolved, target_bb,
                                 (0.0, 0.0, 1.0), method="crop", n_matches=1)
        assert "Close-up of 'top'" in cap
        assert "CROP" in cap or "crop" in cap.lower()
        assert "1200" in cap or "area=" in cap
        assert "scene" in cap.lower() or "camera" in cap.lower()


# --------------------------------------------------------------------------- end-to-end tools (monkeypatched seams)


class TestInspectToolsE2E:
    def _install_seams(self, monkeypatch, matches, part_bbox, tmp_path):
        """Stub cb.call + ensure render writes a real PNG the crop path can open."""
        calls: list[tuple[str, dict]] = []

        def fake_call(method: str, params: dict) -> Any:
            calls.append((method, params))
            if method == "doc_query":
                return {"matches": matches}
            if method == "doc_bbox":
                return part_bbox
            if method == "doc_face_points":
                # A few points around the first match centroid.
                c = matches[0]["centroid"] if matches else [0, 0, 0]
                return {"points": [
                    [c[0] - 5, c[1] - 5, c[2]],
                    [c[0] + 5, c[1] - 5, c[2]],
                    [c[0] - 5, c[1] + 5, c[2]],
                    [c[0] + 5, c[1] + 5, c[2]],
                ]}
            if method == "eval_measure":
                area = float((matches[0].get("area_or_len") if matches else 1.0) or 1.0)
                return {"kind": "area", "area": area}
            if method in ("doc_render_view", "doc_render_png"):
                _fake_png(params["out_path"])
                return {"ok": True, "path": params["out_path"],
                        "view": "front", "dir": [0, -1, 0]}
            if method == "doc_view_metrics":
                return fake_view_metrics(params, faces_from_matches(matches))
            if method == "doc_render_contact_sheet":
                # Labeled multi-view grid (engine default 4 tiles; we accept any list).
                views = params.get("views") or ["front", "back", "left", "right",
                                               "top", "bottom", "iso"]
                # Produce a wide sheet so size assertions pass.
                n = max(len(views), 1)
                cols = min(3, n)
                rows = (n + cols - 1) // cols
                _fake_png(params["out_path"], size=(512 * cols, 400 * rows),
                          color=(170, 180, 190))
                return {"ok": True, "path": params["out_path"], "views": list(views),
                        "cols": cols, "rows": rows, "tile": [512, 400]}
            if method == "doc_tree":
                return {"owner_map": {}, "tree": {"features": [
                    {"id": "f1", "name": "Box", "type": "box"},
                ]}}
            if method == "eval_section_props":
                return {"area_mm2": 120.0, "centroid": [0, 0, 0],
                        "Ixx": 1.0, "Iyy": 1.0, "Ixy": 0.0}
            raise AssertionError(f"unexpected method {method}")

        monkeypatch.setattr(ct.cb, "call", fake_call)
        # Pin _doc so we don't need a live current-doc.
        monkeypatch.setattr(ct_render, "_doc", lambda doc_id=None: doc_id or "doc-test")
        # describe_part excerpt path may call gsl_tools — keep it offline.
        monkeypatch.setattr(ct_render, "_describe_part_excerpt",
                            lambda doc_id, max_chars=900: (
                                "Part bbox 40x30x20 mm | features: f1:box | faces: top Ftop"))
        return calls

    def _tool(self, name: str):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test-inspect")
        ct.register_cad_tools(mcp)
        mgr = getattr(mcp, "_tool_manager", None) or getattr(mcp, "tool_manager", None)
        tools = getattr(mgr, "_tools", {}) if mgr else {}
        if name in tools:
            return tools[name].fn
        for n, t in (tools or {}).items():
            if n == name or getattr(t, "name", None) == name:
                return getattr(t, "fn", t)
        raise AssertionError(f"{name} not registered; tools={list(tools)}")

    def test_inspect_part_returns_image_and_caption(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("inspect_part")

        result = tool_fn(target="top", doc_id="doc-test", view="auto",
                         zoom_factor=1.5, margin=0.15, annotate=True)
        assert isinstance(result, list) and len(result) == 2
        img, caption = result
        assert hasattr(img, "path") or (isinstance(img, str) and img.endswith(".png"))
        png_path = getattr(img, "path", img)
        assert os.path.exists(png_path)
        assert "Close-up" in caption
        # The close-up is FRAMED by the engine, not cropped out of a whole-part render.
        assert "focus" in caption.lower()
        assert "crop" not in caption.lower()
        cam_calls = [p for m, p in calls
                     if m == "doc_render_view" and (p.get("view") or {}).get("camera")]
        assert cam_calls, "inspect_part must render through a camera aimed at the target"
        cam = cam_calls[0]["view"]["camera"]
        assert cam["projection"] == "perspective"
        # Aimed at the target box centre (the top face at z=10), not the part centre.
        assert cam["target"][2] == pytest.approx(10.0, abs=1.0)
        assert "perspective" in caption
        assert any(m == "doc_query" for m, _ in calls)

    def test_inspect_part_unmatched_errors(
            self, monkeypatch, part_bbox, tmp_path):
        self._install_seams(monkeypatch, [], part_bbox, tmp_path)
        tool_fn = self._tool("inspect_part")
        with pytest.raises(ValueError, match="No geometry matched"):
            tool_fn(target={"of": "face", "side": "nope"}, doc_id="doc-test")

    def test_look_around_tiles(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("look_around")
        result = tool_fn(target="top", doc_id="doc-test", n_views=4)
        assert isinstance(result, list) and len(result) == 2
        img, caption = result
        png_path = getattr(img, "path", img)
        assert os.path.exists(png_path)
        from PIL import Image as PILImage
        with PILImage.open(png_path) as im:
            # 2x2 tiles of 512x400 → 1024x800
            assert im.size[0] >= 512 and im.size[1] >= 400
        assert "Look-around" in caption
        assert sum(1 for m, _ in calls if m == "doc_render_view") == 4

    def test_bbox_from_points_and_target_bbox(
            self, monkeypatch, top_face_match):
        def fake_call(method, params):
            if method == "doc_face_points":
                return {"points": [
                    [-10, -5, 10], [10, -5, 10], [-10, 5, 10], [10, 5, 10],
                ]}
            raise AssertionError(method)
        monkeypatch.setattr(ct.cb, "call", fake_call)
        bb = ct._target_bbox("doc-1", [top_face_match])
        assert bb["size"][0] == pytest.approx(20.0)
        assert bb["size"][1] == pytest.approx(10.0)
        assert bb["center"][2] == pytest.approx(10.0)

    def test_render_views_default_grid(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("render_views")
        result = tool_fn(doc_id="doc-test")
        assert isinstance(result, list) and len(result) == 2
        img, caption = result
        png_path = getattr(img, "path", img)
        assert os.path.exists(png_path)
        from PIL import Image as PILImage
        with PILImage.open(png_path) as im:
            # 7 planner tiles in 3 columns: wider and taller than a single render.
            assert im.size[0] >= 3 * 512 and im.size[1] >= 3 * 400
        assert "render_views" in caption
        # The default set is the planner's: measured once, rendered view-by-view so every
        # tile carries its overlay — never the engine's fixed internal sheet.
        assert sum(1 for m, _ in calls if m == "doc_view_metrics") >= 1
        assert not any(m == "doc_render_contact_sheet" for m, _ in calls)
        assert sum(1 for m, _ in calls if m == "doc_render_view") == 7
        for letter in "ABCDEFG":
            assert f" {letter}: dir [" in caption

    def test_render_views_custom_list(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("render_views")
        result = tool_fn(doc_id="doc-test", views=["front", "top", "iso"], annotate=False)
        img, caption = result
        assert os.path.exists(getattr(img, "path", img))
        cs = [p for m, p in calls if m == "doc_render_contact_sheet"]
        assert cs and cs[0].get("views") == ["front", "top", "iso"]
        assert "front" in caption.lower() and "iso" in caption.lower()

    def test_render_views_target_framed(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("render_views")
        result = tool_fn(doc_id="doc-test", views=["front", "top"], target="top")
        img, caption = result
        png_path = getattr(img, "path", img)
        assert os.path.exists(png_path)
        from PIL import Image as PILImage
        with PILImage.open(png_path) as im:
            assert im.size[0] >= 512
        # Target-framed path uses per-view doc_render_view, not contact_sheet.
        assert sum(1 for m, _ in calls if m == "doc_render_view") >= 2
        assert "render_views" in caption

    def test_inspect_part_emphasis_palette(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("inspect_part")
        result = tool_fn(target="top", doc_id="doc-test", emphasize=True,
                         emphasis_mode="emphasis")
        img, caption = result
        assert os.path.exists(getattr(img, "path", img))
        assert "palette_json=" in caption
        assert "EMPHASIS" in caption or "Emphasis" in caption or "emphasis" in caption
        # Parse the embedded palette metadata (assert palette, not pixels).
        palette = _extract_palette(caption)
        assert palette["mode"] == "emphasis"
        assert palette["entries"]
        assert palette["entries"][0]["rgb"] == list(ct._EMPHASIS_PALETTE[0])
        assert "glow_rgb" in palette

    def test_inspect_part_per_face_palette_deterministic(
            self, monkeypatch, top_face_match, fillet_face_match, part_bbox, tmp_path):
        matches = [top_face_match, fillet_face_match]
        self._install_seams(monkeypatch, matches, part_bbox, tmp_path)
        # Build palette directly (same helper the tool uses).
        p1 = ct._build_emphasis_palette(matches, mode="per_face")
        p2 = ct._build_emphasis_palette(matches, mode="per_face")
        assert p1["mode"] == "per_face"
        assert len(p1["entries"]) == 2
        # Deterministic across calls.
        assert p1["entries"][0]["rgb"] == p2["entries"][0]["rgb"]
        assert p1["entries"][1]["rgb"] == p2["entries"][1]["rgb"]
        # Distinct colours for distinct sids.
        assert p1["entries"][0]["rgb"] != p1["entries"][1]["rgb"]
        # Keys track sids.
        assert p1["entries"][0]["sid"] == "Ftop"
        assert p1["entries"][1]["sid"] == "Ffillet"

    def test_inspect_feature_one_call(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("inspect_feature")
        result = tool_fn(target="top", doc_id="doc-test")
        assert isinstance(result, list) and len(result) == 2
        img, caption = result
        png_path = getattr(img, "path", img)
        assert os.path.exists(png_path)
        assert "Close-up" in caption
        assert "describe_part excerpt" in caption
        assert "palette_json=" in caption
        palette = _extract_palette(caption)
        assert palette["mode"] == "emphasis"
        assert palette["entries"][0]["sid"] == "Ftop"
        assert any(meth == "doc_render_view" for meth, _ in calls)

    def test_render_part_emphasis_palette_in_caption(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("render_part")
        result = tool_fn(doc_id="doc-test", emphasize="top", emphasis_mode="per_face")
        assert isinstance(result, list) and len(result) == 2
        img, caption = result
        assert os.path.exists(getattr(img, "path", img))
        assert "palette_json=" in caption
        palette = _extract_palette(caption)
        assert palette["mode"] == "per_face"
        assert palette["entries"][0]["key"]


class TestEmphasisHelpers:
    def test_stable_color_deterministic(self):
        a = ct._stable_color_for_key("Ftop")
        b = ct._stable_color_for_key("Ftop")
        c = ct._stable_color_for_key("Fother")
        assert a == b
        assert a != c

    def test_normalize_view_list_default(self):
        v = ct._normalize_view_list(None)
        assert v == list(ct._STANDARD_ORTHO_VIEWS)
        assert "front" in v and "iso" in v and len(v) == 7

    def test_target_is_internal_bore(self, part_bbox):
        bore = {
            "kind": "face", "surf": "cylinder", "concavity": "bore",
            "centroid": [0, 0, 0], "radius": 3.0, "area_or_len": 50.0,
        }
        assert ct._target_is_internal([bore], part_bbox) is True

    def test_tile_pngs_grid_labels(self, tmp_path):
        tiles = []
        for i, color in enumerate([(200, 0, 0), (0, 200, 0), (0, 0, 200)]):
            p = str(tmp_path / f"t{i}.png")
            _fake_png(p, size=(64, 48), color=color)
            tiles.append(p)
        out = str(tmp_path / "grid.png")
        ct._tile_pngs_grid(tiles, out, labels=["FRONT", "TOP", "ISO"], cols=3)
        assert os.path.exists(out)
        from PIL import Image as PILImage
        with PILImage.open(out) as im:
            # 3 cols × 64 with gaps
            assert im.size[0] >= 64 * 3
            assert im.size[1] >= 48


class TestGuideMentions:
    def test_review_guide_matches_cpp_review_stage(self):
        # reviewer.md is the C++ review-stage prompt (slimmed in f81fcc1c: the reviewer
        # has NO tools there — it judges the feature tree + MEASURED block and answers
        # with the done-token). Assert the real contract, not the retired tool mentions.
        text = ct.cad_guide_text("review")
        assert "MEASURED" in text
        assert '{"done": true' in text

    def test_gsl_guide_mentions_inspect(self):
        text = ct.cad_guide_text("gsl")
        assert "inspect_part" in text
        assert "inspect_feature" in text
        assert "render_views" in text

    def test_overview_mentions_render_views(self):
        text = ct.cad_guide_text("")  # default/overview
        assert "render_views" in text or "inspect_feature" in text

    def test_add_features_mentions_verify_loop(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test-inspect")
        ct.register_cad_tools(mcp)
        mgr = getattr(mcp, "_tool_manager", None)
        tools = getattr(mgr, "_tools", {}) if mgr else {}
        desc = tools["add_features"].description or ""
        assert "VERIFY LOOP" in desc
        assert "render_part" in desc or "render_views" in desc


# --------------------------------------------------------------------------- save_path + study setup (O-16)


def _box_faces():
    """A 40x30x20 box's top, bottom and back faces, in doc_query enumeration order."""
    top = {"kind": "face", "surf": "plane", "side": "top", "sid": "Ftop",
           "centroid": [0.0, 0.0, 10.0], "normal": [0.0, 0.0, 1.0],
           "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0, "radius": None}
    bot = {"kind": "face", "surf": "plane", "side": "bottom", "sid": "Fbot",
           "centroid": [0.0, 0.0, -10.0], "normal": [0.0, 0.0, -1.0],
           "axis": [0.0, 0.0, -1.0], "area_or_len": 1200.0, "radius": None}
    back = {"kind": "face", "surf": "plane", "side": "back", "sid": "Fback",
            "centroid": [0.0, 15.0, 0.0], "normal": [0.0, 1.0, 0.0],
            "axis": [0.0, 1.0, 0.0], "area_or_len": 800.0, "radius": None}
    return top, bot, back


def _box_mesh():
    """A synthetic 40x30x20 box tessellation shaped like _doc_mesh's return value.

    face_id 1/2/3 are the +Z (top), -Z (bottom) and +Y (back) faces, matching the
    doc_query enumeration the fakes below serve, so sid -> topo -> mesh face joins the
    same way it does against a live engine. Outward winding (back-faces are culled)."""
    import numpy as np
    x, y, z = 20.0, 15.0, 10.0
    v = np.asarray([
        [-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
        [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z],
    ], dtype=np.float64)
    quads = [
        (1, [4, 5, 6, 7]),   # +Z top
        (2, [0, 3, 2, 1]),   # -Z bottom
        (3, [2, 3, 7, 6]),   # +Y back
        (4, [0, 1, 5, 4]),   # -Y front
        (5, [1, 2, 6, 5]),   # +X right
        (6, [3, 0, 4, 7]),   # -X left
    ]
    tris, faces = [], []
    for face_id, q in quads:
        faces.append({"body": 1, "face_id": face_id, "first_tri": len(tris),
                      "tri_count": 2, "surf_type": 0})
        tris.append([q[0], q[1], q[2]])
        tris.append([q[0], q[2], q[3]])
    idx = np.asarray(tris, dtype=np.int64)
    tri_face = np.full(idx.shape[0], -1, dtype=np.int32)
    for fi, fr in enumerate(faces):
        tri_face[fr["first_tri"]:fr["first_tri"] + fr["tri_count"]] = fi
    return {"pos": v, "idx": idx, "tri_face": tri_face, "faces": faces,
            "edges": [], "last_body": 1}


class TestRenderSavePath:
    """save_path writes the figure the model is looking at — the same bytes, at a path a
    report can reference (O-16)."""

    def _seams(self, monkeypatch, tmp_path):
        calls: list[tuple[str, dict]] = []
        top, bot, _back = _box_faces()

        def fake_call(method, params):
            calls.append((method, params))
            if method == "doc_query":
                return {"matches": [top, bot]}
            if method == "doc_bbox":
                return {"min": [-20, -15, -10], "max": [20, 15, 10],
                        "center": [0, 0, 0], "size": [40, 30, 20]}
            if method == "doc_face_points":
                c = (params.get("selector") or {}).get("centroid") or [0, 0, 0]
                return {"points": [[c[0] - 5, c[1] - 5, c[2]], [c[0] + 5, c[1] - 5, c[2]],
                                   [c[0] - 5, c[1] + 5, c[2]], [c[0] + 5, c[1] + 5, c[2]]]}
            if method in ("doc_render_view", "doc_render_png"):
                _fake_png(params["out_path"])
                return {"ok": True, "path": params["out_path"]}
            if method == "doc_view_metrics":
                return fake_view_metrics(params, faces_from_matches([top, bot]))
            if method == "doc_tree":
                return {"owner_map": {}, "tree": {"features": [
                    {"id": "f1", "name": "Box", "type": "box"}]}}
            raise AssertionError(f"unexpected method {method}")

        monkeypatch.setattr(ct.cb, "call", fake_call)
        monkeypatch.setattr(ct_render, "_doc", lambda doc_id=None: doc_id or "doc-test")
        monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
        monkeypatch.delenv("CHUDWARE_RENDER_SAVE_ANYWHERE", raising=False)
        return calls

    def _tool(self, name):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test-render-save")
        ct.register_cad_tools(mcp)
        mgr = getattr(mcp, "_tool_manager", None)
        tools = getattr(mgr, "_tools", {}) if mgr else {}
        assert name in tools, f"{name} not registered; tools={sorted(tools)}"
        return tools[name].fn

    def test_render_part_save_path_writes_the_inline_png(self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        out = tmp_path / "figures" / "iso.png"
        img, caption = self._tool("render_part")(doc_id="doc-test", save_path=str(out))
        assert out.exists(), "save_path did not write the file"
        from PIL import Image as PILImage
        with PILImage.open(out) as im:
            assert im.format == "PNG" and im.size[0] > 0
        inline = getattr(img, "path", img)
        saved = out.read_bytes()
        assert saved == open(inline, "rb").read(), (
            "the saved file must be the inline image byte-for-byte")
        assert f"saved: {out}" in caption
        assert f"({len(saved)} B, sha256 " in caption
        assert hashlib.sha256(saved).hexdigest()[:12] in caption

    def test_relative_save_path_is_project_root_relative(self, monkeypatch, tmp_path):
        """A relative save_path resolves against the project root, as in save_part and
        asm_add_component, and the caption reports the absolute file it wrote."""
        self._seams(monkeypatch, tmp_path)
        monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
        monkeypatch.chdir(tmp_path.parent)  # the process cwd is never the base
        _img, caption = self._tool("render_part")(doc_id="doc-test",
                                                  save_path="scratch/smoke/shaft.png")
        out = tmp_path / "scratch" / "smoke" / "shaft.png"
        assert out.exists(), caption
        assert f"saved: {out} (" in caption

    def test_save_path_needs_a_root_and_must_stay_inside_the_project(
            self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        render_part = self._tool("render_part")
        monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
        with pytest.raises(ValueError, match="no project root.*Pass an ABSOLUTE save_path"):
            render_part(doc_id="doc-test", save_path="figures/iso.png")
        outside = tmp_path.parent / "elsewhere" / "iso.png"
        with pytest.raises(ValueError, match="outside this session's project"):
            render_part(doc_id="doc-test", save_path=str(outside))
        # The traversal spelling of the same destination is refused too (the containment
        # test runs on the canonicalised path, not the string).
        with pytest.raises(ValueError, match="outside this session's project"):
            render_part(doc_id="doc-test",
                        save_path=str(tmp_path / ".." / "elsewhere" / "iso.png"))
        monkeypatch.setenv("CHUDWARE_RENDER_SAVE_ANYWHERE", "1")
        _img, caption = render_part(doc_id="doc-test", save_path=str(outside))
        assert outside.exists() and f"saved: {outside}" in caption

    def test_render_views_and_contact_sheet_save_the_finished_sheet(
            self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        for tool, name in (("render_views", "views.png"), ("contact_sheet", "sheet.png")):
            out = tmp_path / name
            img, caption = self._tool(tool)(doc_id="doc-test", save_path=str(out))
            assert out.exists(), f"{tool} ignored save_path"
            assert out.read_bytes() == open(getattr(img, "path", img), "rb").read()
            assert f"saved: {out}" in caption


class TestRenderStudySetup:
    """The FIXTURE/LOAD figure: the faces the solver would use, in two reserved colours,
    with a legend a report can quote (O-16)."""

    def _seams(self, monkeypatch, tmp_path):
        calls: list[tuple[str, dict]] = []
        top, bot, back = _box_faces()

        def fake_call(method, params):
            calls.append((method, params))
            if method == "doc_query":
                q = json.dumps(params.get("query") or {})
                if (params.get("query") or {}).get("of") == "edge":
                    return {"matches": []}
                if "bottom" in q:
                    return {"matches": [bot]}
                if "top" in q:
                    return {"matches": [top]}
                if "back" in q:
                    return {"matches": [back]}
                return {"matches": [top, bot, back]}
            if method == "doc_bbox":
                return {"min": [-20, -15, -10], "max": [20, 15, 10],
                        "center": [0, 0, 0], "size": [40, 30, 20]}
            if method == "doc_face_points":
                sel = params.get("selector") or {}
                c = sel.get("centroid") or [0, 0, 0]
                n = sel.get("normal") or [0, 0, 1]
                # Sample IN the face plane: the two axes the normal does not own.
                ax = [i for i in range(3) if abs(n[i]) < 0.5] or [0, 1]
                pts = []
                for du in (-5.0, 5.0):
                    for dv in (-5.0, 5.0):
                        p = list(c)
                        p[ax[0]] += du
                        p[ax[1]] += dv
                        pts.append(p)
                return {"points": pts, "tol_mm": 1.0, "face_normal": n}
            if method == "asm_world_bodies":
                return {"bodies": [{"bbox": {"min": [-20, -15, -10],
                                             "max": [20, 15, 10]}}]}
            if method == "doc_render_view":
                _fake_png(params["out_path"])
                return {"ok": True, "path": params["out_path"]}
            if method == "doc_tree":
                return {"owner_map": {}, "tree": {"features": [
                    {"id": "f1", "name": "Box", "type": "box"}]}}
            raise AssertionError(f"unexpected method {method}")

        monkeypatch.setattr(ct.cb, "call", fake_call)
        monkeypatch.setattr(ct_render, "_doc", lambda doc_id=None: doc_id or "doc-test")
        # The tessellation is the engine's; the overlay only needs its shape.
        monkeypatch.setattr(cc, "_doc_mesh", lambda doc_id: _box_mesh())
        monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
        monkeypatch.delenv("CHUDWARE_RENDER_SAVE_ANYWHERE", raising=False)
        return calls

    def _tool(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test-study-setup")
        ct.register_cad_tools(mcp)
        tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
        assert "render_study_setup" in tools, sorted(tools)
        return tools["render_study_setup"].fn

    def test_fixture_and_load_get_distinct_reserved_colours(self, monkeypatch, tmp_path):
        """Fixture on the back face, load on the top: one camera sees both, and the two
        groups must land on their OWN reserved colour."""
        self._seams(monkeypatch, tmp_path)
        out = tmp_path / "study-setup.png"
        img, caption = self._tool()(
            fixtures=["back"], load=[{"face": "top", "force": [0, 0, -500]}],
            view="iso", doc_id="doc-test", save_path=str(out))

        # Legend: both groups, their colours, their faces + areas, and the resultant.
        assert "FIXTURE (magenta): Fback 800 mm2" in caption
        assert "LOAD (violet): resultant 500 N" in caption
        assert "[0.000, 0.000, -1.000]" in caption          # direction of the resultant
        assert "Ftop 1200 mm2" in caption
        assert ct_render._FIXTURE_COLOUR != ct_render._LOAD_COLOUR

        # And the pixels agree: the fixture face is painted magenta (r > b > g), the load
        # face violet (b > r > g). A regression that gave both groups one palette slot
        # (or misordered the per-match entries) leaves one of these classes empty.
        from PIL import Image as PILImage
        with PILImage.open(getattr(img, "path", img)) as im:
            colours = {c for _n, c in im.convert("RGB").getcolors(maxcolors=1 << 20)}
        saturated = [c for c in colours if max(c) - min(c) > 80]
        magenta = [c for c in saturated if c[0] > c[2] > c[1]]
        violet = [c for c in saturated if c[2] > c[0] > c[1]]
        assert magenta, f"no fixture-coloured pixels among {sorted(saturated)}"
        assert violet, f"no load-coloured pixels among {sorted(saturated)}"
        assert not set(magenta) & set(violet)
        assert "Visible from this camera: Fback, Ftop" in caption

        # Saved on request, byte-identical to the inline figure.
        assert out.exists()
        assert out.read_bytes() == open(getattr(img, "path", img), "rb").read()
        assert f"saved: {out}" in caption

    def test_a_bc_face_the_camera_cannot_see_is_reported_not_claimed(
            self, monkeypatch, tmp_path):
        """The canonical study — load on top, fixture on the bottom — has no single camera
        that sees both. The caption must still name the fixture AND say it is not in
        frame, because a reader who only sees the load face would otherwise assume it."""
        self._seams(monkeypatch, tmp_path)
        _img, caption = self._tool()(
            fixtures=["bottom"], load=[{"face": "top", "force": [0, 0, -500]}],
            view="iso", doc_id="doc-test")
        assert "FIXTURE (magenta): Fbot 1200 mm2" in caption
        assert "Visible from this camera: Ftop." in caption
        assert "NOT visible from here: Fbot" in caption

    def test_pressure_load_reports_the_derived_resultant(self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        _img, caption = self._tool()(
            fixtures=["back"], load={"face": "top", "pressure": 0.5},
            view="iso", doc_id="doc-test")
        # 0.5 MPa over the 1200 mm2 top face = 600 N inward (-Z), stated as a number.
        assert "pressure 0.5 MPa over 1200 mm2 = 600 N inward" in caption
        assert "resultant 600 N" in caption
        assert "[0.000, 0.000, -1.000]" in caption

    def test_a_load_on_the_fixed_face_is_refused_like_the_solver_refuses_it(
            self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="entirely on a fully fixed face"):
            self._tool()(fixtures=["top"],
                         load=[{"face": "top", "force": [0, 0, -500]}],
                         doc_id="doc-test")

    def test_a_zero_load_is_refused_like_the_solver_refuses_it(self, monkeypatch, tmp_path):
        self._seams(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="no non-zero load"):
            self._tool()(fixtures=["back"],
                         load=[{"face": "top", "force": [0, 0, 0]}],
                         doc_id="doc-test")


# ------------------------------------------------------- engine-backed study-setup figure
# Two defects the monkeypatched fixtures above structurally cannot catch: the multi-body
# tessellation join (the fake mesh has one body) and the load VECTOR reaching the pixels
# (byte-identity is only meaningful against a real render). Small boxes, no solve.

from services.mcp import core_bridge as _cb  # noqa: E402


def _two_box_doc() -> str:
    """Two 20 mm boxes 30 mm apart in X — separate bodies, NOT touching."""
    doc = _cb.call("doc_new", {"name": "study_setup_two_boxes"})["doc_id"]
    _cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {
            "type": "box", "name": "A",
            "params": {"length": {"value": 20}, "width": {"value": 20},
                       "height": {"value": 20}}}},
        {"op": "add_feature", "feature": {
            "type": "transform", "name": "B",
            "params": {"translate": [30.0, 0.0, 0.0], "copy": True}}},
    ]})
    return doc


@pytest.fixture(scope="module")
def two_box_doc() -> str:
    return _two_box_doc()


@pytest.fixture(scope="module")
def two_box_faces(two_box_doc) -> dict:
    ms = _cb.call("doc_query", {"doc_id": two_box_doc, "query": {"of": "face"}})["matches"]
    a = [m for m in ms if m["centroid"][0] < 20]
    b = [m for m in ms if m["centroid"][0] >= 20]
    return {"a_top": next(m["sid"] for m in a if m.get("side") == "top"),
            "a_bot": next(m["sid"] for m in a if m.get("side") == "bottom"),
            "b_top": next(m["sid"] for m in b if m.get("side") == "top")}


@pytest.mark.skipif(not _cb.available(),
                    reason="libchudware not staged; engine-backed render unavailable")
class TestRenderStudySetupOnTheEngine:
    """The study figure against the real tessellation: every body's faces paint, and the
    load vector is part of the raster."""

    def _tool(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test-study-setup-engine")
        ct.register_cad_tools(mcp)
        return mcp._tool_manager._tools["render_study_setup"].fn

    def _png(self, result) -> bytes:
        img = result[0]
        return Path(getattr(img, "path", img)).read_bytes()

    def test_bc_faces_on_two_different_bodies_are_both_painted(
            self, two_box_doc, two_box_faces):
        """Fixture on body A, load on body B. The overlay used to index only the LAST
        body's tessellation, so body B's face was unknown (empty mask, 'no tessellation')
        while body A's id silently aliased onto a body-B face — the figure painted the
        wrong solid or nothing at all."""
        _img, caption = self._tool()(
            fixtures=[two_box_faces["a_top"]],
            load=[{"face": two_box_faces["b_top"], "force": [0, 0, -100]}],
            view="iso", doc_id=two_box_doc)
        assert "WARNING: the colour overlay could NOT be painted" not in caption, caption
        seen = caption.split("Visible from this camera: ", 1)[1].split(".")[0]
        assert two_box_faces["a_top"] in seen and two_box_faces["b_top"] in seen, seen

    def test_the_same_faces_with_a_different_load_are_a_different_figure(
            self, two_box_doc, two_box_faces):
        """The reported bug: an axial and a radial case on one face pair rendered
        byte-identical, so an evidence reviewer could not tell the two apart."""
        args = dict(fixtures=[two_box_faces["a_bot"]], view="iso", doc_id=two_box_doc)
        axial = self._png(self._tool()(
            load=[{"face": two_box_faces["b_top"], "force": [0, 0, -100]}], **args))
        radial = self._png(self._tool()(
            load=[{"face": two_box_faces["b_top"], "force": [0, -700, 0]}], **args))
        assert axial != radial

    def test_a_zero_resultant_pair_still_draws_its_own_entries(
            self, two_box_doc, two_box_faces):
        """Two opposite entries cancel to zero net force — a couple. The resultant is not
        the load: the figure must still differ from the single-entry one."""
        args = dict(fixtures=[two_box_faces["a_bot"]], view="iso", doc_id=two_box_doc)
        one = self._png(self._tool()(
            load=[{"face": two_box_faces["b_top"], "force": [0, 0, -100]}], **args))
        couple = self._tool()(
            load=[{"face": two_box_faces["b_top"], "force": [0, 0, -100]},
                  {"face": two_box_faces["a_top"], "force": [0, 0, 100]}], **args)
        assert "resultant 0 N" in couple[1]
        assert "2 of 2 load entry(ies) are drawn" in couple[1]
        assert self._png(couple) != one

"""Unit tests for close-up inspection helpers (inspect_part / look_around /
render_views / inspect_feature / emphasis palette).

Monkeypatches the render/query seams — no live engine / paid APIs required.
Run:  python3 -m pytest services/mcp/test_inspect_tools.py -q
"""
from __future__ import annotations

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
from services.mcp import cad_render_tools as ct_render  # noqa: E402  T-RF8: inspect/render tools moved


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
        assert "CROP" in caption or "crop" in caption.lower()
        assert any(m == "doc_render_view" for m, _ in calls)
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
            # Default 7 views (6 ortho + iso) tiled — wider than a single render.
            assert im.size[0] >= 512 and im.size[1] >= 400
        assert "render_views" in caption
        # Default path hits the contact-sheet RPC once.
        assert any(m == "doc_render_contact_sheet" for m, _ in calls)
        # Caption names the standard views.
        for name in ("front", "back", "top", "iso"):
            assert name in caption.lower()

    def test_render_views_custom_list(
            self, monkeypatch, top_face_match, part_bbox, tmp_path):
        calls = self._install_seams(monkeypatch, [top_face_match], part_bbox, tmp_path)
        tool_fn = self._tool("render_views")
        result = tool_fn(doc_id="doc-test", views=["front", "top", "iso"])
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
    def test_review_guide_mentions_inspect(self):
        text = ct.cad_guide_text("review")
        assert "inspect_part" in text
        assert "look_around" in text

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


"""Tests for the PLM graphical-diff surface (services/plm/compare.visual_compare
plus the release-time geometry freeze).

Covers:
  * no engine on the box -> a structured degraded block, never an exception
  * ECAD boards diff with NO engine at all (the stored document carries the
    whole layout) and still return a real ``diff_2d`` image
  * a stubbed engine populates ``diff_3d`` / ``diff_3d_changes_only`` /
    ``diff_3d_views`` + the volume + match fields, and NEVER returns a path for
    a render that failed
  * an engine too old for ``doc_render_diff`` (unknown method) still yields
    ``match_pct`` from ``eval_compare`` plus the ``diff_pixel`` fallback
  * ``freeze_geometry_step`` returns None (not an exception) with no engine and
    bytes with a stubbed engine; ``plm_release`` through the worker seam writes
    ``items/<id>/geometry.step`` with the stub and still succeeds without it

The real engine may or may not be present on the box running this file, so every
test pins ``core_bridge`` explicitly instead of depending on it.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from mcp_server import core_bridge
from services.plm import compare as compare_mod
from services.plm import release as release_mod
from services.plm import snapshot, worker_rpc
from services.plm.vault import Vault

visual_diff = pytest.importorskip("services.plm.visual_diff")
Image = pytest.importorskip("PIL.Image")


# ---------------------------------------------------------------------------
# fixtures / helpers (same conventions as test_plm_vault.py)
# ---------------------------------------------------------------------------

def _container(doc: dict, *, extras: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
        for name, data in (extras or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write(project_dir, name, doc, extras=None):
    (project_dir / name).write_bytes(_container(doc, extras=extras))


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


def _pcb(track_len: float = 20.0) -> dict:
    return {"schema": "aicad.pcb/1", "id": "uuid-pcb-1", "name": "Controller",
            "outline": {"shapes": [
                {"type": "line", "p1": [0, 0], "p2": [40, 0]},
                {"type": "line", "p1": [40, 0], "p2": [40, 30]},
                {"type": "line", "p1": [40, 30], "p2": [0, 30]},
                {"type": "line", "p1": [0, 30], "p2": [0, 0]}]},
            "tracks": [{"points": [[2, 2], [track_len, 2]],
                        "width": 0.25, "layer": "F.Cu"}],
            "footprints": [{"id": "u1", "ref": "U1", "pos": [10, 10]}]}


def _png(path: str, colour: tuple[int, int, int]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (48, 32), (255, 255, 255))
    for x in range(4, 4 + 20):
        for y in range(4, 4 + 12):
            img.putpixel((x, y), colour)
    img.save(path)


class _NoEngine:
    """``core_bridge`` with the library absent."""

    @staticmethod
    def available() -> bool:
        return False

    @staticmethod
    def call(method, params):  # pragma: no cover — must never be reached
        raise AssertionError(f"engine call {method!r} attempted with no engine")


class _StubEngine:
    """A scripted engine: records calls, writes real PNGs, and lets a test make
    one specific ``doc_render_diff`` view fail."""

    def __init__(self, *, fail_views=(), render_diff="ok", export_step=b"ISO-10303-21;\n"):
        self.calls: list[tuple[str, dict]] = []
        self.fail_views = {v.upper() for v in fail_views}
        self.render_diff = render_diff  # "ok" | "unknown_method"
        self.export_step = export_step
        self._n = 0

    def available(self) -> bool:
        return True

    def call(self, method, params):
        self.calls.append((method, dict(params or {})))
        if method == "doc_new":
            self._n += 1
            return {"doc_id": f"d{self._n}"}
        if method in ("doc_apply_ops", "doc_close"):
            return {"ok": True}
        if method == "doc_render_png":
            _png(params["out_path"], (30, 30, 200) if params["doc_id"] == "d1"
                 else (200, 30, 30))
            return {"ok": True}
        if method == "eval_compare":
            return {"match_pct": 71.5, "identical": False}
        if method == "doc_export_step":
            if self.export_step is None:
                return {"ok": False}
            Path(params["path"]).write_bytes(self.export_step)
            return {"ok": True}
        if method == "doc_render_diff":
            if self.render_diff == "unknown_method":
                raise RuntimeError("unknown_method: doc_render_diff")
            view = str(params.get("view") or "ISO").upper()
            if view in self.fail_views:
                return {"ok": False, "reason": "render_failed"}
            _png(params["out_path"], (41, 184, 82))
            return {"ok": True, "path": params["out_path"], "view": view,
                    "mode": params.get("mode", "context"),
                    "match_pct": 88.25, "added_volume_mm3": 1200.5,
                    "removed_volume_mm3": 300.25, "unchanged_volume_mm3": 9000.0,
                    "added_regions": 2, "removed_regions": 1, "identical": False}
        raise RuntimeError(f"unknown_method: {method}")


def _use(monkeypatch, engine) -> None:
    monkeypatch.setattr(core_bridge, "available", engine.available)
    monkeypatch.setattr(core_bridge, "call", engine.call)


# ---------------------------------------------------------------------------
# no engine: degrade, never raise
# ---------------------------------------------------------------------------

def test_visual_compare_without_engine_degrades(monkeypatch, tmp_path):
    _use(monkeypatch, _NoEngine())
    res = compare_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "out")
    assert res["degraded"] is True
    assert res["reason"] == "engine_unavailable"
    assert res["hint"]
    # no broken paths handed back
    assert "diff_3d" not in res


def test_visual_compare_unsupported_kind_degrades(monkeypatch, tmp_path):
    _use(monkeypatch, _NoEngine())
    doc = {"schema": "aicad.cam/1", "id": "u", "name": "Job"}
    res = compare_mod.visual_compare(doc, doc, tmp_path / "out")
    assert res == {"degraded": True, "reason": "unsupported_kind",
                   "hint": res["hint"]}


def test_ecad_visual_compare_needs_no_engine(monkeypatch, tmp_path):
    """An ECAD layout diff must work on a box with no libchudware at all."""
    _use(monkeypatch, _NoEngine())
    res = compare_mod.visual_compare(_pcb(20.0), _pcb(34.0), tmp_path / "out")
    assert res["degraded"] is False
    assert Path(res["diff_2d"]).is_file()
    counts = res["counts"]
    assert counts["added"] >= 1 and counts["removed"] >= 1
    assert res["identical"] is False
    # engine-only fields stay empty rather than fabricated
    assert res["diff_3d"] is None and res["image_a"] is None


# ---------------------------------------------------------------------------
# stubbed engine: the real graphical-diff payload
# ---------------------------------------------------------------------------

def test_visual_compare_populates_diff_renders(monkeypatch, tmp_path):
    eng = _StubEngine(fail_views=("FRONT",))
    _use(monkeypatch, eng)
    res = compare_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "out")

    assert res["degraded"] is False
    assert Path(res["diff_3d"]).is_file()
    assert res["diff_3d"] == res["diff_3d_views"]["ISO"]
    assert Path(res["diff_3d_changes_only"]).is_file()

    # the FRONT render "failed" -> no key, and definitely no broken path
    assert set(res["diff_3d_views"]) == {"ISO", "TOP"}
    for path in res["diff_3d_views"].values():
        assert Path(path).is_file()

    # metrics come from doc_render_diff, not from a second eval_compare
    assert res["match_pct"] == 88.25
    assert res["added_volume_mm3"] == 1200.5
    assert res["removed_volume_mm3"] == 300.25
    assert res["identical"] is False
    assert not any(m == "eval_compare" for m, _ in eng.calls)

    # the plain renders + side-by-side + universal pixel diff all still stand
    for key in ("image_a", "image_b", "side_by_side", "diff_pixel"):
        assert Path(res[key]).is_file(), key

    # the diff was asked for with a shared camera per view and both modes
    modes = {(p.get("view"), p.get("mode")) for m, p in eng.calls
             if m == "doc_render_diff"}
    assert ("ISO", "context") in modes and ("ISO", "changes_only") in modes


def test_visual_compare_view_subset(monkeypatch, tmp_path):
    eng = _StubEngine()
    _use(monkeypatch, eng)
    res = compare_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "out",
                                     ["iso"])
    assert set(res["diff_3d_views"]) == {"ISO"}


def test_old_engine_without_render_diff_degrades_to_pixel(monkeypatch, tmp_path):
    """An older libchudware has no doc_render_diff: keep match_pct from
    eval_compare and fall back to the registered pixel diff."""
    eng = _StubEngine(render_diff="unknown_method")
    _use(monkeypatch, eng)
    res = compare_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "out")

    assert res["degraded"] is False
    assert res["diff_3d"] is None
    assert res["diff_3d_changes_only"] is None
    assert res["diff_3d_views"] == {}
    assert res["match_pct"] == 71.5           # from eval_compare
    assert Path(res["diff_pixel"]).is_file()  # the fallback diff image
    assert any(m == "eval_compare" for m, _ in eng.calls)


def test_visual_compare_result_keys_are_the_contract_block(monkeypatch, tmp_path):
    _use(monkeypatch, _StubEngine())
    res = compare_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "out")
    assert set(res) == {
        "degraded", "match_pct", "identical",
        "added_volume_mm3", "removed_volume_mm3",
        "image_a", "image_b", "side_by_side",
        "diff_3d", "diff_3d_changes_only", "diff_3d_views",
        "diff_2d", "diff_pixel", "counts"}


# ---------------------------------------------------------------------------
# release-time geometry freeze
# ---------------------------------------------------------------------------

def test_freeze_geometry_step_without_engine_is_none(monkeypatch):
    _use(monkeypatch, _NoEngine())
    assert release_mod.freeze_geometry_step(_part(3.0)) is None


def test_freeze_geometry_step_with_engine_returns_bytes(monkeypatch):
    eng = _StubEngine()
    _use(monkeypatch, eng)
    data = release_mod.freeze_geometry_step(_part(3.0))
    assert data == b"ISO-10303-21;\n"
    assert any(m == "doc_export_step" for m, _ in eng.calls)
    assert any(m == "doc_close" for m, _ in eng.calls)  # temp doc not leaked


def test_freeze_geometry_step_swallows_export_failure(monkeypatch):
    _use(monkeypatch, _StubEngine(export_step=None))
    assert release_mod.freeze_geometry_step(_part(3.0)) is None


def test_freeze_geometry_step_skips_non_geometry_kinds(monkeypatch):
    _use(monkeypatch, _StubEngine())
    assert release_mod.freeze_geometry_step({"schema": "aicad.drawing/1"}) is None


def _released_tree(project_dir) -> tuple[dict, str]:
    res = worker_rpc._plm_release({"path": str(project_dir), "item_id": _iid(project_dir),
                                   "message": "first production release",
                                   "confirm": True}, lambda _d: None)
    assert res["ok"] is True, res
    v = Vault.open(str(project_dir))
    try:
        tree = v._bind.read_tree(res["commit"])  # noqa: SLF001 — same package
    finally:
        v.close()
    return res, tree


def _iid(project_dir) -> str:
    v = Vault.open(str(project_dir))
    try:
        v.save("auto")
        return v.manifest()["items"][0]["item_id"]
    finally:
        v.close()


def test_plm_release_freezes_geometry_with_engine(monkeypatch, tmp_path):
    _write(tmp_path, "bracket.step+", _part(3.0))
    eng = _StubEngine()
    _use(monkeypatch, eng)
    res, tree = _released_tree(tmp_path)
    iid = res["entry"]["item_id"] if "item_id" in res["entry"] else None
    step_paths = [p for p in tree if p.endswith("/geometry.step")]
    assert step_paths, sorted(tree)
    assert tree[step_paths[0]] == b"ISO-10303-21;\n"
    assert res["geometry_frozen"] is True
    if iid:
        assert step_paths[0] == snapshot.item_prefix(iid) + "geometry.step"


def test_plm_release_succeeds_without_engine(monkeypatch, tmp_path):
    _write(tmp_path, "bracket.step+", _part(3.0))
    _use(monkeypatch, _NoEngine())
    res, tree = _released_tree(tmp_path)
    assert res["geometry_frozen"] is False
    assert not [p for p in tree if p.endswith("/geometry.step")]
    assert res["rev"]


# ---------------------------------------------------------------------------
# live-engine integration
# ---------------------------------------------------------------------------
# Every test above pins core_bridge to a stub, which proves the orchestration
# but CANNOT catch a drift between the params compare.py sends and the ones
# core/doc/session.cpp::doc_render_diff actually reads — a renamed key would
# keep all of them green. This one test talks to the real engine on a real
# buildable recipe, so the Python<->C++ seam is covered where it matters. It
# skips (not fails) on a box without libchudware, matching the convention in
# services/parts/test_x3_browser_catalog.py.

def _live_part(with_boss: bool) -> dict:
    """A recipe the engine can actually build. Rev B adds a boss and drills a hole."""
    feats = [{"id": "f1", "type": "box",
              "params": {"length": 40, "width": 30, "height": 10}}]
    if with_boss:
        feats += [
            {"id": "f2", "type": "box",
             "params": {"length": 10, "width": 10, "height": 12, "center": [12, 0, 5]}},
            {"id": "f3", "type": "hole",
             "params": {"style": "simple", "end": "through_all", "diameter": 8,
                        "face": {"of": "face", "side": "top"}, "positions": [[-12, 0]]}},
        ]
    return {"schema": "aicad.part/1", "id": "uuid-live-1", "name": "Plate",
            "features": feats}


def _green_red_pixels(png: str) -> tuple[int, int]:
    """Count hue-dominant green/red pixels. Shading darkens the contract colours,
    so classify by dominance over a saturation floor rather than exact match."""
    im = Image.open(png).convert("RGB")
    green = red = 0
    for r, g, b in im.getdata():
        if max(r, g, b) - min(r, g, b) < 30:
            continue
        if g > r and g > b:
            green += 1
        elif r > g and r > b:
            red += 1
    return green, red


@pytest.mark.skipif(not core_bridge.available(),
                    reason="native core (libchudware) not available in this environment")
def test_visual_compare_against_the_real_engine(tmp_path):
    out = tmp_path / "imgs"
    res = compare_mod.visual_compare(_live_part(False), _live_part(True), out)

    assert res["degraded"] is False, res
    assert res["identical"] is False

    # Geometry, not vibes: the through-hole removes exactly pi*r^2*t of material.
    import math
    assert res["removed_volume_mm3"] == pytest.approx(math.pi * 4.0 ** 2 * 10.0, rel=0.02)
    assert res["added_volume_mm3"] > 0.0
    assert 0.0 < res["match_pct"] < 100.0

    # Every advertised image must exist, and the headline diff must be COLOURED
    # like a diff -- both an addition and a removal visible in one frame (the
    # removal is interior, so this also proves the unchanged body is ghosted
    # rather than opaque).
    for key in ("image_a", "image_b", "side_by_side", "diff_3d", "diff_3d_changes_only"):
        assert res[key] and Path(res[key]).is_file(), (key, res.get(key))
    for view in ("ISO", "FRONT", "TOP"):
        assert Path(res["diff_3d_views"][view]).is_file(), view

    green, red = _green_red_pixels(res["diff_3d"])
    assert green > 50, f"added geometry is not visible in the diff render ({green} px)"
    assert red > 50, f"removed geometry is not visible through the ghost ({red} px)"


@pytest.mark.skipif(not core_bridge.available(),
                    reason="native core (libchudware) not available in this environment")
def test_visual_compare_identical_revisions_against_the_real_engine(tmp_path):
    res = compare_mod.visual_compare(_live_part(False), _live_part(False), tmp_path / "same")
    assert res["degraded"] is False, res
    assert res["identical"] is True
    assert res["match_pct"] == pytest.approx(100.0, abs=0.5)
    # An unchanged pair still gets an image, so the UI never shows a blank pane.
    assert Path(res["diff_3d"]).is_file()

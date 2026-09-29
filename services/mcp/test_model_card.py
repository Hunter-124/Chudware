"""CAD file cards (model_card), real thumbnails (model_thumbs / mesh_preview) and the
browse_models explorer."""
from __future__ import annotations

import hashlib
import json
import zipfile

import numpy as np
import pytest
from PIL import Image

from services.mcp import model_thumbs
from services.mcp.fs_tools import register_fs_tools
from services.mcp.mesh_preview import BACKGROUND, render_mesh_png
from services.mcp.model_card import model_card
from services.sim.tessellate import BodyTess, write_aicm


def _tools():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("t")
    register_fs_tools(server)
    return {name: entry.fn for name, entry in server._tool_manager._tools.items()}


def _box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=np.float64)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def _two_boxes():
    """Two 10 mm cubes 30 mm apart along X+Y (the iso view's screen x) — a real
    render shows background between them, a bounding-box fill would not."""
    va, fa = _box((0, 0, 0), (10, 10, 10))
    vb, fb = _box((30, -30, 0), (40, -20, 10))
    return np.vstack([va, vb]), np.vstack([fa, fb + len(va)])


def _step_plus(path, *, document, cache=None, meta=None, mesh=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", json.dumps(meta or {"kind": "part", "name": document["name"]}))
        zf.writestr("document.json", json.dumps(document))
        if cache is not None:
            zf.writestr("cache.json", json.dumps(cache))
        if mesh is not None:
            pos, idx = mesh
            body = BodyTess(body_id=1, name="b1", color=(0.62, 0.70, 0.82),
                            pos=pos.astype(np.float32), nrm=np.zeros_like(pos, np.float32),
                            idx=idx.astype(np.uint32))
            zf.writestr("cache.aicm", write_aicm([body]))


def _part_doc(name="bracket"):
    features = ([{"id": f"f{i}", "type": "box", "name": f"Box{i}"} for i in range(1, 3)]
                + [{"id": "f3", "type": "fillet", "name": "Round", "suppressed": True}])
    return {"schema": "aicad.part/1", "name": name, "features": features,
            "material": {"key": "pla"}}


def _write_stl(path, verts, faces):
    record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    data = np.zeros(len(faces), dtype=record)
    data["v"] = verts[faces]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + len(faces).to_bytes(4, "little") + data.tobytes())


# --------------------------------------------------------------------------- cards


def test_part_card_reads_tree_rebuild_and_mesh_without_opening(tmp_path):
    p = tmp_path / "bracket.step+"
    cache = {"response": {"revision": 7, "volume": 7000.0,
                          "rebuild": {"feature_status": [{"id": "f1", "ok": True},
                                                         {"id": "f2", "ok": False}]},
                          "tess": {"bodies": [{"name": "b1"}]}}}
    _step_plus(p, document=_part_doc(), cache=cache, mesh=_two_boxes())
    card = model_card(p)
    assert (card["kind"], card["name"], card["material"]) == ("part", "bracket", "pla")
    assert card["features"] == 3 and card["feature_types"] == {"box": 2, "fillet": 1}
    assert card["suppressed_features"] == 1 and card["failing_features"] == ["f2"]
    assert card["bbox_mm"] == [40.0, 40.0, 10.0]
    assert card["volume_cm3"] == 7.0 and card["revision"] == 7
    for text in ("part 'bracket'", "40×40×10 mm", "rev 7", "⚠ 1 failing feature"):
        assert text in card["summary"]


def test_content_key_is_the_plm_graph_document_hash(tmp_path):
    """The GUI's thumbnail cache is named by cad_graph's meta.dh (blake2b-8 of the raw
    document.json); the card must key the same file, or neither side reuses a render."""
    p = tmp_path / "bracket.step+"
    _step_plus(p, document=_part_doc(), mesh=_two_boxes())
    raw = zipfile.ZipFile(p).read("document.json")
    assert model_card(p)["content_key"] == hashlib.blake2b(raw, digest_size=8).hexdigest()


def test_assembly_card_counts_components_and_mates(tmp_path):
    p = tmp_path / "arm.step+"
    doc = {"schema": "aicad.asm/1", "name": "arm",
           "components": [{"id": "k1", "name": "a", "source": {"path": "parts/a.step+"}},
                          {"id": "k2", "name": "b", "source": {"path": "parts/b.step+"}},
                          {"id": "k3", "name": "b2", "source": {"path": "parts/b.step+"}}],
           "mates": [{"id": "m1", "type": "concentric"}, {"id": "m2", "type": "angle"}]}
    _step_plus(p, document=doc, meta={"kind": "assembly", "name": "arm"})
    card = model_card(p)
    assert "3 components (2 unique) · 2 mates" in card["summary"]
    assert "no cached mesh" in card["geometry"]


def test_binary_stl_card(tmp_path):
    v, f = _box((0, 0, 0), (12, 8, 4))
    p = tmp_path / "block.stl"
    _write_stl(p, v, f)
    card = model_card(p)
    assert card["kind"] == "mesh" and card["triangles"] == 12
    assert card["bbox_mm"] == [12.0, 8.0, 4.0]
    assert card["content_key"].startswith("stl-")


# --------------------------------------------------------------------------- renders


def test_mesh_render_draws_the_parts_not_their_bounding_box(tmp_path):
    pos, tris = _two_boxes()
    out = tmp_path / "r.png"
    render_mesh_png(pos, tris, np.tile([0.62, 0.70, 0.82], (len(tris), 1)), out)
    img = np.asarray(Image.open(out).convert("RGB")).astype(int)
    assert img.shape == (400, 512, 3)
    part = np.abs(img - np.array(BACKGROUND)).max(axis=2) > 12
    assert 0.04 < part.mean() < 0.6
    # Along the row through both cubes the image goes part → background → part.
    row = part[part.any(axis=1)][part[part.any(axis=1)].sum(axis=1).argmax()]
    runs = np.flatnonzero(np.diff(row.astype(int)) != 0)
    assert len(runs) >= 4, "expected two separate silhouettes on one scanline"
    # Faces are shaded differently and edges are drawn: more than two colours on the part.
    assert len({tuple(c) for c in img[part][::7]}) > 3


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.setattr("services.paths.ROOT", tmp_path / "appdata")
    # Unit tests never reach an engine: an exchange file (part.step) is an engine-only
    # render, so it must come out as a named failure rather than depend on the box.
    monkeypatch.setattr(model_thumbs, "_render_engine",
                        lambda card, out: (_ for _ in ()).throw(RuntimeError("no engine here")))
    (tmp_path / "parts").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / ".hidden").mkdir()
    _step_plus(tmp_path / "parts" / "bracket.step+", document=_part_doc(), mesh=_two_boxes())
    v, f = _box((0, 0, 0), (12, 8, 4))
    _write_stl(tmp_path / "parts" / "block.stl", v, f)
    _step_plus(tmp_path / "archive" / "bracket_old.step+", document=_part_doc("old"),
               mesh=_two_boxes())
    _step_plus(tmp_path / ".hidden" / "x.step+", document=_part_doc("hidden"), mesh=_two_boxes())
    (tmp_path / "part.step").write_text("ISO-10303-21;\nHEADER;\nENDSEC;\n")
    (tmp_path / "notes.txt").write_text("not a model")
    return tmp_path


def test_thumbnail_renders_from_the_file_mesh_into_the_shared_gui_cache(project, monkeypatch):
    engine_calls = []
    monkeypatch.setattr(model_thumbs, "_render_engine",
                        lambda card, out: engine_calls.append(card["file"]))
    card = model_card(project / "parts" / "bracket.step+")
    first = model_thumbs.ensure_thumbnail(card)
    assert first["status"] == "rendered" and first["source"] == "mesh"
    assert first["path"] == str(project / ".plm" / "thumbs" / f"{card['content_key']}.png")
    assert model_thumbs.ensure_thumbnail(card)["status"] == "cached"
    assert engine_calls == []  # the file's own mesh sufficed; no document was opened


def test_a_step_plus_without_mesh_goes_to_the_engine_and_failures_are_named(project, monkeypatch):
    p = project / "parts" / "ref_asm.step+"
    _step_plus(p, document={"schema": "aicad.asm/1", "name": "ref", "components": []})
    card = model_card(p)

    def engine(card, out):
        Image.new("RGB", (512, 400), (120, 130, 140)).save(out, format="PNG")
        card["_measured"] = {"bbox_mm": [80.0, 60.0, 5.0], "bbox_min_mm": [0.0, 0.0, 0.0]}

    monkeypatch.setattr(model_thumbs, "_render_engine", engine)
    got = model_thumbs.ensure_thumbnail(card)
    assert got["status"] == "rendered" and got["source"] == "engine"
    # The size the engine measured captions the tile on every later (cached) listing.
    tiles, counts = model_thumbs.build_tiles([("ref_asm.step+", model_card(p))],
                                             render=True, deadline=None)
    assert counts == {"cached": 1} and tiles[0]["card"]["bbox_mm"] == [80.0, 60.0, 5.0]

    other = project / "parts" / "ref_asm2.step+"
    _step_plus(other, document={"schema": "aicad.asm/1", "name": "ref2", "components": []})
    monkeypatch.setattr(model_thumbs, "_render_engine",
                        lambda card, out: (_ for _ in ()).throw(RuntimeError("no bodies")))
    failed = model_thumbs.ensure_thumbnail(model_card(other))
    assert failed["status"] == "unavailable" and "no bodies" in failed["reason"]


def test_render_budget_leaves_the_rest_pending_and_a_repeat_continues(project):
    card = model_card(project / "parts" / "bracket.step+")
    assert model_thumbs.ensure_thumbnail(card, deadline=0.0)["status"] == "pending"
    assert model_thumbs.ensure_thumbnail(card)["status"] == "rendered"
    assert model_thumbs.ensure_thumbnail(card, deadline=0.0)["status"] == "cached"


# --------------------------------------------------------------------------- tools


def test_browse_models_returns_numbered_sheets_live_models_first(project):
    out = _tools()["browse_models"](".")
    assert out["ok"] and out["count"] == 4  # hidden folder skipped, notes.txt ignored
    assert [m["file"] for m in out["models"]] == [
        "part.step", "parts/block.stl", "parts/bracket.step+", "archive/bracket_old.step+"]
    assert [m["n"] for m in out["models"]] == [1, 2, 3, 4]
    assert "summary" not in out["models"][0]  # the tile caption carries it
    assert out["models"][0]["preview"].startswith("unavailable")
    [sheet] = out["sheets"]
    assert sheet["kind"] == "image"
    width, height = Image.open(sheet["path"]).size
    assert width > 4 * 300 and 250 < height < 400  # one row of four labelled tiles
    assert out["renders"] == {"unavailable": 1, "rendered": 3}


def test_browse_models_without_sheets_is_text_only_with_summaries(project):
    out = _tools()["browse_models"](".", sheets=False, limit=2)
    assert "sheets" not in out and out["shown"] == 2
    assert "40×40×10 mm" not in out["models"][0]["summary"]  # part.step: header card only
    assert out["not_shown"] == ["parts/bracket.step+", "archive/bracket_old.step+"]


def test_fs_list_card_and_fs_read_preview_image(project):
    tools = _tools()
    rows = {r["name"]: r for r in tools["fs_list"]("parts")["entries"]}
    assert "40×40×10 mm" in rows["bracket.step+"]["card"]
    read = tools["fs_read"]("parts/bracket.step+")
    assert read["ok"] and read["kind"] == "cad_preview" and "import_part" in read["hint"]
    assert read["preview"]["kind"] == "image"
    assert Image.open(read["preview"]["path"]).size[0] >= 300

"""Text thumbnails of CAD files (model_card) and the browse_models explorer."""
from __future__ import annotations

import json
import zipfile

import numpy as np
import pytest

from services.mcp.fs_tools import register_fs_tools
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


def _l_shape():
    """40 x 10 bar along X plus a 10 x 30 bar along Y at x=0: an L seen from above."""
    va, fa = _box((0, 0, 0), (40, 10, 10))
    vb, fb = _box((0, 10, 0), (10, 30, 10))
    return np.vstack([va, vb]), np.vstack([fa, fb + len(va)])


def _step_plus(path, *, document, cache=None, meta=None, mesh=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", json.dumps(meta or {"kind": "part", "name": document["name"]}))
        zf.writestr("document.json", json.dumps(document))
        if cache is not None:
            zf.writestr("cache.json", json.dumps(cache))
        if mesh is not None:
            pos, idx = mesh
            body = BodyTess(body_id=1, name="b1", color=(0.7, 0.7, 0.7),
                            pos=pos.astype(np.float32), nrm=np.zeros_like(pos, np.float32),
                            idx=idx.astype(np.uint32))
            zf.writestr("cache.aicm", write_aicm([body]))


def _part_doc():
    features = ([{"id": f"f{i}", "type": "box", "name": f"Box{i}"} for i in range(1, 3)]
                + [{"id": "f3", "type": "fillet", "name": "Round", "suppressed": True}])
    return {"schema": "aicad.part/1", "name": "bracket", "features": features,
            "material": {"key": "pla"}}


def test_part_card_reads_tree_rebuild_and_mesh_without_opening(tmp_path):
    p = tmp_path / "bracket.step+"
    cache = {"response": {"revision": 7, "volume": 7000.0,
                          "rebuild": {"feature_status": [{"id": "f1", "ok": True},
                                                         {"id": "f2", "ok": False}]},
                          "tess": {"bodies": [{"name": "b1"}]}}}
    _step_plus(p, document=_part_doc(), cache=cache, mesh=_l_shape())
    card = model_card(p)
    assert (card["kind"], card["name"], card["material"]) == ("part", "bracket", "pla")
    assert card["features"] == 3 and card["feature_types"] == {"box": 2, "fillet": 1}
    assert card["suppressed_features"] == 1
    assert card["failing_features"] == ["f2"]
    assert card["bbox_mm"] == [40.0, 30.0, 10.0]
    assert card["volume_cm3"] == 7.0 and card["revision"] == 7
    for text in ("part 'bracket'", "40×30×10 mm", "rev 7", "⚠ 1 failing feature"):
        assert text in card["summary"]


def test_top_thumbnail_shows_the_shape_not_its_bounding_box(tmp_path):
    p = tmp_path / "l.step+"
    _step_plus(p, document=_part_doc(), mesh=_l_shape())
    top = model_card(p)["thumbnail"]["top"]
    cols = max(len(row) for row in top)
    far_row, near_row = top[0], top[-1]  # +Y (arm tip) first, y=0 (bar) last
    assert len(far_row.rstrip()) <= cols // 3 + 1  # only the 10 mm arm at +Y
    assert sum(ch != " " for ch in near_row) >= cols - 1  # the full 40 mm bar
    # Taller-than-wide glyphs: a 40 x 30 footprint is ~28 cols by ~10 rows.
    assert 8 <= len(top) <= 12


def test_front_view_shades_nearer_faces_denser(tmp_path):
    # A step: a low slab in front (small y) and a tall block behind it.
    va, fa = _box((0, 0, 0), (20, 10, 5))
    vb, fb = _box((0, 10, 0), (20, 20, 20))
    p = tmp_path / "step.stl"
    _write_stl(p, np.vstack([va, vb]), np.vstack([fa, fb + len(va)]))
    front = model_card(p)["thumbnail"]["front"]
    shades = " .:-=+*#%@"
    top_glyph = front[0].strip()[0]      # only the back block reaches z=20
    bottom_glyph = front[-1].strip()[0]  # the slab's front face at y=0 is nearest
    assert shades.index(bottom_glyph) > shades.index(top_glyph)


def _write_stl(path, verts, faces):
    record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    data = np.zeros(len(faces), dtype=record)
    data["v"] = verts[faces]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + len(faces).to_bytes(4, "little") + data.tobytes())


def test_binary_stl_card(tmp_path):
    v, f = _box((0, 0, 0), (12, 8, 4))
    p = tmp_path / "block.stl"
    _write_stl(p, v, f)
    card = model_card(p)
    assert card["kind"] == "mesh" and card["triangles"] == 12
    assert card["bbox_mm"] == [12.0, 8.0, 4.0]
    assert "12 triangles" in card["summary"]


def test_assembly_card_without_cached_mesh_says_so(tmp_path):
    p = tmp_path / "arm.step+"
    doc = {"schema": "aicad.asm/1", "name": "arm",
           "components": [{"id": "k1", "name": "a", "source": {"path": "parts/a.step+"}},
                          {"id": "k2", "name": "b", "source": {"path": "parts/b.step+"}},
                          {"id": "k3", "name": "b2", "source": {"path": "parts/b.step+"}}],
           "mates": [{"id": "m1", "type": "concentric"}, {"id": "m2", "type": "angle"}]}
    _step_plus(p, document=doc, meta={"kind": "assembly", "name": "arm"})
    card = model_card(p)
    assert "3 components (2 unique)" in card["summary"] and "2 mates" in card["summary"]
    assert "thumbnail" not in card and "no cached mesh" in card["geometry"]


def test_card_follows_the_file_when_it_changes(tmp_path):
    p = tmp_path / "bracket.step+"
    _step_plus(p, document=_part_doc(), mesh=_l_shape())
    assert model_card(p)["bbox_mm"] == [40.0, 30.0, 10.0]
    doc = _part_doc()
    doc["name"] = "bracket_rev_b"
    _step_plus(p, document=doc, mesh=_box((0, 0, 0), (5, 5, 5)))
    card = model_card(p)
    assert card["name"] == "bracket_rev_b" and card["bbox_mm"] == [5.0, 5.0, 5.0]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    (tmp_path / "parts").mkdir()
    (tmp_path / ".archive").mkdir()
    _step_plus(tmp_path / "parts" / "bracket.step+", document=_part_doc(), mesh=_l_shape())
    v, f = _box((0, 0, 0), (12, 8, 4))
    _write_stl(tmp_path / "parts" / "block.stl", v, f)
    _step_plus(tmp_path / ".archive" / "old.step+", document=_part_doc())
    (tmp_path / "notes.txt").write_text("not a model")
    return tmp_path


def test_browse_models_lists_every_model_with_thumbnails(project):
    out = _tools()["browse_models"](".")
    assert out["ok"] and out["count"] == 2
    assert [m["file"] for m in out["models"]] == ["parts/block.stl", "parts/bracket.step+"]
    assert all(m["thumbnail"]["top"] for m in out["models"])
    assert "denser glyph" in out["legend"]


def test_browse_models_limit_names_the_rest_and_thumbnails_can_be_off(project):
    out = _tools()["browse_models"](".", limit=1, thumbnails=False)
    assert out["shown"] == 1 and out["not_shown"] == ["parts/bracket.step+"]
    assert "thumbnail" not in out["models"][0]


def test_fs_list_rows_carry_card_and_fs_read_previews(project):
    tools = _tools()
    rows = {r["name"]: r for r in tools["fs_list"]("parts")["entries"]}
    assert "40×30×10 mm" in rows["bracket.step+"]["card"]
    read = tools["fs_read"]("parts/bracket.step+")
    assert read["ok"] and read["kind"] == "cad_preview"
    assert read["card"]["thumbnail"]["front"] and "import_part" in read["hint"]

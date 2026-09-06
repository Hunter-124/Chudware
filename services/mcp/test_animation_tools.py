"""render_animation live regression — the film boundary over the real engine + ffmpeg.

Two things can silently break an animation and cannot be caught by inspecting the code:
the encoder can write a container that plays back with the wrong number of frames, and the
caption can stop carrying the digest a report cites. Both are asserted against the WRITTEN
file (ffprobe reads it back), not against the tool's own bookkeeping.

Needs the engine (libchudware) for the CAD kind and ffmpeg for both; each is a SKIP, not a
failure, on an unprovisioned box.

Run:  .venv-mcp/bin/python -m pytest services/mcp/test_animation_tools.py -q -p no:randomly
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp.cad_animation_tools import register_cad_animation_tools  # noqa: E402
from services.mcp.cad_tools import register_cad_tools  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or
                                  shutil.which("ffprobe") is None,
                                  reason="ffmpeg/ffprobe not installed")


def _engine_up() -> bool:
    try:
        cb.call("ping", {})
        return True
    except Exception:  # noqa: BLE001
        return False


needs_engine = pytest.mark.skipif(not _engine_up(), reason="libchudware/hostd unavailable")


class _CaptureMCP:
    """FastMCP stand-in that just collects the @mcp.tool() registrations."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *_a, **_k):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn
        return dec


@pytest.fixture(scope="module")
def tools() -> dict:
    m = _CaptureMCP()
    register_cad_tools(m)
    register_cad_animation_tools(m)
    return m.tools


def _probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=width,height,nb_read_frames",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    js = json.loads(out)
    st = (js.get("streams") or [{}])[0]
    return {"frames": int(st.get("nb_read_frames") or 0),
            "width": int(st.get("width") or 0),
            "duration_s": float((js.get("format") or {}).get("duration") or 0.0)}


@needs_engine
@needs_ffmpeg
def test_turntable_of_a_box_writes_a_gif_whose_frames_and_digest_are_reported(tools, tmp_path,
                                                                             monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(tools["new_part"](name="anim_turntable_box"))["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=[{
        "op": "add_feature",
        "feature": {"type": "box", "name": "Slab",
                    "params": {"length": 60, "width": 40, "height": 20}}}]))
    assert built["ok"], built

    out = str(tmp_path / "turntable.gif")
    res = tools["render_animation"](kind="turntable", doc_id=doc, save_path=out,
                                    frames=12, fps=12, size=[320, 240])
    assert isinstance(res, list) and len(res) == 2, res
    caption = res[1]
    assert os.path.exists(out), caption

    # The written film really has the frames the caption claims, read back by ffprobe.
    probe = _probe(out)
    assert probe["frames"] == 12, (probe, caption)
    assert f"{probe['frames']} frames" in caption, caption

    # A report cites the digest, so the caption must carry it and it must be the file's.
    import hashlib
    sha = hashlib.sha256(Path(out).read_bytes()).hexdigest()[:12]
    assert "sha256 " + sha in caption, caption
    assert str(os.path.getsize(out)) in caption, caption

    # The poster beside the film is a real PNG of the same shot.
    poster = tmp_path / "poster_turntable.png"
    assert poster.exists() and poster.stat().st_size > 0, caption
    assert str(poster) in caption, caption


@needs_engine
def test_a_relative_save_path_is_refused_before_any_render(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(tools["new_part"](name="anim_relpath"))["doc_id"]
    res = tools["render_animation"](kind="turntable", doc_id=doc,
                                    save_path="films/turntable.gif", frames=4, fps=4)
    payload = json.loads(res[0])
    assert payload["ok"] is False
    assert "ABSOLUTE" in payload["error"], payload
    assert not (tmp_path / "films").exists()


@needs_engine
def test_explode_refuses_a_part_that_has_no_components(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(tools["new_part"](name="anim_explode_part"))["doc_id"]
    assert json.loads(tools["add_features"](doc_id=doc, ops=[{
        "op": "add_feature", "feature": {"type": "box", "name": "B",
                                         "params": {"length": 10, "width": 10,
                                                    "height": 10}}}]))["ok"]
    assert json.loads(tools["save_part"](path=str(tmp_path / "p.step+"), doc_id=doc))["ok"]
    res = tools["render_animation"](kind="explode", doc_id=doc,
                                    save_path=str(tmp_path / "x.gif"), frames=4, fps=4)
    payload = json.loads(res[0])
    assert payload["ok"] is False
    assert "components" in payload["error"], payload


@needs_ffmpeg
def test_fea_kind_animates_a_solved_field_into_a_playable_mp4(tmp_path, monkeypatch):
    """A tiny two-tet grid with a von Mises field and a displacement vector, written here as
    a real .vtu, must come back as an mp4 whose peak stress is the field's own maximum."""
    pv = pytest.importorskip("pyvista")
    import numpy as np

    pts = np.array([[0.0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10], [10, 10, 10]])
    cells = np.hstack([[4, 0, 1, 2, 3], [4, 1, 2, 3, 4]])
    grid = pv.UnstructuredGrid(cells, np.full(2, pv.CellType.TETRA), pts)
    grid.point_data["von_mises"] = np.array([1.0, 12.5, 4.0, 7.0, 33.25])
    grid.point_data["displacement"] = np.tile(np.array([[0.0, 0.0, 0.25]]), (5, 1))
    vtu = tmp_path / "tiny.vtu"
    grid.save(str(vtu))

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    m = _CaptureMCP()
    register_cad_animation_tools(m)
    out = str(tmp_path / "field.mp4")
    res = m.tools["render_animation"](kind="fea", vtu_path=str(vtu), save_path=out,
                                      frames=8, fps=8, size=[320, 240])
    assert isinstance(res, list) and len(res) == 2, res
    caption = res[1]
    assert os.path.exists(out), caption
    probe = _probe(out)
    assert probe["frames"] == 8, (probe, caption)
    assert probe["duration_s"] > 0.0, probe
    measured = json.loads(caption.split("measured: ", 1)[1].split("  render wall time")[0])
    assert measured["peak_field"] == pytest.approx(33.25), measured
    assert measured["n_cells"] == 2 and measured["n_points"] == 5, measured
    assert measured["peak_disp_mm"] == pytest.approx(0.25), measured
    assert "sha256 " in caption


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:randomly"]))

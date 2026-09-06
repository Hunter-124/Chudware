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


@needs_engine
@needs_ffmpeg
def test_a_steer_sweep_films_the_solved_kingpin_rotation(tools, tmp_path, monkeypatch):
    """A steering assembly owns NO dimensional mate — its wheels are posed concentric to
    kingpin pins — so kind='motion' with `mate_id` has nothing to drive and a steer film
    used to be hand-built frame by frame. `steer=` must drive it: the film carries the
    requested frame count and the caption names the angle range it swept."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def _part(name: str, ops: list) -> str:
        doc = json.loads(tools["new_part"](name=name))["doc_id"]
        assert json.loads(tools["add_features"](doc_id=doc, ops=ops))["ok"]
        path = str(tmp_path / f"{name}.step+")
        assert json.loads(tools["save_part"](path=path, doc_id=doc))["ok"]
        return path

    # A Ø300 x 100 disc laid over so it spins about the vehicle's lateral axis, with a
    # vertical kingpin bore through its centre — the same topology as a real upright.
    wheel = _part("anim_steer_wheel", [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Disc",
                                          "params": {"radius": 150, "height": 100}}},
        {"op": "add_feature", "feature": {"type": "transform", "name": "LayOver",
                                          "params": {"rotate": [1, 0, 0], "angle": 90}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "KingpinBore",
                                          "params": {"radius": 25, "height": 600,
                                                     "mode": "cut"}}}])
    pin = _part("anim_steer_pin", [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Pin",
                                          "params": {"radius": 24, "height": 400}}}])

    asm = json.loads(tools["new_assembly"](name="anim_steer_axle"))["doc_id"]
    stations = {"L": 400.0, "R": -400.0}
    for tag, y in stations.items():
        p = json.loads(tools["asm_add_component"](doc_id=asm, path=pin, fixed=True,
                                                  name=f"kingpin_{tag}", at=[0, y, 0]))
        w = json.loads(tools["asm_add_component"](doc_id=asm, path=wheel,
                                                  name=f"wheel_{tag}", at=[0, y, 0]))
        assert p["ok"] and w["ok"], (p, w)
        m = json.loads(tools["asm_mate"](
            doc_id=asm, mate_type="concentric",
            a={"component": p["component"], "query": {"of": "face", "surf": "round"}},
            b={"component": w["component"], "query": {"of": "face", "surf": "round",
                                                      "pick": "smallest"}}))
        assert m["ok"] and m["converged"], m
    tree = cb.call("doc_tree", {"doc_id": asm})["tree"]
    assert not [m for m in tree.get("mates", [])
                if m.get("type") in ("angle", "distance")], "no drivable mate must exist"
    assert json.loads(tools["save_part"](path=str(tmp_path / "axle.step+"),
                                         doc_id=asm))["ok"]

    out = str(tmp_path / "steer.gif")
    res = tools["render_animation"](
        kind="motion", doc_id=asm, save_path=out, frames=8, fps=8, size=[320, 240],
        steps=3,
        steer={"components": ["wheel_L", "wheel_R"],
               "axis": [{"point": [0, 400, 0], "dir": [0, 0, 1]},
                        {"point": [0, -400, 0], "dir": [0, 0, 1]}],
               "angle_deg": 30})
    assert isinstance(res, list) and len(res) == 2, res
    caption = res[1]
    assert os.path.exists(out), caption

    probe = _probe(out)
    assert probe["frames"] == 8, (probe, caption)
    measured = json.loads(caption.split("measured: ", 1)[1].split("  render wall time")[0])
    # The caption names the range that was swept, and the poses are the solver's own
    # samples held across the film — not 8 interpolations.
    assert measured["steer_range_deg"] == [0.0, 30.0], measured
    assert measured["steer_components"] == ["wheel_L", "wheel_R"], measured
    assert measured["steer_angles_deg"] == [30.0, 30.0], measured
    assert measured["samples"] == 3, measured
    assert "3 rendered" in caption, caption
    assert '"steer_range_deg": [0.0, 30.0]' in caption, caption

    # The film is a clone's business: the live assembly is untouched.
    after = cb.call("doc_tree", {"doc_id": asm})["tree"]
    assert [c.get("transform") for c in after["components"]] == \
        [c.get("transform") for c in tree["components"]]


@needs_engine
def test_a_non_motion_kind_refuses_a_steer_spec_instead_of_ignoring_it(tools, tmp_path,
                                                                      monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(tools["new_part"](name="anim_steer_offkind"))["doc_id"]
    res = tools["render_animation"](kind="turntable", doc_id=doc,
                                    save_path=str(tmp_path / "t.gif"), frames=4, fps=4,
                                    steer={"components": ["x"], "angle_deg": 10})
    payload = json.loads(res[0])
    assert payload["ok"] is False
    assert "kind='motion'" in payload["error"], payload

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

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
from services.mcp import cad_animation_tools as anim  # noqa: E402
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
                                    frames=12, fps=12, size=[320, 240], keyframes=0)
    assert isinstance(res, list) and len(res) == 2, res
    caption = res[1]
    assert os.path.exists(out), caption

    # The written film really has the frames the caption claims, read back by ffprobe.
    probe = _probe(out)
    assert probe["frames"] == 12, (probe, caption)
    assert f"{probe['frames']} frames" in caption, caption
    assert probe["duration_s"] == pytest.approx(1.0, abs=0.02), probe
    measured = json.loads(caption.split("measured: ", 1)[1].split("  render wall time")[0])
    scales = measured["framing"]["output_pixels_per_mm_range"]
    assert scales[0] > 0
    assert scales[1] == pytest.approx(scales[0], rel=1e-6), measured["framing"]

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
def test_a_relative_save_path_without_a_project_root_is_refused_before_any_render(
        tools, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
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
    assert isinstance(res, list) and len(res) == 3, res
    caption = res[2]
    assert os.path.exists(out), caption

    probe = _probe(out)
    assert probe["duration_s"] == pytest.approx(1.0, abs=0.02), probe
    measured = json.loads(caption.split("measured: ", 1)[1].split("  render wall time")[0])
    # The caption names the range that was swept, and the poses are the solver's own
    # samples held across the film — not 8 interpolations.
    assert measured["steer_range_deg"] == [0.0, 30.0], measured
    assert measured["steer_components"] == ["wheel_L", "wheel_R"], measured
    assert measured["steer_angles_deg"] == [30.0, 30.0], measured
    assert measured["samples"] == 3, measured

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
    assert isinstance(res, list) and len(res) == 3, res
    caption = res[2]
    assert os.path.exists(out), caption
    probe = _probe(out)
    assert probe["frames"] == 8, (probe, caption)
    assert probe["duration_s"] == pytest.approx(1.0, abs=0.02), probe
    measured = json.loads(caption.split("measured: ", 1)[1].split("  render wall time")[0])
    assert measured["peak_field"] == pytest.approx(33.25), measured
    assert measured["n_cells"] == 2 and measured["n_points"] == 5, measured
    assert measured["peak_disp_mm"] == pytest.approx(0.25), measured
    assert "sha256 " in caption


def test_keyframe_sheet_contains_both_endpoints_and_first_collision(tmp_path):
    from PIL import Image

    frames = []
    for index in range(13):
        path = tmp_path / f"f{index:05d}.png"
        Image.new("RGB", (960, 540), (index * 17, 50, 75)).save(path)
        frames.append(str(path))
    sheet_path = tmp_path / "keyframes_motion.png"
    indices = anim._keyframe_sheet(frames, str(sheet_path), fps=10, count=6,
                                   collision_index=4)
    assert indices == [0, 2, 4, 7, 10, 12]
    assert sheet_path.exists()
    with Image.open(sheet_path) as sheet:
        assert sheet.format == "PNG"
        assert sheet.size == (1600, 602)  # three 528x297 columns, two rows, 8px gaps
        for tile, index in enumerate(indices):
            x, y = (tile % 3) * 536, (tile // 3) * 305
            assert sheet.getpixel((x + 264, y + 150)) == (index * 17, 50, 75)
            corner = sheet.crop((x, y, x + 160, y + 55))
            has_collision_label = (255, 135, 105) in corner.get_flattened_data()
            assert has_collision_label == (index == 4)
    assert anim._keyframe_indices(13, 2, 4) == [0, 4, 12]
    assert anim._keyframe_indices(13, 1) == [0, 12]
    assert anim._keyframe_indices(20, 100)[0] == 0
    assert anim._keyframe_indices(20, 100)[-1] == 19
    assert len(anim._keyframe_indices(20, 100)) == 12
    assert anim._keyframe_indices(13, 0) == []


@needs_ffmpeg
def test_keyframes_result_and_disabled_shape_without_engine(tmp_path, monkeypatch):
    """Render synthetic field PNGs through the actual film/preview output boundary."""
    from PIL import Image

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    vtu = tmp_path / "field.vtu"
    vtu.write_text("synthetic renderer input", encoding="utf-8")

    def fake_fea_frames(_path, out_dir, count, size, _view, _warp, _field):
        paths = []
        for index in range(count):
            path = os.path.join(out_dir, f"f{index:05d}.png")
            Image.new("RGB", size, (index * 20, 50, 75)).save(path)
            paths.append(path)
        return {"frames": paths, "field": "von_mises", "peak_field": 12.0,
                "warp_factor": 1.0}

    monkeypatch.setattr(anim, "_fea_frames", fake_fea_frames)
    monkeypatch.setattr(anim, "_provenance", lambda: {
        "engine_build_id": None, "engine_build_id_source": "synthetic frames",
        "polymesh_commit": None, "polymesh_commit_source": "synthetic frames"})
    m = _CaptureMCP()
    register_cad_animation_tools(m)
    render = m.tools["render_animation"]
    film = tmp_path / "synthetic.gif"
    result = render(kind="fea", vtu_path=str(vtu), save_path=str(film),
                    frames=5, fps=5, size=[320, 240])
    assert len(result) == 3, result
    assert film.exists()
    sheet = tmp_path / "keyframes_synthetic.png"
    assert sheet.exists() and str(sheet) in result[2]
    assert "frames: [0, 1, 2, 3, 4]" in result[2]
    with Image.open(sheet) as image:
        assert image.size[0] <= 1600
        assert image.getpixel((150, 100)) == (0, 50, 75)
        assert image.getpixel((480, 348)) == (80, 50, 75)

    no_sheet = render(kind="fea", vtu_path=str(vtu),
                      save_path=str(tmp_path / "disabled.gif"),
                      frames=5, fps=5, size=[320, 240], keyframes=0)
    assert len(no_sheet) == 2, no_sheet
    assert not (tmp_path / "keyframes_disabled.png").exists()
    assert "keyframes " not in no_sheet[1]

    def sheet_failure(*_args, **_kwargs):
        raise OSError("preview filesystem unavailable")

    monkeypatch.setattr(anim, "_keyframe_sheet", sheet_failure)
    failed_sheet = render(kind="fea", vtu_path=str(vtu),
                          save_path=str(tmp_path / "failed_sheet.gif"),
                          frames=5, fps=5, size=[320, 240])
    assert len(failed_sheet) == 2, failed_sheet
    assert (tmp_path / "failed_sheet.gif").exists()
    assert "[keyframes unavailable: preview filesystem unavailable]" in failed_sheet[1]


@needs_ffmpeg
def test_reduced_gif_preserves_duration_and_both_endpoint_images(tmp_path):
    from PIL import Image, ImageDraw
    for i in range(30):
        image = Image.new("RGB", (120, 80), (20, 30, 40))
        draw = ImageDraw.Draw(image)
        draw.rectangle((i * 3, 20, i * 3 + 10, 60), fill=(240, 40, 40))
        image.save(tmp_path / f"f{i:05d}.png")
    gif = tmp_path / "reduced.gif"
    anim._encode_gif(str(tmp_path / "f%05d.png"), str(gif), 30, output_fps=10)
    probe = _probe(str(gif))
    assert probe["duration_s"] == pytest.approx(1.0, abs=0.01), probe
    assert probe["frames"] == 10, probe
    with Image.open(gif) as image:
        first = image.convert("RGB")
        image.seek(image.n_frames - 1)
        last = image.convert("RGB")
    assert first.getpixel((5, 30))[0] > 200
    assert first.getpixel((92, 30))[0] < 60
    assert last.getpixel((5, 30))[0] < 60
    assert last.getpixel((92, 30))[0] > 200


@needs_engine
def test_end_on_slender_part_keeps_scale_without_clipping(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(tools["new_part"](name="slender_orbit"))["doc_id"]
    try:
        built = json.loads(tools["add_features"](doc_id=doc, ops=[{
            "op": "add_feature",
            "feature": {"type": "box", "name": "Slender stock",
                        "params": {"length": 1000, "width": 4, "height": 4}}}]))
        assert built["ok"], built
        bounds = anim._world_bodies(doc)[0]["bbox"]
        for elevation in (0, 89):
            scales = []
            for azimuth in (0, 30, 90, 180, 270):
                direction = anim._dir_from_angles(azimuth, elevation)
                focus, margin = anim._orbit_framing(bounds, direction, 640, 360)
                _, _, response = anim._render_view_png(
                    doc, direction, str(tmp_path / f"orbit_{elevation}_{azimuth}.png"),
                    W=640, H=360, focus=focus, margin=margin, clip_near=False,
                    annotate=False, background=list(anim._BG))
                frame = response["frame"]
                scales.append(frame["scale"])
                for mask in range(8):
                    point = [bounds["max" if mask & (1 << a) else "min"][a]
                             - frame["center"][a] for a in range(3)]
                    u = sum(point[a] * frame["right"][a] for a in range(3))
                    v = sum(point[a] * frame["up"][a] for a in range(3))
                    x = (u - frame["cu"]) * frame["scale"] + frame["width"] / 2
                    y = frame["height"] / 2 - (v - frame["cv"]) * frame["scale"]
                    assert 0 < x < frame["width"] and 0 < y < frame["height"]
            assert max(scales) == pytest.approx(min(scales), rel=1e-6)
    finally:
        cb.call("doc_close", {"doc_id": doc})


@needs_engine
@needs_ffmpeg
def test_mated_film_renders_solved_poses_without_resolving_them(tools, tmp_path, monkeypatch):
    import math

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    part = json.loads(tools["new_part"](name="pose_stock"))["doc_id"]
    built = json.loads(tools["add_features"](doc_id=part, ops=[{
        "op": "add_feature", "feature": {"type": "box", "params": {
            "length": 10, "width": 10, "height": 10, "centered": False}}}]))
    assert built["ok"], built
    stock = tmp_path / "pose_stock.step+"
    assert json.loads(tools["save_part"](path=str(stock), doc_id=part))["ok"]
    doc = cb.call("doc_new", {"kind": "assembly", "name": "solved_pose_film"})["doc_id"]
    observed = []
    render = anim._render_view_png

    def measure_rendered_pose(render_doc, *args, **kwargs):
        observed.append(next(body["bbox"] for body in anim._world_bodies(render_doc)
                             if body["component"] == "k2"))
        return render(render_doc, *args, **kwargs)

    monkeypatch.setattr(anim, "_render_view_png", measure_rendered_pose)
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "asm_insert_component", "source": {"path": str(stock)}, "fixed": True},
            {"op": "asm_insert_component", "source": {"path": str(stock)}, "fixed": False}]})
        selector = {"kind": "face", "centroid": [10, 5, 5]}
        added = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [{
            "op": "asm_add_mate", "mate": {
                "type": "angle", "align": "aligned",
                "refs": [{"component": cid, "selector": selector} for cid in ("k1", "k2")],
                "value": {"value": 0, "axis": [0, 1, 0], "axis_point": [50, 0, 20]}}}]})
        mate = added["id_map"]["$0"]
        assert json.loads(tools["save_part"](path=str(tmp_path / "pose_assembly.step+"),
                                              doc_id=doc))["ok"]
        before = cb.call("doc_tree", {"doc_id": doc})["tree"]
        result = tools["render_animation"](
            kind="motion", doc_id=doc, mate_id=mate, start=-30, stop=-150,
            steps=3, frames=3, fps=3, size=[640, 480],
            save_path=str(tmp_path / "solved_poses.gif"))
        assert len(observed) == 3, result
        for bounds, angle in zip(observed, (-30, -90, -150)):
            c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            points = [(50 + c * (x - 50) + s * (z - 20), y,
                       20 - s * (x - 50) + c * (z - 20))
                      for x in (0, 10) for y in (0, 10) for z in (0, 10)]
            assert bounds["min"] == pytest.approx(
                [min(point[a] for point in points) for a in range(3)], abs=1e-4)
            assert bounds["max"] == pytest.approx(
                [max(point[a] for point in points) for a in range(3)], abs=1e-4)
        # Filming never re-poses the assembly; its sweep is recorded as the Motion study.
        after = cb.call("doc_tree", {"doc_id": doc})["tree"]
        assert [s["type"] for s in after.pop("studies")] == ["motion"]
        before.pop("studies", None)
        assert after == before
    finally:
        cb.call("doc_close", {"doc_id": doc})
        cb.call("doc_close", {"doc_id": part})


@needs_engine
@needs_ffmpeg
def test_explosion_keeps_originally_fixed_components_stationary(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    part = json.loads(tools["new_part"](name="explode_stock"))["doc_id"]
    built = json.loads(tools["add_features"](doc_id=part, ops=[{
        "op": "add_feature", "feature": {"type": "box", "params": {
            "length": 10, "width": 10, "height": 10, "centered": False}}}]))
    assert built["ok"], built
    stock = tmp_path / "explode_stock.step+"
    assert json.loads(tools["save_part"](path=str(stock), doc_id=part))["ok"]
    doc = cb.call("doc_new", {"kind": "assembly", "name": "fixed_explosion"})["doc_id"]
    observed = []
    render = anim._render_view_png

    def measure_rendered_pose(render_doc, *args, **kwargs):
        observed.append({body["component"]: body["bbox"]
                         for body in anim._world_bodies(render_doc)})
        return render(render_doc, *args, **kwargs)

    monkeypatch.setattr(anim, "_render_view_png", measure_rendered_pose)
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "asm_insert_component", "source": {"path": str(stock)},
             "fixed": fixed, "transform": anim._translation((x, 0, 0))}
            for x, fixed in ((0, True), (20, True), (80, False))]})
        assert json.loads(tools["save_part"](path=str(tmp_path / "explosion.step+"),
                                              doc_id=doc))["ok"]
        before = cb.call("doc_tree", {"doc_id": doc})
        result = tools["render_animation"](
            kind="explode", doc_id=doc, factor=1, frames=3, fps=3, size=[640, 480],
            save_path=str(tmp_path / "fixed_explosion.gif"))
        assert len(observed) == 3, result
        for frame, moving_x in zip(observed, (80, 100, 120)):
            for component, x in (("k1", 0), ("k2", 20), ("k3", moving_x)):
                assert frame[component]["min"] == pytest.approx([x, 0, 0], abs=1e-4)
                assert frame[component]["max"] == pytest.approx([x + 10, 10, 10], abs=1e-4)
        assert cb.call("doc_tree", {"doc_id": doc}) == before
    finally:
        cb.call("doc_close", {"doc_id": doc})
        cb.call("doc_close", {"doc_id": part})


@pytest.mark.parametrize("size", [(1920, 1080), (1000, 700)])
def test_typography_stays_bounded_and_footer_fields_do_not_overlap(tmp_path, size):
    from PIL import Image, ImageChops
    w, h = size
    card = tmp_path / "card.png"
    anim._caption_card(w, h, [
        "Sample measured folding mechanism presentation",
        "A long result path is kept readable rather than clipped: " + "/measured/geometry" * 8,
        "Samples describe measured poses, not swept-clearance approval."], str(card))
    with Image.open(card) as image:
        bbox = ImageChops.difference(image, Image.new("RGB", size, anim._BG)).getbbox()
        assert bbox[0] >= w * 0.05 and bbox[2] <= w * 0.95
        assert bbox[1] > 0 and bbox[3] < h
        assert bbox[3] - bbox[1] > h * 0.12

    png = tmp_path / "footer.png"
    scene_h = h - round(h * 0.22)
    Image.new("RGB", (w, scene_h), (50, 90, 120)).save(png)
    anim._stamp_footer(str(png),
                       "chudware 0123456789abcdef | polymesh abcdef012345 | "
                       "proto_folding_mechanism | sample 300/300",
                       "Measured: " + "long_filename/" * 12, height=h - scene_h)
    with Image.open(png) as image:
        assert image.size == size
        assert image.crop((0, 0, w, scene_h)).getextrema() == ((50, 50), (90, 90), (120, 120))
        colours = {}
        for colour in ((196, 200, 210), (150, 154, 164)):
            masks = [channel.point([255 if value == target else 0 for value in range(256)])
                     for channel, target in zip(image.split(), colour)]
            colours[colour] = ImageChops.multiply(
                ImageChops.multiply(masks[0], masks[1]), masks[2]).getbbox()
        left, right = colours.values()
        assert left[3] < right[1]
        assert min(left[0], right[0]) > 0 and max(left[2], right[2]) < w - 1
        assert right[3] < h - 1


@pytest.mark.skipif(shutil.which("fc-match") is None, reason="fontconfig unavailable")
def test_fontconfig_fallback_preserves_requested_glyph_size(monkeypatch):
    exists = os.path.exists
    monkeypatch.setattr(anim.os.path, "exists",
                        lambda path: False if "DejaVu" in str(path) else exists(path))
    anim._font.cache_clear()
    try:
        small, large = anim._font(True, 24), anim._font(True, 48)
        assert large.getlength("Measured geometry") > 1.9 * small.getlength("Measured geometry")
        assert large.getbbox("AB 490")[3] - large.getbbox("AB 490")[1] >= 30
    finally:
        anim._font.cache_clear()


def test_pose_framing_transports_world_bounds_and_keeps_partial_pose_state():
    base = {"arm": anim._translation((100, 0, 0)), "fixed": anim._translation((0, 0, 0))}
    bodies = [
        {"component": "arm", "bbox": {"min": [100, 0, 0], "max": [110, 2, 1]}},
        {"component": "fixed", "bbox": {"min": [-20, -3, -1], "max": [-10, 3, 1]}}]
    rotated = [0, -1, 0, 100, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    bb = anim._pose_bounds(bodies, base, [{"arm": rotated},
                                        {"fixed": anim._translation((-10, 0, 0))}])
    assert bb["min"] == pytest.approx([-30, -3, -1])
    assert bb["max"] == pytest.approx([110, 10, 1])
    assert base["arm"][3] == 100


@needs_engine
@needs_ffmpeg
@pytest.mark.parametrize("pattern", ["circular", "linear"])
@pytest.mark.parametrize("kind", ["motion", "explode"])
def test_pattern_film_bounds_contain_native_seed_motion(tools, tmp_path, monkeypatch,
                                                        pattern, kind):
    """Pattern WORLD placement is offset * seed_pose, not seed_delta * world_box."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    name = f"anim_pattern_{pattern}_{kind}"
    part = json.loads(tools["new_part"](name=name + "_cube"))["doc_id"]
    added = json.loads(tools["add_features"](doc_id=part, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Cube",
         "params": {"length": 10, "width": 10, "height": 10, "centered": True}}}]))
    assert added["ok"], added
    part_path = str(tmp_path / "cube.step+")
    assert json.loads(tools["save_part"](doc_id=part, path=part_path))["ok"]
    assembly = json.loads(tools["new_assembly"](name=name))["doc_id"]
    seed_row = json.loads(tools["asm_add_component"](
        doc_id=assembly, path=part_path, name="seed", at=[100, 0, 0]))
    fixed_row = json.loads(tools["asm_add_component"](
        doc_id=assembly, path=part_path, name="fixed", fixed=True, at=[-100, 0, 0]))
    assert seed_row["ok"] and fixed_row["ok"], (seed_row, fixed_row)
    seed = seed_row["component"]
    params = ({"count": 2, "angle": 90, "equal_spacing": False} if pattern == "circular"
              else {"count1": 2, "spacing1": 200})
    refs = ({"components": [seed], "axis": "axis.z"} if pattern == "circular" else
            {"components": [seed], "dir1": "axis.y"})
    added = json.loads(tools["add_features"](doc_id=assembly, ops=[
        {"op": "add_feature", "feature": {"type": f"asm_{pattern}_pattern",
         "name": "Copies", "params": params, "refs": refs}}]))
    assert added["ok"], added
    assert json.loads(tools["save_part"](
        doc_id=assembly, path=str(tmp_path / "assembly.step+")))["ok"]
    before = cb.call("doc_tree", {"doc_id": assembly})
    bodies = anim._world_bodies(assembly)
    instance = next(b for b in bodies if b["component"] == seed + "#1")
    assert anim._bbox_centre(instance["bbox"]) == pytest.approx(
        [0, 100, 0] if pattern == "circular" else [100, 200, 0], abs=1e-5)
    base = {c["id"]: c["transform"] for c in before["tree"]["components"]}
    # Circular: a +200 X seed translation moves its +90 Z copy from Y=100 to Y=300.
    # Linear: a seed rotation must NOT rotate the pattern's world-space translation.
    final = (anim._translation((300, 0, 0)) if pattern == "circular" else
             [-1, 0, 0, 100, 0, -1, 0, 200, 0, 0, 1, 0, 0, 0, 0, 1])
    pose_rows = [{seed: base[seed]}, {seed: final}, {fixed_row["component"]:
                                                 base[fixed_row["component"]]}]
    kwargs = {"poses": pose_rows} if kind == "motion" else {"factor": 2}
    result = tools["render_animation"](
        kind=kind, doc_id=assembly, save_path=str(tmp_path / "pattern.gif"),
        frames=3, fps=3, size=[320, 240], **kwargs)
    assert isinstance(result, list) and len(result) == 3, result
    measured = json.loads(result[2].split("measured: ", 1)[1].split("  render wall time")[0])
    bounds = measured["framing"]["bounds_mm"]
    if kind == "explode":
        pose_rows = [{}] + [
            {cid: anim._mat_mul(anim._translation([v * t for v in offset]), base[cid])
             for cid, offset in measured["offsets_mm"].items()} for t in (0.5, 1.0)]

    # Measure the engine's actual poses independently of the framing calculation.
    probe = anim._clone_detached(assembly, kind)
    try:
        for row in pose_rows:
            anim._apply_poses(probe, row)
            posed = anim._world_bodies(probe)
            for body in posed:
                for axis in range(3):
                    assert bounds["min"][axis] <= body["bbox"]["min"][axis] + 1e-5
                    assert bounds["max"][axis] >= body["bbox"]["max"][axis] - 1e-5
        if kind == "motion":
            copy = next(b for b in posed if b["component"] == seed + "#1")
            assert anim._bbox_centre(copy["bbox"]) == pytest.approx(
                [0, 300, 0] if pattern == "circular" else [100, 400, 0], abs=1e-5)
    finally:
        cb.call("doc_close", {"doc_id": probe})
    after = cb.call("doc_tree", {"doc_id": assembly})
    assert after["revision"] == before["revision"]
    assert after["tree"]["components"] == before["tree"]["components"]


def test_pattern_motion_refuses_unmeasured_instance_transport():
    with pytest.raises(ValueError, match="measured posed world bounds"):
        anim._pose_bounds(
            [{"component": "seed#1", "bbox": {"min": [-5, 95, -5], "max": [5, 105, 5]}}],
            {"seed": anim._translation((100, 0, 0))},
            [{"seed": anim._translation((300, 0, 0))}])


def test_collision_legend_does_not_extend_a_first_hit_into_unmeasured_states():
    measured = {"interference": {"first_collision_step": 1,
                                "detail": [{"a": "arm", "b": "frame"}]}}
    assert anim._collision_legend(measured, 0) == ("", False)
    assert anim._collision_legend(measured, 1)[1] is True
    _, current = anim._collision_legend(measured, 2)
    assert current is False
    measured["sample_collisions"] = [[], [{"a": "arm", "b": "frame"}], []]
    assert anim._collision_legend(measured, 2) == ("", False)


def _two_tet_grid(pv):
    import numpy as np
    pts = np.array([[0.0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10], [10, 10, 10]])
    cells = np.hstack([[4, 0, 1, 2, 3], [4, 1, 2, 3, 4]])
    return pv.UnstructuredGrid(cells, np.full(2, pv.CellType.TETRA), pts)


def test_fea_cell_average_is_volume_weighted_over_retained_cells():
    """Tet A (0,1,2,3) is 1000/6 mm3 and tet B (1,2,3,4) is 2000/6 mm3, so a shared node
    carries (1*10 + 2*30) / 3; a cell with retained == 0 does not count."""
    pv = pytest.importorskip("pyvista")
    grid = _two_tet_grid(pv)
    avg = anim._cell_to_point_average(grid, [10.0, 30.0])
    assert avg[0] == pytest.approx(10.0) and avg[4] == pytest.approx(30.0)
    for shared in (1, 2, 3):
        assert avg[shared] == pytest.approx(70.0 / 3.0)
    grid.cell_data["retained"] = [1, 0]
    masked = anim._cell_to_point_average(grid, [10.0, 30.0])
    assert list(masked) == pytest.approx([10.0, 10.0, 10.0, 10.0, 0.0])


def test_fea_frames_film_a_cell_only_von_mises_warped_by_the_same_case(tmp_path):
    """A topology reanalysis.vtu from before point stress: per-case von Mises only as cell
    data. The film must still be built (cell-averaged, said so) and must warp LC2's stress
    by LC2's displacement, not by the first displacement array in the file."""
    pv = pytest.importorskip("pyvista")
    import numpy as np
    grid = _two_tet_grid(pv)
    grid.point_data["displacement@LC1"] = np.tile([0.0, 0.0, 0.1], (5, 1))
    grid.point_data["displacement@LC2"] = np.tile([0.0, 0.5, 0.0], (5, 1))
    grid.cell_data["retained"] = np.array([1, 1], dtype=np.uint8)
    grid.cell_data["von_mises@LC1"] = [2.0, 6.0]
    grid.cell_data["von_mises@LC2"] = [10.0, 30.0]
    vtu = tmp_path / "reanalysis.vtu"
    grid.save(str(vtu))
    out_dir = tmp_path / "frames"
    out_dir.mkdir()
    rec = anim._fea_frames(str(vtu), str(out_dir), 2, (160, 120), None, None,
                           "von_mises@LC2")
    assert rec["field"] == "von_mises@LC2"
    assert rec["field_source"] == "cell_averaged"
    assert "cell data" in rec["field_note"]
    assert rec["displacement_field"] == "displacement@LC2"
    assert rec["peak_disp_mm"] == pytest.approx(0.5)
    assert rec["peak_field"] == pytest.approx(30.0)
    assert len(rec["frames"]) == 2 and all(os.path.exists(p) for p in rec["frames"])


def test_fea_field_prefers_point_data_and_refusal_names_the_fields():
    pv = pytest.importorskip("pyvista")
    import numpy as np
    grid = _two_tet_grid(pv)
    for case in ("LC1", "LC2"):
        grid.point_data[f"von_mises@{case}"] = np.arange(5, dtype=float)
        grid.point_data[f"displacement@{case}"] = np.zeros((5, 3))
    assert anim._fea_field(grid, "von_mises@LC2") == \
        ("von_mises@LC2", "displacement@LC2", "point")
    with pytest.raises(ValueError, match=r"point fields: .*Set field to one of"):
        anim._fea_field(grid, "von_mises")
    bare = _two_tet_grid(pv)
    bare.point_data["displacement"] = np.zeros((5, 3))
    with pytest.raises(ValueError, match="run_fea_static"):
        anim._fea_field(bare, "von_mises")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:randomly"]))

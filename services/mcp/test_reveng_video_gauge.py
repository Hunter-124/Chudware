"""Video, shared-gauge and metric reverse-engineering orchestration regressions."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from services.mcp import reveng_build_tools as build
from services.mcp import reveng_common as common
from services.mcp import reveng_session_tools as session_tools
from services.mcp.reveng_tools import register_reveng_tools
from services.vision.recon_transform import ReconTransform


class Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHUDWARE_REVENG_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(common, "make_reveng_stage_emitter", lambda *args: lambda *a, **k: None)
    mcp = Collector()
    register_reveng_tools(mcp)
    sid = "re_video_test"
    common._save_session(common._make_session(sid, "video test", None))
    return sid, common._session_path(sid), mcp.tools


def call(tools, name, **kwargs):
    return json.loads(tools[name](**kwargs))


def test_known_dimension_is_physical_length_divided_by_reconstructed_length(env):
    sid, _, tools = env
    out = call(tools, "reveng_set_scale", session_id=sid, method="known_dim",
               params={"value_mm": 50, "recon_length": 25, "source": "caliper"})
    assert out["ok"] and out["scale"]["mm_per_unit"] == 2
    assert out["session"]["recon_transform"]["mm_per_unit"] == 2
    assert out["session"]["recon_transform"]["confirmed"] is False
    points = call(tools, "reveng_set_scale", session_id=sid, method="known_dim",
                  params={"value_mm": 50, "point_a": [1, 2, 3],
                          "point_b": [4, 6, 3], "source": "caliper"})
    assert points["scale"]["mm_per_unit"] == 10
    for bad in ({"value_mm": 50}, {"value_mm": 50, "recon_length": 0},
                {"value_mm": float("inf"), "recon_length": 1}):
        assert not call(tools, "reveng_set_scale", session_id=sid,
                        method="known_dim", params=bad)["ok"]


def test_import_scan_passes_transform_once_to_fitter(env, tmp_path, monkeypatch):
    sid, _, tools = env
    scan = tmp_path / "scan.ply"
    scan.write_text("ply\n", encoding="utf-8")
    result = call(tools, "import_scan_mesh", session_id=sid, path=str(scan), scale_mm=5)
    assert result["ok"]
    assert result["session"]["artifacts"]["mesh_units"] == "reconstruction"
    assert result["session"]["recon_transform"]["confirmed"] is True
    captured = {}

    def fit(mesh_path, **kwargs):
        captured.update(mesh_path=mesh_path, **kwargs)
        return {"ok": True, "fits": []}

    monkeypatch.setattr("services.vision.segment_fit.segment_fit", fit)
    fitted = call(tools, "reveng_fit_primitives", session_id=sid)
    assert fitted["ok"]
    assert captured["transform_applied"] is False
    transform = ReconTransform.from_json(captured["recon_transform"])
    assert np.allclose(transform.apply(np.array([[2., 0., 0.]])), [[10., 0., 0.]])
    assert not call(tools, "import_scan_mesh", session_id=sid, path=str(scan), scale_mm=0)["ok"]


def test_reconstruct_forwards_quality_and_backend(env, monkeypatch):
    sid, _, tools = env
    observed = {}
    monkeypatch.setattr(build, "_reveng_status", lambda: {"capabilities": {"sparse": True}})
    def launch(*args, **kwargs):
        observed.update(kwargs)
        return {"run_id": "run_1", "status": "running"}
    monkeypatch.setattr(build, "_launch_reveng_mission", launch)
    result = call(tools, "reveng_reconstruct", session_id=sid, quality="high", backend="brush")
    assert result["ok"] and observed["quality"] == "high" and observed["backend"] == "brush"
    assert observed["background"] is True


def test_sparse_submits_union_and_reuses_one_dataset(env, monkeypatch):
    sid, root, _ = env
    for folder, count in (("images", 2), ("frames", 2)):
        directory = root / folder
        directory.mkdir(parents=True)
        for i in range(count):
            (directory / f"same_{i}.jpg").write_bytes(f"{folder}-{i}".encode())
    seen = {}
    def sparse(image_dir, out_dir, **kwargs):
        seen["files"] = sorted(p.name for p in Path(image_dir).iterdir())
        return {"ok": True, "registered": 3, "sparse_dir": str(Path(out_dir) / "sparse")}
    def prepare(sparse_dir, image_dir, dataset_dir, *, source_image_map):
        d = Path(dataset_dir)
        (d / "sparse").mkdir(parents=True)
        (d / "images").mkdir()
        seen["mapping"] = source_image_map
        return {"ok": True, "dataset_dir": str(d), "sparse_dir": str(d / "sparse"),
                "images_dir": str(d / "images"), "image_name_map": source_image_map,
                "registered": 3}
    monkeypatch.setattr("services.vision.colmap_pipeline.run_sparse", sparse)
    monkeypatch.setattr("services.vision.colmap_pipeline.prepare_dataset", prepare)
    monkeypatch.setattr(common, "_autosolve_scale", lambda sid: {"attempted": False})
    fn = common._make_pipeline_agent_fn(sid)
    result = fn("", {"node_id": "n_sparse"})
    assert result.success and result.result["submitted"] == 4
    assert result.result["registration_ratio"] == 0.75
    assert len(seen["files"]) == len(set(seen["files"])) == 4
    assert {Path(p).parent.name for p in seen["mapping"].values()} == {"images", "frames"}
    dataset = common._load_session(sid, merge_gaussian=False)["sfm_dataset"]
    assert common._dataset_matches(dataset, common._session_image_paths(sid))
    monkeypatch.setattr("services.vision.colmap_pipeline.run_sparse",
                        lambda *a, **k: pytest.fail("must not solve a second gauge"))
    assert fn("", {"node_id": "n_sparse"}).result["reused"] is True


def test_add_video_returns_while_extraction_is_running(env, tmp_path, monkeypatch):
    sid, root, tools = env
    video = tmp_path / "orbit.mp4"
    video.write_bytes(b"video")
    entered, release = threading.Event(), threading.Event()
    def extract(path, out_dir, **kwargs):
        entered.set()
        assert release.wait(5)
        frame = Path(out_dir) / "frame.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"frame")
        return {"ok": True, "frames": [{"path": str(frame), "sharpness": 100}]}
    monkeypatch.setattr(session_tools, "_extract_frames_safe", extract)
    result = call(tools, "reveng_add_video", session_id=sid, path=str(video))
    assert result["ok"] and result["run_id"].startswith("video_") and result["async"]
    assert entered.wait(5)
    try:
        assert common._load_session(sid, merge_gaussian=False)["run"]["status"] == "running"
        thread = common._MISSION_THREADS[common._rc.scoped_key(sid)][1]
    finally:
        release.set()
    thread.join(5)
    assert not thread.is_alive()
    saved = common._load_session(sid, merge_gaussian=False)
    assert saved["run"]["status"] == "done"
    assert saved["artifacts"]["images"] == 1
    assert saved["source_images"][0]["kind"] == "video"


def test_dense_fallback_uses_same_dataset_and_labels_approximate(env, monkeypatch):
    sid, root, _ = env
    img = root / "images" / "capture.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"image")
    dataset = root / "dataset"
    (dataset / "images").mkdir(parents=True)
    (dataset / "sparse").mkdir()
    s = common._load_session(sid, merge_gaussian=False)
    s["sfm_dataset"] = {"dataset_dir": str(dataset), "images_dir": str(dataset / "images"),
                        "sparse_dir": str(dataset / "sparse"),
                        "source_signature": common._source_signature([str(img.resolve())])}
    transform = ReconTransform.identity()
    transform.mm_per_unit = 3
    transform.confirmed = True
    s["recon_transform"] = transform.to_json()
    common._save_session(s)
    observed = {}
    def train(dataset_dir, out_dir, **kwargs):
        observed.update(dataset_dir=dataset_dir, **kwargs)
        path = root / "splat.ply"
        path.write_bytes(b"splat")
        return {"ok": True, "splat_path": str(path)}
    def mesh(cloud, out_path, **kwargs):
        Path(out_path).write_bytes(b"mesh")
        return {"ok": True, "mesh_path": str(out_path), "approximate": True}
    def cleanup(mesh_path, out_path, **kwargs):
        observed["transform"] = kwargs["recon_transform"]
        Path(out_path).write_bytes(b"metric")
        return {"ok": True, "mesh_path": out_path, "scale_applied": True}
    monkeypatch.delenv("OPENMVS_BIN", raising=False)
    monkeypatch.setattr("services.vision.dense_backends.run_openmvs",
                        lambda *args, **kwargs: {"ok": False, "reason": "not_installed"})
    monkeypatch.setattr("services.vision.gaussian_pipeline.train_splat_from_dataset", train)
    monkeypatch.setattr("services.formats.splat_io.load_splat", lambda path: object())
    monkeypatch.setattr("services.formats.splat_io.splat_to_mesh", mesh)
    monkeypatch.setattr("services.vision.mesh_cleanup.cleanup_mesh", cleanup)
    monkeypatch.setattr(common, "_materialize_metric_splat", lambda s: None)
    fn = common._make_pipeline_agent_fn(sid, quality="high")
    result = fn("", {"node_id": "n_dense"})
    assert result.success and result.result["approximate"] is True
    assert observed["dataset_dir"] == str(dataset) and observed["iterations"] == 7000
    assert observed["transform"].mm_per_unit == 3
    saved = common._load_session(sid, merge_gaussian=False)
    assert saved["artifacts"]["mesh_units"] == "mm"
    assert saved["artifacts"]["mesh_transform_applied"] is True
    assert saved["honesty"]["approximate"] is True


def test_splat_tools_registered_on_main_mcp():
    from services.mcp.server import mcp
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"reveng_import_splat", "reveng_convert_splat", "reveng_splat_to_mesh",
            "reveng_sparse_preview", "reveng_confirm_scale", "reveng_align_axes"} <= names


def test_confirm_scale_requires_explicit_approval_and_respects_mission_gate(env, monkeypatch):
    sid, _, tools = env
    assert call(tools, "reveng_confirm_scale", session_id=sid)["reason"] == "scale_unsolved"
    solved = call(tools, "reveng_set_scale", session_id=sid, method="known_dim",
                  params={"value_mm": 12, "recon_length": 3, "source": "caliper"})
    assert solved["ok"] and not solved["scale"]["confirmed"]
    monkeypatch.setattr(common, "_find_session_run", lambda sid: {"run_id": "run_1", "status": "paused"})
    assert call(tools, "reveng_confirm_scale", session_id=sid)["reason"] == "mission_gate_active"
    monkeypatch.setattr(common, "_find_session_run", lambda sid: None)
    confirmed = call(tools, "reveng_confirm_scale", session_id=sid)
    assert confirmed["ok"] and confirmed["scale"]["confirmed"]
    assert confirmed["session"]["recon_transform"]["confirmed"]


def test_two_point_rejects_raw_source_pixels_for_shared_dataset(env):
    sid, root, tools = env
    sparse = root / "dataset" / "sparse"
    sparse.mkdir(parents=True)
    s = common._load_session(sid, merge_gaussian=False)
    s["artifacts"]["sparse_dir"] = str(sparse)
    s["sfm_dataset"] = {"images_dir": str(root / "dataset" / "images")}
    common._save_session(s)
    result = call(tools, "reveng_set_scale", session_id=sid, method="two_point",
                  params={"image": "raw.jpg", "p1": [0, 0], "p2": [1, 1], "distance_mm": 10})
    assert result["reason"] == "two_point_requires_undistorted_image"


def test_sparse_preview_transforms_points_and_camera_frusta(env):
    sid, root, tools = env
    sparse = root / "sparse"
    sparse.mkdir()
    (sparse / "cameras.txt").write_text(
        "1 PINHOLE 100 80 50 50 50 40\n2 FOV 100 80 50 50 50 40 1\n")
    (sparse / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 frame.jpg\n\n2 1 0 0 0 0 0 0 2 other.jpg\n\n")
    (sparse / "points3D.txt").write_text("1 1 2 3 255 128 0 0.1\n")
    s = common._load_session(sid, merge_gaussian=False)
    s["artifacts"]["sparse_dir"] = str(sparse)
    transform = ReconTransform.identity()
    transform.mm_per_unit = 2
    transform.translation_mm = [10, 0, 0]
    transform.source = "known_dim"
    transform.confirmed = True
    s["recon_transform"] = transform.to_json()
    common._save_session(s)
    result = call(tools, "reveng_sparse_preview", session_id=sid)
    assert result["ok"] and result["units"] == "mm"
    assert result["points"] == [[12.0, 4.0, 6.0, 255, 128, 0]]
    assert len(result["frusta"][0]) == 15
    assert result["frusta"][0][:3] == [10.0, 0.0, 0.0]
    assert len(result["frusta"]) == 1
    assert any("unsupported COLMAP camera model: FOV" in warning for warning in result["warnings"])


def test_standalone_gaussian_publishes_dataset_and_invalidates_old_gauge(env):
    from services.reveng.gaussian_jobs import _persist_shared_output
    sid, root, _ = env
    image = root / "frames" / "shot.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    dataset = root / "training" / "dataset"
    (dataset / "images").mkdir(parents=True)
    (dataset / "sparse").mkdir()
    batch = root / "training" / "batch_image_map.json"
    batch.write_text(json.dumps({"hashed.jpg": "frames/shot.jpg"}))
    s = common._load_session(sid, merge_gaussian=False)
    t = ReconTransform.identity()
    t.confirmed = True
    s["recon_transform"] = t.to_json()
    s["scale"].update(mm_per_unit=1, confirmed=True)
    common._save_session(s)
    payload = {"dataset_dir": str(dataset), "sparse_dir": str(dataset / "sparse"),
               "images_dir": str(dataset / "images"), "batch_image_map_path": str(batch),
               "image_name_map": {"hashed.jpg": "frames/shot.jpg"}, "registered": 1}
    _persist_shared_output(root, {"session_id": sid}, payload, None)
    result = common._load_session(sid, merge_gaussian=False)
    assert "recon_transform" not in result and not result["scale"]["confirmed"]
    assert result["sfm_dataset"]["image_name_map"]["hashed.jpg"] == str(image.resolve())
    assert common._dataset_matches(result["sfm_dataset"], [str(image.resolve())])


def test_metric_splat_is_regenerated_from_immutable_native_gauge(env):
    from services.formats.splat_io import GaussianCloud, load_splat, save_splat
    sid, root, _ = env
    raw = root / "source.ply"
    cloud = GaussianCloud(
        means=np.array([[1., 2., 3.]]), scales=np.array([[1., 2., 3.]]),
        rotations=np.array([[1., 0., 0., 0.]]), opacities=np.array([0.7]),
        sh_dc=np.zeros((1, 3)), sh_rest=None, source_format="ply", lossy=[])
    save_splat(cloud, raw, "ply")
    s = common._load_session(sid, merge_gaussian=False)
    s["artifacts"]["raw_splat_path"] = str(raw)
    t = ReconTransform.identity()
    t.mm_per_unit = 2
    t.translation_mm = [10, 0, 0]
    s["recon_transform"] = t.to_json()
    common._materialize_metric_splat(s)
    first = s["artifacts"]["splat_path"]
    assert np.allclose(load_splat(first).means, [[12, 4, 6]])
    assert np.allclose(load_splat(first).scales, [[2, 4, 6]])
    t.mm_per_unit = 3
    s["recon_transform"] = t.to_json()
    common._materialize_metric_splat(s)
    assert s["artifacts"]["splat_path"] != first
    assert np.allclose(load_splat(s["artifacts"]["splat_path"]).means, [[13, 6, 9]])
    assert s["artifacts"]["raw_splat_path"] == str(raw)


def test_gui_bridge_exposes_splat_tools_and_accepts_video_path_alias(monkeypatch):
    from services.reveng import worker_rpc
    names = set(worker_rpc._BRIDGED_TOOLS)
    assert {"reveng_add_video", "reveng_import_splat", "reveng_convert_splat",
            "reveng_splat_to_mesh", "reveng_sparse_preview", "reveng_confirm_scale"} <= names
    def fake(session_id, path, max_frames=120, wait=False):
        return json.dumps({"ok": True, "session_id": session_id, "path": path})
    monkeypatch.setattr(worker_rpc, "_tool_fn", lambda method: fake)
    result = worker_rpc._call_tool("reveng_add_video", {"session_id": "re_x", "video_path": "orbit.mov"})
    assert result["ok"] and result["path"] == "orbit.mov"

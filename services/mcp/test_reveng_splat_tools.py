"""Session, conversion and honest approximate-surface tool contracts."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from services.formats.splat_io import GaussianCloud, load_splat, save_splat
from services.mcp import reveng_common, reveng_splat_tools


class _Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "sessions"))
    registry = _Collector()
    reveng_splat_tools.register_reveng_splat_tools(registry)
    return registry.tools


def _call(tools, name, **kwargs):
    return json.loads(tools[name](**kwargs))


def _source(path: Path):
    cloud = GaussianCloud(np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32),
                          np.ones((2, 3), dtype=np.float32),
                          np.tile([1, 0, 0, 0], (2, 1)).astype(np.float32),
                          np.array([0.9, 0.8], dtype=np.float32),
                          np.zeros((2, 3), dtype=np.float32),
                          np.full((2, 3, 3), 0.1, dtype=np.float32), "test", [])
    save_splat(cloud, path, "ply")
    return cloud


def test_import_creates_session_and_retains_immutable_source(tools, tmp_path):
    source = tmp_path / "input.ply"
    cloud = _source(source)
    before = source.read_bytes()
    result = _call(tools, "reveng_import_splat", path=str(source))
    assert result["ok"] and result["source_format"] == "ply" and result["count"] == 2
    assert result["artifact"] == {"path": result["gaussian_ply"],
                                  "size_bytes": Path(result["gaussian_ply"]).stat().st_size,
                                  "kind": "gaussian_ply"}
    sid = result["session_id"]
    manifest = reveng_common._load_session(sid, merge_gaussian=False)
    assert manifest["artifacts"]["splat_path"] == result["gaussian_ply"]
    assert manifest["artifacts"]["raw_splat_path"] == result["gaussian_ply"]
    assert manifest["artifacts"]["splat_units"] == "reconstruction"
    assert not manifest["artifacts"]["splat_transform_applied"]
    assert manifest["stages"]["capture"] == "done"
    assert manifest["stages"]["reconstruct"] == "idle"
    assert Path(manifest["imported_splat"]["source_path"]).read_bytes() == before
    source.unlink()
    roundtrip = load_splat(result["gaussian_ply"])
    np.testing.assert_allclose(roundtrip.sh_rest, cloud.sh_rest, atol=1e-6)


def test_import_existing_session_and_reject_unrelated_confirmed_scale(tools, tmp_path):
    source = tmp_path / "source.ply"
    _source(source)
    sid = reveng_common._new_session_id()
    session = reveng_common._make_session(sid, "My part", None)
    reveng_common._save_session(session)
    result = _call(tools, "reveng_import_splat", path=str(source), session_id=sid)
    assert result["ok"] and result["session_id"] == sid
    session = reveng_common._load_session(sid, merge_gaussian=False)
    assert session["name"] == "My part"
    session["scale"]["confirmed"] = True
    reveng_common._save_session(session)
    rejected = _call(tools, "reveng_import_splat", path=str(source), session_id=sid)
    assert not rejected["ok"] and rejected["reason"] == "confirmed_scale_has_unrelated_gauge"
    assert reveng_common._load_session(sid, merge_gaussian=False)["artifacts"]["splat_path"] == result["gaussian_ply"]


def test_reimport_resets_unconfirmed_scale_and_rejects_existing_sfm(tools, tmp_path):
    source = tmp_path / "source.ply"
    _source(source)
    initial = _call(tools, "reveng_import_splat", path=str(source))
    sid = initial["session_id"]
    session = reveng_common._load_session(sid, merge_gaussian=False)
    session["scale"].update(mm_per_unit=10.0, method="known_dim", confirmed=False)
    session["recon_transform"]["mm_per_unit"] = 10.0
    reveng_common._save_session(session)
    replaced = _call(tools, "reveng_import_splat", path=str(source), session_id=sid)
    assert replaced["ok"] and replaced["gaussian_ply"] != initial["gaussian_ply"]
    session = reveng_common._load_session(sid, merge_gaussian=False)
    assert session["scale"]["mm_per_unit"] is None
    assert session["recon_transform"]["mm_per_unit"] == 1.0
    session["artifacts"]["sparse_dir"] = "/existing/reconstruction"
    reveng_common._save_session(session)
    rejected = _call(tools, "reveng_import_splat", path=str(source), session_id=sid)
    assert not rejected["ok"] and rejected["reason"] == "session_has_existing_reconstruction"
    session = reveng_common._load_session(sid, merge_gaussian=False)
    assert session["artifacts"]["sparse_dir"] == "/existing/reconstruction"
    assert session["artifacts"]["splat_path"] == replaced["gaussian_ply"]


@pytest.mark.parametrize("fmt", ["splat", "spz", "ksplat"])
def test_import_supported_binary_formats_into_session(tools, tmp_path, fmt):
    # Shared synthetic bytes exercise the real codecs through the public tool.
    from services.formats.test_splat_io import _ksplat, _legacy_spz
    source = tmp_path / ("sample." + fmt)
    if fmt == "splat":
        ply = tmp_path / "source.ply"
        cloud = _source(ply)
        save_splat(cloud, source, "splat")
    else:
        source.write_bytes(_legacy_spz(3) if fmt == "spz" else _ksplat(1))
    result = _call(tools, "reveng_import_splat", path=str(source))
    assert result["ok"] and result["source_format"] == fmt and result["count"] == 2
    assert load_splat(result["gaussian_ply"]).source_format == "ply"
    assert result["lossy"]


def test_convert_reports_sh_loss_and_never_overwrites_source(tools, tmp_path):
    source = tmp_path / "source.ply"
    _source(source)
    before = source.read_bytes()
    result = _call(tools, "reveng_convert_splat", path=str(source), out_format="splat")
    assert result["ok"] and Path(result["path"]).suffix == ".splat"
    assert any("discards" in loss for loss in result["lossy"])
    assert load_splat(result["path"]).sh_rest is None
    blocked = _call(tools, "reveng_convert_splat", path=str(source), out_format="ply", out_path=str(source))
    assert not blocked["ok"] and blocked["reason"] == "source_equals_output"
    assert source.read_bytes() == before
    unsupported = _call(tools, "reveng_convert_splat", path=str(source), out_format="spz")
    assert not unsupported["ok"] and unsupported["reason"] == "unsupported_output_format"


def test_convert_from_session_handle(tools, tmp_path):
    source = tmp_path / "source.ply"
    _source(source)
    imported = _call(tools, "reveng_import_splat", path=str(source))
    out = tmp_path / "output.splat"
    converted = _call(tools, "reveng_convert_splat", path=imported["session_id"],
                      out_format="splat", out_path=str(out))
    assert converted["ok"] and Path(converted["path"]) == out
    assert len(load_splat(out).means) == 2


@pytest.mark.parametrize("job_kind", ["gaussian", "paused", "running"])
def test_session_mutations_refuse_active_or_paused_jobs(tools, tmp_path, monkeypatch, job_kind):
    from services.reveng import gaussian_jobs
    source = tmp_path / "source.ply"
    _source(source)
    imported = _call(tools, "reveng_import_splat", path=str(source))
    sid = imported["session_id"]
    if job_kind == "gaussian":
        monkeypatch.setattr(gaussian_jobs, "active_run", lambda root: "splat_busy")
        expected_run = "splat_busy"
    else:
        expected_run = "mission_busy"
        monkeypatch.setattr(reveng_splat_tools, "_find_session_run",
                            lambda session_id: {"status": job_kind, "run_id": expected_run})
    for name, args in (
        ("reveng_import_splat", {"path": str(source), "session_id": sid}),
        ("reveng_splat_to_mesh", {"path_or_session": sid}),
    ):
        blocked = _call(tools, name, **args)
        assert not blocked["ok"] and blocked["reason"] == "mission_already_running"
        assert blocked["run_id"] == expected_run
        assert reveng_common._mission_running(sid) is None
    session = reveng_common._load_session(sid, merge_gaussian=False)
    assert session["artifacts"]["splat_path"] == imported["gaussian_ply"]
    assert session["artifacts"]["mesh_path"] is None


def test_session_mesh_records_raw_approximate_artifact(tools, tmp_path, monkeypatch):
    source = tmp_path / "source.ply"
    _source(source)
    imported = _call(tools, "reveng_import_splat", path=str(source))
    sid = imported["session_id"]
    calls = []
    def mesh(cloud, out_path, *, method, min_opacity, remove_floor):
        calls.append((len(cloud.means), method, min_opacity))
        Path(out_path).write_text("fixture mesh")
        return {"ok": True, "mesh_path": str(out_path), "approximate": True, "triangles": 1}
    monkeypatch.setattr(reveng_splat_tools, "splat_to_mesh", mesh)
    result = _call(tools, "reveng_splat_to_mesh", path_or_session=sid, min_opacity=0.7, method="ball_pivoting")
    assert result["ok"] and result["approximate"] and calls == [(2, "ball_pivoting", 0.7)]
    session = reveng_common._load_session(sid, merge_gaussian=False)
    artifacts = session["artifacts"]
    assert artifacts["mesh_path"] == artifacts["raw_mesh_path"] == result["mesh_path"]
    assert artifacts["mesh_units"] == "reconstruction" and not artifacts["mesh_transform_applied"]
    assert session["honesty"]["approximate"] and session["stages"]["reconstruct"] == "done"
    assert session["stages"]["model"] == "idle"


def test_background_session_mesh_reports_through_session_and_holds_claim(tools, tmp_path, monkeypatch):
    import threading
    import time
    source = tmp_path / "source.ply"
    _source(source)
    sid = _call(tools, "reveng_import_splat", path=str(source))["session_id"]
    release = threading.Event()
    outcome = {"fail": False}

    def mesh(cloud, out_path, **kwargs):
        assert release.wait(10)
        if outcome["fail"]:
            return {"ok": False, "reason": "fusion_failed", "hint": "no opaque views"}
        Path(out_path).write_text("mesh")
        return {"ok": True, "approximate": True, "method": "tsdf", "triangles": 1}

    def finished():
        for _ in range(1000):
            session = reveng_common._load_session(sid, merge_gaussian=False)
            if session["mesh_job"]["status"] != "running" and reveng_common._mission_running(sid) is None:
                return session
            time.sleep(.01)
        raise AssertionError("background mesh job did not finish")

    monkeypatch.setattr(reveng_splat_tools, "splat_to_mesh", mesh)
    assert _call(tools, "reveng_splat_to_mesh", path_or_session=sid, wait=False) == {
        "ok": True, "session_id": sid, "pending": True}
    assert reveng_common._load_session(sid, merge_gaussian=False)["mesh_job"]["status"] == "running"
    # The job keeps the session claimed until it has recorded its result.
    busy = _call(tools, "reveng_splat_to_mesh", path_or_session=sid)
    assert not busy["ok"] and busy["reason"] == "mission_already_running"
    release.set()
    session = finished()
    job = session["mesh_job"]
    assert job["status"] == "done" and job["method"] == "tsdf"
    assert session["artifacts"]["mesh_path"] == job["mesh_path"] and Path(job["mesh_path"]).is_file()
    outcome["fail"] = True
    assert _call(tools, "reveng_splat_to_mesh", path_or_session=sid, wait=False)["pending"]
    job = finished()["mesh_job"]
    assert job["status"] == "failed" and "fusion_failed" in job["error"]
    refused = _call(tools, "reveng_splat_to_mesh", path_or_session=str(source), wait=False)
    assert not refused["ok"] and refused["reason"] == "wait_false_requires_session"


@pytest.mark.parametrize("association", ["matching", "supervised", "imported", "unmatched", "stale"])
def test_auto_mesher_receives_only_matching_dataset_poses(tools, tmp_path, monkeypatch, association):
    source = tmp_path / "source.ply"
    _source(source)
    imported = _call(tools, "reveng_import_splat", path=str(source))
    sid = imported["session_id"]
    root = reveng_common._session_path(sid)
    images = root / "images"
    images.mkdir(exist_ok=True)
    image = images / "frame.jpg"
    image.write_bytes(b"image")
    dataset = root / "dataset"
    sparse = dataset / "sparse"
    dataset_images = dataset / "images"
    sparse.mkdir(parents=True)
    dataset_images.mkdir()
    session = reveng_common._load_session(sid, merge_gaussian=False)
    session["sfm_dataset"] = {
        "dataset_dir": str(dataset), "sparse_dir": str(sparse), "images_dir": str(dataset_images),
        "source_signature": reveng_common._source_signature([str(image.resolve())]),
    }
    raw = session["artifacts"]["raw_splat_path"]
    if association != "imported":
        session.pop("imported_splat", None)
    if association in ("matching", "imported", "stale"):
        session["artifacts"]["splat_dataset_dir"] = str(dataset)
    if association == "supervised":
        monkeypatch.setattr("services.reveng.gaussian_jobs.status", lambda _sid: {
            "run_id": "finished", "status": "done", "dataset_dir": str(dataset),
            "splat_path": raw})
    if association == "stale":
        image.write_bytes(b"changed image")
    reveng_common._save_session(session)
    calls = []
    def mesh(cloud, out_path, **kwargs):
        calls.append(kwargs)
        Path(out_path).write_text("mesh")
        return {"ok": True, "approximate": True}
    monkeypatch.setattr(reveng_splat_tools, "splat_to_mesh", mesh)
    result = _call(tools, "reveng_splat_to_mesh", path_or_session=sid)
    assert result["ok"], result
    assert len(calls) == 1
    assert calls[0]["method"] == "auto"
    if association in ("matching", "supervised"):
        assert calls[0]["sparse_dir"] == str(sparse)
    else:
        assert "sparse_dir" not in calls[0]


def test_session_mesh_after_calibration_is_published_in_mm(tools, tmp_path, monkeypatch):
    o3d = pytest.importorskip("open3d")
    from services.vision.recon_transform import ReconTransform
    source = tmp_path / "source.ply"
    _source(source)
    imported = _call(tools, "reveng_import_splat", path=str(source))
    sid = imported["session_id"]
    session = reveng_common._load_session(sid, merge_gaussian=False)
    transform = ReconTransform(4.0, np.eye(3).tolist(), [2, -3, 5], "test", confirmed=True)
    session["recon_transform"] = transform.to_json()
    reveng_common._save_session(session)
    raw_vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
    def mesh(cloud, out_path, *, method, min_opacity, remove_floor):
        triangle = o3d.geometry.TriangleMesh()
        triangle.vertices = o3d.utility.Vector3dVector(raw_vertices)
        triangle.triangles = o3d.utility.Vector3iVector(np.array([[0, 1, 2]]))
        assert o3d.io.write_triangle_mesh(str(out_path), triangle)
        return {"ok": True, "mesh_path": str(out_path), "approximate": True, "triangles": 1}
    monkeypatch.setattr(reveng_splat_tools, "splat_to_mesh", mesh)
    result = _call(tools, "reveng_splat_to_mesh", path_or_session=sid)
    assert result["ok"] and result["units"] == "mm" and result["approximate"]
    session = reveng_common._load_session(sid, merge_gaussian=False)
    assert session["artifacts"]["mesh_transform_applied"]
    assert session["artifacts"]["raw_mesh_path"] == result["raw_mesh_path"]
    assert session["artifacts"]["mesh_path"] == result["mesh_path"]
    metric = o3d.io.read_triangle_mesh(result["mesh_path"])
    np.testing.assert_allclose(np.asarray(metric.vertices), transform.apply(raw_vertices), atol=1e-6)


def test_invalid_import_is_structured_and_preserves_existing_session(tools, tmp_path):
    source = tmp_path / "bad.splat"
    source.write_bytes(b"not a valid splat")
    sid = reveng_common._new_session_id()
    session = reveng_common._make_session(sid, "unchanged", None)
    reveng_common._save_session(session)
    result = _call(tools, "reveng_import_splat", path=str(source), session_id=sid)
    assert not result["ok"]
    after = reveng_common._load_session(sid, merge_gaussian=False)
    assert after["artifacts"]["splat_path"] is None and after["stages"]["capture"] == "idle"

"""Alignment, fit provenance, and derived-state regression coverage."""
import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

from services.mcp import reveng_common as common
from services.mcp.reveng_tools import register_reveng_tools
from services.vision.axis_align import align_axes
from services.vision.recon_transform import ReconTransform


class Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHUDWARE_REVENG_RUNS_DIR", str(tmp_path / "runs"))
    registry = Collector()
    register_reveng_tools(registry)
    sid = "re_alignment_test"
    common._save_session(common._make_session(sid, "alignment", None))
    return sid, registry.tools


def call(tools, name, **kwargs):
    return json.loads(tools[name](**kwargs))


def box_mesh(path):
    angle = 0.47
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0],
                         [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    mesh = trimesh.creation.box(extents=[7, 4, 2])
    mesh.vertices = mesh.vertices @ rotation.T + [3, -2, 5]
    mesh.export(path)
    return mesh, rotation


def attach_mesh(sid, mesh_path, *, confirmed=False):
    session = common._load_session(sid, merge_gaussian=False)
    session["artifacts"].update(mesh_path=str(mesh_path), raw_mesh_path=str(mesh_path),
                                mesh_transform_applied=False, mesh_units="reconstruction")
    transform = ReconTransform.identity()
    transform.confirmed = confirmed
    transform.mm_per_unit = 2
    session["recon_transform"] = transform.to_json()
    common._save_session(session)
    return session


def seed_derived(sid):
    session = common._load_session(sid, merge_gaussian=False)
    session["artifacts"].update(fits_path="old-fits.json", doc_id="old-doc",
                                step_path="old.step", cadquery_path="old.py",
                                mesh_materialization={"old": True})
    session["result"] = {"old": True}
    session["stages"].update(model="done", review="done", deliver="done")
    common._save_session(session)


def assert_invalidated(sid):
    session = common._load_session(sid, merge_gaussian=False)
    for key in ("fits_path", "step_path", "cadquery_path", "mesh_materialization"):
        assert key not in session["artifacts"]
    assert session["artifacts"].get("doc_id") is None
    assert "result" not in session
    assert all(session["stages"][key] == "idle" for key in ("model", "review", "deliver"))


@pytest.mark.parametrize("method", ["pca", "planes", "manual"])
def test_alignment_rotated_box(env, tmp_path, method):
    sid, tools = env
    path = tmp_path / "box.ply"
    mesh, native_rotation = box_mesh(path)
    attach_mesh(sid, path)
    args = {"session_id": sid, "method": method}
    if method == "manual":
        args["rotation"] = native_rotation.T.tolist()
    result = call(tools, "reveng_align_axes", **args)
    assert result["ok"], result
    assert result["alignment"]["method"] == method
    matrix = np.asarray(result["alignment"]["rotation"])
    np.testing.assert_allclose(matrix @ matrix.T, np.eye(3), atol=1e-6)
    assert np.linalg.det(matrix) == pytest.approx(1, abs=1e-6)
    aligned = (np.asarray(mesh.vertices) - np.mean(mesh.vertices, axis=0)) @ matrix.T
    np.testing.assert_allclose(sorted(np.ptp(aligned, axis=0)), [2, 4, 7], atol=1e-5)
    if method == "manual":
        np.testing.assert_allclose(matrix, native_rotation.T, atol=1e-6)


@pytest.mark.parametrize("rotation", [None, [[1, 0], [0, 1]],
    [[-1, 0, 0], [0, 1, 0], [0, 0, 1]],
    [[1, 1, 0], [0, 1, 0], [0, 0, 1]],
    [[float('nan'), 0, 0], [0, 1, 0], [0, 0, 1]]])
def test_manual_alignment_rejects_invalid_rotation(env, rotation):
    sid, tools = env
    before = common._load_session(sid, merge_gaussian=False)
    with pytest.raises(ValueError):
        align_axes(method="manual", rotation=rotation)
    result = call(tools, "reveng_align_axes", session_id=sid, method="manual", rotation=rotation)
    assert not result["ok"]
    assert common._load_session(sid, merge_gaussian=False) == before


def fit_box(env, tmp_path, monkeypatch, *, confirmed=False):
    sid, tools = env
    path = tmp_path / "box.ply"
    box_mesh(path)
    attach_mesh(sid, path, confirmed=confirmed)
    expected = [{"kind": "plane", "params_mm": {"normal": [1, 0, 0]}, "inliers": 20}]
    monkeypatch.setattr("services.vision.segment_fit.segment_fit",
                        lambda *args, **kwargs: {"ok": True, "fits": expected})
    result = call(tools, "reveng_fit_primitives", session_id=sid)
    assert result["ok"] and result["fits"] == expected
    return path, expected


@pytest.mark.parametrize("change", ["mesh_bytes", "calibration"])
def test_fit_provenance_rejects_stale_geometry_or_calibration(env, tmp_path, monkeypatch, change):
    path, expected = fit_box(env, tmp_path, monkeypatch)
    sid, _ = env
    session = common._load_session(sid, merge_gaussian=False)
    assert common._load_current_fits(session)["fits"] == expected
    if change == "mesh_bytes":
        path.write_bytes(path.read_bytes() + b"changed")
    else:
        session["recon_transform"]["mm_per_unit"] = 3
    assert common._load_current_fits(session) is None


def test_materializing_fitted_mesh_preserves_current_hints(env, tmp_path, monkeypatch):
    path, expected = fit_box(env, tmp_path, monkeypatch, confirmed=True)
    sid, _ = env
    def materialize(source, transform, target):
        mesh = trimesh.load(source, force="mesh", process=False)
        mesh.vertices = ReconTransform.from_json(transform).apply(np.asarray(mesh.vertices))
        mesh.export(target, file_type="ply")
        return {"ok": True}
    monkeypatch.setattr("services.vision.mesh_cleanup.apply_transform_to_mesh", materialize)
    session = common._load_session(sid, merge_gaussian=False)
    metric, failure = common._metric_mesh(session)
    assert failure is None and metric != str(path)
    persisted = common._load_session(sid, merge_gaussian=False)
    assert persisted["artifacts"]["mesh_transform_applied"]
    assert common._load_current_fits(persisted)["fits"] == expected
    Path(metric).write_bytes(Path(metric).read_bytes() + b"tampered")
    assert common._load_current_fits(persisted) is None


@pytest.mark.parametrize("operation", ["replace", "scale", "align"])
def test_geometry_changes_clear_derived_state(env, tmp_path, operation):
    sid, tools = env
    path = tmp_path / "box.ply"
    box_mesh(path)
    attach_mesh(sid, path)
    seed_derived(sid)
    if operation == "replace":
        result = call(tools, "import_scan_mesh", session_id=sid, path=str(path))
    elif operation == "scale":
        result = call(tools, "reveng_set_scale", session_id=sid, method="known_dim",
                      params={"value_mm": 10, "recon_length": 2, "source": "caliper"})
    else:
        result = call(tools, "reveng_align_axes", session_id=sid, method="manual",
                      rotation=np.eye(3).tolist())
    assert result["ok"], result
    assert_invalidated(sid)


def test_alignment_preserves_cleaned_mesh_when_restoring_native_gauge(env, tmp_path, monkeypatch):
    sid, tools = env
    original = trimesh.creation.box(extents=[9, 6, 3])
    cleaned = trimesh.creation.box(extents=[8, 5, 2])
    raw = tmp_path / "raw.ply"
    metric = tmp_path / "cleaned-metric.ply"
    original.export(raw)
    angle = 0.41
    rotation = [[np.cos(angle), -np.sin(angle), 0],
                [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
    old = ReconTransform(3, rotation, [7, -4, 9], "caliper", confirmed=True)
    native_vertices = np.asarray(cleaned.vertices).copy()
    cleaned.vertices = old.apply(native_vertices)
    cleaned.export(metric)
    session = common._load_session(sid, merge_gaussian=False)
    session["recon_transform"] = old.to_json()
    session["artifacts"].update(mesh_path=str(metric), raw_mesh_path=str(raw),
                                mesh_units="mm", mesh_transform_applied=True)
    common._save_session(session)

    def transform_mesh(source, transform, target):
        mesh = trimesh.load(source, force="mesh", process=False)
        mesh.vertices = ReconTransform.from_json(transform).apply(mesh.vertices)
        mesh.export(target, file_type="ply")
        return {"ok": True}

    monkeypatch.setattr("services.vision.mesh_cleanup.apply_transform_to_mesh", transform_mesh)
    result = call(tools, "reveng_align_axes", session_id=sid, method="manual",
                  rotation=np.eye(3).tolist())
    assert result["ok"], result
    restored = trimesh.load(result["session"]["artifacts"]["raw_mesh_path"],
                            force="mesh", process=False)
    restored_vertices = np.asarray(restored.vertices)
    assert len(restored_vertices) == len(native_vertices)
    distances = np.linalg.norm(restored_vertices[:, None, :] - native_vertices[None, :, :], axis=2)
    assert distances.min(axis=1).max() < 1e-5
    assert not result["session"]["artifacts"]["mesh_transform_applied"]

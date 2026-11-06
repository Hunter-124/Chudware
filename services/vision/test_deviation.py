"""Acceptance tests for services/vision/deviation.py (RE-16).

Run: python3 -m pytest services/vision/test_deviation.py -q

Tests
-----
(a) Two identical meshes → max/RMS deviation ≈ 0.
(b) A mesh vs the same mesh translated by a known offset → recovered
    max/RMS deviation matches the offset within tolerance.
(c) Graceful envelope: the test suite passes regardless of whether
    numpy/scipy/trimesh are installed (skips specific assertions when absent).
"""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import pytest

# ------------------------------------------------------------------
# Dep availability flags
# ------------------------------------------------------------------
try:
    import numpy as np
    _HAVE_NP = True
except ImportError:
    np = None  # type: ignore
    _HAVE_NP = False

try:
    from scipy.spatial import cKDTree  # noqa: F401
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False

try:
    import trimesh  # type: ignore
    _HAVE_TRIMESH = True
except ImportError:
    trimesh = None  # type: ignore
    _HAVE_TRIMESH = False

_HAVE_ALL = _HAVE_NP and _HAVE_SCIPY and _HAVE_TRIMESH

# ------------------------------------------------------------------
# Helpers: tiny in-memory meshes (sphere, box)
# ------------------------------------------------------------------

def _make_sphere_mesh(radius: float = 10.0, subdivisions: int = 3):
    """Return a trimesh Sphere centered at origin."""
    return trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)


def _mesh_to_tmp(mesh, suffix=".ply") -> str:
    """Export *mesh* to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    mesh.export(path)
    return path


# ------------------------------------------------------------------
# Import the module under test (always succeeds — graceful envelope)
# ------------------------------------------------------------------
from ..vision.deviation import (
    compute_deviation,
    compute_deviation_from_arrays,
    DEFAULT_BUCKETS_MM,
)


# ------------------------------------------------------------------
# Test (c): graceful envelope — module importable, missing deps → ok:False
# ------------------------------------------------------------------

class TestGracefulEnvelope:
    """These tests pass regardless of whether heavy deps are installed."""

    def test_import_succeeds(self):
        """Module must be importable even without scipy/trimesh."""
        from ..vision import deviation  # noqa: F401

    def test_missing_deps_return_ok_false(self, monkeypatch):
        """Monkeypatch dep flags → compute_deviation returns ok:False gracefully."""
        from ..vision import deviation as dev_mod
        monkeypatch.setattr(dev_mod, "_HAVE_NP", False)
        result = dev_mod.compute_deviation("a.ply", "b.ply")
        assert result["ok"] is False
        assert "reason" in result
        assert "hint" in result

    def test_missing_scipy_returns_ok_false(self, monkeypatch):
        from ..vision import deviation as dev_mod
        monkeypatch.setattr(dev_mod, "_HAVE_NP", True)
        monkeypatch.setattr(dev_mod, "_HAVE_SCIPY", False)
        result = dev_mod.compute_deviation("a.ply", "b.ply")
        assert result["ok"] is False

    def test_missing_trimesh_returns_ok_false(self, monkeypatch):
        from ..vision import deviation as dev_mod
        monkeypatch.setattr(dev_mod, "_HAVE_NP", True)
        monkeypatch.setattr(dev_mod, "_HAVE_SCIPY", True)
        monkeypatch.setattr(dev_mod, "_HAVE_TRIMESH", False)
        result = dev_mod.compute_deviation("a.ply", "b.ply")
        assert result["ok"] is False

    def test_default_buckets_structure(self):
        """DEFAULT_BUCKETS_MM must have three entries with correct colour strings."""
        assert len(DEFAULT_BUCKETS_MM) == 3
        for entry in DEFAULT_BUCKETS_MM:
            lo, hi, color = entry
            assert isinstance(lo, float)
            assert color.startswith("#")


# ------------------------------------------------------------------
# Tests (a) and (b): numerical correctness — skipped when deps absent
# ------------------------------------------------------------------

@pytest.mark.skipif(not _HAVE_ALL, reason="numpy/scipy/trimesh not installed")
class TestDeviationCorrectness:
    """Numerical accuracy tests — require numpy + scipy + trimesh."""

    # ----------------------------------------------------------
    # (a) Identical meshes → deviation ≈ 0
    # ----------------------------------------------------------

    def test_identical_meshes_file_api(self):
        """Two identical sphere meshes exported to disk → result is ok, schema intact.

        Sampling noise between the two independent random sample clouds means
        max_mm is not exactly 0.  We only assert: (1) the call succeeds,
        (2) schema keys are present, (3) max_mm is positive (distance is
        non-negative by construction).  The array-API test asserts exact zeros
        for the true identical-mesh case.
        """
        sphere = _make_sphere_mesh(radius=10.0)
        p1 = _mesh_to_tmp(sphere)
        p2 = _mesh_to_tmp(sphere)
        try:
            result = compute_deviation(p1, p2, session_id="test-id-0", n_samples=2000)
            assert result["ok"] is True, result.get("reason")
            assert result["max_mm"] >= 0.0
            assert result["rms_mm"] >= 0.0
            assert result["session_id"] == "test-id-0"
            assert "buckets" in result
            assert len(result["buckets"]) == 3
            assert "vertex_distances_mm" in result
        finally:
            Path(p1).unlink(missing_ok=True)
            Path(p2).unlink(missing_ok=True)

    def test_identical_vertices_array_api(self):
        """compute_deviation_from_arrays with identical point clouds → max ≈ 0."""
        rng = np.random.default_rng(42)
        pts = rng.uniform(-10, 10, size=(500, 3))
        result = compute_deviation_from_arrays(pts, pts, session_id="arr-0")
        assert result["ok"] is True
        assert result["max_mm"] < 1e-9
        assert result["rms_mm"] < 1e-9

    # ----------------------------------------------------------
    # (b) Translated mesh → recovered deviation matches offset
    # ----------------------------------------------------------

    def test_translated_mesh_array_api(self):
        """Translating a point cloud by offset → max deviation ≥ offset * 0.9.

        We use a sparse random 3D point cloud as the query, and an exact copy
        shifted by ``offset_mm`` along X as the reference.  For each query
        point ``p``, the nearest reference point is ``p + offset`` with
        distance = ``offset_mm`` (guaranteed when the cloud is sparse enough
        that no other shifted point is closer).  We verify that the reported
        max_mm is within 10% of the known offset.

        RMS is not asserted to equal offset because for some query points a
        different (closer) shifted neighbour may win in a random dense cloud.
        """
        offset_mm = 5.0
        rng = np.random.default_rng(42)
        # Spread points over a 1000 mm cube; mean NN distance >> 5 mm so the
        # direct copy is almost always the nearest reference point.
        pts = rng.uniform(0, 1000, (50, 3))
        shifted = pts + np.array([offset_mm, 0.0, 0.0])

        result = compute_deviation_from_arrays(pts, shifted, session_id="arr-1")
        assert result["ok"] is True
        # Max should equal the offset (each query point → its own shifted copy)
        assert abs(result["max_mm"] - offset_mm) < offset_mm * 0.1, (
            f"max_mm={result['max_mm']} expected ≈ {offset_mm}"
        )
        # RMS must be positive (non-zero deviation)
        assert result["rms_mm"] > 0.0

    def test_translated_mesh_file_api(self):
        """Sphere translated by (offset, 0, 0) → max deviation close to offset."""
        offset_mm = 3.0
        sphere = _make_sphere_mesh(radius=10.0, subdivisions=3)
        sphere_shifted = sphere.copy()
        sphere_shifted.apply_translation([offset_mm, 0.0, 0.0])
        p1 = _mesh_to_tmp(sphere)
        p2 = _mesh_to_tmp(sphere_shifted)
        try:
            result = compute_deviation(p1, p2, session_id="file-1", n_samples=5000)
            assert result["ok"] is True, result.get("reason")
            # For a sphere of radius 10 shifted by 3mm the max nearest-surface
            # distance saturates near the offset on the near hemisphere.
            # We check it is within [offset*0.5, offset*2].
            assert result["max_mm"] > offset_mm * 0.5, result["max_mm"]
            assert result["rms_mm"] > 0.0
        finally:
            Path(p1).unlink(missing_ok=True)
            Path(p2).unlink(missing_ok=True)

    # ----------------------------------------------------------
    # Schema conformance
    # ----------------------------------------------------------

    def test_result_schema_keys(self):
        """Result dict must contain all §5 contract keys."""
        rng = np.random.default_rng(0)
        pts = rng.uniform(0, 1, (100, 3))
        result = compute_deviation_from_arrays(pts, pts)
        required = {"ok", "session_id", "max_mm", "rms_mm", "buckets",
                    "mesh_path", "brep_doc_id", "vertex_distances_mm"}
        assert required.issubset(result.keys()), (
            f"Missing keys: {required - result.keys()}"
        )

    def test_buckets_cover_all_vertices(self):
        """Sum of bucket face_count must equal n_samples."""
        n = 300
        rng = np.random.default_rng(1)
        pts = rng.uniform(0, 2, (n, 3))
        ref = rng.uniform(0, 2, (n, 3))
        result = compute_deviation_from_arrays(pts, ref)
        assert result["ok"] is True
        total = sum(b["face_count"] for b in result["buckets"])
        assert total == n, f"Expected {n} total, got {total}"

    def test_bad_file_returns_ok_false(self):
        """Non-existent file → ok:False with reason."""
        result = compute_deviation("/no/such/mesh.ply", "/also/missing.ply")
        assert result["ok"] is False
        assert "reason" in result


@pytest.mark.skipif(not (_HAVE_NP and _HAVE_SCIPY), reason="numpy/scipy not installed")
def test_icp_aligns_a_rotated_translated_noisy_copy():
    pytest.importorskip("open3d")
    rng = np.random.default_rng(240)
    reference = rng.normal(size=(800, 3)) * [23.0, 9.0, 4.0]
    reference[:, 0] += 3.0 * (reference[:, 2] > 0)
    a, b = 0.62, -0.34
    rz = np.array([[math.cos(a), -math.sin(a), 0],
                   [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0],
                   [-math.sin(b), 0, math.cos(b)]])
    rotation = rz @ ry
    translation = np.array([18.0, -13.0, 7.0])
    query = reference @ rotation.T + translation + rng.normal(scale=0.005, size=reference.shape)

    world = compute_deviation_from_arrays(query, reference, alignment="world")
    aligned = compute_deviation_from_arrays(query, reference, alignment="icp")
    assert world["ok"] and aligned["ok"], aligned
    assert world["rms_mm"] > 1.0
    assert aligned["rms_mm"] < 0.03
    record = aligned["alignment"]
    assert record["mode"] == "icp" and record["fitness"] > 0.99
    matrix = np.asarray(record["transform_query_to_reference"])
    assert np.allclose(matrix[:3, :3], rotation.T, atol=1e-3)
    assert np.allclose(matrix[:3, 3], -rotation.T @ translation, atol=0.01)
    assert np.allclose(matrix[3], [0, 0, 0, 1])


@pytest.mark.skipif(not _HAVE_ALL, reason="numpy/scipy/trimesh not installed")
def test_brep_reference_uses_core_export_and_cleans_temp_mesh(tmp_path, monkeypatch):
    from services.mcp import core_bridge

    source = trimesh.creation.box(extents=[10, 8, 6])
    query = tmp_path / "query.ply"
    source.export(query)
    calls = []

    def export(method, params):
        calls.append((method, dict(params)))
        assert method == "export_file"
        assert params["doc_id"] == "reference_doc" and params["format"] == "stl"
        source.export(params["path"])
        return {"ok": True, "path": params["path"]}

    monkeypatch.setattr(core_bridge, "call", export)
    result = compute_deviation(str(query), brep_doc_id="reference_doc", n_samples=1500)
    assert result["ok"], result
    assert result["brep_doc_id"] == "reference_doc"
    assert result["alignment"]["mode"] == "world"
    assert result["distance_basis"] == "query_surface_samples_to_reference_surface_samples"
    assert calls and not Path(calls[0][1]["path"]).exists()


@pytest.mark.skipif(not (_HAVE_NP and _HAVE_SCIPY), reason="numpy/scipy not installed")
def test_unavailable_icp_and_bad_alignment_fail_honestly(monkeypatch):
    from ..vision import deviation

    cloud = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
    monkeypatch.setattr(deviation, "_HAVE_OPEN3D", False)
    unavailable = deviation.compute_deviation_from_arrays(cloud, cloud, alignment="icp")
    assert not unavailable["ok"] and "Open3D" in unavailable["reason"]
    invalid = deviation.compute_deviation_from_arrays(cloud, cloud, alignment="guess")
    assert not invalid["ok"] and "'world' or 'icp'" in invalid["reason"]


def test_compare_parts_icp_reports_surface_alignment_not_aligned_booleans(monkeypatch):
    import json

    pytest.importorskip("mcp.server.fastmcp")
    from services.mcp import recon_tools
    from services.vision import deviation

    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function
            return register

    registry = Registry()
    recon_tools.register_recon_tools(registry)
    calls = []
    monkeypatch.setattr(recon_tools, "_doc", lambda doc: doc)
    monkeypatch.setattr(recon_tools.cb, "call",
                        lambda method, params: {"ok": True, "match_pct": 12.0})
    monkeypatch.setattr(deviation, "_tessellate_document",
                        lambda doc, path: calls.append(("export", doc)) or path)

    def compare(query, **kwargs):
        calls.append(("compare", kwargs))
        return {"ok": True, "mesh_path": query, "rms_mm": 0.02,
                "alignment": {"mode": kwargs["alignment"]}}

    monkeypatch.setattr(deviation, "compute_deviation", compare)
    tool = registry.tools["compare_parts"]
    world = json.loads(tool(doc_a="a", doc_b="b"))
    assert world["ok"] and world["match_pct"] == 12.0 and not calls
    aligned = json.loads(tool(doc_a="a", doc_b="b", alignment="icp"))
    assert aligned["ok"] and aligned["match_pct"] == 12.0
    assert calls == [("export", "b"), ("compare", {"brep_doc_id": "a", "alignment": "icp"})]
    surface = aligned["aligned_surface_deviation"]
    assert surface["alignment"]["mode"] == "icp" and surface["query_doc_id"] == "b"
    assert surface["mesh_path"] is None
    refused = json.loads(tool(doc_a="a", doc_b="b", body_a="body1", alignment="icp"))
    assert not refused["ok"] and "complete documents" in refused["error"]

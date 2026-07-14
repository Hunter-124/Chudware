"""Unit tests for CAD-Recode ML parametric backend.

Covers:
  - preprocess FPS determinism + normalize/inverse round-trip
  - subprocess CadQuery sandbox (ok STEP / hang timeout / broken error)
  - API degradation when torch is missing (no network, no model download)
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from engine_oracle.vision import cad_recode as cr


# ---------------------------------------------------------------------------
# Preprocess helpers (no torch required)
# ---------------------------------------------------------------------------

def _make_box_stl(path: Path, size: float = 10.0) -> Path:
    """Write a minimal ASCII STL axis-aligned box of edge *size* (mm)."""
    # 12 triangles, 2 per face, unit box scaled to *size*, centred at origin.
    s = size / 2.0
    # 8 vertices of the cube
    v = [
        (-s, -s, -s),
        (s, -s, -s),
        (s, s, -s),
        (-s, s, -s),
        (-s, -s, s),
        (s, -s, s),
        (s, s, s),
        (-s, s, s),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3),  # bottom
        (4, 6, 5), (4, 7, 6),  # top
        (0, 4, 5), (0, 5, 1),  # front
        (1, 5, 6), (1, 6, 2),  # right
        (2, 6, 7), (2, 7, 3),  # back
        (3, 7, 4), (3, 4, 0),  # left
    ]

    def _facet(a, b, c):
        return (
            "  facet normal 0 0 0\n"
            "    outer loop\n"
            f"      vertex {v[a][0]} {v[a][1]} {v[a][2]}\n"
            f"      vertex {v[b][0]} {v[b][1]} {v[b][2]}\n"
            f"      vertex {v[c][0]} {v[c][1]} {v[c][2]}\n"
            "    endloop\n"
            "  endfacet\n"
        )

    body = "solid box\n" + "".join(_facet(*f) for f in faces) + "endsolid box\n"
    path.write_text(body)
    return path


class TestFpsNumpy:
    def test_deterministic_with_seed(self):
        rng = np.random.default_rng(42)
        pts = rng.normal(size=(200, 3))
        a = cr._fps_numpy(pts, k=16, seed=7)
        b = cr._fps_numpy(pts, k=16, seed=7)
        np.testing.assert_array_equal(a, b)

    def test_different_seed_can_differ(self):
        rng = np.random.default_rng(0)
        pts = rng.normal(size=(200, 3))
        a = cr._fps_numpy(pts, k=8, seed=1)
        b = cr._fps_numpy(pts, k=8, seed=2)
        # First index is seed-dependent; almost always differs.
        assert a[0] != b[0] or not np.array_equal(a, b)

    def test_k_capped_at_n(self):
        pts = np.eye(3)
        ids = cr._fps_numpy(pts, k=10, seed=0)
        assert len(ids) == 3


class TestNormalizeRoundTrip:
    def test_apply_normalize_inverse(self):
        pts = np.array([[1.0, 2.0, 3.0], [-4.0, 0.5, 8.0]])
        transform = {"center": [1.0, 0.0, -2.0], "scale": 0.25}
        normed = cr.apply_normalize(pts, transform)
        back = cr.apply_inverse(normed, transform)
        np.testing.assert_allclose(back, pts, rtol=1e-9, atol=1e-9)

    def test_compute_normalize_max_extent_two(self, tmp_path):
        pytest.importorskip("trimesh")
        stl = _make_box_stl(tmp_path / "box.stl", size=20.0)
        import trimesh

        mesh = trimesh.load_mesh(str(stl), force="mesh")
        t = cr.compute_normalize_transform(mesh)
        # max extent of a 20mm cube is 20 → scale = 2/20 = 0.1
        assert t["scale"] == pytest.approx(0.1)
        assert t["center"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)

    def test_preprocess_shape_and_roundtrip(self, tmp_path):
        pytest.importorskip("trimesh")
        stl = _make_box_stl(tmp_path / "box.stl", size=10.0)
        out = cr.preprocess_mesh(str(stl), n_pre_points=512, n_points=32, seed=0)
        pts = out["points"]
        assert pts.shape == (32, 3)
        assert pts.dtype == np.float32
        t = out["scale_transform"]
        # After normalize, max abs extent should be ~1 (half of size-2 cube).
        assert float(np.max(np.abs(pts))) <= 1.0 + 1e-3
        # Inverse of a normalised corner should land near metric scale.
        metric = cr.apply_inverse(pts, t)
        assert float(np.max(np.linalg.norm(metric, axis=1))) == pytest.approx(
            5.0 * np.sqrt(3), rel=0.15
        )


# ---------------------------------------------------------------------------
# Subprocess CadQuery sandbox
# ---------------------------------------------------------------------------

# Minimal CadQuery snippet that binds global `r` to a 10×10×10 box.
_CANNED_CQ = textwrap.dedent(
    """\
    import cadquery as cq
    r = cq.Workplane("XY").box(10, 10, 10)
    """
)

_BROKEN_CQ = "raise RuntimeError('intentionally broken cadquery source')\n"

_HANG_CQ = textwrap.dedent(
    """\
    import time
    time.sleep(60)
    r = None
    """
)


class TestMaterializeSandbox:
    def test_canned_snippet_exports_step(self, tmp_path):
        cadquery = pytest.importorskip("cadquery")
        del cadquery  # silence unused
        transform = {"center": [0.0, 0.0, 0.0], "scale": 1.0}
        result = cr.materialize_cadquery(
            _CANNED_CQ,
            str(tmp_path),
            transform,
            timeout_s=30.0,
            step_name="ok.step",
        )
        assert result["ok"] is True, result.get("error")
        assert result["step_path"] is not None
        assert Path(result["step_path"]).is_file()
        assert Path(result["step_path"]).stat().st_size > 0

    def test_broken_snippet_returns_error(self, tmp_path):
        # cadquery may or may not be installed; the runner still starts and
        # should report the intentional RuntimeError (or import failure).
        transform = {"center": [0.0, 0.0, 0.0], "scale": 1.0}
        result = cr.materialize_cadquery(
            _BROKEN_CQ,
            str(tmp_path),
            transform,
            timeout_s=15.0,
            step_name="broken.step",
        )
        assert result["ok"] is False
        assert result["error"]
        assert result["step_path"] in (None, "")

    def test_hanging_snippet_times_out(self, tmp_path):
        transform = {"center": [0.0, 0.0, 0.0], "scale": 1.0}
        result = cr.materialize_cadquery(
            _HANG_CQ,
            str(tmp_path),
            transform,
            timeout_s=1.0,
            step_name="hang.step",
        )
        assert result["ok"] is False
        assert "timed out" in (result["error"] or "").lower()


# ---------------------------------------------------------------------------
# API degradation when torch / weights missing
# ---------------------------------------------------------------------------

class TestBuildParametricDegradation:
    def test_not_provisioned_when_torch_missing(self, tmp_path, monkeypatch):
        """Monkeypatch torch import failure → ok=False, error mentions not provisioned."""
        stl = _make_box_stl(tmp_path / "box.stl")

        monkeypatch.setattr(cr, "_torch_importable", lambda: False)
        monkeypatch.setattr(cr, "weights_path", lambda: None)
        monkeypatch.setattr(cr, "is_provisioned", lambda: False)

        result = cr.build_parametric(str(stl), str(tmp_path / "sess"))
        assert result["ok"] is False
        assert result["cadquery_src"] is None
        assert result["step_path"] is None
        assert "not provisioned" in (result["error"] or "").lower()
        assert result["provenance"]["method"] == "ml_cad_recode"
        assert result["provenance"]["model"] == "filapro/cad-recode-v1.5"

    def test_not_provisioned_when_weights_missing(self, tmp_path, monkeypatch):
        stl = _make_box_stl(tmp_path / "box.stl")
        monkeypatch.setattr(cr, "_torch_importable", lambda: True)
        monkeypatch.setattr(cr, "weights_path", lambda: None)
        monkeypatch.setattr(cr, "is_provisioned", lambda: False)

        result = cr.build_parametric(str(stl), str(tmp_path / "sess"))
        assert result["ok"] is False
        assert "not provisioned" in (result["error"] or "").lower()

    def test_module_import_is_dep_free(self):
        """Importing cad_recode must not pull torch/trimesh/cadquery at module level."""
        # Re-import in isolation via source inspection of top-level names.
        import ast

        src = Path(cr.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        top_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_imports.add(node.module.split(".")[0])
        forbidden = {"torch", "transformers", "trimesh", "cadquery", "pytorch3d", "flash_attn"}
        assert not (top_imports & forbidden), (
            f"top-level imports must stay dep-free; found {top_imports & forbidden}"
        )


class TestProvisionDescriptor:
    def test_cad_recode_descriptor_present(self):
        from engine_oracle.vision import reveng_provision as rp

        assert "cad_recode" in rp.REVENG_DESCRIPTORS
        d = rp.REVENG_DESCRIPTORS["cad_recode"]
        assert d["kind"] == "weights"
        assert d["env_var"] == "CAD_RECODE_WEIGHTS"
        assert "filapro/cad-recode-v1.5" in (d.get("url") or "")
        assert d["license"] == "CC-BY-NC-4.0"

    def test_reveng_status_reports_cad_recode(self, monkeypatch):
        from engine_oracle.vision import reveng_provision as rp

        monkeypatch.setattr(rp, "_artifact_available", lambda name: name != "cad_recode")
        monkeypatch.setattr(rp, "_gpu_available", lambda: True)
        monkeypatch.setattr(rp, "_webcam_available", lambda: True)
        status = rp.reveng_status()
        assert "cad_recode" in status
        assert status["cad_recode"]["available"] is False
        assert any("CAD-Recode" in b for b in status["banners"])

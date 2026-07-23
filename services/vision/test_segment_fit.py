"""Tests for ``segment_fit`` — mesh → fitted-primitive extraction (RE-13).

Two independent paths:

* ``test_envelope_backend_absent`` monkeypatches the backend flags to False and
  asserts the exact ``chudware`` degradation envelope. This must pass regardless of
  what is installed, so no ``importorskip`` guard.
* ``test_box_cylinder_fit`` builds a box + cylindrical boss with trimesh, runs the
  fitter, and asserts the recovered plane offset and cylinder radius/axis are within
  0.1 mm of ground truth. Guarded with ``importorskip`` on the exact backends the
  fitter needs (trimesh + pyransac3d), so it *skips* (== pass) when they are absent.
"""
from __future__ import annotations

import numpy as np
import pytest

from ..vision import segment_fit


def test_envelope_backend_absent(monkeypatch):
    """No backend → contract §0 degradation envelope, verbatim."""
    monkeypatch.setattr(segment_fit, "HAVE_OPEN3D", False)
    monkeypatch.setattr(segment_fit, "HAVE_TRIMESH", False)
    monkeypatch.setattr(segment_fit, "HAVE_PYRANSAC3D", False)

    out = segment_fit.segment_fit(mesh_path="/nonexistent.ply", session_id="re_test")
    assert out == {
        "ok": False,
        "reason": "backend_not_provisioned",
        "hint": "pip install open3d",
    }


def _build_box_with_boss():
    """Box 40x30x10 mm with a cylindrical boss (r=4, h=12) centered on the top."""
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("pyransac3d")

    box = trimesh.creation.box(extents=(40.0, 30.0, 10.0))
    # Box spans z in [-5, 5]; top face at z=+5. Boss sits on top, extends up 12mm.
    boss = trimesh.creation.cylinder(radius=4.0, height=12.0, sections=64)
    boss.apply_translation((0.0, 0.0, 5.0 + 6.0))  # base at z=5, top at z=17
    mesh = trimesh.util.concatenate([box, boss])
    return mesh


def test_box_cylinder_fit(tmp_path):
    """Box + cylindrical boss → plane and cylinder recovered within 0.1 mm."""
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("pyransac3d")

    mesh = _build_box_with_boss()
    mesh_path = str(tmp_path / "box_boss.ply")
    mesh.export(mesh_path)

    out = segment_fit.segment_fit(
        mesh_path=mesh_path,
        session_id="re_fit",
        n_samples=40000,
        plane_thresh_mm=0.2,
        cyl_thresh_mm=0.2,
    )

    assert out["ok"] is True
    assert out["schema"] == "aicad.reveng_fits/1"
    assert out["session_id"] == "re_fit"
    assert isinstance(out["fits"], list)
    assert isinstance(out["sections"], list)

    planes = [f for f in out["fits"] if f["kind"] == "plane"]
    cyls = [f for f in out["fits"] if f["kind"] == "cylinder"]
    assert planes, "expected at least one plane fit"
    assert cyls, "expected a cylinder fit for the boss"

    # --- Cylinder: radius truth 4.0 mm, axis ~ +/-Z -------------------------
    cyl = max(cyls, key=lambda f: f["inliers"])
    r = cyl["params_mm"]["radius"]
    axis = np.asarray(cyl["params_mm"]["axis"], dtype=float)
    assert abs(r - 4.0) <= 0.1, f"cylinder radius {r} not within 0.1mm of 4.0"
    # axis should be aligned with Z (dot with unit-Z close to +/-1)
    z_align = abs(float(axis @ np.array([0.0, 0.0, 1.0])))
    assert z_align > 0.99, f"cylinder axis {axis.tolist()} not Z-aligned"

    # --- Plane: some peeled plane must match the top face at z=5 -----------
    # A top-face plane has normal ~ +/-Z and passes through z=5. Its signed
    # offset along its own normal equals +/-5.0. Find the closest match.
    def plane_z(p):
        n = np.asarray(p["params_mm"]["normal"], dtype=float)
        origin = np.asarray(p["params_mm"]["origin"], dtype=float)
        return n, origin

    best_err = None
    for p in planes:
        n, origin = plane_z(p)
        if abs(float(n @ np.array([0.0, 0.0, 1.0]))) > 0.99:
            err = abs(abs(float(origin[2])) - 5.0)
            best_err = err if best_err is None else min(best_err, err)
    assert best_err is not None, "no Z-normal (top/bottom) plane recovered"
    assert best_err <= 0.1, f"top-face plane z-offset off by {best_err}mm (>0.1)"

    # residuals sane
    for f in out["fits"]:
        assert f["residual_mm"] <= 0.5

"""Tests for ``segment_fit`` — mesh → fitted-primitive extraction (RE-13).

``test_envelope_backend_absent`` monkeypatches the backend flags to False and
asserts the exact ``chudware`` degradation envelope. This must pass regardless of
what is installed, so no ``importorskip`` guard.

Every other test builds an exact analytic solid with trimesh — box + boss, lone
sphere, cone, plate with two bosses, annular plate (through bore), the same plate
with a blind bore, and a four-walled pocket — runs the fitter, and asserts the
recovered geometry against the parameters the solid was built from, to the same
0.1 mm rigour the original box+boss test set. They also pin the semantics a
parametric rebuild depends on: a bore reads ``role == "hole"`` with
``through is True`` and the true wall thickness as ``depth_mm``, a blind bore of
the same diameter reads ``through is False``, and the cross-sections come back as
closed polylines whose areas match the analytic profile.

Booleans are deliberately avoided in the fixtures: ``manifold3d`` is an optional
trimesh dependency and is absent on plenty of dev boxes, so the "plate with a
through hole" is an annular plate rather than a bored rectangle.

All of these are guarded with ``importorskip`` on the exact backends the fitter
needs (trimesh + pyransac3d), so they *skip* (== pass) when those are absent.
"""
from __future__ import annotations

import json
import math

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


# ---------------------------------------------------------------------------
# Fitters beyond plane + one cylinder: sphere, cone, the multi-cylinder peel,
# feature semantics (hole / boss / shaft / through / depth) and cross-sections.
# ---------------------------------------------------------------------------
def _require_backends():
    """Skip unless the exact backends the fitter needs are installed."""
    mesh_lib = pytest.importorskip("trimesh")
    pytest.importorskip("pyransac3d")
    return mesh_lib


def _fit(mesh, tmp_path, name, **kwargs):
    """Export ``mesh`` and run the fitter over it with the test defaults."""
    path = str(tmp_path / f"{name}.ply")
    mesh.export(path)
    kwargs.setdefault("n_samples", 40000)
    kwargs.setdefault("plane_thresh_mm", 0.2)
    kwargs.setdefault("cyl_thresh_mm", 0.2)
    out = segment_fit.segment_fit(mesh_path=path, session_id=f"re_{name}", **kwargs)
    assert out["ok"] is True, out
    return out


def _of_kind(out, kind):
    return [f for f in out["fits"] if f["kind"] == kind]


def test_sphere_fit(tmp_path):
    """A lone sphere fits as ONE sphere, not a heap of spurious plane/cylinder bands."""
    mesh_lib = _require_backends()
    mesh = mesh_lib.creation.icosphere(subdivisions=5, radius=7.0)
    mesh.apply_translation((3.0, -2.0, 1.0))
    out = _fit(mesh, tmp_path, "sphere")

    spheres = _of_kind(out, "sphere")
    assert len(spheres) == 1, f"expected one sphere, got {[f['kind'] for f in out['fits']]}"
    fit = spheres[0]
    assert abs(fit["params_mm"]["radius"] - 7.0) <= 0.1
    center = np.asarray(fit["params_mm"]["center"], dtype=float)
    err = float(np.linalg.norm(center - np.array([3.0, -2.0, 1.0])))
    assert err <= 0.1, f"sphere centre off by {err}mm"
    assert fit["confidence"] > 0.9
    assert fit["residual_mm"] <= 0.05
    # A sphere has no flat faces: any "plane" here would be a curved band the
    # plane peel mistook for one, which is exactly what shreds sphere/cone fits.
    assert not _of_kind(out, "plane")


def test_cone_fit(tmp_path):
    """Cone r=6 h=15: apex, half-angle, axis and base radius all recovered."""
    mesh_lib = _require_backends()
    # trimesh builds this with the base on z=0 and the tip at z=+15.
    mesh = mesh_lib.creation.cone(radius=6.0, height=15.0, sections=128)
    out = _fit(mesh, tmp_path, "cone", plane_thresh_mm=0.05)

    cones = _of_kind(out, "cone")
    assert cones, f"no cone recovered, got {[f['kind'] for f in out['fits']]}"
    fit = max(cones, key=lambda f: f["inliers"])
    params = fit["params_mm"]

    apex = np.asarray(params["apex"], dtype=float)
    apex_err = float(np.linalg.norm(apex - np.array([0.0, 0.0, 15.0])))
    assert apex_err <= 0.1, f"apex off by {apex_err}mm"
    truth_half_angle = math.atan2(6.0, 15.0)
    assert abs(params["half_angle_rad"] - truth_half_angle) <= 0.005
    axis = np.asarray(params["axis"], dtype=float)
    # Axis runs apex -> base, so it points down -Z for this cone.
    assert float(axis @ np.array([0.0, 0.0, 1.0])) < -0.99
    assert abs(params["radius_at_base"] - 6.0) <= 0.1
    assert abs(params["height"] - 15.0) <= 0.1
    # The emitted radius/height/angle triple must be self-consistent.
    implied = math.tan(params["half_angle_rad"]) * params["height"]
    assert abs(params["radius_at_base"] - implied) <= 1e-3
    assert fit["residual_mm"] <= 0.05


def test_multi_cylinder_peel(tmp_path):
    """Two bosses of different diameter are peeled as two separate cylinders."""
    mesh_lib = _require_backends()
    plate = mesh_lib.creation.box(extents=(60.0, 40.0, 8.0))
    small = mesh_lib.creation.cylinder(radius=3.0, height=14.0, sections=96)
    small.apply_translation((-15.0, 0.0, 11.0))  # base on the z=4 top face
    large = mesh_lib.creation.cylinder(radius=6.0, height=10.0, sections=96)
    large.apply_translation((15.0, 0.0, 9.0))
    mesh = mesh_lib.util.concatenate([plate, small, large])
    out = _fit(mesh, tmp_path, "multi", n_samples=60000)

    cyls = _of_kind(out, "cylinder")
    assert len(cyls) == 2, f"expected exactly 2 cylinders, got {len(cyls)}"
    by_radius = sorted(cyls, key=lambda f: f["params_mm"]["radius"])
    assert abs(by_radius[0]["params_mm"]["radius"] - 3.0) <= 0.1
    assert abs(by_radius[1]["params_mm"]["radius"] - 6.0) <= 0.1
    # Each cylinder must sit over its own seat, not on a blended axis between them.
    seats = {round(f["params_mm"]["radius"]): f["params_mm"]["origin"] for f in cyls}
    assert abs(seats[3][0] + 15.0) <= 0.1 and abs(seats[3][1]) <= 0.1
    assert abs(seats[6][0] - 15.0) <= 0.1 and abs(seats[6][1]) <= 0.1
    for fit in cyls:
        assert fit["role"] == "boss", f"{fit['params_mm']['radius']} -> {fit['role']}"
        assert fit["through"] is False
    assert abs(by_radius[0]["depth_mm"] - 14.0) <= 0.5
    assert abs(by_radius[1]["depth_mm"] - 10.0) <= 0.5


def test_plate_with_through_hole(tmp_path):
    """Annular plate: the bore reads as a through hole, the OD as external material."""
    mesh_lib = _require_backends()
    mesh = mesh_lib.creation.annulus(r_min=4.0, r_max=20.0, height=10.0, sections=128)
    out = _fit(mesh, tmp_path, "washer", section_axis="z")

    cyls = _of_kind(out, "cylinder")
    assert len(cyls) == 2, f"expected bore + OD, got {len(cyls)}"
    bore = min(cyls, key=lambda f: f["params_mm"]["radius"])
    outer = max(cyls, key=lambda f: f["params_mm"]["radius"])

    assert abs(bore["params_mm"]["radius"] - 4.0) <= 0.1
    assert abs(outer["params_mm"]["radius"] - 20.0) <= 0.1
    # Normals on the bore point at the axis (material outside) -> hole; normals on
    # the OD point away from it (material inside) and it spans the whole part.
    assert bore["role"] == "hole"
    assert bore["through"] is True
    assert abs(bore["depth_mm"] - 10.0) <= 0.1, "a through bore's depth is the wall"
    assert outer["role"] == "shaft"
    assert outer["through"] is False

    axis = np.asarray(bore["params_mm"]["axis"], dtype=float)
    assert abs(float(axis @ np.array([0.0, 0.0, 1.0]))) > 0.99
    offset = float(np.linalg.norm(np.asarray(bore["params_mm"]["origin"], dtype=float)[:2]))
    assert offset <= 0.1, f"bore axis {offset}mm off centre"

    truth = math.pi * (20.0 ** 2 - 4.0 ** 2)
    assert len(out["sections"]) == 12
    for section in out["sections"]:
        assert section["axis"] == [0.0, 0.0, 1.0]
        assert section["closed"] is True
        assert len(section["polylines"]) == 2, "outer profile + bore"
        assert abs(section["area_mm2"] - truth) <= 0.01 * truth


def test_blind_hole_is_not_through(tmp_path):
    """Control for the through test: the same bore stopped half way reads blind."""
    mesh_lib = _require_backends()
    ring = mesh_lib.creation.annulus(r_min=6.0, r_max=20.0, height=7.0, sections=128)
    ring.apply_translation((0.0, 0.0, 2.5))
    floor = mesh_lib.creation.cylinder(radius=20.0, height=5.0, sections=128)
    floor.apply_translation((0.0, 0.0, -3.5))
    # 12 mm thick overall, bored 7 mm deep from the top.
    out = _fit(mesh_lib.util.concatenate([ring, floor]), tmp_path, "blind", n_samples=60000)

    bores = [f for f in _of_kind(out, "cylinder") if f["role"] == "hole"]
    assert bores, "blind bore not recovered"
    bore = min(bores, key=lambda f: f["params_mm"]["radius"])
    assert abs(bore["params_mm"]["radius"] - 6.0) <= 0.1
    assert bore["through"] is False
    assert 6.0 <= bore["depth_mm"] <= 7.1, bore["depth_mm"]


def test_sections_are_closed_profiles(tmp_path):
    """Sections must be real closed polylines in a frame that round-trips to 3D."""
    _require_backends()
    out = _fit(_build_box_with_boss(), tmp_path, "sections")

    sections = out["sections"]
    assert len(sections) == 12
    assert all(s["closed"] for s in sections)
    assert all(s["polylines"] for s in sections)
    axis = np.asarray(sections[0]["axis"], dtype=float)
    # 40 mm X is the largest-extent principal axis of a 40x30x10 slab.
    assert abs(float(axis @ np.array([1.0, 0.0, 0.0]))) > 0.99

    slab = [s for s in sections if len(s["polylines"]) == 1]
    assert len(slab) >= 8, "most stations miss the boss and cut the bare 30x10 slab"
    for section in slab:
        assert abs(section["area_mm2"] - 300.0) <= 1.0
    assert max(s["area_mm2"] for s in sections) > 300.0, "boss stations add material"

    frame = slab[0]["frame"]
    origin = np.asarray(frame["origin"], dtype=float)
    e1 = np.asarray(frame["e1"], dtype=float)
    e2 = np.asarray(frame["e2"], dtype=float)
    poly = np.asarray(slab[0]["polylines"][0], dtype=float)
    lifted = origin + poly[:, :1] * e1 + poly[:, 1:2] * e2
    assert np.allclose(lifted @ axis, slab[0]["station_mm"], atol=1e-3)


def test_sections_manual_fallback_matches_trimesh(tmp_path):
    """The trimesh-free slicer must agree with trimesh's, not merely return something."""
    mesh_lib = _require_backends()
    mesh = mesh_lib.creation.annulus(r_min=4.0, r_max=20.0, height=10.0, sections=96)
    axis = np.array([0.0, 0.0, 1.0])
    e1, e2 = segment_fit._axis_perp_basis(axis)
    stations = [-3.0, 0.0, 2.5]

    manual = segment_fit._sections_manual(
        np.asarray(mesh.vertices, dtype=float),
        np.asarray(mesh.faces, dtype=int),
        axis,
        e1,
        e2,
        stations,
    )
    native = segment_fit._sections_trimesh(mesh, axis, e1, e2, stations)
    assert len(manual) == len(native) == len(stations)

    truth = math.pi * (20.0 ** 2 - 4.0 ** 2)
    for (station_m, polys_m), (station_n, polys_n) in zip(manual, native):
        assert station_m == station_n
        assert len(polys_m) == len(polys_n) == 2
        assert all(segment_fit._polyline_closed(p) for p in polys_m)
        area_m = segment_fit._net_area(polys_m)
        assert abs(area_m - segment_fit._net_area(polys_n)) <= 1e-6
        assert abs(area_m - truth) <= 0.01 * truth, "the bore must subtract, not add"


def test_points_only_reports_missing_sections(tmp_path):
    """No mesh → no cross-sections, and the result says why instead of inventing them."""
    mesh_lib = _require_backends()
    mesh = mesh_lib.creation.annulus(r_min=4.0, r_max=20.0, height=10.0, sections=96)
    path = str(tmp_path / "cloud.ply")
    mesh.export(path)
    pts, _normals = segment_fit._load_points(path, 30000)

    out = segment_fit.segment_fit(
        points=pts, session_id="re_pts", plane_thresh_mm=0.2, cyl_thresh_mm=0.2
    )
    assert out["ok"] is True
    assert out["sections"] == []
    assert ("sections", "no_mesh") in {(n["stage"], n["reason"]) for n in out["notes"]}

    # Geometry still recovers from a bare cloud; roles honestly stay unknown,
    # because estimated normals have no orientation to reason about.
    cyls = _of_kind(out, "cylinder")
    assert cyls
    assert abs(min(f["params_mm"]["radius"] for f in cyls) - 4.0) <= 0.1
    assert all(f["role"] == "unknown" for f in cyls)


def test_plane_roles(tmp_path):
    """An enclosed floor reads as a pocket; a plain face carrying a boss does not."""
    mesh_lib = _require_backends()
    parts = [mesh_lib.creation.box(extents=(50.0, 40.0, 6.0))]
    parts[0].apply_translation((0.0, 0.0, -3.0))  # floor top face at z=0
    for extents, shift in (
        ((50.0, 5.0, 15.0), (0.0, 17.5, 4.5)),
        ((50.0, 5.0, 15.0), (0.0, -17.5, 4.5)),
        ((5.0, 30.0, 15.0), (22.5, 0.0, 4.5)),
        ((5.0, 30.0, 15.0), (-22.5, 0.0, 4.5)),
    ):
        wall = mesh_lib.creation.box(extents=extents)
        wall.apply_translation(shift)
        parts.append(wall)
    out = _fit(mesh_lib.util.concatenate(parts), tmp_path, "pocket", n_samples=60000, max_planes=8)

    floors = [
        p
        for p in _of_kind(out, "plane")
        if abs(p["params_mm"]["origin"][2]) <= 0.2
        and abs(abs(p["params_mm"]["normal"][2]) - 1.0) < 0.01
    ]
    assert floors, "pocket floor plane not recovered"
    assert floors[0]["role"] == "pocket"

    plain = _fit(_build_box_with_boss(), tmp_path, "plain")
    assert all(p["role"] != "pocket" for p in _of_kind(plain, "plane")), (
        "a plain face with a central boss is not enclosed, so it is not a pocket"
    )


def test_v1_contract_keys_preserved(tmp_path):
    """``aicad.reveng_fits/1`` is append-only: no v1 key renamed, dropped or retyped."""
    _require_backends()
    out = _fit(_build_box_with_boss(), tmp_path, "contract")

    assert out["schema"] == "aicad.reveng_fits/1"
    assert out["fits_revision"] >= 2
    for key in ("schema", "ok", "session_id", "mesh_path", "fits", "sections"):
        assert key in out

    for fit in out["fits"]:
        for key in ("kind", "confidence", "params_mm", "source", "inliers", "residual_mm"):
            assert key in fit, f"{fit['kind']} lost v1 key {key}"
        assert fit["kind"] in {"box", "cylinder", "sphere", "cone", "plane"}
        assert 0.0 <= fit["confidence"] <= 1.0
        assert isinstance(fit["face_ids"], list)

    for plane in _of_kind(out, "plane"):
        assert {"normal", "origin", "offset"} <= set(plane["params_mm"])
        assert plane["role"] in {"pocket", "pad", "unknown"}
    for cyl in _of_kind(out, "cylinder"):
        assert {"radius", "height", "axis", "origin"} <= set(cyl["params_mm"])
        assert cyl["role"] in {"hole", "boss", "shaft", "unknown"}
        assert isinstance(cyl["through"], bool)
        assert isinstance(cyl["depth_mm"], float)


def test_fits_are_reproducible(tmp_path):
    """Same mesh in, same numbers out — a scan tool that drifts run to run is unusable."""
    _require_backends()
    mesh = _build_box_with_boss()
    first = _fit(mesh, tmp_path, "determinism", n_samples=12000)
    second = _fit(mesh, tmp_path, "determinism", n_samples=12000)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

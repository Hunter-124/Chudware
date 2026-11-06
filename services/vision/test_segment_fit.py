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
from ..vision.recon_transform import ReconTransform


def _known_mm():
    transform = ReconTransform.identity()
    transform.source = "test_known_mm"
    transform.confirmed = True
    return transform


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
        recon_transform=_known_mm(),
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
    out = segment_fit.segment_fit(mesh_path=path, session_id=f"re_{name}",
                                  recon_transform=_known_mm(), **kwargs)
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
        points=pts, session_id="re_pts", plane_thresh_mm=0.2, cyl_thresh_mm=0.2,
        recon_transform=_known_mm(),
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


def test_a_vertices_only_ply_file_loads_as_a_cloud(tmp_path):
    """A scan PLY with no faces must reach the fitters. ``trimesh.load(force="mesh")``
    turns a vertices-only file into an EMPTY Trimesh instead of raising, so ``_load_mesh``
    returned None and segment_fit refused a 30k-point scan with ``mesh_unreadable`` —
    discarding the input the point-cloud path (``faces=None``) exists to serve."""
    mesh_lib = _require_backends()
    mesh = mesh_lib.creation.annulus(r_min=4.0, r_max=20.0, height=10.0, sections=96)
    pts, _ = mesh_lib.sample.sample_surface(mesh, 30000)
    path = tmp_path / "cloud_only.ply"
    with open(path, "w") as fh:
        fh.write("ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\n"
                 "property float y\nproperty float z\nend_header\n" % len(pts))
        fh.writelines(f"{a:.6f} {b:.6f} {c:.6f}\n" for a, b, c in pts)

    handle = segment_fit._load_mesh(str(path))
    assert handle is not None, "vertices-only PLY was discarded"
    assert len(handle["vertices"]) == 30000
    assert handle["faces"] is None  # a cloud has none; only sectioning needs them

    out = segment_fit.segment_fit(mesh_path=str(path), session_id="re_cloud",
                                  plane_thresh_mm=0.2, cyl_thresh_mm=0.2,
                                  recon_transform=_known_mm())
    assert out["ok"] is True, out
    cyls = _of_kind(out, "cylinder")
    assert cyls, out
    assert abs(min(f["params_mm"]["radius"] for f in cyls) - 4.0) <= 0.1
    assert all(f["role"] == "unknown" for f in cyls)  # unoriented normals: no bore/boss



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


def _rotated_metric_transform():
    a, b = 0.47, -0.38
    rz = np.array([[math.cos(a), -math.sin(a), 0],
                   [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)],
                   [0, math.sin(b), math.cos(b)]])
    return ReconTransform(2.5, (rz @ rx).tolist(), [12.0, -7.0, 3.0],
                          source="test_calibration", residual_mm=0.04, confirmed=True)


def _raw_points(metric, transform):
    return ((metric - np.asarray(transform.translation_mm)) @ np.asarray(transform.rotation)
            / transform.mm_per_unit)


def _require_point_fitter():
    pytest.importorskip("open3d")
    pytest.importorskip("scipy")


def test_unconfirmed_units_are_refused_unless_explicitly_approximate(monkeypatch):
    monkeypatch.setattr(segment_fit, "_backend_available", lambda: True)
    points = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    for transform in (None, ReconTransform.identity()):
        refused = segment_fit.segment_fit(points=points, recon_transform=transform)
        assert refused["ok"] is False
        assert refused["reason"] == "scale_unconfirmed"
        assert "reveng_set_scale" in refused["hint"]
        assert "approximate=True" in refused["hint"]
    result = segment_fit.segment_fit(points=points, approximate=True, n_sections=0,
                                     max_planes=0, max_cylinders=0,
                                     max_cones=0, max_spheres=0)
    assert result["ok"] and result["approximate"]
    assert result["units"] == "reconstruction_units"
    estimated = ReconTransform.identity()
    estimated.mm_per_unit = 3.0
    estimated.source = "unconfirmed_estimate"
    exploratory = segment_fit.segment_fit(
        points=points, recon_transform=estimated.to_json(), approximate=True, n_sections=0,
        max_planes=0, max_cylinders=0, max_cones=0, max_spheres=0)
    assert exploratory["ok"] and exploratory["approximate"] and exploratory["units"] == "mm"
    assert exploratory["recon_transform"]["source"] == "unconfirmed_estimate"
    surface_approx = segment_fit.segment_fit(
        points=points, recon_transform=_known_mm(), approximate=True, n_sections=0,
        max_planes=0, max_cylinders=0, max_cones=0, max_spheres=0)
    assert surface_approx["ok"] and surface_approx["approximate"]
    bad = segment_fit.segment_fit(points=points, approximate=True, transform_applied=True)
    assert bad["reason"] == "invalid_recon_transform"


def test_metric_transform_recovers_cylinder_from_unoriented_points():
    _require_point_fitter()
    angles = np.linspace(0, 2 * math.pi, 48, endpoint=False)
    heights = np.linspace(-8.0, 8.0, 30)
    theta, z = np.meshgrid(angles, heights)
    metric = np.column_stack(((10 + 5 * np.cos(theta)).ravel(),
                              (2 + 5 * np.sin(theta)).ravel(), z.ravel()))
    transform = _rotated_metric_transform()
    out = segment_fit.segment_fit(
        points=_raw_points(metric, transform), recon_transform=transform,
        max_planes=0, max_cylinders=1, max_cones=0, max_spheres=0,
        cyl_thresh_mm=0.3, n_sections=0,
    )
    assert out["ok"] and out["units"] == "mm" and not out["approximate"]
    cyl = max(_of_kind(out, "cylinder"), key=lambda fit: fit["inliers"])
    assert cyl["params_mm"]["radius"] == pytest.approx(5.0, abs=0.2)
    assert cyl["params_mm"]["height"] == pytest.approx(16.0, abs=0.4)
    assert abs(cyl["params_mm"]["axis"][2]) > 0.99
    assert cyl["role"] == "unknown"  # point-cloud normals have no reliable sign
    assert cyl["residual_mm"] < 0.3
    assert cyl["uncertainty"]["calibration_residual_mm"] == 0.04
    assert cyl["uncertainty"]["inlier_standard_error"] >= 0
    assert "estimated_unoriented" in {note["reason"] for note in out["notes"]}


def test_metric_transform_recovers_box_plane_dimensions():
    _require_point_fitter()
    dims = np.array([40.0, 24.0, 12.0])
    faces = []
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        u, v = np.meshgrid(np.linspace(-dims[other[0]] / 2, dims[other[0]] / 2, 22),
                           np.linspace(-dims[other[1]] / 2, dims[other[1]] / 2, 22))
        for side in (-1, 1):
            face = np.zeros((u.size, 3))
            face[:, axis] = side * dims[axis] / 2
            face[:, other[0]], face[:, other[1]] = u.ravel(), v.ravel()
            faces.append(face)
    metric = np.concatenate(faces)
    transform = _rotated_metric_transform()
    out = segment_fit.segment_fit(
        points=_raw_points(metric, transform), recon_transform=transform.to_json(),
        max_planes=6, max_cylinders=0, max_cones=0, max_spheres=0,
        plane_thresh_mm=0.05, n_sections=0,
    )
    assert out["ok"], out
    planes = _of_kind(out, "plane")
    for axis, dimension in enumerate(dims):
        stations = [fit["params_mm"]["origin"][axis] for fit in planes
                    if abs(fit["params_mm"]["normal"][axis]) > 0.99]
        assert len(stations) >= 2
        assert max(stations) - min(stations) == pytest.approx(dimension, abs=0.1)
    assert all(fit["residual_mm"] < 0.05 and "uncertainty" in fit for fit in planes)


def test_transformed_mesh_sections_and_already_applied_flag(tmp_path):
    _require_point_fitter()
    trimesh = pytest.importorskip("trimesh")
    raw = trimesh.creation.box(extents=[2.0, 4.0, 6.0])
    path = tmp_path / "raw_box.ply"
    raw.export(path)
    rotation = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
    transform = ReconTransform(3.0, rotation, [10.0, -4.0, 1.0],
                               source="test_calibration", confirmed=True)
    kwargs = dict(max_planes=0, max_cylinders=0, max_cones=0, max_spheres=0,
                  n_sections=3, section_axis="z", recon_transform=transform)
    out = segment_fit.segment_fit(mesh_path=str(path), **kwargs)
    assert out["ok"] and len(out["sections"]) == 3
    assert all(s["area_mm2"] == pytest.approx(72.0, abs=1e-4) for s in out["sections"])

    matrix = np.eye(4)
    matrix[:3, :3] = 3.0 * np.asarray(rotation)
    matrix[:3, 3] = transform.translation_mm
    raw.apply_transform(matrix)
    applied_path = tmp_path / "metric_box.ply"
    raw.export(applied_path)
    applied = segment_fit.segment_fit(mesh_path=str(applied_path),
                                      transform_applied=True, **kwargs)
    assert applied["ok"] and applied["transform_applied_to_input"]
    assert [s["area_mm2"] for s in applied["sections"]] == pytest.approx(
        [s["area_mm2"] for s in out["sections"]], abs=1e-4)


def test_open3d_only_point_file_estimates_unoriented_normals(tmp_path, monkeypatch):
    pytest.importorskip("open3d")
    monkeypatch.setattr(segment_fit, "HAVE_TRIMESH", False)
    monkeypatch.setattr(segment_fit, "HAVE_SCIPY", False)
    points = np.array([[x, y, 0.0] for x in range(6) for y in range(6)], dtype=float)
    path = tmp_path / "points.ply"
    with path.open("w", encoding="ascii") as file:
        file.write(f"ply\nformat ascii 1.0\nelement vertex {len(points)}\n"
                   "property float x\nproperty float y\nproperty float z\nend_header\n")
        for x, y, z in points:
            file.write(f"{x} {y} {z}\n")
    out = segment_fit.segment_fit(mesh_path=str(path), recon_transform=_known_mm(),
                                  max_planes=0, max_cylinders=0, max_cones=0,
                                  max_spheres=0, n_sections=1)
    assert out["ok"], out
    notes = {(note["stage"], note["reason"]) for note in out["notes"]}
    assert ("normals", "estimated_unoriented") in notes
    assert ("sections", "no_faces") in notes

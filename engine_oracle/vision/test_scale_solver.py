"""Tests for the RE scale mat + solver (RE-05).

Two layers, matching the module split:

* **math core** (always-run, only NumPy): the meyerls corner-edge arithmetic and the DLT
  triangulator on hand-built geometry — proves the numbers without any images.
* **image path** (``importorskip("cv2")``): a *synthetic ArUco set* — a marker of known physical
  size placed in a known 3D frame, projected through several synthetic pinhole cameras into real
  PNGs, with a matching COLMAP text sparse model. ``solve_scale_aruco`` must recover ``mm_per_unit``
  to within 1 % (the task acceptance).

The scale-mat PDF generator is smoke-tested for a well-formed, correctly-sized PDF.
"""
from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")

from engine.vision.scale_solver import (  # noqa: E402
    ColmapCamera,
    ScaleResult,
    scale_from_corner_edges,
    solve_scale_aruco,
    solve_scale_two_point,
    triangulate_point,
)


# --------------------------------------------------------------------------- math core (no cv2)


def test_scale_from_corner_edges_exact_square():
    # A 2-unit square marker known to be 40 mm on a side → 20 mm per unit, zero residual.
    corners = np.array([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], dtype="float64")
    res = scale_from_corner_edges(corners, marker_len_mm=40.0)
    assert isinstance(res, ScaleResult)
    assert res.method == "aruco_mat"
    assert res.mm_per_unit == pytest.approx(20.0, rel=1e-9)
    assert res.residual_pct == pytest.approx(0.0, abs=1e-9)


def test_scale_from_corner_edges_rejects_bad_shape():
    with pytest.raises(ValueError):
        scale_from_corner_edges(np.zeros((3, 3)), marker_len_mm=10.0)
    with pytest.raises(ValueError):
        scale_from_corner_edges(np.zeros((4, 3)), marker_len_mm=10.0)  # zero edges


def _synthetic_camera(name, K, R, t, w, h):
    return ColmapCamera(image_name=name, K=K, R=R, t=t, width=w, height=h)


def _look_at(cam_pos, target, up=(0, 0, 1)):
    """Return (R, t) world→camera for a camera at ``cam_pos`` looking at ``target``."""
    cam_pos = np.asarray(cam_pos, dtype="float64")
    target = np.asarray(target, dtype="float64")
    f = target - cam_pos
    f = f / np.linalg.norm(f)                       # +Z (camera forward)
    up = np.asarray(up, dtype="float64")
    if abs(float(np.dot(f, up))) > 0.99:            # forward ~parallel to up → pick another up
        up = np.array([0.0, 1.0, 0.0])
    r = np.cross(up, f)
    r = r / np.linalg.norm(r)                       # +X (camera right)
    d = np.cross(f, r)                              # +Y (camera down); (r, d, f) is right-handed
    R = np.vstack([r, d, f])                        # world→camera rows, det = +1
    t = -R @ cam_pos
    return R, t


def test_triangulate_point_recovers_known_3d():
    K = np.array([[1200, 0, 640], [0, 1200, 480], [0, 0, 1]], dtype="float64")
    X = np.array([0.3, -0.2, 0.0], dtype="float64")
    cams, pxs = [], []
    for pos in ([2, 0, 3], [-2, 1, 3], [0, -2, 3]):
        R, t = _look_at(pos, [0, 0, 0])
        cam = _synthetic_camera("v", K, R, t, 1280, 960)
        P = K @ np.hstack([R, t.reshape(3, 1)])
        proj = P @ np.append(X, 1.0)
        cams.append(cam)
        pxs.append(proj[:2] / proj[2])
    Xr = triangulate_point(cams, pxs)
    assert np.allclose(Xr, X, atol=1e-6)


# --------------------------------------------------------------------------- synthetic ArUco set

cv2 = pytest.importorskip("cv2")

from engine.vision.scale_mat import MarkerSpec, generate_scale_mat_pdf  # noqa: E402


# Ground truth: a marker whose edge is 2.0 reconstruction units, physically 50 mm.
# So the true scale is 25 mm/unit; the solver must land within 1 %.
_TRUE_MM_PER_UNIT = 25.0
_MARKER_UNITS = 2.0
_MARKER_MM = _TRUE_MM_PER_UNIT * _MARKER_UNITS      # 50.0
_DICT = "DICT_5X5_100"
_MARKER_ID = 7


def _marker_corners_world():
    """Marker centred at origin on the z=0 plane, ordered TL,TR,BR,BL (OpenCV image convention).

    In image space OpenCV returns corners clockwise starting top-left; we lay the marker so that,
    viewed from +Z looking toward -Z, its corners project in that order.
    """
    h = _MARKER_UNITS / 2.0
    # Cameras sit on the +Z side looking toward -Z; image +y points down. So world -Y must map to
    # image top for the marker to project un-mirrored (ArUco rejects a mirrored marker). Lay TL at
    # world (-h, -h): TL,TR,BR,BL then reads as image top-left,tr,br,bl.
    return np.array([
        [-h,  h, 0.0],   # TL
        [ h,  h, 0.0],   # TR
        [ h, -h, 0.0],   # BR
        [-h, -h, 0.0],   # BL
    ], dtype="float64")


def _write_synthetic_set(tmp_path):
    """Render the marker into several posed PNGs + a COLMAP text sparse model. Returns dirs."""
    images_dir = tmp_path / "images"
    sparse_dir = tmp_path / "sparse"
    images_dir.mkdir()
    sparse_dir.mkdir()

    W, H = 1280, 960
    fx = fy = 1400.0
    cx, cy = W / 2.0, H / 2.0
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype="float64")

    # A crisp marker raster we warp into each view via homography (planar marker → planar image).
    # ArUco needs a white quiet zone, so pad the marker; src corners reference the INNER marker
    # edge (matching the world corners) — the pad projects outside those corners as background.
    tile = 600
    pad = tile // 4
    core = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, _DICT)), _MARKER_ID, tile, borderBits=1)
    marker_img = np.full((tile + 2 * pad, tile + 2 * pad), 255, dtype="uint8")
    marker_img[pad:pad + tile, pad:pad + tile] = core
    big = marker_img.shape[0]
    # Source corners = the FULL padded image (so the white quiet zone warps in with the marker,
    # which ArUco needs). ``pad_frac`` is how far the quiet zone extends past the marker edge, in
    # marker-length units — we build matching padded WORLD corners so the projection stays exact.
    src = np.array([[0, 0], [big - 1, 0], [big - 1, big - 1], [0, big - 1]], dtype="float32")
    pad_frac = pad / tile

    corners_w = _marker_corners_world()
    # Padded world corners: push each marker corner outward by pad_frac * marker_len in x/y.
    ext = pad_frac * _MARKER_UNITS
    corners_pad_w = np.array([
        [corners_w[0][0] - ext, corners_w[0][1] + ext, 0.0],   # TL
        [corners_w[1][0] + ext, corners_w[1][1] + ext, 0.0],   # TR
        [corners_w[2][0] + ext, corners_w[2][1] - ext, 0.0],   # BR
        [corners_w[3][0] - ext, corners_w[3][1] - ext, 0.0],   # BL
    ], dtype="float64")
    views = [
        ([0.2, 0.1, 6.0]),
        ([2.2, -0.4, 5.5]),
        ([-1.9, 0.8, 5.7]),
        ([0.6, 2.1, 5.6]),
    ]
    cameras_txt = ["# cameras", f"1 PINHOLE {W} {H} {fx} {fy} {cx} {cy}"]
    images_txt = ["# images"]
    for idx, pos in enumerate(views):
        R, t = _look_at(pos, [0, 0, 0])
        P = K @ np.hstack([R, t.reshape(3, 1)])
        # Project the PADDED world corners → dst (so the warped image includes the quiet zone).
        proj = (P @ np.hstack([corners_pad_w, np.ones((4, 1))]).T).T
        dst = (proj[:, :2] / proj[:, 2:3]).astype("float32")
        Hmat = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(marker_img, Hmat, (W, H),
                                     flags=cv2.INTER_LINEAR, borderValue=255)
        name = f"img{idx:02d}.png"
        cv2.imwrite(str(images_dir / name), warped)

        # COLMAP images.txt pose line: QW QX QY QZ TX TY TZ CAMERA_ID NAME, then a POINTS2D line.
        qw, qx, qy, qz = _rot_to_quat(R)
        images_txt.append(f"{idx + 1} {qw} {qx} {qy} {qz} {t[0]} {t[1]} {t[2]} 1 {name}")
        images_txt.append("")  # empty POINTS2D line

    (sparse_dir / "cameras.txt").write_text("\n".join(cameras_txt) + "\n")
    (sparse_dir / "images.txt").write_text("\n".join(images_txt) + "\n")
    return str(sparse_dir), str(images_dir)


def _rot_to_quat(R):
    """3×3 rotation → COLMAP (w, x, y, z) quaternion."""
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s
    return qw, qx, qy, qz


def test_solve_scale_aruco_synthetic_within_1pct(tmp_path):
    sparse_dir, images_dir = _write_synthetic_set(tmp_path)
    res = solve_scale_aruco(
        sparse_dir=sparse_dir, images_dir=images_dir,
        marker_len_mm=_MARKER_MM, dict_name=_DICT, marker_id=_MARKER_ID)
    assert res.method == "aruco_mat"
    assert res.n_views >= 2
    rel_err = abs(res.mm_per_unit - _TRUE_MM_PER_UNIT) / _TRUE_MM_PER_UNIT
    assert rel_err < 0.01, f"recovered {res.mm_per_unit} vs {_TRUE_MM_PER_UNIT} ({rel_err*100:.3f}%)"


def test_solve_scale_two_point_manual(tmp_path):
    # Two known sparse points 4 units apart, physically 100 mm → 25 mm/unit.
    K = np.array([[1400, 0, 640], [0, 1400, 480], [0, 0, 1]], dtype="float64")
    R, t = _look_at([0, 0, 6], [0, 0, 0])
    cam = ColmapCamera("v0.png", K, R, t, 1280, 960)
    world = np.array([[-2, 0, 0], [2, 0, 0], [0, 1, 0.5]], dtype="float64")
    # Project the two endpoints to get clicks.
    P = K @ np.hstack([R, t.reshape(3, 1)])
    clicks = []
    for w in world[:2]:
        pr = P @ np.append(w, 1.0)
        clicks.append(pr[:2] / pr[2])
    res = solve_scale_two_point(cameras=[cam], points3d=world,
                                p1=clicks[0], p2=clicks[1], distance_mm=100.0)
    assert res.method == "two_point"
    assert res.mm_per_unit == pytest.approx(25.0, rel=1e-6)


def test_scale_mat_pdf_is_wellformed(tmp_path):
    out = tmp_path / "mat.pdf"
    info = generate_scale_mat_pdf(MarkerSpec(dict_name=_DICT, marker_id=3, marker_len_mm=40.0),
                                  out_path=str(out), size="A4", dpi=200)
    assert out.is_file()
    head = out.read_bytes()[:5]
    assert head == b"%PDF-", "output is not a PDF"
    assert info["marker_len_mm"] == 40.0
    assert info["size"] == "A4"

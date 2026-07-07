"""Synthetic bracket fixture generator for Reverse Engineering tests (RE-09, RE-13, RE-24).

Creates a known L-bracket CAD part with two through-holes and renders N calibrated **perspective**
views from known pinhole camera poses, each with a **real** ``cv2.aruco`` marker of known physical
size composited flat on the ``z=0`` scene plane.  The marker is projected through the *same* camera
matrix used to record the pose, so the fixture is both **photogrammetry-reconstructable** (realistic
multi-view overlap) and **scale-solvable** by RE-05's ``solve_scale_aruco`` (a COLMAP-style text
sparse model can be synthesised directly from the recorded intrinsics + poses — see
``services/mcp/test_reveng_e2e_scan.py``).

Writes:

  corpora/reveng/bracket_fixture/
    render_000.png .. render_NNN.png   — synthetic perspective views (800x600 px)
    aruco_mat.png                      — the standalone real ArUco marker (with quiet zone)
    ground_truth.json                  — see OUTPUT FORMAT below
    camera_poses.json                  — see OUTPUT FORMAT below

## Output format (schema stable — RE-09/RE-13/RE-24 consume it)

### ground_truth.json  (schema aicad.reveng_fixture/1)
Frozen dimensional keys (``overall``, ``wall_thickness``, ``holes``, ``hole_spacing``,
``hole_edge_dist``) plus ``render_count`` / ``render_files`` / ``aruco_mat`` /
``camera_poses_file``.  RE-24 additionally reads the ``marker`` block:

```json
"marker": {
  "dict_name": "DICT_5X5_100", "marker_id": 0, "marker_len_mm": 40.0,
  "center_xy_mm": [40.0, -35.0], "plane_z_mm": 0.0,
  "world_corners_mm": [[TL],[TR],[BR],[BL]]
}
```

### camera_poses.json
Array of per-render camera descriptors.  The **original** keys are preserved for RE-20's test
(``index, view, eye_xyz, target_xyz, up_xyz, fov_deg, projection, image_width_px,
image_height_px, scale_px_per_mm, render_file``).  RE-24 additionally reads a COLMAP-ready
block per pose so a sparse model can be synthesised without re-deriving conventions:

```json
"projection": "pinhole",
"intrinsics": {"fx": .., "fy": .., "cx": .., "cy": ..},
"R_world_to_cam": [[..],[..],[..]],     # OpenCV convention (x right, y down, z fwd)
"t_world_to_cam": [..],
"quaternion_wxyz": [w, x, y, z],
"marker_pixels": [[TL],[TR],[BR],[BL]]  # projected marker corners, or null if off-frame
```

## Usage
    python3 tools/reveng/gen_bracket_fixture.py [--out DIR] [--views N]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Ground-truth geometry (frozen — consumers RE-09/13/24 depend on these values)
# ---------------------------------------------------------------------------

BRACKET = {
    "part": "bracket",
    "units": "mm",
    "overall": {"length": 80.0, "width": 50.0, "height": 30.0},
    "wall_thickness": 5.0,
    "holes": [
        {"id": "H1", "diameter": 8.0, "center_x": 20.0, "center_y": 15.0,
         "depth": 30.0, "face": "top"},
        {"id": "H2", "diameter": 8.0, "center_x": 60.0, "center_y": 15.0,
         "depth": 30.0, "face": "top"},
    ],
    "hole_spacing": 40.0,   # H2.cx - H1.cx
    "hole_edge_dist": 12.0,  # nearest hole-center to bracket edge (min)
}

IMG_W, IMG_H = 800, 600
FOV_DEG = 45.0
# Nominal scale retained for schema compatibility (RE-20 test reads this key).
# The renders are now perspective; this is a representative px/mm at the target plane.
SCALE_PX_PER_MM = 8.0

# --- Real ArUco marker on the z=0 scene plane -------------------------------
# A single crisp marker of known physical edge length, placed beside the bracket
# footprint (bracket spans x∈[0,80], y∈[0,50]); the marker sits in y<0 so it does
# not overlap the part.  RE-05's solve_scale_aruco keys off exactly one marker id.
MARKER = {
    "dict_name": "DICT_5X5_100",
    "marker_id": 0,
    "marker_len_mm": 40.0,
    "center_xy_mm": [40.0, -35.0],
    "plane_z_mm": 0.0,
}

# Camera views: perspective pinhole cameras looking at the scene.  Poses are chosen
# so the flat z=0 marker is visible (never edge-on) in >=2 views for triangulation.
_VIEWS: list[dict[str, Any]] = [
    {"view": "top",     "eye": (40, 25, 220),    "target": (40, 10, 0),  "up": (0, 1, 0)},
    {"view": "iso_ne",  "eye": (150, -120, 180), "target": (40, 0, 10),  "up": (0, 0, 1)},
    {"view": "iso_nw",  "eye": (-70, -120, 180), "target": (40, 0, 10),  "up": (0, 0, 1)},
    {"view": "iso_se",  "eye": (150, 120, 180),  "target": (40, 10, 10), "up": (0, 0, 1)},
    {"view": "iso_sw",  "eye": (-70, 120, 180),  "target": (40, 10, 10), "up": (0, 0, 1)},
    {"view": "high_n",  "eye": (40, -100, 220),  "target": (40, 0, 0),   "up": (0, 0, 1)},
]


def _marker_world_corners():
    """Return the 4 marker corners (TL,TR,BR,BL) in world mm on the z=0 plane."""
    import numpy as np  # type: ignore
    cx, cy = MARKER["center_xy_mm"]
    z = MARKER["plane_z_mm"]
    h = MARKER["marker_len_mm"] / 2.0
    return np.array([
        [cx - h, cy + h, z],  # TL
        [cx + h, cy + h, z],  # TR
        [cx + h, cy - h, z],  # BR
        [cx - h, cy - h, z],  # BL
    ], dtype="float64")


# ---------------------------------------------------------------------------
# Camera math (pinhole, OpenCV convention: x right, y down, z forward)
# ---------------------------------------------------------------------------

def _intrinsics():
    import numpy as np  # type: ignore
    fy = (IMG_H / 2.0) / math.tan(math.radians(FOV_DEG) / 2.0)
    fx = fy
    cx, cy = IMG_W / 2.0, IMG_H / 2.0
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype="float64")


def _look_at_Rt(eye, target, up):
    """World→camera R (3x3) and t (3,) for an OpenCV pinhole camera."""
    import numpy as np  # type: ignore
    eye = np.array(eye, dtype="float64")
    target = np.array(target, dtype="float64")
    up = np.array(up, dtype="float64")
    f = target - eye
    f /= np.linalg.norm(f)                 # forward = camera +z
    r = np.cross(f, up)
    r /= np.linalg.norm(r)                 # right = camera +x
    y = np.cross(f, r)
    y /= np.linalg.norm(y)                 # down = camera +y
    R = np.stack([r, y, f], axis=0)        # rows map world→camera axes
    t = -R @ eye
    return R, t


def _rot_to_quat(R):
    """3x3 rotation → COLMAP (w, x, y, z) quaternion."""
    tr = float(R[0, 0] + R[1, 1] + R[2, 2])
    if tr > 0:
        S = math.sqrt(tr + 1.0) * 2
        w = 0.25 * S
        x = (R[2, 1] - R[1, 2]) / S
        y = (R[0, 2] - R[2, 0]) / S
        z = (R[1, 0] - R[0, 1]) / S
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        S = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / S
        x = 0.25 * S
        y = (R[0, 1] + R[1, 0]) / S
        z = (R[0, 2] + R[2, 0]) / S
    elif R[1, 1] > R[2, 2]:
        S = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / S
        x = (R[0, 1] + R[1, 0]) / S
        y = 0.25 * S
        z = (R[1, 2] + R[2, 1]) / S
    else:
        S = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / S
        x = (R[0, 2] + R[2, 0]) / S
        y = (R[1, 2] + R[2, 1]) / S
        z = 0.25 * S
    return [float(w), float(x), float(y), float(z)]


def _project(K, R, t, pts):
    """Project world points → pixel coords. Returns list of (u,v) or None if behind camera."""
    import numpy as np  # type: ignore
    pts = np.asarray(pts, dtype="float64")
    cam = (R @ pts.T + t.reshape(3, 1)).T
    out = []
    for c in cam:
        if c[2] <= 1e-6:
            out.append(None)
            continue
        p = K @ (c / c[2])
        out.append([float(p[0]), float(p[1])])
    return out


# ---------------------------------------------------------------------------
# OpenCV availability
# ---------------------------------------------------------------------------

def _require_cv2():
    try:
        import cv2  # type: ignore
        return cv2
    except ImportError as e:  # pragma: no cover
        sys.exit(f"OpenCV is required: pip install opencv-python-headless  ({e})")


def _make_marker_image(cv2, size_px: int = 400, quiet_zone: int = 60):
    """Render the real ArUco marker with a white quiet zone (needed for detection).

    Returns (padded_gray_image, inner_corner_px) where inner_corner_px are the 4
    marker-square corners (TL,TR,BR,BL) inside the padded image.
    """
    import numpy as np  # type: ignore
    const = getattr(cv2.aruco, MARKER["dict_name"])
    dictionary = cv2.aruco.getPredefinedDictionary(const)
    marker = cv2.aruco.generateImageMarker(dictionary, MARKER["marker_id"], size_px)
    qz = quiet_zone
    padded = np.full((size_px + 2 * qz, size_px + 2 * qz), 255, np.uint8)
    padded[qz:qz + size_px, qz:qz + size_px] = marker
    inner = np.array([
        [qz, qz],
        [qz + size_px - 1, qz],
        [qz + size_px - 1, qz + size_px - 1],
        [qz, qz + size_px - 1],
    ], dtype="float32")
    return padded, inner


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BG = 245          # light grey background (keeps ArUco quiet-zone contrast)
_FILL = (176, 114, 76)   # BGR bracket fill (matches old #4C72B0)
_EDGE = (92, 47, 26)     # BGR bracket edge


def _draw_bracket(cv2, canvas, K, R, t):
    """Draw a filled bracket silhouette + hole markers by projecting key vertices."""
    import numpy as np  # type: ignore
    W = BRACKET["overall"]["length"]
    D = BRACKET["overall"]["width"]
    H = BRACKET["overall"]["height"]

    def proj(pt):
        p = _project(K, R, t, [pt])[0]
        return None if p is None else (int(round(p[0])), int(round(p[1])))

    top = [proj(p) for p in [(0, 0, H), (W, 0, H), (W, D, H), (0, D, H)]]
    bot = [proj(p) for p in [(0, 0, 0), (W, 0, 0), (W, D, 0), (0, D, 0)]]
    if any(p is None for p in top + bot):
        return

    # top face + side edges
    cv2.fillPoly(canvas, [np.array(top, np.int32)], _FILL)
    cv2.polylines(canvas, [np.array(top, np.int32)], True, _EDGE, 2)
    for a, b in zip(top, bot):
        cv2.line(canvas, a, b, _EDGE, 2)
    # bottom outline (visible on iso views)
    cv2.polylines(canvas, [np.array(bot, np.int32)], True, _EDGE, 1)

    # holes on top face
    for hole in BRACKET["holes"]:
        cx, cy = hole["center_x"], hole["center_y"]
        c = proj((cx, cy, H))
        e = proj((cx + hole["diameter"] / 2.0, cy, H))
        if c is None or e is None:
            continue
        r_px = int(round(math.hypot(e[0] - c[0], e[1] - c[1])))
        cv2.circle(canvas, c, max(2, r_px), (46, 26, 26), -1)
        cv2.circle(canvas, c, max(2, r_px), _EDGE, 1)


def _composite_marker(cv2, canvas, marker_padded, inner_px, marker_dst_px):
    """Warp the padded ArUco marker so its inner corners land on marker_dst_px."""
    import numpy as np  # type: ignore
    dst = np.array(marker_dst_px, dtype="float32")
    Hh = cv2.getPerspectiveTransform(inner_px, dst)
    warped = cv2.warpPerspective(marker_padded, Hh, (IMG_W, IMG_H), borderValue=_BG)
    mask = cv2.warpPerspective(np.full_like(marker_padded, 255), Hh, (IMG_W, IMG_H),
                               borderValue=0)
    warped_bgr = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
    canvas[mask > 0] = warped_bgr[mask > 0]


def _make_render(cv2, view, K, marker_padded, inner_px, marker_world):
    """Render one perspective view. Returns (bgr_image, marker_pixels_or_none, R, t)."""
    import numpy as np  # type: ignore
    R, t = _look_at_Rt(view["eye"], view["target"], view["up"])
    canvas = np.full((IMG_H, IMG_W, 3), _BG, np.uint8)

    # marker first (flat on ground), then bracket on top for realistic occlusion order
    mpx = _project(K, R, t, marker_world)
    marker_pixels = None
    if all(p is not None for p in mpx) and all(
        -IMG_W <= p[0] <= 2 * IMG_W and -IMG_H <= p[1] <= 2 * IMG_H for p in mpx
    ):
        _composite_marker(cv2, canvas, marker_padded, inner_px, mpx)
        marker_pixels = mpx

    _draw_bracket(cv2, canvas, K, R, t)
    cv2.putText(canvas, f"view: {view['view']}", (IMG_W - 200, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1, cv2.LINE_AA)
    return canvas, marker_pixels, R, t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_fixture(out_dir: Path, n_views: Optional[int] = None) -> dict:
    """Generate the full fixture into *out_dir*.

    Returns the ground_truth dict (also written to out_dir/ground_truth.json).
    """
    cv2 = _require_cv2()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    views = _VIEWS[:n_views] if n_views else _VIEWS
    K = _intrinsics()
    marker_world = _marker_world_corners()
    marker_padded, inner_px = _make_marker_image(cv2)

    # 1. Standalone marker image (real ArUco, detectable)
    cv2.imwrite(str(out_dir / "aruco_mat.png"), marker_padded)

    # 2. Renders + per-pose COLMAP-ready descriptors
    render_files: list[str] = []
    poses: list[dict] = []
    for i, v in enumerate(views):
        img, marker_pixels, R, t = _make_render(cv2, v, K, marker_padded, inner_px, marker_world)
        fname = f"render_{i:03d}.png"
        cv2.imwrite(str(out_dir / fname), img)
        render_files.append(fname)
        poses.append({
            "index": i,
            "view": v["view"],
            "eye_xyz": list(v["eye"]),
            "target_xyz": list(v["target"]),
            "up_xyz": list(v["up"]),
            "fov_deg": FOV_DEG,
            "projection": "pinhole",
            "image_width_px": IMG_W,
            "image_height_px": IMG_H,
            "scale_px_per_mm": SCALE_PX_PER_MM,
            "render_file": fname,
            "intrinsics": {
                "fx": float(K[0, 0]), "fy": float(K[1, 1]),
                "cx": float(K[0, 2]), "cy": float(K[1, 2]),
            },
            "R_world_to_cam": R.tolist(),
            "t_world_to_cam": t.tolist(),
            "quaternion_wxyz": _rot_to_quat(R),
            "marker_pixels": marker_pixels,
        })

    (out_dir / "camera_poses.json").write_text(json.dumps(poses, indent=2))

    # 3. Ground truth
    gt = {
        "schema": "aicad.reveng_fixture/1",
        **BRACKET,
        "render_count": len(render_files),
        "render_files": render_files,
        "aruco_mat": "aruco_mat.png",
        "camera_poses_file": "camera_poses.json",
        "marker": {
            **MARKER,
            "world_corners_mm": marker_world.tolist(),
        },
    }
    (out_dir / "ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="corpora/reveng/bracket_fixture",
                    help="Output directory (default: corpora/reveng/bracket_fixture)")
    ap.add_argument("--views", type=int, default=None,
                    help="Number of views to render (default: all)")
    args = ap.parse_args()

    gt = build_fixture(Path(args.out), n_views=args.views)
    print(f"Fixture written to {args.out}/")
    print(f"  renders : {gt['render_count']}  ({', '.join(gt['render_files'])})")
    print(f"  marker  : {gt['marker']['dict_name']} id={gt['marker']['marker_id']} "
          f"{gt['marker']['marker_len_mm']} mm")
    print(f"  holes   : {len(gt['holes'])}  (Ø{gt['holes'][0]['diameter']} mm, "
          f"spacing {gt['hole_spacing']} mm)")
    print(f"  overall : {gt['overall']['length']}×{gt['overall']['width']}×"
          f"{gt['overall']['height']} mm")


if __name__ == "__main__":
    _main()

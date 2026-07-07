"""
Synthetic bracket fixture generator for Reverse Engineering tests (RE-09, RE-13, RE-24).

Creates a known L-bracket CAD part with two through-holes, renders N calibrated views from
known camera poses with an ArUco-style scale mat composited into the scene, and writes:

  corpora/reveng/bracket_fixture/
    render_000.png .. render_NNN.png   — synthetic orthographic views (800x600 px)
    aruco_mat.png                      — ChArUco-style 5x7 grid scale mat (A4 @ 150 dpi)
    ground_truth.json                  — see OUTPUT FORMAT below
    camera_poses.json                  — see OUTPUT FORMAT below

## Output format

### ground_truth.json
```json
{
  "schema": "aicad.reveng_fixture/1",
  "part": "bracket",
  "units": "mm",
  "overall": {"length": 80.0, "width": 50.0, "height": 30.0},
  "wall_thickness": 5.0,
  "holes": [
    {"id": "H1", "diameter": 8.0,  "center_x": 20.0, "center_y": 15.0, "depth": 30.0, "face": "top"},
    {"id": "H2", "diameter": 8.0,  "center_x": 60.0, "center_y": 15.0, "depth": 30.0, "face": "top"}
  ],
  "hole_spacing": 40.0,
  "hole_edge_dist": 12.0,
  "render_count": 6,
  "aruco_mat": "aruco_mat.png",
  "camera_poses_file": "camera_poses.json"
}
```

### camera_poses.json
Array of per-render camera descriptors:
```json
[
  {
    "index": 0,
    "view": "top",
    "eye_xyz": [40.0, 25.0, 200.0],
    "target_xyz": [40.0, 25.0, 0.0],
    "up_xyz": [0.0, 1.0, 0.0],
    "fov_deg": 45.0,
    "image_width_px": 800,
    "image_height_px": 600,
    "scale_px_per_mm": 8.0
  },
  ...
]
```

## Scale mat
A 5-column × 7-row ChArUco-style checkerboard (30 mm squares) composited into the bottom-left
corner of every render.  The mat is also saved standalone as aruco_mat.png.

## Usage
    python3 tools/reveng/gen_bracket_fixture.py [--out DIR] [--views N]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

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

# Camera views: (name, eye_xyz, target, up, scale_px_per_mm)
# All views use orthographic projection (scale_px_per_mm is constant across the image).
# eye_xyz is given relative to bracket origin at (0,0,0), bracket spans
# x∈[0,80], y∈[0,50], z∈[0,30].
_VIEWS: list[dict[str, Any]] = [
    {"view": "top",    "eye": (40, 25, 200), "target": (40, 25, 0),  "up": (0, 1, 0)},
    {"view": "front",  "eye": (40, -200, 15), "target": (40, 0, 15), "up": (0, 0, 1)},
    {"view": "right",  "eye": (200, 25, 15),  "target": (0, 25, 15), "up": (0, 0, 1)},
    {"view": "iso_ne", "eye": (180, -140, 160), "target": (40, 25, 15), "up": (0, 0, 1)},
    {"view": "iso_nw", "eye": (-100, -140, 160), "target": (40, 25, 15), "up": (0, 0, 1)},
    {"view": "bottom", "eye": (40, 25, -200), "target": (40, 25, 0), "up": (0, -1, 0)},
]

IMG_W, IMG_H = 800, 600
FOV_DEG = 45.0
# Ortho scale: 8 px per mm → bracket (80 mm wide) occupies 640 px, centred in 800 px view
SCALE_PX_PER_MM = 8.0

# ArUco mat spec (matches calib_sheet.BoardSpec defaults)
ARUCO_MAT = {
    "dict_name": "DICT_5X5_100",
    "squares_x": 5,
    "squares_y": 7,
    "square_len_mm": 30.0,
    "marker_len_mm": 22.0,
}
# Mat composited into renders: bottom-left corner, 150 dpi → 30 mm = 177 px
_MAT_SQUARE_PX = 18   # px per square in composited mat (small, non-intrusive)
_MAT_MARGIN_PX = 10   # offset from image corner


# ---------------------------------------------------------------------------
# Minimal PIL-based rasteriser
# ---------------------------------------------------------------------------

def _require_pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except ImportError as e:
        sys.exit(f"Pillow is required: pip install Pillow  ({e})")


def _make_aruco_mat_image(sq_px: int = 20) -> "Image":
    """Rasterise a simple ChArUco-style checkerboard (no real ArUco encoding needed for
    ground-truth fixture — the pattern is just used as a scale reference in renders)."""
    Image, ImageDraw, _ = _require_pil()
    nx, ny = ARUCO_MAT["squares_x"], ARUCO_MAT["squares_y"]
    w, h = nx * sq_px, ny * sq_px
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    for iy in range(ny):
        for ix in range(nx):
            if (ix + iy) % 2 == 0:
                draw.rectangle(
                    [ix * sq_px, iy * sq_px, (ix + 1) * sq_px - 1, (iy + 1) * sq_px - 1],
                    fill="black",
                )
    # Label the scale (mm per square) in a small caption band
    cap_h = 14
    full = Image.new("RGB", (w, h + cap_h), "white")
    full.paste(img, (0, 0))
    draw2 = ImageDraw.Draw(full)
    draw2.text((2, h + 1), f"{ARUCO_MAT['square_len_mm']:.0f} mm/sq  |  {nx}x{ny}",
               fill="black")
    return full


def _world_to_ortho(pt_mm: tuple, view: dict) -> tuple[float, float]:
    """Project a 3-D world point to 2-D pixel coords using the view's ortho matrix.

    We build a right-hand camera frame from eye/target/up, then project the point
    onto the image plane (ortho: just drop the depth).

    Returns (px_x, py_y) — may be outside [0,IMG_W)×[0,IMG_H).
    """
    import numpy as np  # type: ignore

    eye = np.array(view["eye"], dtype=float)
    tgt = np.array(view["target"], dtype=float)
    up  = np.array(view["up"], dtype=float)

    z_ax = eye - tgt;  z_ax /= np.linalg.norm(z_ax)
    x_ax = np.cross(up, z_ax); norm = np.linalg.norm(x_ax)
    if norm < 1e-9:
        x_ax = np.array([1.0, 0.0, 0.0])
    else:
        x_ax /= norm
    y_ax = np.cross(z_ax, x_ax)

    pt = np.array(pt_mm, dtype=float)
    rel = pt - tgt
    u = float(np.dot(rel, x_ax)) * SCALE_PX_PER_MM + IMG_W / 2
    v = -float(np.dot(rel, y_ax)) * SCALE_PX_PER_MM + IMG_H / 2
    return u, v


def _draw_bracket_view(draw, view: dict) -> None:
    """Draw a schematic bracket outline + holes onto a PIL ImageDraw canvas."""
    try:
        import numpy as np  # type: ignore
        _have_np = True
    except ImportError:
        _have_np = False

    W = BRACKET["overall"]["length"]
    D = BRACKET["overall"]["width"]
    H = BRACKET["overall"]["height"]
    T = BRACKET["wall_thickness"]

    # Build the bracket silhouette: an L-shape when viewed from front/right,
    # a rectangle with two hole circles from top.
    # We compute the 2-D projection for a subset of key 3-D vertices.

    def proj(pt):
        if _have_np:
            return _world_to_ortho(pt, view)
        # Fallback: simple orthographic approximations per named view
        x, y, z = pt
        name = view["view"]
        if name == "top":
            return x * SCALE_PX_PER_MM + (IMG_W - W * SCALE_PX_PER_MM) / 2, \
                   y * SCALE_PX_PER_MM + (IMG_H - D * SCALE_PX_PER_MM) / 2
        elif name == "front":
            return x * SCALE_PX_PER_MM + (IMG_W - W * SCALE_PX_PER_MM) / 2, \
                   (H - z) * SCALE_PX_PER_MM + (IMG_H - H * SCALE_PX_PER_MM) / 2
        else:
            return x * SCALE_PX_PER_MM + IMG_W / 2, (H - z) * SCALE_PX_PER_MM + IMG_H / 2

    FILL = "#4C72B0"
    EDGE = "#1a2f5c"

    # Bracket outer box corners (top face)
    top_pts = [(0, 0, H), (W, 0, H), (W, D, H), (0, D, H)]
    # Bottom face
    bot_pts = [(0, 0, 0), (W, 0, 0), (W, D, 0), (0, D, 0)]

    tp = [proj(p) for p in top_pts]
    bp = [proj(p) for p in bot_pts]

    # Draw top face
    draw.polygon(tp, fill=FILL, outline=EDGE)

    # Draw visible side edges (simplified: just connect corresponding corners)
    for i in range(4):
        draw.line([tp[i], bp[i]], fill=EDGE, width=2)

    # Vertical flange (the "L" leg) — occupies x∈[0,T], z∈[0,H], y∈[0,D]
    flange_top = [(0, 0, H), (T, 0, H), (T, D, H), (0, D, H)]
    flange_bot = [(0, 0, 0), (T, 0, 0), (T, D, 0), (0, D, 0)]
    fp = [proj(p) for p in flange_top]
    fpb = [proj(p) for p in flange_bot]
    draw.polygon(fp, fill="#2a4a80", outline=EDGE)
    for i in range(4):
        draw.line([fp[i], fpb[i]], fill=EDGE, width=2)

    # Two holes on top face
    for hole in BRACKET["holes"]:
        cx, cy = hole["center_x"], hole["center_y"]
        r = hole["diameter"] / 2
        # Draw hole as a dark circle on top face
        c_px = proj((cx, cy, H))
        # Approximate radius in pixels (for top-down this is exact; for other views, shrink)
        r_px = r * SCALE_PX_PER_MM
        draw.ellipse(
            [c_px[0] - r_px, c_px[1] - r_px, c_px[0] + r_px, c_px[1] + r_px],
            fill="#1a1a2e", outline=EDGE, width=1,
        )


def _make_render(view: dict, mat_img) -> "Image":
    Image, ImageDraw, ImageFont = _require_pil()
    img = Image.new("RGB", (IMG_W, IMG_H), "#f8f8f0")
    draw = ImageDraw.Draw(img)

    _draw_bracket_view(draw, view)

    # Composite scale mat into bottom-left corner
    sq = _MAT_SQUARE_PX
    m_resized = mat_img.resize(
        (ARUCO_MAT["squares_x"] * sq, ARUCO_MAT["squares_y"] * sq + 14),
        resample=1,  # LANCZOS
    )
    img.paste(m_resized, (_MAT_MARGIN_PX, IMG_H - m_resized.height - _MAT_MARGIN_PX))

    # Label
    draw.text((IMG_W - 150, 8), f"view: {view['view']}", fill="#333333")
    return img


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_fixture(out_dir: Path, n_views: int | None = None) -> dict:
    """Generate the full fixture into *out_dir*.

    Returns the ground_truth dict (also written to out_dir/ground_truth.json).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    views = _VIEWS[:n_views] if n_views else _VIEWS

    # 1. ArUco mat (standalone)
    mat_img = _make_aruco_mat_image(sq_px=20)
    mat_path = out_dir / "aruco_mat.png"
    mat_img.save(str(mat_path))

    # 2. Renders
    render_files = []
    for i, v in enumerate(views):
        img = _make_render(v, mat_img)
        fname = f"render_{i:03d}.png"
        img.save(str(out_dir / fname))
        render_files.append(fname)

    # 3. Camera poses JSON
    poses = []
    for i, v in enumerate(views):
        poses.append({
            "index": i,
            "view": v["view"],
            "eye_xyz": list(v["eye"]),
            "target_xyz": list(v["target"]),
            "up_xyz": list(v["up"]),
            "fov_deg": FOV_DEG,
            "projection": "orthographic",
            "image_width_px": IMG_W,
            "image_height_px": IMG_H,
            "scale_px_per_mm": SCALE_PX_PER_MM,
            "render_file": render_files[i],
        })
    cam_path = out_dir / "camera_poses.json"
    cam_path.write_text(json.dumps(poses, indent=2))

    # 4. Ground-truth JSON
    gt = {
        "schema": "aicad.reveng_fixture/1",
        **BRACKET,
        "render_count": len(render_files),
        "render_files": render_files,
        "aruco_mat": "aruco_mat.png",
        "camera_poses_file": "camera_poses.json",
    }
    gt_path = out_dir / "ground_truth.json"
    gt_path.write_text(json.dumps(gt, indent=2))

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
                    help="Number of views to render (default: all 6)")
    args = ap.parse_args()

    gt = build_fixture(Path(args.out), n_views=args.views)
    print(f"Fixture written to {args.out}/")
    print(f"  renders : {gt['render_count']}  ({', '.join(gt['render_files'])})")
    print(f"  holes   : {len(gt['holes'])}  (Ø{gt['holes'][0]['diameter']} mm, "
          f"spacing {gt['hole_spacing']} mm)")
    print(f"  overall : {gt['overall']['length']}×{gt['overall']['width']}×{gt['overall']['height']} mm")


if __name__ == "__main__":
    _main()

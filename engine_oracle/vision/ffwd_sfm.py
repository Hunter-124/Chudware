"""Feed-forward SfM fallback: VGGT-1B-Commercial (primary) / MapAnything (alt).

Triggered when classical COLMAP registration falls below ~60%.  Emits
COLMAP text-format poses (cameras.txt / images.txt / points3D.txt) into
``<session>/sparse/`` so RE-09's scale_solver reader consumes them
without modification.

Weights are download-on-demand and NOT shipped with the package.
If absent the public entry-point returns::

    {"ok": False, "reason": "backend_not_provisioned",
     "hint": "provision VGGT weights"}

Never raises — caller always gets a dict.
"""

from __future__ import annotations

import math
import os
import pathlib
import traceback
from typing import Any, Sequence

__all__ = ["run_ffwd_sfm"]

# ---------------------------------------------------------------------------
# COLMAP text writers
# ---------------------------------------------------------------------------

_CAMERAS_HEADER = (
    "# Camera list with one line of data per camera:\n"
    "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
    "# Number of cameras: {n}\n"
)

_IMAGES_HEADER = (
    "# Image list with two lines of data per image:\n"
    "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
    "#   POINTS2D[] as (X, Y, POINT3D_ID)\n"
    "# Number of images: {n}, mean observations per image: 0\n"
)

_POINTS3D_HEADER = (
    "# 3D point list with one line of data per point:\n"
    "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
    "# Number of points: 0, mean track length: 0\n"
)


def _rot_to_quat(R: Any) -> tuple[float, float, float, float]:
    """3×3 rotation matrix → COLMAP quaternion (qw, qx, qy, qz)."""
    # Shepperd's method (numerically stable)
    r00, r01, r02 = float(R[0][0]), float(R[0][1]), float(R[0][2])
    r10, r11, r12 = float(R[1][0]), float(R[1][1]), float(R[1][2])
    r20, r21, r22 = float(R[2][0]), float(R[2][1]), float(R[2][2])
    trace = r00 + r11 + r22
    if trace > 0:
        s = 0.5 / math.sqrt(trace + 1.0)
        qw = 0.25 / s
        qx = (r21 - r12) * s
        qy = (r02 - r20) * s
        qz = (r10 - r01) * s
    elif r00 > r11 and r00 > r22:
        s = 2.0 * math.sqrt(1.0 + r00 - r11 - r22)
        qw = (r21 - r12) / s
        qx = 0.25 * s
        qy = (r01 + r10) / s
        qz = (r02 + r20) / s
    elif r11 > r22:
        s = 2.0 * math.sqrt(1.0 + r11 - r00 - r22)
        qw = (r02 - r20) / s
        qx = (r01 + r10) / s
        qy = 0.25 * s
        qz = (r12 + r21) / s
    else:
        s = 2.0 * math.sqrt(1.0 + r22 - r00 - r11)
        qw = (r10 - r01) / s
        qx = (r02 + r20) / s
        qy = (r12 + r21) / s
        qz = 0.25 * s
    return qw, qx, qy, qz


def write_colmap_sparse(
    sparse_dir: str | pathlib.Path,
    image_names: Sequence[str],
    rotations: Any,          # (N, 3, 3) array-like  R  (world-to-cam)
    translations: Any,       # (N, 3)    array-like  t  (world-to-cam)
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    width: int,
    height: int,
) -> None:
    """Write COLMAP text sparse model into *sparse_dir*.

    Uses a single shared PINHOLE camera for all images.
    Emits an empty points3D.txt (poses-only output).
    """
    sparse_dir = pathlib.Path(sparse_dir)
    sparse_dir.mkdir(parents=True, exist_ok=True)

    n = len(image_names)

    # cameras.txt — one shared camera
    cameras_txt = sparse_dir / "cameras.txt"
    with cameras_txt.open("w", encoding="utf-8") as fh:
        fh.write(_CAMERAS_HEADER.format(n=1))
        fh.write(f"1 PINHOLE {width} {height} {fx:.6f} {fy:.6f} {cx:.6f} {cy:.6f}\n")

    # images.txt — one pose line + one empty POINTS2D line per image
    images_txt = sparse_dir / "images.txt"
    with images_txt.open("w", encoding="utf-8") as fh:
        fh.write(_IMAGES_HEADER.format(n=n))
        for idx, name in enumerate(image_names):
            R = rotations[idx]
            t = translations[idx]
            qw, qx, qy, qz = _rot_to_quat(R)
            tx, ty, tz = float(t[0]), float(t[1]), float(t[2])
            fh.write(
                f"{idx + 1} {qw:.9f} {qx:.9f} {qy:.9f} {qz:.9f}"
                f" {tx:.9f} {ty:.9f} {tz:.9f} 1 {name}\n"
            )
            fh.write("\n")  # empty POINTS2D line

    # points3D.txt — empty (poses only)
    points3d_txt = sparse_dir / "points3D.txt"
    with points3d_txt.open("w", encoding="utf-8") as fh:
        fh.write(_POINTS3D_HEADER)


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _vggt_available() -> bool:
    """Return True only when VGGT weights directory is reachable."""
    weights_dir = os.environ.get("VGGT_WEIGHTS_DIR", "")
    if weights_dir and pathlib.Path(weights_dir).is_dir():
        return True
    # Check common cache locations
    home = pathlib.Path.home()
    for candidate in [
        home / ".cache" / "vggt",
        home / ".cache" / "huggingface" / "hub" / "models--facebook--vggt",
        pathlib.Path("/opt/models/vggt"),
    ]:
        if candidate.exists():
            return True
    return False


def _mapanything_available() -> bool:
    """Return True only when MapAnything weights directory is reachable."""
    weights_dir = os.environ.get("MAPANYTHING_WEIGHTS_DIR", "")
    if weights_dir and pathlib.Path(weights_dir).is_dir():
        return True
    home = pathlib.Path.home()
    for candidate in [
        home / ".cache" / "mapanything",
        pathlib.Path("/opt/models/mapanything"),
    ]:
        if candidate.exists():
            return True
    return False


# ---------------------------------------------------------------------------
# VGGT inference shim
# ---------------------------------------------------------------------------

def _run_vggt(
    image_paths: list[str],
    sparse_dir: pathlib.Path,
) -> dict[str, Any]:
    """Lazy-import VGGT and run feed-forward SfM.

    VGGT outputs per-image extrinsics (R, t) and shared intrinsics.
    """
    try:
        import torch  # type: ignore
        from vggt.models.vggt import VGGT  # type: ignore
        from vggt.utils.pose_enc import pose_encoding_to_extri_intri  # type: ignore
        from vggt.utils.load_fn import load_and_preprocess_images  # type: ignore
    except ImportError as exc:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": f"VGGT import failed: {exc}; provision VGGT weights",
        }

    device = "cuda" if torch.cuda.is_available() else "cpu"
    weights_dir = os.environ.get("VGGT_WEIGHTS_DIR", str(pathlib.Path.home() / ".cache" / "vggt"))
    try:
        model = VGGT.from_pretrained(weights_dir).to(device).eval()
        images = load_and_preprocess_images(image_paths).to(device)
        with torch.no_grad():
            out = model(images)
        extrinsics, intrinsics = pose_encoding_to_extri_intri(
            out["pose_enc"], images.shape[-2:]
        )
        # extrinsics: (N, 4, 4) world-to-cam; intrinsics: (N, 3, 3) K
        Rs = extrinsics[:, :3, :3].cpu().numpy()
        ts = extrinsics[:, :3, 3].cpu().numpy()
        K = intrinsics[0].cpu().numpy()  # use first image intrinsics (shared)
        H, W = images.shape[-2:]
        write_colmap_sparse(
            sparse_dir,
            image_names=[pathlib.Path(p).name for p in image_paths],
            rotations=Rs,
            translations=ts,
            fx=float(K[0, 0]),
            fy=float(K[1, 1]),
            cx=float(K[0, 2]),
            cy=float(K[1, 2]),
            width=W,
            height=H,
        )
        return {"ok": True, "backend": "vggt", "n_images": len(image_paths)}
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "vggt_inference_failed",
            "hint": traceback.format_exc(limit=3),
        }


# ---------------------------------------------------------------------------
# MapAnything inference shim
# ---------------------------------------------------------------------------

def _run_mapanything(
    image_paths: list[str],
    sparse_dir: pathlib.Path,
) -> dict[str, Any]:
    """Lazy-import MapAnything and run feed-forward SfM."""
    try:
        import torch  # type: ignore
        import mapanything  # type: ignore  # noqa: F401
        from mapanything import MapAnythingModel, preprocess  # type: ignore
    except ImportError as exc:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": f"MapAnything import failed: {exc}; provision MapAnything weights",
        }

    device = "cuda" if torch.cuda.is_available() else "cpu"
    weights_dir = os.environ.get(
        "MAPANYTHING_WEIGHTS_DIR",
        str(pathlib.Path.home() / ".cache" / "mapanything"),
    )
    try:
        model = MapAnythingModel.from_pretrained(weights_dir).to(device).eval()
        images = preprocess(image_paths).to(device)
        with torch.no_grad():
            out = model(images)
        Rs = out["rotations"].cpu().numpy()   # (N, 3, 3)
        ts = out["translations"].cpu().numpy()  # (N, 3)
        fx = float(out.get("fx", 500.0))
        fy = float(out.get("fy", 500.0))
        H, W = images.shape[-2:]
        write_colmap_sparse(
            sparse_dir,
            image_names=[pathlib.Path(p).name for p in image_paths],
            rotations=Rs,
            translations=ts,
            fx=fx,
            fy=fy,
            cx=W / 2.0,
            cy=H / 2.0,
            width=W,
            height=H,
        )
        return {"ok": True, "backend": "mapanything", "n_images": len(image_paths)}
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "mapanything_inference_failed",
            "hint": traceback.format_exc(limit=3),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_ffwd_sfm(
    image_paths: list[str],
    session_dir: str | pathlib.Path,
) -> dict[str, Any]:
    """Run feed-forward SfM on *image_paths*, writing COLMAP text poses to
    ``<session_dir>/sparse/``.

    Returns a result dict with at least ``{"ok": bool}``.  On success adds
    ``"backend"`` and ``"n_images"``.  On missing weights returns::

        {"ok": False, "reason": "backend_not_provisioned",
         "hint": "provision VGGT weights"}

    Never raises.
    """
    try:
        if not image_paths:
            return {"ok": False, "reason": "no_images", "hint": "provide at least one image path"}

        sparse_dir = pathlib.Path(session_dir) / "sparse"

        # Primary: VGGT
        if _vggt_available():
            return _run_vggt(image_paths, sparse_dir)

        # Fallback: MapAnything
        if _mapanything_available():
            return _run_mapanything(image_paths, sparse_dir)

        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "provision VGGT weights",
        }
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "unexpected_error",
            "hint": traceback.format_exc(limit=3),
        }

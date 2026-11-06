"""In-process pycolmap sparse Structure-from-Motion pipeline.

Uses pycolmap (and GLOMAP global mapper when available) to reconstruct a
sparse COLMAP model from an image directory, emitting a standard COLMAP model
(cameras / images / points3D) into ``<out_dir>/sparse/``.

Backend contract
----------------
- pycolmap is imported lazily; if absent the function returns the degradation
  envelope ``{"ok": False, "reason": "backend_not_provisioned", "hint": "..."}``
  (same shape as photogrammetry.py).
- On success returns ``{"ok": True, "sparse_dir": str, "registered": int,
  "backend": "pycolmap"|"pycolmap+glomap"}``.
- On partial failure (reconstruction ran but zero images registered) returns
  ok=False with reason "no_images_registered".

GLOMAP
------
The optional ``glomap`` Python binding is attempted first (global mapper,
more robust on unordered captures).  Falls back to pycolmap's incremental
mapper automatically.

Synthetic-fixture note
----------------------
The bracket_fixture renders are orthographic, which real SfM doesn't handle.
For test purposes each render is treated as a pinhole camera using the image
width as focal-length proxy (FOV ~53° diagonal).  Registration counts may be
lower than real-world captures; the acceptance threshold is ≥6/8.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ModelCamera:
    id: int
    model: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class ModelImage:
    id: int
    camera_id: int
    name: str
    qvec: tuple[float, ...]  # COLMAP world-to-camera quaternion, wxyz
    tvec: tuple[float, ...]


@dataclass(frozen=True)
class ModelPoint3D:
    id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float


# COLMAP camera model ID, name and number of parameters, per cameras.bin.
_CAMERA_MODELS = {
    0: ("SIMPLE_PINHOLE", 3), 1: ("PINHOLE", 4),
    2: ("SIMPLE_RADIAL", 4), 3: ("RADIAL", 5), 4: ("OPENCV", 8),
    5: ("OPENCV_FISHEYE", 8), 6: ("FULL_OPENCV", 12), 7: ("FOV", 5),
    8: ("SIMPLE_RADIAL_FISHEYE", 4), 9: ("RADIAL_FISHEYE", 5),
    10: ("THIN_PRISM_FISHEYE", 12), 11: ("RAD_TAN_THIN_PRISM_FISHEYE", 16),
}


def _unpack(stream, fmt):
    size = struct.calcsize("<" + fmt)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"truncated COLMAP binary model: {stream.name}")
    return struct.unpack("<" + fmt, data)


def _skip_binary_records(stream, count, record_size):
    # Do not trust a corrupt count to skip beyond EOF.
    pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    target = pos + count * record_size
    if target > end:
        raise ValueError(f"truncated COLMAP binary model: {stream.name}")
    stream.seek(target)


def _read_binary_model(root: Path, points_wanted=True):
    cameras, images, points = {}, {}, {}
    with (root / "cameras.bin").open("rb") as stream:
        for _ in range(_unpack(stream, "Q")[0]):
            camera_id, model_id, width, height = _unpack(stream, "iiQQ")
            if model_id not in _CAMERA_MODELS:
                raise ValueError(f"unsupported COLMAP camera model id {model_id}")
            model, count = _CAMERA_MODELS[model_id]
            cameras[camera_id] = ModelCamera(camera_id, model, width, height,
                                             _unpack(stream, "d" * count))
    with (root / "images.bin").open("rb") as stream:
        for _ in range(_unpack(stream, "Q")[0]):
            values = _unpack(stream, "i7di")
            image_id, camera_id = values[0], values[8]
            name = bytearray()
            while True:
                byte = stream.read(1)
                if not byte:
                    raise ValueError(f"truncated COLMAP image name: {stream.name}")
                if byte == b"\0":
                    break
                name.extend(byte)
            images[image_id] = ModelImage(image_id, camera_id, name.decode("utf-8"),
                                          tuple(values[1:5]), tuple(values[5:8]))
            _skip_binary_records(stream, _unpack(stream, "Q")[0], 24)
    point_path = root / "points3D.bin"
    if points_wanted and point_path.is_file():
        with point_path.open("rb") as stream:
            for _ in range(_unpack(stream, "Q")[0]):
                values = _unpack(stream, "Q3d3BdQ")
                point_id = values[0]
                points[point_id] = ModelPoint3D(point_id, tuple(values[1:4]),
                                                tuple(values[4:7]), values[7])
                _skip_binary_records(stream, values[8], 8)
    return cameras, images, points


def _read_text_model(root: Path, points_wanted=True):
    cameras, images, points = {}, {}, {}
    with (root / "cameras.txt").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            camera_id = int(fields[0])
            cameras[camera_id] = ModelCamera(camera_id, fields[1], int(fields[2]),
                                             int(fields[3]), tuple(map(float, fields[4:])))
    with (root / "images.txt").open(encoding="utf-8") as stream:
        # Unlike comment/blank lines between entries, the points2D line may
        # legitimately be empty and must still be consumed after each pose.
        for line in stream:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.strip().split(maxsplit=9)
            if len(fields) != 10:
                raise ValueError(f"invalid COLMAP image pose: {line.rstrip()}")
            image_id, camera_id = int(fields[0]), int(fields[8])
            images[image_id] = ModelImage(image_id, camera_id, fields[9],
                                          tuple(map(float, fields[1:5])),
                                          tuple(map(float, fields[5:8])))
            if stream.readline() == "":
                raise ValueError(f"missing COLMAP points2D line for {fields[9]}")
    point_path = root / "points3D.txt"
    if points_wanted and point_path.is_file():
        with point_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                fields = line.split()
                point_id = int(fields[0])
                points[point_id] = ModelPoint3D(point_id, tuple(map(float, fields[1:4])),
                                                tuple(map(int, fields[4:7])), float(fields[7]))
    return cameras, images, points


def read_model(sparse_dir, *, points=True):
    """Read binary or text COLMAP cameras, registered images and 3D points.

    Return three dictionaries keyed by COLMAP ID, containing ModelCamera,
    ModelImage and ModelPoint3D records respectively. Poses are world-to-camera.
    Binary is preferred when both encodings exist. Poses-only text models are
    supported even when points3D.txt is absent; ``points=False`` skips the 3D points
    (the third dictionary is then empty).
    """
    root = Path(sparse_dir)
    if all((root / (name + ".bin")).is_file() for name in ("cameras", "images")):
        return _read_binary_model(root, points)
    if all((root / (name + ".txt")).is_file() for name in ("cameras", "images")):
        return _read_text_model(root, points)
    raise FileNotFoundError(f"COLMAP cameras and images model not found in {root}")


def prepare_dataset(sparse_dir, image_dir, dataset_dir, *, source_image_map=None):
    """Undistort a solved model once without changing its reconstruction gauge.

    The dataset has ``images/``, ``sparse/`` and ``image_name_map.json``.
    Mapping keys are COLMAP image names; values identify the corresponding
    session source images. ``source_image_map`` may map hashed input names.
    The result also reports the undistorted model's ``points3D``,
    ``mean_reprojection_error_px``, ``mean_track_length`` and ``cameras``.
    """
    pycolmap = _import_pycolmap()
    if pycolmap is None:
        return {"ok": False, "reason": "backend_not_provisioned",
                "hint": "Install pycolmap to undistort the shared dataset."}
    root = Path(dataset_dir).expanduser().resolve()
    try:
        if root.exists() and any(root.iterdir()):
            raise ValueError("dataset output directory must be empty")
        root.mkdir(parents=True, exist_ok=True)
        source_model = pycolmap.Reconstruction(str(sparse_dir))
        pycolmap.undistort_images(output_path=str(root), input_path=str(sparse_dir),
                                 image_path=str(image_dir), output_type="COLMAP",
                                 copy_policy=pycolmap.CopyType.copy)
        model_dir = root / "sparse"
        if not any((model_dir / f"cameras{ext}").is_file() for ext in (".bin", ".txt")):
            nested = model_dir / "0"
            for child in nested.iterdir():
                shutil.move(str(child), str(model_dir / child.name))
            nested.rmdir()
        model = pycolmap.Reconstruction(str(model_dir))
        if (model.num_reg_images() != source_model.num_reg_images()
                or model.num_reg_images() < 1 or model.num_points3D() < 1):
            raise ValueError("undistorted reconstruction is incomplete")
        model.write_text(str(model_dir))
        mapping = {}
        for image_id in model.reg_image_ids():
            name = model.images[image_id].name
            image = root / "images" / name
            if not image.is_file() or image.stat().st_size == 0:
                raise ValueError(f"missing undistorted image: {image}")
            if source_image_map is not None and name not in source_image_map:
                raise ValueError(f"source-image mapping missing for {name}")
            mapped_name = name if source_image_map is None else source_image_map[name]
            if not isinstance(mapped_name, str):
                raise ValueError(f"invalid source-image mapping for {name}")
            mapping[name] = mapped_name
        map_path = root / "image_name_map.json"
        map_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        cameras = [{"camera_id": int(camera_id), "model": camera.model.name,
                    "width": int(camera.width), "height": int(camera.height),
                    "params": [float(p) for p in camera.params]}
                   for camera_id, camera in sorted(model.cameras.items())]
        return {"ok": True, "dataset_dir": str(root), "sparse_dir": str(model_dir),
                "images_dir": str(root / "images"), "image_name_map": mapping,
                "image_name_map_path": str(map_path), "registered": model.num_reg_images(),
                "points3D": model.num_points3D(),
                "mean_reprojection_error_px": float(model.compute_mean_reprojection_error()),
                "mean_track_length": float(model.compute_mean_track_length()),
                "cameras": cameras}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "reason": "dataset_preparation_failed", "hint": str(exc)}

# ---------------------------------------------------------------------------
# Public availability check
# ---------------------------------------------------------------------------

def _import_pycolmap():
    """Try to import pycolmap; return the module or None."""
    try:
        import pycolmap  # type: ignore
        return pycolmap
    except ImportError:
        return None


def available() -> bool:
    """Return True if pycolmap is importable."""
    return _import_pycolmap() is not None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_sparse(
    image_dir: str,
    out_dir: str,
    *,
    camera_model: str = "SIMPLE_RADIAL",
    single_camera: bool = True,
    camera_groups: Sequence[Sequence[str]] = (),
) -> dict:
    """Run in-process pycolmap sparse SfM on *image_dir*.

    Parameters
    ----------
    image_dir:
        Directory containing JPEG/PNG input images.
    out_dir:
        Session root; sparse model written to ``<out_dir>/sparse/``.
    camera_model:
        COLMAP camera model string (default ``SIMPLE_RADIAL``).
    single_camera:
        Assume all images share a single camera (suitable for bracket
        fixture and most turntable captures).
    camera_groups:
        With ``single_camera=False``, image names (relative to *image_dir*)
        that share one camera each, e.g. the frames of one video clip. Images
        outside every group keep COLMAP's automatic per-image cameras.

    Returns
    -------
    dict with keys: ok, sparse_dir, registered, backend  (on success)
         or:       ok, reason, hint                       (on failure)
    """
    pycolmap = _import_pycolmap()
    if pycolmap is None:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "pip install pycolmap",
        }

    image_dir = str(Path(image_dir).resolve())
    sparse_dir = str(Path(out_dir) / "sparse")
    os.makedirs(sparse_dir, exist_ok=True)

    # Attempt GLOMAP global mapper first (optional)
    try:
        import glomap  # type: ignore  # noqa: F401
        _has_glomap = True
    except ImportError:
        _has_glomap = False

    try:
        backend = "pycolmap"
        registered = 0
        if _has_glomap:
            try:
                registered = _run_glomap(pycolmap, image_dir, sparse_dir,
                                         camera_model, single_camera, camera_groups)
                backend = "pycolmap+glomap"
            except Exception:
                # An installed binding is not necessarily executable with this
                # pycolmap version. Use the documented incremental fallback.
                registered = 0
        if registered == 0:
            registered = _run_incremental(pycolmap, image_dir, sparse_dir,
                                          camera_model, single_camera, camera_groups)
            backend = "pycolmap"
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"reconstruction error: {exc}",
            "hint": "check image quality and pycolmap installation",
        }

    if registered == 0:
        return {
            "ok": False,
            "reason": "no_images_registered",
            "hint": "ensure images overlap sufficiently and are not all orthographic",
            "backend": backend,
            "sparse_dir": sparse_dir,
        }

    return {
        "ok": True,
        "sparse_dir": sparse_dir,
        "registered": registered,
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_features(
    pycolmap,
    database_path: str,
    image_dir: str,
    camera_model: str,
    single_camera: bool,
    camera_groups: Sequence[Sequence[str]],
) -> None:
    """Write SIFT features; each camera group is read separately onto one new camera."""
    if single_camera or not camera_groups:
        pycolmap.extract_features(
            database_path=database_path,
            image_path=image_dir,
            camera_mode=pycolmap.CameraMode.SINGLE if single_camera else pycolmap.CameraMode.AUTO,
            camera_model=camera_model,
        )
        return
    grouped: set[str] = set()
    for names in camera_groups:
        names = sorted(set(names) - grouped)
        if names:
            pycolmap.extract_features(database_path=database_path, image_path=image_dir,
                                      image_names=names, camera_mode=pycolmap.CameraMode.SINGLE,
                                      camera_model=camera_model)
            grouped.update(names)
    # An empty image_names list means "every image", so only read a nonempty rest.
    rest = sorted(p.name for p in Path(image_dir).iterdir() if p.is_file() and p.name not in grouped)
    if rest:
        pycolmap.extract_features(database_path=database_path, image_path=image_dir,
                                  image_names=rest, camera_mode=pycolmap.CameraMode.AUTO,
                                  camera_model=camera_model)


def _run_incremental(
    pycolmap,
    image_dir: str,
    sparse_dir: str,
    camera_model: str,
    single_camera: bool,
    camera_groups: Sequence[Sequence[str]],
) -> int:
    """Run pycolmap incremental mapper; return number of registered images."""
    with tempfile.TemporaryDirectory() as db_tmp:
        database_path = os.path.join(db_tmp, "colmap.db")

        _extract_features(pycolmap, database_path, image_dir, camera_model,
                          single_camera, camera_groups)

        # Feature matching (exhaustive for small sets)
        pycolmap.match_exhaustive(database_path=database_path)

        # Incremental reconstruction. Every disconnected component lands in a
        # numbered scratch model; only the largest is published to sparse_dir.
        models_dir = os.path.join(db_tmp, "models")
        os.makedirs(models_dir)
        maps = pycolmap.incremental_mapping(
            database_path=database_path,
            image_path=image_dir,
            output_path=models_dir,
        )

        # maps is a dict {reconstruction_id: Reconstruction}
        if not maps:
            return 0

        # Pick the largest reconstruction and write it
        best = max(maps.values(), key=lambda r: r.num_reg_images())
        best.write(sparse_dir)
        best.write_text(sparse_dir)
        return best.num_reg_images()


def _run_glomap(
    pycolmap,
    image_dir: str,
    sparse_dir: str,
    camera_model: str,
    single_camera: bool,
    camera_groups: Sequence[Sequence[str]],
) -> int:
    """Run GLOMAP global mapper; return number of registered images."""
    import glomap  # type: ignore

    with tempfile.TemporaryDirectory() as db_tmp:
        database_path = os.path.join(db_tmp, "colmap.db")

        _extract_features(pycolmap, database_path, image_dir, camera_model,
                          single_camera, camera_groups)
        pycolmap.match_exhaustive(database_path=database_path)

        reconstruction = glomap.mapper(
            database_path=database_path,
            image_path=image_dir,
            output_path=sparse_dir,
        )

        if reconstruction is None:
            return 0

        reconstruction.write(sparse_dir)
        reconstruction.write_text(sparse_dir)
        return reconstruction.num_reg_images()

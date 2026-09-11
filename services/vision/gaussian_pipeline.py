"""Isolated, real SfM → undistortion → Brush jobs in reconstruction units.

Run with ``python -m services.vision.gaussian_pipeline --request request.json``.
The job service owns the process group; this module never detaches children.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from statistics import median
import tempfile
import threading
import time
from typing import BinaryIO
import uuid


_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}
_REQUIRED = {"x", "y", "z", "opacity", *(f"scale_{i}" for i in range(3)),
             *(f"rot_{i}" for i in range(4)), *(f"f_dc_{i}" for i in range(3))}


def _crop_bounds(bounds) -> list[float]:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 6:
        raise ValueError("crop bounds must be [xmin,ymin,zmin,xmax,ymax,zmax]")
    result = [float(v) for v in bounds]
    if not all(math.isfinite(v) for v in result):
        raise ValueError("crop bounds must be finite")
    if any(result[i] >= result[i + 3] for i in range(3)):
        raise ValueError("each crop minimum must be less than its maximum")
    return result


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _scan_splat(path, *, bounds=None, sink: BinaryIO | None = None) -> dict:
    """Stream scalar-vertex PLYs, retaining property bytes rather than re-encoding.

    Gaussian PLYs have no topology. Nonempty other elements and list-valued
    vertices are rejected, since cropping those would require remapping indices.
    """
    import numpy as np

    with open(path, "rb") as src:
        before = _signature(os.fstat(src.fileno()))
        header = []
        properties = []
        count = None
        count_line = None
        element = None
        fmt = None
        size = 0
        while True:
            line = src.readline(65537)
            size += len(line)
            if not line or len(line) > 65536 or size > 1024 * 1024:
                raise ValueError("missing or oversized PLY header")
            header.append(line)
            try:
                words = line.decode("ascii").strip().split()
            except UnicodeDecodeError as exc:
                raise ValueError("non-ASCII PLY header") from exc
            if len(header) == 1:
                if words != ["ply"]:
                    raise ValueError("not a PLY file")
                continue
            if not words:
                raise ValueError("empty PLY header line")
            if words[0] in ("comment", "obj_info"):
                continue
            if words[0] == "format":
                if fmt is not None or len(words) != 3 or words[2] != "1.0":
                    raise ValueError("invalid PLY format")
                fmt = words[1]
                if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
                    raise ValueError("unsupported PLY encoding")
            elif words[0] == "element":
                if len(words) != 3:
                    raise ValueError("invalid PLY element")
                element = words[1]
                n = int(words[2])
                if n < 0:
                    raise ValueError("negative PLY element count")
                if element == "vertex":
                    if count is not None:
                        raise ValueError("duplicate vertex element")
                    count, count_line = n, len(header) - 1
                elif n:
                    raise ValueError("Gaussian PLY must not contain nonempty topology elements")
            elif words[0] == "property":
                if element is None:
                    raise ValueError("PLY property without element")
                if element == "vertex":
                    if len(words) != 3 or words[1] not in _PLY_TYPES:
                        raise ValueError("Gaussian vertex properties must be supported scalars")
                    if any(name == words[2] for name, _ in properties):
                        raise ValueError("duplicate PLY property")
                    properties.append((words[2], _PLY_TYPES[words[1]]))
            elif words == ["end_header"]:
                break
            else:
                raise ValueError("invalid PLY header directive")
        if fmt is None or count is None or count <= 0:
            raise ValueError("empty or incomplete Gaussian PLY")
        names = [name for name, _ in properties]
        if not _REQUIRED.issubset(names):
            raise ValueError("PLY lacks Gaussian position, opacity, scale, rotation or color properties")
        endian = ">" if fmt == "binary_big_endian" else "<"
        dtype = np.dtype([(name, endian + kind) for name, kind in properties])
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        kept = 0

        def select(values):
            nonlocal kept, lo, hi
            for name in names:
                if not np.isfinite(values[name]).all():
                    raise ValueError("nonfinite Gaussian property: " + name)
            rotation_norm = sum(values[f"rot_{i}"].astype(np.float64) ** 2 for i in range(4))
            if np.any(rotation_norm == 0):
                raise ValueError("zero Gaussian rotation quaternion")
            mask = np.ones(len(values), dtype=bool)
            if bounds is not None:
                for i, name in enumerate(("x", "y", "z")):
                    mask &= (values[name] >= bounds[i]) & (values[name] <= bounds[i + 3])
            n = int(mask.sum())
            if n:
                for i, name in enumerate(("x", "y", "z")):
                    chosen = values[name][mask]
                    lo[i] = min(lo[i], float(chosen.min()))
                    hi[i] = max(hi[i], float(chosen.max()))
                kept += n
            return mask

        if fmt == "ascii":
            for _ in range(count):
                row = src.readline(1024 * 1024 + 1)
                if not row or len(row) > 1024 * 1024:
                    raise ValueError("truncated or oversized ASCII PLY record")
                tokens = row.split()
                if len(tokens) != len(properties):
                    raise ValueError("incorrect ASCII PLY property count")
                values = np.empty(1, dtype=dtype)
                for token, (name, kind) in zip(tokens, properties):
                    value = float(token) if kind.startswith("f") else int(token)
                    if not math.isfinite(value):
                        raise ValueError("nonfinite Gaussian property: " + name)
                    if not kind.startswith("f"):
                        limits = np.iinfo(values[name].dtype)
                        if not limits.min <= value <= limits.max:
                            raise ValueError("integer PLY property out of range")
                    values[name][0] = value
                if select(values)[0] and sink is not None:
                    sink.write(row)
            while trailing := src.read(65536):
                if trailing.strip():
                    raise ValueError("trailing ASCII PLY records")
        else:
            if before[2] - src.tell() != count * dtype.itemsize:
                raise ValueError("incomplete or oversized binary PLY payload")
            remaining = count
            while remaining:
                n = min(remaining, 16384)
                payload = src.read(n * dtype.itemsize)
                if len(payload) != n * dtype.itemsize:
                    raise ValueError("truncated binary PLY payload")
                values = np.frombuffer(payload, dtype=dtype, count=n)
                mask = select(values)
                if sink is not None:
                    sink.write(payload if mask.all() else values[mask].tobytes())
                remaining -= n
            if src.read(1):
                raise ValueError("PLY changed while reading")
        if _signature(os.fstat(src.fileno())) != before:
            raise ValueError("PLY changed while reading")
        return {"count": kept, "bounds": [*lo.tolist(), *hi.tolist()] if kept else None,
                "units": "reconstruction", "header": header, "count_line": count_line}


def inspect_splat(path) -> dict:
    """Validate a complete Gaussian PLY; raise ValueError/OSError on invalid input."""
    result = _scan_splat(path)
    return {key: result[key] for key in ("count", "bounds", "units")}


def crop_splat(source_path, output_path, bounds) -> dict:
    """Atomically crop Gaussian centers, preserving every retained property byte.

    Empty/invalid crops leave any existing destination untouched. Bounds in the
    result describe retained centers, not physical CAD dimensions.
    """
    pending = None
    try:
        crop = _crop_bounds(bounds)
        source = Path(source_path).resolve()
        target = Path(output_path).resolve()
        if source == target:
            raise ValueError("crop output must differ from immutable source")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=target.parent) as body:
            info = _scan_splat(source, bounds=crop, sink=body)
            if not info["count"]:
                return {"ok": False, "reason": "empty_crop", "hint": "The crop excludes every Gaussian center."}
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".crop-", delete=False) as dst:
                pending = Path(dst.name)
                for index, line in enumerate(info["header"]):
                    dst.write(f"element vertex {info['count']}\n".encode("ascii")
                              if index == info["count_line"] else line)
                body.seek(0)
                shutil.copyfileobj(body, dst)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(pending, target)
        return {"ok": True, "path": str(target), "splat_path": str(target),
                "count": info["count"], "bounds": info["bounds"], "units": "reconstruction"}
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        return {"ok": False, "reason": "invalid_splat_crop", "hint": str(exc)}
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


class _Progress:
    def __init__(self, work: Path):
        self.work = work
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.value = {"status": "waiting", "stage": "preparing", "pct": 0,
                      "msg": "Preparing reconstruction inputs", "frames": 0,
                      "registered": 0, "iteration": 0, "total_iterations": 0,
                      "splat_path": "", "bounds": None,
                      "units": "reconstruction", "backend": "brush"}
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def update(self, **fields):
        with self.lock:
            self.value.update(fields)
            self.value["updated_at"] = time.time()
            _atomic_json(self.work / "progress.json", self.value)

    def _heartbeat(self):
        while not self.stop.wait(5):
            self.update()

    def finish(self, **fields):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        self.update(**fields)
        _atomic_json(self.work / "result.json", self.value)
        return dict(self.value)


def _view_camera(model, batch_id: str) -> dict | None:
    """Keep the undistorted training calibration and COLMAP world gauge."""
    # Minimal SfM fixtures and older artifacts may not carry camera geometry.
    if not hasattr(model, "cameras") or not hasattr(model, "points3D"):
        return None
    candidates = sorted(model.reg_image_ids(),
                        key=lambda key: (-model.images[key].num_points3D,
                                         model.images[key].name, int(key)))
    for image_id in candidates:
        image = model.images[image_id]
        camera = model.cameras[image.camera_id]
        c2w = image.cam_from_world().inverse().matrix()
        calibration = camera.calibration_matrix()
        eye = [float(c2w[i, 3]) for i in range(3)]
        forward = [float(c2w[i, 2]) for i in range(3)]
        up = [-float(c2w[i, 1]) for i in range(3)]
        intrinsics = [float(calibration[0, 0]), float(calibration[1, 1]),
                      float(calibration[0, 2]), float(calibration[1, 2]),
                      int(camera.width), int(camera.height)]
        if (not all(math.isfinite(value) for value in eye + forward + up + intrinsics)
                or min(intrinsics[0], intrinsics[1], intrinsics[4], intrinsics[5]) <= 0
                or abs(sum(value * value for value in forward) - 1) > 1e-5
                or abs(sum(value * value for value in up) - 1) > 1e-5
                or abs(sum(a * b for a, b in zip(forward, up))) > 1e-5):
            continue
        depths = []
        for observation in image.points2D:
            if not observation.has_point3D():
                continue
            point = model.points3D[observation.point3D_id].xyz
            depth = sum((float(point[i]) - eye[i]) * forward[i] for i in range(3))
            if math.isfinite(depth) and depth > 0:
                depths.append(depth)
        if depths:
            focus_distance = median(depths)
            if not math.isfinite(focus_distance):
                continue
            return {"id": batch_id, "eye": eye, "forward": forward, "up": up,
                    "focus_distance": focus_distance, "intrinsics": intrinsics}
    return None


def run(request: dict, *, supervised_process_group: bool = False) -> dict:
    """Execute one frozen snapshot. The caller allocates a fresh work directory."""
    work = Path(request["work_dir"]).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    progress = _Progress(work)
    progress.update()
    progress.thread.start()
    try:
        iterations = int(request.get("iterations", 2000))
        every = int(request.get("export_every", min(100, iterations)))
        if iterations < 1 or every < 1:
            raise ValueError("iterations and export_every must be positive")
        mode = request.get("mode", "environment")
        if mode not in ("environment", "object"):
            raise ValueError("mode must be environment or object")
        crop = request.get("crop_bounds")
        if crop is not None:
            crop = _crop_bounds(crop)
        session = Path(request["session_dir"]).expanduser().resolve()
        progress.update(total_iterations=iterations, mode=mode, crop_bounds=crop,
                        max_splats=2_000_000)
        paths = list(request.get("images", []))
        if request.get("video_path"):
            from .frames import extract_frames
            progress.update(stage="extracting", msg="Extracting sharp video frames")
            extraction = extract_frames(request["video_path"], str(session / "frames"))
            paths.extend(frame.path for frame in extraction.frames)
            progress.update(video_extraction=extraction.to_dict())
        images = work / "input_images"
        images.mkdir(exist_ok=False)
        # Hash the source identity, not the basename: phone folders commonly
        # contain different IMG_0001.jpg files. Copies freeze concurrent capture.
        seen = set()
        for raw_path in paths:
            src = Path(raw_path).expanduser().resolve()
            if str(src) in seen:
                continue
            seen.add(str(src))
            before = _signature(src.stat())
            name = hashlib.sha256(os.fsencode(src)).hexdigest() + src.suffix.lower()
            shutil.copyfile(src, images / name)
            if _signature(src.stat()) != before:
                raise ValueError("input image changed during snapshot: " + str(src))
        progress.update(frames=len(seen))
        if len(seen) < 3:
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason="no_images_registered", msg="At least three overlapping images are required",
                                   hint="Capture textured, sharp, overlapping views with camera translation.")
        from . import colmap_pipeline
        pycolmap = colmap_pipeline._import_pycolmap()
        if pycolmap is None:
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason="backend_not_provisioned", msg="pycolmap is unavailable",
                                   hint="Install pycolmap==3.13.0 in the Chudware Python environment.")
        progress.update(status="sparse", stage="sparse", pct=5, msg="Estimating cameras and sparse structure")
        sparse = colmap_pipeline.run_sparse(str(images), str(work), single_camera=False)
        if not sparse.get("ok"):
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason=sparse.get("reason", "sfm_failed"),
                                   hint=sparse.get("hint", ""), msg="Sparse reconstruction failed")
        registered = int(sparse["registered"])
        reconstruction = pycolmap.Reconstruction(sparse["sparse_dir"])
        points = int(reconstruction.num_points3D())
        progress.update(registered=registered, sparse_dir=sparse["sparse_dir"],
                        unregistered=max(0, len(seen) - registered))
        if registered < 3 or points == 0:
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason="no_images_registered", msg="Insufficient connected camera registration",
                                   hint="Capture at least three overlapping translated views of textured surfaces.")
        # Disconnected components have unrelated coordinate systems. run_sparse
        # selects the largest component; never concatenate their camera poses.
        dataset = work / "dataset"
        progress.update(stage="undistorting", pct=20,
                        msg=f"Undistorting {registered}/{len(seen)} connected registered views")
        pycolmap.undistort_images(output_path=str(dataset), input_path=sparse["sparse_dir"],
                                 image_path=str(images), output_type="COLMAP", copy_policy="copy")
        model = pycolmap.Reconstruction(str(dataset / "sparse"))
        if model.num_reg_images() != registered or model.num_points3D() == 0:
            raise ValueError("undistorted reconstruction is incomplete")
        for image_id in model.reg_image_ids():
            image_file = dataset / "images" / model.images[image_id].name
            if not image_file.is_file() or image_file.stat().st_size == 0:
                raise ValueError("missing undistorted image: " + str(image_file))
        view_camera = _view_camera(model, str(work))
        published = {}

        def publish(artifact):
            source = str(Path(artifact["splat_path"]).resolve())
            if source not in published:
                info = inspect_splat(source)
                final_path = source
                if crop is not None:
                    final_path = str(work / "published" / (uuid.uuid4().hex + ".ply"))
                    info = crop_splat(source, final_path, crop)
                    if not info["ok"]:
                        if info["reason"] == "empty_crop":
                            return None
                        raise ValueError(info["hint"])
                published[source] = {"splat_path": final_path, "source_splat_path": source,
                                     "bounds": info["bounds"]}
                if view_camera is not None:
                    published[source]["view_camera"] = view_camera
            return {**published[source], "iteration": int(artifact.get("iteration", 0))}

        def on_brush(stage, pct, msg, artifact=None):
            fields = {"status": "training", "stage": stage,
                      "pct": min(99, 25 + int(pct) * 74 // 100), "msg": msg}
            if artifact:
                preview = publish(artifact)
                if preview is not None:
                    fields.update(preview)
            progress.update(**fields)

        from .dense_backends import run_brush
        progress.update(status="training", stage="training", pct=25, msg="Training Gaussian splats")
        result = run_brush(str(dataset / "sparse"), str(work), image_dir=str(dataset),
                           max_steps=iterations, export_mesh=False,
                           export_every=min(every, iterations), progress=on_brush,
                           own_process_group=not supervised_process_group)
        if not result.get("ok"):
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason=result.get("reason", "training_failed"),
                                   hint=result.get("hint", ""), msg="Gaussian training failed")
        artifact = result.get("artifact") or result
        final = publish(artifact)
        if final is None:
            return progress.finish(ok=False, status="failed", stage="failed",
                                   reason="empty_crop", msg="The crop excludes every Gaussian center",
                                   hint="Widen the explicit crop bounds and retry.",
                                   source_splat_path=str(artifact["splat_path"]))
        return progress.finish(**final, ok=True, status="done", stage="done", pct=100,
                               reason="", hint="",
                               msg=f"Gaussian reconstruction complete ({registered}/{len(seen)} registered views)")
    except Exception as exc:
        return progress.finish(ok=False, status="failed", stage="failed",
                               reason="gaussian_pipeline_failed", hint=str(exc), msg=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--supervised-process-group", action="store_true",
                        help="Keep trainer descendants in the supervisor-owned process group")
    args = parser.parse_args(argv)
    try:
        with open(args.request, encoding="utf-8") as stream:
            request = json.load(stream)
        result = run(request, supervised_process_group=args.supervised_process_group)
    except Exception as exc:
        # A malformed request may not provide a writable work_dir. Never claim
        # completion without a terminal result; the job service reconciles exit.
        print(json.dumps({"ok": False, "reason": "invalid_request", "hint": str(exc)}), flush=True)
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

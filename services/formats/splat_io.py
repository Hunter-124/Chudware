"""Bounded Gaussian-splat interchange and approximate surface extraction.

All coordinates are retained in the source reconstruction's world frame: no
axis swap, unit conversion, or camera convention is inferred.  Gaussian
quaternions are normalized (w, x, y, z), scales are positive linear standard
deviations, and SH uses the 3D Gaussian Splatting real-SH convention.  Rest
coefficients are coefficient-major here; Gaussian PLY stores them channel-major.
The reconstructed surface is approximate, not a metrology-grade surface or
the original radiance field.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from pathlib import Path

import numpy as np

MAX_POINTS = 2_000_000  # Also the native renderer's limit.
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_COLMAP_MODEL_BYTES = 512 * 1024 * 1024  # Text cameras + images read for TSDF poses.
_C0 = 0.28209479177387814
_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}
_REQUIRED = {"x", "y", "z", "opacity", *(f"scale_{i}" for i in range(3)),
             *(f"rot_{i}" for i in range(4)), *(f"f_dc_{i}" for i in range(3))}


@dataclass
class GaussianCloud:
    means: np.ndarray
    scales: np.ndarray
    rotations: np.ndarray
    opacities: np.ndarray
    sh_dc: np.ndarray
    sh_rest: np.ndarray | None
    source_format: str
    lossy: list[str]


def _check_cloud(cloud: GaussianCloud) -> GaussianCloud:
    """Validate the interchange invariants and normalize quaternions."""
    means = np.asarray(cloud.means, dtype=np.float32)
    if means.ndim != 2 or means.shape[1:] != (3,) or not 0 < len(means) <= MAX_POINTS:
        raise ValueError("Gaussian positions must be a bounded nonempty Nx3 array")
    n = len(means)
    arrays = {
        "scales": (cloud.scales, (n, 3)), "rotations": (cloud.rotations, (n, 4)),
        "opacities": (cloud.opacities, (n,)), "sh_dc": (cloud.sh_dc, (n, 3)),
    }
    checked = {}
    for name, (value, shape) in arrays.items():
        a = np.asarray(value, dtype=np.float32)
        if a.shape != shape or not np.isfinite(a).all():
            raise ValueError(f"invalid Gaussian {name} shape or values")
        checked[name] = a
    if not np.isfinite(means).all() or np.any(np.abs(means) > 1e15):
        raise ValueError("nonfinite or out-of-range Gaussian position")
    if np.any(checked["scales"] < 1e-15) or np.any(checked["scales"] > 1e15):
        raise ValueError("Gaussian scales must be positive and within renderer limits")
    if np.any((checked["opacities"] < 0) | (checked["opacities"] > 1)):
        raise ValueError("Gaussian opacity must be in [0, 1]")
    norm = np.linalg.norm(checked["rotations"].astype(np.float64), axis=1)
    if np.any(~np.isfinite(norm)) or np.any(norm < 1e-15):
        raise ValueError("zero or invalid Gaussian quaternion")
    rotations = (checked["rotations"] / norm[:, None]).astype(np.float32)
    rest = None
    if cloud.sh_rest is not None:
        rest = np.asarray(cloud.sh_rest, dtype=np.float32)
        if rest.ndim != 3 or rest.shape[0] != n or rest.shape[1] not in (0, 3, 8, 15) or rest.shape[2] != 3:
            raise ValueError("SH rest must be NxKx3 for degree 0, 1, 2 or 3")
        if not np.isfinite(rest).all():
            raise ValueError("nonfinite Gaussian SH coefficient")
        if rest.shape[1] == 0:
            rest = None
    return GaussianCloud(means, checked["scales"], rotations, checked["opacities"],
                         checked["sh_dc"], rest, str(cloud.source_format), list(cloud.lossy))


def _read_ply_header(stream):
    if stream.readline(8).strip() != b"ply":
        raise ValueError("not a PLY file")
    size, fmt, elements, current = 4, None, [], None
    while True:
        line = stream.readline(65537)
        size += len(line)
        if not line or len(line) > 65536 or size > 1024 * 1024:
            raise ValueError("missing or oversized PLY header")
        try:
            words = line.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise ValueError("non-ASCII PLY header") from exc
        if not words:
            raise ValueError("empty PLY header line")
        if words[0] in ("comment", "obj_info"):
            continue
        if words[0] == "format" and len(words) == 3 and words[2] == "1.0" and fmt is None:
            fmt = words[1]
            if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
                raise ValueError("unsupported PLY encoding")
        elif words[0] == "element" and len(words) == 3:
            if fmt is None:
                raise ValueError("missing PLY format")
            count = int(words[2])
            if count < 0:
                raise ValueError("negative PLY element count")
            current = {"name": words[1], "count": count, "props": []}
            elements.append(current)
        elif words[0] == "property" and current is not None:
            if len(words) == 3 and words[1] in _PLY_TYPES:
                current["props"].append((words[2], _PLY_TYPES[words[1]]))
            elif len(words) == 5 and words[1] == "list" and words[2] in _PLY_TYPES and words[3] in _PLY_TYPES:
                current["props"].append((words[4], None))
            else:
                raise ValueError("unsupported PLY property")
            if len(current["props"]) > 128:
                raise ValueError("too many PLY properties")
        elif words == ["end_header"]:
            break
        else:
            raise ValueError("invalid PLY header")
    vertices = [e for e in elements if e["name"] == "vertex"]
    if fmt is None or len(vertices) != 1:
        raise ValueError("PLY requires exactly one vertex element")
    return fmt, elements, vertices[0]


def detect_ply_kind(path) -> str:
    """Classify by PLY header, not extension: mesh, Gaussian, or points."""
    with Path(path).open("rb") as stream:
        _, elements, vertex = _read_ply_header(stream)
    if any(e["name"] == "face" for e in elements):
        return "mesh"
    names = {name for name, _ in vertex["props"]}
    return "gaussian" if _REQUIRED <= names else "points"


def _read_ply(path: Path) -> GaussianCloud:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("splat file exceeds byte limit")
    with path.open("rb") as stream:
        fmt, elements, vertex = _read_ply_header(stream)
        n, props = vertex["count"], vertex["props"]
        if not 0 < n <= MAX_POINTS:
            raise ValueError("invalid or oversized Gaussian vertex count")
        if any(e is not vertex and e["count"] for e in elements):
            raise ValueError("Gaussian PLY cannot contain nonempty topology or other elements")
        names = [name for name, _ in props]
        if len(set(names)) != len(names) or any(kind is None for _, kind in props):
            raise ValueError("duplicate or list-valued Gaussian vertex property")
        if not _REQUIRED <= set(names):
            raise ValueError("PLY is not a Gaussian cloud")
        rest_names = [name for name in names if name.startswith("f_rest_")]
        if len(rest_names) not in (0, 9, 24, 45) or set(rest_names) != {f"f_rest_{i}" for i in range(len(rest_names))}:
            raise ValueError("Gaussian PLY has incomplete or unsupported SH degree")
        endian = ">" if fmt == "binary_big_endian" else "<"
        dtype = np.dtype([(name, endian + kind) for name, kind in props])
        if fmt == "ascii":
            values = np.empty(n, dtype=dtype)
            for i in range(n):
                line = stream.readline(65537)
                if not line or len(line) > 65536:
                    raise ValueError("truncated or oversized ASCII PLY row")
                parts = line.split()
                if len(parts) != len(props):
                    raise ValueError("incorrect ASCII PLY property count")
                for (name, kind), token in zip(props, parts):
                    value = float(token) if kind.startswith("f") else int(token)
                    if not math.isfinite(value):
                        raise ValueError("nonfinite PLY property")
                    if not kind.startswith("f"):
                        bounds = np.iinfo(values[name].dtype)
                        if value < bounds.min or value > bounds.max:
                            raise ValueError("integer PLY property out of range")
                    values[name][i] = value
            while chunk := stream.read(65536):
                if chunk.strip():
                    raise ValueError("unexpected trailing PLY data")
        else:
            if path.stat().st_size - stream.tell() != n * dtype.itemsize:
                raise ValueError("truncated or oversized binary PLY payload")
            values = np.fromfile(stream, dtype=dtype, count=n)
            if len(values) != n or stream.read(1):
                raise ValueError("truncated or changed binary PLY payload")
    for name in names:
        if not np.isfinite(values[name]).all():
            raise ValueError("nonfinite PLY property: " + name)
    def columns(prefix, count):
        return np.column_stack([values[f"{prefix}_{i}"] for i in range(count)])
    means = np.column_stack([values[axis] for axis in "xyz"])
    raw_opacity = values["opacity"].astype(np.float64)
    opacities = np.exp(-np.logaddexp(0.0, -raw_opacity))
    with np.errstate(over="ignore", under="ignore"):
        scales = np.exp(columns("scale", 3).astype(np.float64))
    k = len(rest_names) // 3
    rest = columns("f_rest", len(rest_names)).reshape(n, 3, k).transpose(0, 2, 1) if k else None
    ignored = sorted(set(names) - _REQUIRED - set(rest_names))
    lossy = ["PLY attributes omitted: " + ", ".join(ignored)] if ignored else []
    if any(kind == "f8" for _, kind in props):
        lossy.append("PLY float64 attributes converted to float32")
    return _check_cloud(GaussianCloud(means, scales, columns("rot", 4), opacities,
                                      columns("f_dc", 3), rest, "ply", lossy))


_SPLAT_DTYPE = np.dtype([("xyz", "<f4", (3,)), ("scale", "<f4", (3,)),
                         ("rgba", "u1", (4,)), ("quat", "u1", (4,))])


def _read_splat(path: Path) -> GaussianCloud:
    size = path.stat().st_size
    if size <= 0 or size > MAX_BYTES or size % _SPLAT_DTYPE.itemsize:
        raise ValueError("invalid or oversized .splat payload")
    n = size // _SPLAT_DTYPE.itemsize
    if n > MAX_POINTS:
        raise ValueError(".splat exceeds Gaussian count limit")
    values = np.fromfile(path, dtype=_SPLAT_DTYPE, count=n)
    if len(values) != n:
        raise ValueError("truncated .splat payload")
    rgba = values["rgba"].astype(np.float32) / 255.0
    rotations = (values["quat"].astype(np.float32) - 128.0) / 128.0
    return _check_cloud(GaussianCloud(values["xyz"], values["scale"], rotations,
                                      rgba[:, 3], (rgba[:, :3] - 0.5) / _C0,
                                      None, "splat", [".splat stores color, opacity and rotation with 8-bit precision; higher SH is unavailable"]))


def load_splat(path) -> GaussianCloud:
    """Load Gaussian PLY, antimatter15 .splat, Niantic .spz or .ksplat.

    Input is bounded by MAX_POINTS and MAX_BYTES; malformed, unsupported and
    non-Gaussian files raise ValueError (filesystem errors remain OSError).
    """
    path = Path(path).expanduser()
    suffix = path.suffix.lower()
    if suffix == ".ply":
        return _read_ply(path)
    if suffix == ".splat":
        return _read_splat(path)
    if suffix == ".spz":
        from services.formats._splat_spz import decode_spz
        return _check_cloud(GaussianCloud(**decode_spz(path, max_points=MAX_POINTS, max_bytes=MAX_BYTES)))
    if suffix == ".ksplat":
        from services.formats._splat_ksplat import decode_ksplat
        return _check_cloud(GaussianCloud(**decode_ksplat(path, max_points=MAX_POINTS, max_bytes=MAX_BYTES)))
    raise ValueError("unsupported splat format (expected .ply, .splat, .spz or .ksplat)")


def _record_loss(cloud: GaussianCloud, message: str) -> None:
    if message not in cloud.lossy:
        cloud.lossy.append(message)


def save_splat(cloud: GaussianCloud, path, fmt: str) -> None:
    """Write native-renderer-compatible Gaussian PLY or antimatter15 .splat.

    Output is float32 binary little-endian PLY, with complete SH degree 0–3.
    A .splat cannot carry higher-order SH and quantizes color, opacity, and
    orientation.  Export losses are appended to ``cloud.lossy``.
    """
    value = _check_cloud(cloud)
    inputs = (cloud.means, cloud.scales, cloud.rotations, cloud.opacities, cloud.sh_dc, cloud.sh_rest)
    if any(a is not None and np.issubdtype(np.asarray(a).dtype, np.floating)
           and np.asarray(a).dtype.itemsize > 4 for a in inputs):
        _record_loss(cloud, "Gaussian float64 attributes converted to float32")
    fmt = str(fmt).lower().lstrip(".")
    dest = Path(path)
    if fmt == "splat":
        values = np.empty(len(value.means), dtype=_SPLAT_DTYPE)
        values["xyz"], values["scale"] = value.means, value.scales
        rgb = np.clip(value.sh_dc * _C0 + 0.5, 0.0, 1.0)
        values["rgba"][:, :3] = np.rint(rgb * 255).astype(np.uint8)
        values["rgba"][:, 3] = np.rint(value.opacities * 255).astype(np.uint8)
        values["quat"] = np.clip(np.rint(value.rotations * 128 + 128), 0, 255).astype(np.uint8)
        with dest.open("wb") as stream:
            values.tofile(stream)
        _record_loss(cloud, ".splat quantizes color, opacity and quaternion to 8 bits")
        if value.sh_rest is not None:
            _record_loss(cloud, ".splat discards all higher-order SH coefficients")
        if np.any((value.sh_dc * _C0 + 0.5 < 0) | (value.sh_dc * _C0 + 0.5 > 1)):
            _record_loss(cloud, ".splat clamps DC colors to [0, 1]")
        return
    if fmt not in ("ply", "gaussian_ply"):
        raise ValueError("output format must be ply or splat")
    k = 0 if value.sh_rest is None else value.sh_rest.shape[1]
    names = (["x", "y", "z"] + [f"f_dc_{i}" for i in range(3)] +
             [f"f_rest_{i}" for i in range(3 * k)] + ["opacity"] +
             [f"scale_{i}" for i in range(3)] + [f"rot_{i}" for i in range(4)])
    header = ("ply\nformat binary_little_endian 1.0\n" +
              f"element vertex {len(value.means)}\n" +
              "".join(f"property float {name}\n" for name in names) + "end_header\n")
    values = np.empty(len(value.means), dtype=np.dtype([(name, "<f4") for name in names]))
    for i, axis in enumerate("xyz"):
        values[axis] = value.means[:, i]
        values[f"f_dc_{i}"] = value.sh_dc[:, i]
        values[f"scale_{i}"] = np.log(value.scales[:, i])
    for i in range(4):
        values[f"rot_{i}"] = value.rotations[:, i]
    if k:
        for channel in range(3):
            for i in range(k):
                values[f"f_rest_{channel * k + i}"] = value.sh_rest[:, i, channel]
    opacity = np.clip(value.opacities.astype(np.float64), 1e-12, 1 - 1e-7)
    if np.any(opacity != value.opacities):
        _record_loss(cloud, "exact endpoint opacities clamped to finite Gaussian PLY logits")
    values["opacity"] = np.log(opacity / (1.0 - opacity))
    with dest.open("wb") as stream:
        stream.write(header.encode("ascii"))
        values.tofile(stream)


def splat_to_points(cloud: GaussianCloud, min_opacity=0.5) -> tuple[np.ndarray, np.ndarray]:
    """Extract centers and uint8 DC colors, without claiming they are a surface.

    A plain opacity cut over the whole scene; use ``prune_cloud`` for the subject.
    """
    value = _check_cloud(cloud)
    opacity = float(min_opacity)
    if not math.isfinite(opacity) or not 0 <= opacity <= 1:
        raise ValueError("min_opacity must be between 0 and 1")
    keep = value.opacities >= opacity
    rgb = np.clip(value.sh_dc[keep] * _C0 + 0.5, 0.0, 1.0)
    return value.means[keep], np.rint(rgb * 255).astype(np.uint8)


def subset_cloud(cloud: GaussianCloud, keep: np.ndarray) -> GaussianCloud:
    """The Gaussians selected by a boolean mask, with every attribute kept aligned."""
    value = _check_cloud(cloud)
    keep = np.asarray(keep, dtype=bool)
    if keep.shape != (len(value.means),) or not keep.any():
        raise ValueError("keep must be a nonempty boolean mask over the Gaussians")
    return GaussianCloud(value.means[keep], value.scales[keep], value.rotations[keep],
                         value.opacities[keep], value.sh_dc[keep],
                         None if value.sh_rest is None else value.sh_rest[keep],
                         value.source_format, list(value.lossy))


def write_points_ply(cloud: GaussianCloud, path) -> None:
    """Binary little-endian PLY of Gaussian centers with their DC colors."""
    value = _check_cloud(cloud)
    rgb = np.rint(np.clip(value.sh_dc * _C0 + 0.5, 0.0, 1.0) * 255).astype(np.uint8)
    rows = np.empty(len(value.means), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                            ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    rows["x"], rows["y"], rows["z"] = value.means.T
    rows["red"], rows["green"], rows["blue"] = rgb.T
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(rows)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    with Path(path).open("wb") as stream:
        stream.write(header.encode("ascii"))
        rows.tofile(stream)


def prune_cloud(cloud: GaussianCloud, *, images=None, min_opacity=None,
                remove_floor=True) -> tuple[GaussianCloud, dict]:
    """Reduce a trained scene to its subject; see ``splat_prune`` for the stages.

    ``images`` are the matching registered COLMAP poses. With them the region stage
    is the sphere the cameras orbit; without them it is robust percentile bounds.
    """
    from services.formats.splat_prune import PruneOptions, orbit_focus, prune_gaussians
    value = _check_cloud(cloud)
    options = PruneOptions(min_opacity=min_opacity, remove_floor=bool(remove_floor))
    if images:
        try:
            options.focus, options.radius = orbit_focus(images)
        except ValueError:
            pass  # No converging orbit (e.g. a straight pass): fall back to percentile bounds.
    keep, report = prune_gaussians(value, options)
    return subset_cloud(value, keep), report.as_dict()


def _subject_bounds(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fusion region: the pruned subject's robust box, padded for thin parts pruning missed."""
    lo, hi = np.percentile(points, [0.5, 99.5], axis=0)
    pad = 0.15 * float(np.max(hi - lo))
    return lo - pad, hi + pad


def splat_to_mesh(cloud: GaussianCloud, out_path, *, method="auto", min_opacity=None,
                  depth=9, sparse_dir=None, remove_floor=True, voxel_size=None) -> dict:
    """Approximate a surface using matching registered cameras or Gaussian centers.

    The scene is first pruned to its subject (``prune_cloud``); the pruned Gaussians
    and their center point cloud are written beside the mesh as ``<name>-pruned.ply``
    and ``<name>-points.ply``. ``min_opacity=None`` chooses the opacity cut adaptively.
    ``auto`` uses TSDF depth fusion when ``sparse_dir`` identifies the matching
    undistorted COLMAP training model, and otherwise reports a Poisson fallback.
    TSDF renders the full splat inside the subject bounds from every selected view
    (``splat_depth``) and fuses the subject's median depth with the dataset photos'
    colors; ``voxel_size`` (reconstruction units) overrides its pixel-footprint voxel.
    ``tsdf`` also falls back only when poses are unavailable; invalid cameras,
    failed fusion, and timeouts are errors, not silent Poisson substitutions.
    The caller must establish that the splat and camera model share a gauge.
    No method recovers unseen surfaces or certifies physical dimensions.
    """
    started = time.monotonic()
    value = _check_cloud(cloud)
    if min_opacity is not None:
        opacity = float(min_opacity)
        if not math.isfinite(opacity) or not 0 <= opacity <= 1:
            raise ValueError("min_opacity must be between 0 and 1")
    if method not in ("auto", "tsdf", "poisson", "ball_pivoting"):
        raise ValueError("method must be auto, tsdf, poisson or ball_pivoting")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 4 <= depth <= 12:
        raise ValueError("Poisson depth must be an integer from 4 to 12")
    dest = Path(out_path)
    if dest.suffix.lower() not in (".ply", ".obj"):
        raise ValueError("mesh output must be .ply or .obj")
    fallback_reason = None
    camera_model = None
    if sparse_dir is None:
        fallback_reason = "no matching registered COLMAP camera poses were supplied"
    else:
        from services.vision.colmap_pipeline import read_model
        model_root = Path(sparse_dir)
        binary = all((model_root / (name + ".bin")).is_file() for name in ("cameras", "images"))
        # Binary images skip their 2D keypoints by seeking; text images are read line by
        # line, so only text cameras/images are bounded by size. 3D points are not read.
        if not binary and sum(file.stat().st_size for file in
                              (model_root / "cameras.txt", model_root / "images.txt")
                              if file.is_file()) > MAX_COLMAP_MODEL_BYTES:
            raise ValueError("COLMAP model exceeds the bounded TSDF input size")
        try:
            cameras, images, _ = read_model(model_root, points=False)
        except FileNotFoundError:
            fallback_reason = "matching COLMAP camera model was not found"
        else:
            if not images:
                fallback_reason = "COLMAP model contains no registered camera poses"
            else:
                camera_model = cameras, images
    if method in ("auto", "tsdf"):
        method = "tsdf" if camera_model is not None else "poisson"
    else:
        fallback_reason = None  # An explicit method was not a fallback.
    pruned, prune_report = prune_cloud(value, images=None if camera_model is None else camera_model[1],
                                       min_opacity=min_opacity, remove_floor=remove_floor)
    points = pruned.means
    if len(points) < 30:
        raise ValueError("at least 30 Gaussian centers survive pruning; lower min_opacity "
                         "or pass remove_floor=false")
    pruned_path = dest.with_name(dest.stem + "-pruned.ply")
    points_path = dest.with_name(dest.stem + "-points.ply")
    save_splat(pruned, pruned_path, "ply")
    write_points_ply(pruned, points_path)
    artifacts = {"prune": prune_report, "pruned_splat_path": str(pruned_path),
                 "points_path": str(points_path)}
    if method == "tsdf":
        from services.vision.splat_depth import TIME_BUDGET_SECONDS, fuse_splat_depth
        remaining = TIME_BUDGET_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Gaussian TSDF meshing exceeded its time budget; no mesh was published")
        images_dir = Path(sparse_dir).parent / "images"
        mesh, stats = fuse_splat_depth(value.means, value.scales, value.rotations, value.opacities,
                                       *camera_model, bounds=_subject_bounds(points),
                                       images_dir=images_dir if images_dir.is_dir() else None,
                                       remove_floor=remove_floor, voxel_size=voxel_size,
                                       time_budget=remaining)
        import open3d as o3d
        if not o3d.io.write_triangle_mesh(str(dest), mesh):
            raise OSError("Open3D could not write the approximate mesh")
        return {"ok": True, "mesh_path": str(dest), "approximate": True, "method": method,
                "input_gaussians": len(value.means), "filtered_points": len(points),
                "vertices": len(mesh.vertices), "triangles": len(mesh.triangles),
                "units": "reconstruction", **artifacts, **stats,
                "note": "Approximate median Gaussian depth of the full splat, fused from registered "
                        "views, not a measured surface. Surfaces within the support-plane band "
                        "and unseen geometry are missing; view-dependent effects can still "
                        "cause errors."}
    import open3d as o3d
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(points.astype(np.float64))
    pc.colors = o3d.utility.Vector3dVector(np.clip(pruned.sh_dc * _C0 + 0.5, 0.0, 1.0).astype(np.float64))
    pc.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamKNN(knn=min(30, len(points) - 1)))
    try:
        pc.orient_normals_consistent_tangent_plane(min(25, len(points) - 1))
        normal_note = "Normals oriented by tangent-plane consistency."
    except RuntimeError:
        # Some degenerate point sets cannot form the tangent-plane graph.
        pc.orient_normals_towards_camera_location(np.asarray(points, dtype=np.float64).mean(axis=0))
        pc.normals = o3d.utility.Vector3dVector(-np.asarray(pc.normals))
        normal_note = "Tangent-plane orientation failed; normals oriented away from the point centroid."
    if method == "poisson":
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pc, depth=depth)
        density = np.asarray(densities)
        if len(density):
            mesh.remove_vertices_by_mask(density < np.quantile(density, 0.02))
    else:
        distances = np.asarray(pc.compute_nearest_neighbor_distance())
        positive = distances[np.isfinite(distances) & (distances > 0)]
        if not len(positive):
            raise ValueError("Gaussian centers do not have usable spacing")
        spacing = float(np.median(positive))
        mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            pc, o3d.utility.DoubleVector([spacing * 1.5, spacing * 3, spacing * 6]))
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_unreferenced_vertices()
    if not mesh.has_triangles():
        raise ValueError("no triangles could be reconstructed from these Gaussian centers")
    mesh.compute_vertex_normals()
    if not o3d.io.write_triangle_mesh(str(dest), mesh):
        raise OSError("Open3D could not write the approximate mesh")
    result = {"ok": True, "mesh_path": str(dest), "approximate": True, "method": method,
              "input_gaussians": len(value.means), "filtered_points": len(points),
              "vertices": len(mesh.vertices), "triangles": len(mesh.triangles),
              "units": "reconstruction", **artifacts,
              "note": "Approximate surface from Gaussian centers, not a measured surface. " + normal_note}
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def transform_gaussian(cloud: GaussianCloud, transform) -> GaussianCloud:
    """Apply a reconstruction similarity to centers, scales, and orientation.

    Degree-1/2/3 directional SH would also need basis rotation; a nonidentity
    rotation of such a cloud is refused rather than silently corrupting its color.
    """
    value = _check_cloud(cloud)
    scale = float(transform.mm_per_unit)
    R = np.asarray(transform.rotation, dtype=np.float64)
    t = np.asarray(transform.translation_mm, dtype=np.float64)
    if (not math.isfinite(scale) or scale <= 0 or R.shape != (3, 3) or t.shape != (3,)
            or not np.isfinite(R).all() or not np.isfinite(t).all()
            or not np.allclose(R.T @ R, np.eye(3), atol=1e-6, rtol=0)
            or not np.isclose(np.linalg.det(R), 1.0, atol=1e-6, rtol=0)):
        raise ValueError("invalid reconstruction similarity transform")
    identity_rotation = np.allclose(R, np.eye(3), atol=1e-12, rtol=0)
    if not identity_rotation and value.sh_rest is not None and np.any(value.sh_rest != 0):
        raise ValueError("rotating directional spherical harmonics is not supported")
    rotations = value.rotations
    if not identity_rotation:
        # Stable conversion from a proper rotation matrix to wxyz, then left-compose.
        diag = np.diag(R)
        candidates = np.array([1 + np.trace(R), 1 + 2 * diag[0] - np.trace(R),
                               1 + 2 * diag[1] - np.trace(R), 1 + 2 * diag[2] - np.trace(R)])
        j = int(np.argmax(candidates))
        q = np.zeros(4, dtype=np.float64)
        q[j] = math.sqrt(max(float(candidates[j]), 0.0)) * 0.5
        d = 4.0 * q[j]
        if j == 0:
            q[1:] = [(R[2, 1] - R[1, 2]) / d, (R[0, 2] - R[2, 0]) / d, (R[1, 0] - R[0, 1]) / d]
        elif j == 1:
            q[0], q[2], q[3] = (R[2, 1] - R[1, 2]) / d, (R[0, 1] + R[1, 0]) / d, (R[0, 2] + R[2, 0]) / d
        elif j == 2:
            q[0], q[1], q[3] = (R[0, 2] - R[2, 0]) / d, (R[0, 1] + R[1, 0]) / d, (R[1, 2] + R[2, 1]) / d
        else:
            q[0], q[1], q[2] = (R[1, 0] - R[0, 1]) / d, (R[0, 2] + R[2, 0]) / d, (R[1, 2] + R[2, 1]) / d
        w, x, y, z = value.rotations.T
        a, b, c, d = q
        rotations = np.column_stack((a*w-b*x-c*y-d*z, a*x+b*w+c*z-d*y,
                                     a*y-b*z+c*w+d*x, a*z+b*y-c*x+d*w))
    return _check_cloud(GaussianCloud(scale * (value.means @ R.T) + t, scale * value.scales,
                                      rotations, value.opacities, value.sh_dc, value.sh_rest,
                                      value.source_format, value.lossy))

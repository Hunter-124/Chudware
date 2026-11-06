"""Synthetic cross-format fixtures and approximate-surface coverage."""
from __future__ import annotations

import ctypes
import ctypes.util
import gzip
import math
import struct
import sys
import types

import numpy as np
import pytest

from services.formats import splat_io as io


def _cloud(n=3, degree=3):
    means = np.arange(n * 3, dtype=np.float32).reshape(n, 3) * 0.2 - 0.3
    scales = np.tile([0.25, 0.5, 1.0], (n, 1)).astype(np.float32)
    rotations = np.tile([0.7071068, 0.0, 0.0, 0.7071068], (n, 1)).astype(np.float32)
    opacities = np.linspace(0.2, 0.8, n, dtype=np.float32)
    colors = np.tile([-0.5, 0.2, 0.6], (n, 1)).astype(np.float32)
    k = {0: 0, 1: 3, 2: 8, 3: 15}[degree]
    sh = np.arange(n * k * 3, dtype=np.float32).reshape(n, k, 3) / 1000 if k else None
    return io.GaussianCloud(means, scales, rotations, opacities, colors, sh, "synthetic", [])


@pytest.mark.parametrize("degree", [0, 1, 2, 3])
def test_gaussian_ply_roundtrip_and_native_layout(tmp_path, degree):
    original = _cloud(degree=degree)
    path = tmp_path / "cloud.ply"
    io.save_splat(original, path, "ply")
    assert io.detect_ply_kind(path) == "gaussian"
    header = path.read_bytes().split(b"end_header\n", 1)[0]
    assert b"format binary_little_endian 1.0" in header
    assert header.count(b"property float f_rest_") == {0: 0, 1: 9, 2: 24, 3: 45}[degree]
    loaded = io.load_splat(path)
    np.testing.assert_allclose(loaded.means, original.means, atol=1e-6)
    np.testing.assert_allclose(loaded.scales, original.scales, atol=1e-6)
    np.testing.assert_allclose(loaded.rotations, original.rotations, atol=1e-6)
    np.testing.assert_allclose(loaded.opacities, original.opacities, atol=1e-6)
    np.testing.assert_allclose(loaded.sh_dc, original.sh_dc, atol=1e-6)
    if degree:
        np.testing.assert_allclose(loaded.sh_rest, original.sh_rest, atol=1e-6)
    else:
        assert loaded.sh_rest is None


def test_splat_roundtrip_reports_quantization_and_sh_loss(tmp_path):
    original = _cloud(degree=2)
    path = tmp_path / "cloud.splat"
    io.save_splat(original, path, "splat")
    assert len(path.read_bytes()) == 32 * len(original.means)
    assert any("discards" in loss for loss in original.lossy)
    loaded = io.load_splat(path)
    np.testing.assert_allclose(loaded.means, original.means, atol=1e-6)
    np.testing.assert_allclose(loaded.scales, original.scales, atol=1e-6)
    np.testing.assert_allclose(loaded.opacities, original.opacities, atol=1 / 255)
    np.testing.assert_allclose(loaded.sh_dc, original.sh_dc, atol=1 / (255 * 0.2820948))
    assert np.all(np.abs(np.sum(loaded.rotations * original.rotations, axis=1)) > 0.999)
    assert loaded.sh_rest is None
    ply = tmp_path / "converted.ply"
    io.save_splat(loaded, ply, "ply")
    np.testing.assert_allclose(io.load_splat(ply).means, original.means, atol=1e-6)


def test_detect_ply_mesh_points_and_gaussians(tmp_path):
    path = tmp_path / "points.ply"
    path.write_text("ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n")
    assert io.detect_ply_kind(path) == "points"
    with pytest.raises(ValueError, match="not a Gaussian"):
        io.load_splat(path)
    path.write_text("ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n")
    assert io.detect_ply_kind(path) == "mesh"


def test_ascii_and_big_endian_gaussian_ply(tmp_path):
    props = ["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
             "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2"]
    values = [1, 2, 3, 0, 0, math.log(2), math.log(3), 1, 0, 0, 0, 0.1, -0.2, 0.3]
    for encoding in ("ascii", "binary_big_endian"):
        path = tmp_path / f"{encoding}.ply"
        header = ("ply\nformat " + encoding + " 1.0\nelement vertex 1\n" +
                  "".join("property float " + p + "\n" for p in props) + "end_header\n").encode()
        body = (" ".join(map(str, values)) + "\n").encode() if encoding == "ascii" else struct.pack(">14f", *values)
        path.write_bytes(header + body)
        cloud = io.load_splat(path)
        np.testing.assert_allclose(cloud.means[0], [1, 2, 3])
        np.testing.assert_allclose(cloud.scales[0], [1, 2, 3], atol=1e-6)
        assert cloud.opacities[0] == pytest.approx(0.5)


def _fixed24(value):
    return int(value).to_bytes(3, "little", signed=True)


def _quat_smallest_three(q):
    # Independent construction using the upstream format's index/sign/magnitude fields.
    q = np.asarray(q, dtype=float)
    largest = int(np.argmax(np.abs(q)))
    if q[largest] < 0:
        q = -q
    code = largest
    for i in range(4):
        if i != largest:
            sign = int(q[i] < 0)
            magnitude = int(abs(q[i]) / math.sqrt(0.5) * 511 + 0.5)
            code = (code << 10) | (sign << 9) | magnitude
    return struct.pack("<I", code)


def _legacy_spz(version):
    # Two points, degree 1: fixed xyz, alpha, DC, log scale, rotation, SH blocks.
    header = struct.pack("<4sIIBBBB", b"NGSP", version, 2, 1, 8, 0, 0)
    xyz = b"".join(_fixed24(v) for v in (384, -512, 64, -256, 512, -768))
    alpha = bytes([128, 255])
    dc = bytes([128, 64, 255, 200, 100, 40])
    scales = bytes([160, 160, 160, 176, 144, 160])
    q = [0.3, -0.4, 0.1, math.sqrt(0.74)]  # xyzw
    rotations = (bytes([128, 128, 128, 128, 128, 128]) if version == 2 else
                 _quat_smallest_three([0, 0, 0, 1]) + _quat_smallest_three(q))
    sh = bytes(range(120, 138))
    return gzip.compress(header + xyz + alpha + dc + scales + rotations + sh)


@pytest.mark.parametrize("version", [2, 3])
def test_spz_fixed_point_and_versioned_quaternion(tmp_path, version):
    path = tmp_path / "sample.spz"
    path.write_bytes(_legacy_spz(version))
    cloud = io.load_splat(path)
    np.testing.assert_allclose(cloud.means, [[1.5, -2, 0.25], [-1, 2, -3]])
    np.testing.assert_allclose(cloud.scales, [[1, 1, 1], [math.e, 1 / math.e, 1]], rtol=1e-6)
    np.testing.assert_allclose(cloud.opacities, [128 / 255, 1], atol=1e-7)
    np.testing.assert_allclose(cloud.sh_dc[0], (np.array([128, 64, 255]) / 255 - 0.5) / 0.15, atol=1e-6)
    np.testing.assert_allclose(cloud.sh_rest[0], (np.arange(120, 129).reshape(3, 3) - 128) / 128, atol=1e-6)
    if version == 3:
        np.testing.assert_allclose(cloud.rotations[0], [1, 0, 0, 0], atol=1e-6)
        np.testing.assert_allclose(cloud.rotations[1], [math.sqrt(0.74), 0.3, -0.4, 0.1], atol=0.002)
    else:
        np.testing.assert_allclose(cloud.rotations[0], [1, 0, 0, 0], atol=0.005)
    assert any("quantiz" in s.lower() for s in cloud.lossy)


def test_spz_degree_four_reports_discarded_band(tmp_path):
    raw = bytearray(gzip.decompress(_legacy_spz(3)))
    raw[12] = 4  # degree; keep all other blocks and replace the SH block
    raw = raw[:-18] + bytes((i % 256 for i in range(2 * 24 * 3)))
    path = tmp_path / "degree4.spz"
    path.write_bytes(gzip.compress(raw))
    cloud = io.load_splat(path)
    assert cloud.sh_rest.shape == (2, 15, 3)
    np.testing.assert_allclose(cloud.sh_rest[1, 0], (np.arange(72, 75) - 128) / 128, atol=1e-6)
    assert any("degree" in loss.lower() or "band" in loss.lower() for loss in cloud.lossy)


def test_spz_v4_zstd_streams_match_legacy(tmp_path):
    library = ctypes.util.find_library("zstd")
    if not library:
        pytest.skip("SPZ v4 needs the optional system libzstd")
    zstd = ctypes.CDLL(library)
    zstd.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    zstd.ZSTD_compressBound.restype = ctypes.c_size_t
    zstd.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_int]
    zstd.ZSTD_compress.restype = ctypes.c_size_t
    zstd.ZSTD_isError.argtypes = [ctypes.c_size_t]
    zstd.ZSTD_isError.restype = ctypes.c_uint
    legacy = gzip.decompress(_legacy_spz(3))[16:]
    sizes = [18, 2, 6, 6, 8, 18]
    streams, offset = [], 0
    for size in sizes:
        data = legacy[offset:offset + size]
        offset += size
        dst = ctypes.create_string_buffer(zstd.ZSTD_compressBound(size))
        src = ctypes.create_string_buffer(data)
        written = zstd.ZSTD_compress(dst, len(dst), src, size, 1)
        assert not zstd.ZSTD_isError(written)
        streams.append(dst.raw[:written])
    header = struct.pack("<4sIIBBBBI12s", b"NGSP", 4, 2, 1, 8, 0, 6, 32, bytes(12))
    toc = b"".join(struct.pack("<QQ", len(stream), size) for stream, size in zip(streams, sizes))
    current, older = tmp_path / "current.spz", tmp_path / "older.spz"
    current.write_bytes(header + toc + b"".join(streams))
    older.write_bytes(_legacy_spz(3))
    a, b = io.load_splat(current), io.load_splat(older)
    for field in ("means", "scales", "rotations", "opacities", "sh_dc", "sh_rest"):
        np.testing.assert_allclose(getattr(a, field), getattr(b, field), atol=1e-6)


def _ksplat(level):
    # Two independent sections test section offsets and full/partial bucket tables.
    count, degree = 2, 1
    header = bytearray(4096)
    header[0:2] = b"\x00\x01"
    struct.pack_into("<IIIIH", header, 4, 2, 2, count, count, level)
    struct.pack_into("<ff", header, 36, -1.5, 1.5)
    sections, bodies = [], []
    for section_index in range(2):
        sh_values = np.array([-0.8, -0.7, -0.6, 0.1, 0.2, 0.3, 0.6, 0.7, 0.8], dtype=np.float32)
        section = bytearray(1024)
        if level == 0:
            struct.pack_into("<II", section, 0, 1, 1)
            body = struct.pack("<10f4B", section_index + 1, 2, 3, 0.5, 1, 2,
                               1, 0, 0, 0, 20, 100, 200, 128) + sh_values.astype("<f4").tobytes()
        else:
            struct.pack_into("<IIIIfH", section, 0, 1, 1, 256, 1, 5.0, 12)
            struct.pack_into("<IIIIH", section, 24, 32767, 0, 0, 1, degree)
            center = [section_index + 1.0, 2.0, 3.0]
            sh_encoded = (sh_values.astype("<f2").tobytes() if level == 1 else
                          np.floor((sh_values + 1.5) / 3 * 255).clip(0, 255).astype("u1").tobytes())
            body = (struct.pack("<I3f", 1, *center) +
                    struct.pack("<3H7e4B", 32767, 32867, 32567, 0.5, 1, 2,
                                1, 0, 0, 0, 20, 100, 200, 128) + sh_encoded)
        struct.pack_into("<H", section, 40, degree)
        sections.append(section)
        bodies.append(body)
    return bytes(header) + b"".join(sections) + b"".join(bodies)


@pytest.mark.parametrize("level", [0, 1, 2])
def test_ksplat_sections_compression_sh_and_buckets(tmp_path, level):
    path = tmp_path / f"level-{level}.ksplat"
    path.write_bytes(_ksplat(level))
    cloud = io.load_splat(path)
    delta = [0, 100 * 2.5 / 32767, -200 * 2.5 / 32767] if level else [0, 0, 0]
    np.testing.assert_allclose(cloud.means, np.array([[1, 2, 3], [2, 2, 3]]) + delta, atol=1e-5)
    np.testing.assert_allclose(cloud.scales, [[0.5, 1, 2], [0.5, 1, 2]], atol=1e-5)
    np.testing.assert_allclose(cloud.rotations, [[1, 0, 0, 0], [1, 0, 0, 0]], atol=1e-5)
    np.testing.assert_allclose(cloud.opacities, [128 / 255] * 2, atol=1e-6)
    # The file stores each band channel-major; the shared array is coefficient-major.
    expected = np.array([[-0.8, 0.1, 0.6], [-0.7, 0.2, 0.7], [-0.6, 0.3, 0.8]])
    np.testing.assert_allclose(cloud.sh_rest[0], expected, atol=0.013 if level == 2 else 0.001)


def test_malformed_inputs_and_bounds(tmp_path, monkeypatch):
    path = tmp_path / "bad.spz"
    path.write_bytes(_legacy_spz(3)[:-5])
    with pytest.raises((ValueError, OSError, EOFError)):
        io.load_splat(path)
    path.write_bytes(_legacy_spz(3))
    monkeypatch.setattr(io, "MAX_POINTS", 1)
    with pytest.raises(ValueError):
        io.load_splat(path)
    path = tmp_path / "bad.ksplat"
    path.write_bytes(_ksplat(1)[:-1])
    monkeypatch.setattr(io, "MAX_POINTS", 2_000_000)
    with pytest.raises(ValueError):
        io.load_splat(path)
    path = tmp_path / "bad.splat"
    path.write_bytes(b"x" * 33)
    with pytest.raises(ValueError):
        io.load_splat(path)


def test_splat_to_points_opacity_and_colors():
    cloud = _cloud()
    points, colors = io.splat_to_points(cloud, min_opacity=0.5)
    assert len(points) == len(colors) == 2
    np.testing.assert_allclose(points, cloud.means[1:])
    assert colors.dtype == np.uint8
    with pytest.raises(ValueError):
        io.splat_to_points(cloud, min_opacity=1.1)


@pytest.mark.parametrize("method,model,fallback", [
    ("auto", "absent", True), ("tsdf", "missing", True),
    ("auto", "empty", True), ("poisson", "invalid", False),
])
def test_approximate_sphere_mesh_is_mostly_closed(tmp_path, method, model, fallback):
    o3d = pytest.importorskip("open3d")
    # Fibonacci sampling is uniform and deterministic, without a captured dataset.
    n = 1200
    i = np.arange(n, dtype=np.float64) + 0.5
    z = 1 - 2 * i / n
    angle = math.pi * (3 - math.sqrt(5)) * i
    r = np.sqrt(1 - z * z)
    points = np.column_stack((r * np.cos(angle), r * np.sin(angle), z)).astype(np.float32)
    cloud = io.GaussianCloud(points, np.full((n, 3), 0.04, dtype=np.float32),
                             np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32),
                             np.full(n, 0.9, dtype=np.float32), np.zeros((n, 3), dtype=np.float32),
                             None, "synthetic", [])
    out = tmp_path / "sphere.ply"
    sparse_dir = None
    if model in ("empty", "invalid"):
        (tmp_path / "cameras.txt").write_text("1 PINHOLE 100 100 100 100 50 50\n")
        (tmp_path / "images.txt").write_text("" if model == "empty" else "malformed\n")
        sparse_dir = tmp_path
    elif model == "missing":
        sparse_dir = tmp_path / "missing"
    result = io.splat_to_mesh(cloud, out, depth=6, method=method, sparse_dir=sparse_dir)
    assert result["ok"] and result["approximate"] and result["triangles"] > 100
    assert result["method"] == "poisson"
    assert ("fallback_reason" in result) is fallback
    mesh = o3d.io.read_triangle_mesh(str(out))
    verts = np.asarray(mesh.vertices)
    assert np.all(np.ptp(verts, axis=0) > 1.6)
    faces = np.asarray(mesh.triangles)
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [0, 2]])), axis=1)
    _, incidence = np.unique(edges, axis=0, return_counts=True)
    assert np.count_nonzero(incidence == 1) / len(incidence) < 0.15


def test_tsdf_is_default_with_registered_matching_model(tmp_path, monkeypatch):
    from services.vision import splat_depth

    (tmp_path / "cameras.txt").write_text("1 PINHOLE 100 100 100 100 50 50\n")
    (tmp_path / "images.txt").write_text("1 1 0 0 0 0 0 2 1 view.png\n\n")
    captured = {}
    mesh = types.SimpleNamespace(vertices=[1, 2, 3], triangles=[1])

    def fuse(means, scales, rotations, opacity, cameras, images, **kwargs):
        captured.update(cameras=cameras, images=images, means=means, options=kwargs)
        return mesh, {"registered_views": 1, "selected_views": 1, "integrated_views": 1,
                      "voxel_size": .1}

    monkeypatch.setattr(splat_depth, "fuse_splat_depth", fuse)
    monkeypatch.setitem(sys.modules, "open3d", types.SimpleNamespace(
        io=types.SimpleNamespace(write_triangle_mesh=lambda path, value: value is mesh)))
    result = io.splat_to_mesh(_cloud(80, degree=0), tmp_path / "mesh.ply",
                              min_opacity=.2, sparse_dir=tmp_path)
    assert result["method"] == "tsdf" and result["approximate"]
    assert result["registered_views"] == result["integrated_views"] == 1
    assert "fallback_reason" not in result
    assert captured["cameras"][1].model == "PINHOLE" and captured["images"][1].camera_id == 1
    # Fusion renders the whole splat (pruning only fixes the region), and that region
    # encloses every pruned subject center.
    assert len(captured["means"]) == result["input_gaussians"] == 80
    lo, hi = captured["options"]["bounds"]
    pruned = io.load_splat(result["pruned_splat_path"]).means
    assert np.all((pruned >= lo) & (pruned <= hi))
    assert 0 < captured["options"]["time_budget"] <= splat_depth.TIME_BUDGET_SECONDS


def test_invalid_registered_model_is_not_silently_replaced_by_poisson(tmp_path):
    (tmp_path / "cameras.txt").write_text("1 SIMPLE_RADIAL 100 100 100 50 50 0.1\n")
    (tmp_path / "images.txt").write_text("1 1 0 0 0 0 0 2 1 view.png\n\n")
    with pytest.raises(ValueError, match="undistorted"):
        io.splat_to_mesh(_cloud(80, degree=0), tmp_path / "mesh.ply",
                         min_opacity=.2, sparse_dir=tmp_path)
    assert not (tmp_path / "mesh.ply").exists()


def test_tsdf_timeout_does_not_publish_or_fall_back(tmp_path, monkeypatch):
    from services.vision import splat_depth

    (tmp_path / "cameras.txt").write_text("1 PINHOLE 100 100 100 100 50 50\n")
    (tmp_path / "images.txt").write_text("1 1 0 0 0 0 0 2 1 view.png\n\n")

    def timeout(*args, **kwargs):
        raise TimeoutError("time budget")

    monkeypatch.setattr(splat_depth, "fuse_splat_depth", timeout)
    output = tmp_path / "mesh.ply"
    with pytest.raises(TimeoutError, match="time budget"):
        io.splat_to_mesh(_cloud(80, degree=0), output, min_opacity=.2, sparse_dir=tmp_path)
    assert not output.exists()


def test_tsdf_rejects_oversized_colmap_model_before_reading(tmp_path):
    with (tmp_path / "cameras.txt").open("wb") as stream:
        stream.truncate(io.MAX_COLMAP_MODEL_BYTES + 1)
    (tmp_path / "images.txt").write_text("")
    with pytest.raises(ValueError, match="bounded TSDF input size"):
        io.splat_to_mesh(_cloud(80, degree=0), tmp_path / "mesh.ply",
                         min_opacity=.2, sparse_dir=tmp_path)


def test_similarity_transforms_splat_and_rejects_unrotated_sh():
    from services.vision.recon_transform import ReconTransform
    cloud = _cloud(degree=0)
    transform = ReconTransform(2.0, [[0, -1, 0], [1, 0, 0], [0, 0, 1]], [3, 4, 5], "test")
    converted = io.transform_gaussian(cloud, transform)
    np.testing.assert_allclose(converted.means, transform.apply(cloud.means), atol=1e-6)
    np.testing.assert_allclose(converted.scales, cloud.scales * 2, atol=1e-6)
    # 90° z rotation composed with another 90° z rotation = 180° z.
    assert abs(np.dot(converted.rotations[0], [0, 0, 0, 1])) > 0.99999
    with pytest.raises(ValueError, match="spherical harmonics"):
        io.transform_gaussian(_cloud(degree=2), transform)
    transform.rotation = np.eye(3).tolist()
    assert io.transform_gaussian(_cloud(degree=3), transform).sh_rest.shape[1] == 15

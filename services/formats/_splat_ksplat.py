"""Decode GaussianSplats3D's KSplat v0.1 without changing its coordinates.

Format and algorithms are derived from GaussianSplats3D by Mark Kellogg:
https://github.com/mkkellogg/GaussianSplats3D/blob/main/src/loaders/SplatBuffer.js
https://github.com/mkkellogg/GaussianSplats3D/blob/main/src/loaders/ply/PlyParserUtils.js
https://github.com/mkkellogg/GaussianSplats3D/blob/main/src/loaders/spz/SpzLoader.js

The MIT License (MIT)

Copyright (c) 2023 Mark Kellogg

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import math
import os
import struct
from typing import NamedTuple

import numpy as np


_HEADER_SIZE = 4096
_SECTION_HEADER_SIZE = 1024
_MAIN_HEADER = struct.Struct("<BB2xIIIIH2x3f2f")
_SECTION_HEADER = struct.Struct("<IIIIfH2xIIIIH")
_SH_C0 = 0.28209479177387814
_FLOAT32_MAX = np.finfo(np.float32).max
_MIN_SCALE = 1.00001e-15
_CHUNK_POINTS = 65536


class _Section(NamedTuple):
    count: int
    degree: int
    stride: int
    bucket_size: int
    bucket_count: int
    block_size: float
    scale_range: int
    full_buckets: int
    partial_buckets: int
    base: int
    data_base: int
    end: int


def _read_exact(src, size: int, description: str) -> bytes:
    data = src.read(size)
    if len(data) != size:
        raise ValueError(f"truncated KSplat {description}")
    return data


def _sections(src, section_count: int, compression: int, file_size: int):
    """Read each header in turn; section payloads follow the entire header table."""
    data_offset = _HEADER_SIZE + section_count * _SECTION_HEADER_SIZE
    for index in range(section_count):
        src.seek(_HEADER_SIZE + index * _SECTION_HEADER_SIZE)
        fields = _SECTION_HEADER.unpack(_read_exact(src, _SECTION_HEADER.size, "section header"))
        (count, capacity, bucket_size, bucket_count, block_size, bucket_bytes,
         range_value, declared_size, full, partial, degree) = fields
        if count != capacity:
            raise ValueError("incomplete KSplat section: loaded and maximum counts differ")
        if degree > 3:
            raise ValueError(f"unsupported KSplat spherical harmonics degree {degree}")
        if not math.isfinite(block_size):
            raise ValueError("non-finite KSplat bucket block size")

        components = 3 * ((degree + 1) ** 2 - 1)
        stride = (44 + 4 * components) if compression == 0 else (
            24 + (2 if compression == 1 else 1) * components
        )
        if compression == 0:
            if any((bucket_size, bucket_count, block_size, bucket_bytes, range_value, full, partial)):
                raise ValueError("uncompressed KSplat section has bucket metadata")
            metadata_size = 0
        else:
            if bucket_count != full + partial or bucket_count > count:
                raise ValueError("invalid KSplat bucket counts")
            if count:
                if not bucket_size or block_size <= 0 or bucket_bytes != 12:
                    raise ValueError("invalid KSplat bucket dimensions")
                if full * bucket_size > count or partial > count - full * bucket_size:
                    raise ValueError("invalid KSplat bucket occupancy")
            elif bucket_count or bucket_bytes not in (0, 12) or block_size < 0:
                raise ValueError("invalid empty KSplat section")
            metadata_size = partial * 4 + bucket_count * bucket_bytes

        storage_size = metadata_size + count * stride
        # GaussianSplats3D's uncompressed preallocator writes zero here, even
        # for nonempty sections; the actual layout is calculated from counts.
        if declared_size not in (0, storage_size):
            raise ValueError("KSplat section storage size does not match its layout")
        end = data_offset + storage_size
        if end > file_size:
            raise ValueError("truncated KSplat section data")
        yield _Section(count, degree, stride, bucket_size, bucket_count, block_size,
                       range_value or 32767, full, partial, data_offset,
                       data_offset + metadata_size, end)
        data_offset = end
    if data_offset != file_size:
        raise ValueError("unexpected trailing KSplat data")


def _view(records: bytes, count: int, stride: int, offset: int, dtype, width: int):
    dtype = np.dtype(dtype)
    return np.ndarray((count, width), dtype=dtype, buffer=records, offset=offset,
                      strides=(stride, dtype.itemsize))


def _bucket_data(src, section: _Section):
    src.seek(section.base)
    lengths = np.frombuffer(_read_exact(src, section.partial_buckets * 4, "bucket lengths"), dtype="<u4")
    if lengths.size and (np.any(lengths == 0) or np.any(lengths >= section.bucket_size)):
        raise ValueError("invalid KSplat partial bucket length")
    ends = np.cumsum(lengths, dtype=np.uint64)
    partial_points = int(ends[-1]) if ends.size else 0
    if section.full_buckets * section.bucket_size + partial_points != section.count:
        raise ValueError("KSplat bucket lengths do not match section splat count")
    centers = np.frombuffer(_read_exact(src, section.bucket_count * 12, "bucket centers"),
                            dtype="<f4").reshape(-1, 3)
    if not np.isfinite(centers).all():
        raise ValueError("non-finite KSplat bucket center")
    return ends, centers


def decode_ksplat(path, *, max_points, max_bytes):
    """Return canonical GaussianCloud fields from an unwrapped .ksplat file.

    Input and decoded float32 array sizes are independently limited by
    ``max_bytes``. Payloads are read in chunks; bucket metadata is bounded by
    the validated point count. Incomplete progressive-download buffers are not
    complete KSplat files and are rejected rather than treating capacity as data.
    """
    if not isinstance(max_points, int) or max_points < 0:
        raise ValueError("max_points must be a nonnegative integer")
    if not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")

    with open(path, "rb") as src:
        stat = os.fstat(src.fileno())
        file_size = stat.st_size
        if file_size > max_bytes:
            raise ValueError("KSplat file exceeds max_bytes")
        if file_size < _HEADER_SIZE:
            raise ValueError("truncated KSplat header")
        (major, minor, max_sections, loaded_sections, max_count, loaded_count,
         compression, center_x, center_y, center_z, sh_min, sh_max) = _MAIN_HEADER.unpack(
            _read_exact(src, _MAIN_HEADER.size, "header")
        )
        if (major, minor) != (0, 1):
            raise ValueError(f"unsupported KSplat version {major}.{minor}")
        if compression not in (0, 1, 2):
            raise ValueError(f"unsupported KSplat compression level {compression}")
        if not all(math.isfinite(v) for v in (center_x, center_y, center_z, sh_min, sh_max)):
            raise ValueError("non-finite KSplat header value")
        # Upstream's JavaScript parser treats each zero endpoint as missing.
        sh_min = sh_min or -1.5
        sh_max = sh_max or 1.5
        if sh_min > sh_max:
            raise ValueError("invalid KSplat spherical harmonics range")
        if loaded_sections != max_sections or loaded_count != max_count:
            raise ValueError("incomplete KSplat: loaded and maximum counts differ")
        if not max_count:
            raise ValueError("KSplat contains no Gaussians")
        if max_count > max_points:
            raise ValueError("KSplat exceeds max_points")
        if max_sections > (file_size - _HEADER_SIZE) // _SECTION_HEADER_SIZE:
            raise ValueError("truncated KSplat section header table")

        count = 0
        max_degree = 0
        degrees = set()
        for section in _sections(src, max_sections, compression, file_size):
            count += section.count
            if count > max_count:
                raise ValueError("KSplat section counts exceed header count")
            if section.count:
                max_degree = max(max_degree, section.degree)
                degrees.add(section.degree)
        if count != max_count:
            raise ValueError("KSplat section counts do not match header count")

        sh_count = (max_degree + 1) ** 2 - 1
        decoded_size = count * (56 + 12 * sh_count)
        if decoded_size > max_bytes:
            raise ValueError("decoded KSplat exceeds max_bytes")
        means = np.empty((count, 3), dtype=np.float32)
        scales = np.empty((count, 3), dtype=np.float32)
        rotations = np.empty((count, 4), dtype=np.float32)
        opacities = np.empty(count, dtype=np.float32)
        sh_dc = np.empty((count, 3), dtype=np.float32)
        sh_rest = np.zeros((count, sh_count, 3), dtype=np.float32) if sh_count else None
        clamped_scale = False
        output_offset = 0

        for section in _sections(src, max_sections, compression, file_size):
            if compression:
                partial_ends, bucket_centers = _bucket_data(src, section)
            src.seek(section.data_base)
            for start in range(0, section.count, _CHUNK_POINTS):
                n = min(_CHUNK_POINTS, section.count - start)
                raw = _read_exact(src, n * section.stride, "splat records")
                dest = slice(output_offset + start, output_offset + start + n)
                if compression == 0:
                    positions = _view(raw, n, section.stride, 0, "<f4", 3)
                    scale_values = _view(raw, n, section.stride, 12, "<f4", 3)
                    quat_values = _view(raw, n, section.stride, 24, "<f4", 4)
                    color_offset, sh_offset, sh_type = 40, 44, "<f4"
                    if not np.isfinite(positions).all():
                        raise ValueError("non-finite KSplat position")
                    means[dest] = positions
                else:
                    packed_positions = _view(raw, n, section.stride, 0, "<u2", 3)
                    scale_values = _view(raw, n, section.stride, 6, "<f2", 3)
                    quat_values = _view(raw, n, section.stride, 12, "<f2", 4)
                    color_offset, sh_offset = 20, 24
                    sh_type = "<f2" if compression == 1 else "u1"
                    indices = np.arange(start, start + n, dtype=np.int64)
                    full_points = section.full_buckets * section.bucket_size
                    bucket_indices = np.empty(n, dtype=np.int64)
                    full_mask = indices < full_points
                    bucket_indices[full_mask] = indices[full_mask] // section.bucket_size
                    bucket_indices[~full_mask] = section.full_buckets + np.searchsorted(
                        partial_ends, indices[~full_mask] - full_points, side="right"
                    )
                    # JavaScript calculates these expressions as doubles before
                    # assigning to output buffers; retain that precision here.
                    positions = packed_positions.astype(np.float64)
                    positions -= section.scale_range
                    positions *= (section.block_size * 0.5 / section.scale_range)
                    positions += bucket_centers[bucket_indices]
                    if not np.isfinite(positions).all() or np.any(np.abs(positions) > _FLOAT32_MAX):
                        raise ValueError("invalid decoded KSplat position")
                    means[dest] = positions

                if not np.isfinite(scale_values).all() or np.any(scale_values < 0):
                    raise ValueError("invalid KSplat scale")
                scales[dest] = scale_values
                small = scales[dest] <= 1e-15
                if small.any():
                    scales[dest][small] = _MIN_SCALE
                    clamped_scale = True

                # On disk and in the 3DGS representation the quaternion is wxyz.
                quat = quat_values.astype(np.float64)
                if not np.isfinite(quat).all():
                    raise ValueError("non-finite KSplat quaternion")
                norms = np.sqrt(np.sum(quat * quat, axis=1))
                if np.any(norms == 0):
                    raise ValueError("zero KSplat quaternion")
                rotations[dest] = quat / norms[:, None]

                rgba = _view(raw, n, section.stride, color_offset, "u1", 4)
                sh_dc[dest] = (rgba[:, :3].astype(np.float32) / 255.0 - 0.5) / _SH_C0
                opacities[dest] = rgba[:, 3].astype(np.float32) / 255.0

                if section.degree:
                    component_size = np.dtype(sh_type).itemsize
                    coefficient_base = 0
                    for band in range(1, section.degree + 1):
                        width = 2 * band + 1
                        packed_band = np.ndarray(
                            (n, 3, width), dtype=sh_type, buffer=raw,
                            offset=sh_offset + coefficient_base * 3 * component_size,
                            strides=(section.stride, width * component_size, component_size),
                        )
                        if compression == 2:
                            band_values = packed_band.astype(np.float64)
                            band_values *= (sh_max - sh_min) / 255.0
                            band_values += sh_min
                        else:
                            band_values = packed_band
                        if not np.isfinite(band_values).all() or np.any(np.abs(band_values) > _FLOAT32_MAX):
                            raise ValueError("invalid KSplat spherical harmonics")
                        sh_rest[dest, coefficient_base:coefficient_base + width, :] = band_values.transpose(0, 2, 1)
                        coefficient_base += width
            output_offset += section.count

        after = os.fstat(src.fileno())
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("KSplat file changed while reading")

    lossy = [
        "KSplat stores DC color and opacity as 8-bit RGBA; the original values may have been quantized or clipped.",
        "KSplat scene-center metadata is not represented; positions remain in their source coordinates.",
    ]
    if compression:
        lossy.append("KSplat positions use 16-bit bucket offsets and scales and rotations use float16.")
    if max_degree and compression == 1:
        lossy.append("KSplat higher spherical harmonics use float16.")
    if max_degree and compression == 2:
        lossy.append("KSplat higher spherical harmonics use 8-bit range quantization and may have been clipped.")
    if len(degrees) > 1:
        lossy.append("Higher spherical harmonics absent from lower-degree sections are zero-filled.")
    if max_degree == 3:
        lossy.append("GaussianSplats3D's KSplat encoder writes only the first two SH bands, so its degree-three output may already omit the third band.")
    if clamped_scale:
        lossy.append("Zero or tiny KSplat scales were clamped to 1.00001e-15 for the native renderer.")
    return {"means": means, "scales": scales, "rotations": rotations,
            "opacities": opacities, "sh_dc": sh_dc, "sh_rest": sh_rest,
            "source_format": "ksplat", "lossy": lossy}

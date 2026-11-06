"""Decode Niantic SPZ Gaussian splats without changing their coordinate frame.

SPZ normally uses RUB (right, up, back), but the upstream writer also allows
an unspecified source convention and an extension can describe another one.
The values returned here are the values physically stored in the file: no axis
flip, camera convention, unit conversion, or orientation is inferred. The
cloud stores wxyz quaternions, linear scales, activated opacity, and 3DGS SH
coefficients with RGB as the fastest-changing axis.

Based on the MIT-licensed Niantic SPZ implementation, especially:
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/src/cc/load-spz.cc
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/src/cc/splat-utils.h
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/extensions/README.md
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/extensions/cc/safe-orbit-camera-adobe.cc
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/extensions/cc/coordinate-system-adobe.cc
https://github.com/nianticlabs/spz/blob/affd0ecea7fbb4c265ee119475af7ee5b2997482/LICENSE

Copyright (c) 2024 Niantic Labs
Copyright (c) 2025 Niantic Labs
Copyright (c) 2025 Adobe Inc.
Copyright (c) 2026 Adobe Inc.

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

import ctypes
import ctypes.util
from functools import lru_cache
import math
import os
import stat
import struct
import zlib

import numpy as np


_MAGIC = b"NGSP"
_LEGACY_HEADER = struct.Struct("<4sIIBBBB")
_V4_HEADER = struct.Struct("<4sIIBBBBI12s")
_EXTENSION_HEADER = struct.Struct("<II")
_TOC_ENTRY = struct.Struct("<QQ")
_SH_DIMENSIONS = (0, 3, 8, 15, 24)
_COORDINATES = (
    "unspecified", "LDB", "RDB", "LUB", "RUB", "LDF", "RDF", "LUF", "RUF",
    "LFD", "RFD", "LFU", "RFU", "LBD", "RBD", "LBU", "RBU",
)
_SAFE_ORBIT = 0xADBE0002
_COORDINATE_SYSTEM = 0xADBE0003
_CHUNK = 64 * 1024


class _BoundedSource:
    """Enforce the compressed-byte budget even if a file grows while reading."""

    def __init__(self, source, limit):
        self.source = source
        self.remaining = limit

    def read(self, size):
        data = self.source.read(min(size, self.remaining + 1))
        if len(data) > self.remaining:
            raise ValueError("SPZ compressed input byte limit exceeded")
        self.remaining -= len(data)
        return data


class _GzipReader:
    """Single-member gzip reader whose individual inflate output is bounded."""

    def __init__(self, source):
        self.source = source
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.pending = b""

    def read(self, size):
        out = bytearray()
        while len(out) < size and not self.decoder.eof:
            if not self.pending:
                self.pending = self.source.read(_CHUNK)
            try:
                part = self.decoder.decompress(
                    self.pending, min(_CHUNK, size - len(out))
                )
            except zlib.error as exc:
                raise ValueError(f"invalid SPZ gzip stream: {exc}") from exc
            self.pending = self.decoder.unconsumed_tail
            out.extend(part)
            if not part and not self.pending and not self.decoder.eof:
                # A zero-length compressed read with no output means the gzip
                # member (including its checksum/trailer) ended prematurely.
                if not self.source_has_more():
                    raise ValueError("truncated SPZ gzip stream")
        return out

    def source_has_more(self):
        self.pending = self.source.read(_CHUNK)
        return bool(self.pending)

    def finish(self):
        # Reading once more consumes and validates the gzip CRC and trailer.
        if self.read(1):
            raise ValueError("unexpected data after SPZ packed attributes")
        if self.decoder.unused_data or self.source.read(1):
            raise ValueError("unexpected trailing bytes after SPZ gzip member")


def _read_exact(source, size, label):
    data = source.read(size)
    if len(data) != size:
        raise ValueError(f"truncated SPZ {label}")
    return data


def _layout(version, count, degree, fractional_bits, max_points, max_bytes, header_bytes):
    if count == 0 or count > max_points:
        raise ValueError("invalid or oversized SPZ Gaussian count")
    if degree >= len(_SH_DIMENSIONS):
        raise ValueError(f"unsupported SPZ spherical-harmonic degree {degree}")
    # Upstream's fixed-point decoder uses a signed 32-bit shift. Version 1
    # stores half-float positions and does not use the fractional-bits field.
    if version != 1 and fractional_bits > 30:
        raise ValueError("invalid SPZ fixed-point fractional-bits value")
    dimensions = _SH_DIMENSIONS[degree]
    sizes = (
        count * (6 if version == 1 else 9),
        count,
        count * 3,
        count * 3,
        count * (3 if version < 3 else 4),
        count * dimensions * 3,
    )
    if header_bytes + sum(sizes) > max_bytes:
        raise ValueError("SPZ decompressed byte limit exceeded")
    # The returned arrays are float32, including up to 15 RGB SH triples.
    output_size = count * (14 + 3 * min(dimensions, 15)) * 4
    if output_size > max_bytes:
        raise ValueError("SPZ decoded Gaussian byte limit exceeded")
    return dimensions, sizes


def _read_extensions(source, limit, *, exact, lossy):
    """Read known legacy/v4 ILV records; unknown records may change decoding."""
    used = 0
    records = 0
    coordinate = None
    while not exact or used < limit:
        header = source.read(_EXTENSION_HEADER.size)
        if not header:
            break
        if len(header) != _EXTENSION_HEADER.size:
            raise ValueError("truncated SPZ extension header")
        used += len(header)
        if used > limit:
            raise ValueError("SPZ extension byte limit exceeded")
        kind, length = _EXTENSION_HEADER.unpack(header)
        if length > limit - used:
            raise ValueError("SPZ extension exceeds its available byte limit")
        if kind == _SAFE_ORBIT:
            if length != 12:
                raise ValueError("invalid SPZ safe-orbit extension length")
            values = struct.unpack("<fff", _read_exact(source, length, "safe-orbit extension"))
            if not all(math.isfinite(value) for value in values):
                raise ValueError("nonfinite SPZ safe-orbit extension")
            message = "SPZ safe-orbit camera limits are not represented in the Gaussian cloud"
            if message not in lossy:
                lossy.append(message)
        elif kind == _COORDINATE_SYSTEM:
            if length != 4:
                raise ValueError("invalid SPZ coordinate-system extension length")
            value = struct.unpack("<I", _read_exact(source, length, "coordinate-system extension"))[0]
            if value >= len(_COORDINATES):
                raise ValueError(f"unsupported SPZ coordinate-system value {value}")
            if coordinate is not None and coordinate != value:
                raise ValueError("conflicting SPZ coordinate-system extensions")
            if coordinate is None:
                lossy.append(
                    f"SPZ coordinate-system metadata ({_COORDINATES[value]}) is not represented; "
                    "Gaussian values remain in the stored frame without transformation"
                )
            coordinate = value
        else:
            raise ValueError(f"unsupported SPZ extension 0x{kind:08x}; it may affect decoding")
        used += length
        records += 1
    if exact and used != limit:
        raise ValueError("truncated SPZ extension block")
    if not records:
        raise ValueError("SPZ extension flag is set but no extension is present")
    return used


def _decode_rotations(data, count, version):
    rotations = np.zeros((count, 4), dtype=np.float32)
    if version < 3:
        xyz = np.frombuffer(data, dtype=np.uint8).reshape(count, 3)
        rotations[:, 1:] = xyz
        rotations[:, 1:] *= 1.0 / 127.5
        rotations[:, 1:] -= 1.0
        squared = np.sum(rotations[:, 1:] * rotations[:, 1:], axis=1)
        rotations[:, 0] = np.sqrt(np.maximum(0.0, 1.0 - squared))
    else:
        # The top two bits select the omitted component in upstream xyzw
        # order. Its three 10-bit sign/magnitude fields are read in reverse
        # component order from the least-significant bits of the LE word.
        words = np.frombuffer(data, dtype="<u4").copy()
        largest = words >> 30
        squared = np.zeros(count, dtype=np.float32)
        for index in range(3, -1, -1):
            selected = largest != index
            parts = words[selected]
            component = (parts & 0x1FF).astype(np.float32)
            component *= math.sqrt(0.5) / 511.0
            np.negative(component, out=component, where=(parts & 0x200) != 0)
            rotations[selected, (index + 1) % 4] = component
            squared[selected] += component * component
            words[selected] = parts >> 10
        if np.any(squared > 1.0):
            raise ValueError("invalid SPZ smallest-three quaternion")
        omitted = np.sqrt(1.0 - squared)
        for index in range(4):
            selected = largest == index
            rotations[selected, (index + 1) % 4] = omitted[selected]
    # The older xyz encoding can quantize a unit quaternion slightly outside
    # the unit sphere. Upstream clamps its reconstructed w to zero; normalize
    # the resulting quaternion to meet the interchange contract.
    norm = np.sqrt(np.sum(rotations * rotations, axis=1))
    rotations /= norm[:, None]
    return rotations


def _decode_arrays(read_field, version, count, dimensions, fractional_bits, sizes):
    position_data = read_field(sizes[0], "positions")
    if version == 1:
        means = np.frombuffer(position_data, dtype="<f2").astype(np.float32).reshape(count, 3)
        if not np.isfinite(means).all():
            raise ValueError("nonfinite SPZ position")
    else:
        position_bytes = np.frombuffer(position_data, dtype=np.uint8).reshape(count, 3, 3)
        fixed = position_bytes[:, :, 0].astype(np.int32)
        fixed |= position_bytes[:, :, 1].astype(np.int32) << 8
        fixed |= position_bytes[:, :, 2].astype(np.int32) << 16
        fixed = (fixed << 8) >> 8  # sign-extend 24-bit two's complement
        means = fixed.astype(np.float32)
        means *= 2.0 ** -fractional_bits
    del position_data

    opacities = np.frombuffer(read_field(sizes[1], "opacities"), dtype=np.uint8).astype(np.float32)
    opacities *= 1.0 / 255.0

    sh_dc = np.frombuffer(read_field(sizes[2], "DC colors"), dtype=np.uint8).astype(np.float32)
    sh_dc = sh_dc.reshape(count, 3)
    sh_dc *= 1.0 / 255.0
    sh_dc -= 0.5
    sh_dc /= 0.15

    scales = np.frombuffer(read_field(sizes[3], "scales"), dtype=np.uint8).astype(np.float32)
    scales = scales.reshape(count, 3)
    scales *= 1.0 / 16.0
    scales -= 10.0
    np.exp(scales, out=scales)

    rotations = _decode_rotations(read_field(sizes[4], "rotations"), count, version)

    sh_rest = None
    if dimensions:
        packed_sh = np.frombuffer(read_field(sizes[5], "spherical harmonics"), dtype=np.uint8)
        packed_sh = packed_sh.reshape(count, dimensions, 3)
        sh_rest = packed_sh[:, :15, :].astype(np.float32)
        sh_rest -= 128.0
        sh_rest *= 1.0 / 128.0

    return {
        "means": means,
        "scales": scales,
        "rotations": rotations,
        "opacities": opacities,
        "sh_dc": sh_dc,
        "sh_rest": sh_rest,
        "source_format": "spz",
    }


def _losses(version, degree, flags):
    position_encoding = "float16" if version == 1 else "24-bit fixed-point"
    result = [
        f"SPZ v{version} uses quantized {position_encoding} positions and quantized "
        "rotations, log scales, opacity and DC color; original precision and clipped values cannot be recovered"
    ]
    if degree:
        result.append("SPZ spherical-harmonic coefficients are quantized and may have been clipped")
    if degree == 4:
        result.append("SPZ degree-4 spherical-harmonic band was omitted; only degrees 0 through 3 are supported")
    if flags & 1:
        result.append("SPZ antialiasing flag is not represented in the Gaussian cloud")
    return result


def _legacy(source, max_points, max_bytes):
    stream = _GzipReader(source)
    header = _read_exact(stream, _LEGACY_HEADER.size, "header")
    magic, version, count, degree, fractional_bits, flags, reserved = _LEGACY_HEADER.unpack(header)
    if magic != _MAGIC:
        raise ValueError("invalid SPZ packed-header magic")
    if version not in (1, 2, 3):
        raise ValueError(f"unsupported gzip SPZ version {version}")
    if flags & ~3 or reserved:
        raise ValueError("unsupported SPZ header flags or reserved fields")
    dimensions, sizes = _layout(
        version, count, degree, fractional_bits, max_points, max_bytes, _LEGACY_HEADER.size
    )
    lossy = _losses(version, degree, flags)
    result = _decode_arrays(
        lambda size, label: _read_exact(stream, size, label),
        version, count, dimensions, fractional_bits, sizes,
    )
    if flags & 2:
        _read_extensions(
            stream, max_bytes - _LEGACY_HEADER.size - sum(sizes), exact=False, lossy=lossy
        )
    stream.finish()
    result["lossy"] = lossy
    return result


@lru_cache(maxsize=1)
def _system_zstd():
    """Use a system Zstandard only when Python lacks its optional zstd module."""
    name = ctypes.util.find_library("zstd")
    if not name:
        raise ValueError("SPZ v4 requires Python compression.zstd or a system libzstd")
    try:
        library = ctypes.CDLL(name)
        library.ZSTD_decompress.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t
        ]
        library.ZSTD_decompress.restype = ctypes.c_size_t
        library.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
        library.ZSTD_isError.argtypes = [ctypes.c_size_t]
        library.ZSTD_isError.restype = ctypes.c_uint
        library.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        library.ZSTD_getErrorName.restype = ctypes.c_char_p
    except (OSError, AttributeError) as exc:
        raise ValueError("SPZ v4 requires a usable system libzstd (version 1.4 or later)") from exc
    return library


def _decompress_zstd(data, expected, max_bytes):
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        parameter = zstd.DecompressionParameter.window_log_max
        lower, upper = parameter.bounds()
        window_log = max(lower, min(upper, max_bytes.bit_length()))
        try:
            decoder = zstd.ZstdDecompressor(options={parameter: window_log})
            decoded = decoder.decompress(data, max_length=expected + 1)
            if not decoder.eof and not decoder.needs_input and len(decoded) <= expected:
                decoded += decoder.decompress(b"", max_length=1)
        except (zstd.ZstdError, EOFError) as exc:
            raise ValueError(f"invalid SPZ Zstandard stream: {exc}") from exc
        if len(decoded) != expected or not decoder.eof or decoder.unused_data:
            raise ValueError("SPZ Zstandard stream size or frame boundary mismatch")
        return decoded

    library = _system_zstd()
    source = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p)
    frame_size = library.ZSTD_findFrameCompressedSize(source, len(data))
    if library.ZSTD_isError(frame_size) or frame_size != len(data):
        raise ValueError("invalid SPZ Zstandard frame boundary")
    decoded = bytearray(expected)
    destination = (ctypes.c_ubyte * expected).from_buffer(decoded)
    result = library.ZSTD_decompress(destination, expected, source, len(data))
    if library.ZSTD_isError(result):
        error = library.ZSTD_getErrorName(result).decode("utf-8", errors="replace")
        raise ValueError(f"invalid SPZ Zstandard stream: {error}")
    if result != expected:
        raise ValueError("SPZ Zstandard stream size mismatch")
    return decoded


def _v4(source, source_size, max_points, max_bytes):
    header = _read_exact(source, _V4_HEADER.size, "v4 header")
    magic, version, count, degree, fractional_bits, flags, streams, toc_offset, reserved = (
        _V4_HEADER.unpack(header)
    )
    if magic != _MAGIC or version != 4:
        raise ValueError(f"unsupported plaintext SPZ version {version}")
    if flags & ~3 or any(reserved):
        raise ValueError("unsupported SPZ v4 header flags or reserved fields")
    expected_streams = 5 if degree == 0 else 6
    if streams != expected_streams:
        raise ValueError("invalid SPZ v4 attribute-stream count")
    toc_size = streams * _TOC_ENTRY.size
    if toc_offset < _V4_HEADER.size or toc_offset + toc_size > source_size:
        raise ValueError("invalid SPZ v4 table-of-contents offset")
    dimensions, sizes = _layout(
        version, count, degree, fractional_bits, max_points, max_bytes, toc_offset + toc_size
    )
    lossy = _losses(version, degree, flags)
    extension_size = toc_offset - _V4_HEADER.size
    if flags & 2:
        _read_extensions(source, extension_size, exact=True, lossy=lossy)
    elif extension_size:
        raise ValueError("SPZ v4 has unflagged extension bytes")
    toc = _read_exact(source, toc_size, "v4 table of contents")
    entries = [_TOC_ENTRY.unpack_from(toc, i * _TOC_ENTRY.size) for i in range(streams)]
    if any(compressed == 0 or expanded != sizes[i] for i, (compressed, expanded) in enumerate(entries)):
        raise ValueError("invalid SPZ v4 attribute-stream sizes")
    if toc_offset + toc_size + sum(compressed for compressed, _ in entries) != source_size:
        raise ValueError("SPZ v4 compressed stream sizes do not match file size")
    index = 0

    def read_field(size, label):
        nonlocal index
        compressed, expanded = entries[index]
        index += 1
        if expanded != size:
            raise ValueError(f"invalid SPZ v4 {label} stream size")
        data = _read_exact(source, compressed, f"v4 {label} stream")
        return _decompress_zstd(data, expanded, max_bytes)

    result = _decode_arrays(read_field, version, count, dimensions, fractional_bits, sizes)
    if source.read(1):
        raise ValueError("unexpected trailing bytes after SPZ v4 streams")
    result["lossy"] = lossy
    return result


def decode_spz(path, *, max_points, max_bytes):
    """Decode bounded gzip SPZ v1–3 or Zstandard SPZ v4 into GaussianCloud fields.

    Compressed input, aggregate unpacked attributes, and returned float32
    arrays are each bounded by ``max_bytes``. SPZ v4 requires Python 3.14's
    optional ``compression.zstd`` or a separately available system libzstd;
    versions 1–3 use only the standard-library zlib module.
    """
    if max_points <= 0 or max_bytes <= 0:
        raise ValueError("SPZ byte and point limits must be positive")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb", buffering=0) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SPZ input must be a regular file")
        if info.st_size > max_bytes:
            raise ValueError("SPZ compressed input byte limit exceeded")
        prefix = file.read(4)
        file.seek(0)
        source = _BoundedSource(file, max_bytes)
        if prefix.startswith(b"\x1f\x8b"):
            return _legacy(source, max_points, max_bytes)
        if prefix == _MAGIC:
            return _v4(source, info.st_size, max_points, max_bytes)
        raise ValueError("unrecognized SPZ magic (expected gzip or NGSP)")

"""Portable modern-SolidWorks tessellation parity tests."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from services.formats.formats_solidworks import _portable_tessellations


def _u32(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}I", *values)


def _face(points) -> bytes:
    out = bytearray(_u32(4, 8, 2, 1, 3))
    for values in (points, [(0.0, 0.0, 1.0)] * 3):
        out += _u32(12, 100, 2, 3)
        out += np.asarray(values, dtype="<f4").tobytes()
    out += _u32(12, 100, 2, 0) * 2
    return bytes(out)


def _tetra() -> bytes:
    s = 0.01
    a, b, c, d = (0, 0, 0), (s, 0, 0), (0, s, 0), (0, 0, s)
    return b"".join(_face(p) for p in ((a, c, b), (a, b, d), (a, d, c), (b, c, d)))


def _chunk(name: str, payload: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=-15)
    compressed = compressor.compress(payload) + compressor.flush()
    encoded = bytes(((c << 4) | (c >> 4)) & 0xFF for c in name.encode("ascii"))
    return (b"\0" * 4 + b"\x14\0\x06\0\x08\0" + b"\xfd\0\0\0" +
            _u32(65536, len(compressed), len(payload), len(encoded)) + encoded + compressed)


def _write(tmp_path: Path, assembly: bool) -> Path:
    data = bytearray(15)
    data[7] = 4
    if assembly:
        block = (_u32(0) + b"TETRA-ID" + _u32(4, 0, 0, 1, 0, 0, 0x00201896, 0, 0, 0, 0) +
                 _tetra())
        assert len(block) > 56
        data += _chunk("FaceTessellations/000-000-001", block)
        xml = b"""<?xml version="1.0"?><swSolidWorks><swHeader>
<swFile id="pfile" swDocType="PART"/><swFile id="afile" swDocType="ASSEMBLY"/>
</swHeader><swModelList><swModel id="part" swName="Tetra" swFileRef="pfile"></swModel>
<swModel id="root" swName="Fixture" swFileRef="afile">
<swReference swName="Tetra" swReferenceNumber="1" swModelRef="part" swSuppressed="NO"
 swTransform="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>
<swReference swName="Tetra" swReferenceNumber="2" swModelRef="part" swSuppressed="NO"
 swTransform="1 0 0 0 0 1 0 0 0 0 1 0 0.02 0 0 1"/>
</swModel></swModelList><swConfiguration swName="Default" swModelRef="root"
 swMostRecentConfiguration="YES"/></swSolidWorks>"""
        data += _chunk("swXmlContents/COMPINSTANCETREE", xml)
        path = tmp_path / "fixture.SLDASM"
    else:
        data += _chunk("Contents/DisplayLists", _tetra())
        path = tmp_path / "fixture.SLDPRT"
    path.write_bytes(data)
    return path


def test_modern_part_recovers_saved_triangle_strips(tmp_path):
    bodies = _portable_tessellations(_write(tmp_path, False))
    assert len(bodies) == 1
    name, mesh = bodies[0]
    assert name == "fixture"
    assert mesh.triangle_count == 4
    assert mesh.volume == pytest.approx(1000.0 / 6.0, rel=1e-5)


def test_modern_assembly_preserves_instances_and_row_vector_transforms(tmp_path):
    bodies = _portable_tessellations(_write(tmp_path, True))
    assert [name for name, _ in bodies] == ["Tetra-1", "Tetra-2"]
    centers = [mesh.verts.mean(axis=0) for _, mesh in bodies]
    assert centers[1][0] - centers[0][0] == pytest.approx(20.0, abs=1e-5)
    assert all(mesh.triangle_count == 4 for _, mesh in bodies)

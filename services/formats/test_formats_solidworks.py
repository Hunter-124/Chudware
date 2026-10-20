"""Portable modern-SolidWorks tessellation parity tests."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from services.formats.formats_solidworks import _portable_tessellations
from services.formats import formats_solidworks as sw


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


def _container(*chunks: bytes) -> bytes:
    header = bytearray(15)
    header[7] = 4
    return bytes(header) + b"".join(chunks)


def _block(ordinal: int = 0) -> bytes:
    return (_u32(ordinal) + b"TETRA-ID" +
            _u32(4, 0, 0, 1, 0, 0, 0x00201896, 0, 0, 0, 0) + _tetra())


def _reference(target: str, number: int, shifted: bool = False,
               suppressed: bool = False) -> str:
    return (f'<swReference swName="Tetra" swReferenceNumber="{number}" '
            f'swModelRef="{target}" swSuppressed="{"YES" if suppressed else "NO"}" '
            f'swTransform="1 0 0 0 0 1 0 0 0 0 1 0 {"0.02" if shifted else "0"} 0 0 1"/>')


def _branching(tmp_path: Path, levels: int, branching: bool = True,
               cycle: bool = False, suppressed: bool = False) -> Path:
    xml = ('<swSolidWorks><swHeader><swFile id="pfile" swDocType="PART"/>'
           '<swFile id="afile" swDocType="ASSEMBLY"/></swHeader><swModelList>'
           '<swModel id="part" swFileRef="pfile"/>')
    for i in range(levels):
        target = f"a{i + 1}" if i + 1 < levels else "a0" if cycle else "part"
        xml += f'<swModel id="a{i}" swFileRef="afile">'
        xml += _reference(target, 1)
        if branching:
            xml += _reference(target, 2, True, suppressed)
        xml += "</swModel>"
    xml += ('</swModelList><swConfiguration swModelRef="a0" '
            'swMostRecentConfiguration="YES"/></swSolidWorks>')
    path = tmp_path / "branching.sldasm"
    path.write_bytes(_container(_chunk("FaceTessellations/000", _block()),
                               _chunk("swXmlContents/COMPINSTANCETREE", xml.encode())))
    return path


def test_stream_admission_uses_aggregate_bytes_and_count(tmp_path, monkeypatch):
    path = tmp_path / "streams.sldasm"
    path.write_bytes(_container(_chunk("FaceTessellations/one", b"a" * 64),
                               _chunk("FaceTessellations/two", b"b" * 64)))
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 128)
    monkeypatch.setattr(sw, "_MAX_STREAMS", 2)
    streams = sw._modern_streams(path, True)
    assert streams == {"FaceTessellations/one": b"a" * 64,
                       "FaceTessellations/two": b"b" * 64}
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 127)
    with pytest.raises(ValueError, match="aggregate expanded byte budget"):
        sw._modern_streams(path, True)
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 128)
    monkeypatch.setattr(sw, "_MAX_STREAMS", 1)
    with pytest.raises(ValueError, match="stream count budget"):
        sw._modern_streams(path, True)
    monkeypatch.setattr(sw, "_MAX_STREAMS", 2)
    monkeypatch.setattr(sw, "_MAX_FILE_BYTES", path.stat().st_size)
    assert len(sw._modern_streams(path, True)) == 2
    monkeypatch.setattr(sw, "_MAX_FILE_BYTES", path.stat().st_size - 1)
    with pytest.raises(ValueError, match="input file byte budget"):
        sw._modern_streams(path, True)


@pytest.mark.parametrize("claim", [0, 1, 4095, 4097])
def test_false_expanded_sizes_are_rejected(tmp_path, claim):
    chunk = bytearray(_chunk("Contents/DisplayLists", b"A" * 4096))
    struct.pack_into("<I", chunk, 0x16, claim)
    path = tmp_path / "claimed.sldprt"
    path.write_bytes(_container(bytes(chunk)))
    with pytest.raises(ValueError):
        sw._modern_streams(path, False)


def test_understated_claim_bounds_actual_inflater_output(tmp_path, monkeypatch):
    chunk = bytearray(_chunk("Contents/DisplayLists", b"A" * 4096))
    struct.pack_into("<I", chunk, 0x16, 1)
    path = tmp_path / "understated.sldprt"
    path.write_bytes(_container(bytes(chunk)))
    real_decoder = zlib.decompressobj
    delivered = []

    class MeasuredDecoder:
        def __init__(self, *args):
            self.decoder = real_decoder(*args)

        def decompress(self, *args):
            result = self.decoder.decompress(*args)
            delivered.append(len(result))
            return result

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", MeasuredDecoder)
    with pytest.raises(ValueError, match="claimed expanded size"):
        sw._modern_streams(path, False)
    assert sum(delivered) == 2  # Only the claimed byte plus the surplus witness, not 4096.


@pytest.mark.parametrize("size", [4096, 65535, 65536, 65537, 131073])
def test_bounded_inflation_preserves_complete_streams(tmp_path, size):
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    path = tmp_path / "stream.sldprt"
    path.write_bytes(_container(_chunk("Contents/DisplayLists", payload)))
    assert sw._modern_streams(path, False)["Contents/DisplayLists"] == payload


def test_duplicate_and_empty_stream_admission(tmp_path, monkeypatch):
    one = _chunk("FaceTessellations/one", b"a" * 64)
    path = tmp_path / "streams.sldasm"
    monkeypatch.setattr(sw, "_MAX_STREAMS", 1)
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 64)
    path.write_bytes(_container(one, one))
    assert len(sw._modern_streams(path, True)) == 1
    empty = bytearray(_chunk("FaceTessellations/empty", b""))
    struct.pack_into("<I", empty, 0x12, 0)
    del empty[30 + len("FaceTessellations/empty"):]
    path.write_bytes(_container(one, bytes(empty)))
    with pytest.raises(ValueError, match="stream count budget"):
        sw._modern_streams(path, True)


def _exact_branch_limits(monkeypatch):
    for setting, value in (("_MAX_INSTANCES", 8), ("_MAX_WORK", 14),
                           ("_MAX_VERTICES", 96), ("_MAX_TRIANGLES", 32),
                           ("_MAX_MESH_BYTES", 96 * 24 + 32 * 12)):
        monkeypatch.setattr(sw, setting, value)


def test_branching_assembly_exact_geometry_boundaries(tmp_path, monkeypatch):
    _exact_branch_limits(monkeypatch)
    bodies = _portable_tessellations(_branching(tmp_path, 3))
    assert len(bodies) == 8
    assert bodies[0][0] == "Tetra-1/Tetra-1/Tetra-1"
    assert all(len(mesh.verts) == 12 and len(mesh.tris) == 4 for _, mesh in bodies)
    assert bodies[-1][1].verts.mean(axis=0)[0] - bodies[0][1].verts.mean(axis=0)[0] == \
        pytest.approx(60.0, abs=1e-5)
    assert not np.shares_memory(bodies[0][1].tris, bodies[-1][1].tris)


@pytest.mark.parametrize(("setting", "value", "reason"), [
    ("_MAX_INSTANCES", 7, "instance count"),
    ("_MAX_WORK", 13, "instance work"),
    ("_MAX_VERTICES", 95, "aggregate vertex"),
    ("_MAX_TRIANGLES", 31, "aggregate triangle"),
    ("_MAX_MESH_BYTES", 96 * 24 + 32 * 12 - 1, "aggregate mesh byte"),
])
def test_branching_over_budget_rejected_before_instance_arrays(
        tmp_path, monkeypatch, setting, value, reason):
    _exact_branch_limits(monkeypatch)
    monkeypatch.setattr(sw, setting, value)
    path = _branching(tmp_path, 3)
    placed = []
    column_stack = np.column_stack

    def measured_placement(*args, **kwargs):
        placed.append(True)
        return column_stack(*args, **kwargs)

    monkeypatch.setattr(np, "column_stack", measured_placement)
    with pytest.raises(ValueError, match=f"{reason} budget"):
        _portable_tessellations(path)
    assert placed == []


def test_compact_excessive_dag_rejected_without_materializing_leaves(tmp_path):
    # Keep this fixture tiny; the new preflight, not an attempted million-copy import,
    # must reject it. Native standalone test exercises the same production boundary.
    with pytest.raises(ValueError, match="instance count budget"):
        _portable_tessellations(_branching(tmp_path, 20))


def test_cycle_depth_and_suppression_survive_dag_admission(tmp_path):
    with pytest.raises(ValueError, match="cyclic or too deep"):
        _portable_tessellations(_branching(tmp_path, 2, False, True))
    with pytest.raises(ValueError, match="cyclic or too deep"):
        _portable_tessellations(_branching(tmp_path, 66, False))
    assert len(_portable_tessellations(_branching(tmp_path, 65, False))) == 1
    assert len(_portable_tessellations(_branching(tmp_path, 3, suppressed=True))) == 1


def test_memoized_models_retain_depth_guard_on_longer_paths(tmp_path):
    path = _branching(tmp_path, 65, False)
    xml = sw._modern_streams(path, True)["swXmlContents/COMPINSTANCETREE"].decode()
    xml = xml.replace('<swConfiguration swModelRef="a0"',
                      '<swConfiguration swModelRef="root"')
    root = ('<swModel id="root" swFileRef="afile">' +
            _reference("a60", 1) + _reference("a0", 2) + "</swModel>")
    xml = xml.replace("</swModelList>", root + "</swModelList>")
    path.write_bytes(_container(_chunk("FaceTessellations/000", _block()),
                               _chunk("swXmlContents/COMPINSTANCETREE", xml.encode())))
    with pytest.raises(ValueError, match="cyclic or too deep"):
        _portable_tessellations(path)


@pytest.mark.parametrize("separate_streams", [False, True])
def test_distinct_source_meshes_share_aggregate_geometry_budget(
        tmp_path, monkeypatch, separate_streams):
    xml = ('<swSolidWorks><swFile id="pfile" swDocType="PART"/>'
           '<swFile id="afile" swDocType="ASSEMBLY"/>'
           '<swModel id="p1" swFileRef="pfile"/><swModel id="p2" swFileRef="pfile"/>'
           '<swModel id="root" swFileRef="afile">' +
           _reference("p1", 1) + _reference("p2", 2) +
           '</swModel><swConfiguration swModelRef="root"/></swSolidWorks>')
    path = tmp_path / "sources.sldasm"
    face_chunks = ((_chunk("FaceTessellations/000", _block()),
                    _chunk("FaceTessellations/001", _block())) if separate_streams else
                   (_chunk("FaceTessellations/000", _block() + _block(1)),))
    path.write_bytes(_container(*face_chunks,
                               _chunk("swXmlContents/COMPINSTANCETREE", xml.encode())))
    monkeypatch.setattr(sw, "_MAX_VERTICES", 24)
    assert len(_portable_tessellations(path)) == 2
    monkeypatch.setattr(sw, "_MAX_VERTICES", 23)
    with pytest.raises(ValueError, match="aggregate vertex budget"):
        _portable_tessellations(path)


def test_portable_production_import_preserves_bounded_branching_placements(tmp_path):
    bodies = _portable_tessellations(_branching(tmp_path, 3))
    assert len(bodies) == 8
    assert bodies[0][0] == "Tetra-1/Tetra-1/Tetra-1"
    assert all(mesh.triangle_count == 4 for _, mesh in bodies)
    assert bodies[-1][1].verts.mean(axis=0)[0] - bodies[0][1].verts.mean(axis=0)[0] == \
        pytest.approx(60.0, abs=1e-5)


def test_audit_stream_and_dag_witnesses_use_small_fixed_budgets(tmp_path, monkeypatch):
    path = tmp_path / "aggregate.sldasm"
    path.write_bytes(_container(_chunk("FaceTessellations/one", b"a" * (1024 * 1024)),
                               _chunk("FaceTessellations/two", b"b" * (1024 * 1024))))
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 2 * 1024 * 1024)
    assert sum(map(len, sw._modern_streams(path, True).values())) == 2 * 1024 * 1024
    monkeypatch.setattr(sw, "_MAX_STREAM_BYTES", 2 * 1024 * 1024 - 1)
    with pytest.raises(ValueError, match="aggregate expanded byte budget"):
        sw._modern_streams(path, True)

    dag = _branching(tmp_path, 8)
    monkeypatch.setattr(sw, "_MAX_INSTANCES", 256)
    assert len(_portable_tessellations(dag)) == 256
    monkeypatch.setattr(sw, "_MAX_INSTANCES", 255)
    with pytest.raises(ValueError, match="instance count budget"):
        _portable_tessellations(dag)

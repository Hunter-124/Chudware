"""Deterministic golden-AICM generator (mesh_format.md §4).

Writes box.aicm: a 20 x 10 x 6 box centered at the origin, encoded exactly per AICM v1.
Both the Python writer tests (engine/doc) and the C++ loader tests consume the committed
binary; tests regenerate via generate() and byte-compare. Keep this file byte-stable —
any change to the format is a contract version bump.

Run:  python make_box_aicm.py        (writes box.aicm next to this file)
"""

from __future__ import annotations

import struct
from pathlib import Path

HX, HY, HZ = 10.0, 5.0, 3.0  # half extents: 20 x 10 x 6 box
BODY_ID = 1
BODY_NAME = "Body1"
BODY_COLOR = (0.71, 0.76, 0.82)

# Per-face quads, CCW viewed from outside. Face order: -X, +X, -Y, +Y, -Z, +Z.
_FACES = [
    # (normal, [4 corners])
    ((-1.0, 0.0, 0.0), [(-HX, -HY, -HZ), (-HX, -HY, HZ), (-HX, HY, HZ), (-HX, HY, -HZ)]),
    ((1.0, 0.0, 0.0), [(HX, -HY, -HZ), (HX, HY, -HZ), (HX, HY, HZ), (HX, -HY, HZ)]),
    ((0.0, -1.0, 0.0), [(-HX, -HY, -HZ), (HX, -HY, -HZ), (HX, -HY, HZ), (-HX, -HY, HZ)]),
    ((0.0, 1.0, 0.0), [(-HX, HY, -HZ), (-HX, HY, HZ), (HX, HY, HZ), (HX, HY, -HZ)]),
    ((0.0, 0.0, -1.0), [(-HX, -HY, -HZ), (-HX, HY, -HZ), (HX, HY, -HZ), (HX, -HY, -HZ)]),
    ((0.0, 0.0, 1.0), [(-HX, -HY, HZ), (HX, -HY, HZ), (HX, HY, HZ), (-HX, HY, HZ)]),
]

# 12 box edges as 2-point polylines (curve_type 0 = line).
_C = {
    1: (-HX, -HY, -HZ), 2: (HX, -HY, -HZ), 3: (HX, HY, -HZ), 4: (-HX, HY, -HZ),
    5: (-HX, -HY, HZ), 6: (HX, -HY, HZ), 7: (HX, HY, HZ), 8: (-HX, HY, HZ),
}
_EDGES = [
    (1, 2), (2, 3), (3, 4), (4, 1),      # bottom ring
    (5, 6), (6, 7), (7, 8), (8, 5),      # top ring
    (1, 5), (2, 6), (3, 7), (4, 8),      # verticals
]


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _chunk(fourcc: bytes, payload: bytes) -> bytes:
    assert len(fourcc) == 4
    return fourcc + struct.pack("<Q", len(payload)) + payload


def generate() -> bytes:
    # BODY ------------------------------------------------------------------
    pos: list[float] = []
    nrm: list[float] = []
    idx: list[int] = []
    face_records = []
    for fi, (n, corners) in enumerate(_FACES):
        base = fi * 4
        for c in corners:
            pos.extend(c)
            nrm.extend(n)
        idx.extend([base + 0, base + 1, base + 2, base + 0, base + 2, base + 3])
        face_records.append((fi + 1, fi * 2, 2, 0))  # face_id, first_tri, tri_count, surf=plane

    n_verts = len(pos) // 3
    n_tris = len(idx) // 3
    body = (
        struct.pack("<I", BODY_ID)
        + _str(BODY_NAME)
        + struct.pack("<3f", *BODY_COLOR)
        + struct.pack("<II", n_verts, n_tris)
        + struct.pack(f"<{len(pos)}f", *pos)
        + struct.pack(f"<{len(nrm)}f", *nrm)
        + struct.pack(f"<{len(idx)}I", *idx)
    )

    # FACE ------------------------------------------------------------------
    face = struct.pack("<I", len(face_records))
    for fid, first, count, surf in face_records:
        face += struct.pack("<IIIB", fid, first, count, surf)

    # EDGE ------------------------------------------------------------------
    edge = struct.pack("<I", len(_EDGES))
    pts: list[float] = []
    for ei, (a, b) in enumerate(_EDGES):
        edge += struct.pack("<IIIB", ei + 1, len(pts) // 3, 2, 0)
        pts.extend(_C[a])
        pts.extend(_C[b])
    edge += struct.pack("<I", len(pts) // 3)
    edge += struct.pack(f"<{len(pts)}f", *pts)

    # VERT ------------------------------------------------------------------
    vert = struct.pack("<I", len(_C))
    for vid in sorted(_C):
        vert += struct.pack("<I", vid) + struct.pack("<3f", *_C[vid])

    chunks = [
        _chunk(b"BODY", body),
        _chunk(b"FACE", face),
        _chunk(b"EDGE", edge),
        _chunk(b"VERT", vert),
    ]
    header = b"AICM" + struct.pack("<II", 1, len(chunks))
    return header + b"".join(chunks)


def main() -> None:
    out = Path(__file__).resolve().parent / "box.aicm"
    data = generate()
    out.write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes)")


if __name__ == "__main__":
    main()

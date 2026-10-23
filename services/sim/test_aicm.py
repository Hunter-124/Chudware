"""Tests for services/sim/tessellate.py — AICM writer/parser.

Run: python -m services.sim.test_aicm

House style: plain main() + asserts, no pytest.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from services.sim.tessellate import (
    BodyTess,
    disp_chunk,
    lines_chunk,
    read_aicm,
    scalar_chunk,
    tessellate_body,
    write_aicm,
    write_aicm_file,
)

# Repo-relative path to golden fixture
_FIXTURE = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "box.aicm"


# ---------------------------------------------------------------------------
# Helper: locate the golden-generator and call generate()
# ---------------------------------------------------------------------------
def _golden_generate() -> bytes:
    import sys
    gen_dir = str(Path(__file__).resolve().parents[2] / "contracts" / "fixtures")
    if gen_dir not in sys.path:
        sys.path.insert(0, gen_dir)
    from make_box_aicm import generate  # type: ignore
    return generate()


# ---------------------------------------------------------------------------
# Test 1: parser vs golden binary (box.aicm)
# ---------------------------------------------------------------------------
def test_golden_parse() -> None:
    """read_aicm on the committed golden file must match contract §4 counts."""
    data = _FIXTURE.read_bytes()
    doc = read_aicm(data)

    assert doc["version"] == 1, f"version={doc['version']}"
    assert len(doc["bodies"]) == 1, f"expected 1 body, got {len(doc['bodies'])}"

    body = doc["bodies"][0]
    assert body["n_verts"] == 24, f"n_verts={body['n_verts']}"
    assert body["n_tris"] == 12, f"n_tris={body['n_tris']}"
    assert len(body["faces"]) == 6, f"n_faces={len(body['faces'])}"
    assert len(body["edges"]) == 12, f"n_edges={len(body['edges'])}"
    assert len(body["verts"]) == 8, f"n_verts_brep={len(body['verts'])}"

    # Face ranges must be contiguous and cover all tris
    covered = 0
    for i, fr in enumerate(body["faces"]):
        assert fr["first_tri"] == covered, (
            f"face {i} first_tri={fr['first_tri']} expected {covered}"
        )
        covered += fr["tri_count"]
    assert covered == body["n_tris"], f"face ranges cover {covered} tris != {body['n_tris']}"

    print("test_golden_parse: PASS")


# ---------------------------------------------------------------------------
# Test 2: golden regenerates byte-identical
# ---------------------------------------------------------------------------
def test_golden_byte_stable() -> None:
    """The committed box.aicm must equal generate() output — byte-stable."""
    committed = _FIXTURE.read_bytes()
    generated = _golden_generate()
    assert committed == generated, (
        f"Golden mismatch: committed {len(committed)} bytes, generated {len(generated)} bytes"
    )
    print("test_golden_byte_stable: PASS")


# ---------------------------------------------------------------------------
# Test 3: real shape — Box(20, 10, 6)
# ---------------------------------------------------------------------------
def test_box_tessellation() -> None:
    """Tessellate build123d.Box(20,10,6), write, re-parse, check contract invariants."""
    import build123d as bd
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    box = bd.Box(20, 10, 6)

    bt = tessellate_body(box, body_id=1, name="Body1", color=(0.71, 0.76, 0.82))

    # Basic geometry counts
    assert bt.pos.shape[1] == 3
    assert bt.nrm.shape == bt.pos.shape
    assert bt.idx.shape[1] == 3

    # Face ranges contiguous + cover all tris
    covered = 0
    for fr in bt.faces:
        assert fr.first_tri == covered, f"first_tri={fr.first_tri} expected {covered}"
        covered += fr.tri_count
    assert covered == len(bt.idx), f"face ranges cover {covered} tris, idx has {len(bt.idx)}"

    # Normals unit-length ±1e-3
    norms = np.linalg.norm(bt.nrm, axis=1)
    bad = np.where(np.abs(norms - 1.0) > 1e-3)[0]
    assert len(bad) == 0, f"{len(bad)} normals not unit-length (e.g. idx {bad[0]}, len={norms[bad[0]]:.4f})"

    # Normals point outward: dot(nrm, pos - body_center) > 0 for convex box
    body_center = bt.pos.mean(axis=0)
    dots = np.einsum("ij,ij->i", bt.nrm, bt.pos - body_center)
    bad_out = np.where(dots <= 0)[0]
    assert len(bad_out) == 0, (
        f"{len(bad_out)} vertices have inward-pointing normals "
        f"(e.g. idx {bad_out[0]}, dot={dots[bad_out[0]]:.4f})"
    )

    # Edge count matches TopExp edge-map extent
    BRepMesh_IncrementalMesh(box.wrapped, 0.1, False, 0.5, True).Perform()
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(box.wrapped, TopAbs_EDGE, emap)
    assert len(bt.edges) == emap.Size(), (
        f"edge count {len(bt.edges)} != TopExp extent {emap.Size()}"
    )

    # All face surf_types are 0 (plane) for a box
    for fr in bt.faces:
        assert fr.surf_type == 0, f"face {fr.face_id} surf_type={fr.surf_type} expected 0"

    # Roundtrip write → read
    data = write_aicm([bt])
    doc = read_aicm(data)
    assert len(doc["bodies"]) == 1
    body = doc["bodies"][0]
    assert body["n_verts"] == len(bt.pos)
    assert body["n_tris"] == len(bt.idx)

    # Header check
    assert data[:4] == b"AICM"
    ver, n_chunks = struct.unpack_from("<II", data, 4)
    assert ver == 1
    assert n_chunks == 4  # BODY + FACE + EDGE + VERT

    # Face contiguity in parsed output
    covered = 0
    for fr in body["faces"]:
        assert fr["first_tri"] == covered
        covered += fr["tri_count"]
    assert covered == body["n_tris"]

    print(f"test_box_tessellation: PASS  ({len(bt.pos)} verts, {len(bt.idx)} tris, "
          f"{len(bt.faces)} faces, {len(bt.edges)} edges, {len(bt.verts)} brep verts)")


# ---------------------------------------------------------------------------
# Test 4: filleted + bored box
# ---------------------------------------------------------------------------
def test_complex_shape() -> None:
    """Filleted+bored box — cylinder face must have surf_type 1."""
    import build123d as bd

    # Build a box, fillet some edges, bore a hole
    with bd.BuildPart() as bp:
        bd.Box(30, 20, 10)
        bd.fillet(bp.edges().filter_by(bd.Axis.Z), radius=2)
        bd.Cylinder(radius=4, height=20, mode=bd.Mode.SUBTRACT)

    bt = tessellate_body(bp.part, body_id=2, name="Complex", color=(0.5, 0.6, 0.7))

    # Normals unit-length
    norms = np.linalg.norm(bt.nrm, axis=1)
    bad = np.where(np.abs(norms - 1.0) > 1e-3)[0]
    assert len(bad) == 0, f"{len(bad)} normals not unit-length"

    # At least one cylinder face (surf_type 1)
    surf_types = {fr.surf_type for fr in bt.faces}
    assert 1 in surf_types, f"No cylinder face found; surf_types present: {surf_types}"

    # Face ranges contiguous + cover all tris
    covered = 0
    for fr in bt.faces:
        assert fr.first_tri == covered
        covered += fr.tri_count
    assert covered == len(bt.idx)

    # Parse roundtrip
    doc = read_aicm(write_aicm([bt]))
    body = doc["bodies"][0]
    assert body["n_verts"] == len(bt.pos)
    assert body["n_tris"] == len(bt.idx)

    print(f"test_complex_shape: PASS  ({len(bt.pos)} verts, {len(bt.idx)} tris, "
          f"{len(bt.faces)} faces, surf_types={sorted(surf_types)})")


# ---------------------------------------------------------------------------
# Test 5: scalar / disp / lines chunk roundtrip
# ---------------------------------------------------------------------------
def test_overlay_chunks() -> None:
    """scalar_chunk / disp_chunk / lines_chunk write → read_aicm roundtrip."""
    import build123d as bd

    box = bd.Box(10, 10, 10)
    bt = tessellate_body(box, body_id=99, name="OverlayBox", color=(1.0, 1.0, 1.0))
    n = len(bt.pos)

    # --- scalar_chunk ---
    vals = np.linspace(0.0, 1.0, n, dtype=np.float32)
    sclr = scalar_chunk(99, "von_mises", vals, vmin=0.0, vmax=2.0)

    doc = read_aicm(write_aicm([bt], extra_chunks=[sclr]))
    assert len(doc["scalars"]) == 1
    s = doc["scalars"][0]
    assert s["body_id"] == 99
    assert s["name"] == "von_mises"
    assert abs(s["vmin"] - 0.0) < 1e-6
    assert abs(s["vmax"] - 2.0) < 1e-6
    assert len(s["values"]) == n
    assert np.allclose(s["values"], vals, atol=1e-6)

    # scalar_chunk with NaN + auto vmin/vmax
    vals_nan = vals.copy()
    vals_nan[0] = np.nan
    sclr2 = scalar_chunk(99, "draft", vals_nan)
    doc2 = read_aicm(write_aicm([bt], extra_chunks=[sclr2]))
    s2 = doc2["scalars"][0]
    assert np.isnan(s2["values"][0])
    # vmin/vmax from finite data
    finite_vals = vals_nan[np.isfinite(vals_nan)]
    assert abs(s2["vmin"] - float(finite_vals.min())) < 1e-5
    assert abs(s2["vmax"] - float(finite_vals.max())) < 1e-5

    # --- disp_chunk ---
    vecs = np.random.rand(n, 3).astype(np.float32) * 0.01
    dchunk = disp_chunk(99, 50.0, vecs)

    doc3 = read_aicm(write_aicm([bt], extra_chunks=[dchunk]))
    assert len(doc3["displacements"]) == 1
    d = doc3["displacements"][0]
    assert d["body_id"] == 99
    assert abs(d["scale_hint"] - 50.0) < 1e-6
    assert d["vectors"].shape == (n, 3)
    assert np.allclose(d["vectors"], vecs, atol=1e-6)

    # --- lines_chunk (with and without scalars) ---
    lines = [np.random.rand(5, 3).astype(np.float32), np.random.rand(3, 3).astype(np.float32)]
    lchunk = lines_chunk("streamlines", lines)

    doc4 = read_aicm(write_aicm([bt], extra_chunks=[lchunk]))
    assert len(doc4["lines"]) == 1
    ln = doc4["lines"][0]
    assert ln["name"] == "streamlines"
    assert len(ln["polylines"]) == 2
    assert ln["scalars"] is None
    assert ln["polylines"][0].shape == (5, 3)
    assert ln["polylines"][1].shape == (3, 3)
    assert np.allclose(ln["polylines"][0], lines[0], atol=1e-6)

    # With per-point scalars
    spd = [np.random.rand(5).astype(np.float32), np.random.rand(3).astype(np.float32)]
    lchunk2 = lines_chunk("streamlines_s", lines, scalars=spd)
    doc5 = read_aicm(write_aicm([bt], extra_chunks=[lchunk2]))
    ln2 = doc5["lines"][0]
    assert ln2["scalars"] is not None
    assert len(ln2["scalars"]) == 2
    assert np.allclose(ln2["scalars"][0], spd[0], atol=1e-6)

    print(f"test_overlay_chunks: PASS  ({n} verts; scalar, disp, lines with+without scalars)")


# ---------------------------------------------------------------------------
# Test 6: write_aicm_file path helper
# ---------------------------------------------------------------------------
def test_write_file(tmp_path: Path = None) -> None:
    """write_aicm_file writes a valid parseable file."""
    import tempfile, os
    import build123d as bd

    box = bd.Box(5, 5, 5)
    bt = tessellate_body(box, 1, "Cube", (1.0, 0.0, 0.0))
    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as f:
        tmppath = Path(f.name)
    try:
        write_aicm_file(tmppath, [bt])
        doc = read_aicm(tmppath.read_bytes())
        assert len(doc["bodies"]) == 1
    finally:
        tmppath.unlink(missing_ok=True)
    print("test_write_file: PASS")


# ---------------------------------------------------------------------------
# Test 7: unknown chunk tolerance
# ---------------------------------------------------------------------------
def test_unknown_chunk_skip() -> None:
    """Parser must skip unknown fourccs gracefully."""
    import build123d as bd

    box = bd.Box(5, 5, 5)
    bt = tessellate_body(box, 1, "T", (0.5, 0.5, 0.5))
    base = write_aicm([bt])

    # Inject a fake unknown chunk between BODY groups
    fake_payload = b"\xDE\xAD\xBE\xEF" * 4  # 16 bytes of garbage
    fake_chunk = b"UNKN" + struct.pack("<Q", len(fake_payload)) + fake_payload

    # Re-assemble: header with +1 chunk_count, then real chunks, then fake
    magic = base[:4]
    ver, n_chunks = struct.unpack_from("<II", base, 4)
    rest = base[12:]  # original chunks payload
    new_header = magic + struct.pack("<II", ver, n_chunks + 1)
    mangled = new_header + rest + fake_chunk

    doc = read_aicm(mangled)
    assert len(doc["bodies"]) == 1
    print("test_unknown_chunk_skip: PASS")


# ---------------------------------------------------------------------------
# Test 7b: seam / degenerate edges are NOT drawn (a cylinder has no vertical line)
# ---------------------------------------------------------------------------
def test_seam_edges_not_drawn() -> None:
    """Periodic-surface seam edges and degenerate edges must carry zero polyline points.

    A cylinder (extruded circle) has 3 B-rep edges: 2 circular rims + 1 seam (the vertical line
    where the cylindrical face's u-parametrization wraps). The seam is real topology but NOT a
    visible model edge, so it must be recorded with pt_count==0 (index stays stable, nothing draws).
    A cone adds a degenerate apex edge that must likewise be hidden. A box keeps all 12 edges."""
    import build123d as bd

    # Cylinder: edge records preserved (index stability) but the seam draws nothing.
    cyl = bd.Cylinder(radius=10, height=20)
    btc = tessellate_body(cyl, 1, "Cyl", (0.7, 0.7, 0.8))
    drawn = [e for e in btc.edges if e.pt_count > 0]
    assert len(btc.edges) == 3, f"cylinder edge records = {len(btc.edges)} (expected 3)"
    assert len(drawn) == 2, f"cylinder DREW {len(drawn)} edges (expected 2 rims, seam hidden)"
    # The two drawn edges are the circular rims (curve_type 1 == circle).
    assert all(e.curve_type == 1 for e in drawn), "drawn cylinder edges must be the circular rims"

    # Cone: base circle drawn; seam + degenerate apex hidden.
    cone = bd.Cone(bottom_radius=10, top_radius=0, height=20)
    btk = tessellate_body(cone, 1, "Cone", (0.7, 0.7, 0.8))
    drawn_k = [e for e in btk.edges if e.pt_count > 0]
    assert len(drawn_k) == 1, f"cone DREW {len(drawn_k)} edges (expected 1 base circle)"

    # Box: a non-periodic solid keeps every edge — the filter must not touch real model edges.
    box = bd.Box(10, 20, 30)
    btb = tessellate_body(box, 1, "Box", (0.7, 0.7, 0.8))
    drawn_b = [e for e in btb.edges if e.pt_count > 0]
    assert len(drawn_b) == 12, f"box DREW {len(drawn_b)} edges (expected all 12)"

    print(f"test_seam_edges_not_drawn: PASS  (cyl {len(drawn)}/3, cone {len(drawn_k)}/3, "
          f"box {len(drawn_b)}/12 drawn)")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    print("=== engine.doc.test_aicm ===")
    test_golden_parse()
    test_golden_byte_stable()
    test_box_tessellation()
    test_complex_shape()
    test_overlay_chunks()
    test_write_file()
    test_unknown_chunk_skip()
    test_seam_edges_not_drawn()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()

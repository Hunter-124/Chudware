"""cpp_3dm_parity.py — Python-vs-C++ Rhino .3dm parity (openNURBS via rhino3dm 8.17.0).

The strongest contract in this gate is IMPORT BIT-PARITY: the Python oracle
(engine.doc.formats._import_3dm, which uses the `rhino3dm` wheel) and the C++
core (libaicad.dll: mesh_read_raw) BOTH read the SAME stored openNURBS data. The
Python `rhino3dm` wheel wraps openNURBS; the C++ side links opennurbsStatic built
from the SAME rhino3dm v8.17.0 tag. Reading a STORED ON_Mesh (m_V float32 / m_F
quad-or-tri) is archive-version-stable, so the per-object vertex coords, triangle
indices (after the quad->tri split), names, and object count MUST be bit-identical.

Gates:
  (a) IMPORT bit-parity — for a set of .3dm fixtures (a quad-faced box, a
      multi-object file, an already-triangulated mesh), compare _import_3dm vs the
      C++ mesh_read_raw: EXACT object count, EXACT names, EXACT (float32) vertex
      coords, EXACT triangle indices. The quad->tri split (vi[3]!=vi[2] -> emit a
      second tri (a,c,d)) is the key thing that must match.
  (b) EXPORT round-trip — write a box solid with the C++ core (export_file 3dm),
      then re-import it through the C++ core (mesh_read_raw) and the Python oracle,
      and confirm the mesh survives (triangle count + bbox + body volume). Export
      is NOT byte/mesh-identical to the oracle (the C++ side tessellates a B-rep
      with OCCT, the oracle with build123d), so export is gated as a round-trip.

Run under the RUNTIME venv (it has rhino3dm + build123d):
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_3dm_parity.py

Exit code = number of mismatched cases (0 = full parity).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- fixtures
def write_3dm_fixture(path: str, objects) -> None:
    """Write a .3dm with the Python rhino3dm wheel from explicit
    (name, verts, faces) tuples, where `faces` is a list of 3- or 4-tuples of
    vertex indices (4-tuple = quad). This exercises BOTH the tri path and the
    quad->tri split on the import side. Mirrors the way rhino3dm writes a render
    mesh: Mesh.Vertices.Add + Mesh.Faces.AddFace[/AddQuad] -> ObjectAttributes ->
    File3dm.Objects.AddMesh -> File3dm.Write(path, 0)."""
    import rhino3dm as r

    model = r.File3dm()
    for name, verts, faces in objects:
        m = r.Mesh()
        for v in np.asarray(verts, dtype=np.float64):
            m.Vertices.Add(float(v[0]), float(v[1]), float(v[2]))
        for f in faces:
            if len(f) == 4:
                m.Faces.AddFace(int(f[0]), int(f[1]), int(f[2]), int(f[3]))
            else:
                m.Faces.AddFace(int(f[0]), int(f[1]), int(f[2]))
        attr = r.ObjectAttributes()
        attr.Name = str(name)
        model.Objects.AddMesh(m, attr)
    if not model.Write(str(path), 0):
        raise RuntimeError("rhino3dm File3dm.Write returned failure")


def _box_quads(scale=10.0, offset=(0.0, 0.0, 0.0)):
    """An axis-aligned box: 8 verts + 6 QUAD faces (so the import quad->tri split
    is exercised heavily). Non-trivial float coords surface a float32 mismatch."""
    ox, oy, oz = offset
    v = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=np.float64) * scale
    v[:, 0] += ox
    v[:, 1] += oy
    v[:, 2] += oz
    quads = [
        (0, 1, 2, 3),  # bottom
        (4, 7, 6, 5),  # top
        (0, 4, 5, 1),  # front
        (1, 5, 6, 2),  # right
        (2, 6, 7, 3),  # back
        (3, 7, 4, 0),  # left
    ]
    return v, quads


def make_fixtures(tmp: Path):
    """Returns [(label, 3dm_path)]: a single quad box, a two-object file, and a
    triangulated tetrahedron (all-tri faces)."""
    out = []

    # 1) single named box, irrational scale, all QUAD faces (quad->tri split)
    v, q = _box_quads(scale=12.3456789)
    p = str(tmp / "box.3dm")
    write_3dm_fixture(p, [("MyBox", v, q)])
    out.append(("single quad box (named)", p))

    # 2) two objects (named) — exercises the per-object iteration order + naming
    v1, q1 = _box_quads(scale=5.0)
    v2, q2 = _box_quads(scale=7.5, offset=(20.0, 0.0, 0.0))
    p = str(tmp / "two.3dm")
    write_3dm_fixture(p, [("Alpha", v1, q1), ("Beta", v2, q2)])
    out.append(("two objects", p))

    # 3) a triangulated tetrahedron (4 verts / 4 tri faces) with sharp floats
    vt = np.array([[0.0, 0.0, 0.0], [3.1, 0.0, 0.0],
                   [1.55, 2.7, 0.0], [1.55, 0.9, 2.4]], dtype=np.float64)
    ft = [(0, 1, 2), (0, 3, 1), (1, 3, 2), (0, 2, 3)]
    p = str(tmp / "tet.3dm")
    write_3dm_fixture(p, [("Tetra", vt, ft)])
    out.append(("triangulated tetrahedron", p))

    return out


# --------------------------------------------------------------------------- import bit-parity
def py_import_objects(path: str):
    """The oracle's _import_3dm, returning [(name, verts float32, tris uint32)]."""
    from engine.doc.formats import _import_3dm  # noqa: WPS433

    objs = []
    for name, mb in _import_3dm(Path(path)):
        objs.append((name, np.asarray(mb.verts), np.asarray(mb.tris)))
    return objs


def cpp_import_objects(core: CppCore, path: str):
    """The C++ core's mesh_read_raw, returning [(name, verts float32, tris uint32)].
    The C++ side returns float64 verts (the float32 ON_3fPoint source widened
    exactly), so we cast back to float32 to compare against numpy's float32
    (lossless: float32 -> float64 -> float32 is the identity)."""
    rc, r = core.call("mesh_read_raw", {"path": path, "format": "3dm"})
    if rc != 0 or "objects" not in r:
        raise RuntimeError(f"cpp mesh_read_raw rc={rc}: {str(r)[:160]}")
    objs = []
    for o in r["objects"]:
        v = np.asarray(o["verts"], dtype=np.float64).reshape(-1, 3).astype(np.float32)
        t = np.asarray(o["tris"], dtype=np.uint32).reshape(-1, 3)
        objs.append((o["name"], v, t))
    return objs


def compare_objects(py_objs, cpp_objs):
    diffs = []
    if len(py_objs) != len(cpp_objs):
        return [f"object count {len(py_objs)}!={len(cpp_objs)}"]
    for i, ((pn, pv, pt), (cn, cv, ct)) in enumerate(zip(py_objs, cpp_objs)):
        if pn != cn:
            diffs.append(f"obj{i} name '{pn}'!='{cn}'")
        if pv.shape != cv.shape:
            diffs.append(f"obj{i} verts shape {pv.shape}!={cv.shape}")
        elif not np.array_equal(pv, cv):
            d = float(np.max(np.abs(pv.astype(np.float64) - cv.astype(np.float64))))
            diffs.append(f"obj{i} verts differ max|d|={d:.3e}")
        if pt.shape != ct.shape:
            diffs.append(f"obj{i} tris shape {pt.shape}!={ct.shape}")
        elif not np.array_equal(pt.astype(np.int64), ct.astype(np.int64)):
            nd = int(np.count_nonzero(np.any(pt != ct, axis=1)))
            diffs.append(f"obj{i} tris differ ({nd} rows)")
    return diffs


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[3dm-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[3dm-parity] libaicad ABI={core.abi()}")

    # Both sides MUST be openNURBS from the SAME rhino3dm tag, else parity is weak.
    try:
        import rhino3dm  # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        print(f"[3dm-parity] ABORT: venv has no rhino3dm ({exc})")
        return 3
    rver = rhino3dm.__version__
    print(f"[3dm-parity] venv rhino3dm version = {rver}")
    if rver != "8.17.0":
        print(f"[3dm-parity] ABORT: venv rhino3dm is {rver}, expected 8.17.0 — "
              f"the C++ opennurbsStatic is built from rhino3dm v8.17.0.")
        return 3

    mism = 0
    checks = 0

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            fixtures = make_fixtures(tmp)
        except Exception as exc:  # pragma: no cover
            print(f"[3dm-parity] could not build 3DM fixtures: {exc}")
            return 4

        # ---- (a) IMPORT BIT-PARITY -------------------------------------------------
        print("\n-- import bit-parity (vertices + triangles + names + object count) --")
        for label, path in fixtures:
            checks += 1
            try:
                py_objs = py_import_objects(path)
                cpp_objs = cpp_import_objects(core, path)
            except Exception as exc:
                print(f"  {label:<26} DIFF: {exc}")
                mism += 1
                continue
            diffs = compare_objects(py_objs, cpp_objs)
            if diffs:
                print(f"  {label:<26} DIFF: {'; '.join(diffs)[:110]}")
                mism += 1
            else:
                nv = sum(len(v) for _n, v, _t in py_objs)
                nt = sum(len(t) for _n, _v, t in py_objs)
                print(f"  {label:<26} MATCH  ({len(py_objs)} obj, {nv} verts, {nt} tris)")

        # ---- (b) EXPORT ROUND-TRIP -------------------------------------------------
        print("\n-- export round-trip (C++ export -> re-import) --")
        from tools.cpp_session_parity import CppS  # noqa: WPS433
        BOXV = 30.0 * 20.0 * 10.0
        BOX_TRIS = 12

        def _box_ops():
            return [{"op": "add_feature", "feature": {"type": "box", "name": "Box1", "params": {
                "length": {"value": 30, "expr": None}, "width": {"value": 20, "expr": None},
                "height": {"value": 10, "expr": None}}}}]

        ce = CppS(core)
        ce.apply(0, _box_ops())
        exp = str(tmp / "cpp_export.3dm")
        checks += 1
        rc, ev = core.call("export_file", {"doc_id": ce.doc_id, "path": exp, "format": "3dm"})
        if rc != 0 or not Path(ev.get("path", "")).exists():
            print(f"  {'C++ export':<26} DIFF: export rc={rc} {str(ev)[:60]}")
            mism += 1
        else:
            # C++ re-import: raw mesh round-trips with the right triangle count + bbox.
            try:
                cpp_objs = cpp_import_objects(core, exp)
                tris = sum(len(t) for _n, _v, t in cpp_objs)
                allv = np.vstack([v for _n, v, _t in cpp_objs])
                lo = allv.min(axis=0)
                hi = allv.max(axis=0)
                ok = (tris == BOX_TRIS and
                      np.allclose(lo, [-15, -10, -5], atol=1e-3) and
                      np.allclose(hi, [15, 10, 5], atol=1e-3))
                print(f"  {'C++ export->C++ import':<26} "
                      f"{'MATCH' if ok else f'DIFF tris={tris} bbox={lo}..{hi}'}")
                mism += 0 if ok else 1
            except Exception as exc:
                print(f"  {'C++ export->C++ import':<26} DIFF: {exc}")
                mism += 1

            # Python oracle re-import of the C++-exported 3DM: triangle count survives.
            checks += 1
            try:
                py_objs = py_import_objects(exp)
                ptris = sum(len(t) for _n, _v, t in py_objs)
                ok = ptris == BOX_TRIS
                print(f"  {'C++ export->py import':<26} "
                      f"{'MATCH' if ok else f'DIFF tris={ptris}'}")
                mism += 0 if ok else 1
            except Exception as exc:
                print(f"  {'C++ export->py import':<26} DIFF: {exc}")
                mism += 1

            # C++ import-as-body volume: the exported solid re-imports with the box volume.
            checks += 1
            rc, iv = core.call("import_file", {"path": exp})
            cv = float(iv.get("volume", 0.0)) if rc == 0 else -1.0
            ok = rc == 0 and abs(cv - BOXV) / BOXV <= 1e-3
            print(f"  {'C++ 3dm body round-trip':<26} "
                  f"{'MATCH' if ok else f'DIFF rc={rc} vol={cv}'}")
            mism += 0 if ok else 1

    print(f"\n[3dm-parity] {checks - mism}/{checks} cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

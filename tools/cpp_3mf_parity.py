"""cpp_3mf_parity.py — Python-vs-C++ 3MF parity (lib3mf 2.5.0).

The strongest contract in this gate is IMPORT BIT-PARITY: the Python oracle
(engine.doc.formats._import_3mf) and the C++ core (libaicad.dll: mesh_read_raw)
BOTH wrap the identical lib3mf 2.5.0 — the Python `lib3mf` wheel dlopens
lib3mf.dll, and the C++ side uses the header-only CppDynamic binding which ALSO
dlopens the SAME lib3mf.dll. So the raw 3MF mesh extraction (per-object vertex
coordinates, triangle indices, names, object count) MUST be bit-identical.

Gates:
  (a) IMPORT bit-parity — for a set of .3mf fixtures, compare _import_3mf vs the
      C++ mesh_read_raw: EXACT object count, EXACT names, EXACT (float32) vertex
      coords, EXACT triangle indices. This is the bit-contract.
  (b) EXPORT round-trip — write a mesh with the C++ core (export_file 3mf), then
      re-import it through the C++ core (mesh_read_raw) and through the Python
      oracle, and confirm the mesh survives (triangle count + bbox invariant).
      Export is NOT byte/mesh-identical to the oracle (the C++ side tessellates a
      B-rep solid with OCCT, the oracle with build123d/tessellate.py), so export
      is gated as a round-trip, per the increment spec.

Run under the RUNTIME venv (it has lib3mf + build123d):
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_3mf_parity.py

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
def write_3mf_fixture(path: str, objects: list[tuple[str, np.ndarray, np.ndarray]]) -> None:
    """Write a .3mf with the Python lib3mf wheel from explicit (name, verts, tris) tuples,
    EXACTLY the way _export_3mf_lib3mf does (AddMeshObject -> SetName -> SetGeometry ->
    AddBuildItem(identity) -> QueryWriter("3mf").WriteToFile). This is the controlled
    fixture whose verts/tris are known a priori."""
    import lib3mf

    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()
    identity = wrapper.GetIdentityTransform()
    for name, verts, tris in objects:
        mo = model.AddMeshObject()
        mo.SetName(name)
        ps = []
        for v in np.asarray(verts, dtype=np.float64):
            p = lib3mf.Position()
            p.Coordinates = (float(v[0]), float(v[1]), float(v[2]))
            ps.append(p)
        ts = []
        for t in np.asarray(tris, dtype=np.int64):
            tri = lib3mf.Triangle()
            tri.Indices = (int(t[0]), int(t[1]), int(t[2]))
            ts.append(tri)
        mo.SetGeometry(ps, ts)
        model.AddBuildItem(mo, identity)
    model.QueryWriter("3mf").WriteToFile(str(path))


def _unit_box(scale=10.0, offset=(0.0, 0.0, 0.0)) -> tuple[np.ndarray, np.ndarray]:
    """An axis-aligned box as a closed 12-triangle mesh — non-trivial float coords so a
    float32-vs-float64 mismatch would show up."""
    ox, oy, oz = offset
    v = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=np.float64) * scale
    v[:, 0] += ox
    v[:, 1] += oy
    v[:, 2] += oz
    f = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.uint32)
    return v, f


def make_fixtures(tmp: Path) -> list[tuple[str, str]]:
    """Returns [(label, 3mf_path)]. Includes irrational/odd coords + a two-object file to
    exercise the per-object iteration + naming."""
    out: list[tuple[str, str]] = []

    # 1) single named box, irrational scale (float32 truncation must match on both sides)
    v, f = _unit_box(scale=12.3456789)
    p = str(tmp / "box.3mf")
    write_3mf_fixture(p, [("MyBox", v, f)])
    out.append(("single box (named)", p))

    # 2) two objects (one named, one to be auto-named "<stem>_2" if lib3mf returns "")
    v1, f1 = _unit_box(scale=5.0)
    v2, f2 = _unit_box(scale=7.5, offset=(20.0, 0.0, 0.0))
    p = str(tmp / "two.3mf")
    write_3mf_fixture(p, [("Alpha", v1, f1), ("Beta", v2, f2)])
    out.append(("two objects", p))

    # 3) a tetrahedron (4 verts / 4 tris) with sharp non-grid floats
    vt = np.array([[0.0, 0.0, 0.0], [3.1, 0.0, 0.0],
                   [1.55, 2.7, 0.0], [1.55, 0.9, 2.4]], dtype=np.float64)
    ft = np.array([[0, 1, 2], [0, 3, 1], [1, 3, 2], [0, 2, 3]], dtype=np.uint32)
    p = str(tmp / "tet.3mf")
    write_3mf_fixture(p, [("Tetra", vt, ft)])
    out.append(("tetrahedron", p))

    return out


# --------------------------------------------------------------------------- import bit-parity
def py_import_objects(path: str):
    """The oracle's _import_3mf, returning [(name, verts float32, tris uint32)]."""
    from engine.doc.formats import _import_3mf  # noqa: WPS433

    objs = []
    for name, mb in _import_3mf(Path(path)):
        objs.append((name, np.asarray(mb.verts), np.asarray(mb.tris)))
    return objs


def cpp_import_objects(core: CppCore, path: str):
    """The C++ core's mesh_read_raw, returning [(name, verts float32, tris uint32)] —
    the C++ side returns float64 verts (the float32 source widened exactly), so we cast
    back to float32 to compare against numpy's float32 (lossless: every float32 widened to
    float64 and narrowed back is the identity)."""
    rc, r = core.call("mesh_read_raw", {"path": path, "format": "3mf"})
    if rc != 0 or "objects" not in r:
        raise RuntimeError(f"cpp mesh_read_raw rc={rc}: {str(r)[:120]}")
    objs = []
    for o in r["objects"]:
        v = np.asarray(o["verts"], dtype=np.float64).reshape(-1, 3).astype(np.float32)
        t = np.asarray(o["tris"], dtype=np.uint32).reshape(-1, 3)
        objs.append((o["name"], v, t))
    return objs


def compare_objects(py_objs, cpp_objs) -> list[str]:
    diffs = []
    if len(py_objs) != len(cpp_objs):
        return [f"object count {len(py_objs)}!={len(cpp_objs)}"]
    for i, ((pn, pv, pt), (cn, cv, ct)) in enumerate(zip(py_objs, cpp_objs)):
        if pn != cn:
            diffs.append(f"obj{i} name '{pn}'!='{cn}'")
        # vertex coords: EXACT (both are float32 reconstructions of the same lib3mf data)
        if pv.shape != cv.shape:
            diffs.append(f"obj{i} verts shape {pv.shape}!={cv.shape}")
        elif not np.array_equal(pv, cv):
            d = float(np.max(np.abs(pv.astype(np.float64) - cv.astype(np.float64))))
            diffs.append(f"obj{i} verts differ max|d|={d:.3e}")
        # triangle indices: EXACT
        if pt.shape != ct.shape:
            diffs.append(f"obj{i} tris shape {pt.shape}!={ct.shape}")
        elif not np.array_equal(pt.astype(np.int64), ct.astype(np.int64)):
            nd = int(np.count_nonzero(np.any(pt != ct, axis=1)))
            diffs.append(f"obj{i} tris differ ({nd} rows)")
    return diffs


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[3mf-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[3mf-parity] libaicad ABI={core.abi()}")

    # Both sides MUST wrap lib3mf 2.5.0, else parity is meaningless.
    try:
        import lib3mf  # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        print(f"[3mf-parity] ABORT: venv has no lib3mf ({exc})")
        return 3
    w = lib3mf.Wrapper()
    maj, minor, micro = w.GetLibraryVersion()
    lver = f"{maj}.{minor}.{micro}"
    print(f"[3mf-parity] venv lib3mf version = {lver}")
    if lver != "2.5.0":
        print(f"[3mf-parity] ABORT: venv lib3mf is {lver}, expected 2.5.0 — "
              f"pip install lib3mf==2.5.0 into the runtime venv first.")
        return 3

    mism = 0
    checks = 0

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            fixtures = make_fixtures(tmp)
        except Exception as exc:  # pragma: no cover
            print(f"[3mf-parity] could not build 3MF fixtures: {exc}")
            return 4

        # ---- (a) IMPORT BIT-PARITY -------------------------------------------------
        print("\n-- import bit-parity (vertices + triangles + names + object count) --")
        for label, path in fixtures:
            checks += 1
            try:
                py_objs = py_import_objects(path)
                cpp_objs = cpp_import_objects(core, path)
            except Exception as exc:
                print(f"  {label:<24} DIFF: {exc}")
                mism += 1
                continue
            diffs = compare_objects(py_objs, cpp_objs)
            if diffs:
                print(f"  {label:<24} DIFF: {'; '.join(diffs)[:100]}")
                mism += 1
            else:
                nv = sum(len(v) for _n, v, _t in py_objs)
                nt = sum(len(t) for _n, _v, t in py_objs)
                print(f"  {label:<24} MATCH  ({len(py_objs)} obj, {nv} verts, {nt} tris)")

        # ---- (b) EXPORT ROUND-TRIP -------------------------------------------------
        # Build a box solid in a C++ doc, export it to 3MF, re-import via BOTH cores.
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
        exp = str(tmp / "cpp_export.3mf")
        checks += 1
        rc, ev = core.call("export_file", {"doc_id": ce.doc_id, "path": exp, "format": "3mf"})
        if rc != 0 or not Path(ev.get("path", "")).exists():
            print(f"  {'C++ export':<24} DIFF: export rc={rc} {str(ev)[:60]}")
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
                print(f"  {'C++ export->C++ import':<24} "
                      f"{'MATCH' if ok else f'DIFF tris={tris} bbox={lo}..{hi}'}")
                mism += 0 if ok else 1
            except Exception as exc:
                print(f"  {'C++ export->C++ import':<24} DIFF: {exc}")
                mism += 1

            # Python oracle re-import of the C++-exported 3MF: triangle count survives.
            checks += 1
            try:
                py_objs = py_import_objects(exp)
                ptris = sum(len(t) for _n, _v, t in py_objs)
                ok = ptris == BOX_TRIS
                print(f"  {'C++ export->py import':<24} "
                      f"{'MATCH' if ok else f'DIFF tris={ptris}'}")
                mism += 0 if ok else 1
            except Exception as exc:
                print(f"  {'C++ export->py import':<24} DIFF: {exc}")
                mism += 1

            # C++ import-as-body volume: the exported solid re-imports with the box volume.
            checks += 1
            rc, iv = core.call("import_file", {"path": exp})
            cv = float(iv.get("volume", 0.0)) if rc == 0 else -1.0
            ok = rc == 0 and abs(cv - BOXV) / BOXV <= 1e-3
            print(f"  {'C++ 3mf body round-trip':<24} "
                  f"{'MATCH' if ok else f'DIFF rc={rc} vol={cv}'}")
            mism += 0 if ok else 1

    print(f"\n[3mf-parity] {checks - mism}/{checks} cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

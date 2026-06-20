"""cpp_surf_parity.py — SURFACING parity gate (Python oracle vs C++ core).

The surfacing feature compilers (engine/doc/features/surf_*.py) build OPEN surface
bodies — sheets with no enclosed volume — so the existing volume gate
(tools/cpp_parity.py) is meaningless for them (volume ~= 0). This gate instead
compares, per surviving body, the EXACT surface AREA + face/edge COUNTS against the
Python oracle (and exact VOLUME for the two surface->solid bridges, surf_thicken and
the watertight surf_knit / surf_replace_face).

  - Python ORACLE : engine.doc.compiler.compile_document(doc) -> bodies, each a
                    build123d Shape (area / faces() / edges() / volume).
  - C++  CANDIDATE: libaicad.dll doc_compile_cpp -> "bodies":[{kind,area,volume,
                    n_faces,n_edges}] (added alongside the existing volume signal).

The gate is on TOTALS (sum of area, sum of |faces|, sum of |edges|) across surviving
bodies, NOT per-face identity: OCCT-vs-build123d enumeration order can differ, so we
compare counts + total area (the same owner-map / edge-resolve quirk the volume gate
already lives with). Area tolerance is a tight 1e-6 relative.

Run under the project venv (it imports the engine + reuses tools.cpp_parity.CppCore):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_surf_parity.py

Exit code = number of fixtures that did NOT fully match (0 = full parity).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import (  # noqa: E402  (reuse the proven harness pieces)
    CppCore,
    _circle,
    _dim,
    _line,
    _rect,
    _rel,
    _sketch,
)


# --------------------------------------------------------------------------- builders
def _region(sketch_fid):
    return {"kind": "region", "sketch": sketch_fid}


def _body(owner_fid):
    return {"kind": "body", "owner_feature": owner_fid}


def _surf(fid, ftype, refs, params=None):
    return {"id": fid, "type": ftype, "params": params or {}, "refs": refs}


def _plane_offset(fid, base_datum, offset):
    return {"id": fid, "type": "plane",
            "params": {"method": "offset", "offset": {"value": offset, "expr": None}},
            "refs": {"base": base_datum}}


def _extrude(fid, sketch_fid, depth, mode="new"):
    return {"id": fid, "type": "extrude", "params": {"mode": mode, "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [_region(sketch_fid)]}}


# --------------------------------------------------------------------------- fixtures
def fixtures():
    out = {}

    # surf_planar: a 30x20 rectangle sketch -> its bounding FACE (area 600, 1 face, 4 edges).
    pe, pc = _rect(0, 0, 30, 20)
    out["surf_planar"] = {"features": [
        _sketch("s1", "plane.top", pe, pc),
        _surf("f1", "surf_planar", {"profile": [_region("s1")]})]}

    # surf_extrude: 30x20 rect boundary swept +Z by 10 -> open tube wall (4 side faces,
    # NO caps). Area = perimeter*depth = (30+20)*2*10 = 1000.
    ee, ec = _rect(0, 0, 30, 20)
    out["surf_extrude"] = {"features": [
        _sketch("s1", "plane.top", ee, ec),
        _surf("f1", "surf_extrude", {"profile": [_region("s1")]},
              {"end": "blind", "depth": {"value": 10.0, "expr": None}})]}

    # surf_revolve: rect (x 5..15, y 0..10) on XY, outer wire revolved 360 about Y ->
    # open surface of revolution (two cylinders r=5,15 h=10 + two annular discs).
    re, rc = _rect(5, 0, 10, 10)
    out["surf_revolve"] = {"features": [
        _sketch("s1", "plane.top", re, rc),
        _surf("f1", "surf_revolve", {"profile": [_region("s1")], "axis": "axis.y"},
              {"angle": {"value": 360.0, "expr": None}})]}

    # surf_sweep: circle r=5 on XY swept along a +Z line of length 40 -> open tube wall,
    # area = 2*pi*5*40 = 400*pi.
    prof_e = [_circle("e1", 0, 0, 5)]
    prof_c = [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 5)]
    path_e = [_line("e1", 0, 0, 0, 40)]
    path_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    out["surf_sweep"] = {"features": [
        _sketch("s1", "plane.top", prof_e, prof_c),
        _sketch("s2", "plane.front", path_e, path_c),
        _surf("f1", "surf_sweep", {"profile": [_region("s1")], "path": _region("s2")})]}

    # surf_loft: 20x20 section on XY -> 10x10 section on an offset plane 40 above ->
    # open lofted shell (4 side faces, no caps).
    lo_e, lo_c = _rect(-10, -10, 20, 20)
    hi_e, hi_c = _rect(-5, -5, 10, 10)
    out["surf_loft"] = {"features": [
        _plane_offset("p1", "plane.top", 40.0),
        _sketch("s1", "plane.top", lo_e, lo_c),
        _sketch("s2", "p1", hi_e, hi_c),
        _surf("f1", "surf_loft", {"sections": [_region("s1"), _region("s2")]},
              {"ruled": True})]}

    # surf_ruled: two single-line rails -> the exact ruled face between them.
    # rail_a = line (0,0,0)->(20,0,0) on XY; rail_b = line (0,10,0)->(20,10,0).
    # Both are open single-edge sketches; the ruled surface is a 20x10 strip (area 200).
    ra_e = [_line("e1", 0, 0, 20, 0)]
    ra_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    rb_e = [_line("e1", 0, 10, 20, 10)]
    rb_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    out["surf_ruled"] = {"features": [
        _sketch("sa", "plane.top", ra_e, ra_c),
        _sketch("sb", "plane.top", rb_e, rb_c),
        _surf("f1", "surf_ruled", {"edge_a": _region("sa"), "edge_b": _region("sb")})]}

    # surf_offset: a planar surface (surf_planar 40x40) offset 5 along its normal ->
    # a parallel 40x40 surface (area 1600, 1 face).
    oe, oc = _rect(0, 0, 40, 40)
    out["surf_offset"] = {"features": [
        _sketch("s1", "plane.top", oe, oc),
        _surf("f1", "surf_planar", {"profile": [_region("s1")]}),
        _surf("f2", "surf_offset",
              {"faces": [{"kind": "face", "surf": "plane", "centroid": [20.0, 20.0, 0.0],
                          "axis": [0.0, 0.0, 1.0], "area_or_len": 1600.0,
                          "owner_feature": None, "hint_index": 0}]},
              {"distance": {"value": 5.0, "expr": None}})]}

    # surf_thicken: a 40x40 planar surface thickened 2mm one side -> 40x40x2 solid,
    # V = 3200 (the surface->solid bridge — gated on VOLUME).
    te, tc = _rect(0, 0, 40, 40)
    out["surf_thicken"] = {"features": [
        _sketch("s1", "plane.top", te, tc),
        _surf("f1", "surf_planar", {"profile": [_region("s1")]}),
        _surf("f2", "surf_thicken", {"surface": _body("f1")},
              {"thickness": {"value": 2.0, "expr": None}, "side": "one"})]}

    # surf_knit: the planar BOTTOM (1 face) of a 20x20 sketch + the surf_extrude side
    # WALLS (4 faces) of the same sketch, knit edge-to-edge into one open box shell
    # (5 faces, area 1200, no top). The two inputs have DISTINCT face counts so the
    # oracle's body-selector topo-owner fallback resolves each unambiguously.
    ka_e, ka_c = _rect(0, 0, 20, 20)
    out["surf_knit"] = {"features": [
        _sketch("sa", "plane.top", ka_e, ka_c),
        _surf("f1", "surf_planar", {"profile": [_region("sa")]}),
        _surf("f2", "surf_extrude", {"profile": [_region("sa")]},
              {"end": "blind", "depth": {"value": 10.0, "expr": None}}),
        _surf("f3", "surf_knit", {"surfaces": [_body("f1"), _body("f2")]})]}

    # surf_fill: a closed planar 30x20 rectangle boundary -> the exact flat patch face
    # (BRepFill_Filling over the 4 boundary edges; planar boundary -> exact, area 600).
    fe, fc = _rect(0, 0, 30, 20)
    out["surf_fill"] = {"features": [
        _sketch("s1", "plane.top", fe, fc),
        _surf("f1", "surf_fill", {"edges": [_region("s1")]})]}

    # surf_boundary: the same closed planar rectangle net -> the filled patch (area 600).
    bd_e, bd_c = _rect(0, 0, 30, 20)
    out["surf_boundary"] = {"features": [
        _sketch("s1", "plane.top", bd_e, bd_c),
        _surf("f1", "surf_boundary", {"dir1": [_region("s1")]})]}

    # surf_delete_face: a 40x30x20 box, delete its top (+Z) face -> open shell (5 faces).
    de, dc = _rect(0, 0, 40, 30)
    top_face = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0,
                "owner_feature": None, "hint_index": 5}
    out["surf_delete_face"] = {"features": [
        _sketch("s1", "plane.top", de, dc), _extrude("x1", "s1", 20.0),
        _surf("f1", "surf_delete_face", {"faces": [top_face]})]}

    # surf_trim: a 40x40 planar surface trimmed by a box's vertical (-X) plane at x=20,
    # keep the x<20 half -> area 800 (exactly half). The box solid (built merge:false so
    # it stays a separate operand) survives; the surface is replaced by its trimmed half.
    ts_e, ts_c = _rect(0, 0, 40, 40)        # the surface
    tb_e, tb_c = _rect(20, 0, 20, 40)       # the tool box footprint (x[20,40])
    tool_face = {"kind": "face", "surf": "plane", "centroid": [20.0, 20.0, 5.0],
                 "axis": [1.0, 0.0, 0.0], "area_or_len": 400.0,
                 "owner_feature": None, "hint_index": 0}
    out["surf_trim"] = {"features": [
        _sketch("sb", "plane.top", tb_e, tb_c),
        {"id": "bx", "type": "extrude",
         "params": {"mode": "new", "merge": False, "depth": {"value": 10.0, "expr": None}},
         "refs": {"profile": [_region("sb")]}},
        _sketch("ss", "plane.top", ts_e, ts_c),
        _surf("f1", "surf_planar", {"profile": [_region("ss")]}),
        _surf("f2", "surf_trim", {"surface": _body("f1"), "tool": tool_face},
              {"keep": "negative"})]}

    # surf_replace_face: a 40x40x20 box, replace its TOP (+Z) face with a planar surface
    # at z=10 -> the box is retrimmed down to z=10 (V=16000, a 40x40x10 block). The leftover
    # surf_planar surface (area 1600) survives as a separate surface body.
    rb_e, rb_c = _rect(0, 0, 40, 40)
    rp_e, rp_c = _rect(0, 0, 40, 40)
    rf_top = {"kind": "face", "surf": "plane", "centroid": [20.0, 20.0, 20.0],
              "axis": [0.0, 0.0, 1.0], "area_or_len": 1600.0,
              "owner_feature": None, "hint_index": 5}
    rf_repl = {"kind": "face", "surf": "plane", "centroid": [20.0, 20.0, 10.0],
               "axis": [0.0, 0.0, 1.0], "area_or_len": 1600.0,
               "owner_feature": None, "hint_index": 0}
    out["surf_replace_face"] = {"features": [
        _sketch("sb", "plane.top", rb_e, rb_c),
        {"id": "bx", "type": "extrude",
         "params": {"mode": "new", "depth": {"value": 20.0, "expr": None}},
         "refs": {"profile": [_region("sb")]}},
        _plane_offset("pl", "plane.top", 10.0),
        _sketch("sp", "pl", rp_e, rp_c),
        _surf("f1", "surf_planar", {"profile": [_region("sp")]}),
        _surf("f2", "surf_replace_face", {"faces": [rf_top], "surface": rf_repl})]}

    return out


# --------------------------------------------------------------------------- oracle
def python_compile(doc: dict):
    """Run the Python oracle -> (id->ok, [per-body (kind, area, volume, nf, ne)], error)."""
    try:
        from engine.doc.compiler import compile_document
    except Exception as exc:  # pragma: no cover
        return {}, [], f"import engine failed: {exc}"
    try:
        res = compile_document(doc)
    except Exception as exc:
        return {}, [], f"compile raised: {exc}"
    status = {s["id"]: bool(s.get("ok")) for s in res.feature_status}

    bodies = []
    for b in res.bodies:
        shp = b.shape
        kind = getattr(b, "kind", "solid")
        try:
            area = float(shp.area)
        except Exception:
            area = 0.0
        try:
            vol = float(shp.volume)
        except Exception:
            vol = 0.0
        try:
            nf = len(shp.faces())
        except Exception:
            nf = 0
        try:
            ne = len(shp.edges())
        except Exception:
            ne = 0
        bodies.append((kind, area, vol, nf, ne))
    return status, bodies, None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[surf-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[surf-parity] libaicad ABI={core.abi()}  dll={dll}")

    fx = fixtures()
    name_w = max(len(n) for n in fx)
    print(f"\n{'fixture'.ljust(name_w)}  {'py(area,F,E,V)':>26}  {'cpp(area,F,E,V)':>26}  verdict")
    print("-" * (name_w + 62))

    AREA_TOL = 1e-6  # relative area tolerance (exact BRep area)
    VOL_TOL = 1e-6   # relative volume tolerance (thicken / closed bodies)
    mismatches = 0
    for name, doc in fx.items():
        py_status, py_bodies, py_err = python_compile(doc)
        rc, cpp = core.call("doc_compile_cpp", {"doc": doc})
        cpp_status = {s["id"]: bool(s.get("ok")) for s in cpp.get("statuses", [])}
        cpp_bodies = cpp.get("bodies", [])

        # Totals across surviving bodies. AREA + counts gate ALL bodies; VOLUME is
        # summed only over SOLID-kind bodies (an open surface's raw shape.volume is a
        # meaningless signed value — a full revolve even yields a closed-looking shell
        # with a nonzero volume — so the surface gate is area+counts, volume for solids).
        py_area = sum(b[1] for b in py_bodies)
        py_vol = sum(b[2] for b in py_bodies if b[0] == "solid")
        py_nf = sum(b[3] for b in py_bodies)
        py_ne = sum(b[4] for b in py_bodies)
        cpp_area = sum(float(b.get("area", 0.0)) for b in cpp_bodies)
        cpp_vol = sum(float(b.get("volume", 0.0)) for b in cpp_bodies
                      if b.get("kind") == "solid")
        cpp_nf = sum(int(b.get("n_faces", 0)) for b in cpp_bodies)
        cpp_ne = sum(int(b.get("n_edges", 0)) for b in cpp_bodies)
        # Body KINDS must agree as a MULTISET (surface vs solid classification); body
        # ORDER can legitimately differ between the kernels, so compare sorted.
        py_kinds = sorted(b[0] for b in py_bodies)
        cpp_kinds = sorted(b.get("kind") for b in cpp_bodies)

        diffs = []
        if py_err:
            diffs.append(f"py-error:{py_err[:42]}")
        if rc != 0 and not cpp_status:
            diffs.append(f"cpp-error:{cpp.get('error','?')[:42]}")
        for fid in py_status:
            if py_status[fid] != cpp_status.get(fid, False):
                diffs.append(f"ok:{fid}")
        if len(py_bodies) != len(cpp_bodies):
            diffs.append(f"bodies {len(py_bodies)}!={len(cpp_bodies)}")
        if py_kinds != cpp_kinds:
            diffs.append(f"kinds {py_kinds}!={cpp_kinds}")
        if py_nf != cpp_nf:
            diffs.append(f"F {py_nf}!={cpp_nf}")
        if py_ne != cpp_ne:
            diffs.append(f"E {py_ne}!={cpp_ne}")
        if abs(py_area - cpp_area) / max(abs(py_area), 1.0) > AREA_TOL:
            diffs.append(f"area {py_area:.3f}!={cpp_area:.3f}")
        if abs(py_vol - cpp_vol) / max(abs(py_vol), 1.0) > VOL_TOL:
            diffs.append(f"vol {py_vol:.3f}!={cpp_vol:.3f}")

        verdict = "MATCH" if not diffs else "DIFF: " + ", ".join(diffs)
        if diffs:
            mismatches += 1
        print(f"{name.ljust(name_w)}"
              f"  {f'{py_area:.1f},{py_nf},{py_ne},{py_vol:.0f}':>26}"
              f"  {f'{cpp_area:.1f},{cpp_nf},{cpp_ne},{cpp_vol:.0f}':>26}  {verdict}")

    print(f"\n[surf-parity] {len(fx) - mismatches}/{len(fx)} fixtures at full parity")
    return mismatches


if __name__ == "__main__":
    raise SystemExit(main())

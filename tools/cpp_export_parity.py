"""cpp_export_parity.py — Python-vs-C++ file EXPORT parity (geometry, not bytes).

Companion to cpp_session_parity.py. The C++ core's `export_file` is already the live
export path for doc-cpp documents (host routes it by the doc-cpp id prefix). This gate
confirms the C++ export preserves the SAME geometry the Python oracle would have written,
WITHOUT comparing exported bytes — OCCT 7.9.3 (C++) and the Python oracle kernel emit
structurally-equal-but-not-byte-identical STEP/IGES, so we re-import BOTH written files
through the SAME C++ importer and compare the resulting volume + solid/body count.

For each fixture and each B-rep format (STEP / IGES / BREP) + STL (mesh, loose tol):
  build the doc in the Python session AND the C++ session ->
  export_file from each engine -> re-import both via the C++ import_file ->
  assert |vol_py - vol_cpp| within tol AND body counts equal.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_export_parity.py

Exit code = number of mismatched (fixture, format) cells (0 = full parity).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- ops fixtures
def _sketch(plane="plane.top"):
    return {"op": "add_feature", "feature": {"type": "sketch", "name": "S",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": plane}}}


def _line(x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def _circle(cx, cy, r):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "circle", "construction": False, "center": [cx, cy], "radius": r}}


def _extrude(depth, centroid, area):
    return {"op": "add_feature", "feature": {
        "type": "extrude", "name": "B",
        "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                              "centroid": list(centroid), "area": area, "hint_index": 0}]}}}


def fx_box():
    return [_sketch(), _line(0, 0, 40, 0), _line(40, 0, 40, 30), _line(40, 30, 0, 30),
            _line(0, 30, 0, 0), _extrude(10, (20, 15), 1200.0)]


def fx_cylinder():
    return [_sketch(), _circle(0, 0, 12), _extrude(25, (0, 0), 452.389)]


def fx_two_boss():
    # box (idx5) then a second sketch+boss on top -> two features, one fused body.
    base = fx_box()
    base += [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S2",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.top"}}},
        {"op": "sketch_add_entity", "sketch": "$6",
            "entity": {"type": "circle", "construction": False, "center": [20, 15], "radius": 6}},
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss2",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": 8, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$6",
                                  "centroid": [20, 15], "area": 113.097, "hint_index": 0}]}}},
    ]
    return base


FIXTURES = [("box", fx_box), ("cylinder", fx_cylinder), ("two_boss", fx_two_boss)]

# (format, tolerance on volume mm^3). STL is a faceted mesh -> looser tolerance.
FORMATS = [("step", 1e-4), ("iges", 1e-2), ("brep", 1e-4), ("stl", 1.0)]


# --------------------------------------------------------------------------- engines
class CppDoc:
    def __init__(self, core, ops):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]
        rc, r = core.call("doc_apply_ops", {"doc_id": self.doc_id, "base_revision": 0, "ops": ops})
        if rc != 0:
            raise RuntimeError(f"cpp apply failed: {r}")
        self.volume = float(r.get("volume", 0.0))

    def export(self, path, fmt):
        rc, r = self.core.call("export_file", {"doc_id": self.doc_id, "path": path, "format": fmt})
        if rc != 0:
            raise RuntimeError(f"cpp export {fmt} failed: {r}")
        return r.get("path", path)


def py_export(ops, path, fmt):
    """Build the doc in the Python session, export via engine.doc.formats (the oracle)."""
    from engine.doc import session
    from engine.doc.session import _rebuild, _solid_bodies
    from engine.doc.formats import export_document
    s = session.new_session("part")
    session.apply_ops(s, 0, ops, None)
    result = s.last_result or _rebuild(s)
    bodies = _solid_bodies(result)
    if not bodies:
        raise RuntimeError("python oracle produced no bodies")
    session_bodies = [(bo.name or f"Body{i}", bo.shape) for i, bo in enumerate(bodies, start=1)]
    return export_document(session_bodies, path, fmt)


def reimport_volume(core, path):
    """Re-import a written file through the SAME C++ importer; return (volume, n_bodies)."""
    rc, r = core.call("import_file", {"path": path})
    if rc != 0:
        raise RuntimeError(f"reimport failed: {r}")
    vol = float(r.get("volume", 0.0))
    feats = r.get("tree", {}).get("features", [])
    n = len(feats)
    return vol, n


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()
    core = CppCore(Path(args.dll))

    tmp = Path(tempfile.mkdtemp(prefix="aicad_export_parity_"))
    fails = 0
    checks = 0
    for fxname, fxfn in FIXTURES:
        ops = fxfn()
        try:
            cpp = CppDoc(core, ops)
        except Exception as exc:  # noqa: BLE001
            print(f"[{fxname}] CPP BUILD FAIL: {exc}")
            fails += 1
            continue
        for fmt, tol in FORMATS:
            checks += 1
            cpath = str(tmp / f"{fxname}_cpp.{fmt}")
            ppath = str(tmp / f"{fxname}_py.{fmt}")
            try:
                cpp.export(cpath, fmt)
                py_export(ops, ppath, fmt)
            except Exception as exc:  # noqa: BLE001
                print(f"[{fxname}/{fmt}] EXPORT FAIL: {exc}")
                fails += 1
                continue
            try:
                cvol, cn = reimport_volume(core, cpath)
                pvol, pn = reimport_volume(core, ppath)
            except Exception as exc:  # noqa: BLE001
                print(f"[{fxname}/{fmt}] REIMPORT FAIL: {exc}")
                fails += 1
                continue
            dv = abs(cvol - pvol)
            ok_vol = dv <= max(tol, tol * abs(pvol) if fmt == "stl" else tol)
            # STL: scale tolerance with size (faceting error grows with surface area).
            if fmt == "stl":
                ok_vol = dv <= 0.01 * max(abs(pvol), 1.0) + 5.0
            ok_n = (cn == pn)
            if ok_vol and ok_n:
                print(f"[{fxname}/{fmt}] MATCH  vol cpp={cvol:.3f} py={pvol:.3f} "
                      f"(d={dv:.3g}) bodies={cn}")
            else:
                print(f"[{fxname}/{fmt}] DIFF   vol cpp={cvol:.3f} py={pvol:.3f} "
                      f"(d={dv:.3g}) bodies cpp={cn} py={pn}")
                fails += 1

    print(f"\n[export-parity] {checks - fails}/{checks} (fixture,format) cells at parity")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())

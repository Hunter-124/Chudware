"""cpp_export2d_parity.py — Python-vs-C++ parity for the stateful 2D export RPCs.

Gates the three drawing/export methods ported in the doc-engine cutover Phase 3:
  - export_drawing  : pool an open drawing's view geometry to DXF/PDF
  - export_flat_dxf : a sheet-metal part's flat pattern (OUTLINE + BEND + CUTS layers)
  - drawing_bom     : a part-source drawing has an empty BOM (clean, not an error)

Each builds the SAME doc through the C++ session (libaicad: doc_new/doc_apply_ops/
drawing_new/...) and the Python oracle (engine.doc.session + drawing + export2d), then
compares STRUCTURE/geometry invariants — never exported bytes (OCCT 7.9.3 != the oracle
kernel emits structurally-equal-but-not-byte-identical files).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_export2d_parity.py

Exit code = number of mismatched checks (0 = full parity).
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


def _L(x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def box_ops():
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.top"}}},
        _L(0, 0, 40, 0), _L(40, 0, 40, 30), _L(40, 30, 0, 30), _L(0, 30, 0, 0),
        {"op": "add_feature", "feature": {"type": "extrude", "name": "B",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": 10, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0", "centroid": [20, 15],
                                  "area": 1200.0, "hint_index": 0}]}}},
    ]


def sheetmetal_flat_ops():
    edge = {"kind": "edge", "surf": "line", "centroid": [30.0, 0.0, 0.0], "axis": [1.0, 0.0, 0.0],
            "area_or_len": 60.0, "owner_feature": None, "hint_index": 2}
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.top"}}},
        _L(0, 0, 60, 0), _L(60, 0, 60, 40), _L(60, 40, 0, 40), _L(0, 40, 0, 0),
        {"op": "add_feature", "feature": {"type": "sm_base_flange", "name": "BF",
            "params": {"thickness": {"value": 2.0, "expr": None}, "k_factor": {"value": 0.45, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0", "centroid": [30, 20],
                                  "area": 2400.0, "hint_index": 0}]}}},
        {"op": "add_feature", "feature": {"type": "sm_edge_flange", "name": "EF",
            "params": {"angle": {"value": 90.0, "expr": None}, "length": {"value": 20.0, "expr": None},
                       "radius": {"value": 2.0, "expr": None}, "position": "material_inside",
                       "flip": False, "corner_relief": "rectangular"}, "refs": {"edge": edge}}},
        {"op": "add_feature", "feature": {"type": "sm_flat_pattern", "name": "FP",
                                          "params": {}, "refs": {}}},
    ]


VIEWS = [("base", "front", [100, 100]), ("projected", "top", [100, 40]),
         ("iso", "iso", [220, 40])]


def check(label, ok, detail=""):
    print(f"[{'OK ' if ok else 'XX '}] {label}{('  ' + detail) if detail else ''}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()
    core = CppCore(Path(args.dll))
    from engine.doc import session as docsess
    from engine.doc import drawing as drawingmod
    from engine.doc import export2d
    tmp = Path(tempfile.mkdtemp(prefix="aicad_export2d_parity_"))
    fails = 0

    # ---- export_drawing: same box + same views, compare pooled entity counts ----------
    ops = box_ops()
    rc, r = core.call("doc_new", {"kind": "part"}); cdid = r["doc_id"]
    core.call("doc_apply_ops", {"doc_id": cdid, "base_revision": 0, "ops": ops})
    rc, r = core.call("drawing_new", {"source_doc_id": cdid, "name": "Box - Drawing"}); cdwg = r["doc_id"]
    for t, o, p in VIEWS:
        core.call("drawing_add_view", {"doc_id": cdwg, "type": t, "orientation": o, "position": p})

    ps = docsess.new_session("part"); docsess.apply_ops(ps, 0, ops, None)
    pr = drawingmod.new_drawing(ps.doc_id, "Box - Drawing")
    pdwg = pr["doc_id"]
    for t, o, p in VIEWS:
        drawingmod.add_view(pdwg, type=t, orientation=o, position=p)

    for fmt in ("dxf", "pdf"):
        rc, cr = core.call("export_drawing", {"doc_id": cdwg, "path": str(tmp / f"d_cpp.{fmt}"),
                                              "format": fmt})
        pyr = export2d.export_drawing(pdwg, str(tmp / f"d_py.{fmt}"), fmt)
        ok = (cr.get("visible") == pyr.get("visible") and cr.get("hidden") == pyr.get("hidden")
              and cr.get("entities") == pyr.get("entities"))
        fails += check(f"export_drawing/{fmt}", ok,
                       f"cpp vis/hid/ent={cr.get('visible')}/{cr.get('hidden')}/{cr.get('entities')} "
                       f"py={pyr.get('visible')}/{pyr.get('hidden')}/{pyr.get('entities')}")

    # ---- drawing_bom: a PART-source drawing has an empty BOM (both engines) ------------
    rc, cbom = core.call("drawing_bom", {"doc_id": cdwg})
    pybom = drawingmod.drawing_bom(pdwg)
    ok = (cbom.get("rows") == [] and pybom.get("rows") == []
          and bool(cbom.get("is_assembly")) == bool(pybom.get("is_assembly")))
    fails += check("drawing_bom (part source)", ok,
                   f"cpp rows={len(cbom.get('rows', []))} py rows={len(pybom.get('rows', []))}")

    # ---- export_flat_dxf: sheet-metal flat pattern, compare blank invariants ----------
    fops = sheetmetal_flat_ops()
    rc, r = core.call("doc_new", {"kind": "part"}); cfid = r["doc_id"]
    core.call("doc_apply_ops", {"doc_id": cfid, "base_revision": 0, "ops": fops})
    fs = docsess.new_session("part"); docsess.apply_ops(fs, 0, fops, None)
    for fmt in ("dxf", "pdf"):
        rc, cr = core.call("export_flat_dxf", {"doc_id": cfid, "path": str(tmp / f"f_cpp.{fmt}"),
                                               "format": fmt})
        pyr = export2d.export_flat_dxf(fs.doc_id, str(tmp / f"f_py.{fmt}"), fmt)
        area_ok = abs(float(cr.get("flat_area", 0)) - float(pyr.get("flat_area", 0))) < 1e-3
        ok = (cr.get("bend_lines") == pyr.get("bend_lines")
              and cr.get("outline_points") == pyr.get("outline_points")
              and cr.get("entities") == pyr.get("entities") and area_ok)
        fails += check(f"export_flat_dxf/{fmt}", ok,
                       f"cpp bend/out/ent/area={cr.get('bend_lines')}/{cr.get('outline_points')}/"
                       f"{cr.get('entities')}/{cr.get('flat_area'):.3f} "
                       f"py={pyr.get('bend_lines')}/{pyr.get('outline_points')}/"
                       f"{pyr.get('entities')}/{float(pyr.get('flat_area', 0)):.3f}")

    print(f"\n[export2d-parity] {'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())

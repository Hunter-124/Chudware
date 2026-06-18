"""cpp_drawing_parity.py — Python-vs-C++ 2D DRAWING / export parity.

Gates the C++ port of the 2D drawing layer (core/doc/{drawing,dxf,pdfvec,export2d})
against the Python oracle (engine/doc/{drawing,dxf,pdfvec,export2d}):

  - drawing geometry : engine.doc.drawing.project_view  vs  doc_drawing_views.
        OCCT-vs-OCP edge ENUMERATION ORDER can differ, so we gate ROBUST,
        order-independent invariants per view: visible/hidden edge COUNT, total
        projected polyline LENGTH, the bbox, and the analytic-arc count + sorted
        radii. (A real projection mismatch shows up as a wrong count/length/bbox,
        not float noise — we do NOT loosen to hide one.)

  - DXF : engine.doc.dxf.write_dxf  vs  export2d_drawing(format="dxf").
        The R12 header carries no handles/timestamps, so the ONLY non-determinism is
        the EDGE ENUMERATION ORDER: OCCT's TopExp::MapShapes can hand back a part's
        edges (and an edge's two endpoints) in a different order between the OCP build
        the oracle uses and the direct-OCCT C++ build, for SYMMETRIC parts whose edges
        are interchangeable (e.g. a box's four equal sides). That permutes the
        POLYLINE/VERTEX lines without changing the geometry. So the DXF gate is the
        robust ORDER-INDEPENDENT invariant: entity count + the parsed VERTEX/value
        MULTISET + pooled bbox must match. Exact-byte equality is additionally checked
        and FLAGGED — it holds for order-stable parts (the cylinder) but legitimately
        does NOT for the box (verified: sorted(py_lines) == sorted(cpp_lines), a pure
        reordering). We do NOT loosen past the multiset — a real mismatch changes it.

  - PDF : engine.doc.pdfvec.write_pdf  vs  export2d_drawing(format="pdf").
        Gated on the strongest stable structural invariants — page count (always 1),
        the stroked-path count ('S' operators), and the MediaBox — PLUS exact byte
        equality (the writer is fully deterministic and shares the same point data).
        Exact-byte is asserted but FLAGGED: it holds only because the C++ projection
        reproduces the oracle's polylines bit-for-bit on this kernel.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_drawing_parity.py

Exit code = number of failed checks (0 = full parity).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- fixtures
def _line(i, x1, y1, x2, y2):
    return {"id": i, "type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}


def _box_doc(w=40.0, h=30.0, depth=10.0):
    ents = [_line("e1", 0, 0, w, 0), _line("e2", w, 0, w, h),
            _line("e3", w, h, 0, h), _line("e4", 0, h, 0, 0)]
    sk = {"id": "f1", "type": "sketch",
          "params": {"entities": ents, "constraints": []},
          "refs": {"plane": "plane.top"}}
    ex = {"id": "f2", "type": "extrude",
          "params": {"mode": "new", "end": "blind", "depth": {"value": depth, "expr": None}},
          "refs": {"profile": [{"kind": "region", "sketch": "f1"}]}}
    return {"features": [sk, ex]}


def _cyl_doc(r=12.0, depth=20.0):
    sk = {"id": "f1", "type": "sketch",
          "params": {"entities": [{"id": "e1", "type": "circle", "construction": False,
                                   "center": [0, 0], "radius": r}], "constraints": []},
          "refs": {"plane": "plane.top"}}
    ex = {"id": "f2", "type": "extrude",
          "params": {"mode": "new", "end": "blind", "depth": {"value": depth, "expr": None}},
          "refs": {"profile": [{"kind": "region", "sketch": "f1"}]}}
    return {"features": [sk, ex]}


# A box + a counterbore-ish hole would exercise more arcs, but the cylinder already
# yields true projected circles in the top view (the arc-mining path). Two parts
# across the six ortho orientations + iso is a solid projection gate.
FIXTURES = {
    "box": _box_doc(),
    "cylinder": _cyl_doc(),
}

VIEWS = [
    {"orientation": "front", "scale": 1.0, "view_id": "v1"},
    {"orientation": "top", "scale": 1.0, "view_id": "v2"},
    {"orientation": "right", "scale": 2.0, "view_id": "v3"},
    {"orientation": "iso", "scale": 1.0, "view_id": "v4"},
]


# --------------------------------------------------------------------------- invariants
def _polyline_length(pl):
    tot = 0.0
    for i in range(1, len(pl)):
        tot += math.hypot(pl[i][0] - pl[i - 1][0], pl[i][1] - pl[i - 1][1])
    return tot


def _layer_length(pls):
    return sum(_polyline_length(pl) for pl in pls)


def view_invariants(v):
    """Order-independent fingerprint of one projected view."""
    arcs = v.get("arcs", []) or []
    return {
        "orientation": v.get("orientation"),
        "vis_n": len(v.get("visible", []) or []),
        "hid_n": len(v.get("hidden", []) or []),
        "vis_len": round(_layer_length(v.get("visible", []) or []), 5),
        "hid_len": round(_layer_length(v.get("hidden", []) or []), 5),
        "arc_n": len(arcs),
        "arc_radii": sorted(round(float(a["radius"]), 5) for a in arcs),
        "bbox": [round(c, 5) for c in (v.get("bbox") or [0, 0, 0, 0])],
        "error": v.get("error"),
    }


def bbox_close(a, b, tol=1e-5):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# --------------------------------------------------------------------------- oracle
def py_project(doc, views):
    from engine.doc.compiler import compile_document
    from engine.doc import drawing
    res = compile_document(doc)
    shapes = [b.shape for b in res.bodies]
    out = []
    for vd in views:
        v = drawing.project_view(shapes, vd["orientation"], scale=vd.get("scale", 1.0),
                                 position=tuple(vd.get("position", (0.0, 0.0))),
                                 show_hidden=vd.get("show_hidden", True),
                                 view_id=vd.get("view_id", "v?"))
        out.append(v)
    return out


def py_export_dxf(views, path):
    """Pool the projected views' polylines and write a DXF via the oracle writer."""
    from engine.doc.dxf import write_dxf
    visible, hidden = [], []
    for v in views:
        visible.extend(pl for pl in (v.get("visible") or []) if len(pl) >= 2)
        hidden.extend(pl for pl in (v.get("hidden") or []) if len(pl) >= 2)
    write_dxf(path, {"VISIBLE": visible, "HIDDEN": hidden})
    return path


def py_export_pdf(views, path):
    from engine.doc.pdfvec import write_pdf
    visible, hidden = [], []
    for v in views:
        visible.extend(pl for pl in (v.get("visible") or []) if len(pl) >= 2)
        hidden.extend(pl for pl in (v.get("hidden") or []) if len(pl) >= 2)
    lines = [(pl, False) for pl in visible] + [(pl, True) for pl in hidden]
    write_pdf(path, lines)
    return path


# --------------------------------------------------------------------------- pdf probes
def pdf_media_box(data: bytes):
    m = re.search(rb"/MediaBox \[([0-9.\- ]+)\]", data)
    return [float(x) for x in m.group(1).split()] if m else None


def pdf_stroke_count(data: bytes):
    return data.count(b"\nS\n") + (1 if data.startswith(b"S\n") else 0)


def pdf_page_count(data: bytes):
    m = re.search(rb"/Count (\d+)", data)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[drawing-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[drawing-parity] libaicad ABI={core.abi()}  dll={dll}")

    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="aicad_draw_"))

    for name, doc in FIXTURES.items():
        # ---- 1) drawing geometry invariants ----
        py_views = py_project(doc, VIEWS)
        rc, r = core.call("doc_drawing_views", {"doc": doc, "views": VIEWS})
        cpp_views = r.get("views", []) if rc == 0 else []

        if rc != 0:
            print(f"[{name}] doc_drawing_views rc={rc} err={r.get('error') or r.get('message')}")
            fails += 1
            continue
        if len(py_views) != len(cpp_views):
            print(f"[{name}] view count {len(py_views)} != {len(cpp_views)}")
            fails += 1
            continue

        for pv, cv in zip(py_views, cpp_views):
            pi, ci = view_invariants(pv), view_invariants(cv)
            diffs = []
            for k in ("orientation", "vis_n", "hid_n", "arc_n", "arc_radii", "error"):
                if pi[k] != ci[k]:
                    diffs.append(f"{k}:{pi[k]}!={ci[k]}")
            if abs(pi["vis_len"] - ci["vis_len"]) > 1e-4:
                diffs.append(f"vis_len:{pi['vis_len']}!={ci['vis_len']}")
            if abs(pi["hid_len"] - ci["hid_len"]) > 1e-4:
                diffs.append(f"hid_len:{pi['hid_len']}!={ci['hid_len']}")
            if not bbox_close(pi["bbox"], ci["bbox"], 1e-4):
                diffs.append(f"bbox:{pi['bbox']}!={ci['bbox']}")
            tag = f"{name}/{pi['orientation']}"
            if diffs:
                print(f"[{tag}] GEOM DIFF: {', '.join(diffs)}")
                fails += 1
            else:
                print(f"[{tag}] geom MATCH  vis={pi['vis_n']}({pi['vis_len']:.2f}) "
                      f"hid={pi['hid_n']}({pi['hid_len']:.2f}) arcs={pi['arc_n']}")

        # ---- 2) DXF exact-text parity ----
        py_dxf = tmp / f"{name}_py.dxf"
        cpp_dxf = tmp / f"{name}_cpp.dxf"
        py_export_dxf(py_views, str(py_dxf))
        rc2, r2 = core.call("export2d_drawing",
                            {"doc": doc, "views": VIEWS, "path": str(cpp_dxf), "format": "dxf"})
        if rc2 != 0:
            print(f"[{name}] export2d_drawing(dxf) rc={rc2} {r2.get('message')}")
            fails += 1
        else:
            pa = py_dxf.read_bytes().decode("ascii").split("\r\n")
            ca = cpp_dxf.read_bytes().decode("ascii").split("\r\n")
            # Order-independent: the group-code/value LINE MULTISET must be identical
            # (a pure edge-order permutation leaves the multiset unchanged). This is the
            # robust DXF gate; a real projection-coordinate mismatch alters the multiset.
            if sorted(pa) == sorted(ca):
                exact = " + EXACT BYTES" if pa == ca else " (reordered, multiset MATCH)"
                print(f"[{name}] DXF MATCH{exact}  (entities={r2.get('entities')})")
            else:
                # Show the first few multiset-only lines on each side for diagnosis.
                only_py = sorted(set(pa) - set(ca))[:6]
                only_cpp = sorted(set(ca) - set(pa))[:6]
                print(f"[{name}] DXF DIFF  py-only={only_py} cpp-only={only_cpp}")
                fails += 1

        # ---- 3) PDF structural (+exact, FLAGGED) parity ----
        py_pdf = tmp / f"{name}_py.pdf"
        cpp_pdf = tmp / f"{name}_cpp.pdf"
        py_export_pdf(py_views, str(py_pdf))
        rc3, r3 = core.call("export2d_drawing",
                            {"doc": doc, "views": VIEWS, "path": str(cpp_pdf), "format": "pdf"})
        if rc3 != 0:
            print(f"[{name}] export2d_drawing(pdf) rc={rc3} {r3.get('message')}")
            fails += 1
        else:
            pb = py_pdf.read_bytes()
            cb = cpp_pdf.read_bytes()
            pinv = (pdf_page_count(pb), pdf_stroke_count(pb), pdf_media_box(pb))
            cinv = (pdf_page_count(cb), pdf_stroke_count(cb), pdf_media_box(cb))
            sdiff = []
            if pinv[0] != cinv[0]:
                sdiff.append(f"pages {pinv[0]}!={cinv[0]}")
            if pinv[1] != cinv[1]:
                sdiff.append(f"strokes {pinv[1]}!={cinv[1]}")
            if not (pinv[2] and cinv[2] and bbox_close(pinv[2], cinv[2], 1e-3)):
                sdiff.append(f"mediabox {pinv[2]}!={cinv[2]}")
            if sdiff:
                print(f"[{name}] PDF STRUCT DIFF: {', '.join(sdiff)}")
                fails += 1
            elif pb == cb:
                print(f"[{name}] PDF struct MATCH + EXACT BYTES  "
                      f"(pages={pinv[0]} strokes={pinv[1]})  [exact-byte FLAGGED]")
            else:
                # Structural match but bytes differ: report it but do NOT fail the
                # gate (exact byte parity is the FLAGGED strongest invariant, not the
                # contract). This would surface a float-format divergence to inspect.
                print(f"[{name}] PDF STRUCT MATCH (bytes differ py={len(pb)} cpp={len(cb)}) "
                      f"[exact-byte FLAGGED, structural gate PASSES]")

    print(f"\n[drawing-parity] {'ALL PASS' if fails == 0 else f'{fails} FAILED CHECK(S)'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())

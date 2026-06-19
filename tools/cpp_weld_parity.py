"""cpp_weld_parity.py — Python-vs-C++ weldment cut-list parity.

Gates the C++ doc-engine weld_cut_list method (libaicad.dll) against the Python oracle
(engine/doc/cutlist.py::cut_list_for_session via engine/worker.py::_weld_cut_list). Each
weldment part is built through the SAME doc_apply_ops batch on both engines (a sketch with
structural-member line segments + a weld_member feature), then the cut list is built on
each and the full grouped table is compared.

Cut-list comparison rationale:
  * The member `length` is the swept-axis length: the exact segment length for a straight
    member (read off the sketch line endpoints), and the true ARC LENGTH for an arc member
    (the oracle's edge.length / the C++ port's BRepGProp::LinearProperties — the same GProp
    measure, so identical to kernel precision). So length_mm / total_length_mm match to 1e-6.
  * rows are grouped by (profile, resolved-dims, length@0.1mm) then sorted (profile asc,
    length desc) with 1-based item numbers. The grouping/sort/description logic is the ported
    pure function, so item / profile / description / quantity / structure must match EXACTLY.
  * profiles + total_members are exact.

Both LINE and ARC segments are weldment paths (a closed circle is skipped, like the oracle's
_segment_edges). Every case uses trim_corners=False so each stored length equals its exact
swept-axis length (matching the oracle corpus in engine/doc/test_weld_cutlist.py). Arc cases
keep ccw=True (the default): the C++ solver round-trips an arc's center/radius/start/end but
not its `ccw` flag, while the oracle reads the raw entity — so only the shared default is
exercised, which is all the cut list needs (length is direction-independent regardless).

Run under the venv:
  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" \\
        tools\\cpp_weld_parity.py --dll core\\build-occt\\bin\\Release\\libaicad.dll

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- ops
def _sketch(plane="plane.top"):
    return {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch",
                                             "params": {"entities": [], "constraints": []},
                                             "refs": {"plane": plane}}}


def _line(sketch, x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": sketch,
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def _arc(sketch, cx, cy, r, start, end, ccw=True):
    """An OPEN arc segment (degrees for start/end), a valid weld member path. ccw stays
    True so both engines build the same arc (the C++ solver does not round-trip ccw)."""
    return {"op": "sketch_add_entity", "sketch": sketch,
            "entity": {"type": "arc", "construction": False, "center": [cx, cy],
                       "radius": r, "start": start, "end": end, "ccw": ccw}}


def _weld(sketch, profile, dims, segments=None, trim=False):
    params = {"profile": profile, "trim_corners": trim}
    params.update(dims)
    refs = {"sketch": sketch}
    if segments is not None:
        refs["segments"] = segments
    return {"op": "add_feature",
            "feature": {"type": "weld_member", "name": "Weld", "params": params, "refs": refs}}


def weldment(segments_xy, profile, dims, sketch_ph="$0", trim=False, seg_filter=None):
    """One sketch (line segments) + one weld_member feature. `segments_xy` is a list of
    ((x1,y1),(x2,y2)) line endpoints; the sketch is the op at index `sketch_ph`."""
    ops = [_sketch()]
    for (a, b) in segments_xy:
        ops.append(_line(sketch_ph, a[0], a[1], b[0], b[1]))
    ops.append(_weld(sketch_ph, profile, dims, segments=seg_filter, trim=trim))
    return ops


def weldment_arcs(arcs, profile, dims, lines=None, sketch_ph="$0", trim=False, seg_filter=None):
    """One sketch with ARC (and optional LINE) segments + one weld_member feature.

    `arcs` is a list of (cx, cy, r, start_deg, end_deg[, ccw]); `lines` (optional, added
    FIRST so they take the lower entity ids e1..) is a list of ((x1,y1),(x2,y2)). An arc's
    swept length is r * span_radians, so the oracle and the C++ port (both GProp linear
    length) agree to kernel precision."""
    ops = [_sketch()]
    for ln in (lines or []):
        ops.append(_line(sketch_ph, ln[0][0], ln[0][1], ln[1][0], ln[1][1]))
    for a in arcs:
        ccw = a[5] if len(a) > 5 else True
        ops.append(_arc(sketch_ph, a[0], a[1], a[2], a[3], a[4], ccw))
    ops.append(_weld(sketch_ph, profile, dims, segments=seg_filter, trim=trim))
    return ops


# --------------------------------------------------------------------------- engines
def py_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("part")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)
    return PyS()


class CppS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]

    def apply(self, base_rev, ops):
        rc, r = self.core.call("doc_apply_ops",
                               {"doc_id": self.doc_id, "base_revision": base_rev, "ops": ops})
        return r


# --------------------------------------------------------------------------- oracle
def py_cutlist(py):
    from engine.doc import cutlist
    return cutlist.cut_list_for_session(py.s)


# --------------------------------------------------------------------------- compare
def _rel(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def compare_cutlist(pres, cres):
    """Return a list of diff strings ([] == match)."""
    diffs = []
    if int(pres["total_members"]) != int(cres["total_members"]):
        diffs.append(f"total_members {pres['total_members']}!={cres['total_members']}")
    if not _rel(pres["total_length_mm"], cres["total_length_mm"]):
        diffs.append(f"total_length {pres['total_length_mm']}!={cres['total_length_mm']}")
    if list(pres["profiles"]) != list(cres["profiles"]):
        diffs.append(f"profiles {pres['profiles']}!={cres['profiles']}")

    pr, cr = pres["rows"], cres["rows"]
    if len(pr) != len(cr):
        diffs.append(f"row count {len(pr)}!={len(cr)}")
        return diffs
    for i, (p, c) in enumerate(zip(pr, cr)):
        if int(p["item"]) != int(c["item"]):
            diffs.append(f"row{i}.item {p['item']}!={c['item']}")
        if p["profile"] != c["profile"]:
            diffs.append(f"row{i}.profile {p['profile']}!={c['profile']}")
        if p["description"] != c["description"]:
            diffs.append(f"row{i}.description {p['description']!r}!={c['description']!r}")
        if not _rel(p["length_mm"], c["length_mm"]):
            diffs.append(f"row{i}.length_mm {p['length_mm']}!={c['length_mm']}")
        if int(p["quantity"]) != int(c["quantity"]):
            diffs.append(f"row{i}.quantity {p['quantity']}!={c['quantity']}")
        if not _rel(p["total_length_mm"], c["total_length_mm"]):
            diffs.append(f"row{i}.total_length_mm {p['total_length_mm']}!={c['total_length_mm']}")
    return diffs


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[weld-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[weld-parity] libaicad ABI={core.abi()}")

    mism = 0
    total = 0

    # Each case: (label, ops). The oracle path is cut_list_for_session; the C++ path is
    # weld_cut_list. Lines are chosen so trim_corners=False keeps stored length == segment
    # length, exactly mirroring engine/doc/test_weld_cutlist.py worked examples.
    SQ = {"size": 20.0, "thickness": 2.0}
    cases = [
        # Two equal 100mm legs + one 80mm -> 2 grouped rows (the 100mm pair quantity 2).
        ("group identical (3 mem -> 2 rows)",
         weldment([((0, 0), (100, 0)), ((100, 0), (100, 80)), ((100, 80), (0, 80))],
                  "square_tube", SQ)),
        # Three distinct lengths -> three rows.
        ("distinct lengths (3 rows)",
         weldment([((0, 0), (60, 0)), ((60, 0), (60, 90)), ((60, 90), (60, 210))],
                  "square_tube", SQ)),
        # Length-desc within a profile (flat_bar).
        ("length-desc within profile",
         weldment([((0, 0), (40, 0)), ((0, 30), (160, 30))],
                  "flat_bar", {"w": 40.0, "thickness": 6.0})),
        # Single member -> one row, human description.
        ("single member (description)",
         weldment([((0, 0), (100, 0))], "square_tube", SQ)),
        # rect_tube (asymmetric dims order w,h,thickness in the description).
        ("rect_tube description",
         weldment([((0, 0), (120, 0))], "rect_tube", {"w": 60.0, "h": 40.0, "thickness": 3.0})),
        # round_tube + angle_L + c_channel descriptions (each library label).
        ("round_tube description",
         weldment([((0, 0), (75, 0))], "round_tube", {"od": 20.0, "thickness": 2.0})),
        ("angle_L description",
         weldment([((0, 0), (90, 0))], "angle_L", {"leg": 30.0, "thickness": 3.0})),
        ("c_channel description",
         weldment([((0, 0), (50, 0))], "c_channel", {"h": 50.0, "w": 25.0, "thickness": 3.0})),
        # i_beam + t_section -> fallback (title-cased) descriptions in the oracle.
        ("i_beam fallback description",
         weldment([((0, 0), (200, 0))], "i_beam", {"h": 100.0, "w": 50.0, "tw": 5.0, "tf": 7.0})),
        ("t_section fallback description",
         weldment([((0, 0), (120, 0))], "t_section", {"h": 60.0, "w": 60.0, "tw": 6.0, "tf": 6.0})),
        # Default dims (only profile supplied) -> resolved-dims must match defaults.
        ("default dims (square_tube)",
         weldment([((0, 0), (50, 0))], "square_tube", {})),
        # Segment filter: only e1, e3 swept (skip the middle).
        ("segment filter",
         weldment([((0, 0), (100, 0)), ((100, 0), (100, 50)), ((100, 50), (0, 50))],
                  "square_tube", SQ, seg_filter=["e1", "e3"])),
        # ---- ARC members (the new path) -------------------------------------
        # Single quarter arc r=100 (0..90deg): length = 100*pi/2 ~= 157.08mm -> one row.
        ("single arc member",
         weldment_arcs([(0, 0, 100, 0, 90)], "square_tube", SQ)),
        # Two IDENTICAL quarter arcs -> one grouped row, quantity 2 (length-grouping of
        # arc lengths, not chords — a chord would be ~141.4, the arc is ~157.08).
        ("two identical arcs (qty 2)",
         weldment_arcs([(0, 0, 100, 0, 90), (300, 0, 100, 0, 90)], "square_tube", SQ)),
        # Distinct arc lengths (r=100 vs r=60 over 90deg) -> two rows, length desc.
        ("distinct arc lengths (2 rows)",
         weldment_arcs([(0, 0, 100, 0, 90), (400, 0, 60, 0, 90)], "square_tube", SQ)),
        # Non-zero start angle (45..135deg, span still 90) — length depends on the SPAN,
        # not the absolute angles, so it equals the 0..90 case for the same radius.
        ("arc nonzero start angle",
         weldment_arcs([(50, 50, 120, 45, 135)], "square_tube", SQ)),
        # Semicircle r=50 (0..180deg): length = 50*pi ~= 157.08mm -> single row.
        ("semicircle arc member",
         weldment_arcs([(0, 0, 50, 0, 180)], "square_tube", SQ)),
        # Mixed: one straight 120mm line + one quarter arc r=100 in ONE feature -> two
        # rows (the line=120.0, the arc=157.08) — exercises line+arc pooling.
        ("mixed line + arc (2 rows)",
         weldment_arcs([(0, 0, 100, 0, 90)], "square_tube", SQ,
                       lines=[((0, -300), (120, -300))])),
        # round_tube arc (r=80, 0..90deg => ~125.66mm) — arc length with a non-square
        # profile's library description.
        ("round_tube arc description",
         weldment_arcs([(0, 0, 80, 0, 90)], "round_tube", {"od": 20.0, "thickness": 2.0})),
        # Arc segment filter: two arcs added (e1,e2), keep only e1 -> one member.
        ("arc segment filter",
         weldment_arcs([(0, 0, 100, 0, 90), (300, 0, 100, 0, 90)],
                       "square_tube", SQ, seg_filter=["e1"])),
    ]
    for label, ops in cases:
        total += 1
        py = py_session(); py.apply(0, ops)
        cpp = CppS(core); cpp.apply(0, ops)
        try:
            pres = py_cutlist(py)
        except Exception as exc:  # noqa: BLE001
            print(f"  {('weld_cut_list ' + label):<40} DIFF: py raised {type(exc).__name__}: {exc}")
            mism += 1
            continue
        rc, cres = core.call("weld_cut_list", {"doc_id": cpp.doc_id})
        if rc != 0:
            print(f"  {('weld_cut_list ' + label):<40} DIFF: cpp rc={rc} {str(cres)[:70]}")
            mism += 1
            continue
        diffs = compare_cutlist(pres, cres)
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
        print(f"  {('weld_cut_list ' + label):<40} {verdict}")
        mism += 0 if not diffs else 1

    # ---- multi-feature: two profiles pooled across two weld_member features ----
    # square_tube legs (100,100) on sketch f0; round_tube leg (50) on sketch f3.
    total += 1
    multi = [
        _sketch(), _line("$0", 0, 0, 100, 0), _line("$0", 0, 50, 100, 50),
        _weld("$0", "square_tube", SQ),
        _sketch(), _line("$4", 0, 0, 50, 0),
        _weld("$4", "round_tube", {"od": 20.0, "thickness": 2.0}),
    ]
    py = py_session(); py.apply(0, multi)
    cpp = CppS(core); cpp.apply(0, multi)
    try:
        pres = py_cutlist(py)
        rc, cres = core.call("weld_cut_list", {"doc_id": cpp.doc_id})
        diffs = ["cpp rc=%d" % rc] if rc != 0 else compare_cutlist(pres, cres)
    except Exception as exc:  # noqa: BLE001
        diffs = [f"py raised {type(exc).__name__}: {exc}"]
    verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
    print(f"  {'weld_cut_list two profiles 2 feats':<40} {verdict}")
    mism += 0 if not diffs else 1

    # ---- empty: a part with NO weld_member feature -> empty rows + zero totals ----
    total += 1
    empty = [
        {"op": "add_feature", "feature": {"type": "box", "name": "Box", "params": {
            "length": {"value": 20, "expr": None}, "width": {"value": 20, "expr": None},
            "height": {"value": 20, "expr": None}}}},
    ]
    py = py_session(); py.apply(0, empty)
    cpp = CppS(core); cpp.apply(0, empty)
    try:
        pres = py_cutlist(py)
        rc, cres = core.call("weld_cut_list", {"doc_id": cpp.doc_id})
        diffs = ["cpp rc=%d" % rc] if rc != 0 else compare_cutlist(pres, cres)
        if not diffs and (cres["rows"] or cres["total_members"]):
            diffs = ["cpp non-empty for no-weldment part"]
    except Exception as exc:  # noqa: BLE001
        diffs = [f"py raised {type(exc).__name__}: {exc}"]
    verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
    print(f"  {'weld_cut_list no weldment (empty)':<40} {verdict}")
    mism += 0 if not diffs else 1

    print(f"\n[weld-parity] {total - mism}/{total} checks at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

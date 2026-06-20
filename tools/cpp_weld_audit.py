"""Adversarial audit of the weldment cut-list (py-vs-C++ doc engine), focused on the
freshly-landed ARC-member support (2aa9cd57/575de8d9) and the (profile, dims,
length@0.1mm) grouping logic. cpp_weld_parity covers the base cases; this stresses
the corners fresh code gets wrong:

  * mixed straight + arc members of EQUAL swept length -> must group into one row;
  * arcs of different radii/spans -> distinct length rows, correct arc-length;
  * a closed rectangular frame -> quantity grouping (2 + 2);
  * wide-span arcs (near-full-circle) -> r*span precision at the extreme;
  * length-grouping boundary (within vs beyond the 0.1 mm bucket);
  * trim_corners=True alongside arcs;
  * a non-top sketch plane (world-frame arc lift).

Compared with cpp_weld_parity's own compare_cutlist, so a DIFF is a real divergence."""
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402
from tools.cpp_weld_parity import (py_session, CppS, py_cutlist, compare_cutlist,
                                   weldment, weldment_arcs)  # noqa: E402

PROF = "square_tube"
DIMS = {"size": 20.0, "thickness": 2.0}


def quarter_len(r):
    return r * math.pi / 2.0  # 90-degree arc swept length


def cases():
    out = []

    # 1) mixed straight + arc of EQUAL swept length (line len = quarter-arc len) -> group.
    L = quarter_len(20.0)
    out.append(("mixed straight+arc equal length",
                weldment_arcs([(0, 0, 20.0, 0, 90)], PROF, DIMS,
                              lines=[((100, 0), (100 + L, 0))])))

    # 2) two arcs, different radii -> two distinct length rows.
    out.append(("two arcs different radii",
                weldment_arcs([(0, 0, 20.0, 0, 90), (200, 0, 35.0, 0, 90)], PROF, DIMS)))

    # 3) closed rectangular frame (4 lines: 2x 120, 2x 60) -> quantity 2 + 2.
    out.append(("closed rect frame 4 lines",
                weldment([((0, 0), (120, 0)), ((120, 0), (120, 60)),
                          ((120, 60), (0, 60)), ((0, 60), (0, 0))], PROF, DIMS)))

    # 4) wide-span arc (350 deg) -> arc-length precision near full circle.
    out.append(("wide-span arc 350deg",
                weldment_arcs([(0, 0, 30.0, 0, 350)], PROF, DIMS)))

    # 5) length-grouping: two lines that round to the SAME 0.1mm bucket -> group (qty 2).
    out.append(("group within 0.1mm bucket",
                weldment([((0, 0), (100.00, 0)), ((0, 50), (100.03, 50))], PROF, DIMS)))

    # 6) length-grouping: two lines a clear 0.5mm apart -> two rows.
    out.append(("split beyond 0.1mm bucket",
                weldment([((0, 0), (100.0, 0)), ((0, 50), (100.5, 50))], PROF, DIMS)))

    # 7) trim_corners=True with an arc present.
    out.append(("trim_corners with arc",
                weldment_arcs([(0, 0, 25.0, 0, 90)], PROF, DIMS,
                              lines=[((0, 0), (80, 0))], trim=True)))

    # 8) arc on a NON-top plane (front) -> world-frame lift of the swept axis.
    out.append(("arc on front plane",
                weldment_arcs([(0, 0, 28.0, 10, 160)], PROF, DIMS, plane="plane.front")))

    # 9) three mixed members spanning two profiles is out of scope (single weld feature),
    # so instead: a long chain of 5 collinear-ish lines of distinct lengths -> 5 rows sorted.
    out.append(("five distinct-length lines",
                weldment([((0, 0), (40, 0)), ((0, 10), (95, 10)), ((0, 20), (60, 20)),
                          ((0, 30), (140, 30)), ((0, 40), (75, 40))], PROF, DIMS)))

    return out


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[weld-audit] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[weld-audit] libaicad ABI={core.abi()}")
    mism = 0
    cs = cases()
    for label, ops in cs:
        try:
            py = py_session(); py.apply(None, ops)
            cpp = CppS(core); cpp.apply(None, ops)
            pres = py_cutlist(py)
            rc, cres = core.call("weld_cut_list", {"doc_id": cpp.doc_id})
            if rc != 0:
                print(f"  {label:<34} DIFF: cpp weld_cut_list rc={rc}: {cres}")
                mism += 1
                continue
            diffs = compare_cutlist(pres, cres)
            if diffs:
                print(f"  {label:<34} DIFF: {'; '.join(diffs)[:100]}")
                mism += 1
            else:
                print(f"  {label:<34} MATCH  ({len(pres['rows'])} row(s), "
                      f"{pres['total_members']} members)")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<34} DIFF: raised {type(exc).__name__}: {exc}")
            mism += 1
    print(f"\n[weld-audit] {len(cs) - mism}/{len(cs)} MATCH ; {mism} divergence(s)")
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""cpp_audit3_parity.py — THIRD adversarial audit: feature INTERACTIONS + ORDERING.

A read-only differential bug hunt over the C++ doc-engine feature compilers, the
companion to ``tools/cpp_audit_parity.py`` (56 single-feature param combos) and
``tools/cpp_audit2_parity.py`` (50 sketch/eval/asm cases). Those two throw curated
PARAMETER variety at one feature at a time; the gap they leave is FEATURE
INTERACTIONS — how compilers compose when one feature's body feeds the next, and
how the result depends on ORDER. The two prior adversarial passes found 23 real
bugs the curated fixtures missed; this one targets the next tier:

  * pattern / mirror of a SUBTRACTIVE feature (cut, hole) — must re-apply the
    subtraction per instance (N holes), NOT fuse N copies of the holed body;
  * nested patterns (linear of circular, circular of linear) and pattern <-> mirror
    compositions, including a subtractive inner feature;
  * draft/fillet/chamfer ORDER sensitivity on a shared body;
  * variable-radius fillet on a patterned body;
  * boolean whose tool body is itself a pattern;
  * deep multi-feature dependency chains (8+ features), then EDIT an early feature
    and recompile (the whole batch — incl. the edit — is replayed on both engines).

Each case drives the SAME ``doc_apply_ops`` op batch through the Python ORACLE
(engine.doc) and the C++ core (libaicad.dll) and compares the geometry signature
(volume ~1e-6 rel, body count, BREP solid/face count + bbox, per-feature status).
It REUSES the machinery of ``tools/cpp_audit_parity.py`` (Batch op builder, both
engine wrappers, the geometry-signature comparator ``audit()``); see that file for
the signature/tolerance rationale. A DIVERGENCE is a SUCCESS of the audit.

CRITICAL — feature ids are MINTED (idgen "f1","f2",...) and an explicit ``id`` in
an add_feature op is IGNORED by BOTH engines, so a pattern/mirror seed MUST be
referenced by the ``$N`` op-index placeholder that ``Batch.feature()`` returns —
never a hardcoded "fN" (the existing audit's "linear pattern OF a cut" case points
its seed at the wrong id, so both engines fail the pattern identically and it
falsely "matches"; this audit wires seeds correctly and exposes the real divergence).

Run under the venv against the freshly-built DLL:
  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" tools\\cpp_audit3_parity.py \
        --dll core\\build-occt\\bin\\Release\\libaicad.dll

Exit code = number of divergences + gaps (0 = full parity / no bugs in these combos).
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Reuse the part-audit machinery wholesale (op builder, engine wrappers, comparator).
from tools import cpp_audit_parity as A          # noqa: E402
from tools.cpp_audit_parity import Batch, _dim, _region_ref as R, audit  # noqa: E402
from tools.cpp_parity import CppCore             # noqa: E402


# --------------------------------------------------------------------------- builders
def boss(b, sketch, params=None, name="X", profiles=None):
    """A boss extrude over a sketch region (default 40x30 rect, depth 20)."""
    p = {"mode": "boss", "end": "blind", "depth": _dim(20)}
    if params:
        p.update(params)
    return b.feature({"type": "extrude", "name": name, "params": p,
                      "refs": {"profile": profiles or [R(sketch, [20, 15], 1200.0)]}})


def cut_circle(b, plane_sketch_name, cx, cy, r, name="Cut", end="through_all", depth=None):
    """A through (or blind) circular cut sketched on plane.top; returns its placeholder."""
    s = b.sketch(plane_sketch_name, "plane.top")
    b.circle(s, cx, cy, r)
    p = {"mode": "cut", "end": end}
    if depth is not None:
        p["depth"] = _dim(depth)
    return b.feature({"type": "extrude", "name": name, "params": p,
                      "refs": {"profile": [R(s, [cx, cy], math.pi * r * r)]}})


# selectors reused across the dressing-order cases (a 40x30x20 base box)
VEDGE = {"kind": "edge", "surf": "line", "centroid": [0.0, 0.0, 10.0], "axis": [0.0, 0.0, 1.0],
         "area_or_len": 20.0, "owner_feature": None, "hint_index": 0}
VEDGE2 = {"kind": "edge", "surf": "line", "centroid": [40.0, 0.0, 10.0], "axis": [0.0, 0.0, 1.0],
          "area_or_len": 20.0, "owner_feature": None, "hint_index": 0}
VEDGE_X80 = {"kind": "edge", "surf": "line", "centroid": [80.0, 0.0, 10.0], "axis": [0.0, 0.0, 1.0],
             "area_or_len": 20.0, "owner_feature": None, "hint_index": 0}
XFACE_80 = {"kind": "face", "surf": "plane", "centroid": [80.0, 20.0, 10.0], "axis": [1, 0, 0],
            "area_or_len": 800.0, "owner_feature": None, "hint_index": 0}
XFACE = {"kind": "face", "surf": "plane", "centroid": [40.0, 15.0, 10.0], "axis": [1, 0, 0],
         "area_or_len": 600.0, "owner_feature": None, "hint_index": 0}
HFACE = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0], "axis": [0, 0, 1],
         "area_or_len": 1200.0, "owner_feature": None, "hint_index": 5}


def base_box(b, w=40, h=30, depth=20):
    s = b.rect_sketch(w=w, h=h)
    boss(b, s, {"depth": _dim(depth)}, "Base")
    return s


def run_cases(core, td):
    # ===================================================================== GROUP 1
    # Pattern / mirror of a SUBTRACTIVE feature. Python re-applies the cut/hole per
    # instance (the seed's tool_body + op are replicated); a wrong port that fuses
    # copies of the post-cut body instead gives a totally different solid.
    print("--- GROUP 1: pattern/mirror of a CUT/HOLE (subtractive) ---")

    # 1a. linear pattern of a through-cut -> a row of 4 holes in an 80x20 bar.
    b = Batch()
    s = b.rect_sketch(w=80, h=20)
    boss(b, s, {"depth": _dim(10)}, "Bar", [R(s, [40, 10], 1600.0)])
    cut = cut_circle(b, "C", 10, 10, 3)
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(4), "spacing1": _dim(15)},
               "refs": {"dir1": "axis.x", "features": [cut]}})
    audit(core, td, "linear pattern OF a cut (4 holes)", b.ops)

    # 1b. 2-D grid of holes (3x2).
    b = Batch()
    s = b.rect_sketch(w=80, h=50)
    boss(b, s, {"depth": _dim(10)}, "Bar", [R(s, [40, 25], 4000.0)])
    cut = cut_circle(b, "C", 12, 12, 3)
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(3), "spacing1": _dim(20),
                          "count2": _dim(2), "spacing2": _dim(20)},
               "refs": {"dir1": "axis.x", "dir2": "axis.y", "features": [cut]}})
    audit(core, td, "linear pattern OF a cut 3x2 grid", b.ops)

    # 1c. circular pattern of a cut -> radial bolt-circle holes in a disc.
    b = Batch()
    sc = b.sketch("D", "plane.top")
    b.circle(sc, 0, 0, 30)
    boss(b, sc, {"depth": _dim(10)}, "Disc", [R(sc, [0, 0], math.pi * 900)])
    cut = cut_circle(b, "C", 20, 0, 3)
    b.feature({"type": "circular_pattern", "name": "CP",
               "params": {"count": _dim(6), "angle": _dim(360)},
               "refs": {"axis": "axis.z", "features": [cut]}})
    audit(core, td, "circular pattern OF a cut (6 holes)", b.ops)

    # 1d. mirror of a cut feature across plane.right (x=0). Bar straddles x=0; the cut
    # is at +X, its reflection bores a second hole at -X. Whole-body mirror would
    # DOUBLE the holed bar (wrong); feature-mirror re-cuts the reflected tool.
    b = Batch()
    s = b.rect_sketch(x0=-40, y0=0, w=80, h=20)
    boss(b, s, {"depth": _dim(10)}, "Bar", [R(s, [0, 10], 1600.0)])
    cut = cut_circle(b, "C", 20, 10, 4)
    b.feature({"type": "mirror", "name": "M", "params": {},
               "refs": {"plane": "plane.right", "features": [cut]}})
    audit(core, td, "mirror OF a cut feature", b.ops)

    # 1e. linear pattern of a Hole-Wizard counterbore (also op="cut").
    b = Batch()
    s = b.rect_sketch(w=80, h=30)
    boss(b, s, {"depth": _dim(20)}, "Plate", [R(s, [40, 15], 2400.0)])
    hole = b.feature({"type": "hole", "name": "H",
                      "params": {"style": "cbore", "end": "through_all", "standard": "custom",
                                 "diameter": _dim(6), "cbore_dia": _dim(12), "cbore_depth": _dim(4)},
                      "refs": {"face": HFACE, "points": [[-25, 0]]}})
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(3), "spacing1": _dim(25)},
               "refs": {"dir1": "axis.x", "features": [hole]}})
    audit(core, td, "linear pattern OF a cbore hole", b.ops)

    # 1f. circular pattern of a simple hole.
    b = Batch()
    sc = b.sketch("D", "plane.top")
    b.circle(sc, 0, 0, 30)
    boss(b, sc, {"depth": _dim(12)}, "Disc", [R(sc, [0, 0], math.pi * 900)])
    hface = {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, 12.0], "axis": [0, 0, 1],
             "area_or_len": math.pi * 900, "owner_feature": None, "hint_index": 0}
    hole = b.feature({"type": "hole", "name": "H",
                      "params": {"style": "simple", "end": "through_all", "standard": "custom",
                                 "diameter": _dim(5)},
                      "refs": {"face": hface, "points": [[20, 0]]}})
    b.feature({"type": "circular_pattern", "name": "CP",
               "params": {"count": _dim(8), "angle": _dim(360)},
               "refs": {"axis": "axis.z", "features": [hole]}})
    audit(core, td, "circular pattern OF a simple hole", b.ops)

    # ===================================================================== GROUP 2
    # Nested patterns + pattern<->mirror compositions. With the tool_body/op model
    # these should MATCH; they guard against the fix breaking additive composition.
    print("--- GROUP 2: nested patterns + pattern/mirror composition ---")

    def cube(b, x0=0, y0=0, s=10, depth=10, name="Cube"):
        sk = b.rect_sketch(x0=x0, y0=y0, w=s, h=s, name="S" + name)
        return boss(b, sk, {"depth": _dim(depth)}, name, [R(sk, [x0 + s / 2, y0 + s / 2], s * s)])

    # NOTE: a pattern/mirror used AS A SEED must produce a SINGLE solid — the oracle
    # cannot .translate/.rotate a disjoint ShapeList seed (a pre-existing engine
    # limitation, orthogonal to this audit), so inner SEED patterns below are
    # CONTIGUOUS (abutting copies fuse to one solid). Final (outer) patterns may be
    # disjoint. This keeps every case a legitimate two-engine geometry comparison.

    # 2a. circular pattern OF a contiguous linear pattern (nested, additive).
    b = Batch()
    cube(b, x0=20, y0=-5)                                  # f2 boss, 10-cube at +X
    lp = b.feature({"type": "linear_pattern", "name": "LP",  # contiguous 3-cube bar
                    "params": {"count1": _dim(3), "spacing1": _dim(10)},
                    "refs": {"dir1": "axis.x", "features": ["f2"]}})
    b.feature({"type": "circular_pattern", "name": "CP",
               "params": {"count": _dim(4), "angle": _dim(360)},
               "refs": {"axis": "axis.z", "features": [lp]}})
    audit(core, td, "circular pattern OF a linear pattern", b.ops)

    # 2b. linear pattern OF a contiguous linear pattern (nested -> grid, additive).
    b = Batch()
    cube(b, x0=0, y0=0)                                    # f2 boss at origin corner
    lp = b.feature({"type": "linear_pattern", "name": "LP",  # contiguous bar along +X
                    "params": {"count1": _dim(3), "spacing1": _dim(10)},
                    "refs": {"dir1": "axis.x", "features": ["f2"]}})
    b.feature({"type": "linear_pattern", "name": "LP2",     # stack the bar along +Y
               "params": {"count1": _dim(2), "spacing1": _dim(10)},
               "refs": {"dir1": "axis.y", "features": [lp]}})
    audit(core, td, "linear pattern OF a linear pattern", b.ops)

    # 2c. mirror OF a contiguous linear pattern (additive feature mirror).
    b = Batch()
    cube(b, x0=10, y0=10)
    lp = b.feature({"type": "linear_pattern", "name": "LP",  # contiguous bar along +Y
                    "params": {"count1": _dim(3), "spacing1": _dim(10)},
                    "refs": {"dir1": "axis.y", "features": ["f2"]}})
    b.feature({"type": "mirror", "name": "M", "params": {},
               "refs": {"plane": "plane.right", "features": [lp]}})
    audit(core, td, "mirror OF a linear pattern", b.ops)

    # 2d. linear pattern OF a mirror (additive). The mirrored seed must be ONE solid, so
    # the cube touches x=0 (cube + its reflection share the x=0 face -> contiguous).
    b = Batch()
    cube(b, x0=0, y0=20)
    mr = b.feature({"type": "mirror", "name": "M", "params": {},
                    "refs": {"plane": "plane.right", "features": ["f2"]}})
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(2), "spacing1": _dim(70)},
               "refs": {"dir1": "axis.y", "features": [mr]}})
    audit(core, td, "linear pattern OF a mirror", b.ops)

    # 2e. linear pattern OF a circular-pattern-OF-a-cut (subtractive inner, nested).
    b = Batch()
    sc = b.sketch("D", "plane.top")
    b.circle(sc, 0, 0, 30)
    boss(b, sc, {"depth": _dim(10)}, "Disc", [R(sc, [0, 0], math.pi * 900)])
    cut = cut_circle(b, "C", 20, 0, 3)
    cp = b.feature({"type": "circular_pattern", "name": "CP",
                    "params": {"count": _dim(6), "angle": _dim(360)},
                    "refs": {"axis": "axis.z", "features": [cut]}})
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(2), "spacing1": _dim(60)},
               "refs": {"dir1": "axis.z", "features": [cp]}})
    audit(core, td, "linear pattern OF circ-pattern-of-cut", b.ops)

    # ===================================================================== GROUP 3
    # ORDER sensitivity: draft / fillet / chamfer applied in both orders.
    print("--- GROUP 3: draft / fillet / chamfer ordering ---")

    # 3a. draft (+X face) THEN fillet a vertical edge.
    b = Batch()
    base_box(b)
    b.feature({"type": "draft", "name": "Dr", "params": {"angle": _dim(8)},
               "refs": {"neutral": "plane.top", "faces": [XFACE]}})
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(3)},
               "refs": {"edges": [VEDGE]}})
    audit(core, td, "draft THEN fillet", b.ops)

    # 3b. fillet THEN draft (reverse order — different geometry than 3a).
    b = Batch()
    base_box(b)
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(3)},
               "refs": {"edges": [VEDGE]}})
    b.feature({"type": "draft", "name": "Dr", "params": {"angle": _dim(8)},
               "refs": {"neutral": "plane.top", "faces": [XFACE]}})
    audit(core, td, "fillet THEN draft", b.ops)

    # 3c. fillet one edge THEN chamfer an adjacent edge.
    b = Batch()
    base_box(b)
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(4)},
               "refs": {"edges": [VEDGE]}})
    b.feature({"type": "chamfer", "name": "C", "params": {"length": _dim(3)},
               "refs": {"edges": [VEDGE2]}})
    audit(core, td, "fillet THEN chamfer (adjacent edges)", b.ops)

    # 3d. chamfer THEN fillet (reverse).
    b = Batch()
    base_box(b)
    b.feature({"type": "chamfer", "name": "C", "params": {"length": _dim(3)},
               "refs": {"edges": [VEDGE]}})
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(4)},
               "refs": {"edges": [VEDGE2]}})
    audit(core, td, "chamfer THEN fillet (adjacent edges)", b.ops)

    # 3e. shell THEN fillet (hollow box, then round a remaining edge).
    b = Batch()
    base_box(b)
    b.feature({"type": "shell", "name": "Sh", "params": {"thickness": _dim(2)},
               "refs": {"remove": [HFACE]}})
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(2)},
               "refs": {"edges": [VEDGE]}})
    audit(core, td, "shell THEN fillet", b.ops)

    # ===================================================================== GROUP 4
    # variable-radius fillet on a PATTERNED body + boolean tool = a pattern.
    print("--- GROUP 4: fillet-on-pattern + boolean with patterned tool ---")

    # 4a. linear pattern of 3 cubes, then a variable-radius fillet on a seed edge.
    b = Batch()
    sk = b.rect_sketch(x0=0, y0=0, w=10, h=10)
    boss(b, sk, {"depth": _dim(10)}, "Cube", [R(sk, [5, 5], 100.0)])
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(3), "spacing1": _dim(20)},
               "refs": {"dir1": "axis.x", "features": ["f2"]}})
    b.feature({"type": "fillet", "name": "F",
               "params": {"radius": _dim(2), "radius2": _dim(5), "variable": True},
               "refs": {"edges": [VEDGE]}})
    audit(core, td, "variable-radius fillet on patterned body", b.ops)

    # 4b. boolean SUBTRACT whose tool body is itself a linear pattern (3 pegs).
    b = Batch()
    sa = b.rect_sketch(x0=0, y0=0, w=80, h=30, name="A")
    fa = boss(b, sa, {"depth": _dim(20), "merge": False}, "Plate", [R(sa, [40, 15], 2400.0)])
    sp = b.sketch("Peg", "plane.top")
    b.circle(sp, 15, 15, 5)
    fp = boss(b, sp, {"depth": _dim(20), "merge": False}, "Peg",
              [R(sp, [15, 15], math.pi * 25)])
    fpat = b.feature({"type": "linear_pattern", "name": "LP",
                      "params": {"count1": _dim(3), "spacing1": _dim(25)},
                      "refs": {"dir1": "axis.x", "features": [fp]}})
    b.feature({"type": "boolean", "name": "Bool", "params": {"op": "subtract"},
               "refs": {"targets": [{"kind": "body", "owner_feature": fa}],
                        "tools": [{"kind": "body", "owner_feature": fpat}]}})
    audit(core, td, "boolean subtract with patterned tool", b.ops)

    # 4c. boolean COMBINE with a patterned tool (overlapping union).
    b = Batch()
    sa = b.rect_sketch(x0=0, y0=0, w=80, h=20, name="A")
    fa = boss(b, sa, {"depth": _dim(10), "merge": False}, "Bar", [R(sa, [40, 10], 1600.0)])
    sp = b.rect_sketch(x0=10, y0=5, w=10, h=10, name="P")
    fp = boss(b, sp, {"depth": _dim(20), "merge": False}, "Post", [R(sp, [15, 10], 100.0)])
    fpat = b.feature({"type": "linear_pattern", "name": "LP",
                      "params": {"count1": _dim(3), "spacing1": _dim(20)},
                      "refs": {"dir1": "axis.x", "features": [fp]}})
    b.feature({"type": "boolean", "name": "Bool", "params": {"op": "combine"},
               "refs": {"targets": [{"kind": "body", "owner_feature": fa}],
                        "tools": [{"kind": "body", "owner_feature": fpat}]}})
    audit(core, td, "boolean combine with patterned tool", b.ops)

    # ===================================================================== GROUP 5
    # Deep dependency chains (8+ features) + EDIT-an-early-feature & recompile.
    print("--- GROUP 5: deep chains + edit-early-feature recompile ---")

    def deep_chain(b, base_depth=20):
        # An 8-feature dependency chain: f1 sketch, f2 boss base, f3 fillet, f4 chamfer,
        # f5 draft, f6 cut sketch, f7 cut, f8 linear-pattern-of-cut. The DRESSING sits on
        # the clean base BEFORE the cut/pattern (the cut is last -> a fresh subtractive
        # tail), so the chain composes cleanly on both engines.
        s = b.rect_sketch(w=80, h=40)
        boss(b, s, {"depth": _dim(base_depth)}, "Base", [R(s, [40, 20], 3200.0)])
        b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(3)},
                   "refs": {"edges": [VEDGE]}})
        b.feature({"type": "chamfer", "name": "Ch", "params": {"length": _dim(3)},
                   "refs": {"edges": [VEDGE_X80]}})
        b.feature({"type": "draft", "name": "Dr", "params": {"angle": _dim(6)},
                   "refs": {"neutral": "plane.top", "faces": [XFACE_80]}})
        cut = cut_circle(b, "C", 15, 20, 4)
        b.feature({"type": "linear_pattern", "name": "LP",
                   "params": {"count1": _dim(3), "spacing1": _dim(20)},
                   "refs": {"dir1": "axis.x", "features": [cut]}})
        return cut

    # 5a. the full 8-feature chain, no edit (build it).
    b = Batch()
    deep_chain(b)
    audit(core, td, "deep chain (8 features)", b.ops)

    # 5b. same chain, then EDIT the base boss depth (f2: 20 -> 32) and recompile.
    # The edit ripples through the fillet/chamfer/draft/cut/pattern; both engines replay
    # the whole batch incl. the edit. (Feature ids are minted f1,f2,...; base boss = f2.)
    b = Batch()
    deep_chain(b)
    b.ops.append({"op": "edit_feature", "id": "f2", "params": {"depth": _dim(32)}})
    audit(core, td, "deep chain + edit base depth recompile", b.ops)

    # 5c. edit the CUT radius mid-chain (f3 sketch is the cut profile; the cut is f4 here
    # because cut_circle adds a sketch then an extrude). Edit the cut extrude's end from
    # through_all to blind/depth and recompile.
    b = Batch()
    s = b.rect_sketch(w=80, h=40)
    boss(b, s, {"depth": _dim(20)}, "Base", [R(s, [40, 20], 3200.0)])
    cut = cut_circle(b, "C", 15, 20, 4)         # f3 sketch, f4 extrude-cut
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(3), "spacing1": _dim(20)},
               "refs": {"dir1": "axis.x", "features": [cut]}})
    b.ops.append({"op": "edit_feature", "id": "f4",
                  "params": {"end": "blind", "depth": _dim(8)}})
    audit(core, td, "edit cut depth then pattern recompile", b.ops)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()
    dll = Path(args.dll)
    if not dll.exists():
        print(f"[audit3] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[audit3] libaicad ABI={core.abi()}  dll={dll}")
    print(f"[audit3] vol_tol(rel)={A.VOL_TOL}  bbox_tol(abs)={A.BBOX_TOL}\n")

    td = tempfile.mkdtemp(prefix="aicad_audit3_")
    run_cases(core, td)

    print("\n" + "=" * 78)
    print("AUDIT3 SUMMARY (feature interactions + ordering)")
    print("=" * 78)
    print(f"  MATCHED:     {len(A.MATCHED)}")
    print(f"  DIVERGENCES: {len(A.DIVERGENCES)}")
    print(f"  GAPS:        {len(A.GAPS)}")
    if A.DIVERGENCES:
        print("\n  --- DIVERGENCES (bugs: wrong geometry / status) ---")
        for name, detail in A.DIVERGENCES:
            print(f"    [{name}]\n        {detail}")
    if A.GAPS:
        print("\n  --- GAPS (unsupported in C++) ---")
        for name, detail in A.GAPS:
            print(f"    [{name}] {detail}")
    print()
    return len(A.DIVERGENCES) + len(A.GAPS)


if __name__ == "__main__":
    raise SystemExit(main())

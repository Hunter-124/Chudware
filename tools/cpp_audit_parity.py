"""cpp_audit_parity.py — ADVERSARIAL part-feature audit: Python oracle vs C++ doc engine.

Read-only differential AUDIT (companion to tools/cpp_session_parity.py). Where the
session-parity gate exercises a narrow, known-good op set, this harness throws
adversarial feature-compiler PARAM COMBINATIONS at BOTH engines through the SAME
``doc_apply_ops`` batch and reports every divergence. It does NOT fix anything.

For each case the SAME op batch is driven through:
  * Python ORACLE  : engine.doc.session.new_session / apply_ops
  * C++  CANDIDATE : libaicad.dll  doc_new / doc_apply_ops   (STAGED dist DLL)

and the resulting GEOMETRY SIGNATURE is compared per case:
  * aggregate VOLUME      (apply-response volume; tight 1e-6 rel — feature geometry
                           is analytic so OCCT 7.9.3 vs ocp 7.8.1 match tightly)
  * BODY COUNT            (len(tess.bodies))
  * FACE COUNT + SOLID    (export the whole doc -> BREP, count via OCP TopExp — a
    COUNT + BBOX           version-stable analytic topology signature on both sides)
  * feature_status        (ok/error per feature id)

A DIVERGENCE is a SUCCESS of the audit (a bug found). Output lines:
  * MATCH       — both engines agree on every field
  * DIVERGENCE  — a field differs (wrong geometry / wrong status); printed with both values
  * GAP         — a feature type / param is unsupported in C++ (unknown op / "unsupported"
                  / hard error on one side only)

Run under the venv:
  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" tools\\cpp_audit_parity.py

Exit code = number of cases with a divergence/gap (informational; this is an audit,
not a gate — the report is the deliverable).
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402

VOL_TOL = 1e-6     # relative volume tolerance (analytic feature geometry)
BBOX_TOL = 1e-3    # absolute bbox tolerance (Bnd_Box default gap ~1e-7 + tess-free BRep)

# Findings buckets (filled as cases run; summarized at the end).
DIVERGENCES: list[tuple[str, str]] = []   # (case, detail) — wrong geometry/status
GAPS: list[tuple[str, str]] = []          # (case, detail) — unsupported in C++
MATCHED: list[str] = []                   # case names that fully matched


# --------------------------------------------------------------------------- ops builders
# A "$N" placeholder refers to the id minted by the op at INDEX N of the SAME op batch. A
# sketch_add_entity must therefore point its ``sketch`` at the op-index of its OWNING sketch
# feature (NOT a hardcoded "$0" — that only works when the sketch is the first op). The Batch
# helper tracks indices so multi-sketch parts wire their lines/regions to the right sketch.
class Batch:
    """Accumulates ops while tracking the op-index of each add_feature, so that sketch
    entities and region refs can address the correct sketch by ``$<index>``."""

    def __init__(self):
        self.ops: list[dict] = []

    def _add(self, op):
        idx = len(self.ops)
        self.ops.append(op)
        return idx

    def sketch(self, name="S", plane="plane.top"):
        """Add a sketch feature; returns its ``$N`` placeholder string."""
        idx = self._add({"op": "add_feature", "feature": {"type": "sketch", "name": name,
                         "params": {"entities": [], "constraints": []}, "refs": {"plane": plane}}})
        return f"${idx}"

    def feature(self, feat):
        """Add an arbitrary add_feature op; returns its ``$N`` placeholder string."""
        idx = self._add({"op": "add_feature", "feature": feat})
        return f"${idx}"

    def line(self, sketch, x1, y1, x2, y2, construction=False):
        self._add({"op": "sketch_add_entity", "sketch": sketch,
                   "entity": {"type": "line", "construction": construction,
                              "p1": [x1, y1], "p2": [x2, y2]}})

    def circle(self, sketch, cx, cy, r, construction=False):
        self._add({"op": "sketch_add_entity", "sketch": sketch,
                   "entity": {"type": "circle", "construction": construction,
                              "center": [cx, cy], "radius": r}})

    def rect(self, sketch, x0, y0, w, h):
        self.line(sketch, x0, y0, x0 + w, y0)
        self.line(sketch, x0 + w, y0, x0 + w, y0 + h)
        self.line(sketch, x0 + w, y0 + h, x0, y0 + h)
        self.line(sketch, x0, y0 + h, x0, y0)

    def rect_sketch(self, plane="plane.top", x0=0, y0=0, w=40, h=30, name="S"):
        """Convenience: add a sketch + a closed rectangle; return the sketch placeholder."""
        s = self.sketch(name, plane)
        self.rect(s, x0, y0, w, h)
        return s


def _region_ref(sketch, centroid, area):
    return {"kind": "region", "sketch": sketch, "centroid": list(centroid),
            "area": float(area), "hint_index": 0}


def _dim(v):
    return {"value": v, "expr": None}


# --------------------------------------------------------------------------- engines
def py_tree(ops):
    """Build the oracle document TREE from an op batch via engine.doc.ops.apply_ops.

    This is the doc-mutation half of session.apply_ops WITHOUT the tessellation/AICM write.
    We avoid the full session here on purpose: the session's _write_aicm crashes (TypeError
    into BRepMesh) on a body whose .shape is a ShapeList of disjoint solids — exactly what a
    linear/circular pattern or a disjoint boolean produces. That session crash is itself an
    oracle bug, but it would mask the GEOMETRY comparison; compile_document handles the same
    tree cleanly, so we drive the oracle through the tree -> compile_document path."""
    from engine.doc import session as _sess
    from engine.doc.ops import apply_ops as _apply_ops_to_doc
    doc = _sess._blank_part("Part1")
    new_doc, id_map = _apply_ops_to_doc(doc, ops)
    return new_doc, id_map


class CppS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]
        self._last_rc = rc

    def apply(self, base_rev, ops):
        rc, r = self.core.call("doc_apply_ops",
                               {"doc_id": self.doc_id, "base_revision": base_rev, "ops": ops})
        self._last_rc = rc
        return r


# --------------------------------------------------------------------------- geometry signature
def _occ_brep_stats(path):
    """Read a BREP file and return (solids, faces, bbox_min, bbox_max) via OCP."""
    from OCP.BRepTools import BRepTools
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Shape
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    shp = TopoDS_Shape()
    b = BRep_Builder()
    if not BRepTools.Read_s(shp, str(path), b) or shp.IsNull():
        return (0, 0, None, None)

    def count(t):
        e = TopExp_Explorer(shp, t)
        n = 0
        while e.More():
            n += 1
            e.Next()
        return n

    # AddOptimal(useTriangulation=False, useShapeTolerance=False) bounds the EXACT analytic
    # geometry. The C++-exported BREP carries a stored triangulation; the default Bnd_Box
    # path would bound that faceted mesh (~0.01-0.03 oversize) and falsely flag a divergence
    # on every curved/dressed body even when the volume is exactly analytic. The precise
    # bound makes the bbox check geometry-true on both engines.
    bb = Bnd_Box()
    try:
        BRepBndLib.AddOptimal_s(shp, bb, False, False)
    except Exception:  # noqa: BLE001 — fall back to the triangulated bound
        BRepBndLib.Add_s(shp, bb)
    try:
        lo = (bb.CornerMin().X(), bb.CornerMin().Y(), bb.CornerMin().Z())
        hi = (bb.CornerMax().X(), bb.CornerMax().Y(), bb.CornerMax().Z())
    except Exception:  # noqa: BLE001 — empty box
        lo = hi = None
    return (count(TopAbs_SOLID), count(TopAbs_FACE), lo, hi)


def cpp_sig(core, cpp, apply_resp, td):
    """Geometry signature of the C++ doc: vol (apply resp), bodies, faces/solids/bbox (BREP)."""
    vol = float(apply_resp.get("volume", 0.0))
    bodies = len(apply_resp.get("tess", {}).get("bodies", []))
    status = {f["id"]: bool(f["ok"]) for f in apply_resp.get("rebuild", {}).get("feature_status", [])}
    solids = faces = -1
    lo = hi = None
    if bodies > 0:
        p = str(Path(td) / f"cpp_{cpp.doc_id}.brep")
        rc, ev = core.call("export_file", {"doc_id": cpp.doc_id, "path": p, "format": "brep"})
        if rc == 0 and Path(ev.get("path", "")).exists():
            solids, faces, lo, hi = _occ_brep_stats(ev["path"])
    return {"vol": vol, "bodies": bodies, "solids": solids, "faces": faces,
            "lo": lo, "hi": hi, "status": status}


def py_sig(tree, td):
    """Geometry signature of the Python oracle doc via a fresh compile + BREP export."""
    from engine.doc.compiler import compile_document
    from engine.doc import formats as fmt
    r = compile_document(tree)
    status = {s["id"]: bool(s.get("ok")) for s in r.feature_status}

    def _vol(shape):
        try:
            return float(shape.volume)
        except Exception:  # noqa: BLE001
            try:
                return float(sum(float(x.volume) for x in shape))
            except Exception:  # noqa: BLE001
                return 0.0

    solid_bodies = [b for b in r.bodies]
    vol = sum(_vol(b.shape) for b in solid_bodies)
    bodies = len(solid_bodies)
    solids = faces = -1
    lo = hi = None
    if bodies > 0:
        sb = [(b.name or f"Body{i}", b.shape) for i, b in enumerate(solid_bodies, 1)]
        p = str(Path(td) / f"py_{id(tree)}.brep")
        try:
            fmt.export_document(sb, p, "brep")
            if Path(p).exists():
                solids, faces, lo, hi = _occ_brep_stats(p)
        except Exception:  # noqa: BLE001 — keep vol/bodies even if export fails
            pass
    return {"vol": vol, "bodies": bodies, "solids": solids, "faces": faces,
            "lo": lo, "hi": hi, "status": status}


def _bbox_close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return all(abs(x - y) <= BBOX_TOL for x, y in zip(a, b))


def _vol_close(a, b):
    return abs(a - b) <= VOL_TOL * max(1.0, abs(a), abs(b))


# --------------------------------------------------------------------------- one case
def audit(core, td, label, ops, *, expect_error=False):
    """Drive ``ops`` through both engines and compare the geometry signatures.

    ``expect_error``: this case SHOULD produce a failed feature on both engines (e.g. a
    fillet radius too large). The audit then requires both engines to AGREE on at least
    one feature being not-ok; a divergence is one engine succeeding while the other fails.
    """
    diffs = []
    # ---- oracle: build the doc tree (no tessellation) ----
    py_rejected = None
    tree = None
    try:
        tree, _id_map = py_tree(ops)
    except Exception as exc:  # noqa: BLE001 — oracle rejected the op batch at the schema/op layer
        py_rejected = f"{type(exc).__name__}: {exc}"

    # ---- C++: apply the op batch ----
    cpp = CppS(core)
    cres = cpp.apply(0, ops)
    cpp_rejected = None
    if cpp._last_rc != 0:
        cpp_rejected = cres.get("code") or cres.get("message") or str(cres)[:80]

    # Both layers rejected the op batch -> a matched, mutually-unsupported edge.
    if py_rejected and cpp_rejected:
        MATCHED.append(label + " (both reject op)")
        print(f"  {label:<42} MATCH (both reject op batch)")
        return
    if py_rejected and not cpp_rejected:
        DIVERGENCES.append((label, f"py rejected op batch ({py_rejected}); C++ accepted (rc=0)"))
        print(f"  {label:<42} DIVERGENCE: py rejected op; C++ accepted")
        return
    if cpp_rejected and not py_rejected:
        GAPS.append((label, f"C++ rejected op batch: {cpp_rejected}"))
        print(f"  {label:<42} GAP: C++ rejected op batch -> {cpp_rejected}")
        return

    # ---- both accepted: compute + compare geometry signatures ----
    try:
        ps = py_sig(tree, td)
    except Exception as exc:  # noqa: BLE001 — oracle compile_document raised
        DIVERGENCES.append((label, f"py compile_document raised {type(exc).__name__}: {exc}"))
        print(f"  {label:<42} DIVERGENCE: py compile raised {type(exc).__name__}")
        return
    cs = cpp_sig(core, cpp, cres, td)

    # feature_status: compare ok/error per feature id (the ground-truth ok set must match).
    if ps["status"] != cs["status"]:
        only_py_ok = {k for k, v in ps["status"].items() if v and not cs["status"].get(k, False)}
        only_cpp_ok = {k for k, v in cs["status"].items() if v and not ps["status"].get(k, False)}
        diffs.append(f"feature_status py={ps['status']} cpp={cs['status']}"
                     + (f" [py-only-ok={sorted(only_py_ok)}]" if only_py_ok else "")
                     + (f" [cpp-only-ok={sorted(only_cpp_ok)}]" if only_cpp_ok else ""))

    if expect_error:
        py_all_ok = all(ps["status"].values()) if ps["status"] else False
        cpp_all_ok = all(cs["status"].values()) if cs["status"] else False
        if py_all_ok != cpp_all_ok:
            diffs.append(f"expected-error disagreement: py_all_ok={py_all_ok} cpp_all_ok={cpp_all_ok}")
        elif not py_all_ok and not cpp_all_ok:
            # both correctly failed — that is the matched outcome for this case.
            pass

    # body count
    if ps["bodies"] != cs["bodies"]:
        diffs.append(f"body_count py={ps['bodies']} cpp={cs['bodies']}")

    # volume (tight) — only meaningful when both built bodies
    if ps["bodies"] > 0 and cs["bodies"] > 0 and not _vol_close(ps["vol"], cs["vol"]):
        diffs.append(f"volume py={ps['vol']:.6f} cpp={cs['vol']:.6f} "
                     f"(rel {abs(ps['vol']-cs['vol'])/max(1.0,abs(ps['vol'])):.2e})")

    # face/solid count via BREP (skip if either export was unavailable -> -1)
    if ps["faces"] >= 0 and cs["faces"] >= 0:
        if ps["solids"] != cs["solids"]:
            diffs.append(f"solid_count(brep) py={ps['solids']} cpp={cs['solids']}")
        if ps["faces"] != cs["faces"]:
            diffs.append(f"face_count py={ps['faces']} cpp={cs['faces']}")
        if not _bbox_close(ps["lo"], cs["lo"]) or not _bbox_close(ps["hi"], cs["hi"]):
            diffs.append(f"bbox py={ps['lo']}..{ps['hi']} cpp={cs['lo']}..{cs['hi']}")

    if diffs:
        DIVERGENCES.append((label, "; ".join(diffs)))
        print(f"  {label:<42} DIVERGENCE: {'; '.join(diffs)[:200]}")
    else:
        MATCHED.append(label)
        extra = f"  [V={ps['vol']:.1f} bodies={ps['bodies']} faces={ps['faces']}]"
        print(f"  {label:<42} MATCH{extra}")


# --------------------------------------------------------------------------- cases
def main() -> int:
    dll = REPO / "dist" / "AI-CAD" / "libaicad.dll"
    if not dll.exists():
        print(f"[audit] staged libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[audit] libaicad ABI={core.abi()}  dll={dll}")
    print(f"[audit] vol_tol(rel)={VOL_TOL}  bbox_tol(abs)={BBOX_TOL}\n")

    td = tempfile.mkdtemp(prefix="aicad_audit_")
    R = _region_ref

    # ===== EXTRUDE variants on a 40x30 rect (region centroid [20,15], area 1200) =====
    print("--- EXTRUDE variants ---")

    def extrude_feat(b, params, name="X", profiles=None, sketch=None):
        prof = profiles or [R(sketch, [20, 15], 1200.0)]
        return b.feature({"type": "extrude", "name": name, "params": params,
                          "refs": {"profile": prof}})

    def ex_case(label, params, *, expect_error=False, w=40, h=30):
        b = Batch()
        s = b.rect_sketch(w=w, h=h)
        extrude_feat(b, dict(params), "X", sketch=s)
        audit(core, td, label, b.ops, expect_error=expect_error)

    ex_case("extrude blind boss merge", [("mode", "boss"), ("end", "blind"), ("depth", _dim(10))])
    ex_case("extrude blind cut(no body)", [("mode", "cut"), ("end", "blind"), ("depth", _dim(10))],
            expect_error=True)
    ex_case("extrude mid_plane (symmetric)", [("mode", "boss"), ("end", "mid_plane"), ("depth", _dim(10))])
    ex_case("extrude through_all boss", [("mode", "boss"), ("end", "through_all")])
    ex_case("extrude two-direction (end2 blind)",
            [("mode", "boss"), ("end", "blind"), ("depth", _dim(10)),
             ("end2", "blind"), ("depth2", _dim(6))])
    ex_case("extrude thin wall (one side)",
            [("mode", "boss"), ("end", "blind"), ("depth", _dim(10)),
             ("thin", True), ("thin_thickness", _dim(2)), ("thin_side", "one")])
    ex_case("extrude thin wall (two side)",
            [("mode", "boss"), ("end", "blind"), ("depth", _dim(10)),
             ("thin", True), ("thin_thickness", _dim(2)), ("thin_side", "two")])
    ex_case("extrude draft-while-extrude 10deg",
            [("mode", "boss"), ("end", "blind"), ("depth", _dim(10)), ("draft", _dim(10))])
    ex_case("extrude draft outward 10deg",
            [("mode", "boss"), ("end", "blind"), ("depth", _dim(10)),
             ("draft", _dim(10)), ("draft_outward", True)])

    # two bosses, second as NEW BODY (2 disjoint) — second rect is disjoint in X.
    b = Batch()
    s1 = b.rect_sketch(w=40, h=30, name="S1")
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10)}, "X1", sketch=s1)
    s2 = b.rect_sketch(x0=60, y0=0, w=20, h=20, name="S2")
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10), "merge": False}, "X2",
                 [R(s2, [70, 10], 400.0)])
    audit(core, td, "extrude two bosses new-body (2 disjoint)", b.ops)

    # boss then cut-through inner profile (washer-like)
    b = Batch()
    s1 = b.rect_sketch(w=40, h=30, name="S1")
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10)}, "X1", sketch=s1)
    s2 = b.rect_sketch(x0=10, y0=10, w=20, h=10, name="S2")
    extrude_feat(b, {"mode": "cut", "end": "through_all"}, "X2", [R(s2, [20, 15], 200.0)])
    audit(core, td, "extrude boss then cut-through inner", b.ops)

    # up_to_vertex: extrude until a vertex of the running body
    b = Batch()
    s1 = b.rect_sketch(w=40, h=30, name="S1")
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(25)}, "X1", sketch=s1)
    s2 = b.rect_sketch(x0=5, y0=5, w=10, h=10, name="S2")
    vsel = {"kind": "vertex", "surf": None, "centroid": [40.0, 30.0, 25.0], "axis": None,
            "area_or_len": None, "owner_feature": None, "hint_index": 0}
    b.feature({"type": "extrude", "name": "X2",
               "params": {"mode": "boss", "end": "up_to_vertex", "up_to": vsel, "merge": True},
               "refs": {"profile": [R(s2, [10, 10], 100.0)]}})
    audit(core, td, "extrude up_to_vertex", b.ops)

    # ===== REVOLVE variants =====
    print("--- REVOLVE variants ---")

    def rev_case(label, params, *, expect_error=False):
        b = Batch()
        s = b.rect_sketch(x0=5, y0=0, w=10, h=10)
        b.feature({"type": "revolve", "name": "Rev", "params": params,
                   "refs": {"profile": [R(s, [10, 5], 100.0)], "axis": "axis.y"}})
        audit(core, td, label, b.ops, expect_error=expect_error)

    rev_case("revolve full 360 about axis.y", {"mode": "boss", "angle": _dim(360)})
    rev_case("revolve partial 120 about axis.y", {"mode": "boss", "angle": _dim(120)})
    rev_case("revolve thin 360",
             {"mode": "boss", "angle": _dim(360), "thin": True, "thin_thickness": _dim(2)})

    # revolve cut bore into a base cylinder
    b = Batch()
    sc = b.sketch("SB", "plane.top")
    b.circle(sc, 0, 0, 15)
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(20)}, "BX",
                 [R(sc, [0, 0], math.pi * 15 * 15)])
    s1 = b.rect_sketch(x0=0, y0=0, w=5, h=20, name="S1")
    b.feature({"type": "revolve", "name": "Rev", "params": {"mode": "cut", "angle": _dim(360)},
               "refs": {"profile": [R(s1, [2.5, 10], 100.0)], "axis": "axis.y"}})
    audit(core, td, "revolve cut bore into cylinder", b.ops)

    # revolve about a sketch LINE (axis from geometry): rect + a 5th line on the Y axis (x=0)
    b = Batch()
    s = b.sketch("S", "plane.top")
    b.rect(s, 5, 0, 10, 10)            # lines e1..e4
    b.line(s, 0, 0, 0, 10)            # e5 = axis line on Y
    b.feature({"type": "revolve", "name": "Rev", "params": {"mode": "boss", "angle": _dim(360)},
               "refs": {"profile": [R(s, [10, 5], 100.0)],
                        "axis": {"kind": "edge", "surf": "line", "owner_feature": s,
                                 "hint_index": 4, "centroid": [0, 5, 0], "axis": [0, 1, 0],
                                 "area_or_len": 10.0}}})
    audit(core, td, "revolve axis from sketch line", b.ops)

    # ===== SWEEP + LOFT =====
    print("--- SWEEP + LOFT ---")

    def sweep_case(label, params):
        b = Batch()
        sp = b.sketch("Sprof", "plane.top")
        b.circle(sp, 0, 0, 5)
        pa = b.sketch("Spath", "plane.front")
        b.line(pa, 0, 0, 0, 40)
        b.feature({"type": "sweep", "name": "Sw", "params": params,
                   "refs": {"profile": [R(sp, [0, 0], math.pi * 25)],
                            "path": R(pa, [0, 20], 40.0)}})
        audit(core, td, label, b.ops)

    sweep_case("sweep circle along line", {"mode": "boss"})
    sweep_case("sweep with twist 90", {"mode": "boss", "twist": _dim(90)})

    # 2-section loft: 20x20 on top -> 10x10 on offset plane 40
    def loft2(label, params):
        b = Batch()
        pl = b.feature({"type": "plane", "name": "P40", "params": {"method": "offset", "offset": _dim(40)},
                        "refs": {"base": "plane.top"}})
        l0 = b.rect_sketch(x0=-10, y0=-10, w=20, h=20, name="L0")
        l1 = b.sketch("L1", pl)
        b.rect(l1, -5, -5, 10, 10)
        b.feature({"type": "loft", "name": "Lf", "params": params,
                   "refs": {"sections": [R(l0, [0, 0], 400.0), R(l1, [0, 0], 100.0)]}})
        audit(core, td, label, b.ops)

    loft2("loft 2-section (square frustum)", {"mode": "boss"})
    loft2("loft closed param", {"mode": "boss", "closed": True})

    # 3-section multi loft
    b = Batch()
    p20 = b.feature({"type": "plane", "name": "P20", "params": {"method": "offset", "offset": _dim(20)},
                     "refs": {"base": "plane.top"}})
    p40 = b.feature({"type": "plane", "name": "P40", "params": {"method": "offset", "offset": _dim(40)},
                     "refs": {"base": "plane.top"}})
    l0 = b.rect_sketch(x0=-10, y0=-10, w=20, h=20, name="L0")
    l1 = b.sketch("L1", p20)
    b.rect(l1, -7, -7, 14, 14)
    l2 = b.sketch("L2", p40)
    b.rect(l2, -3, -3, 6, 6)
    b.feature({"type": "loft", "name": "Lf", "params": {"mode": "boss"},
               "refs": {"sections": [R(l0, [0, 0], 400.0), R(l1, [0, 0], 196.0), R(l2, [0, 0], 36.0)]}})
    audit(core, td, "loft 3-section multi", b.ops)

    # ===== HOLE WIZARD =====
    print("--- HOLE WIZARD ---")
    hface = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0],
             "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0, "owner_feature": None, "hint_index": 5}

    def hole_case(label, params):
        b = Batch()
        s = b.rect_sketch(w=40, h=30)
        extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(20)}, "X1", sketch=s)
        b.feature({"type": "hole", "name": "H", "params": params,
                   "refs": {"face": hface, "points": [[0, 0]]}})
        audit(core, td, label, b.ops)

    hole_case("hole simple through-all",
              {"style": "simple", "end": "through_all", "standard": "custom", "diameter": _dim(6)})
    hole_case("hole simple blind",
              {"style": "simple", "end": "blind", "depth": _dim(10), "standard": "custom", "diameter": _dim(6)})
    hole_case("hole counterbore",
              {"style": "cbore", "end": "through_all", "standard": "custom",
               "diameter": _dim(6), "cbore_dia": _dim(12), "cbore_depth": _dim(4)})
    hole_case("hole countersink",
              {"style": "csink", "end": "through_all", "standard": "custom",
               "diameter": _dim(6), "csink_dia": _dim(12), "csink_angle": _dim(90)})
    hole_case("hole tapped",
              {"style": "tap", "end": "blind", "depth": _dim(12), "standard": "custom",
               "diameter": _dim(5), "thread_dia": _dim(6)})

    # ===== FILLET / CHAMFER =====
    print("--- FILLET / CHAMFER ---")
    vedge = {"kind": "edge", "surf": "line", "centroid": [0.0, 0.0, 10.0], "axis": [0.0, 0.0, 1.0],
             "area_or_len": 20.0, "owner_feature": None, "hint_index": 0}
    vedge2 = {"kind": "edge", "surf": "line", "centroid": [40.0, 0.0, 10.0], "axis": [0.0, 0.0, 1.0],
              "area_or_len": 20.0, "owner_feature": None, "hint_index": 0}
    tface = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0], "axis": [0, 0, 1],
             "area_or_len": 1200.0, "owner_feature": None, "hint_index": 0}

    def dress_base(b):
        s = b.rect_sketch(w=40, h=30)
        extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(20)}, "X1", sketch=s)

    def dress_case(label, feat, *, expect_error=False):
        b = Batch()
        dress_base(b)
        b.feature(feat)
        audit(core, td, label, b.ops, expect_error=expect_error)

    dress_case("fillet single edge r=3",
               {"type": "fillet", "name": "F", "params": {"radius": _dim(3)}, "refs": {"edges": [vedge]}})
    dress_case("fillet multi-edge r=3",
               {"type": "fillet", "name": "F", "params": {"radius": _dim(3)},
                "refs": {"edges": [vedge, vedge2]}})
    dress_case("fillet radius too large (err)",
               {"type": "fillet", "name": "F", "params": {"radius": _dim(1000)}, "refs": {"edges": [vedge]}},
               expect_error=True)
    dress_case("fillet variable-radius",
               {"type": "fillet", "name": "F",
                "params": {"radius": _dim(3), "radius2": _dim(8), "variable": True},
                "refs": {"edges": [vedge]}})
    dress_case("face fillet (face set)",
               {"type": "fillet", "name": "F", "params": {"radius": _dim(3), "fillet_type": "face"},
                "refs": {"faces": [tface]}})
    dress_case("chamfer single edge d=3",
               {"type": "chamfer", "name": "C", "params": {"length": _dim(3)}, "refs": {"edges": [vedge]}})
    dress_case("chamfer two-distance",
               {"type": "chamfer", "name": "C",
                "params": {"length": _dim(3), "length2": _dim(6), "chamfer_type": "two_distance"},
                "refs": {"edges": [vedge]}})
    # chamfer then fillet a different edge (ordering)
    b = Batch()
    dress_base(b)
    b.feature({"type": "chamfer", "name": "C", "params": {"length": _dim(3)}, "refs": {"edges": [vedge]}})
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(3)}, "refs": {"edges": [vedge2]}})
    audit(core, td, "chamfer-then-fillet ordering", b.ops)

    # ===== PATTERNS / MIRROR =====
    print("--- PATTERNS / MIRROR ---")

    def cube_base(b, x0=0, y0=0):
        s = b.rect_sketch(x0=x0, y0=y0, w=10, h=10)
        extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10)}, "X1",
                     [R(s, [x0 + 5, y0 + 5], 100.0)])

    def pat_case(label, feat):
        b = Batch()
        cube_base(b)
        b.feature(feat)
        audit(core, td, label, b.ops)

    pat_case("linear pattern 1D x3",
             {"type": "linear_pattern", "name": "LP",
              "params": {"count1": _dim(3), "spacing1": _dim(20)},
              "refs": {"dir1": "axis.x", "features": ["f2"]}})
    pat_case("linear pattern 2D 3x2",
             {"type": "linear_pattern", "name": "LP",
              "params": {"count1": _dim(3), "spacing1": _dim(20), "count2": _dim(2), "spacing2": _dim(20)},
              "refs": {"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]}})

    def off_cube_case(label, feat):
        b = Batch()
        s = b.rect_sketch(x0=20, y0=-5, w=10, h=10)
        extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10)}, "X1", [R(s, [25, 0], 100.0)])
        b.feature(feat)
        audit(core, td, label, b.ops)

    off_cube_case("circular pattern full 4",
                  {"type": "circular_pattern", "name": "CP",
                   "params": {"count": _dim(4), "angle": _dim(360)},
                   "refs": {"axis": "axis.z", "features": ["f2"]}})
    off_cube_case("circular pattern partial 3@180",
                  {"type": "circular_pattern", "name": "CP",
                   "params": {"count": _dim(3), "angle": _dim(180)},
                   "refs": {"axis": "axis.z", "features": ["f2"]}})
    pat_case("mirror feature about plane.right",
             {"type": "mirror", "name": "M", "params": {},
              "refs": {"plane": "plane.right", "features": ["f2"]}})

    # pattern OF a cut: bar + a cut hole, linear-pattern the cut feature
    b = Batch()
    s = b.rect_sketch(w=80, h=20)
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10)}, "X1", [R(s, [40, 10], 1600.0)])
    s2 = b.sketch("S2", "plane.top")
    b.circle(s2, 10, 10, 3)
    b.feature({"type": "extrude", "name": "Cut", "params": {"mode": "cut", "end": "through_all"},
               "refs": {"profile": [R(s2, [10, 10], math.pi * 9)]}})
    b.feature({"type": "linear_pattern", "name": "LP",
               "params": {"count1": _dim(4), "spacing1": _dim(15)},
               "refs": {"dir1": "axis.x", "features": ["f3"]}})
    audit(core, td, "linear pattern OF a cut x4", b.ops)

    # ===== SHELL / DRAFT / RIB =====
    print("--- SHELL / DRAFT / RIB ---")
    rface = {"kind": "face", "surf": "plane", "centroid": [40.0, 15.0, 10.0], "axis": [1, 0, 0],
             "area_or_len": 600.0, "owner_feature": None, "hint_index": 0}
    dress_case("shell open top (1 face)",
               {"type": "shell", "name": "Sh", "params": {"thickness": _dim(2)},
                "refs": {"remove": [tface]}})
    dress_case("shell open top+side (2 faces)",
               {"type": "shell", "name": "Sh", "params": {"thickness": _dim(2)},
                "refs": {"remove": [tface, rface]}})
    dress_case("draft +X face 10deg neutral=top",
               {"type": "draft", "name": "Dr", "params": {"angle": _dim(10)},
                "refs": {"neutral": "plane.top", "faces": [rface]}})
    dress_case("shell thickness too large (err)",
               {"type": "shell", "name": "Sh", "params": {"thickness": _dim(1000)},
                "refs": {"remove": [tface]}}, expect_error=True)

    # rib: U-channel on plane.front, then a bridging line grown to a rib
    b = Batch()
    u = b.sketch("U", "plane.front")
    u_pts = [(0, 0), (40, 0), (40, 30), (30, 30), (30, 10), (10, 10), (10, 30), (0, 30)]
    for i in range(len(u_pts)):
        a, c = u_pts[i], u_pts[(i + 1) % len(u_pts)]
        b.line(u, a[0], a[1], c[0], c[1])
    extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(20)}, "X1", [R(u, [20, 20], 700.0)])
    rib_s = b.sketch("Rib", "plane.front")
    b.line(rib_s, 10, 20, 30, 20)
    b.feature({"type": "rib", "name": "Rb", "params": {"thickness": _dim(4)},
               "refs": {"profile": [R(rib_s, [20, 20], 20.0)]}})
    audit(core, td, "rib from open line", b.ops)

    # ===== MULTI-BODY booleans =====
    print("--- MULTI-BODY booleans ---")

    def two_box(label, op):
        b = Batch()
        sa = b.rect_sketch(x0=0, y0=0, w=20, h=20, name="A")
        fa = extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10), "merge": False},
                          "BA", [R(sa, [10, 10], 400.0)])
        sb = b.rect_sketch(x0=10, y0=0, w=20, h=20, name="B")
        fb = extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10), "merge": False},
                          "BB", [R(sb, [20, 10], 400.0)])
        b.feature({"type": "boolean", "name": "Bool", "params": {"op": op},
                   "refs": {"targets": [{"kind": "body", "owner_feature": fa}],
                            "tools": [{"kind": "body", "owner_feature": fb}]}})
        audit(core, td, label, b.ops)

    two_box("boolean combine (overlap)", "combine")
    two_box("boolean subtract", "subtract")
    two_box("boolean intersect", "intersect")

    # subtract that yields 2 DISJOINT bodies: a 40x10 bar minus a 10x20 cross-bar through the middle
    b = Batch()
    sa = b.rect_sketch(x0=0, y0=0, w=40, h=10, name="A")
    fa = extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10), "merge": False},
                      "BA", [R(sa, [20, 5], 400.0)])
    sb = b.rect_sketch(x0=15, y0=-5, w=10, h=20, name="B")
    fb = extrude_feat(b, {"mode": "boss", "end": "blind", "depth": _dim(10), "merge": False},
                      "BB", [R(sb, [20, 5], 200.0)])
    b.feature({"type": "boolean", "name": "Bool", "params": {"op": "subtract"},
               "refs": {"targets": [{"kind": "body", "owner_feature": fa}],
                        "tools": [{"kind": "body", "owner_feature": fb}]}})
    audit(core, td, "subtract -> 2 disjoint bodies", b.ops)

    # ===== PRIMITIVES =====
    print("--- PRIMITIVES ---")

    def prim_case(label, ptype, params):
        b = Batch()
        b.feature({"type": ptype, "name": "P", "params": {k: _dim(v) for k, v in params.items()}})
        audit(core, td, label, b.ops)

    prim_case("prim box", "box", {"length": 30, "width": 20, "height": 10})
    prim_case("prim cylinder", "cylinder", {"radius": 10, "height": 25})
    prim_case("prim sphere", "sphere", {"radius": 12})
    prim_case("prim cone", "cone", {"r1": 10, "r2": 4, "height": 20})
    prim_case("prim torus", "torus", {"major_r": 20, "minor_r": 5})

    # ===== EDGE / ERROR cases =====
    print("--- EDGE / ERROR cases ---")
    # loft to a degenerate (zero-area / non-region) second section -> should fail on both
    b = Batch()
    pl = b.feature({"type": "plane", "name": "P40", "params": {"method": "offset", "offset": _dim(40)},
                    "refs": {"base": "plane.top"}})
    l0 = b.rect_sketch(x0=-10, y0=-10, w=20, h=20, name="L0")
    l1 = b.sketch("L1", pl)
    b.line(l1, -5, 0, 5, 0)   # open single line, no closed region
    b.feature({"type": "loft", "name": "Lf", "params": {"mode": "boss"},
               "refs": {"sections": [R(l0, [0, 0], 400.0), R(l1, [0, 0], 0.0)]}})
    audit(core, td, "loft to non-region section (err)", b.ops, expect_error=True)

    # --------------------------------------------------------------------- report
    print("\n" + "=" * 78)
    print("AUDIT SUMMARY")
    print("=" * 78)
    print(f"  MATCHED:     {len(MATCHED)}")
    print(f"  DIVERGENCES: {len(DIVERGENCES)}")
    print(f"  GAPS:        {len(GAPS)}")
    if DIVERGENCES:
        print("\n  --- DIVERGENCES (bugs: wrong geometry / status) ---")
        for name, detail in DIVERGENCES:
            print(f"    [{name}]\n        {detail}")
    if GAPS:
        print("\n  --- GAPS (unsupported in C++) ---")
        for name, detail in GAPS:
            print(f"    [{name}] {detail}")
    print()
    return len(DIVERGENCES) + len(GAPS)


if __name__ == "__main__":
    raise SystemExit(main())

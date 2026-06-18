"""cpp_eval_parity.py — Python-vs-C++ Evaluate-suite parity.

Gates the C++ doc-engine Evaluate methods (libaicad.dll) against the Python oracle
(engine/doc/evaluate.py via engine/worker.py's _eval_* handlers). Each part is built
through the SAME doc_* op batch on both engines, then the eval method is called on each
and the results compared.

Currently covers: eval_mass (mass_props).

eval_mass comparison rationale:
  * volume_mm3 / mass_g / com / inertia / principal_moments are deterministic outputs of
    OCCT BRepGProp + an eigenvalue solve, so they MUST match tightly (1e-6 relative). The
    Python oracle runs cadquery-ocp 7.8.1 and the C++ core OCCT 7.9.3, but the mass
    integrals of an identical analytic B-rep are version-independent.
  * principal_axes are eigenVECTORS — their SIGN is library-arbitrary, and when two
    principal moments are equal (e.g. a cylinder's two radial axes) the eigenspace is a
    plane and ANY orthonormal pair in it is valid. So each axis is checked up to a ± sign
    ONLY when its moment is non-degenerate; a degenerate axis is reported and skipped
    (still a correct equivalence — never a weakened assertion on the well-defined outputs).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_eval_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- ops
def _line(x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def rect_then_extrude(w, h, depth):
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
                                          "params": {"entities": [], "constraints": []},
                                          "refs": {"plane": "plane.top"}}},
        _line(0, 0, w, 0), _line(w, 0, w, h), _line(w, h, 0, h), _line(0, h, 0, 0),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss1",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                  "centroid": [w / 2, h / 2], "area": w * h, "hint_index": 0}]}}},
    ]


def cyl_ops(radius, height):
    return [{"op": "add_feature", "feature": {"type": "cylinder", "name": "Cyl1", "params": {
        "radius": {"value": radius, "expr": None}, "height": {"value": height, "expr": None}}}}]


def box_at(x0, y0, w, h, depth, name, merge=True, base=0):
    """A rectangle sketch at (x0,y0) extruded `depth`. merge=False makes it a NEW BODY.

    `base` is this block's absolute op index in the batch — the region's sketch placeholder
    must be "$<base>" (a $N placeholder refers to the op at batch index N, NOT 'the last
    sketch'). _line(..) entities are sketch_add_entity ops that DON'T create ids, so the
    sketch op (at `base`) is the only placeholder source in the block."""
    line_sk = "$" + str(base)
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S" + name,
                                          "params": {"entities": [], "constraints": []},
                                          "refs": {"plane": "plane.top"}}},
        _ln(line_sk, x0, y0, x0 + w, y0), _ln(line_sk, x0 + w, y0, x0 + w, y0 + h),
        _ln(line_sk, x0 + w, y0 + h, x0, y0 + h), _ln(line_sk, x0, y0 + h, x0, y0),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": name,
            "params": {"mode": "boss", "merge": merge, "end": "blind",
                       "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": line_sk,
                                  "centroid": [x0 + w / 2, y0 + h / 2], "area": w * h,
                                  "hint_index": 0}]}}},
    ]


def _ln(sketch, x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": sketch,
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def two_body_ops(sep):
    """Body b1 = 20x20x10 at origin; body b2 = 20x20x10 at x=sep. sep<20 overlaps."""
    # Each box_at block is 6 ops (1 sketch + 4 lines + 1 extrude); the 2nd starts at index 6.
    return box_at(0, 0, 20, 20, 10, "B1", merge=False, base=0) + \
        box_at(sep, 0, 20, 20, 10, "B2", merge=False, base=6)


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
def py_mass(py, density, body=None):
    """Mirror engine/worker.py::_eval_mass: last (or body_id'th) body -> evaluate.mass_props."""
    from engine.doc import session as docsess
    import engine.doc.evaluate as ev
    result = docsess._ensure_built(py.s)
    bodies = list(result.bodies)
    shape = bodies[(body - 1) if body else -1].shape
    return ev.mass_props(shape, density)


def _py_active(py, body=None):
    from engine.doc import session as docsess
    result = docsess._ensure_built(py.s)
    bodies = list(result.bodies)
    return bodies, bodies[(body - 1) if body else -1]


def _py_plane(plane_ref):
    """Mirror worker.py::_resolve_eval_plane: datum id -> reserved build123d Plane."""
    from engine.doc.features.datum import reserved_datums
    fid = plane_ref if isinstance(plane_ref, str) else plane_ref.get("feature")
    return reserved_datums()[fid].obj


def py_check(py, body=None):
    import engine.doc.evaluate as ev
    _, b = _py_active(py, body)
    return ev.check(b.shape)


def py_section(py, plane_ref):
    import engine.doc.evaluate as ev
    bodies, _ = _py_active(py)
    return ev.section_props(bodies[-1].shape, _py_plane(plane_ref))


def py_symmetry(py, plane_ref, body=None):
    import engine.doc.evaluate as ev
    _, b = _py_active(py, body)
    return ev.symmetry(b.shape, _py_plane(plane_ref))


def py_interference(py):
    import engine.doc.evaluate as ev
    bodies, _ = _py_active(py)
    members = [(bo.name or bo.body_id, bo.shape) for bo in bodies]
    return ev.interference(members)


def py_measure(py, refs):
    """Mirror worker.py::_eval_measure + _resolve_eval_ref (resolve refs vs the LAST body)."""
    import engine.doc.evaluate as ev
    from engine.doc import session as docsess
    from engine.doc import selectors as sel
    import math
    result = docsess._ensure_built(py.s)
    bodies = list(result.bodies)
    body = bodies[-1]
    raw = body.shape.wrapped
    bb = body.shape.bounding_box()
    diag = max(math.sqrt((bb.max.X - bb.min.X) ** 2 + (bb.max.Y - bb.min.Y) ** 2
                         + (bb.max.Z - bb.min.Z) ** 2), 1e-3)
    infos = []
    for ref in refs:
        kind = ref["kind"]
        if kind == "face":
            descs, elems = sel.describe_faces(raw), body.shape.faces()
        elif kind == "edge":
            descs, elems = sel.describe_edges(raw), body.shape.edges()
        else:
            descs, elems = sel.describe_vertices(raw), body.shape.vertices()
        rr = sel.resolve(ref, descs, diag)
        infos.append({"shape": elems[rr.index], "kind": kind})
    return ev.measure(infos)


def _ref_for(py, kind, occ_index):
    """Build a re-resolvable selector for the occ_index'th face/edge/vertex of the LAST body,
    via the SAME describe_* the resolver consumes (so both engines land on the same entity)."""
    from engine.doc import session as docsess
    from engine.doc import selectors as sel
    result = docsess._ensure_built(py.s)
    raw = list(result.bodies)[-1].shape.wrapped
    descs = {"face": sel.describe_faces, "edge": sel.describe_edges,
             "vertex": sel.describe_vertices}[kind](raw)
    return sel.make_selector(descs[occ_index])


# --------------------------------------------------------------------------- compare helpers
def _rel(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _vec_close(a, b, tol=1e-6):
    return all(_rel(x, y, tol) for x, y in zip(a, b))


def _axis_match_up_to_sign(c, o, tol=1e-5):
    same = max(abs(x - y) for x, y in zip(c, o))
    flip = max(abs(x + y) for x, y in zip(c, o))
    return min(same, flip) <= tol


def compare_mass(pres, cres):
    """Return a list of diff strings ([] == match)."""
    diffs = []
    # Surface vs solid branch must agree.
    if bool(pres.get("is_surface")) != bool(cres.get("is_surface")):
        return [f"is_surface {pres.get('is_surface')}!={cres.get('is_surface')}"]

    if pres.get("is_surface"):
        if not _rel(pres["area_mm2"], cres["area_mm2"]):
            diffs.append(f"area {pres['area_mm2']:.4f}!={cres['area_mm2']:.4f}")
        if not _vec_close(pres["com"], cres["com"], 1e-5):
            diffs.append(f"com {pres['com']}!={cres['com']}")
        return diffs

    if not _rel(pres["volume_mm3"], cres["volume_mm3"]):
        diffs.append(f"vol {pres['volume_mm3']:.4f}!={cres['volume_mm3']:.4f}")
    if not _rel(pres["mass_g"], cres["mass_g"]):
        diffs.append(f"mass {pres['mass_g']:.6f}!={cres['mass_g']:.6f}")
    if not _vec_close(pres["com"], cres["com"], 1e-5):
        diffs.append(f"com {pres['com']}!={cres['com']}")
    if not _vec_close(pres["inertia"], cres["inertia"], 1e-5):
        diffs.append("inertia tensor mismatch")
    pm_p, pm_c = pres["principal_moments"], cres["principal_moments"]
    if not _vec_close(pm_p, pm_c, 1e-5):
        diffs.append(f"principal_moments {pm_p}!={pm_c}")
    if pres.get("material", {}).get("density_g_cm3") != cres.get("material", {}).get("density_g_cm3"):
        diffs.append("density mismatch")

    # principal_axes: up-to-sign, but ONLY for non-degenerate moments (a repeated moment
    # spans a plane/space where any orthonormal basis is valid).
    if not diffs:
        n_axes_checked = 0
        for i in range(3):
            degenerate = any(j != i and abs(pm_p[i] - pm_p[j]) <= 1e-4 * max(1.0, abs(pm_p[i]))
                             for j in range(3))
            if degenerate:
                continue
            n_axes_checked += 1
            if not _axis_match_up_to_sign(cres["principal_axes"][i], pres["principal_axes"][i]):
                diffs.append(f"axis[{i}] {cres['principal_axes'][i]} !~ ±{pres['principal_axes'][i]}")
        compare_mass.last_axes_note = f"axes±:{n_axes_checked}/3"
    return diffs


def compare_check(pres, cres):
    diffs = []
    pi, ci = pres.get("issues", []), cres.get("issues", [])
    if len(pi) != len(ci):
        return [f"issue count {len(pi)}!={len(ci)}"]
    # Compare the multiset of (kind, severity) classifications (exact keys/strings).
    pk = sorted((d.get("kind"), d.get("severity")) for d in pi)
    ck = sorted((d.get("kind"), d.get("severity")) for d in ci)
    if pk != ck:
        diffs.append(f"issue kinds {pk}!={ck}")
    return diffs


def compare_section(pres, cres):
    diffs = []
    for k in ("area_mm2", "Ixx", "Iyy", "Ixy"):
        if not _rel(pres[k], cres[k]):
            diffs.append(f"{k} {pres[k]:.6g}!={cres[k]:.6g}")
    if not _vec_close(pres["centroid"], cres["centroid"], 1e-5):
        diffs.append(f"centroid {pres['centroid']}!={cres['centroid']}")
    return diffs


def compare_symmetry(pres, cres):
    diffs = []
    if bool(pres["symmetric"]) != bool(cres["symmetric"]):
        diffs.append(f"symmetric {pres['symmetric']}!={cres['symmetric']}")
    if not _rel(pres["deviation_mm3"], cres["deviation_mm3"], 1e-5):
        diffs.append(f"deviation {pres['deviation_mm3']:.6f}!={cres['deviation_mm3']:.6f}")
    return diffs


def compare_interference(pres, cres):
    diffs = []
    if bool(pres["ok"]) != bool(cres["ok"]):
        diffs.append(f"ok {pres['ok']}!={cres['ok']}")
    if pres["summary"] != cres["summary"]:
        diffs.append(f"summary {pres['summary']!r}!={cres['summary']!r}")
    # parts: name + volume + bbox (order-stable, AICM order on both engines).
    if len(pres["parts"]) != len(cres["parts"]):
        diffs.append(f"part count {len(pres['parts'])}!={len(cres['parts'])}")
    else:
        for pp, cp in zip(pres["parts"], cres["parts"]):
            if pp["name"] != cp["name"]:
                diffs.append(f"part name {pp['name']}!={cp['name']}")
            if not _rel(pp["volume"], cp["volume"]):
                diffs.append(f"part vol {pp['volume']}!={cp['volume']}")
            if pp.get("bbox") and cp.get("bbox"):
                for key in ("min", "max", "size"):
                    if not _vec_close(pp["bbox"][key], cp["bbox"][key], 1e-5):
                        diffs.append(f"bbox {pp['name']}.{key}")
    # interference / clearance pair sets (a,b ordered; volume/distance numeric).
    def _pairset(lst, val):
        return {(d["a"], d["b"]): d[val] for d in lst}
    pi, ci = _pairset(pres["interferences"], "volume"), _pairset(cres["interferences"], "volume")
    if set(pi) != set(ci):
        diffs.append(f"interf pairs {set(pi)}!={set(ci)}")
    else:
        for k in pi:
            if not _rel(pi[k], ci[k]):
                diffs.append(f"interf vol {k} {pi[k]}!={ci[k]}")
    pcl, ccl = _pairset(pres["clearances"], "distance"), _pairset(cres["clearances"], "distance")
    if set(pcl) != set(ccl):
        diffs.append(f"clear pairs {set(pcl)}!={set(ccl)}")
    else:
        for k in pcl:
            if not _rel(pcl[k], ccl[k]):
                diffs.append(f"clear dist {k} {pcl[k]}!={ccl[k]}")
    return diffs


def compare_measure(pres, cres):
    diffs = []
    if pres.get("kind") != cres.get("kind"):
        diffs.append(f"kind {pres.get('kind')}!={cres.get('kind')}")
    for k in ("length", "radius", "area", "volume", "distance", "angle"):
        if k in pres or k in cres:
            if (k in pres) != (k in cres):
                diffs.append(f"{k} presence")
            elif not _rel(pres[k], cres[k], 1e-5):
                diffs.append(f"{k} {pres[k]:.6f}!={cres[k]:.6f}")
    if "points" in pres or "points" in cres:
        if (("points" in pres) != ("points" in cres)) or \
                len(pres.get("points", [])) != len(cres.get("points", [])):
            diffs.append("points presence/count")
        else:
            for pp, cp in zip(pres["points"], cres["points"]):
                if not _vec_close(pp, cp, 1e-5):
                    diffs.append(f"point {pp}!={cp}")
    return diffs


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[eval-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[eval-parity] libaicad ABI={core.abi()}")

    mism = 0
    total = 0

    # eval_mass over solids with distinct geometry + a density sweep.
    cases = [
        ("box 40x30x10  d=7.85", rect_then_extrude(40, 30, 10), 7.85, None),
        ("box 40x30x10  d=2.70", rect_then_extrude(40, 30, 10), 2.70, None),  # density scaling
        ("box 40x30x10  body=1", rect_then_extrude(40, 30, 10), 7.85, 1),     # explicit body id
        ("cube 20       d=7.85", rect_then_extrude(20, 20, 20), 7.85, None),  # degenerate axes
        ("cyl r10 h25   d=7.85", cyl_ops(10, 25), 7.85, None),                # 2 degenerate axes
        ("box 50x20x8   d=7.85", rect_then_extrude(50, 20, 8), 7.85, None),   # distinct moments
    ]
    for label, ops, density, body in cases:
        total += 1
        py = py_session()
        py.apply(0, ops)
        cpp = CppS(core)
        cpp.apply(0, ops)

        try:
            pres = py_mass(py, density, body)
        except Exception as exc:  # noqa: BLE001
            print(f"  {('eval_mass ' + label):<28} DIFF: py raised {type(exc).__name__}: {exc}")
            mism += 1
            continue
        cargs = {"doc_id": cpp.doc_id, "density_g_cm3": density}
        if body:
            cargs["body"] = body
        rc, cres = core.call("eval_mass", cargs)
        if rc != 0:
            print(f"  {('eval_mass ' + label):<28} DIFF: cpp rc={rc} {str(cres)[:70]}")
            mism += 1
            continue

        compare_mass.last_axes_note = ""
        diffs = compare_mass(pres, cres)
        note = f"  [{compare_mass.last_axes_note}]" if not diffs and compare_mass.last_axes_note else ""
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
        print(f"  {('eval_mass ' + label):<28} {verdict}{note}")
        mism += 0 if not diffs else 1

    # bad body_id errors on both engines.
    total += 1
    py = py_session(); py.apply(0, rect_then_extrude(10, 10, 10))
    cpp = CppS(core); cpp.apply(0, rect_then_extrude(10, 10, 10))
    be_diffs = []
    try:
        py_mass(py, 7.85, body=9)
        be_diffs.append("py did not raise")
    except Exception:  # noqa: BLE001 — IndexError/OpError expected
        pass
    rc, cres = core.call("eval_mass", {"doc_id": cpp.doc_id, "density_g_cm3": 7.85, "body": 9})
    if rc == 0:
        be_diffs.append("cpp did not error")
    print(f"  {'eval_mass bad body_id':<28} {'MATCH' if not be_diffs else 'DIFF: ' + '; '.join(be_diffs)}")
    mism += 0 if not be_diffs else 1

    def run(label, ops, method, cargs_extra, py_fn, cmp_fn):
        nonlocal total, mism
        total += 1
        py = py_session(); py.apply(0, ops)
        cpp = CppS(core); cpp.apply(0, ops)
        try:
            pres = py_fn(py)
        except Exception as exc:  # noqa: BLE001
            print(f"  {(method + ' ' + label):<30} DIFF: py raised {type(exc).__name__}: {exc}")
            mism += 1
            return
        rc, cres = core.call(method, {"doc_id": cpp.doc_id, **cargs_extra})
        if rc != 0:
            print(f"  {(method + ' ' + label):<30} DIFF: cpp rc={rc} {str(cres)[:70]}")
            mism += 1
            return
        diffs = cmp_fn(pres, cres)
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
        print(f"  {(method + ' ' + label):<30} {verdict}")
        mism += 0 if not diffs else 1

    # ---- 1. eval_check : valid solids report no issues on both engines ----
    for label, ops in [("box 40x30x10", rect_then_extrude(40, 30, 10)),
                       ("cube 20", rect_then_extrude(20, 20, 20)),
                       ("cyl r10 h25", cyl_ops(10, 25))]:
        run(label, ops, "eval_check", {}, lambda py: py_check(py), compare_check)

    # ---- 2. eval_section_props : section the LAST body by a reserved datum plane ----
    for label, ops, plane in [
        ("box top", rect_then_extrude(40, 30, 10), "plane.top"),
        ("box front", rect_then_extrude(40, 30, 10), "plane.front"),
        ("box right", rect_then_extrude(40, 30, 10), "plane.right"),
        ("box datum-sel", rect_then_extrude(40, 30, 10), {"kind": "datum", "feature": "plane.front"}),
        ("cyl front", cyl_ops(10, 25), "plane.front"),
    ]:
        run(label, ops, "eval_section_props", {"plane": plane},
            lambda py, p=plane: py_section(py, p), compare_section)

    # ---- 3. eval_symmetry : symmetric + asymmetric cases across reserved planes ----
    for label, ops, plane in [
        ("box top (sym)", rect_then_extrude(40, 30, 10), "plane.top"),
        ("box front (sym)", rect_then_extrude(40, 30, 10), "plane.front"),
        ("box right (sym)", rect_then_extrude(40, 30, 10), "plane.right"),
        ("cyl top (sym)", cyl_ops(10, 25), "plane.top"),
        # An L-shape (offset box) is NOT symmetric about plane.right (origin x=0):
        ("offset front (asym)", box_at(5, 0, 30, 20, 10, "B1"), "plane.right"),
    ]:
        run(label, ops, "eval_symmetry", {"plane": plane},
            lambda py, p=plane: py_symmetry(py, p), compare_symmetry)

    # ---- 4. eval_interference : single-body (empty) + 2-body overlap + 2-body clearance ----
    run("single body", rect_then_extrude(40, 30, 10), "eval_interference", {},
        lambda py: py_interference(py), compare_interference)
    run("2-body overlap", two_body_ops(10), "eval_interference", {},
        lambda py: py_interference(py), compare_interference)   # boxes 0-20 & 10-30 overlap
    run("2-body clearance", two_body_ops(30), "eval_interference", {},
        lambda py: py_interference(py), compare_interference)   # gap 10 mm

    # ---- 5. eval_measure : single (face/edge/vertex) + pair (faces) ----
    # Build refs from the LAST body's describe_* so both engines resolve the same entity.
    def measure_case(label, ops, kinds_indices):
        nonlocal total, mism
        total += 1
        py = py_session(); py.apply(0, ops)
        cpp = CppS(core); cpp.apply(0, ops)
        refs = [_ref_for(py, k, i) for (k, i) in kinds_indices]
        # The oracle resolves measure refs against the WHOLE-shape describe_* pool (worker.py::
        # _resolve_eval_ref). For a prismatic part, edge/vertex selectors built from that same
        # pool tie geometrically ("symmetric ambiguity") and DANGLE — so the oracle raises. The
        # faithful parity assertion is then "both engines fail identically", not a relaxed check.
        py_err = None
        try:
            pres = py_measure(py, refs)
        except Exception as exc:  # noqa: BLE001 — dangling ref -> OpError/TypeError
            py_err = exc
        rc, cres = core.call("eval_measure", {"doc_id": cpp.doc_id, "refs": refs})
        if py_err is not None:
            # Oracle could not resolve -> C++ MUST also reject (rc != 0). Same outcome = MATCH.
            ok = rc != 0
            verdict = "MATCH (both dangle)" if ok else f"DIFF: py raised but cpp rc={rc}"
            print(f"  {('eval_measure ' + label):<30} {verdict}")
            mism += 0 if ok else 1
            return
        if rc != 0:
            print(f"  {('eval_measure ' + label):<30} DIFF: cpp rc={rc} {str(cres)[:70]}")
            mism += 1
            return
        diffs = compare_measure(pres, cres)
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
        print(f"  {('eval_measure ' + label):<30} {verdict}")
        mism += 0 if not diffs else 1

    box_ops = rect_then_extrude(40, 30, 10)
    cyl_o = cyl_ops(10, 25)
    measure_case("1 face (area)", box_ops, [("face", 0)])
    measure_case("1 edge (length)", box_ops, [("edge", 0)])      # dangles on both (symmetric)
    measure_case("1 vertex (point)", box_ops, [("vertex", 0)])   # dangles on both (symmetric)
    measure_case("1 cyl edge (radius)", cyl_o, [("edge", 0)])    # dangles on both (symmetric)
    measure_case("2 faces (dist+angle)", box_ops, [("face", 0), ("face", 1)])

    print(f"\n[eval-parity] {total - mism}/{total} checks at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

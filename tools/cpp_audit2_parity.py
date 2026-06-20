"""cpp_audit2_parity.py — ADVERSARIAL differential audit of the C++ doc engine.

READ-ONLY bug hunt (it fixes nothing). Applies the same adversarial differential method that
found 16 bugs in the part feature compilers to the THREE areas not yet adversarially audited:

  A. SKETCH MODE      (sketch_enter/solve/drag/commit + under/full/over status, regions)
  B. EVALUATE suite   (mass/section/symmetry/interference/measure/check/draft/curvature/thickness)
  C. ASSEMBLY         (interference/clearance reporting, mate combos, explode, patterns, world bodies)

It drives the SAME op batch / call sequence on BOTH the Python ORACLE (engine.doc.session +
engine.worker handlers) and the staged C++ core (dist/AI-CAD/libaicad.dll), and prints every
divergence. A DIVERGENCE is a SUCCESS (a bug found). GAPS (a method/param unsupported in C++
where Python succeeds) are reported separately.

Tolerances: analytic 1e-6 rel; field methods reuse the eval-harness method (distinct-value set +
range for piecewise-constant fields; modal/physical band for thickness). No check is weakened to
hide a divergence.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_audit2_parity.py \
        --dll dist\AI-CAD\libaicad.dll

Exit code = number of divergences/gaps found (0 = full parity, i.e. no bugs in these areas).
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# =========================================================================== record / tally
FINDINGS: list[tuple[str, str, str]] = []   # (area, case, detail)


class Tally:
    def __init__(self):
        self.total = 0
        self.diff = 0

    def check(self, area, label, diffs):
        self.total += 1
        if diffs:
            self.diff += 1
            detail = "; ".join(str(d) for d in diffs)
            FINDINGS.append((area, label, detail))
            print(f"  [{area}] {label:<44} DIFF: {detail}")
        else:
            print(f"  [{area}] {label:<44} MATCH")


T = Tally()


# =========================================================================== generic helpers
def _rel(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _vec_close(a, b, tol=1e-6):
    return len(a) == len(b) and all(_rel(x, y, tol) for x, y in zip(a, b))


# =========================================================================== sketch builders
def _line(i, x1, y1, x2, y2, c=False):
    return {"id": i, "type": "line", "construction": c, "p1": [x1, y1], "p2": [x2, y2]}


def _circle(i, cx, cy, r, c=False):
    return {"id": i, "type": "circle", "construction": c, "center": [cx, cy], "radius": r}


def _arc(i, cx, cy, r, a0, a1, c=False, ccw=True):
    return {"id": i, "type": "arc", "construction": c,
            "center": [cx, cy], "radius": r, "start": a0, "end": a1, "ccw": ccw}


def _spline(i, pts, c=False):
    return {"id": i, "type": "spline", "construction": c, "points": [list(p) for p in pts]}


def _rel_c(i, t, refs):
    return {"id": i, "type": t, "refs": refs}


def _dim(i, t, refs, value, driven=False):
    return {"id": i, "type": t, "refs": refs,
            "value": {"value": value, "expr": None}, "driven": driven}


def _sketch_feature(entities, constraints, plane="plane.top"):
    return [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Sketch1",
        "params": {"entities": entities, "constraints": constraints},
        "refs": {"plane": plane}}}]


# =========================================================================== engines (sketch/eval)
def py_part_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("part")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)

        def enter(self, fid):
            return session.sketch_enter(self.s, fid)

        def solve(self, fid):
            return session.sketch_solve(self.s, fid)

        def overview(self, fid):
            return session.sketch_overview(self.s, fid)

        def regions(self, fid):
            return session.sketch_regions(self.s, fid)

        def drag(self, fid, handle, xy, targets=None):
            return session.sketch_drag(self.s, fid, handle, xy, targets)

        def drag_commit(self, fid):
            return session.sketch_drag_commit(self.s, fid, None)

        def probe(self, fid, action):
            return session.sketch_probe(self.s, fid, action)

    return PyS()


class CppPartS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]

    def _call(self, m, extra):
        return self.core.call(m, {"doc_id": self.doc_id, **extra})

    def apply(self, base_rev, ops):
        return self._call("doc_apply_ops", {"base_revision": base_rev, "ops": ops})[1]

    def enter(self, fid):
        return self._call("sketch_enter", {"sketch": fid})[1]

    def solve(self, fid):
        return self._call("sketch_solve", {"sketch": fid})[1]

    def overview(self, fid):
        return self._call("doc_sketch_overview", {"sketch": fid})[1]

    def regions(self, fid):
        return self._call("doc_sketch_regions", {"sketch": fid})[1]

    def drag(self, fid, handle, xy, targets=None):
        return self._call("sketch_drag",
                          {"sketch": fid, "handle": handle, "xy": xy, "targets": targets})[1]

    def drag_commit(self, fid):
        return self._call("sketch_drag_commit", {"sketch": fid})[1]

    def probe(self, fid, action):
        return self._call("sketch_probe", {"sketch": fid, "action": action})[1]


def sketch_fid(apply_resp):
    for f in apply_resp.get("tree", {}).get("features", []):
        if f.get("type") == "sketch":
            return f["id"]
    return None


# =========================================================================== sketch compare
def ent_map(resp):
    out = {}
    for e in resp.get("entities", []):
        eid = e.get("id")
        out[eid] = {k: v for k, v in e.items() if k not in ("id", "construction", "type")}
    return out


def _val_close(av, bv, tol):
    """Deep compare scalars / (nested) lists within tol."""
    if isinstance(av, list) and isinstance(bv, list):
        if len(av) != len(bv):
            return False
        return all(_val_close(x, y, tol) for x, y in zip(av, bv))
    if isinstance(av, list) or isinstance(bv, list):
        return False
    try:
        return abs(float(av) - float(bv)) <= tol
    except (TypeError, ValueError):
        return av == bv


def _coords_close(a, b, tol=1e-6):
    if set(a.keys()) != set(b.keys()):
        return False, f"keys {sorted(a.keys())}!={sorted(b.keys())}"
    for k in a:
        if not _val_close(a[k], b[k], tol):
            return False, f"{k} {a[k]}!={b[k]}"
    return True, ""


def diff_entities(pr, cr, tol=1e-6):
    pm, cm = ent_map(pr), ent_map(cr)
    if set(pm.keys()) != set(cm.keys()):
        return [f"entity ids {sorted(pm.keys())}!={sorted(cm.keys())}"]
    diffs = []
    for eid in pm:
        ok, why = _coords_close(pm[eid], cm[eid], tol)
        if not ok:
            diffs.append(f"ent {eid}: {why}")
    return diffs


def diff_solve_resp(pr, cr, skip_coords=False):
    # skip_coords: for an UNDER-constrained sketch the solved pose has a FREE DOF, so the
    # exact entity coordinates are gauge-arbitrary (each engine slides to a different — both
    # valid — solution). The caller verifies CONSTRAINT SATISFACTION separately; here we still
    # compare the parity-meaningful invariants (dof/status/solve_rejected/conflicting/redundant).
    diffs = [] if skip_coords else diff_entities(pr, cr)
    if int(pr.get("dof", -99)) != int(cr.get("dof", -98)):
        diffs.append(f"dof {pr.get('dof')}!={cr.get('dof')}")
    if pr.get("status") != cr.get("status"):
        diffs.append(f"status {pr.get('status')!r}!={cr.get('status')!r}")
    if pr.get("solve_rejected") != cr.get("solve_rejected"):
        diffs.append(f"solve_rejected {pr.get('solve_rejected')}!={cr.get('solve_rejected')}")
    if set(pr.get("conflicting", []) or []) != set(cr.get("conflicting", []) or []):
        diffs.append(f"conflicting {pr.get('conflicting')}!={cr.get('conflicting')}")
    if set(pr.get("redundant", []) or []) != set(cr.get("redundant", []) or []):
        diffs.append(f"redundant {pr.get('redundant')}!={cr.get('redundant')}")
    if (pr.get("statuses", {}) or {}) != (cr.get("statuses", {}) or {}):
        diffs.append(f"statuses {pr.get('statuses')}!={cr.get('statuses')}")
    return diffs


def run_sketch_enter(label, ops, expect_status=None, gauge_free=None):
    py = py_part_session(); pr_apply = py.apply(0, ops)
    cpp = CppPartS(CORE); cr_apply = cpp.apply(0, ops)
    pfid, cfid = sketch_fid(pr_apply), sketch_fid(cr_apply)
    if pfid != cfid:
        T.check("SKETCH", label, [f"fid py={pfid} cpp={cfid}"])
        return
    pe, ce = py.enter(pfid), cpp.enter(cfid)
    d = diff_solve_resp(pe, ce, skip_coords=gauge_free is not None)
    # gauge_free(resp)->list[str]: for an under-constrained sketch, assert CONSTRAINT
    # SATISFACTION (e.g. an entity lies on its collinear line) in EACH engine instead of
    # comparing the gauge-arbitrary free-DOF coords. Returns any violations.
    if gauge_free is not None:
        d += [f"py: {m}" for m in gauge_free(pe)]
        d += [f"cpp: {m}" for m in gauge_free(ce)]
    if expect_status is not None and pe.get("status") != expect_status:
        d.append(f"ORACLE status {pe.get('status')!r} != expected {expect_status!r}")
    T.check("SKETCH", label, d)
    return py, cpp, pfid


# =========================================================================== eval engines
def py_eval_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("part")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)
    return PyS()


class CppEvalS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]

    def apply(self, base_rev, ops):
        return self.core.call("doc_apply_ops",
                              {"doc_id": self.doc_id, "base_revision": base_rev, "ops": ops})[1]


# ---- eval op builders (mirror cpp_eval_parity) ----
def _eln(sketch, x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": sketch,
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def rect_extrude(w, h, depth, plane="plane.top"):
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": plane}}},
        _eln("$0", 0, 0, w, 0), _eln("$0", w, 0, w, h), _eln("$0", w, h, 0, h), _eln("$0", 0, h, 0, 0),
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Boss1",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                  "centroid": [w / 2, h / 2], "area": w * h, "hint_index": 0}]}}},
    ]


def box_at(x0, y0, w, h, depth, name, merge=True, base=0):
    sk = "$" + str(base)
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S" + name,
            "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.top"}}},
        _eln(sk, x0, y0, x0 + w, y0), _eln(sk, x0 + w, y0, x0 + w, y0 + h),
        _eln(sk, x0 + w, y0 + h, x0, y0 + h), _eln(sk, x0, y0 + h, x0, y0),
        {"op": "add_feature", "feature": {"type": "extrude", "name": name,
            "params": {"mode": "boss", "merge": merge, "end": "blind",
                       "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": sk,
                                  "centroid": [x0 + w / 2, y0 + h / 2], "area": w * h, "hint_index": 0}]}}},
    ]


def prim(fid, ptype, params):
    return [{"op": "add_feature", "feature": {"id": fid, "type": ptype,
             "params": {k: {"value": v, "expr": None} for k, v in params.items()}}}]


def cyl_ops(r, h):
    return prim("c1", "cylinder", {"radius": r, "height": h})


def sphere_ops(r):
    return prim("s1", "sphere", {"radius": r})


def torus_ops(R, r):
    return prim("t1", "torus", {"major_r": R, "minor_r": r})


def two_body_ops(sep):
    return box_at(0, 0, 20, 20, 10, "B1", merge=False, base=0) + \
        box_at(sep, 0, 20, 20, 10, "B2", merge=False, base=6)


# through-hole part: 30x30x10 box minus a centered cylinder (non-convex)
def box_with_hole(side, depth, hole_r):
    half = side / 2.0
    return [
        {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
            "params": {"length": {"value": side, "expr": None}, "width": {"value": side, "expr": None},
                       "height": {"value": depth, "expr": None}, "centered": True}, "refs": {}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Hole",
            "params": {"radius": {"value": hole_r, "expr": None},
                       "height": {"value": depth * 2.0, "expr": None}, "centered": True, "mode": "cut"},
            "refs": {}}},
    ]


# ---- eval oracle wrappers (mirror worker handlers) ----
def py_mass(py, density, body=None):
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


def py_interference_part(py):
    import engine.doc.evaluate as ev
    from engine.doc import session as docsess
    result = docsess._ensure_built(py.s)
    members = [(bo.name or bo.body_id, bo.shape) for bo in result.bodies]
    return ev.interference(members)


def py_measure(py, refs):
    import engine.doc.evaluate as ev
    from engine.doc import session as docsess
    from engine.doc import selectors as sel
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
    from engine.doc import session as docsess
    from engine.doc import selectors as sel
    result = docsess._ensure_built(py.s)
    raw = list(result.bodies)[-1].shape.wrapped
    descs = {"face": sel.describe_faces, "edge": sel.describe_edges,
             "vertex": sel.describe_vertices}[kind](raw)
    return sel.make_selector(descs[occ_index])


def py_field_aicm(py, method, params):
    import engine.worker as worker
    handler = worker._METHODS[method]
    full = {"doc_id": py.doc_id, **params}
    return handler(full, lambda *_a, **_k: None)["aicm_path"]


def _read_aicm_field(path):
    from engine.doc import tessellate as tess
    with open(path, "rb") as f:
        data = f.read()
    parsed = tess.read_aicm(data)
    sc = parsed["scalars"][0] if parsed["scalars"] else None
    body = parsed["bodies"][0] if parsed["bodies"] else None
    return sc, body


# ---- eval compare (mirror cpp_eval_parity) ----
def compare_mass(pres, cres):
    diffs = []
    if bool(pres.get("is_surface")) != bool(cres.get("is_surface")):
        return [f"is_surface {pres.get('is_surface')}!={cres.get('is_surface')}"]
    if pres.get("is_surface"):
        if not _rel(pres["area_mm2"], cres["area_mm2"]):
            diffs.append(f"area {pres['area_mm2']:.4f}!={cres['area_mm2']:.4f}")
        return diffs
    if not _rel(pres["volume_mm3"], cres["volume_mm3"]):
        diffs.append(f"vol {pres['volume_mm3']:.4f}!={cres['volume_mm3']:.4f}")
    if not _rel(pres["mass_g"], cres["mass_g"]):
        diffs.append(f"mass {pres['mass_g']:.6f}!={cres['mass_g']:.6f}")
    if not _vec_close(pres["com"], cres["com"], 1e-5):
        diffs.append(f"com {pres['com']}!={cres['com']}")
    if not _vec_close(pres["inertia"], cres["inertia"], 1e-5):
        diffs.append("inertia tensor mismatch")
    if not _vec_close(pres["principal_moments"], cres["principal_moments"], 1e-5):
        diffs.append(f"principal_moments {pres['principal_moments']}!={cres['principal_moments']}")
    return diffs


def compare_check(pres, cres):
    pi, ci = pres.get("issues", []), cres.get("issues", [])
    if len(pi) != len(ci):
        return [f"issue count {len(pi)}!={len(ci)} (py={[d.get('kind') for d in pi]} cpp={[d.get('kind') for d in ci]})"]
    pk = sorted((d.get("kind"), d.get("severity")) for d in pi)
    ck = sorted((d.get("kind"), d.get("severity")) for d in ci)
    return [f"issue kinds {pk}!={ck}"] if pk != ck else []


def compare_section(pres, cres):
    diffs = []
    for k in ("area_mm2", "Ixx", "Iyy", "Ixy"):
        if not _rel(pres[k], cres[k], 1e-5):
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
    if len(pres["parts"]) != len(cres["parts"]):
        diffs.append(f"part count {len(pres['parts'])}!={len(cres['parts'])}")

    def _pairset(lst, val):
        return {(d["a"], d["b"]): d[val] for d in lst}
    pi, ci = _pairset(pres["interferences"], "volume"), _pairset(cres["interferences"], "volume")
    if set(pi) != set(ci):
        diffs.append(f"interf pairs py={set(pi)} cpp={set(ci)}")
    else:
        for k in pi:
            if not _rel(pi[k], ci[k]):
                diffs.append(f"interf vol {k} py={pi[k]:.4f} cpp={ci[k]:.4f}")
    pcl, ccl = _pairset(pres["clearances"], "distance"), _pairset(cres["clearances"], "distance")
    if set(pcl) != set(ccl):
        diffs.append(f"clearance pairs py={set(pcl)} cpp={set(ccl)}")
    else:
        for k in pcl:
            if not _rel(pcl[k], ccl[k]):
                diffs.append(f"clear dist {k} py={pcl[k]:.4f} cpp={ccl[k]:.4f}")
    return diffs


def compare_measure(pres, cres):
    diffs = []
    if pres.get("kind") != cres.get("kind"):
        diffs.append(f"kind {pres.get('kind')}!={cres.get('kind')}")
    for k in ("length", "radius", "area", "volume", "distance", "angle"):
        if (k in pres) != (k in cres):
            diffs.append(f"{k} presence py={k in pres} cpp={k in cres}")
        elif k in pres and not _rel(pres[k], cres[k], 1e-5):
            diffs.append(f"{k} {pres[k]:.6f}!={cres[k]:.6f}")
    return diffs


# ---- field compare (mirror cpp_eval_parity) ----
def _finite_vals(sc):
    import numpy as np
    v = np.asarray(sc["values"], dtype=np.float64)
    return v[np.isfinite(v)]


def _distinct_rounded(sc, ndigits):
    import numpy as np
    fv = _finite_vals(sc)
    return set(np.round(fv, ndigits).tolist()) if len(fv) else set()


def _close_sets(sa, sb, tol):
    def covered(src, dst):
        return all(any(abs(x - y) <= tol for y in dst) for x in src)
    return covered(sa, sb) and covered(sb, sa)


def compare_field_set(psc, csc, set_digits=1, set_tol=5e-2, range_tol=1e-2):
    diffs = []
    if psc is None or csc is None:
        return [f"missing SCLR (py={psc is not None} cpp={csc is not None})"]
    ps, cs = _distinct_rounded(psc, set_digits), _distinct_rounded(csc, set_digits)
    if not _close_sets(ps, cs, set_tol):
        diffs.append(f"value-set py={sorted(round(x,2) for x in ps)} cpp={sorted(round(x,2) for x in cs)}")
    if abs(psc["vmin"] - csc["vmin"]) > range_tol * max(1.0, abs(psc["vmin"])):
        diffs.append(f"vmin py={psc['vmin']:.4f} cpp={csc['vmin']:.4f}")
    if abs(psc["vmax"] - csc["vmax"]) > range_tol * max(1.0, abs(psc["vmax"])):
        diffs.append(f"vmax py={psc['vmax']:.4f} cpp={csc['vmax']:.4f}")
    return diffs


def compare_field_physical(csc, expect, band_frac=0.06, band_abs=0.4):
    import numpy as np
    if csc is None:
        return ["missing SCLR"], ""
    fv = _finite_vals(csc)
    if not len(fv):
        return ["no finite values"], ""
    binned = np.round(fv, 1)
    vals, counts = np.unique(binned, return_counts=True)
    modal = float(vals[int(np.argmax(counts))])
    band = max(band_abs, band_frac * expect)
    note = f"modal={modal:.3f} expect={expect:.1f} band=±{band:.2f}"
    if abs(modal - expect) > band:
        return [f"modal thickness {modal:.3f} outside {expect:.1f}±{band:.2f}"], note
    return [], note


def run_eval(area, label, ops, method, cargs_extra, py_fn, cmp_fn):
    py = py_eval_session(); py.apply(0, ops)
    cpp = CppEvalS(CORE); cpp.apply(0, ops)
    try:
        pres = py_fn(py)
    except Exception as exc:  # noqa: BLE001
        T.check(area, label, [f"py raised {type(exc).__name__}: {exc}"])
        return
    rc, cres = CORE.call(method, {"doc_id": cpp.doc_id, **cargs_extra})
    if rc != 0:
        T.check(area, label, [f"GAP: cpp rc={rc} {str(cres)[:90]}"])
        return
    T.check(area, label, cmp_fn(pres, cres))


def run_field(area, label, ops, method, params, cmp_fn):
    py = py_eval_session(); py.apply(0, ops)
    cpp = CppEvalS(CORE); cpp.apply(0, ops)
    try:
        ppath = py_field_aicm(py, method, params)
        psc, _ = _read_aicm_field(ppath)
    except Exception as exc:  # noqa: BLE001
        T.check(area, label, [f"py raised {type(exc).__name__}: {exc}"])
        return
    rc, cres = CORE.call(method, {"doc_id": cpp.doc_id, **params})
    if rc != 0:
        T.check(area, label, [f"GAP: cpp rc={rc} {str(cres)[:90]}"])
        return
    try:
        csc, _ = _read_aicm_field(cres["aicm_path"])
    except Exception as exc:  # noqa: BLE001
        T.check(area, label, [f"cpp aicm unreadable: {exc}"])
        return
    res = cmp_fn(psc, csc)
    if isinstance(res, tuple):
        diffs, note = res
        if note and not diffs:
            label = f"{label} [{note}]"
    else:
        diffs = res
    T.check(area, label, diffs)


# =========================================================================== assembly engine
_TMP = Path(tempfile.mkdtemp(prefix="aicad_audit2_"))


def _write_box_step(name, w, h, d):
    from build123d import Box, Location, export_step
    path = _TMP / f"{name}.step"
    if not path.exists():
        export_step(Location((w / 2, h / 2, d / 2)) * Box(w, h, d), str(path))
    return str(path)


def _write_cyl_step(name, r, h):
    from build123d import Cylinder, Location, export_step
    path = _TMP / f"{name}.step"
    if not path.exists():
        export_step(Location((0, 0, h / 2)) * Cylinder(r, h), str(path))
    return str(path)


def _face_selector(centroid, axis, area):
    return {"kind": "face", "surf": "plane", "centroid": list(centroid), "axis": list(axis),
            "area_or_len": area, "owner_feature": None, "hint_index": 0}


def _cyl_face_selector(centroid, axis, area):
    return {"kind": "face", "surf": "cylinder", "centroid": list(centroid), "axis": list(axis),
            "area_or_len": area, "owner_feature": None, "hint_index": 0}


def _insert(path, name, transform=None, fixed=None):
    op = {"op": "asm_insert_component", "source": {"path": path}, "name": name}
    if transform is not None:
        op["transform"] = transform
    if fixed is not None:
        op["fixed"] = fixed
    return op


def _mate(mtype, refs, **kw):
    m = {"type": mtype, "refs": refs}
    m.update(kw)
    return {"op": "asm_add_mate", "mate": m}


def py_asm_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("assembly")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)

        def solve(self, drag=None):
            return session.solve_mates(self.s, drag, None)

        def set_explode(self, active, factor):
            return session.set_explode_state(self.s, active, factor, None)

        def world_bodies(self, explode=False):
            return session.asm_world_bodies(self.s, explode=explode)

        def interference(self):
            import engine.doc.evaluate as ev
            return ev.interference(session.asm_world_bodies(self.s, explode=False))
    return PyS()


class CppAsmS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "assembly"})
        assert rc == 0, f"cpp doc_new(assembly) rc={rc}: {r}"
        self.doc_id = r["doc_id"]

    def apply(self, base_rev, ops):
        rc, r = self.core.call("doc_apply_ops",
                               {"doc_id": self.doc_id, "base_revision": base_rev, "ops": ops})
        assert rc == 0, f"cpp doc_apply_ops rc={rc}: {r}"
        return r

    def solve(self, drag=None):
        params = {"doc_id": self.doc_id}
        if drag is not None:
            params["drag"] = drag
        rc, r = self.core.call("doc_solve_mates", params)
        assert rc == 0, f"cpp doc_solve_mates rc={rc}: {r}"
        return r

    def set_explode(self, active, factor):
        rc, r = self.core.call("doc_set_explode_state",
                               {"doc_id": self.doc_id, "active": active, "factor": factor})
        return rc, r

    def world_bodies_rpc(self, explode=False):
        rc, r = self.core.call("asm_world_bodies", {"doc_id": self.doc_id, "explode": explode})
        return rc, r


def _comp_transforms(resp):
    return {c["id"]: c["transform"] for c in resp["tree"].get("components", [])}


def _mate_rows(resp):
    return {m["id"]: (m["type"], m["status"]) for m in resp["rebuild"].get("mates", [])}


def compare_asm_status(pres, cres):
    diffs = []
    pm, cm = _mate_rows(pres), _mate_rows(cres)
    if pm != cm:
        diffs.append(f"mate_status py={pm} cpp={cm}")
    if pres["rebuild"].get("dof") != cres["rebuild"].get("dof"):
        diffs.append(f"dof py={pres['rebuild'].get('dof')} cpp={cres['rebuild'].get('dof')}")
    if bool(pres["rebuild"].get("over_defined")) != bool(cres["rebuild"].get("over_defined")):
        diffs.append(f"over_defined py={pres['rebuild'].get('over_defined')} cpp={cres['rebuild'].get('over_defined')}")
    if len(pres["tess"]["bodies"]) != len(cres["tess"]["bodies"]):
        diffs.append(f"body count py={len(pres['tess']['bodies'])} cpp={len(cres['tess']['bodies'])}")
    return diffs


def asm_world_bbox(py):
    """Oracle world bbox per component {cid: (min,max)} from asm_world_bodies shapes."""
    out = {}
    for name, shape in py.world_bodies(explode=False):
        bb = shape.bounding_box()
        out[name] = ([bb.min.X, bb.min.Y, bb.min.Z], [bb.max.X, bb.max.Y, bb.max.Z])
    return out


# =========================================================================== MAIN
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "dist" / "AI-CAD" / "libaicad.dll"))
    args = ap.parse_args()
    dll = Path(args.dll)
    if not dll.exists():
        print(f"[audit2] libaicad.dll not found at {dll}")
        return 2
    global CORE
    CORE = CppCore(dll)
    print(f"[audit2] libaicad ABI={CORE.abi()}  dll={dll}\n")

    # =================================================================== A. SKETCH MODE
    print("=== A. SKETCH MODE (adversarial) ===")

    # A1. Arc + line tangent chain (open). Arc center fixed, radius dim, tangent to line.
    def arc_tangent():
        ents = [_arc("e1", 0, 0, 10, 0, 90),
                _line("e2", 10, 0, 30, 0)]
        cons = [_rel_c("c1", "fix", ["e1.center"]),
                _dim("c2", "radius", ["e1"], 10.0),
                _rel_c("c3", "coincident", ["e1.start", "e2.p1"]),
                _rel_c("c4", "tangent", ["e1", "e2"]),
                _rel_c("c5", "horizontal", ["e2"]),
                _dim("c6", "distance", ["e2.p1", "e2.p2"], 20.0)]
        return _sketch_feature(ents, cons)
    run_sketch_enter("arc+line tangent chain", arc_tangent())

    # A2. Two circles equal + concentric  (equal radius, shared center)
    def circles_equal_concentric():
        ents = [_circle("e1", 0, 0, 12), _circle("e2", 0, 0, 5)]
        cons = [_rel_c("c1", "fix", ["e1.center"]),
                _rel_c("c2", "concentric", ["e1", "e2"]),
                _rel_c("c3", "equal", ["e1", "e2"]),
                _dim("c4", "radius", ["e1"], 12.0)]
        return _sketch_feature(ents, cons)
    run_sketch_enter("two circles equal+concentric", circles_equal_concentric())

    # A3. Symmetric points about a construction centerline
    def symmetric_midpoint():
        ents = [_line("e1", 0, -20, 0, 20, c=True),
                _line("e2", -10, 5, 10, 5),
                _line("e3", -8, -8, 8, -8)]
        cons = [_rel_c("c1", "fix", ["e1.p1"]), _rel_c("c2", "fix", ["e1.p2"]),
                _rel_c("c3", "symmetric", ["e2.p1", "e2.p2", "e1"]),
                _rel_c("c4", "symmetric", ["e3.p1", "e3.p2", "e1"]),
                _rel_c("c5", "horizontal", ["e2"]), _rel_c("c6", "horizontal", ["e3"]),
                _dim("c7", "distance", ["e2.p1", "e2.p2"], 20.0)]
        return _sketch_feature(ents, cons)
    run_sketch_enter("symmetric+construction line", symmetric_midpoint())

    # A4. Spline through 4 points, two endpoints fixed (under-defined)
    def spline_sketch():
        ents = [_spline("e1", [(0, 0), (10, 8), (20, -4), (30, 6)])]
        cons = [_rel_c("c1", "fix", ["e1.p0"]), _rel_c("c2", "fix", ["e1.p3"])]
        return _sketch_feature(ents, cons)
    run_sketch_enter("spline (under)", spline_sketch())

    # A5. Collinear lines
    def collinear_sketch():
        ents = [_line("e1", 0, 0, 10, 0), _line("e2", 20, 2, 30, 2)]
        cons = [_rel_c("c1", "fix", ["e1.p1"]), _rel_c("c2", "horizontal", ["e1"]),
                _rel_c("c3", "collinear", ["e1", "e2"]),
                _dim("c4", "distance", ["e1.p1", "e1.p2"], 10.0)]
        return _sketch_feature(ents, cons)
    # e1 is fixed horizontal at y=0; e2 is collinear to e1 but otherwise FREE (dof=2). The
    # solved e2 slides along the line (a free DOF) to a gauge-arbitrary x that differs between
    # engines — NOT a parity break. The load-bearing invariant is that e2 actually LIES on
    # e1's line (collinear satisfied: both endpoints at y=0) and the solve is not rejected.
    def _collinear_ok(resp):
        em = ent_map(resp)
        return [f"e2.{pt} off collinear line (y={em['e2'][pt][1]:.2e})"
                for pt in ("p1", "p2") if abs(em["e2"][pt][1]) > 1e-6]
    run_sketch_enter("collinear lines", collinear_sketch(), gauge_free=_collinear_ok)

    # A6. Progressive under -> full -> over (rect chain)
    def rect_ents():
        return [_line("e1", 0, 0, 40, 0), _line("e2", 40, 0, 40, 30),
                _line("e3", 40, 30, 0, 30), _line("e4", 0, 30, 0, 0)]

    def rect_chain():
        return [_rel_c("c1", "coincident", ["e1.p2", "e2.p1"]),
                _rel_c("c2", "coincident", ["e2.p2", "e3.p1"]),
                _rel_c("c3", "coincident", ["e3.p2", "e4.p1"]),
                _rel_c("c4", "coincident", ["e4.p2", "e1.p1"]),
                _rel_c("c5", "horizontal", ["e1"]), _rel_c("c6", "horizontal", ["e3"]),
                _rel_c("c7", "vertical", ["e2"]), _rel_c("c8", "vertical", ["e4"]),
                _rel_c("c9", "fix", ["e1.p1"])]

    # under: one dim only
    run_sketch_enter("rect under (1 dim)",
                     _sketch_feature(rect_ents(), rect_chain() +
                                     [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0)]),
                     expect_status="under")
    # over via redundant GEOMETRIC relation (extra horizontal on e1) — must NOT over-define
    run_sketch_enter("rect + redundant GEOM rel (not over)",
                     _sketch_feature(rect_ents(), rect_chain() +
                                     [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                                      _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0),
                                      _rel_c("c12", "horizontal", ["e1"])]),
                     expect_status="full")
    # over via redundant DRIVING dim
    run_sketch_enter("rect + redundant DRIVING dim (over)",
                     _sketch_feature(rect_ents(), rect_chain() +
                                     [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                                      _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0),
                                      _dim("c12", "distance", ["e1.p1", "e1.p2"], 40.0)]),
                     expect_status="over")

    # A7. Sketch on plane.front and plane.right (plane frame divergence)
    run_sketch_enter("rect on plane.front",
                     _sketch_feature(rect_ents(), rect_chain() +
                                     [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                                      _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0)],
                                     plane="plane.front"),
                     expect_status="full")
    run_sketch_enter("circle on plane.right",
                     _sketch_feature([_circle("e1", 0, 0, 20)],
                                     [_rel_c("c1", "fix", ["e1.center"]),
                                      _dim("c2", "radius", ["e1"], 20.0)],
                                     plane="plane.right"),
                     expect_status="full")

    # A8. Fully-constrained drag must NOT move (solver resists)
    full_rect = _sketch_feature(rect_ents(), rect_chain() +
                                [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                                 _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0)])
    py = py_part_session(); pa = py.apply(0, full_rect)
    cpp = CppPartS(CORE); ca = cpp.apply(0, full_rect)
    pfid = sketch_fid(pa)
    py.enter(pfid); cpp.enter(pfid)
    pd = py.drag(pfid, "e2.p1", [80.0, 40.0])
    cd = cpp.drag(pfid, "e2.p1", [80.0, 40.0])
    T.check("SKETCH", "fully-constrained drag (no-move)", diff_entities(pd, cd))

    # A9. Regions: washer (even-odd) + multi-loop (two disjoint squares)
    def washer():
        ents = [_line("e1", -20, -20, 20, -20), _line("e2", 20, -20, 20, 20),
                _line("e3", 20, 20, -20, 20), _line("e4", -20, 20, -20, -20),
                _circle("e5", 0, 0, 8.0)]
        cons = [_rel_c("c1", "coincident", ["e1.p2", "e2.p1"]),
                _rel_c("c2", "coincident", ["e2.p2", "e3.p1"]),
                _rel_c("c3", "coincident", ["e3.p2", "e4.p1"]),
                _rel_c("c4", "coincident", ["e4.p2", "e1.p1"])]
        return _sketch_feature(ents, cons)

    def two_squares():
        def sq(b, x0, y0, s):
            return [_line(f"e{b}", x0, y0, x0 + s, y0), _line(f"e{b+1}", x0 + s, y0, x0 + s, y0 + s),
                    _line(f"e{b+2}", x0 + s, y0 + s, x0, y0 + s), _line(f"e{b+3}", x0, y0 + s, x0, y0)]

        def chain(b, cb):
            return [_rel_c(f"c{cb}", "coincident", [f"e{b}.p2", f"e{b+1}.p1"]),
                    _rel_c(f"c{cb+1}", "coincident", [f"e{b+1}.p2", f"e{b+2}.p1"]),
                    _rel_c(f"c{cb+2}", "coincident", [f"e{b+2}.p2", f"e{b+3}.p1"]),
                    _rel_c(f"c{cb+3}", "coincident", [f"e{b+3}.p2", f"e{b}.p1"])]
        return _sketch_feature(sq(1, 0, 0, 10) + sq(5, 30, 0, 10), chain(1, 1) + chain(5, 5))

    for label, mk in [("washer regions (even-odd)", washer),
                      ("two-square multi-loop regions", two_squares)]:
        py = py_part_session(); pa = py.apply(0, mk())
        cpp = CppPartS(CORE); cpp.apply(0, mk())
        pfid = sketch_fid(pa)
        prg, crg = py.regions(pfid), cpp.regions(pfid)
        rdiffs = []
        preg, creg = prg.get("regions", []), crg.get("regions", [])
        if len(preg) != len(creg):
            rdiffs.append(f"region count py={len(preg)} cpp={len(creg)}")
        else:
            for pr_, cr_ in zip(preg, creg):
                if bool(pr_.get("is_material")) != bool(cr_.get("is_material")):
                    rdiffs.append(f"is_material[{pr_.get('index')}] {pr_.get('is_material')}!={cr_.get('is_material')}")
                pa2, ca2 = float(pr_.get("area", 0)), float(cr_.get("area", 0))
                if abs(pa2 - ca2) > max(1e-3, 1e-3 * abs(pa2)):
                    rdiffs.append(f"area[{pr_.get('index')}] py={pa2:.4f} cpp={ca2:.4f}")
                pc2, cc2 = pr_.get("centroid", []), cr_.get("centroid", [])
                if len(pc2) == 2 == len(cc2) and any(abs(a - b) > 1e-3 for a, b in zip(pc2, cc2)):
                    rdiffs.append(f"centroid[{pr_.get('index')}] py={pc2} cpp={cc2}")
        T.check("SKETCH", label, rdiffs)

    # =================================================================== B. EVALUATE
    print("\n=== B. EVALUATE (tricky geometry) ===")

    # B1. mass on tricky bodies
    run_eval("EVAL", "mass: box w/ through-hole", box_with_hole(30, 10, 6), "eval_mass",
             {"density_g_cm3": 7.85}, lambda py: py_mass(py, 7.85), compare_mass)
    run_eval("EVAL", "mass: 2nd body of 2-body (body=2)", two_body_ops(30), "eval_mass",
             {"density_g_cm3": 7.85, "body": 2}, lambda py: py_mass(py, 7.85, 2), compare_mass)
    run_eval("EVAL", "mass: sphere r12", sphere_ops(12), "eval_mass",
             {"density_g_cm3": 2.70}, lambda py: py_mass(py, 2.70), compare_mass)
    run_eval("EVAL", "mass: torus R20 r5", torus_ops(20, 5), "eval_mass",
             {"density_g_cm3": 7.85}, lambda py: py_mass(py, 7.85), compare_mass)

    # B2. check on a healthy through-hole part (non-convex) — should be no issues both
    run_eval("EVAL", "check: box w/ through-hole", box_with_hole(30, 10, 6), "eval_check",
             {}, lambda py: py_check(py), compare_check)
    run_eval("EVAL", "check: torus", torus_ops(20, 5), "eval_check",
             {}, lambda py: py_check(py), compare_check)

    # B3. section through off-origin offset plane (datum) — use reserved planes only (resolvable)
    for label, ops, plane in [
        ("section: hole-part front", box_with_hole(30, 10, 6), "plane.front"),
        ("section: hole-part top", box_with_hole(30, 10, 6), "plane.top"),
        ("section: torus top", torus_ops(20, 5), "plane.top"),
        ("section: sphere right", sphere_ops(12), "plane.right"),
    ]:
        run_eval("EVAL", label, ops, "eval_section_props", {"plane": plane},
                 lambda py, p=plane: py_section(py, p), compare_section)

    # B4. symmetry: non-symmetric L-shape; symmetric hole-part
    run_eval("EVAL", "symmetry: hole-part top (sym)", box_with_hole(30, 10, 6), "eval_symmetry",
             {"plane": "plane.top"}, lambda py: py_symmetry(py, "plane.top"), compare_symmetry)
    run_eval("EVAL", "symmetry: L-offset front (asym)", box_at(5, 0, 30, 20, 10, "B1"),
             "eval_symmetry", {"plane": "plane.right"},
             lambda py: py_symmetry(py, "plane.right"), compare_symmetry)

    # B5. measure: face area on hole-part; curved cyl face; two-face angle
    def measure_case(label, ops, kinds_indices):
        py = py_eval_session(); py.apply(0, ops)
        cpp = CppEvalS(CORE); cpp.apply(0, ops)
        try:
            refs = [_ref_for(py, k, i) for (k, i) in kinds_indices]
        except Exception as exc:  # noqa: BLE001
            T.check("EVAL", label, [f"py ref build raised {exc}"]); return
        py_err = None
        try:
            pres = py_measure(py, refs)
        except Exception as exc:  # noqa: BLE001
            py_err = exc
        rc, cres = CORE.call("eval_measure", {"doc_id": cpp.doc_id, "refs": refs})
        if py_err is not None:
            T.check("EVAL", label, [] if rc != 0 else [f"py dangled but cpp rc=0 (cres={str(cres)[:60]})"])
            return
        if rc != 0:
            T.check("EVAL", label, [f"GAP: cpp rc={rc} {str(cres)[:80]}"]); return
        T.check("EVAL", label, compare_measure(pres, cres))

    measure_case("measure: hole-part face0", box_with_hole(30, 10, 6), [("face", 0)])
    measure_case("measure: cyl curved face", cyl_ops(10, 25), [("face", 0)])
    measure_case("measure: cyl 2 faces (dist/angle)", cyl_ops(10, 25), [("face", 0), ("face", 1)])
    measure_case("measure: sphere face", sphere_ops(12), [("face", 0)])

    # B6. interference (part-level, multi-body): overlap + clearance
    run_eval("EVAL", "interference: 2-body overlap", two_body_ops(10), "eval_interference",
             {}, lambda py: py_interference_part(py), compare_interference)
    run_eval("EVAL", "interference: 2-body clearance", two_body_ops(30), "eval_interference",
             {}, lambda py: py_interference_part(py), compare_interference)

    # B7. curvature/thickness on cyl/sphere/torus
    run_field("EVAL", "curvature mean: torus", torus_ops(20, 5), "eval_curvature",
              {"kind": "mean"}, lambda p, c: compare_field_set(p, c, set_digits=3, set_tol=1e-2))
    run_field("EVAL", "curvature gaussian: torus", torus_ops(20, 5), "eval_curvature",
              {"kind": "gaussian"}, lambda p, c: compare_field_set(p, c, set_digits=4, set_tol=5e-3))
    run_field("EVAL", "curvature mean: sphere r12", sphere_ops(12), "eval_curvature",
              {"kind": "mean"}, lambda p, c: compare_field_set(p, c, set_digits=3, set_tol=5e-3))
    run_field("EVAL", "thickness: box w/ through-hole(~10)", box_with_hole(30, 10, 6),
              "eval_thickness", {}, lambda p, c: compare_field_physical(c, 10.0))
    run_field("EVAL", "draft pull+Z: hole-part", box_with_hole(30, 10, 6), "eval_draft",
              {"pull": [0, 0, 1]}, lambda p, c: compare_field_set(p, c, set_digits=0, set_tol=1.0))

    # =================================================================== C. ASSEMBLY
    print("\n=== C. ASSEMBLY (interference/clearance, mate combos, explode, patterns) ===")

    box20 = _write_box_step("box20", 20, 20, 20)
    box10 = _write_box_step("box10", 10, 10, 10)
    holeblk = _write_cyl_step("holeblk", 8, 20)
    pegcyl = _write_cyl_step("pegcyl", 4, 30)

    f_k1_xp = _face_selector([20, 10, 10], [1, 0, 0], 400)
    f_k2_xm = _face_selector([0, 10, 10], [-1, 0, 0], 400)

    # ---- C1. eval_interference on ASSEMBLY world bodies: overlap + clearance ----
    # Two fixed boxes that OVERLAP (k2 shifted +X by 10 => overlap 10x20x20).
    asm_overlap = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
    ]
    # Two fixed boxes with a 10mm GAP (k2 at x=30).
    asm_gap = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
    ]
    # Three components: k1@0, k2 overlaps k1 (+X 10), k3 gap from both (x=50).
    asm_three = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
        _insert(box10, "k3", transform=[1, 0, 0, 50, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
    ]
    for label, ops in [("asm interference: 2-comp OVERLAP", asm_overlap),
                       ("asm interference: 2-comp GAP", asm_gap),
                       ("asm interference: 3-comp mixed", asm_three)]:
        py = py_asm_session(); py.apply(0, ops)
        cpp = CppEvalS(CORE)  # eval_interference works on any doc_id; need assembly doc
        # build a C++ assembly doc and apply
        cppa = CppAsmS(CORE); cppa.apply(0, ops)
        try:
            pres = py.interference()
        except Exception as exc:  # noqa: BLE001
            T.check("ASM", label, [f"py raised {exc}"]); continue
        rc, cres = CORE.call("eval_interference", {"doc_id": cppa.doc_id})
        if rc != 0:
            T.check("ASM", label, [f"GAP: cpp eval_interference rc={rc} {str(cres)[:80]}"]); continue
        T.check("ASM", label, compare_interference(pres, cres))

    # ---- C2. mate combos beyond cpp_asm_parity: parallel, distance, angle, width, tangent ----
    # For an UNDER-constrained assembly (dof>0) the gauge freedom makes raw poses non-unique, so
    # the correct equivalence (per cpp_asm_parity) is: BOTH engines' poses null every active mate
    # residual. We use the oracle's own residual blocks. Fully-determined (dof==0) cases also get a
    # tight transform check. fixed (anchored) components must match regardless.
    from tools.cpp_asm_parity import mate_residuals

    def asm_mate_case(label, ops, fixed_ids=("k1",)):
        try:
            py = py_asm_session(); pres = py.apply(0, ops)
            cpp = CppAsmS(CORE); cres = cpp.apply(0, ops)
        except Exception as exc:  # noqa: BLE001
            T.check("ASM", label, [f"raised {type(exc).__name__}: {exc}"]); return None, None, None
        diffs = compare_asm_status(pres, cres)
        pt, ct = _comp_transforms(pres), _comp_transforms(cres)
        dof = pres["rebuild"].get("dof", 0)
        # anchored components must match tightly
        for cid in fixed_ids:
            if cid in pt and cid in ct and not _vec_close(pt[cid], ct[cid], 1e-4):
                diffs.append(f"{cid} (fixed) transform differs")
        if dof == 0:
            for cid in pt:
                if cid in ct and not _vec_close(pt[cid], ct[cid], 1e-4):
                    diffs.append(f"{cid} transform py={[round(x,4) for x in pt[cid]]} cpp={[round(x,4) for x in ct[cid]]}")
        else:
            # residual-invariant: both poses must satisfy every active mate.
            try:
                pr = mate_residuals(py, pt)
                crr = mate_residuals(py, ct)
            except Exception as exc:  # noqa: BLE001
                diffs.append(f"residual-invariant raised: {exc}")
                pr = crr = {}
            for mid, rv in pr.items():
                if rv > 1e-5:
                    diffs.append(f"PY pose violates {mid}: |r|={rv:.2e}")
            for mid, rv in crr.items():
                if rv > 1e-5:
                    diffs.append(f"CPP pose violates {mid}: |r|={rv:.2e}")
        T.check("ASM", label, diffs)
        return py, cpp, pres

    # parallel: k1 +X face parallel to k2 +X face (faces share normal, free to translate)
    asm_parallel = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[0, -1, 0, 30, 1, 0, 0, 5, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("parallel", [{"component": "k1", "selector": f_k1_xp},
                           {"component": "k2", "selector": _face_selector([20, 10, 10], [1, 0, 0], 400)}]),
    ]
    asm_mate_case("mate parallel (faces)", asm_parallel)

    # distance mate: k1 +X face to k2 -X face at 15mm
    asm_distance = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 50, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("distance", [{"component": "k1", "selector": f_k1_xp},
                           {"component": "k2", "selector": f_k2_xm}],
              value={"value": 15.0, "expr": None}),
    ]
    asm_mate_case("mate distance=15 (faces)", asm_distance)

    # angle mate: k1 +X face to k2 +X face at 45deg
    asm_angle = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 40, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("angle", [{"component": "k1", "selector": f_k1_xp},
                        {"component": "k2", "selector": _face_selector([20, 10, 10], [1, 0, 0], 400)}],
              value={"value": 45.0, "expr": None}),
    ]
    asm_mate_case("mate angle=45 (faces)", asm_angle)

    # tangent mate: peg cyl side tangent to a box face
    cyl_side = _cyl_face_selector([0, 0, 15], [0, 0, 1], 2 * math.pi * 4 * 30)
    asm_tangent = [
        _insert(box20, "k1", fixed=True),
        _insert(pegcyl, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 10, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("tangent", [{"component": "k1", "selector": f_k1_xp},
                          {"component": "k2", "selector": cyl_side}]),
    ]
    asm_mate_case("mate tangent (face<->cyl)", asm_tangent)

    # width mate: k2 centered between k1's two +/-X faces
    asm_width = [
        _insert(box20, "k1", fixed=True),
        _insert(box10, "k2", transform=[1, 0, 0, 5, 0, 1, 0, 5, 0, 0, 1, 5, 0, 0, 0, 1]),
        _mate("width", [{"component": "k1", "selector": f_k1_xp},
                        {"component": "k1", "selector": _face_selector([0, 10, 10], [-1, 0, 0], 400)},
                        {"component": "k2", "selector": _face_selector([10, 5, 5], [1, 0, 0], 100)},
                        {"component": "k2", "selector": _face_selector([0, 5, 5], [-1, 0, 0], 100)}]),
    ]
    asm_mate_case("mate width (4 faces)", asm_width)

    # 3-component chain: k1-k2 coincident, k2-k3 coincident
    asm_chain = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _insert(box20, "k3", transform=[1, 0, 0, 60, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
        _mate("coincident", [{"component": "k2", "selector": _face_selector([20, 10, 10], [1, 0, 0], 400)},
                             {"component": "k3", "selector": f_k2_xm}]),
    ]
    asm_mate_case("3-component coincident chain", asm_chain)

    # ---- C3. EXPLODE state: compare exploded transforms ----
    explode_ops = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
        {"op": "asm_set_explode", "steps": [{"component": "k2", "vector": [1, 0, 0], "distance": 40.0}]},
    ]
    py = py_asm_session(); py.apply(0, explode_ops)
    cpp = CppAsmS(CORE); cpp.apply(0, explode_ops)
    py.set_explode(True, 1.0)
    rc_ex, _ = cpp.set_explode(True, 1.0)
    exdiffs = []
    if rc_ex != 0:
        exdiffs.append(f"GAP: cpp doc_set_explode_state rc={rc_ex}")
    else:
        # exploded world bbox: k2 should be shifted +40 in X
        py_bb = asm_world_bbox_exploded(py)
        rc, wb = cpp.world_bodies_rpc(explode=True)
        if rc != 0:
            exdiffs.append(f"cpp asm_world_bodies(explode) rc={rc}")
        else:
            by_comp = {b["component"]: b for b in wb["bodies"]}
            for cid, (mn, mx) in py_bb.items():
                cb = by_comp.get(cid)
                if cb is None or cb.get("bbox") is None:
                    exdiffs.append(f"{cid} missing in cpp explode world bodies"); continue
                if not (_vec_close(mn, cb["bbox"]["min"], 1e-3) and _vec_close(mx, cb["bbox"]["max"], 1e-3)):
                    exdiffs.append(f"{cid} exploded bbox py={[round(v,2) for v in mn]}..{[round(v,2) for v in mx]} "
                                   f"cpp={[round(v,2) for v in cb['bbox']['min']]}..{[round(v,2) for v in cb['bbox']['max']]}")
    T.check("ASM", "explode state (factor=1.0)", exdiffs)

    # ---- C4. component pattern (linear + circular) ----
    # asm_linear_pattern of k2 (3 copies along +X spacing 30)
    asm_lin_pat = [
        _insert(box10, "k1", fixed=True),
        _insert(box10, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
        {"op": "add_feature", "feature": {"type": "asm_linear_pattern", "name": "LPat",
            "params": {"count1": {"value": 3, "expr": None}, "spacing1": {"value": 30, "expr": None}},
            "refs": {"dir1": "axis.x", "components": ["k2"]}}},
    ]
    asm_circ_pat = [
        _insert(box10, "k1", fixed=True),
        _insert(box10, "k2", transform=[1, 0, 0, 40, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
        {"op": "add_feature", "feature": {"type": "asm_circular_pattern", "name": "CPat",
            "params": {"count": {"value": 4, "expr": None}, "angle": {"value": 360, "expr": None}},
            "refs": {"axis": "axis.z", "components": ["k2"]}}},
    ]
    for label, ops in [("asm linear pattern (3x)", asm_lin_pat),
                       ("asm circular pattern (4x)", asm_circ_pat)]:
        try:
            py = py_asm_session(); pres = py.apply(0, ops)
            cpp = CppAsmS(CORE); cres = cpp.apply(0, ops)
        except Exception as exc:  # noqa: BLE001
            T.check("ASM", label, [f"raised {type(exc).__name__}: {exc}"]); continue
        pdiffs = []
        # body count parity (pattern instances appended to bodies)
        pn = len(pres["tess"]["bodies"]); cn = len(cres["tess"]["bodies"])
        if pn != cn:
            pdiffs.append(f"body count py={pn} cpp={cn}")
        # world bodies count via RPC
        rc, wb = cpp.core.call("asm_world_bodies", {"doc_id": cpp.doc_id})
        if rc != 0:
            pdiffs.append(f"asm_world_bodies rc={rc}")
        else:
            pw = len(py.world_bodies(explode=False))
            cw = len(wb["bodies"])
            if pw != cw:
                pdiffs.append(f"world body count py={pw} cpp={cw}")
        T.check("ASM", label, pdiffs)

    # ---- C5. over-defined assembly: redundant coincident pinning same pair twice ----
    asm_over = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
    ]
    asm_mate_case("over-defined assembly (dup mate)", asm_over)

    # ---- C6. asm_world_bodies output shape (components + bbox present) ----
    py = py_asm_session(); py.apply(0, asm_gap)
    cpp = CppAsmS(CORE); cpp.apply(0, asm_gap)
    rc, wb = cpp.core.call("asm_world_bodies", {"doc_id": cpp.doc_id})
    wdiffs = []
    if rc != 0:
        wdiffs.append(f"GAP: rc={rc}")
    else:
        pbb = asm_world_bbox(py)
        by_comp = {b["component"]: b for b in wb["bodies"]}
        if set(by_comp) != set(pbb):
            wdiffs.append(f"components py={sorted(pbb)} cpp={sorted(by_comp)}")
    T.check("ASM", "asm_world_bodies shape", wdiffs)

    # =================================================================== summary
    print(f"\n[audit2] {T.total - T.diff}/{T.total} MATCH ; {T.diff} divergence(s)/gap(s) found")
    return T.diff


def asm_world_bbox_exploded(py):
    out = {}
    for name, shape in py.world_bodies(explode=True):
        bb = shape.bounding_box()
        out[name] = ([bb.min.X, bb.min.Y, bb.min.Z], [bb.max.X, bb.max.Y, bb.max.Z])
    return out


if __name__ == "__main__":
    raise SystemExit(main())

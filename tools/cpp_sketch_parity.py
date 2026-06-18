"""cpp_sketch_parity.py — Python-vs-C++ interactive sketch-mode parity.

Gates the C18 stateful sketch-edit RPCs the C++ core now implements (libaicad.dll:
sketch_enter / sketch_solve / sketch_drag / sketch_drag_commit / sketch_exit /
doc_sketch_overview / doc_sketch_regions / sketch_probe) against the Python oracle
(engine/doc/session.py sketch_* on a live DocSession).

The C++ sketch SOLVER is a verified bit-port (tools/cpp_solver_diff.py), so the solved
coordinates must agree TIGHTLY (~1e-6). What this gate adds is parity of the per-session
LIFECYCLE wrapped around it: plane frame, the under/full/over `status` classification
(the SW-parity sensitivity — redundant geometric relations must NOT over-define, only
redundant driving dims do), conflicting/redundant sets, the drag soft-target landing,
the committed sketch params + post-rebuild revision/tree, the exit ops-response, and the
even-odd region enumeration.

For each fixture it drives the SAME doc op batch + sketch sequence on BOTH the Python
DocSession and the C++ session, and diffs the responses.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_sketch_parity.py --dll core\build-occt\bin\Release\libaicad.dll

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


# --------------------------------------------------------------------------- fixtures
# Each fixture is a SINGLE add_feature op building a sketch with explicit-id entities
# + constraints inline (the stored-sketch shape the host re-opens), so constraint
# handles reference real ids — placeholder "$N" ids only resolve for op-level id
# fields, not inside constraint refs/handles.

def _line(i, x1, y1, x2, y2):
    return {"id": i, "type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}


def _circle(i, cx, cy, r):
    return {"id": i, "type": "circle", "construction": False, "center": [cx, cy], "radius": r}


def _rel(i, t, refs):
    return {"id": i, "type": t, "refs": refs}


def _dim(i, t, refs, value, driven=False):
    return {"id": i, "type": t, "refs": refs,
            "value": {"value": value, "expr": None}, "driven": driven}


def _sketch_feature(entities, constraints):
    return [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Sketch1",
        "params": {"entities": entities, "constraints": constraints},
        "refs": {"plane": "plane.top"}}}]


def _rect_ents():
    return [_line("e1", 0, 0, 40, 0), _line("e2", 40, 0, 40, 30),
            _line("e3", 40, 30, 0, 30), _line("e4", 0, 30, 0, 0)]


def _rect_chain():
    return [_rel("c1", "coincident", ["e1.p2", "e2.p1"]),
            _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
            _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
            _rel("c4", "coincident", ["e4.p2", "e1.p1"]),
            _rel("c5", "horizontal", ["e1"]), _rel("c6", "horizontal", ["e3"]),
            _rel("c7", "vertical", ["e2"]), _rel("c8", "vertical", ["e4"]),
            _rel("c9", "fix", ["e1.p1"])]


def fully_defined_rect():
    """Rectangle with a width + height dimension, origin-fixed. Fully defined (dof 0)."""
    cons = _rect_chain() + [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                            _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0)]
    return _sketch_feature(_rect_ents(), cons)


def under_defined_rect():
    """Rectangle, origin-fixed, h/v relations but NO dims — under-defined (dof > 0)."""
    return _sketch_feature(_rect_ents(), _rect_chain())


def over_defined_rect():
    """Fully-defined rect plus a REDUNDANT DRIVING width dim — over-defined."""
    cons = _rect_chain() + [_dim("c10", "distance", ["e1.p1", "e1.p2"], 40.0),
                            _dim("c11", "distance", ["e2.p1", "e2.p2"], 30.0),
                            _dim("c12", "distance", ["e1.p1", "e1.p2"], 40.0)]
    return _sketch_feature(_rect_ents(), cons)


def circle_sketch():
    """A single circle dimensioned by radius, center-fixed. Fully defined."""
    return _sketch_feature(
        [_circle("e1", 0, 0, 20.0)],
        [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 20.0)])


def washer_sketch():
    """A square outer loop with a concentric circle hole — exercises even-odd regions."""
    ents = [_line("e1", -20, -20, 20, -20), _line("e2", 20, -20, 20, 20),
            _line("e3", 20, 20, -20, 20), _line("e4", -20, 20, -20, -20),
            _circle("e5", 0, 0, 8.0)]
    cons = [_rel("c1", "coincident", ["e1.p2", "e2.p1"]),
            _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
            _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
            _rel("c4", "coincident", ["e4.p2", "e1.p1"])]
    return _sketch_feature(ents, cons)


# --------------------------------------------------------------------------- engines
def py_session():
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

        def exit(self, fid):
            return session.sketch_exit(self.s, fid, None)

        def probe(self, fid, action):
            return session.sketch_probe(self.s, fid, action)

        def project(self, fid, ref):
            return session.sketch_project_ref(self.s, fid, ref)
    return PyS()


class CppS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]

    def _call(self, m, extra):
        rc, r = self.core.call(m, {"doc_id": self.doc_id, **extra})
        return rc, r

    def apply(self, base_rev, ops):
        rc, r = self._call("doc_apply_ops", {"base_revision": base_rev, "ops": ops})
        return r

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

    def exit(self, fid):
        return self._call("sketch_exit", {"sketch": fid})[1]

    def probe(self, fid, action):
        return self._call("sketch_probe", {"sketch": fid, "action": action})[1]

    def project(self, fid, ref):
        return self._call("sketch_project_ref", {"sketch": fid, "ref": ref})[1]


# --------------------------------------------------------------------------- helpers
def sketch_fid(apply_resp):
    """The id of the (only) sketch feature after applying a batch (from the tree)."""
    for f in apply_resp.get("tree", {}).get("features", []):
        if f.get("type") == "sketch":
            return f["id"]
    return None


def ent_map(resp):
    """entities list -> {id: {coord: val}} dropping id/construction/type."""
    out = {}
    for e in resp.get("entities", []):
        eid = e.get("id")
        rec = {k: v for k, v in e.items() if k not in ("id", "construction", "type")}
        out[eid] = rec
    return out


def coords_close(a, b, tol=1e-6):
    """Deep-compare two coord dicts (scalars + lists) within tol."""
    if set(a.keys()) != set(b.keys()):
        return False, f"keys {sorted(a.keys())} != {sorted(b.keys())}"
    for k in a:
        av, bv = a[k], b[k]
        if isinstance(av, list) and isinstance(bv, list):
            if len(av) != len(bv):
                return False, f"{k} len {len(av)}!={len(bv)}"
            for x, y in zip(av, bv):
                if abs(float(x) - float(y)) > tol:
                    return False, f"{k} {av}!={bv}"
        else:
            try:
                if abs(float(av) - float(bv)) > tol:
                    return False, f"{k} {av}!={bv}"
            except (TypeError, ValueError):
                if av != bv:
                    return False, f"{k} {av!r}!={bv!r}"
    return True, ""


def diff_entities(pr, cr, tol=1e-6):
    pm, cm = ent_map(pr), ent_map(cr)
    if set(pm.keys()) != set(cm.keys()):
        return [f"entity ids {sorted(pm.keys())} != {sorted(cm.keys())}"]
    diffs = []
    for eid in pm:
        ok, why = coords_close(pm[eid], cm[eid], tol)
        if not ok:
            diffs.append(f"ent {eid}: {why}")
    return diffs


def diff_solve_resp(label, pr, cr):
    """Diff a sketch_enter/sketch_solve response (entities, dof, status, sets, statuses)."""
    diffs = diff_entities(pr, cr)
    if int(pr.get("dof", -99)) != int(cr.get("dof", -98)):
        diffs.append(f"dof {pr.get('dof')}!={cr.get('dof')}")
    if pr.get("status") != cr.get("status"):
        diffs.append(f"status {pr.get('status')!r}!={cr.get('status')!r}")
    if pr.get("solve_rejected") != cr.get("solve_rejected"):
        diffs.append(f"solve_rejected {pr.get('solve_rejected')}!={cr.get('solve_rejected')}")
    if set(pr.get("conflicting", [])) != set(cr.get("conflicting", [])):
        diffs.append(f"conflicting {pr.get('conflicting')}!={cr.get('conflicting')}")
    if set(pr.get("redundant", [])) != set(cr.get("redundant", [])):
        diffs.append(f"redundant {pr.get('redundant')}!={cr.get('redundant')}")
    # per-entity statuses
    ps, cs = pr.get("statuses", {}), cr.get("statuses", {})
    if ps != cs:
        diffs.append(f"statuses {ps}!={cs}")
    return diffs


def diff_plane_frame(pr, cr, tol=1e-9):
    pf, cf = pr.get("plane_frame", []), cr.get("plane_frame", [])
    if len(pf) != len(cf):
        return [f"plane_frame len {len(pf)}!={len(cf)}"]
    for i, (a, b) in enumerate(zip(pf, cf)):
        if abs(float(a) - float(b)) > tol:
            return [f"plane_frame[{i}] {a}!={b}"]
    return []


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[sketch-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[sketch-parity] libaicad ABI={core.abi()}")

    mism = 0
    total = 0

    def check(label, diffs):
        nonlocal mism, total
        total += 1
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(str(d) for d in diffs)
        print(f"  {label:<42} {verdict}")
        mism += 0 if not diffs else 1

    fixtures = [
        ("rect (full)", fully_defined_rect, "full"),
        ("rect (under)", under_defined_rect, "under"),
        ("rect (over)", over_defined_rect, "over"),
        ("circle (full)", circle_sketch, "full"),
    ]

    # ---------------------------------------------------------------- enter/solve/status
    for label, mk, expect_status in fixtures:
        ops = mk()
        py = py_session()
        pr_apply = py.apply(0, ops)
        cpp = CppS(core)
        cr_apply = cpp.apply(0, ops)

        pfid = sketch_fid(pr_apply)
        cfid = sketch_fid(cr_apply)
        if pfid != cfid:
            check(f"{label} sketch-fid", [f"py fid {pfid} != cpp {cfid}"])
            continue

        pe, ce = py.enter(pfid), cpp.enter(cfid)
        d = diff_solve_resp(label, pe, ce) + diff_plane_frame(pe, ce)
        # The expected classification must hold on BOTH (oracle is ground truth here).
        if pe.get("status") != expect_status:
            d.append(f"ORACLE status {pe.get('status')!r} != expected {expect_status!r}")
        check(f"{label} enter", d)

        # warm re-solve
        ps, cs = py.solve(pfid), cpp.solve(cfid)
        check(f"{label} solve(warm)", diff_solve_resp(label, ps, cs) + diff_plane_frame(ps, cs))

        # overview (read-only)
        po, co = py.overview(pfid), cpp.overview(cfid)
        od = diff_entities(po, co)
        if po.get("status") != co.get("status"):
            od.append(f"status {po.get('status')!r}!={co.get('status')!r}")
        if int(po.get("dof", -1)) != int(co.get("dof", -2)):
            od.append(f"dof {po.get('dof')}!={co.get('dof')}")
        check(f"{label} overview", od + diff_plane_frame(po, co))

    # ---------------------------------------------------------------- drag + commit + exit
    # Drag a free corner of the UNDER-defined rect and check it lands near the target,
    # both sides agree, then commit + exit and compare the ops-response shape.
    ops = under_defined_rect()
    py = py_session()
    pr_apply = py.apply(0, ops)
    cpp = CppS(core)
    cr_apply = cpp.apply(0, ops)
    pfid = sketch_fid(pr_apply)

    py.enter(pfid)
    cpp.enter(pfid)
    # Handle: the far corner of the rectangle (e2.p1 / e1.p2 shared corner). Use the
    # second line's start point handle "<e1>.p2" — recover the entity ids from the tree.
    ent_ids = [f["id"] for f in pr_apply["tree"]["features"]
               for f in [f] if f.get("type") == "sketch"]
    # entity ids inside the sketch params:
    sk_feat = next(f for f in pr_apply["tree"]["features"] if f["id"] == pfid)
    line_ids = [e["id"] for e in sk_feat["params"]["entities"] if e["type"] == "line"]
    # Drag the shared corner between line0.p2 and line1.p1 to a new location.
    handle = f"{line_ids[0]}.p2"
    target = [50.0, 5.0]
    pd = py.drag(pfid, handle, target)
    cd = cpp.drag(pfid, handle, target)
    dd = diff_entities(pd, cd)
    # The dragged handle should land near the target on both (soft target, so ~within a mm).
    def handle_xy(resp, h):
        eid, pt = h.split(".")
        for e in resp.get("entities", []):
            if e.get("id") == eid and pt in e:
                return e[pt]
        return None
    pxy, cxy = handle_xy(pd, handle), handle_xy(cd, handle)
    if pxy and cxy and any(abs(a - b) > 1e-6 for a, b in zip(pxy, cxy)):
        dd.append(f"drag handle py={pxy} cpp={cxy}")
    check("under drag", dd)

    # commit -> ops-response (revision + tree + committed params)
    pc, cc = py.drag_commit(pfid), cpp.drag_commit(pfid)
    cdiffs = []
    if pc.get("revision") != cc.get("revision"):
        cdiffs.append(f"revision {pc.get('revision')}!={cc.get('revision')}")
    # committed entity params in the tree match
    pt_feat = next(f for f in pc["tree"]["features"] if f["id"] == pfid)
    ct_feat = next(f for f in cc["tree"]["features"] if f["id"] == pfid)
    for pe2, ce2 in zip(pt_feat["params"]["entities"], ct_feat["params"]["entities"]):
        ok, why = coords_close({k: v for k, v in pe2.items() if k not in ("id", "type", "construction")},
                               {k: v for k, v in ce2.items() if k not in ("id", "type", "construction")})
        if not ok:
            cdiffs.append(f"committed {pe2.get('id')}: {why}")
    # doc_status.active_sketch parity
    pas = pc.get("rebuild", {}).get("doc_status", {}).get("active_sketch")
    cas = cc.get("rebuild", {}).get("doc_status", {}).get("active_sketch")
    if pas != cas:
        cdiffs.append(f"doc_status.active_sketch {pas!r}!={cas!r}")
    check("under drag_commit", cdiffs)

    # exit -> ops-response shape (revision present, active_sketch cleared to null)
    px, cx = py.exit(pfid), cpp.exit(pfid)
    xdiffs = []
    for key in ("revision", "id_map"):
        if (key in px) != (key in cx):
            xdiffs.append(f"key {key} present py={key in px} cpp={key in cx}")
    pxas = px.get("rebuild", {}).get("doc_status", {}).get("active_sketch")
    cxas = cx.get("rebuild", {}).get("doc_status", {}).get("active_sketch")
    if pxas != cxas:
        xdiffs.append(f"exit active_sketch {pxas!r}!={cxas!r}")
    check("under exit", xdiffs)

    # ---------------------------------------------------------------- regions
    for label, mk in [("circle", circle_sketch), ("washer", washer_sketch),
                      ("rect", fully_defined_rect)]:
        ops = mk()
        py = py_session()
        pr_apply = py.apply(0, ops)
        cpp = CppS(core)
        cr_apply = cpp.apply(0, ops)
        pfid = sketch_fid(pr_apply)

        prg, crg = py.regions(pfid), cpp.regions(pfid)
        rdiffs = diff_plane_frame(prg, crg)
        preg = prg.get("regions", [])
        creg = crg.get("regions", [])
        if len(preg) != len(creg):
            rdiffs.append(f"region count {len(preg)}!={len(creg)}")
        else:
            for pr_, cr_ in zip(preg, creg):
                if pr_.get("index") != cr_.get("index"):
                    rdiffs.append(f"index {pr_.get('index')}!={cr_.get('index')}")
                if bool(pr_.get("is_material")) != bool(cr_.get("is_material")):
                    rdiffs.append(f"is_material[{pr_.get('index')}] "
                                  f"{pr_.get('is_material')}!={cr_.get('is_material')}")
                pc2, cc2 = pr_.get("centroid", []), cr_.get("centroid", [])
                if len(pc2) == 2 == len(cc2) and any(abs(a - b) > 1e-3
                                                     for a, b in zip(pc2, cc2)):
                    rdiffs.append(f"centroid[{pr_.get('index')}] {pc2}!={cc2}")
                pa, ca = float(pr_.get("area", 0)), float(cr_.get("area", 0))
                if abs(pa - ca) > max(1e-3, 1e-3 * abs(pa)):
                    rdiffs.append(f"area[{pr_.get('index')}] {pa:.4f}!={ca:.4f}")
        check(f"{label} regions", rdiffs)

    # ---------------------------------------------------------------- probe (over-define pre-flight)
    # On the fully-defined rect, adding ANOTHER driving width dim should over-define.
    ops = fully_defined_rect()
    py = py_session()
    pr_apply = py.apply(0, ops)
    cpp = CppS(core)
    cr_apply = cpp.apply(0, ops)
    pfid = sketch_fid(pr_apply)
    py.enter(pfid)
    cpp.enter(pfid)
    sk_feat = next(f for f in pr_apply["tree"]["features"] if f["id"] == pfid)
    line_ids = [e["id"] for e in sk_feat["params"]["entities"] if e["type"] == "line"]
    probe_actions = [
        ("add redundant dim", {"add_constraint": {
            "type": "distance", "refs": [f"{line_ids[0]}.p1", f"{line_ids[0]}.p2"],
            "value": {"value": 40.0, "expr": None}, "driven": False}}),
        ("add geometric rel", {"add_constraint": {
            "type": "horizontal", "refs": [line_ids[0]]}}),
    ]
    for plabel, action in probe_actions:
        pp, cp = py.probe(pfid, action), cpp.probe(pfid, action)
        pdiffs = []
        for key in ("ok", "status", "dof_before", "dof_after", "converged", "would_over_define"):
            if pp.get(key) != cp.get(key):
                pdiffs.append(f"{key} {pp.get(key)!r}!={cp.get(key)!r}")
        if set(pp.get("conflicting", [])) != set(cp.get("conflicting", [])):
            pdiffs.append(f"conflicting {pp.get('conflicting')}!={cp.get('conflicting')}")
        if set(pp.get("redundant", [])) != set(cp.get("redundant", [])):
            pdiffs.append(f"redundant {pp.get('redundant')}!={cp.get('redundant')}")
        check(f"probe {plabel}", pdiffs)

    # ---------------------------------------------------------------- project_ref
    # Datum-plane projection: project FRONT (XZ, normal -Y) into a TOP-plane sketch ->
    # the intersection is the world X axis, i.e. the sketch-local v=0 line.
    ops = under_defined_rect()
    py = py_session()
    pr_apply = py.apply(0, ops)
    cpp = CppS(core)
    cr_apply = cpp.apply(0, ops)
    pfid = sketch_fid(pr_apply)

    def diff_entity_obj(pe, ce, tol=1e-6):
        diffs = []
        if pe.get("type") != ce.get("type"):
            return [f"type {pe.get('type')!r}!={ce.get('type')!r}"]
        for key in ("p1", "p2", "p", "center"):
            if key in pe or key in ce:
                pv, cv = pe.get(key), ce.get(key)
                if pv is None or cv is None:
                    diffs.append(f"{key} present py={pv is not None} cpp={cv is not None}")
                elif any(abs(a - b) > tol for a, b in zip(pv, cv)):
                    diffs.append(f"{key} {pv}!={cv}")
        if "radius" in pe or "radius" in ce:
            if abs(float(pe.get("radius", 0)) - float(ce.get("radius", 0))) > tol:
                diffs.append(f"radius {pe.get('radius')}!={ce.get('radius')}")
        return diffs

    for ref_label, ref in [("datum front", {"datum": "front"}),
                          ("datum right", {"datum": "right"})]:
        try:
            pp = py.project(pfid, ref)
        except Exception as exc:  # noqa: BLE001
            check(f"project {ref_label}", [f"py raised {exc}"])
            continue
        cp = cpp.project(pfid, ref)
        if "entity" not in cp:
            check(f"project {ref_label}", [f"cpp no entity: {str(cp)[:80]}"])
            continue
        check(f"project {ref_label}", diff_entity_obj(pp["entity"], cp["entity"]))

    print(f"\n[sketch-parity] {total - mism}/{total} checks at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

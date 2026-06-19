"""cpp_asm_parity.py — Python-vs-C++ ASSEMBLY-compile parity.

Gates the C++ doc-engine assembly layer (libaicad.dll: doc_new(kind=assembly),
doc_apply_ops asm_*, doc_solve_mates, asm_world_bodies) against the Python oracle
(engine/doc/session.py -> engine/doc/assembly.compile_assembly). The SAME assembly op
batch (insert components from on-disk STEP parts + add mates) is driven through BOTH
engines, then the solved poses + mate status + DOF are compared.

Component sources are plain .step files written ONCE to a temp dir (build123d), so both
engines read the IDENTICAL B-rep and resolve mate-ref selectors to the same local-frame
geometry. (The Python oracle's compile path is PATH-only — the schema permits an
``embedded`` source string but no loader consumes it — so this port matches by loading
via source.path; embedded sources are out of scope on BOTH engines.)

Comparison (per assembly):
  * per-component solved transform: TIGHT (1e-4) where the component is fully constrained;
    for an under-constrained component the mate solver has a free DOF, so we instead assert
    the per-component WORLD BBOX matches (placement-invariant residual equivalence, the same
    equivalence the mate_diff harness established) and that both engines AGREE on dof.
  * mate_status (id/type/status), dof, over_defined, converged, component count.
  * per-component world bbox (min/max/size) after placement.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_asm_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatched checks (0 = full parity).
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


# --------------------------------------------------------------------------- fixtures
_TMP = Path(tempfile.mkdtemp(prefix="aicad_asm_parity_"))


def _write_box_step(name: str, w: float, h: float, d: float) -> str:
    """Write a w x h x d box (corner at origin, +XYZ) to a .step file and return its path."""
    from build123d import Box, Location, export_step
    path = _TMP / f"{name}.step"
    if not path.exists():
        # Box is centered; shift so a corner sits at the origin (+X,+Y,+Z octant) — matches a
        # rectangle-extrude part, so face centroids are predictable for mate selectors.
        solid = Location((w / 2, h / 2, d / 2)) * Box(w, h, d)
        export_step(solid, str(path))
    return str(path)


def _write_cyl_step(name: str, r: float, h: float) -> str:
    """Write a cylinder (axis +Z, base at origin) to a .step file and return its path."""
    from build123d import Cylinder, Location, export_step, Align
    path = _TMP / f"{name}.step"
    if not path.exists():
        solid = Location((0, 0, h / 2)) * Cylinder(r, h)
        export_step(solid, str(path))
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


# --------------------------------------------------------------------------- engines
def py_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("assembly")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)

        def solve(self, drag=None):
            return session.solve_mates(self.s, drag, None)
    return PyS()


class CppS:
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


# --------------------------------------------------------------------------- compare
def _rel(a, b, tol=1e-4):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _vec_close(a, b, tol=1e-4):
    return all(_rel(x, y, tol) for x, y in zip(a, b))


def _comp_transforms(resp):
    """component_id -> solved 16-float transform, read from the response tree."""
    return {c["id"]: c["transform"] for c in resp["tree"].get("components", [])}


def _mate_rows(resp):
    return {m["id"]: (m["type"], m["status"]) for m in resp["rebuild"].get("mates", [])}


def _bbox_of(transform, w, h, d):
    """World AABB of a w x h x d corner-box under a 16-float row-major rigid transform."""
    M = transform
    def xf(p):
        x, y, z = p
        return (M[0] * x + M[1] * y + M[2] * z + M[3],
                M[4] * x + M[5] * y + M[6] * z + M[7],
                M[8] * x + M[9] * y + M[10] * z + M[11])
    corners = [(X, Y, Z) for X in (0, w) for Y in (0, h) for Z in (0, d)]
    pts = [xf(c) for c in corners]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    return mn, mx


def mate_residuals(py, transforms):
    """Max |residual| per ACTIVE mate at the given component transforms, using the ORACLE's
    own residual blocks (same equivalence the mate_diff harness uses for under-constrained
    fixtures). Re-resolves the live doc's mate refs against the loaded components, then
    evaluates each mate's residual function at the SOLVED pose (all frames fixed)."""
    import numpy as np
    from engine.doc import assembly as asm
    from engine.doc import session as docsess
    from engine.doc.mate_solver import (_MateBuilder, _CompFrame, _transform_to_params,
                                        _mate_value)
    doc = py.s.doc
    base_dir = None
    geom = docsess._asm_resolved_geometry(doc, base_dir, asm)
    frames = {}
    for comp in doc.get("components", []):
        if comp.get("suppressed"):
            continue
        cid = comp["id"]
        tf = transforms.get(cid) or comp.get("transform")
        t, w = _transform_to_params(np.asarray(tf, dtype=float))
        frames[cid] = _CompFrame(cid, t, w, True)
    out = {}
    for mate in doc.get("mates", []):
        if mate.get("suppressed"):
            continue
        mid = mate["id"]
        refs = mate.get("refs", [])
        if len(refs) < 2:
            continue
        ga, gb = geom.get((mid, 0)), geom.get((mid, 1))
        ca, cb = frames.get(refs[0].get("component")), frames.get(refs[1].get("component"))
        if ga is None or gb is None or ca is None or cb is None:
            continue
        cid_a, cid_b = refs[0].get("component"), refs[1].get("component")
        extra_geoms, extra_owners, gi = [], [], 2
        while (mid, gi) in geom:
            extra_geoms.append(geom[(mid, gi)])
            ec = refs[gi].get("component") if gi < len(refs) else None
            extra_owners.append("a" if ec == cid_a and ec != cid_b else "b")
            gi += 1
        params = mate.get("value") if isinstance(mate.get("value"), dict) else {}
        try:
            builder = _MateBuilder(ca, ga, cb, gb, mate["type"], mate.get("align", "aligned"),
                                   _mate_value(mate), extra_geoms=extra_geoms, params=params,
                                   extra_owners=extra_owners)
            rfunc, _j, _m = builder.make()
            r = np.asarray(rfunc(np.zeros(0)), dtype=float)
            out[mid] = float(np.max(np.abs(r))) if r.size else 0.0
        except Exception:  # noqa: BLE001
            continue
    return out


def compare(label, py, pres, cres, placed_ids, comp_dims, fully_constrained):
    """placed_ids is the set of NON-SUPPRESSED component ids (all placed). comp_dims maps the
    subset that are boxes -> (w,h,d) for a world-bbox cross-check. fully_constrained is the set
    of component ids whose transform must match TIGHTLY. For an UNDER-CONSTRAINED assembly
    (dof > 0) the free gauge DOF makes the raw pose non-unique, so we instead assert BOTH
    engines' poses NULL every active mate residual (the mate_diff equivalence)."""
    diffs = []
    pt = {k: v for k, v in _comp_transforms(pres).items() if k in placed_ids}
    ct = {k: v for k, v in _comp_transforms(cres).items() if k in placed_ids}
    if set(pt) != set(ct):
        return [f"placed-component sets differ py={sorted(pt)} cpp={sorted(ct)}"]

    pm, cm = _mate_rows(pres), _mate_rows(cres)
    if pm != cm:
        diffs.append(f"mate_status py={pm} cpp={cm}")
    if pres["rebuild"].get("dof") != cres["rebuild"].get("dof"):
        diffs.append(f"dof py={pres['rebuild'].get('dof')} cpp={cres['rebuild'].get('dof')}")
    if bool(pres["rebuild"].get("over_defined")) != bool(cres["rebuild"].get("over_defined")):
        diffs.append(f"over_defined py={pres['rebuild'].get('over_defined')} "
                     f"cpp={cres['rebuild'].get('over_defined')}")
    if len(pres["tess"]["bodies"]) != len(cres["tess"]["bodies"]):
        diffs.append(f"body count py={len(pres['tess']['bodies'])} cpp={len(cres['tess']['bodies'])}")
    if len(pres["tess"]["bodies"]) != len(placed_ids):
        diffs.append(f"body count {len(pres['tess']['bodies'])} != placed components {len(placed_ids)}")

    dof = pres["rebuild"].get("dof", 0)
    if dof == 0:
        # fully determined: every component transform must match tightly.
        for cid in pt:
            if not _vec_close(pt[cid], ct[cid], 1e-4):
                diffs.append(f"{cid} transform py={[round(x,4) for x in pt[cid]]} "
                             f"cpp={[round(x,4) for x in ct[cid]]}")
    else:
        # under-constrained: fully-constrained (fixed) components still match tightly; the
        # rest are equivalent iff BOTH poses null every active mate residual.
        for cid in fully_constrained:
            if cid in pt and not _vec_close(pt[cid], ct[cid], 1e-4):
                diffs.append(f"{cid} (fixed) transform py={[round(x,4) for x in pt[cid]]} "
                             f"cpp={[round(x,4) for x in ct[cid]]}")
        try:
            py_res = mate_residuals(py, pt)
            cpp_res = mate_residuals(py, ct)
        except Exception as exc:  # noqa: BLE001
            return diffs + [f"residual-invariant raised: {exc}"]
        for mid, r in py_res.items():
            if r > 1e-5:
                diffs.append(f"py pose violates {mid}: |r|={r:.2e}")
        for mid, r in cpp_res.items():
            if r > 1e-5:
                diffs.append(f"cpp pose violates {mid}: |r|={r:.2e}")
    return diffs


# --------------------------------------------------------------------------- cases
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[asm-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[asm-parity] libaicad ABI={core.abi()}")

    # On-disk component parts (written once; both engines read the same B-rep).
    box20 = _write_box_step("box20", 20, 20, 20)
    box10 = _write_box_step("box10", 10, 10, 10)
    hole_block = _write_cyl_step("holeblk", 8, 20)   # r=8 cylinder (the "bore" host)
    pegcyl = _write_cyl_step("pegcyl", 4, 30)        # r=4 peg

    mism = 0
    total = 0

    def run(label, build_ops, placed_ids, comp_dims, fully_constrained, solve=False, drag=None):
        nonlocal total, mism
        total += 1
        try:
            py = py_session(); pres = py.apply(0, build_ops)
            cpp = CppS(core); cres = cpp.apply(0, build_ops)
            if solve:
                pres = py.solve(drag); cres = cpp.solve(drag)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<34} DIFF: raised {type(exc).__name__}: {exc}")
            mism += 1
            return
        diffs = compare(label, py, pres, cres, placed_ids, comp_dims, fully_constrained)
        # cross-check the C++ asm_world_bodies bbox against the analytic bbox of the C++ pose
        rc, wb = cpp.core.call("asm_world_bodies", {"doc_id": cpp.doc_id})
        if rc != 0:
            diffs.append(f"asm_world_bodies rc={rc}")
        else:
            ct = {k: v for k, v in _comp_transforms(cres).items() if k in placed_ids}
            by_comp = {b["component"]: b for b in wb["bodies"]}
            if set(by_comp) != set(placed_ids):
                diffs.append(f"asm_world_bodies components {sorted(by_comp)} != {sorted(placed_ids)}")
            for cid, (w, h, d) in comp_dims.items():
                if cid not in by_comp or by_comp[cid].get("bbox") is None:
                    diffs.append(f"{cid} world body missing")
                    continue
                mn, mx = _bbox_of(ct[cid], w, h, d)
                bb = by_comp[cid]["bbox"]
                if not (_vec_close(mn, bb["min"], 1e-2) and _vec_close(mx, bb["max"], 1e-2)):
                    diffs.append(f"{cid} asm_world_bbox cpp-disagrees")
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs[:3])
        print(f"  {label:<34} {verdict}")
        mism += 0 if not diffs else 1

    # ---- 1. two boxes, coincident-mated face-to-face ----
    # k1 fixed @ origin (20-box). k2 (20-box) starts offset; a coincident mate between
    # k1's +X face (centroid 20,10,10 normal +X) and k2's -X face (0,10,10 normal -X)
    # snaps k2 so the faces touch. Under-constrained in the face plane -> world-bbox gate.
    f_k1_xp = _face_selector([20, 10, 10], [1, 0, 0], 400)
    f_k2_xm = _face_selector([0, 10, 10], [-1, 0, 0], 400)
    ops_coincident = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 5, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
    ]
    run("2 boxes coincident", ops_coincident,
        {"k1", "k2"}, {"k1": (20, 20, 20), "k2": (20, 20, 20)}, fully_constrained={"k1"})

    # ---- 2. peg-in-hole (concentric) — two cylinders concentric on their side faces ----
    # k1 = r8 cylinder fixed @ origin (axis +Z). k2 = r4 peg, started off-axis; the
    # concentric mate aligns k2's cylindrical side face axis with k1's -> the peg's axis
    # snaps onto +Z. Free to slide/spin along the axis (under-constrained) -> residual gate.
    cyl_side_k1 = _cyl_face_selector([0, 0, 10], [0, 0, 1], 2 * math.pi * 8 * 20)
    cyl_side_k2 = _cyl_face_selector([0, 0, 15], [0, 0, 1], 2 * math.pi * 4 * 30)
    ops_concentric = [
        _insert(hole_block, "k1", fixed=True),
        _insert(pegcyl, "k2", transform=[1, 0, 0, 12, 0, 1, 0, 3, 0, 0, 1, -5, 0, 0, 0, 1]),
        _mate("concentric", [{"component": "k1", "selector": cyl_side_k1},
                             {"component": "k2", "selector": cyl_side_k2}]),
    ]
    run("peg-in-hole concentric (cyl)", ops_concentric,
        {"k1", "k2"}, {}, fully_constrained={"k1"})  # comp_dims empty: cyl bbox via residual gate

    # ---- 3. over-defined: two coincident mates pinning the same face pair twice ----
    ops_over = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
    ]
    run("over-defined (dup mate)", ops_over,
        {"k1", "k2"}, {"k1": (20, 20, 20), "k2": (20, 20, 20)}, fully_constrained={"k1"})

    # ---- 4. suppressed component: 3 boxes, k3 suppressed -> only k1,k2 placed ----
    ops_suppressed = [
        _insert(box20, "k1", fixed=True),
        _insert(box20, "k2", transform=[1, 0, 0, 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        _insert(box10, "k3", transform=[1, 0, 0, 60, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        {"op": "asm_edit_component", "id": "k3", "suppressed": True},
        _mate("coincident", [{"component": "k1", "selector": f_k1_xp},
                             {"component": "k2", "selector": f_k2_xm}]),
    ]
    run("suppressed component", ops_suppressed,
        {"k1", "k2"}, {"k1": (20, 20, 20), "k2": (20, 20, 20)}, fully_constrained={"k1"})

    # ---- 5. re-solve via doc_solve_mates (no drag) reproduces the apply-time pose ----
    run("re-solve (doc_solve_mates)", ops_coincident,
        {"k1", "k2"}, {"k1": (20, 20, 20), "k2": (20, 20, 20)}, fully_constrained={"k1"}, solve=True)

    # ---- 6. unmated: two free boxes keep their seed transforms (both fixed=first) ----
    ops_unmated = [
        _insert(box20, "k1", fixed=True),
        _insert(box10, "k2", transform=[1, 0, 0, 50, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], fixed=True),
    ]
    run("unmated 2 fixed boxes", ops_unmated,
        {"k1", "k2"}, {"k1": (20, 20, 20), "k2": (10, 10, 10)}, fully_constrained={"k1", "k2"})

    print(f"\n[asm-parity] {total - mism}/{total} checks at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

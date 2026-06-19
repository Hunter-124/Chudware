"""cpp_derived_parity.py — Python-vs-C++ COPY/PASTE-AS-DERIVED parity.

Companion to cpp_session_parity.py / cpp_eval_parity.py. It drives the SAME copy/paste-as-
derived flow through the Python document session (engine.doc.session) and the C++ session
(libaicad.dll: doc_apply_ops with copy_feature / paste_feature_as_derived, plus the new
doc_refresh_derived RPC) and compares the results: the copy BUNDLE, the read-only fast-path
(no revision bump on a pure copy), the pasted derived tree + volume, and the live re-pull
after the source changes / a source feature is deleted.

The realistic host flow is cross-document: a SOURCE part holds f1 = rect sketch, f2 = blind
extrude; copy_feature packages f2 (+ its closure) into a portable bundle; paste_feature_as_-
derived appends it into a TARGET part stamping a derived_from link; doc_refresh_derived later
re-pulls the live source definition so a source-side edit propagates (SolidWorks derived part).

Both engines mint feature ids deterministically (f1, f2, ...), so a fresh target's ids match;
the only engine-specific stamp fields are the source doc id (each engine has its own id space)
and the random derived-group id — both are normalized out before comparison.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_derived_parity.py

Exit code = number of mismatched steps (0 = full parity).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- fixtures
def _dim(value, expr=None):
    return {"value": value, "expr": expr}


def _rect_sketch_feature(width=20.0, height=20.0):
    """add_feature.feature (no id) for a closed 4-line rectangle with an INTERNAL coincident
    (last line's end -> first line's start), exactly the engine.doc.test_copy_paste fixture."""
    ents = [
        {"id": "e1", "type": "line", "construction": False, "p1": [0, 0], "p2": [width, 0]},
        {"id": "e2", "type": "line", "construction": False, "p1": [width, 0], "p2": [width, height]},
        {"id": "e3", "type": "line", "construction": False, "p1": [width, height], "p2": [0, height]},
        {"id": "e4", "type": "line", "construction": False, "p1": [0, height], "p2": [0, 0]},
    ]
    cons = [{"id": "c1", "type": "coincident", "refs": ["e4.p2", "e1.p1"]}]
    return {"type": "sketch", "name": "Sketch1",
            "params": {"entities": ents, "constraints": cons}, "refs": {"plane": "plane.top"}}


def _extrude_feature(sketch_ref, depth=10.0):
    # merge=False so a pasted copy at the same coordinates is a SECOND body (not a boolean union).
    return {"type": "extrude", "name": "Boss1",
            "params": {"mode": "boss", "end": "blind", "depth": _dim(depth), "flip": False,
                       "draft": _dim(0.0), "draft_outward": False, "merge": False},
            "refs": {"profile": [{"kind": "region", "sketch": sketch_ref, "centroid": [10.0, 10.0],
                                  "hint_index": 0}]}}


def _source_ops(depth=10.0):
    """One batch: f1 = rect sketch ($0), f2 = blind extrude profiling that sketch."""
    return [
        {"op": "add_feature", "feature": _rect_sketch_feature()},
        {"op": "add_feature", "feature": _extrude_feature("$0", depth)},
    ]


def _box_op():
    return {"op": "add_feature", "feature": {"type": "box", "name": "Box1",
            "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}}}


# --------------------------------------------------------------------------- engines
class PyEng:
    """Thin wrapper over engine.doc.session so the driver calls the same shape on both sides."""

    def __init__(self):
        from engine.doc import session as P
        self.P = P

    def new(self, name):
        return self.P.new_session("part", name)

    def apply(self, s, ops):
        return self.P.apply_ops(s, None, ops, None)

    def copy(self, s, fid):
        return self.apply(s, [{"op": "copy_feature", "id": fid}])

    def paste_derived(self, s, bundle, source_doc_id):
        return self.apply(s, [{"op": "paste_feature_as_derived", "bundle": bundle,
                               "source_doc_id": source_doc_id}])

    def refresh(self, s, fid=None):
        return self.P.refresh_derived(s, id=fid)

    def close(self, s):
        return self.P.close_session(s.doc_id)

    def revision(self, s):
        return s.revision

    def vol(self, resp):
        from engine.doc.compiler import compile_document
        r = compile_document(resp["tree"])
        v = 0.0
        for b in r.bodies:
            try:
                v += float(b.shape.volume)
            except Exception:  # noqa: BLE001
                try:
                    v += float(sum(float(x.volume) for x in b.shape))
                except Exception:  # noqa: BLE001
                    pass
        return v


class CppEng:
    def __init__(self, core):
        self.core = core

    def new(self, name):
        rc, r = self.core.call("doc_new", {"kind": "part", "name": name})
        return {"doc_id": r["doc_id"], "revision": r.get("revision", 0)}

    def apply(self, s, ops):
        rc, r = self.core.call("doc_apply_ops", {"doc_id": s["doc_id"], "ops": ops})
        s["revision"] = r.get("revision", s["revision"])
        return r

    def copy(self, s, fid):
        return self.apply(s, [{"op": "copy_feature", "id": fid}])

    def paste_derived(self, s, bundle, source_doc_id):
        return self.apply(s, [{"op": "paste_feature_as_derived", "bundle": bundle,
                               "source_doc_id": source_doc_id}])

    def refresh(self, s, fid=None):
        params = {"doc_id": s["doc_id"]}
        if fid:
            params["id"] = fid
        rc, r = self.core.call("doc_refresh_derived", params)
        if isinstance(r, dict) and "revision" in r:
            s["revision"] = r["revision"]
        return r

    def refresh_raw(self, s, fid=None):
        params = {"doc_id": s["doc_id"]}
        if fid:
            params["id"] = fid
        return self.core.call("doc_refresh_derived", params)

    def close(self, s):
        rc, r = self.core.call("doc_close", {"doc_id": s["doc_id"]})
        return r

    def revision(self, s):
        return s["revision"]

    def vol(self, resp):
        return float(resp.get("volume", 0.0))


# --------------------------------------------------------------------------- normalization
def norm_bundle(b):
    """Strip the engine-specific source doc id (each engine has its own id space)."""
    if not isinstance(b, dict):
        return b
    out = dict(b)
    out["source_doc_id"] = "<DOC>"
    return out


def derived_features(tree):
    return [f for f in tree.get("features", []) if isinstance(f, dict) and f.get("derived_from")]


def norm_derived(tree):
    """Project the derived stamp onto engine-independent fields: drop the doc id + group id
    (both engine-specific), keep doc_name + the source_feature link + type/params we compare."""
    out = []
    for f in tree.get("features", []):
        if not isinstance(f, dict):
            continue
        df = f.get("derived_from")
        if not df:
            continue
        out.append({
            "id": f.get("id"),
            "type": f.get("type"),
            "source_feature": df.get("source_feature"),
            "doc_name": df.get("doc_name"),
            "stale": (f.get("derived_stale") or {}).get("reason"),
        })
    return out


def feat_ids(tree):
    return [f.get("id") for f in tree.get("features", []) if isinstance(f, dict)]


def depth_of(tree, fid):
    for f in tree.get("features", []):
        if isinstance(f, dict) and f.get("id") == fid:
            return f.get("params", {}).get("depth", {}).get("value")
    return None


# --------------------------------------------------------------------------- driver
def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[derived-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[derived-parity] libaicad ABI={core.abi()}")

    py, cpp = PyEng(), CppEng(core)
    mism = 0
    total = 0

    def check(label, ok, detail=""):
        nonlocal mism, total
        total += 1
        if not ok:
            mism += 1
        print(f"  {label:<34} {'MATCH' if ok else 'DIFF: ' + str(detail)[:110]}")

    # 1) Build the SOURCE part (f1 sketch + f2 extrude, depth 10) on both engines.
    ps = py.new("Src")
    cs = cpp.new("Src")
    pr = py.apply(ps, _source_ops(10.0))
    cr = cpp.apply(cs, _source_ops(10.0))
    check("source build feat_ids", feat_ids(pr["tree"]) == feat_ids(cr["tree"]),
          f"{feat_ids(pr['tree'])} != {feat_ids(cr['tree'])}")
    check("source build volume",
          abs(py.vol(pr) - cpp.vol(cr)) / max(abs(py.vol(pr)), 1.0) < 1e-3,
          f"{py.vol(pr):.1f} != {cpp.vol(cr):.1f}")

    # 2) copy_feature(f2) — the BUNDLE must match (normalized) AND be a read-only batch
    # (no revision bump on either engine).
    p_rev0, c_rev0 = py.revision(ps), cpp.revision(cs)
    pc = py.copy(ps, "f2")
    cc = cpp.copy(cs, "f2")
    p_bundle = pc["id_map"]["$bundle"]
    c_bundle = cc["id_map"]["$bundle"]
    check("copy bundle parity", norm_bundle(p_bundle) == norm_bundle(c_bundle),
          {"py": norm_bundle(p_bundle), "cpp": norm_bundle(c_bundle)})
    check("copy read-only (no rev bump)",
          py.revision(ps) == p_rev0 and cpp.revision(cs) == c_rev0,
          f"py {p_rev0}->{py.revision(ps)} cpp {c_rev0}->{cpp.revision(cs)}")
    # Bundle self-consistency invariants both engines must satisfy.
    check("bundle shape invariants",
          p_bundle.get("kind") == "aicad.feature_bundle/1"
          and c_bundle.get("kind") == "aicad.feature_bundle/1"
          and p_bundle.get("feature_order") == ["$0", "$1"] == c_bundle.get("feature_order")
          and p_bundle.get("root") == "$1" == c_bundle.get("root")
          and p_bundle.get("source_ids") == {"$0": "f1", "$1": "f2"} == c_bundle.get("source_ids"),
          {"py": p_bundle.get("source_ids"), "cpp": c_bundle.get("source_ids")})

    # 3) paste_feature_as_derived into a FRESH target -> sketch + extrude closure stamped derived.
    pt = py.new("Tgt")
    ct = cpp.new("Tgt")
    pp = py.paste_derived(pt, p_bundle, ps.doc_id)
    cp = cpp.paste_derived(ct, c_bundle, cs["doc_id"])
    check("paste feat_ids", feat_ids(pp["tree"]) == feat_ids(cp["tree"]),
          f"{feat_ids(pp['tree'])} != {feat_ids(cp['tree'])}")
    check("paste derived stamp", norm_derived(pp["tree"]) == norm_derived(cp["tree"]),
          {"py": norm_derived(pp["tree"]), "cpp": norm_derived(cp["tree"])})
    check("paste two derived members",
          len(derived_features(pp["tree"])) == 2 == len(derived_features(cp["tree"])),
          f"py {len(derived_features(pp['tree']))} cpp {len(derived_features(cp['tree']))}")
    check("paste volume",
          abs(py.vol(pp) - cpp.vol(cp)) / max(abs(py.vol(pp)), 1.0) < 1e-3,
          f"{py.vol(pp):.1f} != {cpp.vol(cp):.1f}")
    # The derived extrude id + its depth (10) must agree (deterministic ids in a fresh target).
    p_dext = next(f["id"] for f in pp["tree"]["features"]
                  if f.get("derived_from") and f["type"] == "extrude")
    c_dext = next(f["id"] for f in cp["tree"]["features"]
                  if f.get("derived_from") and f["type"] == "extrude")
    check("paste derived extrude id+depth",
          p_dext == c_dext and depth_of(pp["tree"], p_dext) == 10.0 == depth_of(cp["tree"], c_dext),
          f"py {p_dext}={depth_of(pp['tree'], p_dext)} cpp {c_dext}={depth_of(cp['tree'], c_dext)}")

    # 4) Edit the SOURCE extrude depth 10 -> 25; the target is STALE until refreshed.
    py.apply(ps, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(25.0)}}])
    cpp.apply(cs, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(25.0)}}])

    # Drive the refresh and compare the propagated dimension + volume + revision bump.
    p_rev_before, c_rev_before = py.revision(pt), cpp.revision(ct)
    p_vol_before, c_vol_before = py.vol(pp), cpp.vol(cp)
    prr = py.refresh(pt)
    crr = cpp.refresh(ct)
    check("refresh propagates depth 25",
          depth_of(prr["tree"], p_dext) == 25.0 == depth_of(crr["tree"], c_dext),
          f"py {depth_of(prr['tree'], p_dext)} cpp {depth_of(crr['tree'], c_dext)}")
    check("refresh volume 2.5x",
          abs(py.vol(prr) / p_vol_before - 2.5) < 0.02
          and abs(cpp.vol(crr) / c_vol_before - 2.5) < 0.02,
          f"py {py.vol(prr) / p_vol_before:.3f} cpp {cpp.vol(crr) / c_vol_before:.3f}")
    check("refresh bumps revision +1",
          py.revision(pt) == p_rev_before + 1 and cpp.revision(ct) == c_rev_before + 1,
          f"py {p_rev_before}->{py.revision(pt)} cpp {c_rev_before}->{cpp.revision(ct)}")
    check("refresh summary status=refreshed",
          prr["derived"]["groups"][0]["status"] == "refreshed"
          == crr["derived"]["groups"][0]["status"],
          {"py": prr["derived"]["groups"], "cpp": crr["derived"]["groups"]})
    check("refresh stamp parity", norm_derived(prr["tree"]) == norm_derived(crr["tree"]),
          {"py": norm_derived(prr["tree"]), "cpp": norm_derived(crr["tree"])})

    # 5) refresh on a plain (non-derived) document is a no-op with NO revision bump.
    pb = py.new("Plain")
    cb = cpp.new("Plain")
    py.apply(pb, [_box_op()])
    cpp.apply(cb, [_box_op()])
    p_rev, c_rev = py.revision(pb), cpp.revision(cb)
    prn = py.refresh(pb)
    crn = cpp.refresh(cb)
    check("no-derived refresh: empty groups",
          prn["derived"]["groups"] == [] == crn["derived"]["groups"],
          {"py": prn["derived"]["groups"], "cpp": crn["derived"]["groups"]})
    check("no-derived refresh: no rev bump",
          py.revision(pb) == p_rev and cpp.revision(cb) == c_rev,
          f"py {p_rev}->{py.revision(pb)} cpp {c_rev}->{cpp.revision(cb)}")

    # 6) refresh by a NON-derived id is a clean invalid_op on both engines.
    try:
        py.refresh(pb, fid="f1")
        py_bad_ok = False
    except py.P.OpError as exc:
        py_bad_ok = exc.code == "invalid_op"
    rc, cbad = cpp.refresh_raw(cb, fid="f1")
    cpp_bad_ok = rc != 0 and isinstance(cbad, dict) and cbad.get("code") == "invalid_op"
    check("refresh non-derived id -> invalid_op", py_bad_ok and cpp_bad_ok,
          f"py={py_bad_ok} cpp={cpp_bad_ok} {cbad}")

    # 7) Source FEATURE deleted -> refresh leaves last-good + a stale marker, no crash.
    #    Rebuild an independent source+target pair, derive, then delete the source closure.
    ps2 = py.new("Src2")
    cs2 = cpp.new("Src2")
    py.apply(ps2, _source_ops(10.0))
    cpp.apply(cs2, _source_ops(10.0))
    pb2 = py.copy(ps2, "f2")["id_map"]["$bundle"]
    cb2 = cpp.copy(cs2, "f2")["id_map"]["$bundle"]
    pt2 = py.new("Tgt2")
    ct2 = cpp.new("Tgt2")
    ppaste = py.paste_derived(pt2, pb2, ps2.doc_id)
    cpaste = cpp.paste_derived(ct2, cb2, cs2["doc_id"])
    pdx = next(f["id"] for f in ppaste["tree"]["features"]
               if f.get("derived_from") and f["type"] == "extrude")
    cdx = next(f["id"] for f in cpaste["tree"]["features"]
               if f.get("derived_from") and f["type"] == "extrude")
    last_py = depth_of(ppaste["tree"], pdx)
    last_cpp = depth_of(cpaste["tree"], cdx)
    # Delete the whole source closure (f1 cascade drops f1 + dependent f2).
    py.apply(ps2, [{"op": "delete_feature", "id": "f1", "cascade": True}])
    cpp.apply(cs2, [{"op": "delete_feature", "id": "f1", "cascade": True}])
    prd = py.refresh(pt2)
    crd = cpp.refresh(ct2)
    check("deleted-source: last-good kept",
          depth_of(prd["tree"], pdx) == last_py and depth_of(crd["tree"], cdx) == last_cpp,
          f"py {depth_of(prd['tree'], pdx)}={last_py} cpp {depth_of(crd['tree'], cdx)}={last_cpp}")
    check("deleted-source: status source_feature_deleted",
          prd["derived"]["groups"][0]["status"] == "source_feature_deleted"
          == crd["derived"]["groups"][0]["status"],
          {"py": prd["derived"]["groups"], "cpp": crd["derived"]["groups"]})

    # 8) Source DOC closed -> refresh leaves last-good + source_doc_closed, no crash.
    ps3 = py.new("Src3")
    cs3 = cpp.new("Src3")
    py.apply(ps3, _source_ops(10.0))
    cpp.apply(cs3, _source_ops(10.0))
    pb3 = py.copy(ps3, "f2")["id_map"]["$bundle"]
    cb3 = cpp.copy(cs3, "f2")["id_map"]["$bundle"]
    pt3 = py.new("Tgt3")
    ct3 = cpp.new("Tgt3")
    ppaste3 = py.paste_derived(pt3, pb3, ps3.doc_id)
    cpaste3 = cpp.paste_derived(ct3, cb3, cs3["doc_id"])
    pdx3 = next(f["id"] for f in ppaste3["tree"]["features"]
                if f.get("derived_from") and f["type"] == "extrude")
    cdx3 = next(f["id"] for f in cpaste3["tree"]["features"]
                if f.get("derived_from") and f["type"] == "extrude")
    last_py3 = depth_of(ppaste3["tree"], pdx3)
    last_cpp3 = depth_of(cpaste3["tree"], cdx3)
    py.close(ps3)
    cpp.close(cs3)
    prc = py.refresh(pt3)
    crc = cpp.refresh(ct3)
    check("closed-source: last-good kept",
          depth_of(prc["tree"], pdx3) == last_py3 and depth_of(crc["tree"], cdx3) == last_cpp3,
          f"py {depth_of(prc['tree'], pdx3)}={last_py3} cpp {depth_of(crc['tree'], cdx3)}={last_cpp3}")
    check("closed-source: status source_doc_closed",
          prc["derived"]["groups"][0]["status"] == "source_doc_closed"
          == crc["derived"]["groups"][0]["status"],
          {"py": prc["derived"]["groups"], "cpp": crc["derived"]["groups"]})

    # 9) paste_feature (NON-derived) round-trip into the SAME doc: a second body of equal
    #    volume, fresh non-colliding ids, and INDEPENDENCE (editing the original sketch must
    #    NOT move the pasted copy). Validates op_paste_feature + the in-closure ref re-link.
    ps9 = py.new("RT")
    cs9 = cpp.new("RT")
    py.apply(ps9, _source_ops(10.0))
    cpp.apply(cs9, _source_ops(10.0))
    pb9 = py.copy(ps9, "f2")["id_map"]["$bundle"]
    cb9 = cpp.copy(cs9, "f2")["id_map"]["$bundle"]
    ppr = py.apply(ps9, [{"op": "paste_feature", "bundle": pb9}])
    cpr = cpp.apply(cs9, [{"op": "paste_feature", "bundle": cb9}])
    check("paste_feature feat_ids", feat_ids(ppr["tree"]) == feat_ids(cpr["tree"]),
          f"{feat_ids(ppr['tree'])} != {feat_ids(cpr['tree'])}")
    check("paste_feature two-body volume",
          abs(py.vol(ppr) - cpp.vol(cpr)) / max(abs(py.vol(ppr)), 1.0) < 1e-3,
          f"{py.vol(ppr):.1f} != {cpp.vol(cpr):.1f}")
    # The pasted (non-derived) features carry NO derived_from link.
    check("paste_feature no derived stamp",
          derived_features(ppr["tree"]) == [] == derived_features(cpr["tree"]),
          {"py": len(derived_features(ppr["tree"])), "cpp": len(derived_features(cpr["tree"]))})
    p_psk = ppr["id_map"]["$0#0"]   # pasted sketch (dependency-first: sketch then extrude)
    c_psk = cpr["id_map"]["$0#0"]

    def _e1_p2(tree, fid):
        for f in tree.get("features", []):
            if f.get("id") == fid:
                ents = f.get("params", {}).get("entities", [])
                return ents[0].get("p2") if ents else None
        return None

    # Edit the ORIGINAL sketch's first line; the pasted copy must be untouched (independent).
    ppe = py.apply(ps9, [{"op": "sketch_edit_entity", "sketch": "f1", "id": "e1",
                          "params": {"p2": [50.0, 0.0]}}])
    cpe = cpp.apply(cs9, [{"op": "sketch_edit_entity", "sketch": "f1", "id": "e1",
                           "params": {"p2": [50.0, 0.0]}}])
    check("paste_feature independence",
          _e1_p2(ppe["tree"], p_psk) == [20.0, 0.0] and _e1_p2(cpe["tree"], c_psk) == [20.0, 0.0]
          and _e1_p2(ppe["tree"], "f1") == [50.0, 0.0] == _e1_p2(cpe["tree"], "f1"),
          f"py pasted={_e1_p2(ppe['tree'], p_psk)} orig={_e1_p2(ppe['tree'], 'f1')} "
          f"cpp pasted={_e1_p2(cpe['tree'], c_psk)} orig={_e1_p2(cpe['tree'], 'f1')}")

    # 10) A garbage bundle is a clean invalid_op (atomic rejection) on both engines.
    pg = py.new("Bad")
    cg = cpp.new("Bad")
    py.apply(pg, [_box_op()])
    cpp.apply(cg, [_box_op()])
    bad_bundles = [{"kind": "nope"},
                   {"kind": "aicad.feature_bundle/1"},                       # missing order/features
                   {"kind": "aicad.feature_bundle/1", "feature_order": [], "features": {}}]
    for i, bad in enumerate(bad_bundles):
        try:
            py.apply(pg, [{"op": "paste_feature", "bundle": bad}])
            py_ok = False
        except py.P.OpError as exc:
            py_ok = exc.code == "invalid_op"
        rc, cre = cpp.core.call("doc_apply_ops", {"doc_id": cg["doc_id"],
                                                  "ops": [{"op": "paste_feature", "bundle": bad}]})
        cpp_ok = rc != 0 and isinstance(cre, dict) and cre.get("code") == "invalid_op"
        check(f"garbage bundle #{i} -> invalid_op", py_ok and cpp_ok, f"py={py_ok} cpp={cpp_ok} {cre}")
    # The box doc must be UNCHANGED after the rejected pastes (atomic).
    pgt = py.P.tree(pg)["tree"]
    rc, cgt = cpp.core.call("doc_tree", {"doc_id": cg["doc_id"]})
    check("garbage bundle atomic (doc unchanged)",
          feat_ids(pgt) == ["f1"] == feat_ids(cgt["tree"]),
          f"py {feat_ids(pgt)} cpp {feat_ids(cgt.get('tree', {}))}")

    # 11) Cross-document EXTERNAL ref: copy the extrude WITHOUT its closure (include_dependencies
    #     =False) so its profile region points at the out-of-closure sketch f1; paste into an EMPTY
    #     target and require the dangling region to be flagged identically (external marker, doc id
    #     normalized). Validates the out-of-closure branch of resolve_paste_refs / build_feature_bundle.
    psx = py.new("X")
    csx = cpp.new("X")
    py.apply(psx, _source_ops(10.0))
    cpp.apply(csx, _source_ops(10.0))
    pcx = py.apply(psx, [{"op": "copy_feature", "id": "f2", "include_dependencies": False}])
    ccx = cpp.apply(csx, [{"op": "copy_feature", "id": "f2", "include_dependencies": False}])
    pbx = pcx["id_map"]["$bundle"]
    cbx = ccx["id_map"]["$bundle"]
    check("no-deps bundle external_refs",
          pbx.get("external_refs") == ["f1"] == cbx.get("external_refs")
          and pbx.get("feature_order") == ["$0"] == cbx.get("feature_order"),
          {"py": pbx.get("external_refs"), "cpp": cbx.get("external_refs")})

    def _norm_doc(obj):
        """Recursively normalize engine-specific doc ids (and the bundle's group) for comparison."""
        if isinstance(obj, dict):
            return {k: ("<DOC>" if k == "doc_id" else _norm_doc(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_norm_doc(v) for v in obj]
        return obj

    ptx = py.new("XT")
    ctx = cpp.new("XT")
    try:
        ppx = py.apply(ptx, [{"op": "paste_feature", "bundle": pbx}])
        py_paste_err = None
    except py.P.OpError as exc:
        ppx, py_paste_err = None, exc.code
    rc, cpx = cpp.core.call("doc_apply_ops", {"doc_id": ctx["doc_id"],
                                              "ops": [{"op": "paste_feature", "bundle": cbx}]})
    cpp_paste_err = None if rc == 0 else (cpx.get("code") if isinstance(cpx, dict) else "error")
    if py_paste_err or cpp_paste_err:
        # Both engines must AGREE on rejecting (or accepting) the dangling-region paste.
        check("cross-doc paste agree (reject)", py_paste_err == cpp_paste_err,
              f"py={py_paste_err} cpp={cpp_paste_err}")
    else:
        def _region(tree):
            for f in tree.get("features", []):
                if f.get("type") == "extrude":
                    prof = f.get("refs", {}).get("profile", [])
                    return _norm_doc(prof[0]) if prof else None
            return None
        check("cross-doc external region marker",
              _region(ppx["tree"]) == _region(cpx["tree"]),
              {"py": _region(ppx["tree"]), "cpp": _region(cpx["tree"])})

    # 12) refresh BY ID targets only that group (and propagates the source dimension).
    psr = py.new("SrcR")
    csr = cpp.new("SrcR")
    py.apply(psr, _source_ops(10.0))
    cpp.apply(csr, _source_ops(10.0))
    pbr = py.copy(psr, "f2")["id_map"]["$bundle"]
    cbr = cpp.copy(csr, "f2")["id_map"]["$bundle"]
    ptr = py.new("TgtR")
    ctr = cpp.new("TgtR")
    ppr2 = py.paste_derived(ptr, pbr, psr.doc_id)
    cpr2 = cpp.paste_derived(ctr, cbr, csr["doc_id"])
    p_de = next(f["id"] for f in ppr2["tree"]["features"]
                if f.get("derived_from") and f["type"] == "extrude")
    c_de = next(f["id"] for f in cpr2["tree"]["features"]
                if f.get("derived_from") and f["type"] == "extrude")
    py.apply(psr, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(30.0)}}])
    cpp.apply(csr, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(30.0)}}])
    prr2 = py.refresh(ptr, fid=p_de)
    crr2 = cpp.refresh(ctr, fid=c_de)
    check("refresh-by-id propagates depth 30",
          depth_of(prr2["tree"], p_de) == 30.0 == depth_of(crr2["tree"], c_de),
          f"py {depth_of(prr2['tree'], p_de)} cpp {depth_of(crr2['tree'], c_de)}")
    check("refresh-by-id one group in summary",
          len(prr2["derived"]["groups"]) == 1 == len(crr2["derived"]["groups"]),
          f"py {len(prr2['derived']['groups'])} cpp {len(crr2['derived']['groups'])}")

    print(f"\n[derived-parity] {total - mism}/{total} steps at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())

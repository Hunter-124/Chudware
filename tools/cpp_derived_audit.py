"""Adversarial audit of the copy/paste-as-derived subsystem (py-vs-C++ doc engine).

cpp_derived_parity covers the happy path (build → copy → paste-derived → edit →
refresh). This stresses the re-id / multi-group corners fresh paste code most often
gets wrong, and asserts py/cpp parity on each:

  A. paste the SAME bundle twice into one target — re-id must not collide; 2 stamps,
     volume accumulates.
  B. paste into a PRE-POPULATED target (already has a box: f1/e?/c?) — pasted ids must
     dodge the existing ones.
  C. TWO independent sources pasted into one target — two derived groups; a refresh of
     ONE group (after editing only its source) must leave the other untouched.
  D. delete the source feature, then refresh — both engines must mark the SAME stale reason.

A DIFF is a SUCCESS (a real divergence the curated harness missed)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_derived_parity import (CppCore, PyEng, CppEng, _source_ops, _box_op,
                                      _dim, feat_ids, norm_derived, derived_features)


def src_doc_id(sess):
    return sess.doc_id if hasattr(sess, "doc_id") else sess["doc_id"]


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[derived-audit] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    py, cpp = PyEng(), CppEng(core)
    print(f"[derived-audit] libaicad ABI={core.abi()}")
    mism = 0
    total = 0

    def check(label, ok, detail=""):
        nonlocal mism, total
        total += 1
        if not ok:
            mism += 1
        print(f"  {label:<40} {'MATCH' if ok else 'DIFF: ' + str(detail)[:100]}")

    def both(fn):
        """Run fn on (engine, session) for each side; return (py_result, cpp_result)."""
        return fn(py, _ps), fn(cpp, _cs)

    # ---- shared SOURCE part (sketch + extrude, depth 10) -------------------------
    def build_source(depth=10.0):
        ps = py.new("Src"); cs = cpp.new("Src")
        py.apply(ps, _source_ops(depth)); cpp.apply(cs, _source_ops(depth))
        pc = py.copy(ps, "f2"); cc = cpp.copy(cs, "f2")
        return ps, cs, pc["id_map"]["$bundle"], cc["id_map"]["$bundle"]

    # ==== A. double paste of the same bundle into one fresh target ================
    ps, cs, pb, cb = build_source()
    pt = py.new("Tgt"); ct = cpp.new("Tgt")
    py.paste_derived(pt, pb, src_doc_id(ps)); cpp.paste_derived(ct, cb, src_doc_id(cs))
    pr = py.paste_derived(pt, pb, src_doc_id(ps)); cr = cpp.paste_derived(ct, cb, src_doc_id(cs))
    pids, cids = feat_ids(pr["tree"]), feat_ids(cr["tree"])
    check("A: double-paste feat_ids parity", pids == cids, f"py={pids} cpp={cids}")
    check("A: double-paste no dup ids", len(pids) == len(set(pids)) and len(cids) == len(set(cids)),
          f"py={pids} cpp={cids}")
    # each paste-derived stamps the FULL closure (sketch + extrude), so 2 pastes = 4 stamps.
    n_pderv = len(derived_features(pr["tree"])); n_cderv = len(derived_features(cr["tree"]))
    check("A: derived-stamp count parity (4=2closure x2)", n_pderv == n_cderv == 4,
          f"py={n_pderv} cpp={n_cderv}")
    check("A: double-paste volume parity",
          abs(py.vol(pr) - cpp.vol(cr)) / max(abs(py.vol(pr)), 1.0) < 1e-3,
          f"py={py.vol(pr):.1f} cpp={cpp.vol(cr):.1f}")

    # ==== B. paste into a PRE-POPULATED target (existing box) =====================
    ps, cs, pb, cb = build_source()
    pt = py.new("Tgt2"); ct = cpp.new("Tgt2")
    py.apply(pt, [_box_op()]); cpp.apply(ct, [_box_op()])
    pr = py.paste_derived(pt, pb, src_doc_id(ps)); cr = cpp.paste_derived(ct, cb, src_doc_id(cs))
    pids, cids = feat_ids(pr["tree"]), feat_ids(cr["tree"])
    check("B: populated-target feat_ids parity", pids == cids, f"py={pids} cpp={cids}")
    check("B: populated-target no id collision",
          len(pids) == len(set(pids)) and len(cids) == len(set(cids)), f"py={pids} cpp={cids}")
    check("B: populated-target volume parity",
          abs(py.vol(pr) - cpp.vol(cr)) / max(abs(py.vol(pr)), 1.0) < 1e-3,
          f"py={py.vol(pr):.1f} cpp={cpp.vol(cr):.1f}")

    # ==== C. two independent sources -> two groups -> selective refresh ===========
    psA, csA, pbA, cbA = build_source(10.0)
    psB, csB, pbB, cbB = build_source(20.0)
    pt = py.new("Tgt3"); ct = cpp.new("Tgt3")
    py.paste_derived(pt, pbA, src_doc_id(psA)); cpp.paste_derived(ct, cbA, src_doc_id(csA))
    pr = py.paste_derived(pt, pbB, src_doc_id(psB)); cr = cpp.paste_derived(ct, cbB, src_doc_id(csB))
    pgs = norm_derived(pr["tree"]); cgs = norm_derived(cr["tree"])
    check("C: two groups stamp parity", pgs == cgs, f"py={pgs} cpp={cgs}")
    # edit only source A's depth, refresh ALL: only the A-derived feature changes.
    py.apply(psA, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(40.0)}}])
    cpp.apply(csA, [{"op": "edit_feature", "id": "f2", "params": {"depth": _dim(40.0)}}])
    prr = py.refresh(pt); crr = cpp.refresh(ct)
    check("C: selective refresh volume parity",
          abs(py.vol(prr) - cpp.vol(crr)) / max(abs(py.vol(prr)), 1.0) < 1e-3,
          f"py={py.vol(prr):.1f} cpp={cpp.vol(crr):.1f}")
    check("C: selective refresh stamp parity", norm_derived(prr["tree"]) == norm_derived(crr["tree"]),
          f"py={norm_derived(prr['tree'])} cpp={norm_derived(crr['tree'])}")

    # ==== D. delete the source feature, then refresh -> stale reason parity =======
    ps, cs, pb, cb = build_source()
    pt = py.new("Tgt4"); ct = cpp.new("Tgt4")
    py.paste_derived(pt, pb, src_doc_id(ps)); cpp.paste_derived(ct, cb, src_doc_id(cs))
    # remove the source's extrude (f2) — the derived re-pull now has no source def.
    try:
        py.apply(ps, [{"op": "delete_feature", "id": "f2"}])
        cpp.apply(cs, [{"op": "delete_feature", "id": "f2"}])
        prr = py.refresh(pt)
        rc, crr = cpp.refresh_raw(ct)
        p_reason = (norm_derived(prr["tree"])[0] or {}).get("stale") if norm_derived(prr["tree"]) else None
        c_tree = crr.get("tree") if isinstance(crr, dict) else {}
        c_reason = (norm_derived(c_tree)[0] or {}).get("stale") if norm_derived(c_tree) else None
        check("D: deleted-source stale reason parity", p_reason == c_reason,
              f"py={p_reason!r} cpp={c_reason!r}")
    except Exception as exc:  # noqa: BLE001
        check("D: deleted-source refresh", False, f"raised {type(exc).__name__}: {exc}")

    print(f"\n[derived-audit] {total - mism}/{total} MATCH ; {mism} divergence(s)")
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(main())

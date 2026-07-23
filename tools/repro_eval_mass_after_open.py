"""Repro for: eval_mass fails 'document has no bodies to evaluate' right after doc_open.

Drives the C++ core directly via core_bridge (the same DLL the MCP loads).
"""
import sys, os, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
import core_bridge as cb


def show(label, r):
    keys = {k: r[k] for k in ("doc_id", "volume", "body_count", "name") if k in r}
    print(f"  {label}: {json.dumps(keys)}")


def try_eval(doc_id):
    try:
        m = cb.call("eval_mass", {"doc_id": doc_id, "density_g_cm3": 7.85})
        print(f"  eval_mass OK: volume_mm3={m.get('volume_mm3')} body_count={m.get('body_count')}")
        return True
    except cb.CoreError as e:
        print(f"  eval_mass FAIL: code={e.code!r} msg={e}")
        return False


def make_box_doc():
    d = cb.call("doc_new", {"name": "box_src"})
    did = d["doc_id"]
    op = cb.feat("box", "Box1", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})
    r = cb.call("doc_apply_ops", {"doc_id": did, "ops": [op]})
    show("after box build", {**r, "doc_id": did})
    return did


def main():
    tmp = tempfile.mkdtemp(prefix="chudware_repro_")

    # ---------- .step+ case ----------
    print("=== .step+ case ===")
    did = make_box_doc()
    sp_path = os.path.join(tmp, "box.step+")
    cb.call("doc_save", {"doc_id": did, "path": sp_path})
    try:
        cb.call("doc_close", {"doc_id": did})
    except Exception as e:
        print("  (doc_close:", e, ")")
    r = cb.call("doc_open", {"path": sp_path})
    new_id = r["doc_id"]
    show("doc_open", r)
    print("  --> eval_mass immediately after doc_open:")
    ok1 = try_eval(new_id)
    print("  --> doc_apply_ops with EMPTY ops then eval_mass:")
    cb.call("doc_apply_ops", {"doc_id": new_id, "ops": []})
    ok2 = try_eval(new_id)

    # ---------- IGES case ----------
    print("\n=== IGES case ===")
    did2 = make_box_doc()
    igs_path = os.path.join(tmp, "box.igs")
    try:
        ex = cb.call("export_file", {"doc_id": did2, "path": igs_path, "format": "iges"})
        print("  export:", {k: ex[k] for k in ("ok", "path") if k in ex})
    except Exception as e:
        print("  export FAIL:", e)
    try:
        cb.call("doc_close", {"doc_id": did2})
    except Exception:
        pass
    if os.path.isfile(igs_path):
        r2 = cb.call("doc_open", {"path": igs_path})
        new_id2 = r2["doc_id"]
        show("doc_open(iges)", r2)
        print("  --> eval_mass immediately after doc_open:")
        try_eval(new_id2)
        print("  --> doc_apply_ops empty then eval_mass:")
        cb.call("doc_apply_ops", {"doc_id": new_id2, "ops": []})
        try_eval(new_id2)

    print("\nRESULT: step+ immediate-eval ok?", ok1, " after-empty-ops ok?", ok2)


if __name__ == "__main__":
    main()

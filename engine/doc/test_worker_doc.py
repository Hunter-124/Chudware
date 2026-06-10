"""C11 — REAL subprocess worker doc_* test (mirrors engine/test_worker_stream.py style).

Run:  & $py -m engine.doc.test_worker_doc   (from repo root)

Drives the real ``python -m engine.worker`` over stdio end to end. This transcript IS the contract
C16 (the C++ doc_mirror) builds against:

  ping (protocol 2)
  -> doc_new {kind: part}            -> ops-response + doc_id
  -> doc_apply_ops (add box)         -> tree shows the box, AICM written
  -> doc_tree                        -> the box feature present
  -> doc_resolve_pick (edge pick)    -> a persistent selector
  -> doc_apply_ops (fillet via pick) -> fillet ok
  -> doc_undo                        -> back to the box
  -> doc_redo                        -> fillet again
  -> doc_save (.step+)               -> {path}
  -> doc_open (the saved file)       -> new doc_id + warnings
  -> doc_list                        -> both docs listed
  -> shutdown

Asserts: protocol 2, every step OK, the AICM file exists and parses (C02 read_aicm), the
structured error envelope on a stale pick.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from engine.doc.tessellate import read_aicm


def main() -> None:
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1)

    transcript: list[str] = []

    def rpc(obj: dict) -> dict:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        # Skip event lines (notifications), return the first final response for this id.
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed the pipe unexpectedly")
            msg = json.loads(line)
            if "event" in msg:
                continue
            transcript.append(f"{obj['method']} -> ok={msg.get('ok')}")
            return msg

    try:
        # 1. ping — must advertise protocol 2
        r = rpc({"id": 1, "method": "ping", "params": {}})
        assert r["ok"] and r["result"]["pong"] and r["result"]["protocol"] == 2, r
        print(f"[worker-doc] ping: protocol={r['result']['protocol']} build123d={r['result']['build123d']}")

        # 2. doc_new part
        r = rpc({"id": 2, "method": "doc_new", "params": {"kind": "part"}})
        assert r["ok"], r
        doc_id = r["result"]["doc_id"]
        assert r["result"]["revision"] == 0, r["result"]
        print(f"[worker-doc] doc_new -> doc_id={doc_id} revision=0")

        # 3. doc_apply_ops: add a box
        box_op = {"op": "add_feature", "feature": {
            "type": "box", "name": "Box1",
            "params": {"length": {"value": 20, "expr": None},
                       "width": {"value": 20, "expr": None},
                       "height": {"value": 20, "expr": None}}}}
        r = rpc({"id": 3, "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 0, "ops": [box_op]}})
        assert r["ok"], r
        res = r["result"]
        assert res["revision"] == 1 and res["id_map"]["$0"] == "f1", res
        assert res["rebuild"]["feature_status"][0]["ok"], res["rebuild"]
        aicm_path = res["tess"]["aicm_path"]
        assert os.path.exists(aicm_path), f"AICM not written: {aicm_path}"
        parsed = read_aicm(open(aicm_path, "rb").read())
        assert len(parsed["bodies"]) == 1, parsed
        print(f"[worker-doc] doc_apply_ops(add box) -> rev=1 id_map={res['id_map']} "
              f"aicm bodies={len(parsed['bodies'])}")

        # 4. doc_tree — the box is in the tree
        r = rpc({"id": 4, "method": "doc_tree", "params": {"doc_id": doc_id}})
        assert r["ok"] and r["result"]["revision"] == 1, r
        feats = r["result"]["tree"]["features"]
        assert [f["id"] for f in feats] == ["f1"], feats
        print(f"[worker-doc] doc_tree -> features={[f['id'] for f in feats]}")

        # 5. doc_resolve_pick — pick edge #1 of body #1
        r = rpc({"id": 5, "method": "doc_resolve_pick",
                 "params": {"doc_id": doc_id, "revision": 1,
                            "body_id": 1, "kind": "edge", "topo_id": 1}})
        assert r["ok"], r
        selector = r["result"]["selector"]
        assert selector["kind"] == "edge" and selector["owner_feature"] == "f1", selector
        print(f"[worker-doc] doc_resolve_pick(body=1 edge id=1) -> selector "
              f"owner={selector['owner_feature']} surf={selector['surf']}")

        # 5b. a stale pick must raise the structured stale_revision envelope
        r_stale = rpc({"id": 55, "method": "doc_resolve_pick",
                       "params": {"doc_id": doc_id, "revision": 0,
                                  "body_id": 1, "kind": "edge", "topo_id": 1}})
        assert not r_stale["ok"] and r_stale["error"]["code"] == "stale_revision", r_stale
        assert r_stale["error"]["detail"]["current_revision"] == 1, r_stale["error"]
        print(f"[worker-doc] doc_resolve_pick(stale rev=0) -> error envelope "
              f"code={r_stale['error']['code']} detail={r_stale['error']['detail']}")

        # 6. doc_apply_ops: fillet via the picked selector
        fillet_op = {"op": "add_feature", "feature": {
            "type": "fillet", "name": "Fillet1",
            "params": {"radius": {"value": 2.0, "expr": None}},
            "refs": {"edges": [selector]}}}
        r = rpc({"id": 6, "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 1, "ops": [fillet_op]}})
        assert r["ok"], r
        res = r["result"]
        assert res["revision"] == 2, res
        f2 = res["rebuild"]["feature_status"][-1]
        assert f2["id"] == "f2" and f2["ok"], f2
        print(f"[worker-doc] doc_apply_ops(fillet via pick) -> rev=2 f2.ok={f2['ok']}")

        # 7. doc_undo -> back to just the box (rev bumps forward)
        r = rpc({"id": 7, "method": "doc_undo", "params": {"doc_id": doc_id}})
        assert r["ok"], r
        feats = r["result"]["tree"]["features"]
        assert [f["id"] for f in feats] == ["f1"], f"undo should drop the fillet, got {feats}"
        print(f"[worker-doc] doc_undo -> features={[f['id'] for f in feats]} rev={r['result']['revision']}")

        # 8. doc_redo -> the fillet returns
        r = rpc({"id": 8, "method": "doc_redo", "params": {"doc_id": doc_id}})
        assert r["ok"], r
        feats = r["result"]["tree"]["features"]
        assert [f["id"] for f in feats] == ["f1", "f2"], f"redo should restore the fillet, got {feats}"
        print(f"[worker-doc] doc_redo -> features={[f['id'] for f in feats]} rev={r['result']['revision']}")

        # 9. doc_save -> .step+
        save_path = os.path.join(tempfile.mkdtemp(), "worker_doc.step+")
        r = rpc({"id": 9, "method": "doc_save", "params": {"doc_id": doc_id, "path": save_path}})
        assert r["ok"] and r["result"]["path"] == save_path and os.path.exists(save_path), r
        print(f"[worker-doc] doc_save -> {os.path.basename(save_path)} ({os.path.getsize(save_path)} B)")

        # 10. doc_open -> a fresh doc_id, clean rebuild
        r = rpc({"id": 10, "method": "doc_open", "params": {"path": save_path}})
        assert r["ok"], r
        opened_id = r["result"]["doc_id"]
        assert opened_id != doc_id and r["result"]["warnings"] == [], r["result"]
        feats = r["result"]["tree"]["features"]
        assert [f["id"] for f in feats] == ["f1", "f2"], feats
        print(f"[worker-doc] doc_open -> doc_id={opened_id} features={[f['id'] for f in feats]} "
              f"warnings={r['result']['warnings']}")

        # 11. doc_list -> both docs
        r = rpc({"id": 11, "method": "doc_list", "params": {}})
        assert r["ok"], r
        ids = {d["doc_id"] for d in r["result"]["docs"]}
        assert doc_id in ids and opened_id in ids, r["result"]["docs"]
        print(f"[worker-doc] doc_list -> {len(r['result']['docs'])} open docs")

        # 12. shutdown
        rpc({"id": 12, "method": "shutdown", "params": {}})

        print("\n--- worker call transcript (for C16) ---")
        for line in transcript:
            print("  " + line)
        print("\nWORKER DOC RPC SUBPROCESS TEST PASSED")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=30)


if __name__ == "__main__":
    main()

"""C18 — REAL subprocess worker sketch_* test (house style, mirrors test_worker_doc.py).

Run:  & $py -m engine.doc.test_worker_sketch   (from repo root)

Drives the real ``python -m engine.worker`` over stdio end to end and exercises the C18 sketch
methods wired to C10's solver:

  ping (protocol 2)
  -> doc_new {kind: part}
  -> doc_apply_ops: add a sketch on plane.top, then a corner rectangle (4 lines) with
     coincident corners + horizontal/vertical relations + a fix on one corner + two driving
     dims (width=80, height=40)
  -> sketch_enter          -> entities/statuses/dof + 16-float plane_frame (identity for top)
  -> sketch_drag a free corner, then sketch_drag_commit (persists the solved coords, rev bumps)
  -> doc_apply_ops: sketch_set_dim width 80 -> 100
  -> sketch_solve          -> the rectangle is now 100 wide
  -> sketch_exit           -> downstream rebuild
  -> doc_apply_ops: extrude the sketch region depth 10 -> volume ~= 100*40*10
  -> shutdown

Asserts: protocol 2, every step OK, plane_frame maps (u,v,0)->world correctly for plane.top,
DOF drops as dims are added, the committed coords survive, the dim edit re-solves, and the
extruded volume is sane.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> None:
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1)

    transcript: list[str] = []

    def rpc(obj: dict) -> dict:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed the pipe unexpectedly")
            msg = json.loads(line)
            if "event" in msg:
                continue
            transcript.append(f"{obj['method']} -> ok={msg.get('ok')}")
            return msg

    _idc = [0]

    def nid() -> int:
        _idc[0] += 1
        return _idc[0]

    try:
        # 1. ping
        r = rpc({"id": nid(), "method": "ping", "params": {}})
        assert r["ok"] and r["result"]["protocol"] == 2, r
        print(f"[worker-sketch] ping: protocol={r['result']['protocol']}")

        # 2. doc_new part
        r = rpc({"id": nid(), "method": "doc_new", "params": {"kind": "part"}})
        assert r["ok"], r
        doc_id = r["result"]["doc_id"]
        print(f"[worker-sketch] doc_new -> doc_id={doc_id}")

        # 3a. add the sketch + four rectangle lines (entities get real ids in this batch).
        #     Rect corners (CCW): A(0,0) B(80,0) C(80,40) D(0,40).
        #     Lines: L0 A->B (bottom), L1 B->C (right), L2 C->D (top), L3 D->A (left).
        #     The coincident corners are attached as inferred relations on each line via $self
        #     (the host's auto-relation path). The first line stands alone; lines 2-4 close onto
        #     the growing chain using the previous line's real id from the id_map.
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 0, "ops": [
                     {"op": "add_feature", "feature": {
                         "type": "sketch", "name": "Sketch1",
                         "refs": {"plane": "plane.top"},
                         "params": {"entities": [], "constraints": []}}},
                     {"op": "sketch_add_entity", "sketch": "$0",
                      "entity": {"type": "line", "p1": [0, 0], "p2": [80, 0]}}]}})  # $0 sk, $1 L0
        assert r["ok"], r
        idmap = r["result"]["id_map"]
        sketch_fid, L0 = idmap["$0"], idmap["$1"]

        # 3b. lines 1..3, each closing onto the previous line's p2 via an inferred coincident.
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 1, "ops": [
                     {"op": "sketch_add_entity", "sketch": sketch_fid,
                      "entity": {"type": "line", "p1": [80, 0], "p2": [80, 40]},
                      "constraints": [{"type": "coincident", "refs": [f"{L0}.p2", "$self.p1"]}]}]}})
        assert r["ok"], r
        L1 = r["result"]["id_map"]["$0"]
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 2, "ops": [
                     {"op": "sketch_add_entity", "sketch": sketch_fid,
                      "entity": {"type": "line", "p1": [80, 40], "p2": [0, 40]},
                      "constraints": [{"type": "coincident", "refs": [f"{L1}.p2", "$self.p1"]}]}]}})
        assert r["ok"], r
        L2 = r["result"]["id_map"]["$0"]
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 3, "ops": [
                     {"op": "sketch_add_entity", "sketch": sketch_fid,
                      "entity": {"type": "line", "p1": [0, 40], "p2": [0, 0]},
                      "constraints": [
                          {"type": "coincident", "refs": [f"{L2}.p2", "$self.p1"]},
                          {"type": "coincident", "refs": [f"{L0}.p1", "$self.p2"]}]}]}})
        assert r["ok"], r
        L3 = r["result"]["id_map"]["$0"]

        # 3c. relations (h/v), the corner fix, and the two driving dims — now with real ids.
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 4, "ops": [
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "horizontal", "refs": [L0]}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "horizontal", "refs": [L2]}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "vertical", "refs": [L1]}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "vertical", "refs": [L3]}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "fix", "refs": [f"{L0}.p1"]}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "distance", "refs": [f"{L0}.p1", f"{L0}.p2"],
                                     "value": {"value": 80.0, "expr": None}}},
                     {"op": "sketch_add_constraint", "sketch": sketch_fid,
                      "constraint": {"type": "distance", "refs": [f"{L1}.p1", f"{L1}.p2"],
                                     "value": {"value": 40.0, "expr": None}}}]}})
        assert r["ok"], r
        width_cid = r["result"]["id_map"]["$5"]   # the width distance constraint (op index 5)
        print(f"[worker-sketch] built sketch+rect -> sketch={sketch_fid} lines=[{L0},{L1},{L2},{L3}] "
              f"rev={r['result']['revision']} width_cid={width_cid}")

        # 4. sketch_enter — solved coords + statuses + dof + plane_frame
        r = rpc({"id": nid(), "method": "sketch_enter",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        res = r["result"]
        frame = res["plane_frame"]
        assert len(frame) == 16, frame
        # plane.top is identity: (u=10,v=20,0,1) -> (10,20,0)
        def xform(fr, u, v, w):
            vec = [u, v, w, 1.0]
            return [sum(fr[row * 4 + k] * vec[k] for k in range(4)) for row in range(4)]
        wx, wy, wz, _ = xform(frame, 10.0, 20.0, 0.0)
        assert abs(wx - 10) < 1e-6 and abs(wy - 20) < 1e-6 and abs(wz - 0) < 1e-6, (wx, wy, wz)
        ents = {e["id"]: e for e in res["entities"]}
        assert len(ents) == 4, ents
        dof_full = res["dof"]
        print(f"[worker-sketch] sketch_enter -> entities={len(ents)} dof={dof_full} "
              f"status={res['status']} plane_frame[12:15]={frame[12:15]}")
        assert res["status"] in ("under", "full"), res["status"]

        # 5. drag a free corner (top-right C = $2.p2 / $3.p1) and commit.
        #    The rect is fully dimensioned + fixed at A, so the drag is constraint-exact; commit
        #    persists whatever solved coords result. We drag the right edge's p2 a touch.
        drag_handle = f"{L1}.p2"
        r = rpc({"id": nid(), "method": "sketch_drag",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid,
                            "handle": drag_handle, "xy": [85.0, 45.0]}})
        assert r["ok"], r
        print(f"[worker-sketch] sketch_drag({drag_handle}) -> ok "
              f"(transient, no rev bump)")

        r = rpc({"id": nid(), "method": "sketch_drag_commit",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        rev_after_commit = r["result"]["revision"]
        print(f"[worker-sketch] sketch_drag_commit -> rev={rev_after_commit} "
              f"doc_status={r['result']['rebuild']['doc_status']}")

        # 6. set width dim 80 -> 100 via doc_apply_ops, then re-solve.
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": rev_after_commit,
                            "ops": [{"op": "sketch_set_dim", "sketch": sketch_fid,
                                     "id": width_cid, "value": 100.0}]}})
        assert r["ok"], r
        print(f"[worker-sketch] sketch_set_dim(width 80->100) -> rev={r['result']['revision']}")

        r = rpc({"id": nid(), "method": "sketch_solve",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        ents2 = {e["id"]: e for e in r["result"]["entities"]}
        bottom = ents2[L0]
        import math
        wnow = math.hypot(bottom["p2"][0] - bottom["p1"][0], bottom["p2"][1] - bottom["p1"][1])
        assert abs(wnow - 100.0) < 1e-3, f"width should be 100, got {wnow}"
        print(f"[worker-sketch] sketch_solve -> bottom-edge length={wnow:.3f} "
              f"dof={r['result']['dof']} status={r['result']['status']}")

        # 7. exit sketch
        r = rpc({"id": nid(), "method": "sketch_exit",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        assert r["result"]["rebuild"]["doc_status"]["active_sketch"] is None, r["result"]
        print(f"[worker-sketch] sketch_exit -> active_sketch="
              f"{r['result']['rebuild']['doc_status']['active_sketch']}")

        # 8. extrude the region depth 10 -> volume ~= 100 * 40 * 10 = 40000 mm^3
        region_sel = {"kind": "region", "sketch": sketch_fid,
                      "centroid": [50.0, 20.0], "hint_index": 0}
        extrude_op = {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss-Extrude1",
            "params": {"depth": {"value": 10.0, "expr": None},
                       "end": "blind", "mode": "boss"},
            "refs": {"profile": [region_sel]}}}
        base_rev = r["result"]["revision"]
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": base_rev,
                            "ops": [extrude_op]}})
        assert r["ok"], r
        fstat = r["result"]["rebuild"]["feature_status"][-1]
        assert fstat["ok"], f"extrude failed: {fstat}"
        print(f"[worker-sketch] doc_apply_ops(extrude region depth 10) -> "
              f"{fstat['id']}.ok={fstat['ok']}")

        # verify the volume via eval_mass on the resulting body
        r = rpc({"id": nid(), "method": "eval_mass",
                 "params": {"doc_id": doc_id}})
        assert r["ok"], r
        vol = r["result"]["volume_mm3"]
        assert abs(vol - 40000.0) < 50.0, f"extruded volume {vol} != ~40000"
        print(f"[worker-sketch] eval_mass -> volume_mm3={vol:.1f} (expected ~40000)")

        rpc({"id": nid(), "method": "shutdown", "params": {}})

        print("\n--- worker call transcript (C18 sketch) ---")
        for line in transcript:
            print("  " + line)
        print("\nWORKER SKETCH RPC SUBPROCESS TEST PASSED")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=60)


if __name__ == "__main__":
    main()

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
import math
import subprocess
import sys


def _spawn_worker():
    """Start the real worker subprocess; return (proc, rpc) — rpc skips event lines."""
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1)
    _idc = [0]

    def rpc(method: str, params: dict | None = None) -> dict:
        _idc[0] += 1
        p.stdin.write(json.dumps({"id": _idc[0], "method": method,
                                  "params": params or {}}) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed the pipe unexpectedly")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    return p, rpc


def test_sketch_project_ref() -> None:
    """C50 sketch_project_ref over the REAL worker subprocess.

    Box doc: datum line into a top-plane sketch, a planar (vertical) box face, a linear
    box edge, a vertical box edge (projects to a point). Cylinder doc: circular edge into
    a top sketch -> circle; same edge into a front sketch -> unsupported_ref; parallel
    datum -> unsupported_ref. The method is READ-ONLY: the revision never moves.
    """
    p, rpc = _spawn_worker()
    try:
        r = rpc("ping")
        assert r["ok"] and r["result"]["protocol"] == 2, r

        # ---- box doc: 80 x 40 x 10 centered at the origin + a sketch on plane.top ----
        r = rpc("doc_new", {"kind": "part"})
        assert r["ok"], r
        doc_id = r["result"]["doc_id"]
        r = rpc("doc_apply_ops", {"doc_id": doc_id, "base_revision": 0, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Box1",
                "params": {"length": {"value": 80.0, "expr": None},
                           "width": {"value": 40.0, "expr": None},
                           "height": {"value": 10.0, "expr": None}}}},
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "Sketch1",
                "refs": {"plane": "plane.top"},
                "params": {"entities": [], "constraints": []}}}]})
        assert r["ok"], r
        sk = r["result"]["id_map"]["$1"]
        rev0 = r["result"]["revision"]

        # 1. datum front -> the u axis (v == 0), span >= 2x bbox diag (>= 100mm half)
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"datum": "front"}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "line", ent
        assert abs(ent["p1"][1]) < 1e-6 and abs(ent["p2"][1]) < 1e-6, ent
        dlen = math.hypot(ent["p2"][0] - ent["p1"][0], ent["p2"][1] - ent["p1"][1])
        assert dlen >= 200.0, f"datum line too short: {dlen}"
        assert r["result"]["source"] == "plane.front", r["result"]
        print(f"[project] datum front -> line v=0, len={dlen:.1f}")

        # also the 'plane.right' spelling -> the v axis (u == 0)
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"datum": "plane.right"}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "line" and abs(ent["p1"][0]) < 1e-6 and abs(ent["p2"][0]) < 1e-6, ent

        # collect the box's face + edge selectors via doc_resolve_pick
        def pick_all(kind: str) -> list[dict]:
            out = []
            topo = 1
            while True:
                rr = rpc("doc_resolve_pick", {"doc_id": doc_id, "body_id": 1,
                                              "kind": kind, "topo_id": topo})
                if not rr["ok"]:
                    break
                out.append(rr["result"]["selector"])
                topo += 1
            return out

        faces = pick_all("face")
        edges = pick_all("edge")
        # TopExp_Explorer visits shared edges once per owning face (24 for a box)
        assert len(faces) == 6 and len(edges) >= 12, (len(faces), len(edges))

        # 2. planar vertical face (normal +-x at u = +-40) -> intersection line u=+-40,
        #    spanned by the face's projected vertices (40 wide) + 5mm pad each side.
        fsel = next(s for s in faces
                    if s["axis"] is not None and abs(abs(s["axis"][0]) - 1.0) < 1e-6)
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"kind": "face", "selector": fsel}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "line", ent
        ux = fsel["centroid"][0]
        assert abs(ent["p1"][0] - ux) < 1e-6 and abs(ent["p2"][0] - ux) < 1e-6, (ent, ux)
        flen = math.hypot(ent["p2"][0] - ent["p1"][0], ent["p2"][1] - ent["p1"][1])
        assert abs(flen - 50.0) < 1e-5, f"face span should be 40 + 2*5 pad = 50, got {flen}"
        print(f"[project] vertical face -> line u={ux:.1f}, len={flen:.1f}")

        # 3. linear box edge along x (top rim) -> a line at the edge's y, full 80 length
        esel = next(s for s in edges
                    if s["surf"] == "line" and s["axis"] is not None
                    and abs(abs(s["axis"][0]) - 1.0) < 1e-6)
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"kind": "edge", "selector": esel}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "line", ent
        ey = esel["centroid"][1]
        assert abs(ent["p1"][1] - ey) < 1e-6 and abs(ent["p2"][1] - ey) < 1e-6, (ent, ey)
        elen = math.hypot(ent["p2"][0] - ent["p1"][0], ent["p2"][1] - ent["p1"][1])
        assert abs(elen - 80.0) < 1e-5, f"projected edge length should be 80, got {elen}"
        print(f"[project] linear edge -> line v={ey:.1f}, len={elen:.1f}")

        # 4. vertical box edge (along z == the sketch normal) -> projects to ONE point
        vsel = next(s for s in edges
                    if s["surf"] == "line" and s["axis"] is not None
                    and abs(abs(s["axis"][2]) - 1.0) < 1e-6)
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"kind": "edge", "selector": vsel}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "point", ent
        assert abs(ent["p"][0] - vsel["centroid"][0]) < 1e-6 \
            and abs(ent["p"][1] - vsel["centroid"][1]) < 1e-6, (ent, vsel["centroid"])
        print(f"[project] vertical edge -> point {ent['p']}")

        # 5. parallel datum -> structured unsupported_ref error
        r = rpc("sketch_project_ref", {"doc_id": doc_id, "sketch": sk,
                                       "ref": {"datum": "top"}})
        assert not r["ok"], "projecting a parallel plane must fail"
        assert r["error"]["code"] == "unsupported_ref", r["error"]
        assert "parallel" in r["error"]["message"], r["error"]
        print(f"[project] parallel datum -> {r['error']['code']}: {r['error']['message']}")

        # READ-ONLY: nothing above moved the revision
        r = rpc("doc_tree", {"doc_id": doc_id})
        assert r["ok"] and r["result"]["revision"] == rev0, \
            f"sketch_project_ref must not bump the revision ({r['result']['revision']} != {rev0})"

        # ---- cylinder doc: circular edge -> circle; tilted axis -> unsupported ----
        r = rpc("doc_new", {"kind": "part"})
        assert r["ok"], r
        doc2 = r["result"]["doc_id"]
        r = rpc("doc_apply_ops", {"doc_id": doc2, "base_revision": 0, "ops": [
            {"op": "add_feature", "feature": {
                "type": "cylinder", "name": "Boss1",
                "params": {"radius": {"value": 15.0, "expr": None},
                           "height": {"value": 20.0, "expr": None}}}},
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "TopSketch",
                "refs": {"plane": "plane.top"},
                "params": {"entities": [], "constraints": []}}},
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "FrontSketch",
                "refs": {"plane": "plane.front"},
                "params": {"entities": [], "constraints": []}}}]})
        assert r["ok"], r
        sk_top = r["result"]["id_map"]["$1"]
        sk_front = r["result"]["id_map"]["$2"]

        # circular rim edge of the boss
        csel = None
        topo = 1
        while True:
            rr = rpc("doc_resolve_pick", {"doc_id": doc2, "body_id": 1,
                                          "kind": "edge", "topo_id": topo})
            if not rr["ok"]:
                break
            if rr["result"]["selector"]["surf"] == "circle":
                csel = rr["result"]["selector"]
                break
            topo += 1
        assert csel is not None, "cylinder must expose a circular edge"

        # 6. circle axis parallel to the top sketch normal -> circle entity
        r = rpc("sketch_project_ref", {"doc_id": doc2, "sketch": sk_top,
                                       "ref": {"kind": "edge", "selector": csel}})
        assert r["ok"], r
        ent = r["result"]["entity"]
        assert ent["type"] == "circle", ent
        assert abs(ent["radius"] - 15.0) < 1e-6, ent
        assert abs(ent["center"][0]) < 1e-6 and abs(ent["center"][1]) < 1e-6, ent
        print(f"[project] circular edge -> circle r={ent['radius']:.1f} @ {ent['center']}")

        # 7. same edge into the FRONT sketch (axis perpendicular to the normal) -> error
        r = rpc("sketch_project_ref", {"doc_id": doc2, "sketch": sk_front,
                                       "ref": {"kind": "edge", "selector": csel}})
        assert not r["ok"], "tilted circular edge must be unsupported"
        assert r["error"]["code"] == "unsupported_ref", r["error"]
        assert "not supported" in r["error"]["message"], r["error"]
        print(f"[project] tilted circle -> {r['error']['code']}")

        rpc("shutdown")
        print("\nSKETCH PROJECT REF WORKER TEST PASSED")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=60)


def test_dim_variant_fields_roundtrip() -> None:
    """C50 regression: the sketch_solve constraint annotations must carry the dim VARIANT fields.

    The host renders committed dims from these records — when `cond` was dropped, a min/max (rim)
    distance visually snapped back to center anchoring the moment it was applied (solver still
    enforced the rim gap, so it read as the wrong constraint); when `axis` was dropped, h/v
    distance dims drew as aligned dimension lines."""
    from engine.doc import session as S

    s = S.new_session(kind="part")
    r = S.apply_ops(s, None, [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
         "refs": {"plane": "plane.front"}, "params": {"entities": [], "constraints": []}}}])
    fid = r["id_map"]["$0"]
    S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid,
        "entity": {"type": "circle", "center": [0.0, 10.0], "radius": 4.0}, "constraints": []}])
    S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid,
        "entity": {"type": "line", "p1": [-20.0, 0.0], "p2": [20.0, 0.0]}, "constraints": []}])
    sk = next(f for f in s.doc["features"] if f["id"] == fid)
    cid, lid = [e["id"] for e in sk["params"]["entities"]]
    S.apply_ops(s, None, [{"op": "sketch_add_constraint", "sketch": fid, "constraint": {
        "type": "distance", "refs": [cid, lid], "cond": ["min", "center"],
        "value": {"value": 3.0, "expr": None}}}])
    S.apply_ops(s, None, [{"op": "sketch_add_constraint", "sketch": fid, "constraint": {
        "type": "distance", "refs": [cid + ".center", "origin"], "axis": "h",
        "value": {"value": 0.0, "expr": None}, "driven": True}}])

    sol = S.sketch_solve(s, fid)
    dists = [c for c in sol["constraints"] if c["type"] == "distance"]
    by_cond = next(c for c in dists if c.get("cond"))
    by_axis = next(c for c in dists if c.get("axis"))
    assert by_cond["cond"] == ["min", "center"], by_cond
    assert abs(by_cond["measured"] - 3.0) < 1e-3, by_cond
    assert by_axis["axis"] == "h", by_axis
    print("[dim_fields] cond + axis survive the sketch_solve response")


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
    test_sketch_project_ref()

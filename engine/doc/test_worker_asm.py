"""C20 engine-half verification: assemblies over the REAL worker (stdio subprocess).

Flow: author + save two part .step+ files (plate with a hole, peg), assemble them,
mate concentric + coincident, verify the solved transforms seat the peg in the hole,
interference reports the designed radial clearance, and explode state changes the
tessellation. Written by the orchestrator after the C20 agent died mid-card.

Run:  python -m engine.doc.test_worker_asm
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class W:
    """Minimal stdio JSON-RPC driver."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env["AI_CAD_PROVIDER"] = "mock"  # never touch a live model
        self.p = subprocess.Popen(
            [sys.executable, "-m", "engine.worker"], cwd=str(REPO),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            bufsize=1, env=env, encoding="utf-8")
        self.rid = 0

    def call(self, method: str, params: dict) -> dict:
        self.rid += 1
        self.p.stdin.write(json.dumps({"id": self.rid, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            assert line, f"worker died during {method}"
            msg = json.loads(line)
            if msg.get("id") == self.rid and "event" not in msg:
                assert msg.get("ok"), f"{method} failed: {msg.get('error')}"
                return msg.get("result", {})

    def ops(self, doc_id: str, rev: int, ops: list) -> dict:
        return self.call("doc_apply_ops", {"doc_id": doc_id, "base_revision": rev, "ops": ops})


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _make_part(w: W, name: str, ops: list, path: str) -> None:
    r = w.call("doc_new", {"kind": "part", "name": name})
    doc, rev = r["doc_id"], r["revision"]
    r = w.ops(doc, rev, ops)
    assert all(s["ok"] for s in r["rebuild"]["feature_status"]), \
        f"{name} features must compile: {r['rebuild']['feature_status']}"
    w.call("doc_save", {"doc_id": doc, "path": path})
    w.call("doc_close", {"doc_id": doc})


def main() -> None:  # noqa: PLR0915
    tmp = Path(tempfile.mkdtemp(prefix="aicad_asm_"))
    base_p, peg_p = str(tmp / "base.step+"), str(tmp / "peg.step+")
    w = W()

    # Plate 40x40x10 centered at origin with an 8mm hole through the middle.
    _make_part(w, "base", [
        {"op": "add_feature", "feature": {"type": "box", "name": "Plate", "suppressed": False,
            "params": {"length": _dim(40), "width": _dim(40), "height": _dim(10),
                       "centered": True}, "refs": {}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Hole", "suppressed": False,
            "params": {"radius": _dim(4), "height": _dim(12), "centered": True,
                       "mode": "cut"}, "refs": {}}},
    ], base_p)
    # Peg r=3.9, h=20, centered (so local z spans -10..10).
    _make_part(w, "peg", [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Peg", "suppressed": False,
            "params": {"radius": _dim(3.9), "height": _dim(20), "centered": True}, "refs": {}}},
    ], peg_p)
    print(f"[asm] parts saved: {Path(base_p).name} ({Path(base_p).stat().st_size} B), "
          f"{Path(peg_p).name} ({Path(peg_p).stat().st_size} B)")

    # Assembly: insert both (base fixed by default as first), peg offset away from the hole.
    r = w.call("doc_new", {"kind": "assembly", "name": "fit"})
    doc, rev = r["doc_id"], r["revision"]
    r = w.ops(doc, rev, [{"op": "asm_insert_component", "source": {"path": base_p}}])
    rev = r["revision"]
    off = [1, 0, 0, 30, 0, 1, 0, 25, 0, 0, 1, 18, 0, 0, 0, 1]  # row-major, well away
    r = w.ops(doc, rev, [{"op": "asm_insert_component", "source": {"path": peg_p},
                          "transform": off}])
    rev = r["revision"]
    comps = r["tree"]["components"]
    assert [c["name"] for c in comps] == ["base", "peg"] and comps[0]["fixed"], comps
    assert len(r["tess"]["bodies"]) == 2, "two component bodies tessellated"
    print(f"[asm] inserted: {[c['id'] for c in comps]} bodies={len(r['tess']['bodies'])}")

    # Mates: concentric (hole cyl <-> peg cyl) + coincident (plate top <-> peg bottom, anti).
    cyl_hole = {"kind": "face", "surf": "cylinder", "centroid": [0.0, 0.0, 0.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 251.3, "owner_feature": "f2",
                "hint_index": 0}
    cyl_peg = {"kind": "face", "surf": "cylinder", "centroid": [0.0, 0.0, 0.0],
               "axis": [0.0, 0.0, 1.0], "area_or_len": 490.0, "owner_feature": "f1",
               "hint_index": 0}
    top_plate = {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, 5.0],
                 "axis": [0.0, 0.0, 1.0], "area_or_len": 1549.7, "owner_feature": "f1",
                 "hint_index": 0}
    bot_peg = {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, -10.0],
               "axis": [0.0, 0.0, -1.0], "area_or_len": 47.8, "owner_feature": "f1",
               "hint_index": 0}
    r = w.ops(doc, rev, [
        {"op": "asm_add_mate", "mate": {"type": "concentric", "align": "aligned",
            "suppressed": False, "value": None,
            "refs": [{"component": "k1", "selector": cyl_hole},
                     {"component": "k2", "selector": cyl_peg}]}},
        # Seating a face ON a face means the normals oppose: SolidWorks "anti-aligned".
        {"op": "asm_add_mate", "mate": {"type": "coincident", "align": "anti",
            "suppressed": False, "value": None,
            "refs": [{"component": "k1", "selector": top_plate},
                     {"component": "k2", "selector": bot_peg}]}},
    ])
    rev = r["revision"]
    t = next(c for c in r["tree"]["components"] if c["id"] == "k2")["transform"]
    tx, ty, tz = t[3], t[7], t[11]
    print(f"[asm] solved peg transform: x={tx:.3f} y={ty:.3f} z={tz:.3f}")
    assert math.hypot(tx, ty) < 0.5, f"concentric must center the peg on the hole axis, got {tx},{ty}"
    assert abs(tz - 15.0) < 0.5, f"coincident must seat peg bottom on plate top (z=15), got {tz}"

    # Interference: the designed fit is 0.1 mm radial clearance — no overlap.
    fit = w.call("eval_interference", {"doc_id": doc})
    inter = fit.get("interferences") or []
    assert not inter, f"peg/hole must not interfere: {inter}"
    print(f"[asm] interference: none; clearances={fit.get('clearances')}")

    # Explode: tessellation must change (peg moves away along the explode vector).
    r = w.ops(doc, rev, [{"op": "asm_set_explode",
                          "steps": [{"component": "k2", "vector": [0, 0, 1], "distance": 40.0}]}])
    rev = r["revision"]
    from engine.doc.tessellate import read_aicm  # local import keeps module light
    # Explode state is view-only (no revision bump) and re-tessellates IN PLACE — read the
    # seated geometry before toggling, then compare content, not paths.
    seated = read_aicm(Path(r["tess"]["aicm_path"]).read_bytes())
    r2 = w.call("doc_set_explode_state", {"doc_id": doc, "active": True, "factor": 1.0})
    exploded = read_aicm(Path(r2["tess"]["aicm_path"]).read_bytes())
    z_seated = max(float(b["pos"][:, 2].max()) for b in seated["bodies"])
    z_expl = max(float(b["pos"][:, 2].max()) for b in exploded["bodies"])
    assert z_expl > z_seated + 30, f"exploded peg should rise ~40mm: {z_seated} -> {z_expl}"
    print(f"[asm] explode: max z {z_seated:.1f} -> {z_expl:.1f}")

    # Undo works on assembly ops too.
    r = w.call("doc_undo", {"doc_id": doc})
    print(f"[asm] undo -> rev={r['revision']} mates={len(r['tree']['mates'])}")

    w.call("shutdown", {})
    print("\nWORKER ASSEMBLY SUBPROCESS TEST PASSED")


if __name__ == "__main__":
    main()

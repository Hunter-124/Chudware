r"""C30-B — arc-slot sketch entity (arc-spine slot) end-to-end tests.

Run (repo root, mock provider):
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" -m pytest \
        engine\\doc\\test_sketch_arcslot.py -q

Covers:
  - solve_sketch round-trips a free arc_slot and reports honest DOF (6)
  - fix on center / coincident on spine endpoints drop DOF
  - width > diameter raises FeatureError cleanly from region construction
  - _entity_to_edges produces a closed 4-edge capsule loop
  - a single arc_slot profile extrudes to a solid whose volume matches the
    analytic capsule-swept area x depth (sweep*r*w + pi*(w/2)^2) within 2%

The first checks run in-process against the solver/compiler; the extrude check
drives the real subprocess worker (mirrors test_sketch_conics.py).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys

import pytest

from engine.doc.sketch_solver import solve_sketch
from engine.doc.features.sketch import _entity_to_edges
from engine.doc.features.base import FeatureError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _solve(entities, constraints):
    return solve_sketch({"entities": entities, "constraints": constraints}, {}, warm=None)


def _arc_slot(eid="e1", center=(0.0, 0.0), radius=40.0, a0=0.0, a1=90.0, width=10.0):
    return {"id": eid, "type": "arc_slot", "construction": False,
            "center": list(center), "radius": radius, "a0": a0, "a1": a1, "width": width}


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

def test_arc_slot_dof_and_roundtrip():
    ent = _arc_slot()
    sol = _solve([ent], [])
    assert sol.converged, sol
    assert sol.dof == 6, f"free arc_slot dof {sol.dof} != 6"
    assert sol.statuses["e1"] == "under"

    out = sol.entities["e1"]
    assert out["type"] == "arc_slot"
    assert abs(out["radius"] - 40.0) < 1e-6 and abs(out["a1"] - 90.0) < 1e-6
    assert abs(out["width"] - 10.0) < 1e-6


def test_arc_slot_fix_and_coincident_drop_dof():
    ent = _arc_slot()
    # fix the center -> removes 2 dof
    sol = _solve([ent], [{"id": "c1", "type": "fix", "refs": ["e1.center"]}])
    assert sol.dof == 4, f"arc_slot + center fix dof {sol.dof} != 4"

    # coincident the spine start with a line endpoint -> shared point, dof drops by 2.
    line = {"id": "e2", "type": "line", "construction": False,
            "p1": [40.0, 0.0], "p2": [80.0, 0.0]}
    sol2 = _solve([ent, line], [
        {"id": "c1", "type": "coincident", "refs": ["e1.start", "e2.p1"]},
    ])
    assert sol2.converged, sol2
    # 6 (arc_slot) + 4 (line) = 10 vars; one coincident = 2 independent residuals.
    assert sol2.dof == 8, f"coincident arc_slot+line dof {sol2.dof} != 8"


# ---------------------------------------------------------------------------
# region geometry
# ---------------------------------------------------------------------------

def test_arc_slot_capsule_edges():
    edges = _entity_to_edges(_arc_slot())
    # outer arc + end cap + inner arc + start cap = 4 edges forming a closed loop.
    assert len(edges) == 4, f"arc_slot capsule must be 4 edges, got {len(edges)}"


def test_arc_slot_width_exceeds_diameter():
    # radius - width/2 <= 0 must raise a clean FeatureError, not crash.
    bad = _arc_slot(radius=4.0, width=10.0)   # 4 - 5 < 0
    with pytest.raises(FeatureError, match="width exceeds diameter"):
        _entity_to_edges(bad)


# ---------------------------------------------------------------------------
# closed arc_slot profile -> extrude -> analytic volume (worker path)
# ---------------------------------------------------------------------------

def test_arc_slot_profile_extrudes():
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1)
    _idc = [0]

    def nid():
        _idc[0] += 1
        return _idc[0]

    def rpc(obj):
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed the pipe")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    # arc_slot: center (0,0), r=40, a0..a1 = 0..90 deg (sweep pi/2), width 10.
    r, sweep_deg, width = 40.0, 90.0, 10.0
    depth = 5.0
    sweep = math.radians(sweep_deg)
    area = sweep * r * width + math.pi * (width / 2.0) ** 2
    expect_vol = area * depth

    try:
        r0 = rpc({"id": nid(), "method": "ping", "params": {}})
        assert r0["ok"] and r0["result"]["protocol"] == 2, r0

        r0 = rpc({"id": nid(), "method": "doc_new", "params": {"kind": "part"}})
        assert r0["ok"], r0
        doc_id = r0["result"]["doc_id"]

        r0 = rpc({"id": nid(), "method": "doc_apply_ops",
                  "params": {"doc_id": doc_id, "base_revision": 0, "ops": [
                      {"op": "add_feature", "feature": {
                          "type": "sketch", "name": "Sketch1",
                          "refs": {"plane": "plane.top"},
                          "params": {"entities": [], "constraints": []}}},
                      {"op": "sketch_add_entity", "sketch": "$0",
                       "entity": {"type": "arc_slot", "center": [0.0, 0.0],
                                  "radius": r, "a0": 0.0, "a1": sweep_deg,
                                  "width": width}},
                  ]}})
        assert r0["ok"], r0
        sketch_fid = r0["result"]["id_map"]["$0"]

        # enter + solve sanity
        r0 = rpc({"id": nid(), "method": "sketch_enter",
                  "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r0["ok"], r0
        assert len(r0["result"]["entities"]) == 1, r0["result"]["entities"]

        r0 = rpc({"id": nid(), "method": "sketch_exit",
                  "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r0["ok"], r0
        base_rev = r0["result"]["revision"]

        # extrude the capsule region (centroid near spine midpoint angle = 45 deg).
        mid = math.radians((0.0 + sweep_deg) / 2.0)
        region_sel = {"kind": "region", "sketch": sketch_fid,
                      "centroid": [r * math.cos(mid), r * math.sin(mid)],
                      "hint_index": 0}
        extrude_op = {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss-Extrude1",
            "params": {"depth": {"value": depth, "expr": None},
                       "end": "blind", "mode": "boss"},
            "refs": {"profile": [region_sel]}}}
        r0 = rpc({"id": nid(), "method": "doc_apply_ops",
                  "params": {"doc_id": doc_id, "base_revision": base_rev,
                             "ops": [extrude_op]}})
        assert r0["ok"], r0
        fstat = r0["result"]["rebuild"]["feature_status"][-1]
        assert fstat["ok"], f"extrude failed: {fstat}"

        r0 = rpc({"id": nid(), "method": "eval_mass", "params": {"doc_id": doc_id}})
        assert r0["ok"], r0
        vol = r0["result"]["volume_mm3"]
        rel = abs(vol - expect_vol) / expect_vol
        assert rel < 0.02, (
            f"arc_slot volume {vol:.1f} vs analytic {expect_vol:.1f} (rel {rel:.4f} > 2%)")

        rpc({"id": nid(), "method": "shutdown", "params": {}})
        print(f"[arcslot] arc_slot extruded volume_mm3={vol:.1f} "
              f"(analytic {expect_vol:.1f}, rel {rel*100:.2f}%)")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=60)


def main() -> None:
    test_arc_slot_dof_and_roundtrip()
    test_arc_slot_fix_and_coincident_drop_dof()
    test_arc_slot_capsule_edges()
    test_arc_slot_width_exceeds_diameter()
    test_arc_slot_profile_extrudes()
    print("ALL ARC-SLOT SKETCH TESTS PASSED")


if __name__ == "__main__":
    main()

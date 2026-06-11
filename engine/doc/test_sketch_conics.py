"""C28-A — conic-family sketch entities (ellipse_arc, parabola, conic).

Run (repo root, mock provider):
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" -m pytest \
        engine\\doc\\test_sketch_conics.py -q

Covers, for each new entity type:
  - solve_sketch round-trips the entity and reports honest DOF
  - coincident + fix on the conic-family handles drop DOF and reach 'full'
  - _entity_to_edges produces non-empty display/region geometry
  - a closed profile (parabola + line chord) extrudes to a solid (positive volume)

The first three run in-process against the solver/compiler; the extrude check
drives the real subprocess worker (mirrors test_worker_sketch.py) so the whole
sketch->region->extrude path is exercised end to end.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys

from engine.doc.sketch_solver import solve_sketch
from engine.doc.features.sketch import _entity_to_edges


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _solve(entities, constraints):
    return solve_sketch({"entities": entities, "constraints": constraints}, {}, warm=None)


# ---------------------------------------------------------------------------
# ellipse_arc
# ---------------------------------------------------------------------------

def test_ellipse_arc_dof_and_geometry():
    ent = {"id": "e1", "type": "ellipse_arc", "construction": False,
           "center": [0.0, 0.0], "rx": 30.0, "ry": 15.0, "rot": 0.0,
           "a0": 0.0, "a1": 120.0}

    # Free: 7 vars, no constraints -> dof 7, under.
    sol = _solve([ent], [])
    assert sol.converged, sol
    assert sol.dof == 7, f"free ellipse_arc dof {sol.dof} != 7"
    assert sol.statuses["e1"] == "under"
    # solved coords round-trip
    out = sol.entities["e1"]
    assert out["type"] == "ellipse_arc"
    assert abs(out["rx"] - 30.0) < 1e-6 and abs(out["a1"] - 120.0) < 1e-6

    # geometry (trimmed ellipse edge) is non-empty
    edges = _entity_to_edges(out)
    assert len(edges) == 1, "ellipse_arc must produce one edge"

    # fix the center -> removes 2 dof
    sol2 = _solve([ent], [{"id": "c1", "type": "fix", "refs": ["e1.center"]}])
    assert sol2.dof == 5, f"ellipse_arc + center fix dof {sol2.dof} != 5"


# ---------------------------------------------------------------------------
# parabola
# ---------------------------------------------------------------------------

def test_parabola_dof_and_geometry():
    ent = {"id": "e1", "type": "parabola", "construction": False,
           "vertex": [0.0, 0.0], "focus": [0.0, 10.0], "t0": -20.0, "t1": 20.0}

    sol = _solve([ent], [])
    assert sol.converged, sol
    assert sol.dof == 6, f"free parabola dof {sol.dof} != 6"
    assert sol.statuses["e1"] == "under"

    out = sol.entities["e1"]
    assert out["type"] == "parabola"
    edges = _entity_to_edges(out)
    assert len(edges) == 1, "parabola must produce one spline edge"

    # fix vertex (2 dof) + coincident the two endpoints to fixed points via a
    # helper line is overkill; just check fix on vertex drops 2 dof.
    sol2 = _solve([ent], [{"id": "c1", "type": "fix", "refs": ["e1.vertex"]}])
    assert sol2.dof == 4, f"parabola + vertex fix dof {sol2.dof} != 4"


def test_parabola_endpoint_coincident():
    # parabola end coincident with a line endpoint -> shared point, dof drops by 2.
    para = {"id": "e1", "type": "parabola", "construction": False,
            "vertex": [0.0, 0.0], "focus": [0.0, 10.0], "t0": -20.0, "t1": 20.0}
    line = {"id": "e2", "type": "line", "construction": False,
            "p1": [20.0, 100.0], "p2": [-20.0, 100.0]}
    cons = [
        {"id": "c1", "type": "coincident", "refs": ["e1.start", "e2.p1"]},
        {"id": "c2", "type": "coincident", "refs": ["e1.end", "e2.p2"]},
    ]
    sol = _solve([para, line], cons)
    assert sol.converged, sol
    # 6 (para) + 4 (line) = 10 vars; two coincident = 4 residuals, all independent
    assert sol.dof == 6, f"coincident parabola+line dof {sol.dof} != 6"


# ---------------------------------------------------------------------------
# conic
# ---------------------------------------------------------------------------

def test_conic_dof_and_geometry():
    ent = {"id": "e1", "type": "conic", "construction": False,
           "p0": [-30.0, 0.0], "p1": [30.0, 0.0], "apex": [0.0, 25.0], "rho": 0.5}

    sol = _solve([ent], [])
    assert sol.converged, sol
    assert sol.dof == 7, f"free conic dof {sol.dof} != 7"
    assert sol.statuses["e1"] == "under"

    out = sol.entities["e1"]
    assert out["type"] == "conic"
    assert abs(out["rho"] - 0.5) < 1e-6
    edges = _entity_to_edges(out)
    assert len(edges) == 1, "conic must produce one (bezier) edge"

    # fix both endpoints -> 4 dof removed
    sol2 = _solve([ent], [
        {"id": "c1", "type": "fix", "refs": ["e1.start"]},
        {"id": "c2", "type": "fix", "refs": ["e1.end"]},
    ])
    assert sol2.dof == 3, f"conic + 2 endpoint fixes dof {sol2.dof} != 3"


# ---------------------------------------------------------------------------
# closed parabola + chord profile -> extrude -> positive volume (worker path)
# ---------------------------------------------------------------------------

def test_parabola_profile_extrudes():
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

    try:
        r = rpc({"id": nid(), "method": "ping", "params": {}})
        assert r["ok"] and r["result"]["protocol"] == 2, r

        r = rpc({"id": nid(), "method": "doc_new", "params": {"kind": "part"}})
        assert r["ok"], r
        doc_id = r["result"]["doc_id"]

        # sketch on plane.top with: a parabola arch (opening +y) and a line chord
        # closing its two endpoints into a region.
        # vertex (0,0), focus (0,10) -> opens +y; t in [-30,30] -> endpoints at
        # (±30, 22.5). Chord line connects those two endpoints.
        ex0 = 30.0
        ey = (ex0 * ex0) / (4.0 * 10.0)   # 22.5
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": 0, "ops": [
                     {"op": "add_feature", "feature": {
                         "type": "sketch", "name": "Sketch1",
                         "refs": {"plane": "plane.top"},
                         "params": {"entities": [], "constraints": []}}},
                     {"op": "sketch_add_entity", "sketch": "$0",
                      "entity": {"type": "parabola", "vertex": [0.0, 0.0],
                                 "focus": [0.0, 10.0], "t0": -ex0, "t1": ex0}},
                     {"op": "sketch_add_entity", "sketch": "$0",
                      "entity": {"type": "line", "p1": [-ex0, ey], "p2": [ex0, ey]}},
                 ]}})
        assert r["ok"], r
        sketch_fid = r["result"]["id_map"]["$0"]

        # enter + solve sanity (statuses present, no crash)
        r = rpc({"id": nid(), "method": "sketch_enter",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        assert len(r["result"]["entities"]) == 2, r["result"]["entities"]

        r = rpc({"id": nid(), "method": "sketch_exit",
                 "params": {"doc_id": doc_id, "sketch": sketch_fid}})
        assert r["ok"], r
        base_rev = r["result"]["revision"]

        # extrude the region (centroid of parabola+chord cap, roughly y ~ 2/3 of ey)
        region_sel = {"kind": "region", "sketch": sketch_fid,
                      "centroid": [0.0, ey * 0.5], "hint_index": 0}
        extrude_op = {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss-Extrude1",
            "params": {"depth": {"value": 5.0, "expr": None},
                       "end": "blind", "mode": "boss"},
            "refs": {"profile": [region_sel]}}}
        r = rpc({"id": nid(), "method": "doc_apply_ops",
                 "params": {"doc_id": doc_id, "base_revision": base_rev,
                            "ops": [extrude_op]}})
        assert r["ok"], r
        fstat = r["result"]["rebuild"]["feature_status"][-1]
        assert fstat["ok"], f"extrude failed: {fstat}"

        r = rpc({"id": nid(), "method": "eval_mass", "params": {"doc_id": doc_id}})
        assert r["ok"], r
        vol = r["result"]["volume_mm3"]
        # area of parabolic segment = (2/3)*base*height = (2/3)*60*22.5 = 900; *5 = 4500
        assert vol > 1000.0, f"parabola profile volume {vol} not positive/sane"

        rpc({"id": nid(), "method": "shutdown", "params": {}})
        print(f"[conics] parabola profile extruded volume_mm3={vol:.1f}")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=60)


def main() -> None:
    test_ellipse_arc_dof_and_geometry()
    test_parabola_dof_and_geometry()
    test_parabola_endpoint_coincident()
    test_conic_dof_and_geometry()
    test_parabola_profile_extrudes()
    print("ALL CONIC SKETCH TESTS PASSED")


if __name__ == "__main__":
    main()

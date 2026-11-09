"""shot_planner._edge_faces: which B-rep faces each tessellated edge polyline borders.

Run: python3 -m pytest services/mcp/test_shot_planner_edge_faces.py -q
"""
from __future__ import annotations

import numpy as np

from services.mcp import shot_planner as sp


# The six faces of the unit cube, each a 2x2 grid of quads with its OWN vertices (as in the
# display mesh, where a face does not share vertices with its neighbour; they only coincide
# in position along the shared edge discretisation).
_AXES = [(0, 0.0), (0, 1.0), (1, 0.0), (1, 1.0), (2, 0.0), (2, 1.0)]


def cube(body: int = 1, first_face_id: int = 1):
    pos, idx, tri_face, faces = [], [], [], []
    for f, (axis, level) in enumerate(_AXES):
        u, v = [a for a in range(3) if a != axis]
        base = len(pos)
        for i in range(3):
            for j in range(3):
                p = [0.0, 0.0, 0.0]
                p[axis], p[u], p[v] = level, i / 2.0, j / 2.0
                pos.append(p)
        first = len(idx)
        for i in range(2):
            for j in range(2):
                a = base + i * 3 + j
                idx += [[a, a + 1, a + 3], [a + 1, a + 4, a + 3]]
        faces.append({"body": body, "face_id": first_face_id + f, "first_tri": first,
                      "tri_count": 8, "surf_type": 0})
        tri_face += [f] * 8
    edges = []
    edge_id = 1
    for axis in range(3):
        u, v = [a for a in range(3) if a != axis]
        for ul in (0.0, 1.0):
            for vl in (0.0, 1.0):
                pts = []
                for t in (0.0, 0.5, 1.0):
                    p = [0.0, 0.0, 0.0]
                    p[axis], p[u], p[v] = t, ul, vl
                    pts.append(p)
                edges.append({"body": body, "edge_id": edge_id, "pts": np.asarray(pts)})
                edge_id += 1
    return {"pos": np.asarray(pos, dtype=np.float64), "idx": np.asarray(idx, dtype=np.int64),
            "tri_face": np.asarray(tri_face, dtype=np.int32), "faces": faces, "edges": edges}


def metrics_for(mesh: dict, body: int | None = None) -> dict:
    return {"faces": [{"body": f["body"] if body is None else body, "face_id": f["face_id"]}
                      for f in mesh["faces"]],
            "edges": [{"body": e["body"] if body is None else body, "edge_id": e["edge_id"]}
                      for e in mesh["edges"]]}


def edge_faces(mesh: dict, metrics: dict):
    return sp._edge_faces("doc", metrics, mesh=mesh)


def test_each_cube_edge_touches_exactly_its_two_faces():
    mesh = cube()
    got = edge_faces(mesh, metrics_for(mesh))
    assert all(len(faces) == 2 for faces in got)
    # Edges 0-3 run along X at (y, z) in {0,1}^2: they border the y and z faces.
    assert got[0] == [2, 4] and got[3] == [3, 5]


def test_positions_match_on_the_1e4_grid_like_np_round():
    mesh = cube()
    # Within a rounding cell of the vertices (and a signed zero): still the same position.
    mesh["edges"][0]["pts"] = mesh["edges"][0]["pts"] + 3e-5
    mesh["edges"][1]["pts"] = mesh["edges"][1]["pts"] - 3e-5
    mesh["edges"][2]["pts"] = mesh["edges"][2]["pts"] * 1.0 - 1e-9
    got = edge_faces(mesh, metrics_for(mesh))
    assert [len(got[i]) for i in range(3)] == [2, 2, 2]
    # One cell away: no vertex is there, so the edge touches nothing.
    mesh["edges"][3]["pts"] = mesh["edges"][3]["pts"] + 6e-5
    assert edge_faces(mesh, metrics_for(mesh))[3] == []


def test_engine_tables_numbered_differently_pair_bodies_positionally():
    mesh = cube(body=1)
    got = edge_faces(mesh, metrics_for(mesh, body=7))
    assert all(len(faces) == 2 for faces in got)


def test_unmatched_rows_are_ignored_not_misattributed():
    mesh = cube()
    metrics = metrics_for(mesh)
    del metrics["faces"][4:]                      # faces the engine never listed
    del metrics["edges"][5]                       # a tessellated edge with no engine row
    mesh["edges"].append({"body": 1, "edge_id": 99, "pts": np.zeros((3, 3))})
    mesh["edges"][0]["pts"] = np.zeros((0, 3))    # an engine edge whose polyline is empty
    got = edge_faces(mesh, metrics)
    assert got[0] == []


def test_no_faces_in_the_engine_answer():
    mesh = cube()
    metrics = {"faces": [], "edges": metrics_for(mesh)["edges"]}
    assert edge_faces(mesh, metrics) == [[] for _ in mesh["edges"]]


def test_no_polylines_at_all():
    mesh = cube()
    mesh["edges"] = []
    assert edge_faces(mesh, {"faces": metrics_for(cube())["faces"], "edges": []}) == []


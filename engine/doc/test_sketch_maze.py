"""Stress tests for ultra-complex hand-drawn sketches (the "line maze").

These reproduce what the host Line tool emits while a user rubber-bands a long chain
of segments — coincident chain joints, auto horizontal/vertical relations on near-axis
segments (the host forces those to EXACT axis at placement), endpoint snaps, and a few
tool switches (line -> circle -> tangent line -> line) mid-sketch. Some segments are
deliberately a hair off-axis so the auto-h/v forcing kicks in like it does interactively.

Two regressions are pinned here:

  1. PRE-SNAPPED RELATIONS PERSIST. Every relation the host emits for a snapped placement
     (coincident / horizontal / vertical / point_on / tangent) survives into the solved
     sketch — none are silently dropped.

  2. NO FALSE "OVER DEFINED". A maze of relations (especially a line drawn tangent to a
     circle, whose `tangent` our linearized rank test reports as redundant at the tangent
     point) must NOT read Over Defined. Only a redundant *driving dimension* or a genuine
     contradiction is Over Defined — verified by the two guard cases at the end.

Plain main()-runner with asserts (house style). Run from repo root:

    & $py -m engine.doc.test_sketch_maze
"""

from __future__ import annotations

import math

from engine.doc.sketch_solver import solve_sketch
from engine.doc.session import _overall_status

# Host inference thresholds (host/src/ui/sketch_mode.cpp: kHVDeg / kInferHVDeg).
kHVDeg = 5.0


# ---------------------------------------------------------------------------
# Host Line-tool emitter: turn a clicked polyline into the exact ops the host
# sends (entities + constraints), including auto-h/v forcing and endpoint snaps.
# ---------------------------------------------------------------------------

class SketchBuilder:
    """Accumulates entities + constraints the way the host applyOps stream does."""

    def __init__(self) -> None:
        self.entities: list = []
        self.constraints: list = []
        self._cid = 0
        self._eid = 0

    def _newc(self, **kw) -> str:
        self._cid += 1
        cid = f"c{self._cid}"
        kw["id"] = cid
        self.constraints.append(kw)
        return cid

    def _newe(self) -> str:
        self._eid += 1
        return f"e{self._eid}"

    def line(self, p1, p2, *, start_snap=None, end_snap=None, prev_id=None,
             infer_hv=True):
        """Emit one Line segment exactly like the host placement code.

        start_snap / end_snap: existing handle string to snap onto, e.g. "e3.p2"
        (coincident), ("point_on", "e2") for a rim, or ("tangent", "e2") for a
        rim contact that also pins direction (point_on + tangent), or None.
        prev_id: when chaining, the predecessor's id (p1 gets a chain coincident
        and start_snap is ignored, mirroring the host).
        """
        p1 = [float(p1[0]), float(p1[1])]
        p2 = [float(p2[0]), float(p2[1])]
        du, dv = p2[0] - p1[0], p2[1] - p1[1]

        # Auto h/v: the host forces the placed point to EXACT axis, then adds the relation.
        rel = None
        if infer_hv:
            ang = math.degrees(math.atan2(abs(dv), abs(du)))
            if ang < kHVDeg:
                rel = "horizontal"
                p2[1] = p1[1]
            elif ang > 90.0 - kHVDeg:
                rel = "vertical"
                p2[0] = p1[0]

        eid = self._newe()
        self.entities.append({"id": eid, "type": "line", "construction": False,
                              "p1": list(p1), "p2": list(p2)})

        # Start: chain coincident wins over a start snap (host behavior).
        if prev_id is not None:
            self._newc(type="coincident", refs=[f"{prev_id}.p2", f"{eid}.p1"])
        else:
            self._emit_snap(start_snap, f"{eid}.p1")
        if rel:
            self._newc(type=rel, refs=[eid])
        self._emit_snap(end_snap, f"{eid}.p2")
        return eid

    def circle(self, center, radius, *, fix_center=True, set_radius=True):
        eid = self._newe()
        self.entities.append({"id": eid, "type": "circle",
                              "center": [float(center[0]), float(center[1])],
                              "radius": float(radius)})
        if fix_center:
            self._newc(type="fix", refs=[f"{eid}.center"])
        if set_radius:
            self._newc(type="radius", refs=[eid],
                       value={"value": float(radius)}, driven=False)
        return eid

    def _emit_snap(self, snap, pt):
        if snap is None:
            return
        if isinstance(snap, tuple):
            kind, eid = snap
            if kind == "point_on":
                self._newc(type="point_on", refs=[pt, eid])
            elif kind == "tangent":
                # Host emits BOTH for a tangent rim snap.
                self._newc(type="point_on", refs=[pt, eid])
                self._newc(type="tangent", refs=[self.entities[-1]["id"], eid])
            else:
                raise ValueError(f"bad snap kind {kind}")
        else:
            self._newc(type="coincident", refs=[snap, pt])

    def sketch(self) -> dict:
        return {"entities": list(self.entities), "constraints": list(self.constraints)}


def _solve(b: SketchBuilder):
    sk = b.sketch()
    sol = solve_sketch(sk, {}, warm=None)
    cons_by_id = {c["id"]: c for c in sk["constraints"]}
    status = _overall_status(sol.statuses, sol.dof, sol.conflicting, sol.redundant,
                             cons_by_id)
    return sk, sol, status


def _types(sk) -> list:
    return [c["type"] for c in sk["constraints"]]


# ---------------------------------------------------------------------------
# 1) Open staircase: 16 near-axis segments, every one a hair off (forced h/v).
# ---------------------------------------------------------------------------

def test_open_staircase_not_overdefined() -> None:
    b = SketchBuilder()
    pts = [(0.0, 0.0)]
    x, y = 0.0, 0.0
    for k in range(8):
        x += 12.0 + (0.4 if k % 2 == 0 else -0.3)     # slightly-off horizontal
        pts.append((x, y))
        y += 9.0 + (0.35 if k % 2 else -0.2)          # slightly-off vertical
        pts.append((x, y))
    prev = None
    nseg = 0
    for i in range(len(pts) - 1):
        prev = b.line(pts[i], pts[i + 1], prev_id=prev)
        nseg += 1
    sk, sol, status = _solve(b)
    assert sol.converged, "staircase must converge"
    assert not sol.conflicting, f"no conflicts expected: {sol.conflicting}"
    assert status != "over", f"open staircase must not read Over Defined (got {status})"
    # Every near-axis segment kept its forced h/v relation.
    hv = [t for t in _types(sk) if t in ("horizontal", "vertical")]
    assert len(hv) == nseg, f"each of {nseg} segments should carry an h/v relation, got {len(hv)}"
    print(f"[staircase] {nseg} segments, status={status} dof={sol.dof} hv={len(hv)}")


# ---------------------------------------------------------------------------
# 2) Closed rectilinear Manhattan maze: trace it by hand, snap last -> first.
# ---------------------------------------------------------------------------

def test_closed_manhattan_maze_not_overdefined() -> None:
    b = SketchBuilder()
    pts = [(0, 0), (30.2, 0.1), (30.0, 10.1), (15.1, 10.0), (15.0, 20.3),
           (45.1, 20.0), (45.0, 35.2), (0.1, 35.0), (0.0, 0.2)]
    prev = None
    first_id = None
    ids = []
    for i in range(len(pts) - 1):
        end_snap = None
        if i == len(pts) - 2:
            end_snap = f"{first_id}.p1"      # close the loop onto the first vertex
        prev = b.line(pts[i], pts[i + 1], prev_id=prev, end_snap=end_snap)
        ids.append(prev)
        if first_id is None:
            first_id = prev
    sk, sol, status = _solve(b)
    assert sol.converged, "closed maze must converge"
    assert not sol.conflicting, f"closed maze should not conflict: {sol.conflicting}"
    assert status != "over", f"closed maze must not read Over Defined (got {status})"
    # The closing coincident persisted (relation kept).
    assert any(c["type"] == "coincident" and f"{first_id}.p1" in c["refs"]
               for c in sk["constraints"]), "closing coincident must be kept"
    print(f"[manhattan] {len(ids)} segments, status={status} dof={sol.dof} "
          f"redundant={sol.redundant}")


# ---------------------------------------------------------------------------
# 3) Tool switching mid-sketch: line chain -> circle -> tangent line -> chain.
#    The tangent line is the headline case: `tangent` comes back redundant in
#    our solver, but the sketch must NOT read Over Defined and the relation
#    must persist.
# ---------------------------------------------------------------------------

def test_tool_switch_tangent_not_overdefined() -> None:
    b = SketchBuilder()
    # a) draw a short open line chain
    prev = b.line((0, 0), (40.1, 0.0), prev_id=None)       # horizontal
    prev = b.line((40.0, 0.0), (40.0, 25.2), prev_id=prev)  # vertical
    # b) switch tool: drop a circle
    circ = b.circle((70.0, 12.0), 8.0)
    # c) switch tool: draw a line tangent to the circle (rim snap upgraded to tangent)
    tan_line = b.line((70.0, 4.0), (70.0, -20.0), prev_id=None,
                      start_snap=("tangent", circ), infer_hv=False)
    # d) switch back: another free segment
    b.line((70.0, -20.0), (30.0, -20.1), prev_id=tan_line)

    sk, sol, status = _solve(b)
    assert sol.converged, "tool-switch sketch must converge"
    assert not sol.conflicting, f"no genuine conflict expected: {sol.conflicting}"
    assert status != "over", (
        f"a tangent relation reported redundant must NOT make the sketch Over Defined "
        f"(got {status}, redundant={sol.redundant})")
    # The pre-snapped tangent relation persisted.
    assert any(c["type"] == "tangent" for c in sk["constraints"]), \
        "tangent relation must be kept after placing the line"
    assert any(c["type"] == "point_on" for c in sk["constraints"]), \
        "point_on (rim contact) must be kept too"
    print(f"[tool-switch] status={status} dof={sol.dof} redundant={sol.redundant} "
          f"types={sorted(set(_types(sk)))}")


# ---------------------------------------------------------------------------
# 4) GUARD: the fix must not mask a genuine over-define (redundant DRIVING dim).
# ---------------------------------------------------------------------------

def test_redundant_driving_dim_still_overdefined() -> None:
    b = SketchBuilder()
    e1 = b.line((0, 0), (80.0, 0.0), prev_id=None)
    e2 = b.line((80.0, 0.0), (80.0, 50.0), prev_id=e1)
    e3 = b.line((80.0, 50.0), (0.0, 50.0), prev_id=e2)
    b.line((0.0, 50.0), (0.0, 0.0), prev_id=e3, end_snap=f"{e1}.p1")
    b._newc(type="fix", refs=[f"{e1}.p1"])
    b._newc(type="distance", refs=[f"{e1}.p1", f"{e1}.p2"],
            value={"value": 80.0}, driven=False)
    b._newc(type="distance", refs=[f"{e2}.p1", f"{e2}.p2"],
            value={"value": 50.0}, driven=False)
    # duplicate driving width dimension -> redundant DRIVING dim -> Over Defined
    b._newc(type="distance", refs=[f"{e1}.p1", f"{e1}.p2"],
            value={"value": 80.0}, driven=False)
    sk, sol, status = _solve(b)
    assert status == "over", f"redundant driving dim must read Over Defined (got {status})"
    print(f"[guard:dup-dim] status={status} redundant={sol.redundant}")


# ---------------------------------------------------------------------------
# 5) GUARD: a genuine contradiction is still Over Defined + conflicting.
# ---------------------------------------------------------------------------

def test_conflicting_dim_still_overdefined() -> None:
    b = SketchBuilder()
    e1 = b.line((0, 0), (80.0, 0.0), prev_id=None)
    b._newc(type="fix", refs=[f"{e1}.p1"])
    b._newc(type="horizontal", refs=[e1])
    b._newc(type="distance", refs=[f"{e1}.p1", f"{e1}.p2"],
            value={"value": 80.0}, driven=False)
    # contradictory second length on the same segment
    b._newc(type="distance", refs=[f"{e1}.p1", f"{e1}.p2"],
            value={"value": 95.0}, driven=False)
    sk, sol, status = _solve(b)
    assert status == "over", f"contradiction must read Over Defined (got {status})"
    assert sol.conflicting, "contradiction must populate conflicting"
    print(f"[guard:conflict] status={status} conflicting={sol.conflicting}")


# ---------------------------------------------------------------------------
# 6) The precise degeneracy that motivated the fix: a line whose start is
#    point_on a circle rim AND tangent. Our linearized rank test reports the
#    `tangent` as redundant (its gradient coincides with point_on at the tangent
#    point), yet the sketch is well-posed/under-defined. It must NOT read Over
#    Defined, and the tangent relation is kept. (Before the fix this is exactly
#    what forced the host to drop pre-snapped tangent/parallel relations.)
# ---------------------------------------------------------------------------

def test_redundant_tangent_is_not_overdefined() -> None:
    sk = {
        "entities": [
            {"id": "e1", "type": "circle", "center": [0.0, 0.0], "radius": 10.0},
            {"id": "e2", "type": "line", "p1": [10.0, 0.0], "p2": [10.0, 30.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.center"]},
            {"id": "c2", "type": "radius", "refs": ["e1"],
             "value": {"value": 10.0}, "driven": False},
            {"id": "c3", "type": "point_on", "refs": ["e2.p1", "e1"]},
            {"id": "c4", "type": "tangent", "refs": ["e2", "e1"]},
        ],
    }
    sol = solve_sketch(sk, {}, warm=None)
    cons_by_id = {c["id"]: c for c in sk["constraints"]}
    status = _overall_status(sol.statuses, sol.dof, sol.conflicting, sol.redundant,
                             cons_by_id)
    assert "c4" in sol.redundant, \
        f"expected the tangent to be flagged redundant (the degeneracy), got {sol.redundant}"
    assert not sol.conflicting, "no genuine conflict"
    assert status != "over", \
        f"a redundant geometric relation must not over-define (got {status})"
    print(f"[redundant-tangent] redundant={sol.redundant} status={status} dof={sol.dof}")


# ---------------------------------------------------------------------------
# 7) Straight slot exactly as the host addDecomposedSlot emits it: 2 sides +
#    2 cap arcs + construction spine, 6 coincidents, equal radii, and the 4
#    restored side<->cap tangents. Must converge, stay under-defined (slot has
#    free position/angle/length/width), and NOT read Over Defined.
# ---------------------------------------------------------------------------

def test_straight_slot_relations_not_overdefined() -> None:
    hw = 5.0
    sk = {
        "entities": [
            {"id": "l1", "type": "line", "p1": [0.0, hw], "p2": [40.0, hw]},
            {"id": "l2", "type": "line", "p1": [0.0, -hw], "p2": [40.0, -hw]},
            {"id": "c1", "type": "arc", "center": [0.0, 0.0], "radius": hw,
             "start": 90.0, "end": -90.0, "ccw": True},
            {"id": "c2", "type": "arc", "center": [40.0, 0.0], "radius": hw,
             "start": -90.0, "end": 90.0, "ccw": True},
            {"id": "sp", "type": "line", "construction": True,
             "p1": [0.0, 0.0], "p2": [40.0, 0.0]},
        ],
        "constraints": [
            {"id": "k1", "type": "coincident", "refs": ["l1.p1", "c1.start"]},
            {"id": "k2", "type": "coincident", "refs": ["l1.p2", "c2.end"]},
            {"id": "k3", "type": "coincident", "refs": ["l2.p1", "c1.end"]},
            {"id": "k4", "type": "coincident", "refs": ["l2.p2", "c2.start"]},
            {"id": "k5", "type": "coincident", "refs": ["sp.p1", "c1.center"]},
            {"id": "k6", "type": "coincident", "refs": ["sp.p2", "c2.center"]},
            {"id": "k7", "type": "equal", "refs": ["c1", "c2"]},
            {"id": "t1", "type": "tangent", "refs": ["l1", "c1"]},
            {"id": "t2", "type": "tangent", "refs": ["l1", "c2"]},
            {"id": "t3", "type": "tangent", "refs": ["l2", "c1"]},
            {"id": "t4", "type": "tangent", "refs": ["l2", "c2"]},
        ],
    }
    sol = solve_sketch(sk, {}, warm=None)
    cons_by_id = {c["id"]: c for c in sk["constraints"]}
    status = _overall_status(sol.statuses, sol.dof, sol.conflicting, sol.redundant,
                             cons_by_id)
    assert sol.converged, "slot must converge"
    assert not sol.conflicting, f"slot tangents must not conflict: {sol.conflicting}"
    assert status != "over", f"freshly drawn slot must not read Over Defined (got {status})"
    assert sol.dof > 0, f"undimensioned slot should be under-defined, dof={sol.dof}"
    print(f"[slot] status={status} dof={sol.dof} redundant={sol.redundant}")


def main() -> None:
    test_open_staircase_not_overdefined()
    test_closed_manhattan_maze_not_overdefined()
    test_tool_switch_tangent_not_overdefined()
    test_redundant_tangent_is_not_overdefined()
    test_straight_slot_relations_not_overdefined()
    test_redundant_driving_dim_still_overdefined()
    test_conflicting_dim_still_overdefined()
    print("\nSKETCH MAZE TESTS PASSED")


if __name__ == "__main__":
    main()

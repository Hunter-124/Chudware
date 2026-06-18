"""cpp_parity.py — Python-engine vs C++-core (libaicad.dll) parity dashboard.

The strangler migration (docs/CPP_MIGRATION.md) cuts a feature over to the C++
core only once it matches the Python oracle. This is the offline gate that
measures that, fixture by fixture:

  - Python ORACLE : engine.doc.compiler.compile_document(doc) -> per-feature
                    status + body list (the production compiler).
  - C++  CANDIDATE: libaicad.dll aicad_call("doc_compile_cpp", {"doc": doc})
                    -> per-feature status + body_count + AICM bytes.

For each fixture it diffs per-feature ok and the surviving body count, and prints
a table. Run under the project venv (it imports the engine):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_parity.py \
        --dll core\build-occt\bin\Release\libaicad.dll

Exit code is the number of fixtures that did NOT fully match (0 = full parity),
so it doubles as a CI gate over whatever fixture set is in parity range.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- fixtures
# Part trees in the shape BOTH engines consume (mirrors core/tests/test_document.cpp).
# These exercise the ported C++ subset; as more features land, add fixtures here.

def _line(i, x1, y1, x2, y2):
    return {"id": i, "type": "line", "construction": False,
            "p1": [x1, y1], "p2": [x2, y2]}


def _circle(i, cx, cy, r):
    return {"id": i, "type": "circle", "construction": False,
            "center": [cx, cy], "radius": r}


def _rel(i, t, refs):
    return {"id": i, "type": t, "refs": refs}


def _dim(i, t, refs, value):
    return {"id": i, "type": t, "refs": refs,
            "value": {"value": value, "expr": None}, "driven": False}


def _rect(x0, y0, w, h):
    ents = [_line("e1", x0, y0, x0 + w, y0), _line("e2", x0 + w, y0, x0 + w, y0 + h),
            _line("e3", x0 + w, y0 + h, x0, y0 + h), _line("e4", x0, y0 + h, x0, y0)]
    cons = [_rel("c1", "coincident", ["e1.p2", "e2.p1"]),
            _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
            _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
            _rel("c4", "coincident", ["e4.p2", "e1.p1"]),
            _rel("c5", "horizontal", ["e1"]), _rel("c6", "horizontal", ["e3"]),
            _rel("c7", "vertical", ["e2"]), _rel("c8", "vertical", ["e4"]),
            _rel("c9", "fix", ["e1.p1"]),
            _dim("c10", "distance", ["e1.p1", "e1.p2"], w),
            _dim("c11", "distance", ["e2.p1", "e2.p2"], h)]
    return ents, cons


# Canonical schema (engine/doc/schema.py): a feature is {id, type, params:{...},
# refs:{slot: <selector|datum>}}. sketch.refs.plane = a reserved datum string
# ("plane.top"); extrude.refs.profile = a SEL_LIST of region selectors
# {"kind":"region","sketch":fid}. This is the shape the host actually sends and
# the C++ core must converge to (see docs/CPP_MIGRATION.md "schema convergence").
def _polyline(pts, closed=True, prefix="e"):
    # Lines connecting consecutive points, chained by coincident constraints; the
    # first point is fixed. Under-defined but solves to the drawn coordinates (both
    # engines), which is all a profile/contour needs.
    ents, cons = [], []
    n = len(pts)
    segs = n if closed else n - 1
    for i in range(segs):
        a, b = pts[i], pts[(i + 1) % n]
        ents.append(_line(f"{prefix}{i}", a[0], a[1], b[0], b[1]))
    for i in range(segs - 1):
        cons.append(_rel(f"k{i}", "coincident", [f"{prefix}{i}.p2", f"{prefix}{i+1}.p1"]))
    if closed:
        cons.append(_rel("kc", "coincident", [f"{prefix}{segs-1}.p2", f"{prefix}0.p1"]))
    cons.append(_rel("fx", "fix", [f"{prefix}0.p1"]))
    return ents, cons


def _sketch(fid, plane_datum, ents, cons):
    return {"id": fid, "type": "sketch",
            "params": {"entities": ents, "constraints": cons},
            "refs": {"plane": plane_datum}}


def _extrude(fid, sketch_fid, depth=None, mode="new", end="blind", merge=None):
    # depth is a canonical dim object {value,expr} (exercises num_or_expr dim handling).
    params = {"mode": mode}
    if merge is not None:
        params["merge"] = merge
    if end == "through_all":
        params["end"] = "through_all"
    else:
        params["depth"] = {"value": depth, "expr": None}
    return {"id": fid, "type": "extrude", "params": params,
            "refs": {"profile": [{"kind": "region", "sketch": sketch_fid}]}}


def _revolve(fid, sketch_fid, axis_datum, angle_deg, mode="boss", merge=None):
    params = {"mode": mode, "angle": {"value": angle_deg, "expr": None}}
    if merge is not None:
        params["merge"] = merge
    return {"id": fid, "type": "revolve", "params": params,
            "refs": {"profile": [{"kind": "region", "sketch": sketch_fid}], "axis": axis_datum}}


def _plane_offset(fid, base_datum, offset):
    return {"id": fid, "type": "plane",
            "params": {"method": "offset", "offset": {"value": offset, "expr": None}},
            "refs": {"base": base_datum}}


def _loft(fid, section_fids):
    return {"id": fid, "type": "loft", "params": {"mode": "boss"},
            "refs": {"sections": [{"kind": "region", "sketch": s} for s in section_fids]}}


def _boolean(fid, op, target_fid, tool_fid):
    # op is the canonical name (combine/subtract/intersect); targets/tools are body
    # selectors carrying owner_feature (the earlier feature that built that body).
    return {"id": fid, "type": "boolean", "params": {"op": op},
            "refs": {"targets": [{"kind": "body", "owner_feature": target_fid}],
                     "tools": [{"kind": "body", "owner_feature": tool_fid}]}}


def fixtures():
    out = {}

    ents, cons = _rect(0, 0, 30, 20)
    out["box"] = {"features": [
        _sketch("s1", "plane.top", ents, cons), _extrude("x1", "s1", 25.0)]}

    out["cylinder"] = {"features": [
        _sketch("s1", "plane.top", [_circle("e1", 0, 0, 10)],
                [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 10)]),
        _extrude("x1", "s1", 40.0)]}

    re, rc = _rect(0, 0, 40, 40)
    ce = [_circle("h1", 20, 20, 8)]
    cc = [_dim("hc", "radius", ["h1"], 8)]
    out["washer"] = {"features": [
        _sketch("s1", "plane.top", re + ce, rc + cc), _extrude("x1", "s1", 10.0)]}

    # revolve: rect (x 5..15, y 0..10) on XY revolved 360 about Y -> annulus
    # r=5,R=15,h=10, V = pi(R^2-r^2)h = 2000*pi.
    rev_e, rev_c = _rect(5, 0, 10, 10)
    out["revolve"] = {"features": [
        _sketch("s1", "plane.top", rev_e, rev_c), _revolve("r1", "s1", "axis.y", 360.0)]}

    # revolve boss MERGE onto an existing body (P55m): the revolve mode=boss (merge
    # default True) must FUSE into the running body the way the oracle does — NOT add a
    # second body. Base = a circle r=10 extruded 10 (cylinder along +Z, V=1000*pi),
    # then a rect x[0,15] y[0,20] revolved 360 about axis.y -> a r=15 cylinder along +Y
    # that OVERLAPS the base; the boss fuses into it -> ONE body. The union volume is the
    # SAME OCCT boolean on both engines, so the total volume + body count (1) is the gate.
    rbm_e, rbm_c = _rect(0, 0, 15, 20)
    out["revolve_boss_merge"] = {"features": [
        _sketch("sb", "plane.top", [_circle("e1", 0, 0, 10)],
                [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 10)]),
        _extrude("bx", "sb", 10.0),
        _sketch("s1", "plane.top", rbm_e, rbm_c), _revolve("r1", "s1", "axis.y", 360.0)]}

    # revolve CUT into an existing body (P55m): base = circle r=15 extruded 20 (cylinder
    # along +Z), then a rect x[0,5] y[0,20] revolved 360 about axis.y -> a r=5 cylinder
    # along +Y that bores through the base; mode=cut subtracts it -> ONE body, volume
    # reduced by the same OCCT boolean on both engines. Gates the cut->subtract path.
    rcu_e, rcu_c = _rect(0, 0, 5, 20)
    out["revolve_cut"] = {"features": [
        _sketch("sb", "plane.top", [_circle("e1", 0, 0, 15)],
                [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 15)]),
        _extrude("bx", "sb", 20.0),
        _sketch("s1", "plane.top", rcu_e, rcu_c),
        _revolve("r1", "s1", "axis.y", 360.0, mode="cut")]}

    # revolve mode=new / merge=False -> a SEPARATE body (regression guard for the
    # standalone path): same base cylinder, then the same boss but merge:false keeps it a
    # distinct body -> TWO bodies. Total volume = base + the standalone revolve (disjoint
    # sum is the SAME on both engines).
    rnb_e, rnb_c = _rect(20, 0, 10, 20)  # x[20,30]: a r=20..30 annulus along +Y, disjoint from the r=10 base
    out["revolve_new_body"] = {"features": [
        _sketch("sb", "plane.top", [_circle("e1", 0, 0, 10)],
                [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 10)]),
        _extrude("bx", "sb", 10.0),
        _sketch("s1", "plane.top", rnb_e, rnb_c),
        _revolve("r1", "s1", "axis.y", 360.0, merge=False)]}

    # boolean: two 20x20x10 boxes overlapping by 10x20x10=2000; merge:false keeps
    # each a separate body so the boolean has two operands.
    ae, ac = _rect(0, 0, 20, 20)
    be, bc = _rect(10, 0, 20, 20)
    def _two_box_then(op):
        return {"features": [
            _sketch("sa", "plane.top", ae, ac), _extrude("ba", "sa", 10.0, merge=False),
            _sketch("sb", "plane.top", be, bc), _extrude("bb", "sb", 10.0, merge=False),
            _boolean("bo", op, "ba", "bb")]}
    out["boolean_combine"] = _two_box_then("combine")     # V = 6000
    out["boolean_subtract"] = _two_box_then("subtract")   # V = 2000
    out["boolean_intersect"] = _two_box_then("intersect")  # V = 2000

    # extrude cut: 20x20x10 box, then a 10x10 inner profile cut through_all -> V=3000.
    ce2, cc2 = _rect(0, 0, 20, 20)
    ie, ic = _rect(5, 5, 10, 10)
    out["extrude_cut"] = {"features": [
        _sketch("sc", "plane.top", ce2, cc2), _extrude("bc", "sc", 10.0),
        _sketch("si", "plane.top", ie, ic), _extrude("xc", "si", mode="cut", end="through_all")]}

    # ---- selector-driven features (fillet/chamfer/hole/mirror) on a 40x30x20 box ----
    # The edge/face selectors carry a geometric fingerprint that resolves on BOTH
    # engines (same scored matcher); the base geometry fixes the centroids below.
    def _sel_base():
        e, c = _rect(0, 0, 40, 30)
        return [_sketch("s1", "plane.top", e, c), _extrude("f2", "s1", 20.0)]
    edge_sel = {"kind": "edge", "surf": "line", "centroid": [0.0, 0.0, 10.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 20.0,
                "owner_feature": None, "hint_index": 0}
    face_sel = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0,
                "owner_feature": None, "hint_index": 5}

    out["fillet"] = {"features": _sel_base() + [
        {"id": "f3", "type": "fillet", "params": {"radius": {"value": 3, "expr": None}},
         "refs": {"edges": [edge_sel]}}]}
    out["chamfer"] = {"features": _sel_base() + [
        {"id": "f3", "type": "chamfer", "params": {"length": {"value": 3, "expr": None}},
         "refs": {"edges": [edge_sel]}}]}
    out["hole"] = {"features": _sel_base() + [
        {"id": "f3", "type": "hole",
         "params": {"style": "simple", "end": "through_all", "standard": "custom",
                    "diameter": {"value": 6, "expr": None}},
         "refs": {"face": face_sel, "points": [[0, 0]]}}]}
    # mirror about the reserved YZ plane (x=0); box touches x=0 -> contiguous, V doubles.
    out["mirror"] = {"features": _sel_base() + [
        {"id": "f3", "type": "mirror", "params": {}, "refs": {"plane": "plane.right"}}]}

    # shell: hollow the box to a 2mm wall, open the top face -> 1 body.
    out["shell"] = {"features": _sel_base() + [
        {"id": "f3", "type": "shell", "params": {"thickness": {"value": 2, "expr": None}},
         "refs": {"remove": [face_sel]}}]}
    # draft: taper the +X face 10 degrees about the XY (plane.top) neutral -> 1 body.
    xface_sel = {"kind": "face", "surf": "plane", "centroid": [40.0, 15.0, 10.0],
                 "axis": [1.0, 0.0, 0.0], "area_or_len": 600.0,
                 "owner_feature": None, "hint_index": 0}
    out["draft"] = {"features": _sel_base() + [
        {"id": "f3", "type": "draft", "params": {"angle": {"value": 10, "expr": None}},
         "refs": {"neutral": "plane.top", "faces": [xface_sel]}}]}

    # linear pattern: 3 abutting 10-cubes along +X -> contiguous, V=3000.
    le, lc = _rect(0, 0, 10, 10)
    out["linear_pattern"] = {"features": [
        _sketch("s1", "plane.top", le, lc), _extrude("f2", "s1", 10.0),
        {"id": "f3", "type": "linear_pattern",
         "params": {"count1": {"value": 3, "expr": None}, "spacing1": {"value": 10, "expr": None}},
         "refs": {"dir1": "axis.x", "features": ["f2"]}}]}
    # circular pattern: a cube offset +X, 4 copies about Z over 360 degrees.
    qe, qc = _rect(20, -5, 10, 10)
    out["circular_pattern"] = {"features": [
        _sketch("s1", "plane.top", qe, qc), _extrude("f2", "s1", 10.0),
        {"id": "f3", "type": "circular_pattern",
         "params": {"count": {"value": 4, "expr": None}, "angle": {"value": 360, "expr": None}},
         "refs": {"axis": "axis.z", "features": ["f2"]}}]}

    # loft: 20x20 section on XY -> 10x10 section on an offset plane 40 above ->
    # square frustum, V = h/3*(A1+A2+sqrt(A1*A2)) = 40/3*(400+100+200) = 9333.33.
    lo_e, lo_c = _rect(-10, -10, 20, 20)
    hi_e, hi_c = _rect(-5, -5, 10, 10)
    out["loft"] = {"features": [
        _plane_offset("f1", "plane.top", 40.0),
        _sketch("f2", "plane.top", lo_e, lo_c),
        _sketch("f3", "f1", hi_e, hi_c),
        _loft("f4", ["f2", "f3"])]}

    # sweep: a circle (r=5) on XY swept along a +Z line of length 40 -> cylinder,
    # V = pi*25*40 = 1000*pi. Path is an OPEN sketch (single fixed line) on plane.front.
    prof_e = [_circle("e1", 0, 0, 5)]
    prof_c = [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 5)]
    path_e = [_line("e1", 0, 0, 0, 40)]
    path_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    out["sweep"] = {"features": [
        _sketch("s1", "plane.top", prof_e, prof_c),
        _sketch("s2", "plane.front", path_e, path_c),
        {"id": "f3", "type": "sweep", "params": {"mode": "boss"},
         "refs": {"profile": [{"kind": "region", "sketch": "s1"}],
                  "path": {"kind": "region", "sketch": "s2"}}}]}

    # rib: a U-channel (closed U cross-section on plane.front, extruded 20), then an
    # open line bridging the notch grown into a rib wall. Volume must INCREASE (the
    # geometric volume gate makes this a real test of the rib geometry).
    u_pts = [(0, 0), (40, 0), (40, 30), (30, 30), (30, 10), (10, 10), (10, 30), (0, 30)]
    u_e, u_c = _polyline(u_pts, closed=True, prefix="u")
    rib_e, rib_c = _polyline([(10, 20), (30, 20)], closed=False, prefix="r")
    out["rib"] = {"features": [
        _sketch("f1", "plane.front", u_e, u_c), _extrude("f2", "f1", 20.0),
        _sketch("f3", "plane.front", rib_e, rib_c),
        {"id": "f4", "type": "rib", "params": {"thickness": {"value": 4, "expr": None}},
         "refs": {"profile": [{"kind": "region", "sketch": "f3"}]}}]}

    # base primitives (centered on origin).
    def _prim(fid, ptype, params):
        return {"id": fid, "type": ptype,
                "params": {k: {"value": v, "expr": None} for k, v in params.items()}}
    out["prim_box"] = {"features": [_prim("f1", "box", {"length": 30, "width": 20, "height": 10})]}
    out["prim_cylinder"] = {"features": [_prim("f1", "cylinder", {"radius": 10, "height": 25})]}
    out["prim_sphere"] = {"features": [_prim("f1", "sphere", {"radius": 12})]}
    out["prim_cone"] = {"features": [_prim("f1", "cone", {"r1": 10, "r2": 4, "height": 20})]}
    out["prim_torus"] = {"features": [_prim("f1", "torus", {"major_r": 20, "minor_r": 5})]}

    # ---- sheet metal (engine/doc/features/sheetmetal.py + sheetmetal_flat.py) ----
    # A 60x40 base sheet (thickness 2, k 0.45) -> V = 4800. Bend ops fold a wall off
    # edge[2] of the base sheet: the long straight edge at (30,0,0) along +X (the y=0
    # thickness face's bottom boundary). The edge selector carries the geometric
    # fingerprint + hint_index=2 so BOTH engines' scored matcher resolves it to that
    # SAME unique edge (the describe-space duplicate co-ranking is broken by the hint).
    sm_e, sm_c = _rect(0, 0, 60, 40)

    def _base_flange(fid, sketch_fid, thickness=2.0, k=0.45):
        return {"id": fid, "type": "sm_base_flange",
                "params": {"thickness": {"value": thickness, "expr": None},
                           "k_factor": {"value": k, "expr": None}},
                "refs": {"profile": [{"kind": "region", "sketch": sketch_fid}]}}

    sm_edge_sel = {"kind": "edge", "surf": "line", "centroid": [30.0, 0.0, 0.0],
                   "axis": [1.0, 0.0, 0.0], "area_or_len": 60.0,
                   "owner_feature": None, "hint_index": 2}

    # base flange alone: V = 60*40*2 = 4800.
    out["sm_base_flange"] = {"features": [
        _sketch("s1", "plane.top", sm_e, sm_c), _base_flange("b1", "s1")]}

    # base + 90 degree edge flange (wall 20, radius 2, material_inside): V = 7470.563.
    sm_edge_flange = {"id": "e1", "type": "sm_edge_flange",
                      "params": {"angle": {"value": 90.0, "expr": None},
                                 "length": {"value": 20.0, "expr": None},
                                 "radius": {"value": 2.0, "expr": None},
                                 "position": "material_inside", "flip": False,
                                 "corner_relief": "rectangular"},
                      "refs": {"edge": sm_edge_sel}}
    out["sm_edge_flange"] = {"features": [
        _sketch("s1", "plane.top", sm_e, sm_c), _base_flange("b1", "s1"), sm_edge_flange]}

    # base + closed hem (fold the edge 180 back on itself): V = 6752.605.
    sm_hem = {"id": "h1", "type": "sm_hem", "params": {"type": "closed", "flip": False},
              "refs": {"edge": sm_edge_sel}}
    out["sm_hem"] = {"features": [
        _sketch("s1", "plane.top", sm_e, sm_c), _base_flange("b1", "s1"), sm_hem]}

    # base + jog (two 90 bends -> a parallel offset step): V = 9048.907.
    sm_jog = {"id": "j1", "type": "sm_jog",
              "params": {"angle": {"value": 90.0, "expr": None},
                         "offset": {"value": 10.0, "expr": None},
                         "length": {"value": 20.0, "expr": None},
                         "radius": {"value": 2.0, "expr": None},
                         "position": "material_inside", "flip": False},
              "refs": {"edge": sm_edge_sel}}
    out["sm_jog"] = {"features": [
        _sketch("s1", "plane.top", sm_e, sm_c), _base_flange("b1", "s1"), sm_jog]}

    # base + edge flange, then UNFOLD to the flat pattern (developed blank). The flat
    # solid's volume is its OWN construction (base block + developed strip BA+wall),
    # NOT the folded volume: V = 7266.637.
    sm_flat = {"id": "fp1", "type": "sm_flat_pattern", "params": {}, "refs": {}}
    out["sm_flat_pattern"] = {"features": [
        _sketch("s1", "plane.top", sm_e, sm_c), _base_flange("b1", "s1"),
        sm_edge_flange, sm_flat]}

    # ---- weldments (engine/doc/features/weldment.py + engine/doc/weld_profiles.py) ----
    # A weld_member sweeps a structural profile perpendicular to each sketch segment,
    # one SEPARATE member body per segment (an assembly). The C++ port does NOT trim
    # shared corners (miter/butt/cope), so fixtures use members that do NOT share a
    # vertex — each member's volume is then the exact swept prism area*length, the
    # robust geometry gate. Profiles are built identically in both engines (hollow
    # tubes subtract the bore), so the cross-section area — hence volume — matches.

    def _weld_member(fid, sketch_fid, profile, dims, segments=None, justify="center",
                     angle=None, trim_corners=False):
        # Profile dimension keys (size/thickness/w/...) are read RAW by the oracle's
        # ProfileSpec.resolve (float(supplied[k])) — they are NOT compiler-core dim
        # objects, so pass them as bare numbers (the C++ jnum reads both shapes).
        params = {"profile": profile, "justify": justify, "trim_corners": trim_corners}
        params.update(dims)
        if angle is not None:
            params["angle"] = {"value": angle, "expr": None}
        refs = {"sketch": sketch_fid}
        if segments is not None:
            refs["segments"] = segments
        return {"id": fid, "type": "weld_member", "params": params, "refs": refs}

    # single square-tube member along ONE horizontal line of length 100 on plane.top.
    # square_tube size=20 t=2 -> area = 20^2 - 16^2 = 144 ; V = 144*100 = 14400.
    wm1_e = [_line("e1", 0, 0, 100, 0)]
    wm1_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    out["weld_member_single"] = {"features": [
        _sketch("s1", "plane.top", wm1_e, wm1_c),
        _weld_member("m1", "s1", "square_tube", {"size": 20.0, "thickness": 2.0})]}

    # member GROUP: three PARALLEL non-touching flat-bar members (no shared vertex ->
    # no trim). flat_bar w=40 t=6 -> area=240 ; three lines of length 80 -> 3 bodies,
    # V = 3*240*80 = 57600.
    wm2_e = [_line("e1", 0, 0, 80, 0), _line("e2", 0, 20, 80, 20), _line("e3", 0, 40, 80, 40)]
    wm2_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"]),
             _rel("f3", "fix", ["e2.p1"]), _rel("f4", "fix", ["e2.p2"]),
             _rel("f5", "fix", ["e3.p1"]), _rel("f6", "fix", ["e3.p2"])]
    out["weld_member_group"] = {"features": [
        _sketch("s1", "plane.top", wm2_e, wm2_c),
        _weld_member("m1", "s1", "flat_bar", {"w": 40.0, "thickness": 6.0})]}

    # round-tube member (exercises the circular profile + bore). od=30 t=3 ->
    # area = pi*(15^2-12^2) = pi*81 ; line length 120 -> V = pi*81*120 = 9720*pi.
    wm3_e = [_line("e1", 0, 0, 120, 0)]
    wm3_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"])]
    out["weld_member_round"] = {"features": [
        _sketch("s1", "plane.top", wm3_e, wm3_c),
        _weld_member("m1", "s1", "round_tube", {"od": 30.0, "thickness": 3.0})]}

    # ---- end cap: a plate covering the open end of a single square-tube member ----
    # The end face is at the member's far end (x=100); its outward normal is +X.
    # 'outer' cap covers the whole 20x20 end -> plate V = 400*thickness = 400*5 = 2000.
    # The member volume (144*100=14400) is unchanged; total = 16400, 2 bodies.
    cap_face = {"kind": "face", "surf": "plane", "centroid": [100.0, 0.0, 0.0],
                "axis": [1.0, 0.0, 0.0], "area_or_len": 144.0,
                "owner_feature": None, "hint_index": 0}
    out["weld_end_cap"] = {"features": [
        _sketch("s1", "plane.top", wm1_e, wm1_c),
        _weld_member("m1", "s1", "square_tube", {"size": 20.0, "thickness": 2.0}),
        {"id": "c1", "type": "weld_end_cap",
         "params": {"type": "outer", "thickness": {"value": 5.0, "expr": None}},
         "refs": {"face": cap_face}}]}

    # ---- gusset: a triangular plate in the L-corner of two perpendicular members ----
    # Two flat-bar members sharing a corner at the origin: one along +X, one along +Y.
    # We pick the TOP face (+Z) of each member; the gusset is built in the +Z plane.
    # d1=d2=30 -> right-triangle legs 30 -> tri area 450 ; V = 450*thickness = 450*4 = 1800.
    gus_e = [_line("e1", 0, 0, 100, 0), _line("e2", 0, 0, 0, 100)]
    gus_c = [_rel("f1", "fix", ["e1.p1"]), _rel("f2", "fix", ["e1.p2"]),
             _rel("f3", "fix", ["e2.p2"])]
    # The two members' coplanar +Z "top" faces (normal +Z), the faces facing up out
    # of the L-corner. flat_bar w=40 t=6 swept along the segment orients the 40-wide
    # profile dimension along Z, so each member's +Z face spans z=+20, area 600
    # (length 100 x half-width 6): the +X member's at centroid (50,0,20), the +Y
    # member's at (0,50,20). Their longest edge (along the member axis, length 100)
    # gives the axis the gusset apex/legs are built from.
    gtop1 = {"kind": "face", "surf": "plane", "centroid": [50.0, 0.0, 20.0],
             "axis": [0.0, 0.0, 1.0], "area_or_len": 600.0,
             "owner_feature": None, "hint_index": 0}
    gtop2 = {"kind": "face", "surf": "plane", "centroid": [0.0, 50.0, 20.0],
             "axis": [0.0, 0.0, 1.0], "area_or_len": 600.0,
             "owner_feature": None, "hint_index": 0}
    out["weld_gusset"] = {"features": [
        _sketch("s1", "plane.top", gus_e, gus_c),
        _weld_member("m1", "s1", "flat_bar", {"w": 40.0, "thickness": 6.0}),
        {"id": "g1", "type": "weld_gusset",
         "params": {"type": "triangular", "thickness": {"value": 4.0, "expr": None},
                    "d1": {"value": 30.0, "expr": None}, "d2": {"value": 30.0, "expr": None}},
         "refs": {"faces": [gtop1, gtop2]}}]}

    # ---- corner trim: two members meeting at an L-corner (miter / butt / cope) ----
    # Two square-tube members sharing the vertex at the origin: one along +X (len 100),
    # one along +Y (len 100). square_tube size=20 t=2 -> area 144, untrimmed each
    # V=14400 (sum 28800). The trim removes the corner overlap so the oracle's TOTAL
    # volume drops below 28800; the gate compares the C++ total volume to that exact
    # oracle value (OCCT-version-independent: same kernel boolean both engines). Body
    # count stays 2 (members are separate bodies). The no-trim case is the regression
    # guard: trim_corners=False must keep the full 28800.
    corner_e = [_line("e1", 0, 0, 100, 0), _line("e2", 0, 0, 0, 100)]
    corner_c = [_rel("f1", "fix", ["e1.p2"]), _rel("f2", "fix", ["e2.p2"]),
                _rel("f3", "coincident", ["e1.p1", "e2.p1"])]
    out["weld_corner_notrim"] = {"features": [
        _sketch("s1", "plane.top", corner_e, corner_c),
        _weld_member("m1", "s1", "square_tube", {"size": 20.0, "thickness": 2.0},
                     trim_corners=False)]}
    # miter (default joint): both members cut by the bisecting mitre plane.
    out["weld_corner_miter"] = {"features": [
        _sketch("s1", "plane.top", corner_e, corner_c),
        _weld_member("m1", "s1", "square_tube", {"size": 20.0, "thickness": 2.0},
                     trim_corners=True)]}
    # butt: the (equal-length -> first/longer) through member stays full, the other is
    # square-cut against the through member's near face.
    out["weld_corner_butt"] = {"features": [
        _sketch("s1", "plane.top", corner_e, corner_c),
        {"id": "m1", "type": "weld_member",
         "params": {"profile": "square_tube", "justify": "center", "trim_corners": True,
                    "joint": "butt", "size": 20.0, "thickness": 2.0},
         "refs": {"sketch": "s1"}}]}
    # cope: the trimmed member's end is cut to the through member's contour (boolean
    # subtract of the through solid).
    out["weld_corner_cope"] = {"features": [
        _sketch("s1", "plane.top", corner_e, corner_c),
        {"id": "m1", "type": "weld_member",
         "params": {"profile": "square_tube", "justify": "center", "trim_corners": True,
                    "joint": "cope", "size": 20.0, "thickness": 2.0},
         "refs": {"sketch": "s1"}}]}

    # ---- split (engine/doc/features/split.py, planar tool path) ------------------
    # A 30x20x25 box (V=15000) split by a datum plane (plane.top offset 10). keep=both
    # -> TWO solids (the z[0,10] piece 6000 + the z[10,25] piece 9000, total 15000, the
    # planar split's defining property: pieces sum to the original). keep=above keeps
    # only the +normal (z>10) piece, V=9000. Gated on exact per-body + total volume.
    sp_e, sp_c = _rect(0, 0, 30, 20)
    def _split_base():
        return [_sketch("s1", "plane.top", sp_e, sp_c), _extrude("x1", "s1", 25.0),
                _plane_offset("p1", "plane.top", 10.0)]
    def _split(keep):
        return {"id": "sp", "type": "split", "params": {"keep": keep},
                "refs": {"tool": {"kind": "datum", "feature": "p1"}}}
    out["split_both"] = {"features": _split_base() + [_split("both")]}    # 2 bodies, 15000
    out["split_above"] = {"features": _split_base() + [_split("above")]}  # 1 body, 9000

    # ---- split by a NON-PLANAR surface (split.py::_surface_split, BOP path) ---------
    # A FACE selector resolving to a CURVED face takes the general BOP splitter path
    # (BRepAlgoAPI_Splitter) instead of the planar half-space path. Target body A is a
    # 40x40x30 box (V=48000); tool body B is a coaxial cylinder r=10, h=40 centred at
    # (10,20) (taller than A so its lateral face spans A's full Z), built merge:false so
    # both stay standalone. The split tool is B's LATERAL CYLINDRICAL face. Per the
    # oracle the body that OWNS the tool face (B) passes through UNCHANGED; box A is
    # divided by the curved face into the cylindrical plug it carves out + the
    # remainder. The cylinder is OFFSET in X (cx=10) so the two pieces have distinct
    # centroids -> the median bucket along the tool's local normal is non-degenerate
    # (keep=above/below pick opposite single pieces). Gated the SAME way as the planar
    # split: per-body + total volume + body count, version-independent (exact BRep vol).
    #   keep=both  -> 3 bodies (A's 2 pieces + B), total V = 48000 + pi*100*40.
    #   keep=above -> 2 bodies (one A piece + B); below -> the OTHER A piece + B.
    SS_CX = 10.0
    ss_box_e, ss_box_c = _rect(0, 0, 40, 40)
    ss_cyl_e = [_circle("ec", SS_CX, 20, 10)]
    ss_cyl_c = [_rel("cc1", "fix", ["ec.center"]), _dim("cc2", "radius", ["ec"], 10)]
    # Tool: B's lateral cylindrical face (surf="cylinder", axis +Z, area=2*pi*r*h).
    ss_tool = {"kind": "face", "surf": "cylinder",
               "centroid": [SS_CX - 10.0, 20.0, 20.0], "axis": [0.0, 0.0, 1.0],
               "area_or_len": 2.0 * 3.141592653589793 * 10.0 * 40.0,
               "owner_feature": None, "hint_index": 0}
    def _ss_base():
        return [_sketch("sa", "plane.top", ss_box_e, ss_box_c),
                _extrude("ba", "sa", 30.0, merge=False),
                _sketch("sb", "plane.top", ss_cyl_e, ss_cyl_c),
                _extrude("bb", "sb", 40.0, merge=False)]
    def _ss_split(keep):
        return {"id": "sp", "type": "split", "params": {"keep": keep}, "refs": {"tool": ss_tool}}
    out["split_surface_both"] = {"features": _ss_base() + [_ss_split("both")]}    # 3 bodies
    out["split_surface_above"] = {"features": _ss_base() + [_ss_split("above")]}  # 2 bodies
    out["split_surface_below"] = {"features": _ss_base() + [_ss_split("below")]}  # 2 bodies

    # ---- datum point / coordinate (engine/doc/features/datum.py compile_point) ----
    def _point(fid, x, y, z):
        return {"id": fid, "type": "point",
                "params": {"method": "coordinate", "coordinate": [x, y, z]}, "refs": {}}
    def _datum(fid):
        return {"kind": "datum", "feature": fid}

    # ---- datum plane: midplane (between plane.top and an offset plane 20) ----------
    # The midplane sits at z=10; a 20x20 sketch on it extruded 5 -> a 2000 boss. The
    # boss volume (2000) + its placement encode the midplane's correctness; gated on
    # the matching solid the same way the loft fixture gates the offset plane.
    dp_e, dp_c = _rect(0, 0, 20, 20)
    out["datum_plane_midplane"] = {"features": [
        _plane_offset("p2", "plane.top", 20.0),
        {"id": "pm", "type": "plane", "params": {"method": "midplane"},
         "refs": {"base": "plane.top", "second": _datum("p2")}},
        _sketch("s1", "pm", dp_e, dp_c), _extrude("x1", "s1", 5.0)]}

    # ---- datum plane: angle / three_point / parallel_point -------------------------
    # The oracle's angle/three_point/parallel_point compilers resolve against the last
    # body (ctx.bodies), so each fixture seeds a 10x10x5 base block (merge:false -> a
    # separate operand) first; the datum-driven 20x20x5 boss (V=2000) is then built ON
    # the new plane. Total V=2500 (block 500 + boss 2000), 2 bodies. The boss is built
    # by the SAME OCCT extrude through the SAME datum on both engines, so its volume +
    # body count is the gate (matches probe: py==cpp exactly).
    blk_e, blk_c = _rect(0, 0, 10, 10)
    def _datum_base():
        return [_sketch("sb", "plane.top", blk_e, blk_c),
                _extrude("bx", "sb", 5.0, merge=False)]
    def _datum_boss(plane_fid):
        return [_sketch("s1", plane_fid, dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]

    out["datum_plane_angle"] = {"features": _datum_base() + [
        {"id": "pl", "type": "plane", "params": {"method": "angle", "angle": {"value": 30.0, "expr": None}},
         "refs": {"base": "plane.top", "second": "axis.x"}}] + _datum_boss("pl")}

    out["datum_plane_three_point"] = {"features": _datum_base() + [
        _point("pa", 0, 0, 5), _point("pb", 20, 0, 5), _point("pc", 0, 20, 5),
        {"id": "pl", "type": "plane", "params": {"method": "three_point"},
         "refs": {"points": [_datum("pa"), _datum("pb"), _datum("pc")]}}] + _datum_boss("pl")}

    out["datum_plane_parallel_point"] = {"features": _datum_base() + [
        _point("pa", 3, 4, 12),
        {"id": "pl", "type": "plane", "params": {"method": "parallel_point"},
         "refs": {"base": "plane.top", "points": [_datum("pa")]}}] + _datum_boss("pl")}

    # ---- datum axis: two_points, consumed by a revolve about it --------------------
    # Two point datums on the Y axis -> a datum axis (0,0,0)->(0,1,0). A rect (x 5..15,
    # y 0..10) revolved 360 about that axis -> annulus r=5,R=15,h=10, V=2000*pi. The
    # revolve's volume encodes the axis (location + direction); the SAME revolve about
    # the SAME datum axis on both engines is the gate.
    rev_e2, rev_c2 = _rect(5, 0, 10, 10)
    out["datum_axis_revolve"] = {"features": [
        _point("pa", 0, 0, 0), _point("pb", 0, 1, 0),
        {"id": "ax", "type": "axis", "params": {"method": "two_points"},
         "refs": {"a": _datum("pa"), "b": _datum("pb")}},
        _sketch("s1", "plane.top", rev_e2, rev_c2),
        {"id": "r1", "type": "revolve",
         "params": {"mode": "boss", "angle": {"value": 360.0, "expr": None}},
         "refs": {"profile": [{"kind": "region", "sketch": "s1"}], "axis": _datum("ax")}}]}

    # ====================================================================
    # SELECTOR-DRIVEN datums (P55l): datums defined by TOPOLOGY SELECTORS that
    # resolve against the running body, not by coordinate/datum refs. The C++
    # datum compilers route the face/edge/vertex selector through the SAME
    # resolve_face_picks/resolve_edge_picks/describe_vertices machinery the
    # dress/hole compilers use, then construct the datum from the resolved
    # topology, matching engine/doc/features/datum.py. Each fixture seeds a
    # known box (so the selectors resolve to fixed centroids/axes on BOTH
    # engines via the identical scored matcher) and CONSUMES the datum with a
    # boss/revolve, so the resulting solid's volume + body count is the gate
    # (the established datum-parity bar — see datum_plane_angle above).
    # ====================================================================

    # A 40x30x20 base box (V=24000), x[0,40] y[0,30] z[0,20], built merge:false so
    # the datum-driven boss stays a SEPARATE operand (2 bodies, sum gated).
    sd_e, sd_c = _rect(0, 0, 40, 30)
    def _sel_datum_base():
        return [_sketch("sb", "plane.top", sd_e, sd_c),
                _extrude("bx", "sb", 20.0, merge=False)]

    # ---- datum plane via TWO PICKED FACES: midplane between the top (+Z, z=20) and
    # bottom (-Z, z=0) faces -> the z=10 mid-plane. A 20x20 boss extruded 5 on it
    # (V=2000) gates the resolved planar-face plane. 2 bodies, total 26000.
    sd_top = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 20.0],
              "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0,
              "owner_feature": None, "hint_index": 0}
    sd_bot = {"kind": "face", "surf": "plane", "centroid": [20.0, 15.0, 0.0],
              "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0,
              "owner_feature": None, "hint_index": 1}
    out["datum_plane_midplane_faces"] = {"features": _sel_datum_base() + [
        {"id": "pl", "type": "plane", "params": {"method": "midplane"},
         "refs": {"base": sd_top, "second": sd_bot}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum plane via a PICKED EDGE HINGE: rotate plane.top 30 degrees about a
    # picked bottom edge (the +X-aligned edge at y=0,z=0, midpoint [20,0,0]). The
    # 20x20 boss on the tilted plane (V=2000) is invariant to the tilt; the SAME
    # edge-hinge rotation on both engines is the gate. 2 bodies, total 26000.
    sd_edge = {"kind": "edge", "surf": "line", "centroid": [20.0, 0.0, 0.0],
               "axis": [1.0, 0.0, 0.0], "area_or_len": 40.0,
               "owner_feature": None, "hint_index": 0}
    out["datum_plane_angle_edge"] = {"features": _sel_datum_base() + [
        {"id": "pl", "type": "plane",
         "params": {"method": "angle", "angle": {"value": 30.0, "expr": None}},
         "refs": {"base": "plane.top", "second": sd_edge}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum plane via THREE PICKED VERTICES: three corners of the base box
    # define a plane; a 20x20 boss on it (V=2000) gates the vertex picks + plane
    # construction (origin/normal). Corners: (0,0,0), (40,0,0), (0,30,0) -> the z=0
    # plane. 2 bodies, total 26000.
    sd_v0 = {"kind": "vertex", "surf": None, "centroid": [0.0, 0.0, 0.0],
             "axis": None, "area_or_len": None, "owner_feature": None, "hint_index": 0}
    sd_v1 = {"kind": "vertex", "surf": None, "centroid": [40.0, 0.0, 0.0],
             "axis": None, "area_or_len": None, "owner_feature": None, "hint_index": 1}
    sd_v2 = {"kind": "vertex", "surf": None, "centroid": [0.0, 30.0, 0.0],
             "axis": None, "area_or_len": None, "owner_feature": None, "hint_index": 2}
    out["datum_plane_three_point_vertices"] = {"features": _sel_datum_base() + [
        {"id": "pl", "type": "plane", "params": {"method": "three_point"},
         "refs": {"points": [sd_v0, sd_v1, sd_v2]}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # The axis/point datum FEATURE compilers are gated through the clean plane->boss
    # consumer (the proven datum-parity path above), NOT a revolve: the C++ revolve
    # mode=boss does not yet fuse into the running body the way the oracle does
    # (a pre-existing revolve-merge gap, orthogonal to datum wiring), so a revolve
    # consumer would diff on body COUNT even when the datum geometry is identical.
    # Routing through an `angle` plane (whose hinge is the datum axis) / a
    # `three_point` plane (whose points are the datum points) gates the resolved
    # axis direction + point positions via the boss's body count + volume the SAME
    # way datum_plane_angle does.

    # ---- datum AXIS via a PICKED EDGE, consumed as the hinge of an angle plane:
    # pick the bottom +X edge (y=0,z=0) -> an axis datum along +X at the origin; tilt
    # plane.top 30 degrees about it; build a 20x20 boss (V=2000) on the tilted plane.
    # The boss's body count + volume gate the picked-edge axis (direction + location).
    # 2 bodies (box 24000 + boss 2000 = 26000).
    out["datum_axis_edge_plane"] = {"features": _sel_datum_base() + [
        {"id": "ax", "type": "axis", "params": {"method": "edge"}, "refs": {"a": sd_edge}},
        {"id": "pl", "type": "plane",
         "params": {"method": "angle", "angle": {"value": 30.0, "expr": None}},
         "refs": {"base": "plane.top", "second": _datum("ax")}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum AXIS via a PICKED CYLINDRICAL FACE, consumed as an angle-plane hinge:
    # a thin coaxial cylinder (r=4, h=2) on plane.top establishes a pickable lateral
    # cylindrical face -> an axis datum along Z through the centre. Tilt plane.front
    # 45 degrees about that Z axis; build a 20x20 boss (V=2000) on the tilted plane.
    # The boss gates the cylinder face's axis (direction + location). 2 bodies.
    cyl_pe = [_circle("e1", 0, 0, 4)]
    cyl_pc = [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 4)]
    sd_cyl = {"kind": "face", "surf": "cylinder", "centroid": [0.0, 0.0, 1.0],
              "axis": [0.0, 0.0, 1.0], "area_or_len": 2.0 * 3.141592653589793 * 4.0 * 2.0,
              "owner_feature": None, "hint_index": 0}
    out["datum_axis_cylinder_plane"] = {"features": [
        _sketch("sc", "plane.top", cyl_pe, cyl_pc),
        _extrude("bx", "sc", 2.0, merge=False),
        {"id": "ax", "type": "axis", "params": {"method": "cylinder"}, "refs": {"a": sd_cyl}},
        {"id": "pl", "type": "plane",
         "params": {"method": "angle", "angle": {"value": 45.0, "expr": None}},
         "refs": {"base": "plane.front", "second": _datum("ax")}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum POINT via PICKED VERTICES, consumed as the points of a three_point
    # plane: three point datums each built from a PICKED CORNER of the base box, then
    # a plane through the three points + a 20x20 boss (V=2000) on it. This gates the
    # vertex-pick -> point FEATURE compiler (each `point` resolves a picked vertex,
    # then the plane is built from the three). Corners (0,0,0),(40,0,0),(0,30,0) ->
    # the z=0 plane. 2 bodies, total 26000.
    def _point_vertex(fid, vsel):
        return {"id": fid, "type": "point", "params": {"method": "vertex"},
                "refs": {"target": vsel}}
    out["datum_point_vertex_plane"] = {"features": _sel_datum_base() + [
        _point_vertex("pa", sd_v0), _point_vertex("pb", sd_v1), _point_vertex("pc", sd_v2),
        {"id": "pl", "type": "plane", "params": {"method": "three_point"},
         "refs": {"points": [_datum("pa"), _datum("pb"), _datum("pc")]}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ====================================================================
    # P55p: the last datum sub-cases that need OCCT surface-projection /
    # curve-tangent. plane.on_surface (z_dir = surface normal at a projected
    # point), plane.normal_to_curve (z_dir = curve tangent at a landing point),
    # axis.on_surface (a face's revolve axis / planar normal, OR a 2-point line).
    # Each is consumed by a plane->boss (or angle-hinge) consumer so the resolved
    # datum origin/normal/direction is gated by the boss's volume + body count
    # (OCCT-version-independent), the established datum-parity bar above.
    # ====================================================================

    # ---- datum plane via plane.on_surface on the TOP planar face (z=20): the
    # tangent plane IS that z=20 plane (origin = projected point, normal = +Z).
    # A 20x20 boss extruded 5 on it (V=2000) gates the surface projection + normal.
    # 2 bodies, total 26000.
    out["datum_plane_on_surface"] = {"features": _sel_datum_base() + [
        {"id": "pl", "type": "plane", "params": {"method": "on_surface"},
         "refs": {"base": sd_top}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum plane via plane.normal_to_curve on the picked bottom +X edge: the
    # plane's normal is the edge tangent (+X), its origin the curve landing point
    # (the edge midpoint [20,0,0]). A 20x20 boss extruded 5 on that plane (V=2000)
    # is invariant to the in-plane frame; the SAME curve-tangent on both engines is
    # the gate (point + tangent via BRepAdaptor_Curve D1). 2 bodies, total 26000.
    out["datum_plane_normal_to_curve"] = {"features": _sel_datum_base() + [
        {"id": "pl", "type": "plane", "params": {"method": "normal_to_curve"},
         "refs": {"second": sd_edge}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum axis via axis.on_surface on the TOP planar face: the axis is the
    # surface normal (+Z) through the face area centroid [20,15,20]. Routed as the
    # hinge of an angle plane (tilt plane.front 45 about the +Z axis) -> a 20x20
    # boss (V=2000), the proven axis-datum consumer path. Gates the planar-face
    # normal axis (direction + location). 2 bodies, total 26000.
    out["datum_axis_on_surface_face"] = {"features": _sel_datum_base() + [
        {"id": "ax", "type": "axis", "params": {"method": "on_surface"},
         "refs": {"a": sd_top}},
        {"id": "pl", "type": "plane",
         "params": {"method": "angle", "angle": {"value": 45.0, "expr": None}},
         "refs": {"base": "plane.front", "second": _datum("ax")}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- datum axis via axis.on_surface TWO-POINT form: two point datums (picked
    # corners (0,0,0) and (40,0,0)) -> a line along +X at the origin. Routed as the
    # hinge of an angle plane (tilt plane.top 30 about it) -> a 20x20 boss (V=2000).
    # Gates the two-point on-surface axis (direction + location). 2 bodies, 26000.
    out["datum_axis_on_surface_points"] = {"features": _sel_datum_base() + [
        _point_vertex("pa", sd_v0), _point_vertex("pb", sd_v1),
        {"id": "ax", "type": "axis", "params": {"method": "on_surface"},
         "refs": {"a": _datum("pa"), "b": _datum("pb")}},
        {"id": "pl", "type": "plane",
         "params": {"method": "angle", "angle": {"value": 30.0, "expr": None}},
         "refs": {"base": "plane.top", "second": _datum("ax")}},
        _sketch("s1", "pl", dp_e, dp_c), _extrude("x1", "s1", 5.0, merge=False)]}

    # ---- curve: constant-pitch helix consumed as a sweep path ----------------------
    # A circle (r=2) on plane.top swept along a helix (pitch 5, height 20, radius 10)
    # -> a coiled tube. The C++ helix replicates build123d Edge.make_helix exactly
    # (Geom_CylindricalSurface + wrapped 2D line + BuildCurves3d), so the swept solid's
    # volume matches the oracle to full tolerance (probe: py==cpp on the exact value).
    hx_prof_e = [_circle("e1", 0, 0, 2)]
    hx_prof_c = [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 2)]
    out["helix_sweep"] = {"features": [
        _sketch("s1", "plane.top", hx_prof_e, hx_prof_c),
        {"id": "hx", "type": "helix",
         "params": {"pitch": {"value": 5.0, "expr": None}, "height": {"value": 20.0, "expr": None},
                    "radius": {"value": 10.0, "expr": None}},
         "refs": {"base": "plane.top"}},
        {"id": "sw", "type": "sweep", "params": {"mode": "boss"},
         "refs": {"profile": [{"kind": "region", "sketch": "s1"}],
                  "path": {"kind": "region", "sketch": "hx"}}}]}

    # ---- curve: VARIABLE-pitch helix (pitch_table) consumed as a sweep path ---------
    # Same r=2 circle swept along a variable-pitch helix whose pitch_table ramps pitch
    # 5->10 and diameter 16->24 over 6 turns. Both engines build the path as a B-spline
    # sampled 48/turn through the SAME analytic curve (piecewise-linear radius +
    # trapezoidal-integrated height), so the swept tube's exact BRep volume matches to
    # full tolerance. Regression-guards the variable-pitch port against the oracle.
    vx_prof_e = [_circle("e1", 0, 0, 2)]
    vx_prof_c = [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 2)]
    out["helix_varpitch_sweep"] = {"features": [
        _sketch("s1", "plane.top", vx_prof_e, vx_prof_c),
        {"id": "hx", "type": "helix",
         "params": {"pitch_table": [
             {"turn": 0, "pitch": 5,  "dia": 16},
             {"turn": 3, "pitch": 5,  "dia": 16},
             {"turn": 6, "pitch": 10, "dia": 24},
         ]},
         "refs": {"base": "plane.top"}},
        {"id": "sw", "type": "sweep", "params": {"mode": "boss"},
         "refs": {"profile": [{"kind": "region", "sketch": "s1"}],
                  "path": {"kind": "region", "sketch": "hx"}}}]}

    # ---- import feature (engine/doc/features/import_feature.py, B-rep path) --------
    # Export a known 30x20x10 box (V=6000) to a temp STEP, then import it as a frozen
    # body feature on BOTH engines and compare volume. Skipped if build123d's STEP
    # export is unavailable (keeps the harness runnable without the heavy export deps).
    try:
        import tempfile
        from build123d import Box, export_step  # noqa: WPS433
        step_path = str(Path(tempfile.gettempdir()) / "aicad_parity_import_box.step")
        export_step(Box(30, 20, 10), step_path)
        out["import_step"] = {"features": [
            {"id": "im", "type": "import",
             "params": {"source_path": step_path, "format": "step"}, "refs": {}}]}
    except Exception:  # pragma: no cover - export unavailable
        pass

    return out


# --------------------------------------------------------------------------- C++ DLL
class CppCore:
    def __init__(self, dll_path: Path):
        dll_path = dll_path.resolve()
        if hasattr(os, "add_dll_directory"):
            # Let the DLL's OCCT sibling DLLs (TK*, jemalloc, tbb12) resolve.
            os.add_dll_directory(str(dll_path.parent))
        self.lib = ctypes.CDLL(str(dll_path))
        self.lib.aicad_call.restype = ctypes.c_int
        self.lib.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.POINTER(ctypes.c_char_p)]
        self.lib.aicad_free.restype = None
        self.lib.aicad_free.argtypes = [ctypes.c_char_p]
        self.lib.aicad_abi_version.restype = ctypes.c_char_p

    def abi(self) -> str:
        return self.lib.aicad_abi_version().decode()

    def call(self, method: str, params: dict) -> tuple[int, dict]:
        out = ctypes.c_char_p()
        rc = self.lib.aicad_call(method.encode(), json.dumps(params).encode(),
                                 ctypes.byref(out))
        data = {}
        if out.value:
            try:
                data = json.loads(out.value.decode())
            finally:
                self.lib.aicad_free(out)
        return rc, data


# --------------------------------------------------------------------------- oracle
def python_compile(doc: dict):
    """Run the Python production compiler. Returns (id->ok, body_count, volume, error)."""
    try:
        from engine.doc.compiler import compile_document  # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        return {}, 0, 0.0, f"import engine failed: {exc}"
    try:
        res = compile_document(doc)
    except Exception as exc:
        return {}, 0, 0.0, f"compile raised: {exc}"
    status = {s["id"]: bool(s.get("ok")) for s in res.feature_status}

    def _vol(shape) -> float:
        # A body's shape is a single Solid (has .volume) OR a ShapeList of disjoint
        # solids (e.g. a disjoint pattern) -> sum the parts.
        try:
            return float(shape.volume)
        except Exception:
            pass
        try:
            return float(sum(float(s.volume) for s in shape))
        except Exception:
            return 0.0

    vol = sum(_vol(b.shape) for b in res.bodies)
    return status, len(res.bodies), vol, None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"),
                    help="path to libaicad.dll")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO))

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[parity] libaicad.dll not found at {dll}\n"
              f"         build it: cmake --build core/build-occt --config Release --target aicad")
        return 2
    core = CppCore(dll)
    print(f"[parity] libaicad ABI={core.abi()}  dll={dll}")

    fx = fixtures()
    name_w = max(len(n) for n in fx)
    print(f"\n{'fixture'.ljust(name_w)}  {'py(ok,bodies,vol)':>22}  {'cpp(ok,bodies,vol)':>22}  verdict")
    print("-" * (name_w + 56))

    VOL_TOL = 1e-3  # relative volume tolerance (tessellation-independent: exact BRep volume)
    mismatches = 0
    for name, doc in fx.items():
        py_status, py_bodies, py_vol, py_err = python_compile(doc)
        rc, cpp = core.call("doc_compile_cpp", {"doc": doc})
        cpp_status = {s["id"]: bool(s.get("ok")) for s in cpp.get("statuses", [])}
        cpp_bodies = cpp.get("body_count", 0)
        cpp_vol = float(cpp.get("volume", 0.0))

        py_ok = sum(1 for v in py_status.values() if v)
        cpp_ok = sum(1 for v in cpp_status.values() if v)

        diffs = []
        if py_err:
            diffs.append(f"py-error:{py_err[:40]}")
        if rc != 0 and not cpp_status:
            diffs.append(f"cpp-error:{cpp.get('error','?')[:40]}")
        for fid in py_status:
            if py_status[fid] != cpp_status.get(fid, False):
                diffs.append(fid)
        if py_bodies != cpp_bodies:
            diffs.append(f"bodies {py_bodies}!={cpp_bodies}")
        denom = max(abs(py_vol), 1.0)
        if abs(py_vol - cpp_vol) / denom > VOL_TOL:
            diffs.append(f"vol {py_vol:.1f}!={cpp_vol:.1f}")

        verdict = "MATCH" if not diffs else "DIFF: " + ", ".join(diffs)
        if diffs:
            mismatches += 1
        print(f"{name.ljust(name_w)}  {f'{py_ok}/{len(py_status)},{py_bodies},{py_vol:.0f}':>22}"
              f"  {f'{cpp_ok}/{len(cpp_status)},{cpp_bodies},{cpp_vol:.0f}':>22}  {verdict}")

    print(f"\n[parity] {len(fx) - mismatches}/{len(fx)} fixtures at full parity")
    return mismatches


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate the mate-solver differential corpus from the Python test scenarios.

Mirrors engine/doc/test_mate_solver.py + test_mate_solver_ext.py: builds the same
components / mates / resolved_geometry, serializes them to JSON fixtures under
core/mate/corpus/ (the form tools/mate_diff.py + mate_cli consume). Transforms are
computed with the oracle's _rodrigues so the C++ side reads identical matrices.

Run from repo root with the project venv (numpy needed):
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" tools/gen_mate_corpus.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.doc.mate_solver import _rodrigues  # noqa: E402

CORPUS = REPO_ROOT / "core" / "mate" / "corpus"


def ident16():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def xform(t, w=(0, 0, 0)):
    R = _rodrigues(np.asarray(w, dtype=float))
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = np.asarray(t, dtype=float)
    return [float(v) for v in M.reshape(16)]


def comp(cid, transform, fixed=False, suppressed=False, name=None):
    d = {"id": cid, "transform": transform, "fixed": fixed, "suppressed": suppressed}
    if name:
        d["name"] = name
    return d


def mate(mid, mtype, refs, align="aligned", value=None, suppressed=False):
    return {"id": mid, "type": mtype, "align": align, "suppressed": suppressed,
            "refs": [{"component": c} for c in refs], "value": value}


def geom(mid, ri, kind, point=(0, 0, 0), normal=(0, 0, 1), direction=(0, 0, 1),
         radius=0.0):
    return {"mate_id": mid, "ref_index": ri,
            "geom": {"kind": kind, "point": list(point), "normal": list(normal),
                     "direction": list(direction), "radius": radius}}


def fixture(name, components, mates, resolved_geometry, warm=None, drag=None):
    fx = {"name": name, "components": components, "mates": mates,
          "resolved_geometry": resolved_geometry}
    if warm is not None:
        fx["warm"] = warm
    if drag is not None:
        fx["drag"] = drag
    return fx


def write(fx):
    CORPUS.mkdir(parents=True, exist_ok=True)
    path = CORPUS / f"{fx['name']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fx, f, indent=2)
    return path


# --------------------------------------------------------------------------- scenarios


def peg_in_hole(name, align_peg="aligned"):
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((30.0, -10.0, 25.0), (0.3, -0.2, 0.5)), name="peg"),
    ]
    mates = [
        mate("m1", "concentric", ["k1", "k2"]),
        mate("m2", "coincident", ["k1", "k2"], align=align_peg),
    ]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=4.0),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=3.9),
        geom("m2", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m2", 1, "plane", point=(0, 0, 0), normal=(0, 0, -1)),
    ]
    return fixture(name, components, mates, g)


def over_define_redundant():
    fx = peg_in_hole("over_redundant_concentric")
    fx["mates"].append(mate("m3", "concentric", ["k1", "k2"]))
    fx["resolved_geometry"].append(
        geom("m3", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=4.0))
    fx["resolved_geometry"].append(
        geom("m3", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=3.9))
    return fx


def over_define_conflicting():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((30.0, -10.0, 25.0), (0.3, -0.2, 0.5)), name="peg"),
    ]
    mates = [
        mate("m1", "concentric", ["k1", "k2"]),
        mate("m2", "distance", ["k1", "k2"], value={"value": 5.0, "expr": None}),
        mate("m3", "distance", ["k1", "k2"], value={"value": 12.0, "expr": None}),
    ]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=4.0),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=3.9),
        geom("m2", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m2", 1, "plane", point=(0, 0, 0), normal=(0, 0, -1)),
        geom("m3", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m3", 1, "plane", point=(0, 0, 0), normal=(0, 0, -1)),
    ]
    return fixture("over_conflicting_distances", components, mates, g)


def coincident_slider():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((5.0, 5.0, 20.0)), name="slider"),
    ]
    mates = [mate("m1", "coincident", ["k1", "k2"], align="anti")]
    g = [
        geom("m1", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m1", 1, "plane", point=(0, 0, 0), normal=(0, 0, -1)),
    ]
    return fixture("coincident_plane_slider", components, mates, g)


def drag_slider():
    """Warm + drag: seat first (baseline), then a warm+drag fixture."""
    base = coincident_slider()
    base["name"] = "drag_baseline_slider"
    # The warm state is the baseline solved params; mate_diff computes it by
    # solving the baseline first, so we just emit a drag fixture that warms from
    # the same start. We use the start transform as warm so both engines warm
    # identically; the drag pulls in x/y while z stays seated.
    warm = {
        "k1": {"t": [0.0, 0.0, 0.0], "w": [0.0, 0.0, 0.0]},
        "k2": {"t": [5.0, 5.0, 0.0], "w": [0.0, 0.0, 0.0]},
    }
    fx = coincident_slider()
    fx["name"] = "drag_slider_xy"
    fx["warm"] = warm
    fx["drag"] = {"component": "k2", "target": [40.0, -15.0, 99.0]}
    return base, fx


# --- advanced ---


def width_centers_tab():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((37.0, 4.0, 1.0)), name="block"),
    ]
    mates = [mate("m1", "width", ["k2", "k1", "k1"])]
    g = [
        geom("m1", 0, "point", point=(0, 0, 0)),
        geom("m1", 1, "plane", point=(0, 0, 0), normal=(1, 0, 0)),
        geom("m1", 2, "plane", point=(20, 0, 0), normal=(1, 0, 0)),
    ]
    return fixture("width_centers_tab", components, mates, g)


def symmetric_mirror():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((25.0, 3.0, -2.0)), name="block"),
    ]
    mates = [mate("m1", "symmetric", ["k1", "k2", "k1"])]
    g = [
        geom("m1", 0, "point", point=(8, 0, 0)),
        geom("m1", 1, "point", point=(0, 0, 0)),
        geom("m1", 2, "plane", point=(0, 0, 0), normal=(1, 0, 0)),
    ]
    return fixture("symmetric_mirror_yz", components, mates, g)


def distance_limit_case(name, start_z):
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((0.0, 0.0, start_z)), name="slider"),
    ]
    mates = [mate("m1", "distance_limit", ["k1", "k2"], value={"min": 5.0, "max": 12.0})]
    g = [
        geom("m1", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m1", 1, "point", point=(0, 0, 0)),
    ]
    return fixture(name, components, mates, g)


def angle_limit_case():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((0.0, 0.0, 0.0), (0.0, 0.0, math.radians(80.0))), name="arm"),
    ]
    mates = [mate("m1", "angle_limit", ["k1", "k2"], value={"min": 0.0, "max": 45.0})]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("angle_limit_clamp45", components, mates, g)


def profile_center_case():
    components = [
        comp("k1", ident16(), fixed=True, name="base"),
        comp("k2", xform((22.0, -7.0, 13.0)), name="prof"),
    ]
    mates = [mate("m1", "profile_center", ["k1", "k2"])]
    g = [
        geom("m1", 0, "point", point=(4, 5, 6)),
        geom("m1", 1, "point", point=(0, 0, 0)),
    ]
    return fixture("profile_center", components, mates, g)


# --- mechanical ---


def gear_case():
    phi = math.radians(30.0)
    components = [
        comp("k0", ident16(), fixed=True, name="frame"),
        comp("kA", xform((0, 0, 0), (0.0, 0.0, phi)), fixed=True, name="pinion"),
        comp("kB", xform((10, 0, 0), (0.0, 0.0, 0.0)), name="wheel"),
    ]
    mates = [
        mate("m1", "concentric", ["k0", "kB"]),
        mate("m2", "gear", ["kA", "kB"], value={"ratio": 2.0}),
    ]
    g = [
        geom("m1", 0, "axis", point=(10, 0, 0), direction=(0, 0, 1)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m2", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m2", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
    ]
    return fixture("gear_ratio2", components, mates, g)


def rack_pinion_case():
    phi = math.radians(20.0)
    r = 5.0
    components = [
        comp("k0", ident16(), fixed=True, name="frame"),
        comp("kA", xform((0, 0, 0), (0.0, 0.0, phi)), fixed=True, name="pinion"),
        comp("kB", xform((0, -10, 0)), name="rack"),
    ]
    mates = [
        mate("m1", "coincident", ["k0", "kB"]),
        mate("m2", "rack_pinion", ["kA", "kB"], value={"pitch_radius": r}),
    ]
    g = [
        geom("m1", 0, "plane", point=(0, -10, 0), normal=(0, 1, 0)),
        geom("m1", 1, "plane", point=(0, 0, 0), normal=(0, -1, 0)),
        geom("m2", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m2", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("rack_pinion", components, mates, g)


def screw_case():
    pitch = 4.0
    components = [
        comp("k0", ident16(), fixed=True, name="nut"),
        comp("kB", xform((0, 0, 0), (0.0, 0.0, 0.0)), name="bolt"),
    ]
    mates = [
        mate("m1", "concentric", ["k0", "kB"]),
        mate("m2", "parallel", ["k0", "kB"]),
        mate("m3", "screw", ["k0", "kB"], value={"pitch": pitch}),
    ]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=1.0),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=1.0),
        geom("m2", 0, "axis", point=(0, 0, 0), direction=(0, 1, 0)),
        geom("m2", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m3", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m3", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
    ]
    return fixture("screw_pitch", components, mates, g)


def hinge_case():
    components = [
        comp("k1", ident16(), fixed=True, name="frame"),
        comp("k2", xform((0, 0, 0), (0.0, 0.0, math.radians(140.0))), name="door"),
    ]
    mates = [mate("m1", "hinge", ["k1", "k2"], value={"min": 0.0, "max": 90.0})]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1)),
        geom("m1", 2, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m1", 3, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("hinge_clamp90", components, mates, g)


def slot_case():
    components = [
        comp("k1", ident16(), fixed=True, name="plate"),
        comp("k2", xform((6.0, 9.0, -4.0)), name="pin"),
    ]
    mates = [mate("m1", "slot", ["k2", "k1"])]
    g = [
        geom("m1", 0, "point", point=(0, 0, 0)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("slot_on_centerline", components, mates, g)


def cam_follower_case():
    R, rf = 10.0, 2.0
    components = [
        comp("k1", ident16(), fixed=True, name="cam"),
        comp("k2", xform((20.0, 5.0, 0.0)), name="follower"),
    ]
    mates = [mate("m1", "cam_follower", ["k2", "k1"])]
    g = [
        geom("m1", 0, "sphere", point=(0, 0, 0), radius=rf),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=R),
    ]
    return fixture("cam_follower", components, mates, g)


# --- extra coverage: parallel / perpendicular / angle / lock / tangent / distance ---


def parallel_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((4, 4, 4), (0.2, 0.5, -0.3))),
    ]
    mates = [mate("m1", "parallel", ["k1", "k2"])]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("parallel_axes", components, mates, g)


def perpendicular_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((4, 4, 4), (0.2, 0.5, -0.3))),
    ]
    mates = [mate("m1", "perpendicular", ["k1", "k2"])]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("perpendicular_axes", components, mates, g)


def angle_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((0, 0, 0), (0.0, 0.0, math.radians(10.0)))),
    ]
    mates = [mate("m1", "angle", ["k1", "k2"], value={"value": 30.0})]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0)),
    ]
    return fixture("angle_30deg", components, mates, g)


def distance_planes_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((0, 0, 22.0)), name="top"),
    ]
    mates = [mate("m1", "distance", ["k1", "k2"], align="anti",
                  value={"value": 8.0, "expr": None})]
    g = [
        geom("m1", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m1", 1, "plane", point=(0, 0, 0), normal=(0, 0, -1)),
    ]
    return fixture("distance_planes_8", components, mates, g)


def lock_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((12, 3, -5), (0.1, 0.2, 0.3))),
    ]
    mates = [mate("m1", "lock", ["k1", "k2"])]
    g = [
        geom("m1", 0, "point", point=(0, 0, 0)),
        geom("m1", 1, "point", point=(0, 0, 0)),
    ]
    return fixture("lock_relative", components, mates, g)


def tangent_cyl_plane_case():
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((0, 0, 14.0)), name="cyl"),
    ]
    mates = [mate("m1", "tangent", ["k1", "k2"])]
    g = [
        # plane on A (z=0, +z), cylinder on B (axis along x, radius 3)
        geom("m1", 0, "plane", point=(0, 0, 0), normal=(0, 0, 1)),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(1, 0, 0), radius=3.0),
    ]
    return fixture("tangent_cyl_plane", components, mates, g)


def suppressed_and_unresolved_case():
    """A suppressed mate + an unresolved (missing geom) mate alongside a real one."""
    components = [
        comp("k1", ident16(), fixed=True),
        comp("k2", xform((30.0, -10.0, 25.0), (0.3, -0.2, 0.5)), name="peg"),
    ]
    mates = [
        mate("m1", "concentric", ["k1", "k2"]),
        mate("m2", "coincident", ["k1", "k2"], suppressed=True),
        mate("m3", "distance", ["k1", "k2"], value={"value": 5.0, "expr": None}),
    ]
    g = [
        geom("m1", 0, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=4.0),
        geom("m1", 1, "axis", point=(0, 0, 0), direction=(0, 0, 1), radius=3.9),
        # m2 suppressed (geom omitted ok); m3 missing geom -> unresolved.
    ]
    return fixture("suppressed_and_unresolved", components, mates, g)


def main():
    paths = []
    paths.append(write(peg_in_hole("peg_in_hole_aligned", "aligned")))
    paths.append(write(peg_in_hole("peg_in_hole_anti", "anti")))
    paths.append(write(over_define_redundant()))
    paths.append(write(over_define_conflicting()))
    paths.append(write(coincident_slider()))
    base, drag_fx = drag_slider()
    paths.append(write(base))
    paths.append(write(drag_fx))
    paths.append(write(width_centers_tab()))
    paths.append(write(symmetric_mirror()))
    paths.append(write(distance_limit_case("distance_limit_hi", 30.0)))
    paths.append(write(distance_limit_case("distance_limit_lo", 1.0)))
    paths.append(write(distance_limit_case("distance_limit_in", 8.0)))
    paths.append(write(angle_limit_case()))
    paths.append(write(profile_center_case()))
    paths.append(write(gear_case()))
    paths.append(write(rack_pinion_case()))
    paths.append(write(screw_case()))
    paths.append(write(hinge_case()))
    paths.append(write(slot_case()))
    paths.append(write(cam_follower_case()))
    paths.append(write(parallel_case()))
    paths.append(write(perpendicular_case()))
    paths.append(write(angle_case()))
    paths.append(write(distance_planes_case()))
    paths.append(write(lock_case()))
    paths.append(write(tangent_cyl_plane_case()))
    paths.append(write(suppressed_and_unresolved_case()))
    print(f"wrote {len(paths)} fixtures to {CORPUS}")
    for p in paths:
        print("  ", p.name)


if __name__ == "__main__":
    main()

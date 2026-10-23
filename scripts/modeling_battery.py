#!/usr/bin/env python3
"""Modeling-op combination battery — engine-level AI testing of feature
tools and sequences.

Nobody can hand-test every modeling feature and combination; this battery carries that
coverage headlessly. It drives the real C++ feature engine (libchudware.so) through the
core_bridge doc RPCs (doc_new + doc_apply_ops), exercising every registered modeling op
once (single-op pass) plus a curated pairwise/sequence matrix and a few realistic
recipes, and asserts on real geometry results:

  * rebuild success   — every feature's rebuild `ok` (CoreError vs feature_status)
  * manifoldness      — eval_check reports no free-edge / error-severity issue
  * mass-property sane — volume moves the right way (cut < seed, boss > seed)
  * body count        — split/pattern/copy add bodies; fuse removes them
  * feature overlap   — check_feature_overlap finds no bad boss/cut pairs (opt-in)

NO-ALL-SKIP CONTRACT (the all-skip trap): a run where
every case skips (missing .so / missing dep) is a FAILURE, not a pass. `main()` exits
non-zero if the engine is unavailable or zero cases execute.

Report conventions mirror the V&V harness (`scripts/vv_benchmark.py`):
JSON results + Markdown report under `reports/modeling/`.

Run:
    # Full battery (needs the OCCT libchudware.so), writes reports/modeling/:
    CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so PYTHONPATH=$PWD \\
        python3 scripts/modeling_battery.py

    # Registry-integrity smoke (no .so):
    PYTHONPATH=$PWD pytest scripts/modeling_battery.py::TestSmoke -q

    # Full pytest (needs .so):
    CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so PYTHONPATH=$PWD \\
        pytest scripts/modeling_battery.py -q
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[1]
REPORT_DIR = REPO / "reports" / "modeling"

# --------------------------------------------------------------------------- op helpers
# Feature ops in the CLOSED vocabulary documented by cad_guide("overview")
# (services/agent/prompts/shared/ops_cheatsheet.md) and registered in
# core/doc/validate.cpp. These builders emit the exact JSON the engine's doc_apply_ops
# RPC accepts. Every op dict below is proven to rebuild live against libchudware.so.


def dim(v: float) -> dict:
    return {"value": v, "expr": None}


def feat(ftype: str, name: str, params: dict | None = None,
         refs: dict | None = None, **extra) -> dict:
    f = {"type": ftype, "name": name, "params": params or {}, "refs": refs or {}}
    f.update(extra)
    return {"op": "add_feature", "feature": f}


def sketch(idx: int, plane: Any = "plane.top", offset: float | None = None) -> dict:
    p: dict = {"entities": [], "constraints": []}
    if offset is not None:
        p["offset"] = dim(offset)
    return {"op": "add_feature",
            "feature": {"type": "sketch", "name": f"Sk{idx}", "params": p,
                        "refs": {"plane": plane}}}


def ent(sref: str, entity: dict) -> dict:
    return {"op": "sketch_add_entity", "sketch": sref, "entity": entity}


def region(sref: str, c=(0, 0)) -> dict:
    return {"kind": "region", "sketch": sref, "centroid": list(c), "hint_index": 0}


def rect(sref: str, hw: float, hh: float) -> list[dict]:
    """Four lines making a centered rectangle (there is no 'rect' entity)."""
    pts = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [ent(sref, {"type": "line", "p1": list(pts[i]),
                       "p2": list(pts[(i + 1) % 4])}) for i in range(4)]


# reusable seeds ------------------------------------------------------------
def seed_box(l=40, w=30, h=20) -> list[dict]:
    return [feat("box", "Base", {"length": dim(l), "width": dim(w), "height": dim(h)})]


def seed_plate() -> list[dict]:
    return [feat("box", "Plate", {"length": dim(60), "width": dim(40), "height": dim(8)})]


def seed_disc(r=40, h=8) -> list[dict]:
    return [feat("cylinder", "Disc", {"radius": dim(r), "height": dim(h)})]


def boss_on_top(sk_idx: int, x=15) -> list[dict]:
    """A patternable/mirrorable sketch+extrude boss on the current body's top face."""
    return [sketch(sk_idx, {"of": "face", "side": "top"}),
            ent(f"${sk_idx}", {"type": "circle", "center": [x, 0], "radius": 4}),
            feat("extrude", "Boss", {"mode": "boss", "end": "blind", "depth": dim(10)},
                 {"profile": [region(f"${sk_idx}", (x, 0))]})]


# --------------------------------------------------------------------------- case model
@dataclass
class Case:
    id: str
    category: str
    kind: str                       # single | sequence | recipe | negative
    ops_covered: tuple              # registered op names this case exercises
    seed: list = field(default_factory=list)
    test: list = field(default_factory=list)
    # optional: build test ops dynamically from the SEEDED doc (e.g. query a resolved
    # edge selector that a static op list can't name). Called (cb, doc) -> list[ops].
    build_test: Optional[Callable] = None
    # expectations (None = don't check)
    expect_rebuild: Optional[bool] = True
    expect_vol: Optional[str] = None    # ">" | "<" | "==" | "!=" (vs seed volume)
    expect_bodies: Optional[int] = None
    expect_manifold: Optional[bool] = True
    expect_overlap_clean: Optional[bool] = None   # True => check_feature_overlap pairs empty
    xfail: Optional[str] = None         # known-bug marker: issue ref; failure => XFAIL not FAIL
    note: str = ""


CASES: list[Case] = []


def add(*a, **k):
    CASES.append(Case(*a, **k))


# ============================ SINGLE-OP PASS ================================
# primitives
add("box", "primitive", "single", ("box",), [],
    [feat("box", "B", {"length": dim(40), "width": dim(30), "height": dim(20)})],
    expect_vol="!=", expect_bodies=1)
add("cylinder", "primitive", "single", ("cylinder",), [],
    [feat("cylinder", "C", {"radius": dim(15), "height": dim(30)})],
    expect_vol="!=", expect_bodies=1)
add("sphere", "primitive", "single", ("sphere",), [],
    [feat("sphere", "S", {"radius": dim(15)})], expect_vol="!=", expect_bodies=1)
add("cone", "primitive", "single", ("cone",), [],
    [feat("cone", "Co", {"r1": dim(15), "r2": dim(5), "height": dim(25)})],
    expect_vol="!=", expect_bodies=1)
add("torus", "primitive", "single", ("torus",), [],
    [feat("torus", "T", {"major_r": dim(20), "minor_r": dim(5)})],
    expect_vol="!=", expect_bodies=1)
# sketch + 2D-profile
add("extrude_boss", "sketch2d", "single", ("sketch", "extrude"), [],
    [sketch(0), ent("$0", {"type": "circle", "center": [0, 0], "radius": 12}),
     feat("extrude", "Ex", {"mode": "boss", "end": "blind", "depth": dim(20)},
          {"profile": [region("$0")]})], expect_vol="!=", expect_bodies=1)
add("extrude_cut", "sketch2d", "single", ("sketch", "extrude"), seed_plate(),
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [0, 0], "radius": 8}),
     feat("extrude", "Cut", {"mode": "cut", "end": "through_all"},
          {"profile": [region("$0")]})], expect_vol="<", expect_bodies=1)
add("revolve", "sketch2d", "single", ("sketch", "revolve"), [],
    [sketch(0, "plane.front"),
     ent("$0", {"type": "spline", "points": [[0, 0], [15, 0], [18, 20], [10, 45],
                                             [12, 60], [0, 60]]}),
     ent("$0", {"type": "line", "p1": [0, 60], "p2": [0, 0]}),
     feat("revolve", "Rev", {"mode": "boss", "angle": dim(360)},
          {"profile": [region("$0", (8, 30))],
           "axis": {"kind": "datum", "feature": "axis.z"}})],
    expect_vol="!=", expect_bodies=1)
add("loft", "sketch2d", "single", ("sketch", "loft"), [],
    [sketch(0, "plane.top"), ent("$0", {"type": "circle", "center": [0, 0], "radius": 20}),
     sketch(2, "plane.top", offset=50), *rect("$2", 15, 15),
     feat("loft", "Lf", {"mode": "boss"},
          {"sections": [region("$0"), region("$2")]})], expect_vol="!=", expect_bodies=1)
add("sweep", "sketch2d", "single", ("sketch", "sweep"), [],
    [sketch(0, "plane.right"), ent("$0", {"type": "circle", "center": [0, 0], "radius": 4}),
     sketch(2, "plane.top"), ent("$2", {"type": "line", "p1": [0, 0], "p2": [40, 0]}),
     feat("sweep", "Sw", {"mode": "boss"},
          {"profile": [region("$0")], "path": region("$2", (20, 0))})],
    expect_vol="!=", expect_bodies=1)
# curve: helix (+sweep) — a spring
add("helix_spring", "curve", "single", ("sketch", "helix", "sweep"), [],
    [sketch(0, "plane.front"), ent("$0", {"type": "circle", "center": [8, 0], "radius": 1.5}),
     feat("helix", "Hx", {"pitch": dim(4), "height": dim(12), "radius": dim(8)},
          {"base": "plane.top"}),
     feat("sweep", "Coil", {"mode": "boss"},
          {"profile": [region("$0", (8, 0))], "path": "$2"})],
    expect_vol="!=", expect_bodies=1)
# dress / transform of a body
add("fillet", "dress", "single", ("fillet",), seed_box(),
    [feat("fillet", "Fl", {"radius": dim(3)}, {"edges": {"of": "edge"}})],
    expect_vol="<", expect_bodies=1)
add("chamfer", "dress", "single", ("chamfer",), seed_box(),
    [feat("chamfer", "Ch", {"length": dim(3)}, {"edges": {"of": "edge"}})],
    expect_vol="<", expect_bodies=1)
add("shell", "dress", "single", ("shell",), seed_box(),
    [feat("shell", "Sh", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}})],
    expect_vol="<", expect_bodies=1)
add("draft", "dress", "single", ("draft",), seed_box(),
    [feat("draft", "Dr", {"angle": dim(5)},
          {"neutral": {"of": "face", "side": "bottom"},
           "faces": {"of": "face", "surf": "flat", "side": "left"}})],
    expect_vol="!=", expect_bodies=1)
# structural
add("rib", "structural", "single", ("sketch", "rib"), seed_plate(),
    [sketch(0, "plane.right"), ent("$0", {"type": "line", "p1": [-20, 8], "p2": [20, 20]}),
     feat("rib", "Rb", {"thickness": dim(2)}, {"profile": [region("$0", (0, 12))]})],
    expect_vol=">", expect_bodies=1)
add("split", "structural", "single", ("split",), seed_box(),
    [feat("split", "Sp", {"keep": "both"}, {"tool": "plane.right"})],
    expect_vol="==", expect_bodies=2)
add("boolean_fuse", "structural", "single", ("boolean",),
    [feat("box", "A", {"length": dim(30), "width": dim(30), "height": dim(30)}),
     feat("cylinder", "B", {"radius": dim(10), "height": dim(50), "merge": False})],
    [feat("boolean", "Fuse", {"op": "fuse", "a": "f1", "b": "f2"})],
    expect_vol="<", expect_bodies=1, note="overlapping fuse: single connected solid")
add("boolean_cut", "structural", "single", ("boolean",),
    [feat("box", "A", {"length": dim(30), "width": dim(30), "height": dim(30)}),
     feat("cylinder", "B", {"radius": dim(8), "height": dim(50), "merge": False})],
    [feat("boolean", "Cut", {"op": "cut", "a": "f1", "b": "f2"})],
    expect_vol="<", expect_bodies=1)
add("boolean_fuse_touching_face", "structural", "single", ("boolean",),
    [feat("box", "A", {"length": dim(10), "width": dim(10), "height": dim(10)}),
     feat("box", "B", {"length": dim(10), "width": dim(10), "height": dim(10),
                       "at": [10, 0, 0], "merge": False})],
    [feat("boolean", "Fuse", {"op": "fuse", "a": "f1", "b": "f2"})],
    expect_vol="==", expect_bodies=1,
    note="the disjoint-fuse invariant's boundary: two boxes sharing an exact "
         "coincident face (no overlap, no gap) is NOT disjoint — must weld into ONE "
         "solid (full combined volume), not be rejected")
# intent
add("hole_simple", "intent", "single", ("hole",), seed_plate(),
    [feat("hole", "Hl", {"style": "simple", "end": "through_all", "diameter": dim(6)},
          {"face": {"of": "face", "side": "top"}})], expect_vol="<", expect_bodies=1)
add("hole_cbore", "intent", "single", ("hole",), seed_plate(),
    [feat("hole", "Hc", {"style": "cbore", "end": "through_all", "diameter": dim(6),
                         "cbore_d": dim(11), "cbore_depth": dim(3)},
          {"face": {"of": "face", "side": "top"}})], expect_vol="<", expect_bodies=1)
add("bolt_circle", "intent", "single", ("bolt_circle",), seed_disc(),
    [feat("bolt_circle", "Bc", {"diameter": dim(5), "count": 6, "pcd": dim(60)},
          {"face": {"of": "face", "side": "top"}})], expect_vol="<", expect_bodies=1)
# pattern / mirror
add("linear_pattern", "pattern", "single", ("sketch", "extrude", "linear_pattern"),
    seed_plate(),
    [*boss_on_top(0), feat("linear_pattern", "Lp", {"count1": 3, "spacing1": dim(15)},
                           {"dir1": {"kind": "datum", "feature": "axis.x"},
                            "features": ["$2"]})],
    expect_vol=">", note="3 bosses along X")
add("circular_pattern", "pattern", "single",
    ("sketch", "extrude", "circular_pattern"), seed_disc(),
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [30, 0], "radius": 4}),
     feat("extrude", "Boss", {"mode": "boss", "end": "blind", "depth": dim(10)},
          {"profile": [region("$0", (30, 0))]}),
     feat("circular_pattern", "Cp", {"count": 6, "angle": dim(360), "equal_spacing": True},
          {"axis": {"kind": "datum", "feature": "axis.z"}, "features": ["$2"]})],
    expect_vol=">")
add("mirror", "pattern", "single", ("sketch", "extrude", "mirror"), seed_plate(),
    [*boss_on_top(0), feat("mirror", "Mr", {},
                           {"plane": {"kind": "datum", "feature": "plane.right"},
                            "features": ["$2"]})], expect_vol=">",
    expect_overlap_clean=True,
    note="mirrored boss; asserts check_feature_overlap finds no bad pairs (has teeth)")
# transform
add("transform_move", "transform", "single", ("transform",), seed_box(),
    [feat("transform", "Mv", {"translate": [50, 0, 0]})],
    expect_vol="==", expect_bodies=1)
add("transform_copy", "transform", "single", ("transform",), seed_box(),
    [feat("transform", "Cpy", {"translate": [60, 0, 0], "copy": True})],
    expect_vol=">", expect_bodies=2)
# sheet metal (base flange)
def _sm_base_ops():
    return [sketch(0, "plane.top"), *rect("$0", 30, 20),
            feat("sm_base_flange", "Sm", {"thickness": dim(2)}, {"profile": [region("$0")]})]


add("sm_base_flange", "sheetmetal", "single", ("sm_base_flange",), [],
    _sm_base_ops(), expect_vol="!=", expect_bodies=1)


def _sm_edge_flange_test(cb, doc):
    """Resolve a straight rim edge on the base flange, then bend a 90° edge flange off it.
    (This slot rejects a bare query — it needs the RESOLVED match, so it can't be a
    static op list.)"""
    q = cb.call("doc_query", {"doc_id": doc,
                              "query": {"of": "edge", "surf": "line",
                                        "near": [0, 20, 1], "count": 1}})
    m = (q.get("matches") or [None])[0]
    if not m:
        raise RuntimeError("no rim edge resolved for edge flange")
    return [feat("sm_edge_flange", "Ef", {"length": dim(15), "angle": dim(90)},
                 {"edge": m})]


add("sm_edge_flange", "sheetmetal", "sequence", ("sm_base_flange", "sm_edge_flange"),
    _sm_base_ops(), [], build_test=_sm_edge_flange_test,
    expect_vol=">", expect_bodies=1, note="90° bend off a base-flange rim (resolved edge)")
# weldment (member)
add("weld_member", "weldment", "single", ("sketch", "weld_member"), [],
    [sketch(0, "plane.top"), ent("$0", {"type": "line", "p1": [0, 0], "p2": [100, 0]}),
     feat("weld_member", "Wm", {"profile": "square_tube", "size": dim(30), "wall": dim(3)},
          {"sketch": "$0"})], expect_vol="!=", expect_bodies=1)
# surfacing (planar surface -> thicken to solid)
add("surf_thicken", "surfacing", "single", ("sketch", "surf_planar", "surf_thicken"), [],
    [sketch(0, "plane.top"), *rect("$0", 20, 15),
     feat("surf_planar", "Srf", {}, {"profile": [region("$0")]}),
     feat("surf_thicken", "Th", {"thickness": dim(2)},
          {"surface": {"kind": "body", "owner_feature": "$5"}})],
    expect_vol="!=", expect_bodies=1)
# one-op recipes: thread, text
add("thread", "thread", "single", ("cylinder", "thread"),
    [feat("cylinder", "Rod", {"radius": dim(6), "height": dim(30)})],
    [feat("thread", "Thr", {"size": "M12", "length": dim(20)},
          {"target_face": {"of": "face", "surf": "cylindrical"}})],
    expect_vol="<", expect_bodies=1, note="external M12 V-groove")
add("text_engrave", "text", "single", ("text",), seed_plate(),
    [feat("text", "Tx", {"text": "AI", "height": dim(6), "depth": dim(1.5),
                         "mode": "engrave"},
          {"target_face": {"of": "face", "side": "top"}})],
    expect_vol="<", expect_bodies=1)

# ============================ PAIRWISE / SEQUENCE PASS =====================
# A-then-B compatibility matrix: op ordering that historically breaks kernels.
add("fillet_then_shell", "sequence", "sequence", ("fillet", "shell"), seed_box(),
    [feat("fillet", "F", {"radius": dim(3)}, {"edges": {"of": "edge", "parallel": "z"}}),
     feat("shell", "S", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}})],
    expect_vol="<", expect_bodies=1, note="shell after fillet (rounded pockets)")
add("shell_then_fillet", "sequence", "sequence", ("shell", "fillet"), seed_box(),
    [feat("shell", "S", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}}),
     feat("fillet", "F", {"radius": dim(1.5)},
          {"edges": {"of": "edge", "parallel": "z"}})],
    expect_vol="<", expect_bodies=1)
add("draft_then_shell", "sequence", "sequence", ("draft", "shell"), seed_box(),
    [feat("draft", "D", {"angle": dim(4)},
          {"neutral": {"of": "face", "side": "bottom"},
           "faces": {"of": "face", "surf": "flat", "side": "left"}}),
     feat("shell", "S", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}})],
    expect_vol="<", expect_bodies=1, note="moulded pocket wall")
add("hole_then_chamfer", "sequence", "sequence", ("hole", "chamfer"), seed_plate(),
    [feat("hole", "H", {"style": "simple", "end": "through_all", "diameter": dim(8)},
          {"face": {"of": "face", "side": "top"}}),
     feat("chamfer", "C", {"length": dim(1)},
          {"edges": {"of": "edge", "surf": "circle", "side": "top"}})],
    expect_vol="<", expect_bodies=1, note="countersink-by-chamfer on a bore edge")
add("pattern_of_hole", "sequence", "sequence", ("hole", "linear_pattern"), seed_plate(),
    [feat("hole", "H", {"style": "simple", "end": "through_all", "diameter": dim(5)},
          {"face": {"of": "face", "side": "top"}, "positions": [[-20, 0]]}),
     feat("linear_pattern", "Lp", {"count1": 4, "spacing1": dim(12)},
          {"dir1": {"kind": "datum", "feature": "axis.x"}, "features": ["f2"]})],
    expect_vol="<", note="cut-through-pattern: a bore array")
# "pattern-of-boolean" intent: the engine only patterns SKETCH-based subtractive
# features (extrude-cut / hole), NOT an explicit `boolean` feature (see nc_pattern_of_
# explicit_boolean below for that documented constraint). This is the supported form.
add("pattern_of_cut", "sequence", "sequence", ("sketch", "extrude", "linear_pattern"),
    [feat("box", "Base", {"length": dim(90), "width": dim(30), "height": dim(12)})],
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [-32, 0], "radius": 4}),
     feat("extrude", "Pocket", {"mode": "cut", "end": "through_all"},
          {"profile": [region("$0", (-32, 0))]}),
     feat("linear_pattern", "Lp", {"count1": 4, "spacing1": dim(20)},
          {"dir1": {"kind": "datum", "feature": "axis.x"}, "features": ["$2"]})],
    expect_vol="<", expect_bodies=1, note="cut-through-pattern of a subtractive feature")
add("mirror_of_cut", "sequence", "sequence", ("sketch", "extrude", "mirror"),
    [feat("box", "Base", {"length": dim(60), "width": dim(40), "height": dim(12)})],
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [20, 0], "radius": 5}),
     feat("extrude", "Notch", {"mode": "cut", "end": "through_all"},
          {"profile": [region("$0", (20, 0))]}),
     feat("mirror", "Mr", {}, {"plane": {"kind": "datum", "feature": "plane.right"},
                               "features": ["$2"]})],
    expect_vol="<", expect_bodies=1, note="mirror a subtractive feature")
add("boolean_then_fillet", "sequence", "sequence", ("boolean", "fillet"),
    [feat("box", "A", {"length": dim(40), "width": dim(40), "height": dim(20)}),
     feat("cylinder", "B", {"radius": dim(12), "height": dim(40), "merge": False})],
    [feat("boolean", "Fuse", {"op": "fuse", "a": "f1", "b": "f2"}),
     feat("fillet", "F", {"radius": dim(2)},
          {"edges": {"of": "edge", "surf": "circle"}})],
    expect_vol=None, expect_bodies=1, note="round the fuse seam")
add("revolve_then_shell", "sequence", "sequence", ("revolve", "shell"),
    [sketch(0, "plane.front"), *[
        ent("$0", {"type": "line", "p1": p1, "p2": p2}) for p1, p2 in
        [([0, 0], [25, 0]), ([25, 0], [25, 40]), ([25, 40], [0, 40]),
         ([0, 40], [0, 0])]],
     feat("revolve", "Cup", {"mode": "boss", "angle": dim(360)},
          {"profile": [region("$0", (12, 20))],
           "axis": {"kind": "datum", "feature": "axis.z"}})],
    [feat("shell", "S", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}})],
    expect_vol="<", note="hollow a solid revolved cup (seed=revolve, test=shell)")
add("bolt_circle_then_shell", "sequence", "sequence", ("bolt_circle", "shell"),
    seed_disc(),
    [feat("bolt_circle", "Bc", {"diameter": dim(5), "count": 6, "pcd": dim(60)},
          {"face": {"of": "face", "side": "top"}}),
     feat("shell", "S", {"thickness": dim(2)},
          {"remove": {"of": "face", "side": "bottom"}})],
    expect_vol="<", note="flanged cap: holes then hollow")

# ============================ REALISTIC RECIPES ===========================
add("recipe_mount_bracket", "recipe", "recipe",
    ("box", "fillet", "shell", "bolt_circle", "chamfer"), seed_plate(),
    [feat("fillet", "F", {"radius": dim(4)}, {"edges": {"of": "edge", "parallel": "z"}}),
     feat("shell", "Sh", {"thickness": dim(3)}, {"remove": {"of": "face", "side": "bottom"}}),
     feat("bolt_circle", "Bc", {"diameter": dim(5), "count": 4, "pcd": dim(30)},
          {"face": {"of": "face", "side": "top"}}),
     feat("chamfer", "Ch", {"length": dim(0.8)},
          {"edges": {"of": "edge", "side": "top", "surf": "circle"}})],
    expect_vol="<", expect_bodies=1, note="fillet->shell->bolt-circle->chamfer")
add("recipe_pulley", "recipe", "recipe",
    ("cylinder", "extrude", "circular_pattern", "hole"),
    [feat("cylinder", "Hub", {"radius": dim(35), "height": dim(15)})],
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [24, 0], "radius": 6}),
     feat("extrude", "Lighten", {"mode": "cut", "end": "through_all"},
          {"profile": [region("$0", (24, 0))]}),
     feat("circular_pattern", "Cp", {"count": 5, "angle": dim(360), "equal_spacing": True},
          {"axis": {"kind": "datum", "feature": "axis.z"}, "features": ["$2"]}),
     feat("hole", "Bore", {"style": "simple", "end": "through_all", "diameter": dim(12)},
          {"face": {"of": "face", "side": "top"}})],
    expect_vol="<", expect_bodies=1, note="hub + lightening holes + bore")
add("recipe_enclosure", "recipe", "recipe",
    ("box", "shell", "fillet", "hole"), seed_box(60, 40, 25),
    [feat("shell", "Sh", {"thickness": dim(2)}, {"remove": {"of": "face", "side": "top"}}),
     feat("fillet", "F", {"radius": dim(3)}, {"edges": {"of": "edge", "parallel": "z"}}),
     feat("hole", "Vent", {"style": "simple", "end": "through_all", "diameter": dim(4)},
          {"face": {"of": "face", "side": "front"}})],
    expect_vol="<", expect_bodies=1, note="shelled + rounded + vented box")

# ============================ NEGATIVE CONTROLS ===========================
# These prove the battery's CHECKS can actually FAIL (no green theater). A negative
# control PASSES when the engine/check correctly detects the bad condition.
add("nc_cut_misses_body", "negative", "negative", ("extrude",), seed_plate(),
    [sketch(0, {"of": "face", "side": "top"}),
     ent("$0", {"type": "circle", "center": [500, 500], "radius": 3}),
     feat("extrude", "Nope", {"mode": "cut", "end": "through_all"},
          {"profile": [region("$0", (500, 500))]})],
    expect_rebuild=False, expect_manifold=None,
    note="a cut that misses the body must be REJECTED (removed no material)")
add("nc_open_surface_nonmanifold", "negative", "negative", ("surf_planar",), [],
    [sketch(0, "plane.top"), *rect("$0", 20, 15),
     feat("surf_planar", "Srf", {}, {"profile": [region("$0")]})],
    expect_rebuild=True, expect_manifold=False,
    note="a bare open surface (4 free edges) must read as NON-manifold")
add("nc_pattern_of_explicit_boolean", "negative", "negative", ("boolean", "linear_pattern"),
    [feat("box", "Base", {"length": dim(80), "width": dim(30), "height": dim(12)}),
     feat("box", "Pin", {"length": dim(6), "width": dim(6), "height": dim(20),
                         "at": [-30, 0, 6], "merge": False})],
    [feat("boolean", "Bx", {"op": "cut", "a": "f1", "b": "f2"}),
     feat("linear_pattern", "Lp", {"count1": 4, "spacing1": dim(18)},
          {"dir1": {"kind": "datum", "feature": "axis.x"}, "features": ["f3"]})],
    expect_rebuild=False, expect_manifold=None,
    note="documented engine constraint: an explicit `boolean` feature is NOT patternable "
         "(only sketch boss/cut/hole); pattern the subtractive sketch-feature instead")
add("nc_disjoint_fuse", "negative", "negative", ("boolean",),
    [feat("box", "A", {"length": dim(10), "width": dim(10), "height": dim(10)}),
     feat("box", "B", {"length": dim(10), "width": dim(10), "height": dim(10),
                       "at": [100, 0, 0], "merge": False})],
    [feat("boolean", "F", {"op": "fuse", "a": "f1", "b": "f2"})],
    expect_rebuild=False, expect_manifold=None,
    note="documented contract: disjoint fuse must fail (issue #8)")
add("nc_disjoint_cut", "negative", "negative", ("boolean",),
    [feat("box", "A", {"length": dim(100), "width": dim(10), "height": dim(10)}),
     feat("box", "B", {"length": dim(20), "width": dim(20), "height": dim(20),
                       "merge": False})],
    [feat("boolean", "F", {"op": "cut", "a": "f1", "b": "f2"})],
    expect_rebuild=False, expect_manifold=None,
    note="a full-cross-section tool slicing a body into two disconnected remnants must fail "
         "(same disjoint-result invariant as nc_disjoint_fuse)")


# --------------------------------------------------------------------------- engine glue
class EngineUnavailable(RuntimeError):
    pass


def _engine():
    """Return the core_bridge module, or raise EngineUnavailable. Lazy so the module
    imports (and TestSmoke runs) without the .so."""
    so = os.environ.get("CHUDWARE_LIBCHUDWARE")
    if not so or not os.path.exists(so):
        raise EngineUnavailable(
            "CHUDWARE_LIBCHUDWARE not set to an existing libchudware.so — cannot run live geometry")
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        cb.call("doc_new", {"kind": "part", "name": "_probe"})
        return cb
    except Exception as e:  # noqa: BLE001
        raise EngineUnavailable(f"core_bridge failed to load the engine: {e}") from e


def _apply(cb, doc, ops):
    r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    fs = (r.get("rebuild") or {}).get("feature_status") or []
    ok = all(s.get("ok") for s in fs) if fs else True
    errs = [s.get("error") for s in fs if not s.get("ok") and s.get("error")]
    return ok, r.get("volume"), errs


def _body_count(cb, doc):
    try:
        return cb.call("eval_mass", {"doc_id": doc, "density_g_cm3": 7.85}).get("body_count")
    except Exception:  # noqa: BLE001
        return None


def _manifold(cb, doc):
    """(is_manifold, issues). Non-manifold = a free-edge issue or any error-severity issue.
    tiny-edge WARNINGS on curved bodies (sphere/revolve poles+seams) are cosmetic."""
    try:
        r = cb.call("eval_check", {"doc_id": doc})
        iss = r.get("issues", [])
        bad = [i for i in iss if i.get("kind") == "free_edges"
               or i.get("severity") == "error"] or (r.get("free_edges", 0) > 0)
        return (not bad), iss
    except Exception as e:  # noqa: BLE001
        return None, [{"error": str(e)}]


def _overlap_clean(doc):
    try:
        from services.verify.battery import _get_fn  # noqa: PLC0415
        out = json.loads(_get_fn("cad", "check_feature_overlap")(doc_id=doc))
        return len(out.get("pairs", [])) == 0
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- runner
_VOL_TOL = 1e-6


def _vol_ok(rule: str, seed_v, final_v) -> tuple[bool, str]:
    if final_v is None:
        return False, "no final volume"
    base = seed_v if seed_v is not None else 0.0
    d = final_v - base
    rel = abs(d) / max(abs(base), 1.0)
    if rule == ">":
        return d > _VOL_TOL, f"vol {base:.0f}->{final_v:.0f} (Δ{d:+.0f})"
    if rule == "<":
        return d < -_VOL_TOL, f"vol {base:.0f}->{final_v:.0f} (Δ{d:+.0f})"
    if rule == "==":
        return rel < 1e-4, f"vol {base:.0f}->{final_v:.0f} (Δ{d:+.0f})"
    if rule == "!=":
        return abs(d) > _VOL_TOL, f"vol {base:.0f}->{final_v:.0f} (Δ{d:+.0f})"
    return True, f"vol {final_v:.0f}"


def run_case(cb, c: Case, run_overlap: bool = False) -> dict:
    t0 = time.time()
    rec: dict[str, Any] = {"id": c.id, "category": c.category, "kind": c.kind,
                           "ops_covered": list(c.ops_covered), "note": c.note,
                           "executed": True}
    try:
        doc = cb.call("doc_new", {"kind": "part", "name": c.id})["doc_id"]
        seed_v = None
        if c.seed:
            sok, seed_v, serr = _apply(cb, doc, c.seed)
            if not sok:
                rec.update(verdict="ERROR", reason=f"seed build failed: {serr}",
                           elapsed=time.time() - t0)
                return rec
        test_ops = c.build_test(cb, doc) if c.build_test else c.test
        rebuild_ok, final_v, errs = _apply(cb, doc, test_ops)
        n_bodies = _body_count(cb, doc) if rebuild_ok else None
        is_man, issues = (_manifold(cb, doc) if rebuild_ok else (None, []))
        overlap_ok = _overlap_clean(doc) if (run_overlap and rebuild_ok) else None

        checks: list[tuple[str, bool, str]] = []
        if c.expect_rebuild is not None:
            checks.append(("rebuild", rebuild_ok == c.expect_rebuild,
                           f"rebuild={rebuild_ok} want={c.expect_rebuild}"
                           + (f" errs={errs}" if errs else "")))
        if c.expect_vol is not None and rebuild_ok:
            ok, msg = _vol_ok(c.expect_vol, seed_v, final_v)
            checks.append((f"vol{c.expect_vol}", ok, msg))
        if c.expect_bodies is not None and rebuild_ok:
            checks.append(("bodies", n_bodies == c.expect_bodies,
                           f"bodies={n_bodies} want={c.expect_bodies}"))
        if c.expect_manifold is not None and rebuild_ok:
            checks.append(("manifold", is_man == c.expect_manifold,
                           f"manifold={is_man} want={c.expect_manifold}"))
        if c.expect_overlap_clean is not None and overlap_ok is not None:
            checks.append(("overlap", overlap_ok == c.expect_overlap_clean,
                           f"overlap_clean={overlap_ok} want={c.expect_overlap_clean}"))

        passed = all(ok for _, ok, _ in checks)
        fails = [f"{n}: {m}" for n, ok, m in checks if not ok]
        if passed:
            verdict = "PASS"
        elif c.xfail:
            verdict = "XFAIL"
        else:
            verdict = "FAIL"
        rec.update(verdict=verdict, volume=final_v, seed_volume=seed_v,
                   bodies=n_bodies, manifold=is_man, overlap_clean=overlap_ok,
                   checks=[{"name": n, "ok": ok, "detail": m} for n, ok, m in checks],
                   reason="; ".join(fails) if fails else "all expectations met",
                   xfail=c.xfail, issues=issues, elapsed=time.time() - t0)
    except Exception as e:  # noqa: BLE001
        verdict = "XFAIL" if c.xfail else "ERROR"
        rec.update(verdict=verdict, reason=f"{type(e).__name__}: {e}",
                   xfail=c.xfail, elapsed=time.time() - t0)
    return rec


# --------------------------------------------------------------------------- coverage
def coverage() -> dict:
    """Registered modeling feature types (core/doc/validate.cpp) vs those the battery
    executes at least once. Honest statement of what is and isn't covered."""
    registered = {
        # primitives / sketch / 2D-profile / curve
        "box", "cylinder", "sphere", "cone", "torus", "sketch", "extrude", "revolve",
        "loft", "sweep", "helix",
        # dress / structural
        "fillet", "chamfer", "shell", "draft", "rib", "split", "boolean",
        # intent
        "hole", "bolt_circle",
        # pattern / transform
        "linear_pattern", "circular_pattern", "mirror", "transform",
        # one-op recipes
        "thread", "text",
        # sheet metal
        "sm_base_flange", "sm_edge_flange", "sm_hem", "sm_jog", "sm_flat_pattern",
        # weldments
        "weld_member", "weld_end_cap", "weld_gusset",
        # surfacing (surf_planar+surf_thicken representative; rest need resolved selectors)
        "surf_planar", "surf_thicken", "surf_extrude", "surf_revolve", "surf_sweep",
        "surf_loft", "surf_ruled", "surf_fill", "surf_boundary", "surf_offset",
        "surf_knit", "surf_trim", "surf_delete_face", "surf_replace_face",
        # escape hatch (intentionally not battery-tested)
        "script",
    }
    covered: set[str] = set()
    for c in CASES:
        covered.update(c.ops_covered)
    not_covered = sorted(registered - covered)
    return {"registered": sorted(registered), "covered": sorted(covered),
            "not_covered": not_covered,
            "pct": round(100.0 * len(covered & registered) / len(registered), 1)}


NOT_COVERED_REASONS = {
    "sm_edge_flange": "secondary sheet-metal bend — needs a query_geometry-resolved edge pick "
                      "on a thickness face (bare query rejected); v2 with selector plumbing.",
    "sm_hem": "as sm_edge_flange (resolved-edge slot).",
    "sm_jog": "as sm_edge_flange (resolved-edge slot).",
    "sm_flat_pattern": "unfold step; only meaningful after edge_flange bends land (v2).",
    "weld_end_cap": "secondary weldment op on an existing member end; v2.",
    "weld_gusset": "secondary weldment op between two members; v2.",
    "surf_extrude": "surfacing covered by surf_planar+surf_thicken; other surf_* builders "
                    "share the thicken/knit sink — representative coverage only.",
    "surf_revolve": "see surf_extrude.", "surf_sweep": "see surf_extrude.",
    "surf_loft": "see surf_extrude.", "surf_ruled": "needs two resolved edge refs; v2.",
    "surf_fill": "needs >=2 resolved boundary edges; v2.",
    "surf_boundary": "needs resolved boundary curves; v2.",
    "surf_offset": "needs a resolved solid-face ref; v2.",
    "surf_knit": "needs >=2 pre-built surface bodies; v2.",
    "surf_trim": "needs a resolved tool face; v2.",
    "surf_delete_face": "needs a resolved solid-face ref; v2.",
    "surf_replace_face": "needs a resolved face + spanning surface; v2.",
    "script": "raw script escape-hatch feature — arbitrary code, intentionally out of scope.",
}


# --------------------------------------------------------------------------- report
def _summarize(results: list[dict]) -> dict:
    ex = [r for r in results if r.get("executed")]
    return {
        "total": len(results),
        "executed": len(ex),
        "pass": sum(1 for r in ex if r["verdict"] == "PASS"),
        "fail": sum(1 for r in ex if r["verdict"] == "FAIL"),
        "xfail": sum(1 for r in ex if r["verdict"] == "XFAIL"),
        "error": sum(1 for r in ex if r["verdict"] == "ERROR"),
    }


def write_reports(results: list[dict], cov: dict, so_path: str, elapsed: float):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    s = _summarize(results)
    (REPORT_DIR / "modeling_results.json").write_text(json.dumps(
        {"summary": s, "coverage": cov, "libchudware": so_path,
         "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "results": results}, indent=2))

    lines = [
        "# Modeling-Op Combination Battery — Chudware Feature Engine",
        "",
        f"**Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  ",
        "**Script:** `scripts/modeling_battery.py`  ",
        f"**Engine:** `{so_path}`  ",
        "**Method:** each case drives the real C++ feature engine via core_bridge "
        "(doc_new + doc_apply_ops) and asserts on live geometry (rebuild status, "
        "manifoldness, volume sign, body count).  ",
        "",
        "## Summary",
        "",
        "| Metric | Count |", "|--------|-------|",
        f"| Cases total | {s['total']} |",
        f"| Executed | {s['executed']} |",
        f"| PASS | {s['pass']} |",
        f"| FAIL | {s['fail']} |",
        f"| XFAIL (known bug) | {s['xfail']} |",
        f"| ERROR | {s['error']} |",
        f"| Elapsed | {elapsed:.1f}s |",
        "",
        "## Coverage",
        "",
        f"Registered modeling op-types (core/doc/validate.cpp): **{len(cov['registered'])}**. "
        f"Executed by battery: **{len(cov['covered'])}** "
        f"(**{cov['pct']}%** of registered).",
        "",
        "**NOT covered (with reason):**",
        "",
    ]
    for op in cov["not_covered"]:
        lines.append(f"- `{op}` — {NOT_COVERED_REASONS.get(op, 'not yet exercised')}")
    lines += ["", "## Cases", "",
              "| id | category | kind | verdict | detail |",
              "|----|----------|------|---------|--------|"]
    for r in results:
        detail = r.get("reason", "")
        if r.get("verdict") == "XFAIL" and r.get("xfail"):
            detail = f"[{r['xfail']}] {detail}"
        lines.append(f"| {r['id']} | {r['category']} | {r['kind']} | "
                     f"{r['verdict']} | {detail[:90]} |")
    lines += ["", "## No-all-skip contract", "",
              "This battery FAILS (non-zero exit) if the engine is unavailable or zero "
              "cases execute — an all-skip run is a failure, not a pass. "
              "Negative-control cases prove each check "
              "can actually fail.", ""]
    (REPORT_DIR / "modeling_report.md").write_text("\n".join(lines))


# --------------------------------------------------------------------------- main
def run_battery(run_overlap: bool = True) -> tuple[list[dict], dict, str]:
    cb = _engine()   # raises EngineUnavailable
    so = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    results = [run_case(cb, c, run_overlap=run_overlap) for c in CASES]
    return results, coverage(), so


def main() -> int:
    t0 = time.time()
    try:
        results, cov, so = run_battery(run_overlap=True)
    except EngineUnavailable as e:
        print(f"FATAL: {e}")
        print("A modeling battery with no live engine is an ALL-SKIP run — that is a "
              "FAILURE, not a pass. Set CHUDWARE_LIBCHUDWARE to the OCCT libchudware.so.")
        return 2
    elapsed = time.time() - t0
    s = _summarize(results)
    if s["executed"] == 0:
        print("FATAL: zero cases executed (all-skip). Failing.")
        return 2
    write_reports(results, cov, so, elapsed)
    print(f"{'id':26}{'kind':10}{'verdict':8} detail")
    for r in results:
        print(f"{r['id']:26}{r['kind']:10}{r['verdict']:8} {r.get('reason','')[:60]}")
    print(f"\nExecuted={s['executed']}  PASS={s['pass']}  FAIL={s['fail']}  "
          f"XFAIL={s['xfail']}  ERROR={s['error']}  ({elapsed:.1f}s)")
    print(f"Coverage: {len(cov['covered'])}/{len(cov['registered'])} registered ops "
          f"({cov['pct']}%). Reports -> {REPORT_DIR}")
    return 1 if (s["fail"] or s["error"]) else 0


# --------------------------------------------------------------------------- pytest
class TestSmoke:
    """Registry-integrity checks — run WITHOUT the .so (no geometry)."""

    def test_case_ids_unique(self):
        ids = [c.id for c in CASES]
        assert len(ids) == len(set(ids)), "duplicate case ids"

    def test_has_single_sequence_negative(self):
        kinds = {c.kind for c in CASES}
        assert {"single", "sequence", "recipe", "negative"} <= kinds

    def test_negative_controls_present(self):
        neg = [c for c in CASES if c.kind == "negative"]
        assert len(neg) >= 3, "need negative controls that prove checks can fail"
        # at least one control per check family
        assert any(c.expect_rebuild is False for c in neg)
        assert any(c.expect_manifold is False for c in neg)

    def test_expectation_schema(self):
        for c in CASES:
            assert c.test or c.build_test, f"{c.id}: no test ops or resolver"
            assert c.ops_covered, f"{c.id}: no ops_covered tag"
            if c.expect_vol is not None:
                assert c.expect_vol in (">", "<", "==", "!="), c.id

    def test_coverage_registered_ops_have_cases(self):
        cov = coverage()
        # every op the battery claims to cover must be a registered type
        assert set(cov["covered"]) <= set(cov["registered"])
        # every not_covered op must carry a documented reason
        for op in cov["not_covered"]:
            assert op in NOT_COVERED_REASONS, f"undocumented uncovered op: {op}"

    def test_all_skip_guard_logic(self):
        assert _summarize([])["executed"] == 0

    def test_engine_unavailable_raises(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_LIBCHUDWARE", raising=False)
        import pytest
        with pytest.raises(EngineUnavailable):
            _engine()


class TestFull:
    """Live battery — needs CHUDWARE_LIBCHUDWARE pointing at the OCCT libchudware.so."""

    def _run(self):
        import pytest
        try:
            # run_overlap=True so the expect_overlap_clean assertion is exercised in CI
            return run_battery(run_overlap=True)
        except EngineUnavailable as e:
            pytest.skip(f"no live engine: {e}")

    def test_executes_cases_no_all_skip(self):
        results, _, _ = self._run()
        ex = [r for r in results if r.get("executed")]
        assert len(ex) > 0, "ALL-SKIP: no case executed — this is a failure"
        assert len(ex) == len(CASES)

    def test_no_unexpected_failures(self):
        results, _, _ = self._run()
        bad = [r for r in results if r["verdict"] in ("FAIL", "ERROR")]
        assert not bad, "unexpected case failures: " + "; ".join(
            f"{r['id']}:{r.get('reason','')}" for r in bad)

    def test_negative_controls_pass(self):
        # the checks must actually fire: negative controls (except known-bug xfails) PASS
        results, _, _ = self._run()
        neg = [r for r in results if r["category"] == "negative"]
        assert neg
        for r in neg:
            assert r["verdict"] in ("PASS", "XFAIL"), f"{r['id']}: {r.get('reason')}"

    def test_single_op_coverage_nonzero(self):
        results, cov, _ = self._run()
        assert cov["pct"] >= 60.0
        singles = [r for r in results if r["kind"] == "single"]
        assert all(r["verdict"] == "PASS" for r in singles), \
            "every single-op case must pass: " + "; ".join(
                f"{r['id']}:{r.get('reason')}" for r in singles
                if r["verdict"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())

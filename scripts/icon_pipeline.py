#!/usr/bin/env python3
"""T-G15 screenshot-based ribbon icon pipeline.

Regenerable, one-command pipeline that turns a data-driven recipe table into ribbon
icon tiles that are *actual clean-viewport screenshots of demo parts* (per Erik's
T-G12 round-1 ask), then injects them into the icon atlases the C++ loader consumes.

Flow (see docs in each function):
  1. RECIPES  -> a JSON recipe list (name, camera, build steps).
  2. The AI-CAD GUI, run headless with AI_CAD_SHOT=iconpipe + AI_CAD_ICON_RECIPES,
     builds each demo part (newPart + applyOps, the real engine) and renders it through
     the renderer's TRANSPARENT thumbnail path (no chrome, no gizmo, alpha=0 background)
     to scratch/icon_render/<name>.png at 2x-ish resolution.
  3. Each render is fitted (aspect-preserving, small inset) into that icon's @32 and @20
     atlas cells and composited into atlas_dark.png AND atlas_light.png (photographic
     renders are full-colour, so both theme atlases hold the same art -- matches
     pack_icons.py). All OTHER cells are preserved byte-for-byte.
  4. A contact sheet of every generated tile is written for round-2 human review.

The recipes double as a smoke test: a build step that fails prints ok=0 and yields an
empty tile, which the contact sheet makes obvious.

Shared visual contract (work/tasks/T-G15.md): icons carry NO baked background plate --
they render on transparency so the one asset works on the ribbon glass in both light and
dark mode. Modified-face emphasis (selection-highlight colour) is a follow-up layer (needs
a highlight overlay in the thumbnail FBO keyed by selection topo-ids) -- see the handoff.

Usage:
  python3 scripts/icon_pipeline.py --render        # run the app + regenerate + inject
  python3 scripts/icon_pipeline.py --inject-only   # re-tile from existing scratch renders
  python3 scripts/icon_pipeline.py --list          # print the recipe table
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "host" / "build-linux" / "bin" / "AI-CAD"
ICON_DIR = REPO / "host" / "assets" / "icons"
DEPLOYED_ICON_DIR = REPO / "host" / "build-linux" / "bin" / "assets" / "icons"
RENDER_DIR = REPO / "scratch" / "icon_render"
CONTACT_SHEET = REPO / "scratch" / "icon_contact_sheet.png"
RENDER_PX = 128  # square offscreen render size; downscaled into the 32/20 cells


def dim(v: float) -> dict:
    """Engine dimension object: a literal value with no driving expression."""
    return {"value": float(v), "expr": None}


def box(length=52.0, width=40.0, height=30.0, name="Box1") -> dict:
    """A centred rectangular block -- the common base body for the dress-up demos."""
    return {"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": dim(length), "width": dim(width), "height": dim(height)},
        "centered": True}}


# ---------------------------------------------------------------------------------------
# RECIPE TABLE  (icon-name -> demo build).  icon-name MUST be an existing atlas.json cell
# so the UVs stay put and the C++ loader needs no change.  Each recipe:
#   yaw/pitch : camera (deg), iso-ish so the family reads consistently.
#   steps     : ordered build steps. A step is either a raw {op:...} (applied verbatim) or
#               {feature:{...}, selrefs:{<refkey>:{select,n}}} whose edge/face refs are
#               resolved at runtime from the built geometry (dress-up ops need real topo
#               picks). Selectors: "vertical_edges" (|axis.z|~1), "top_face" (highest
#               centroid Z) -- the same resolvePick patterns the m1 scenario uses.
#
# First tranche (this session): Features 3D-Body + Dress-up + Appearance material presets,
# proven end-to-end. Remaining groups (Pattern/Multi-body/Curves, Sheet-Metal, Manufacture,
# glass/paint presets) are handed off -- Manufacture needs the CAM-simulator source, not
# this modeling viewport; patterns/booleans need their op schemas verified first.
# ---------------------------------------------------------------------------------------


def appearance(metallic: float, roughness: float, rgb: tuple[float, float, float]) -> dict:
    """set_appearance on the first body -- the exact op the m1 scenario uses. Values must
    mirror ui::kMaterials (host/src/ui/appearance_common.cpp) so the icon IS the preset."""
    return {"op": "set_appearance", "id": "b1", "color": list(rgb),
            "metallic": metallic, "roughness": roughness}


def sketch(entities: list, plane: str = "plane.front", name: str = "Sk") -> dict:
    """Single add_feature step that creates a sketch WITH inline entities (params.entities).
    Using inline entities keeps each iconpipe step self-contained (one add_feature op per
    applyOps call). The feature is assigned a persistent id by the engine (f1, f2, ...) which
    later steps reference by that id -- NOT $N batch-local refs (those only work within one
    applyOps batch; each step is its own call in the runner)."""
    return {"op": "add_feature", "feature": {
        "type": "sketch", "name": name,
        "params": {"entities": entities, "constraints": []},
        "refs": {"plane": plane}}}


def datum(axis: str) -> dict:
    """Datum axis selector (axis.x / axis.y / axis.z) for pattern/mirror ops."""
    return {"kind": "datum", "feature": axis}


def region(sketch_id: str, cx: float = 0.0, cy: float = 0.0) -> dict:
    """Region selector for a sketch profile."""
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy], "hint_index": 0}


def axis_edge(sketch_id: str, cx: float, cy: float, cz: float,
              ax: float, ay: float, az: float, length: float = 40.0) -> dict:
    """Line edge selector (for revolve axis) -- a straight edge in the named sketch."""
    return {"kind": "edge", "surf": "line",
            "centroid": [cx, cy, cz], "axis": [ax, ay, az],
            "area_or_len": length, "owner_feature": sketch_id, "hint_index": 0}


def body_sel(feature_id: str) -> dict:
    """Body selector for boolean ops (targets / tools).
    Canonical format: {"kind":"body","owner_feature":"fN"} — see document.cpp line 389."""
    return {"kind": "body", "owner_feature": feature_id}


RECIPES: dict[str, dict] = {
    # -----------------------------------------------------------------------
    # Features -> 3D Body (first tranche: extrude_boss already live; adding remaining)
    # -----------------------------------------------------------------------

    # A plain solid block reads as "create a 3D body / boss".
    "extrude_boss": {"yaw": 35, "pitch": 26, "steps": [box(52, 40, 34)]},

    # revolve_boss: L-shaped cross-section on plane.front, revolved 360° around the
    # Y-axis.  plane.front is the XZ plane (right=X, up=Z); the axis is X=0 in the sketch,
    # which maps to the world Y-axis (axis=[0,1,0]).  Profile stays at sketch-x > 0 (= world
    # X > 0) so it never crosses the axis (cheatsheet mistake #1).
    "revolve_boss": {"yaw": 35, "pitch": 20, "steps": [
        # Step 1: sketch (plane.front = XZ). axis at sketch-x=0 (construction),
        # L-profile at sketch-x=4..22 (world X=4..22).
        sketch([
            {"type": "line", "p1": [0, -20], "p2": [0, 20], "id": "axis",
             "construction": True},
            {"type": "line", "p1": [4, -18], "p2": [22, -18], "id": "e1"},
            {"type": "line", "p1": [22, -18], "p2": [22, 10], "id": "e2"},
            {"type": "line", "p1": [22, 10], "p2": [12, 10], "id": "e3"},
            {"type": "line", "p1": [12, 10], "p2": [12, 18], "id": "e4"},
            {"type": "line", "p1": [12, 18], "p2": [4, 18], "id": "e5"},
            {"type": "line", "p1": [4, 18], "p2": [4, -18], "id": "e6"},
        ], plane="plane.front", name="RevolveSk"),
        # Step 2: revolve 360° about the axis line in the sketch (Y-axis in world).
        # axis centroid=[0,0,0], axis=[0,1,0] matches the construction line at sketch-x=0
        # on plane.front (XZ).  owner_feature="f1" is the sketch id.
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Revolve1",
            "params": {"mode": "boss", "angle": dim(360)},
            "refs": {
                "profile": [region("f1", 13, 0)],
                "axis": axis_edge("f1", 0, 0, 0, 0, 1, 0, 40),
            }}}]},

    # revolve_cut: open cylinder (sketch+extrude) with a revolved groove cut
    "revolve_cut": {"yaw": 35, "pitch": 20, "steps": [
        # Step 1: cylinder via circle sketch + extrude
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 20, "id": "c1"},
        ], plane="plane.top", name="CylSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl1",
            "params": {"mode": "boss", "depth": dim(40)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: groove profile on plane.front (XZ).  Cyl extrudes +Z from Z=0 to Z=40.
        # Groove cross-section: thin rect at world-X=[12,20], world-Z=[17,23] (mid-height).
        # In plane.front coords: sketch-x=world-X, sketch-y=world-Z.
        sketch([
            {"type": "line", "p1": [0, -25], "p2": [0, 25], "id": "axis",
             "construction": True},
            {"type": "line", "p1": [12, 17], "p2": [20, 17], "id": "g1"},
            {"type": "line", "p1": [20, 17], "p2": [20, 23], "id": "g2"},
            {"type": "line", "p1": [20, 23], "p2": [12, 23], "id": "g3"},
            {"type": "line", "p1": [12, 23], "p2": [12, 17], "id": "g4"},
        ], plane="plane.front", name="GrooveSk"),
        # Step 3: revolve-cut 360° about Y-axis (sketch-x=0 on plane.front)
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {
                "profile": [region("f3", 16, 20)],
                "axis": axis_edge("f3", 0, 0, 0, 0, 1, 0, 50),
            }}}]},

    # sweep: circle profile swept along an L-path -- classic pipe / rail icon
    # Profile sketch (f1) on plane.right (YZ), path sketch (f2) on plane.top (XY).
    "sweep": {"yaw": 40, "pitch": 28, "steps": [
        # Step 1: circular profile (r=5) on plane.right
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 5, "id": "c1"},
        ], plane="plane.right", name="SwProf"),
        # Step 2: L-shaped open path on plane.top
        sketch([
            {"type": "line", "p1": [0, 0], "p2": [30, 0], "id": "p1"},
            {"type": "line", "p1": [30, 0], "p2": [30, 25], "id": "p2"},
        ], plane="plane.top", name="SwPath"),
        # Step 3: sweep (profile=f1, path=f2)
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Pipe1",
            "params": {"mode": "boss"},
            "refs": {
                "profile": [region("f1", 0, 0)],
                "path": region("f2", 15, 0),
            }}}]},

    # sweep_cut: same pipe host, then sweep-cut a slot along a curved path
    "sweep_cut": {"yaw": 40, "pitch": 28, "steps": [
        # Step 1: base block
        sketch([
            {"type": "line", "p1": [-25, -15], "p2": [25, -15], "id": "b1"},
            {"type": "line", "p1": [25, -15], "p2": [25, 15], "id": "b2"},
            {"type": "line", "p1": [25, 15], "p2": [-25, 15], "id": "b3"},
            {"type": "line", "p1": [-25, 15], "p2": [-25, -15], "id": "b4"},
        ], plane="plane.top", name="BaseSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base1",
            "params": {"mode": "boss", "depth": dim(20)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: small square cut profile on plane.right
        sketch([
            {"type": "line", "p1": [-4, 8], "p2": [4, 8], "id": "c1"},
            {"type": "line", "p1": [4, 8], "p2": [4, 16], "id": "c2"},
            {"type": "line", "p1": [4, 16], "p2": [-4, 16], "id": "c3"},
            {"type": "line", "p1": [-4, 16], "p2": [-4, 8], "id": "c4"},
        ], plane="plane.right", name="CutProf"),
        # Step 3: straight path on plane.top
        sketch([
            {"type": "line", "p1": [-20, 0], "p2": [20, 0], "id": "p1"},
        ], plane="plane.top", name="CutPath"),
        # Step 4: sweep-cut
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "SlotCut",
            "params": {"mode": "cut"},
            "refs": {
                "profile": [region("f3", 0, 12)],
                "path": region("f4", 0, 0),
            }}}]},

    # loft: circle at bottom, square at top (50mm up) -- the classic blend icon
    "loft": {"yaw": 35, "pitch": 30, "steps": [
        # Step 1: circle sketch on plane.top (Z=0)
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="LoftBot"),
        # Step 2: square sketch on plane.top offset 50mm up
        # offset is a TOP-LEVEL feature field (document.cpp reads feat.at("offset"),
        # not params.offset); refs.plane sets the base plane.
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "LoftTop",
            "params": {
                "entities": [
                    {"type": "line", "p1": [-12, -12], "p2": [12, -12], "id": "s1"},
                    {"type": "line", "p1": [12, -12], "p2": [12, 12], "id": "s2"},
                    {"type": "line", "p1": [12, 12], "p2": [-12, 12], "id": "s3"},
                    {"type": "line", "p1": [-12, 12], "p2": [-12, -12], "id": "s4"},
                ],
                "constraints": [],
            },
            "offset": 50,
            "refs": {"plane": "plane.top"}}},
        # Step 3: loft (sections: f1 bottom, f2 top)
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "Blend1",
            "params": {"mode": "boss"},
            "refs": {
                "sections": [region("f1", 0, 0), region("f2", 0, 0)],
            }}}]},

    # loft_cut: sketch+extrude block (non-centered, top at Z=36) with a loft-cut pocket
    "loft_cut": {"yaw": 35, "pitch": 30, "steps": [
        # Step 1: base block via sketch+extrude (NOT box, which is centered).
        # Sketch on plane.top (Z=0), extrude +Z to Z=36.
        sketch([
            {"type": "line", "p1": [-26, -20], "p2": [26, -20], "id": "b1"},
            {"type": "line", "p1": [26, -20], "p2": [26, 20], "id": "b2"},
            {"type": "line", "p1": [26, 20], "p2": [-26, 20], "id": "b3"},
            {"type": "line", "p1": [-26, 20], "p2": [-26, -20], "id": "b4"},
        ], plane="plane.top", name="BlockSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: large circle at top face (Z=36)
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "LCTop",
            "params": {
                "entities": [
                    {"type": "circle", "center": [0, 0], "radius": 16, "id": "c1"},
                ],
                "constraints": [],
            },
            "offset": 36,
            "refs": {"plane": "plane.top"}}},
        # Step 3: small square at mid-height (Z=16)
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "LCMid",
            "params": {
                "entities": [
                    {"type": "line", "p1": [-5, -5], "p2": [5, -5], "id": "s1"},
                    {"type": "line", "p1": [5, -5], "p2": [5, 5], "id": "s2"},
                    {"type": "line", "p1": [5, 5], "p2": [-5, 5], "id": "s3"},
                    {"type": "line", "p1": [-5, 5], "p2": [-5, -5], "id": "s4"},
                ],
                "constraints": [],
            },
            "offset": 16,
            "refs": {"plane": "plane.top"}}},
        # Step 4: loft-cut from top (f3) down to mid (f4)
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "TaperPocket",
            "params": {"mode": "cut"},
            "refs": {
                "sections": [region("f3", 0, 0), region("f4", 0, 0)],
            }}}]},

    # -----------------------------------------------------------------------
    # Features -> Dress-up (first tranche already live -- do NOT redo)
    # -----------------------------------------------------------------------

    "fillet": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "fillet", "name": "Fillet1",
                     "params": {"radius": dim(7.0)}},
         "selrefs": {"edges": {"select": "vertical_edges", "n": 4}}}]},

    "chamfer": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "chamfer", "name": "Chamfer1",
                     "params": {"length": dim(6.0)}},
         "selrefs": {"edges": {"select": "vertical_edges", "n": 4}}}]},

    "shell": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "shell", "name": "Shell1",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}}]},

    # -----------------------------------------------------------------------
    # Features -> Pattern  (cheatsheet: seed MUST be sketch-based, not a bare box)
    # -----------------------------------------------------------------------

    # linear_pattern: sketch+extrude boss, then 1x3 linear pattern along X
    "linear_pattern": {"yaw": 35, "pitch": 26, "steps": [
        # Step 1: base plate
        sketch([
            {"type": "line", "p1": [-35, -15], "p2": [35, -15], "id": "b1"},
            {"type": "line", "p1": [35, -15], "p2": [35, 15], "id": "b2"},
            {"type": "line", "p1": [35, 15], "p2": [-35, 15], "id": "b3"},
            {"type": "line", "p1": [-35, 15], "p2": [-35, -15], "id": "b4"},
        ], plane="plane.top", name="PlateSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: seed boss (circle extrude at x=-22)
        sketch([
            {"type": "circle", "center": [-22, 0], "radius": 7, "id": "c1"},
        ], plane="plane.top", name="BossSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss",
            "params": {"mode": "boss", "depth": dim(12)},
            "refs": {"profile": [region("f3", -22, 0)]}}},
        # Step 3: linear pattern 3× along X, 22mm pitch
        {"op": "add_feature", "feature": {
            "type": "linear_pattern", "name": "LinPat",
            "params": {
                "count1": 3,
                "spacing1": dim(22),
            },
            "refs": {
                "dir1": datum("axis.x"),
                "features": ["f4"],
            }}}]},

    # circular_pattern: extrude boss, then 6× around Z
    "circular_pattern": {"yaw": 0, "pitch": 60, "steps": [
        # Step 1: base disc
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 22, "id": "base"},
        ], plane="plane.top", name="DiscSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Disc",
            "params": {"mode": "boss", "depth": dim(6)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: seed spoke (thin rectangle offset from center)
        sketch([
            {"type": "line", "p1": [6, -3], "p2": [20, -3], "id": "s1"},
            {"type": "line", "p1": [20, -3], "p2": [20, 3], "id": "s2"},
            {"type": "line", "p1": [20, 3], "p2": [6, 3], "id": "s3"},
            {"type": "line", "p1": [6, 3], "p2": [6, -3], "id": "s4"},
        ], plane="plane.top", name="SpokeSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Spoke",
            "params": {"mode": "boss", "depth": dim(10)},
            "refs": {"profile": [region("f3", 13, 0)]}}},
        # Step 3: circular pattern 6× around Z axis
        {"op": "add_feature", "feature": {
            "type": "circular_pattern", "name": "Wheel",
            "params": {
                "count": 6,
                "angle": dim(360),
                "equal_spacing": True,
            },
            "refs": {
                "axis": datum("axis.z"),
                "features": ["f4"],
            }}}]},

    # mirror: sketch+extrude boss on one side, mirror across right plane
    "mirror": {"yaw": 35, "pitch": 26, "steps": [
        # Step 1: asymmetric base
        sketch([
            {"type": "line", "p1": [-30, -14], "p2": [30, -14], "id": "b1"},
            {"type": "line", "p1": [30, -14], "p2": [30, 14], "id": "b2"},
            {"type": "line", "p1": [30, 14], "p2": [-30, 14], "id": "b3"},
            {"type": "line", "p1": [-30, 14], "p2": [-30, -14], "id": "b4"},
        ], plane="plane.top", name="BaseSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 2: L-bracket seed on right side
        sketch([
            {"type": "line", "p1": [4, -10], "p2": [26, -10], "id": "l1"},
            {"type": "line", "p1": [26, -10], "p2": [26, 10], "id": "l2"},
            {"type": "line", "p1": [26, 10], "p2": [14, 10], "id": "l3"},
            {"type": "line", "p1": [14, 10], "p2": [14, 0], "id": "l4"},
            {"type": "line", "p1": [14, 0], "p2": [4, 0], "id": "l5"},
            {"type": "line", "p1": [4, 0], "p2": [4, -10], "id": "l6"},
        ], plane="plane.top", name="LBSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LBoss",
            "params": {"mode": "boss", "depth": dim(16)},
            "refs": {"profile": [region("f3", 15, -5)]}}},
        # Step 3: mirror across front plane (YZ) -- mirrors x+ boss to x- side
        {"op": "add_feature", "feature": {
            "type": "mirror", "name": "Mir1",
            "params": {},
            "refs": {
                "plane": datum("plane.front"),
                "features": ["f4"],
            }}}]},

    # -----------------------------------------------------------------------
    # Features -> Multi-body  (cheatsheet: boolean needs TWO bodies; second body
    # must be OFFSET so there's a meaningful overlap zone, not total containment)
    # -----------------------------------------------------------------------

    # combine (union): two overlapping extrude bosses merged into one body
    "combine": {"yaw": 35, "pitch": 26, "steps": [
        # Body 1: base block
        sketch([
            {"type": "line", "p1": [-20, -14], "p2": [20, -14], "id": "b1"},
            {"type": "line", "p1": [20, -14], "p2": [20, 14], "id": "b2"},
            {"type": "line", "p1": [20, 14], "p2": [-20, 14], "id": "b3"},
            {"type": "line", "p1": [-20, 14], "p2": [-20, -14], "id": "b4"},
        ], plane="plane.top", name="Body1Sk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body1",
            "params": {"mode": "boss", "depth": dim(24)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Body 2: offset cylinder (partially overlapping)
        sketch([
            {"type": "circle", "center": [16, 0], "radius": 14, "id": "c1"},
        ], plane="plane.top", name="Body2Sk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body2",
            "params": {"mode": "boss", "depth": dim(24)},
            "merge": False,
            "refs": {"profile": [region("f3", 16, 0)]}}},
        # boolean combine (union): targets=Body1, tools=Body2
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Union1",
            "params": {"op": "union"},
            "refs": {
                "targets": [body_sel("f2")],
                "tools": [body_sel("f4")],
            }}}]},

    # subtract: block with a cylinder tool subtracted from it
    "subtract": {"yaw": 35, "pitch": 26, "steps": [
        # Body 1: target block
        sketch([
            {"type": "line", "p1": [-22, -16], "p2": [22, -16], "id": "b1"},
            {"type": "line", "p1": [22, -16], "p2": [22, 16], "id": "b2"},
            {"type": "line", "p1": [22, 16], "p2": [-22, 16], "id": "b3"},
            {"type": "line", "p1": [-22, 16], "p2": [-22, -16], "id": "b4"},
        ], plane="plane.top", name="TgtSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Target",
            "params": {"mode": "boss", "depth": dim(28)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Body 2: cylinder tool (centered, slightly taller to visibly pierce)
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "c1"},
        ], plane="plane.top", name="ToolSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tool",
            "params": {"mode": "boss", "depth": dim(34)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # boolean subtract: cut cylinder from block
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Sub1",
            "params": {"op": "subtract"},
            "refs": {
                "targets": [body_sel("f2")],
                "tools": [body_sel("f4")],
            }}}]},

    # intersect: block ∩ cylinder = rounded rectangular solid
    "intersect": {"yaw": 35, "pitch": 26, "steps": [
        # Body 1: block
        sketch([
            {"type": "line", "p1": [-20, -20], "p2": [20, -20], "id": "b1"},
            {"type": "line", "p1": [20, -20], "p2": [20, 20], "id": "b2"},
            {"type": "line", "p1": [20, 20], "p2": [-20, 20], "id": "b3"},
            {"type": "line", "p1": [-20, 20], "p2": [-20, -20], "id": "b4"},
        ], plane="plane.top", name="BlkSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(30)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Body 2: cylinder (same height, slightly smaller radius -- clear intersection)
        sketch([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="CylSk"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl",
            "params": {"mode": "boss", "depth": dim(30)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # boolean intersect
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Isect1",
            "params": {"op": "intersect"},
            "refs": {
                "targets": [body_sel("f2")],
                "tools": [body_sel("f4")],
            }}}]},

    # -----------------------------------------------------------------------
    # Features -> Curves  (curve_helix: yields a path, not a solid by itself;
    # must sweep a profile along it to show a visible coil in the icon)
    # -----------------------------------------------------------------------

    # curve_helix: spring coil (helix path + sweep of a circle profile)
    "curve_helix": {"yaw": 35, "pitch": 30, "steps": [
        # Step 1: wire profile circle (r=1.5mm, centered at (coil_radius=10, 0) on plane.front)
        sketch([
            {"type": "circle", "center": [10, 0], "radius": 1.5, "id": "c1"},
        ], plane="plane.front", name="WireProf"),
        # Step 2: helix (base=plane.top, radius=10mm, pitch=5mm, 4 turns → height=20mm)
        {"op": "add_feature", "feature": {
            "type": "helix", "name": "CoilPath",
            "params": {
                "pitch": dim(5),
                "height": dim(20),
                "radius": dim(10),
            },
            "refs": {"base": "plane.top"}}},
        # Step 3: sweep the wire along the helix
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Spring",
            "params": {"mode": "boss"},
            "refs": {
                "profile": [region("f1", 10, 0)],
                "path": "f2",
            }}}]},

    # -----------------------------------------------------------------------
    # Sheet Metal -> Create  (sm_base_flange and sm_flat_pattern only;
    # sm_edge_flange/sm_hem/sm_jog PARKED -- need resolved edge selector
    # that the iconpipe runner doesn't support yet -- see work/questions/T-BL29.md)
    # -----------------------------------------------------------------------

    # sm_base_flange: L-shaped sheet-metal blank (80×50mm, t=2mm)
    "sm_base_flange": {"yaw": 35, "pitch": 30, "steps": [
        # Step 1: rectangular profile sketch
        sketch([
            {"type": "line", "p1": [-25, -20], "p2": [25, -20], "id": "b1"},
            {"type": "line", "p1": [25, -20], "p2": [25, 20], "id": "b2"},
            {"type": "line", "p1": [25, 20], "p2": [-25, 20], "id": "b3"},
            {"type": "line", "p1": [-25, 20], "p2": [-25, -20], "id": "b4"},
        ], plane="plane.top", name="FlangeSk"),
        # Step 2: sm_base_flange (thickness=2mm)
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}}]},

    # sm_flat_pattern: same base flange then flatten -- shows developed blank
    "sm_flat_pattern": {"yaw": 0, "pitch": 90, "steps": [
        # Step 1: profile sketch
        sketch([
            {"type": "line", "p1": [-25, -20], "p2": [25, -20], "id": "b1"},
            {"type": "line", "p1": [25, -20], "p2": [25, 20], "id": "b2"},
            {"type": "line", "p1": [25, 20], "p2": [-25, 20], "id": "b3"},
            {"type": "line", "p1": [-25, 20], "p2": [-25, -20], "id": "b4"},
        ], plane="plane.top", name="FlangeSk"),
        # Step 2: base flange
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Step 3: flatten to developed blank
        {"op": "add_feature", "feature": {
            "type": "sm_flat_pattern", "name": "Flat",
            "params": {}}}]},

    # -----------------------------------------------------------------------
    # Appearance -> Material (first tranche already live -- do NOT redo)
    # appr_glass / appr_paint: these atlas cells exist but are NOT wired to any
    # ribbon button (ribbon_def.cpp has no entry for them). Generating icons for
    # them is vestigial work; documented and skipped. See work/questions/T-BL29.md.
    # -----------------------------------------------------------------------

    "appr_metal":   {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26), appearance(0.90, 0.35, (0.83, 0.85, 0.88))]},   # Aluminum
    "appr_plastic": {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26), appearance(0.00, 0.50, (0.71, 0.76, 0.82))]},   # Plastic
    "appr_default": {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26)]},                                               # engine default grey

    # -----------------------------------------------------------------------
    # Appearance -> Display: display_shaded / display_shaded_edges / display_hlr /
    # display_hlv / display_wireframe are line-art SDF glyphs. They are NOT
    # photographic icons. The SDF invisible-icon bug (kFar int32 overflow) was
    # fixed in T-G15 (kFar=1<<12). In-context verification via AI_CAD_RIBBON_TAB=Appearance
    # is the correct check -- adding them here as photographic renders would override
    # the correct line-art with a boring box screenshot. Skipped -- see task notes.
    # -----------------------------------------------------------------------
}


def write_recipes(path: Path) -> None:
    recipes = [{"name": n, "yaw": r["yaw"], "pitch": r["pitch"], "steps": r["steps"]}
               for n, r in RECIPES.items()]
    path.write_text(json.dumps({"out_dir": str(RENDER_DIR), "px": RENDER_PX,
                                "recipes": recipes}, indent=1), encoding="utf-8")


def run_app(recipes_json: Path, theme: str = "dark", timeout_s: int = 300) -> int:
    """Run the GUI headless (AI_CAD_SHOT=iconpipe) to render every recipe to RENDER_DIR.

    Runs under xvfb-run (llvmpipe) so the pipeline is deterministic and independent of the
    desktop/GPU state; the whole multi-recipe run completes in a few seconds because the
    scenario drains every recipe in one tick. NOTE: the HOST binary's headless flag is
    --ui-screenshot (NOT --shot; that is AI-CAD2's flag -- passing --shot silently starts
    the interactive GUI, which never exits headless). One invocation renders ALL recipes.
    """
    if not BIN.exists():
        sys.exit(f"binary not found: {BIN} -- build host/build-linux first")
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("DISPLAY", None)  # force xvfb-run's own server; the live :0 may be unattended
    env.update({
        "AI_CAD_NO_FILE_DIALOGS": "1",
        "AI_CAD_REPO": str(REPO),
        "AI_CAD_SHOT": "iconpipe",
        "AI_CAD_ICON_RECIPES": str(recipes_json),
        "AI_CAD_THEME": theme,
    })
    throwaway = REPO / "scratch" / "iconpipe_final.png"
    print(f"[icon_pipeline] rendering {len(RECIPES)} recipes via {BIN.name} (theme={theme}) ...")
    proc = subprocess.run(["xvfb-run", "-a", str(BIN), "--ui-screenshot", str(throwaway)],
                          env=env, capture_output=True, text=True, timeout=timeout_s)
    for line in proc.stdout.splitlines():
        if "iconpipe" in line:
            print("   " + line)
    return proc.returncode


def _fit(img: Image.Image, w: int, h: int, inset: int) -> Image.Image:
    """Aspect-preserving fit of a transparent render into a w*h cell with an inset margin,
    centred on transparency. No corner plate (contract: icons are plate-free / transparent)."""
    img = img.convert("RGBA")
    iw, ih = max(1, w - 2 * inset), max(1, h - 2 * inset)
    sw, sh = img.size
    scale = min(iw / sw, ih / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    cell = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cell.alpha_composite(img.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
    return cell


def inject(names: list[str]) -> list[str]:
    """Composite each rendered tile into the @32/@20 cells of both atlases (in place).
    Only the named cells are overwritten; every other icon is preserved byte-for-byte."""
    atlas = json.loads((ICON_DIR / "atlas.json").read_text(encoding="utf-8"))
    cells = atlas["cell"]
    done: list[str] = []
    atlases = {fn: Image.open(ICON_DIR / fn).convert("RGBA")
               for fn in ("atlas_dark.png", "atlas_light.png")}
    for name in names:
        src = RENDER_DIR / f"{name}.png"
        if not src.exists():
            print(f"   SKIP {name}: no render at {src}")
            continue
        render = Image.open(src).convert("RGBA")
        for key in (f"{name}@32", f"{name}@20"):
            if key not in cells:
                continue
            c = cells[key]
            inset = 2 if c["w"] >= 32 else 1
            tile = _fit(render, c["w"], c["h"], inset)
            for im in atlases.values():
                # clear the old glyph in this cell to transparency, then drop in the render
                im.paste((0, 0, 0, 0), (c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]))
                im.alpha_composite(tile, (c["x"], c["y"]))
        done.append(name)
    for fn, im in atlases.items():
        im.save(ICON_DIR / fn)
    # Publish the photographic names as atlas.json "raster_only": the C++ loader must NEVER derive
    # SDF glyphs from these (their neutral-grey shading can pass the loader's chroma gate, but an
    # SDF re-raster would flatten them to a tinted silhouette). See icons_sdf.h.
    atlas["raster_only"] = sorted(set(atlas.get("raster_only", [])) | set(done))
    (ICON_DIR / "atlas.json").write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    # Deploy to the built app so a verification run picks the new art up immediately.
    if DEPLOYED_ICON_DIR.exists():
        for fn in list(atlases) + ["atlas.json"]:
            shutil.copy2(ICON_DIR / fn, DEPLOYED_ICON_DIR / fn)
    print(f"[icon_pipeline] injected {len(done)} icon(s): {', '.join(done)}")
    return done


def contact_sheet(names: list[str]) -> None:
    """One image of every generated tile (on a checkerboard so transparency is visible)."""
    cols = 4
    cell, pad, label = 96, 12, 16
    rows = (len(names) + cols - 1) // cols
    W, H = cols * (cell + pad) + pad, rows * (cell + label + pad) + pad
    sheet = Image.new("RGBA", (W, H), (44, 46, 52, 255))
    # checkerboard swatch to reveal transparency
    chk = Image.new("RGBA", (cell, cell), (200, 200, 200, 255))
    for y in range(0, cell, 8):
        for x in range(0, cell, 8):
            if (x // 8 + y // 8) % 2:
                for yy in range(y, min(y + 8, cell)):
                    for xx in range(x, min(x + 8, cell)):
                        chk.putpixel((xx, yy), (150, 150, 150, 255))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(sheet)
    for i, name in enumerate(names):
        cx = pad + (i % cols) * (cell + pad)
        cy = pad + (i // cols) * (cell + label + pad)
        sheet.alpha_composite(chk, (cx, cy))
        src = RENDER_DIR / f"{name}.png"
        if src.exists():
            r = Image.open(src).convert("RGBA").resize((cell, cell), Image.LANCZOS)
            sheet.alpha_composite(r, (cx, cy))
        draw.text((cx + 2, cy + cell + 2), name, fill=(230, 230, 230, 255))
    sheet.convert("RGB").save(CONTACT_SHEET)
    print(f"[icon_pipeline] contact sheet -> {CONTACT_SHEET}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true", help="run the app, regenerate, inject")
    ap.add_argument("--inject-only", action="store_true", help="re-tile from existing renders")
    ap.add_argument("--list", action="store_true", help="print the recipe table and exit")
    ap.add_argument("--theme", default="dark", help="AI_CAD_THEME for the render run")
    args = ap.parse_args(argv)

    if args.list:
        for n, r in RECIPES.items():
            print(f"{n:16s} yaw={r['yaw']:>3} pitch={r['pitch']:>3} steps={len(r['steps'])}")
        return 0

    names = list(RECIPES.keys())
    if args.render:
        recipes_json = REPO / "scratch" / "icon_recipes.json"
        recipes_json.parent.mkdir(parents=True, exist_ok=True)
        write_recipes(recipes_json)
        rc = run_app(recipes_json, theme=args.theme)
        print(f"[icon_pipeline] app exit={rc}")

    if args.render or args.inject_only:
        done = inject(names)
        contact_sheet(names)
        if not done:
            sys.exit("no icons injected -- check the render step")
    elif not args.list:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

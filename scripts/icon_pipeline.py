#!/usr/bin/env python3
"""Screenshot-based ribbon icon pipeline.

Regenerable, one-command pipeline that turns a data-driven recipe table into ribbon
icon tiles that are *actual clean-viewport screenshots of demo parts*, then injects them
into the icon atlases the C++ loader consumes.

Flow (see docs in each function):
  1. RECIPES  -> a JSON recipe list (name, camera, build steps).
  2. The Chudware GUI, run headless with CHUDWARE_SHOT=iconpipe + CHUDWARE_ICON_RECIPES,
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

Shared visual contract: icons carry NO baked background plate --
they render on transparency so the one asset works on the ribbon glass in both light and
dark mode. Modified-face emphasis (selection-highlight colour) is a follow-up layer (needs
a highlight overlay in the thumbnail FBO keyed by selection topo-ids).

v2 adds three layers on top of the screenshot flow --
  * operation-COLOR emphasis: recipes color the op-created body with the app accent
    teal (ui_theme accent #2DD4BF) via set_appearance, so the tile reads "this is what
    the op made" (per-face emphasis is still a renderer follow-up; per-body v1).
  * operation-INTENT annotation: a minimal overlay (arrow / rotation cue / mirror line /
    boolean badge) composited onto the tile in Python post-render (ANNOTATIONS table).
  * Manufacture toolpath tiles: scripts/cam_toolpath_icons.py drives the REAL CAM chain
    (cam_new -> add_tool/setup/operation -> cam_generate -> cam_simulate) and renders the
    generated cutter moves over the stock (design decision 2026-07-04: showcase the
    generated toolpaths). Those cells (cam_*) are appended to the atlas byte-preservingly
    by ensure_cells(); ribbon_def.cpp points the Manufacture op buttons at them.

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


def _find_host_bin() -> Path:
    """Locate the host GUI binary (chudware lowercase is the CMake target name;
    Chudware capitalized kept for stale build dirs / Windows layouts)."""
    base = REPO / "host" / "build-linux" / "bin"
    for c in (base / "chudware", base / "Release" / "chudware",
              base / "Release" / "Chudware.exe", base / "Chudware.exe",
              base / "Release" / "Chudware", base / "Chudware"):
        if c.exists():
            return c
    return base / "chudware"  # default (nonexistent -> run_app prints a clear build hint)


BIN = _find_host_bin()
# Canonical atlas the C++ loader injects into (shared assets/icons staging source).
ICON_DIR = REPO / "assets" / "icons"
# Runtime copy the host binary reads; redeploy here after inject so a re-run picks up
# new tiles without a full rebuild.
DEPLOYED_ICON_DIR = BIN.parent / "assets" / "icons"
RENDER_DIR = REPO / "scratch" / "icon_render"
CONTACT_SHEET = REPO / "scratch" / "icon_contact_sheet.png"
RENDER_PX = 128  # square offscreen render size; downscaled into the 32/20 cells


# Recipe-builder vocabulary lives in scripts/icon_recipes/helpers.py (single source);
# re-exported here so the historical RECIPES table below and cam_toolpath_icons keep working.
from icon_recipes.helpers import *  # noqa: F401,F403
from icon_recipes.helpers import ACCENT_RGB, accent, appearance, body_sel, box, datum, dim, region, sketch_with_id, sphere


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


RECIPES: dict[str, dict] = {
    # -----------------------------------------------------------------------
    # Features -> 3D Body
    # -----------------------------------------------------------------------

    # Plain solid block reads as "create a 3D body / boss".
    "extrude_boss": {"yaw": 35, "pitch": 26, "steps": [box(52, 40, 34), accent()]},

    # ws_model: the title-bar "Model" workspace exemplar tile. A recognizable little
    # mechanical part (NOT a bare box): an L-bracket plate with two through-holes bored
    # through the base flange. f1=L-profile sketch on plane.top, f2=extrude, f3/f4=hole
    # circle sketches on the top face, f5/f6=extrude-cuts. Region selectors only (the
    # iconpipe runner resolves regions but not face+point hole picks). accent() last so
    # the whole body reads in app teal. Iso camera to match the 3D-body family.
    "ws_model": {"yaw": 35, "pitch": 26, "steps": [
        # L-shaped profile (base flange 44 wide, upstand 16 tall wall on the +Y edge).
        *sketch_with_id([
            {"type": "line", "p1": [-22, -16], "p2": [22, -16], "id": "l1"},
            {"type": "line", "p1": [22, -16], "p2": [22, -4], "id": "l2"},
            {"type": "line", "p1": [22, -4], "p2": [-10, -4], "id": "l3"},
            {"type": "line", "p1": [-10, -4], "p2": [-10, 16], "id": "l4"},
            {"type": "line", "p1": [-10, 16], "p2": [-22, 16], "id": "l5"},
            {"type": "line", "p1": [-22, 16], "p2": [-22, -16], "id": "l6"},
        ], plane="plane.top", name="BracketSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bracket",
            "params": {"mode": "boss", "depth": dim(10)},
            "refs": {"profile": [region("f1", 0, -10)]}}},
        # Two mounting holes bored through the wide base flange (on the top face, Z=10).
        *sketch_with_id([
            {"type": "circle", "center": [10, -10], "radius": 4.5, "id": "h1"},
        ], plane="plane.top", name="Hole1Sk", fid="f3", offset=10),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hole1",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f3", 10, -10)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-16, -10], "radius": 4.5, "id": "h2"},
        ], plane="plane.top", name="Hole2Sk", fid="f5", offset=10),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hole2",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f5", -16, -10)]}}},
        accent(),
    ]},

    # ws_drawing: the title-bar "Drawing" workspace exemplar tile. The iconpipe render
    # path is SHADED-only (renderThumbnail has no display-style arg — see scenarios.cpp
    # runIconPipe), so we CANNOT ask the engine for a true HLR/wireframe view here. To
    # keep this tile from reading as a second ws_model iso bracket, we render the same
    # class of part but with a FLAT, face-on ORTHOGRAPHIC camera (yaw~0/pitch~0) — a clean
    # front elevation, which is exactly how a part appears on a drawing sheet. A slotted
    # gusset plate (rectangular plate + a central slot + two bolt holes) gives a
    # recognizable, drawing-like silhouette when viewed straight-on. Steel-grey appearance
    # (not app teal) so it reads as a neutral "drawing", distinct from the teal 3D tiles.
    "ws_drawing": {"yaw": 0, "pitch": 0, "steps": [
        # Plate profile on plane.front (XZ) so face-on camera sees the elevation.
        *sketch_with_id([
            {"type": "line", "p1": [-24, -16], "p2": [24, -16], "id": "pl1"},
            {"type": "line", "p1": [24, -16], "p2": [24, 16], "id": "pl2"},
            {"type": "line", "p1": [24, 16], "p2": [-24, 16], "id": "pl3"},
            {"type": "line", "p1": [-24, 16], "p2": [-24, -16], "id": "pl4"},
        ], plane="plane.front", name="PlateSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Central rounded slot (two arcs would need pick topo; a plain rectangle slot reads
        # fine face-on) cut through the plate.
        *sketch_with_id([
            {"type": "line", "p1": [-9, -4], "p2": [9, -4], "id": "s1"},
            {"type": "line", "p1": [9, -4], "p2": [9, 4], "id": "s2"},
            {"type": "line", "p1": [9, 4], "p2": [-9, 4], "id": "s3"},
            {"type": "line", "p1": [-9, 4], "p2": [-9, -4], "id": "s4"},
        ], plane="plane.front", name="SlotSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Slot",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # Two bolt holes flanking the slot.
        *sketch_with_id([
            {"type": "circle", "center": [-17, 0], "radius": 3, "id": "bh1"},
        ], plane="plane.front", name="Bolt1Sk", fid="f5"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bolt1",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f5", -17, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [17, 0], "radius": 3, "id": "bh2"},
        ], plane="plane.front", name="Bolt2Sk", fid="f7"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bolt2",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f7", 17, 0)]}}},
        # Neutral steel-grey (NOT teal) so it reads as a drawing sheet, not a 3D body.
        {"op": "set_appearance", "id": "b1", "color": [0.62, 0.66, 0.70],
         "metallic": 0.1, "roughness": 0.6},
    ]},

    # ws_electronics: the title-bar "Electronics" workspace exemplar tile. A small PCB:
    # a thin GREEN board (b1) with two dark component boxes (b2, b3) and two copper-colored
    # pads (b4, b5). Body ids are assigned in first-produced order (document.cpp
    # apply_doc_appearance: appearances["bN"] -> Nth surviving body), so each separate
    # extrude gets its own body we can color independently. Iso camera so the board's
    # thickness + components read as 3D. Must read as "a circuit board".
    "ws_electronics": {"yaw": 32, "pitch": 34, "steps": [
        # Board: thin wide rectangle on plane.top, small extrude for PCB thickness.
        *sketch_with_id([
            {"type": "line", "p1": [-26, -18], "p2": [26, -18], "id": "bd1"},
            {"type": "line", "p1": [26, -18], "p2": [26, 18], "id": "bd2"},
            {"type": "line", "p1": [26, 18], "p2": [-26, 18], "id": "bd3"},
            {"type": "line", "p1": [-26, 18], "p2": [-26, -18], "id": "bd4"},
        ], plane="plane.top", name="BoardSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Board",
            "params": {"mode": "boss", "depth": dim(3)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Chip 1: dark box sitting on the board (Z=3 top face), big central IC.
        *sketch_with_id([
            {"type": "line", "p1": [-10, -8], "p2": [10, -8], "id": "c1a"},
            {"type": "line", "p1": [10, -8], "p2": [10, 8], "id": "c1b"},
            {"type": "line", "p1": [10, 8], "p2": [-10, 8], "id": "c1c"},
            {"type": "line", "p1": [-10, 8], "p2": [-10, -8], "id": "c1d"},
        ], plane="plane.top", name="Chip1Sk", fid="f3", offset=3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Chip1",
            "params": {"mode": "boss", "depth": dim(4)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # Chip 2: smaller component to the side.
        *sketch_with_id([
            {"type": "line", "p1": [15, -14], "p2": [22, -14], "id": "c2a"},
            {"type": "line", "p1": [22, -14], "p2": [22, -6], "id": "c2b"},
            {"type": "line", "p1": [22, -6], "p2": [15, -6], "id": "c2c"},
            {"type": "line", "p1": [15, -6], "p2": [15, -14], "id": "c2d"},
        ], plane="plane.top", name="Chip2Sk", fid="f5", offset=3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Chip2",
            "params": {"mode": "boss", "depth": dim(3)}, "merge": False,
            "refs": {"profile": [region("f5", 18, -10)]}}},
        # Pad 1: small copper pad.
        *sketch_with_id([
            {"type": "circle", "center": [-19, 12], "radius": 2.6, "id": "pd1"},
        ], plane="plane.top", name="Pad1Sk", fid="f7", offset=3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pad1",
            "params": {"mode": "boss", "depth": dim(1.2)}, "merge": False,
            "refs": {"profile": [region("f7", -19, 12)]}}},
        # Pad 2: second copper pad.
        *sketch_with_id([
            {"type": "circle", "center": [18, 13], "radius": 2.6, "id": "pd2"},
        ], plane="plane.top", name="Pad2Sk", fid="f9", offset=3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pad2",
            "params": {"mode": "boss", "depth": dim(1.2)}, "merge": False,
            "refs": {"profile": [region("f9", 18, 13)]}}},
        # Color each body by its 1..N production order.
        {"op": "set_appearance", "id": "b1", "color": [0.11, 0.45, 0.22],
         "metallic": 0.05, "roughness": 0.55},  # green PCB
        {"op": "set_appearance", "id": "b2", "color": [0.10, 0.10, 0.12],
         "metallic": 0.2, "roughness": 0.5},    # black IC
        {"op": "set_appearance", "id": "b3", "color": [0.10, 0.10, 0.12],
         "metallic": 0.2, "roughness": 0.5},    # black component
        {"op": "set_appearance", "id": "b4", "color": [0.80, 0.52, 0.24],
         "metallic": 0.85, "roughness": 0.35},  # copper pad
        {"op": "set_appearance", "id": "b5", "color": [0.80, 0.52, 0.24],
         "metallic": 0.85, "roughness": 0.35},  # copper pad
    ]},

    # ws_parts: the title-bar "Parts Library" workspace exemplar tile. A standard hex-head
    # bolt — a fastener reads instantly as "parts / library hardware". Hex head (6-sided
    # polygon prism) on top of a cylindrical shank, with two shallow thread grooves near
    # the tip. Metallic steel appearance. Iso camera to match the 3D family. f1=hex sketch,
    # f2=head extrude; f3=shank circle, f4=shank extrude (downward); grooves are thin
    # revolve-cuts around the shank axis.
    "ws_parts": {"yaw": 35, "pitch": 26, "steps": [
        # Hex head: regular hexagon on plane.top, extruded up for the head.
        *sketch_with_id([
            {"type": "line", "p1": [14, 0], "p2": [7, 12.12], "id": "hx1"},
            {"type": "line", "p1": [7, 12.12], "p2": [-7, 12.12], "id": "hx2"},
            {"type": "line", "p1": [-7, 12.12], "p2": [-14, 0], "id": "hx3"},
            {"type": "line", "p1": [-14, 0], "p2": [-7, -12.12], "id": "hx4"},
            {"type": "line", "p1": [-7, -12.12], "p2": [7, -12.12], "id": "hx5"},
            {"type": "line", "p1": [7, -12.12], "p2": [14, 0], "id": "hx6"},
        ], plane="plane.top", name="HexSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Head",
            "params": {"mode": "boss", "depth": dim(9)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Shank: circle on the head's TOP face (Z=9), extruded UP +30 (all-positive; a
        # negative depth silently no-ops). Fuses to the head as one bolt body (b1). The
        # head-below/shank-above orientation reads fine as a bolt in an iso icon.
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 6.5, "id": "sk1"},
        ], plane="plane.top", name="ShankSk", fid="f3", offset=9),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Shank",
            "params": {"mode": "boss", "depth": dim(30)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # Two thread grooves: thin rectangles on plane.front revolved around Z to cut a
        # shallow ring near the shank free end (now high Z, sketch-y ~30-36). Unique axis
        # ids per sketch (ids are doc-scoped).
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [0, 42], "id": "axis1", "construction": True},
            {"type": "line", "p1": [5.5, 30], "p2": [6.6, 30], "id": "g1a"},
            {"type": "line", "p1": [6.6, 30], "p2": [6.6, 32], "id": "g1b"},
            {"type": "line", "p1": [6.6, 32], "p2": [5.5, 32], "id": "g1c"},
            {"type": "line", "p1": [5.5, 32], "p2": [5.5, 30], "id": "g1d"},
        ], plane="plane.front", name="Groove1Sk", fid="f5"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {
                "profile": [region("f5", 6.0, 31)],
                "axis": datum("axis.z"),
            }}},
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [0, 42], "id": "axis2", "construction": True},
            {"type": "line", "p1": [5.5, 35], "p2": [6.6, 35], "id": "g2a"},
            {"type": "line", "p1": [6.6, 35], "p2": [6.6, 37], "id": "g2b"},
            {"type": "line", "p1": [6.6, 37], "p2": [5.5, 37], "id": "g2c"},
            {"type": "line", "p1": [5.5, 37], "p2": [5.5, 35], "id": "g2d"},
        ], plane="plane.front", name="Groove2Sk", fid="f7"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove2",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {
                "profile": [region("f7", 6.0, 36)],
                "axis": datum("axis.z"),
            }}},
        # Steel appearance (metallic grey) so it reads as hardware, not app teal.
        {"op": "set_appearance", "id": "b1", "color": [0.70, 0.72, 0.75],
         "metallic": 0.9, "roughness": 0.35},
    ]},

    # extrude_cut: solid block with a deep rectangular pocket cut into its TOP face,
    # so material is obviously REMOVED (reads as an extruded cut). f1=block sketch,
    # f2=block extrude, f3=pocket sketch on top face, f4=extrude-cut.
    "extrude_cut": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-26, -20], "p2": [26, -20], "id": "b1"},
            {"type": "line", "p1": [26, -20], "p2": [26, 20], "id": "b2"},
            {"type": "line", "p1": [26, 20], "p2": [-26, 20], "id": "b3"},
            {"type": "line", "p1": [-26, 20], "p2": [-26, -20], "id": "b4"},
        ], plane="plane.top", name="CutBlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "CutBlock",
            "params": {"mode": "boss", "depth": dim(30)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Pocket profile on the top face (Z=30): a rectangle inset from the walls.
        *sketch_with_id([
            {"type": "line", "p1": [-15, -11], "p2": [15, -11], "id": "p1"},
            {"type": "line", "p1": [15, -11], "p2": [15, 11], "id": "p2"},
            {"type": "line", "p1": [15, 11], "p2": [-15, 11], "id": "p3"},
            {"type": "line", "p1": [-15, 11], "p2": [-15, -11], "id": "p4"},
        ], plane="plane.top", name="PocketSk", fid="f3", offset=30),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pocket",
            "params": {"mode": "cut", "depth": dim(22)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
    ]},

    # revolve_boss: stepped-rectangle profile on plane.front (XZ), revolve around Z axis.
    # plane.front = XZ; sketch-x = world-X, sketch-y = world-Z.
    # Profile at sketch-x=[5,20] (world X), sketch-y=[-18,18] (world Z).
    # Revolve axis = Z (world Z, which lies IN the XZ plane at X=0).
    # Result: hollow cylinder (inner r=5, outer r=20, height=36mm), like a cup/boss.
    # f1=sketch, f2=revolve.
    "revolve_boss": {"yaw": 35, "pitch": 20, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [5, -18], "p2": [20, -18], "id": "e1"},
            {"type": "line", "p1": [20, -18], "p2": [20, 18], "id": "e2"},
            {"type": "line", "p1": [20, 18], "p2": [5, 18], "id": "e3"},
            {"type": "line", "p1": [5, 18], "p2": [5, -18], "id": "e4"},
        ], plane="plane.front", name="RevolveSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Revolve1",
            "params": {"mode": "boss", "angle": dim(360)},
            "refs": {
                "profile": [region("f1", 12, 0)],
                "axis": datum("axis.z"),
            }}},
        accent(),
    ]},

    # revolve_cut: circle extrude cylinder → revolve-cut a groove.
    # f1=circle sketch, f2=cylinder extrude, f3=groove sketch, f4=revolve-cut.
    "revolve_cut": {"yaw": 35, "pitch": 20, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 20, "id": "c1"},
        ], plane="plane.top", name="CylSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl1",
            "params": {"mode": "boss", "depth": dim(40)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Groove: thin rect at world-X=[12,20], world-Z=[17,23] on plane.front
        *sketch_with_id([
            {"type": "line", "p1": [0, -25], "p2": [0, 25], "id": "axis", "construction": True},
            {"type": "line", "p1": [12, 17], "p2": [20, 17], "id": "g1"},
            {"type": "line", "p1": [20, 17], "p2": [20, 23], "id": "g2"},
            {"type": "line", "p1": [20, 23], "p2": [12, 23], "id": "g3"},
            {"type": "line", "p1": [12, 23], "p2": [12, 17], "id": "g4"},
        ], plane="plane.front", name="GrooveSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {
                "profile": [region("f3", 16, 20)],
                "axis": datum("axis.z"),
            }}},
    ]},

    # sweep: circle profile (f1) on plane.right, L-path (f2) on plane.top → pipe.
    "sweep": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 5, "id": "c1"},
        ], plane="plane.right", name="SwProf", fid="f1"),
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [30, 0], "id": "p1"},
            {"type": "line", "p1": [30, 0], "p2": [30, 25], "id": "p2"},
        ], plane="plane.top", name="SwPath", fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Pipe1",
            "params": {"mode": "boss"},
            "refs": {
                "profile": [region("f1", 0, 0)],
                "path": region("f2", 15, 0),
            }}},
        accent(),
    ]},

    # sweep_cut: base block (f1 sketch, f2 extrude), cut profile (f3 sketch),
    # path (f4 sketch), sweep-cut (f5).
    "sweep_cut": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-25, -15], "p2": [25, -15], "id": "b1"},
            {"type": "line", "p1": [25, -15], "p2": [25, 15], "id": "b2"},
            {"type": "line", "p1": [25, 15], "p2": [-25, 15], "id": "b3"},
            {"type": "line", "p1": [-25, 15], "p2": [-25, -15], "id": "b4"},
        ], plane="plane.top", name="BaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base1",
            "params": {"mode": "boss", "depth": dim(20)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [-4, 8], "p2": [4, 8], "id": "c1"},
            {"type": "line", "p1": [4, 8], "p2": [4, 16], "id": "c2"},
            {"type": "line", "p1": [4, 16], "p2": [-4, 16], "id": "c3"},
            {"type": "line", "p1": [-4, 16], "p2": [-4, 8], "id": "c4"},
        ], plane="plane.right", name="CutProf", fid="f3"),
        *sketch_with_id([
            {"type": "line", "p1": [-20, 0], "p2": [20, 0], "id": "p1"},
        ], plane="plane.top", name="CutPath", fid="f4"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "SlotCut",
            "params": {"mode": "cut"},
            "refs": {
                "profile": [region("f3", 0, 12)],
                "path": region("f4", 0, 0),
            }}},
    ]},

    # loft: circle (f1, Z=0) → square (f2, Z=50) → loft boss (f3).
    "loft": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="LoftBot", fid="f1"),
        *sketch_with_id([
            {"type": "line", "p1": [-12, -12], "p2": [12, -12], "id": "s1"},
            {"type": "line", "p1": [12, -12], "p2": [12, 12], "id": "s2"},
            {"type": "line", "p1": [12, 12], "p2": [-12, 12], "id": "s3"},
            {"type": "line", "p1": [-12, 12], "p2": [-12, -12], "id": "s4"},
        ], plane="plane.top", name="LoftTop", fid="f2", offset=50),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "Blend1",
            "params": {"mode": "boss"},
            "refs": {"sections": [region("f1", 0, 0), region("f2", 0, 0)]}}},
        accent(),
    ]},

    # loft_cut: block (f1 sketch, f2 extrude) with loft-cut pocket
    # from large circle (f3, Z=36) to small square (f4, Z=16) → loft-cut (f5).
    "loft_cut": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-26, -20], "p2": [26, -20], "id": "b1"},
            {"type": "line", "p1": [26, -20], "p2": [26, 20], "id": "b2"},
            {"type": "line", "p1": [26, 20], "p2": [-26, 20], "id": "b3"},
            {"type": "line", "p1": [-26, 20], "p2": [-26, -20], "id": "b4"},
        ], plane="plane.top", name="BlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 16, "id": "c1"},
        ], plane="plane.top", name="LCTop", fid="f3", offset=36),
        *sketch_with_id([
            {"type": "line", "p1": [-5, -5], "p2": [5, -5], "id": "s1"},
            {"type": "line", "p1": [5, -5], "p2": [5, 5], "id": "s2"},
            {"type": "line", "p1": [5, 5], "p2": [-5, 5], "id": "s3"},
            {"type": "line", "p1": [-5, 5], "p2": [-5, -5], "id": "s4"},
        ], plane="plane.top", name="LCMid", fid="f4", offset=16),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "TaperPocket",
            "params": {"mode": "cut"},
            "refs": {"sections": [region("f3", 0, 0), region("f4", 0, 0)]}}},
    ]},

    # -----------------------------------------------------------------------
    # Features -> Dress-up (first tranche already live -- do NOT redo)
    # -----------------------------------------------------------------------

    # fillet/chamfer: round/bevel EVERY edge of the block (all 12) so the tile
    # unmistakably reads as the dress-up op. "all_edges" selector (scenarios.cpp
    # resolveSel) walks tid 1..12 and takes every edge; n=12 = a box's edge count.
    # Moderate radius/length so all-corner blends stay geometrically valid at 32px.
    "fillet": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "fillet", "name": "Fillet1",
                     "params": {"radius": dim(5.0)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}}]},

    "chamfer": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "chamfer", "name": "Chamfer1",
                     "params": {"length": dim(4.5)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}}]},

    "shell": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "shell", "name": "Shell1",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}}]},

    # draft: a block with a strongly TAPERED side wall. The standalone `draft`
    # feature needs neutral+faces selectors the iconpipe runner can't resolve, so
    # we bake the taper into the extrude (contract: extrude `draft` dim -> taper).
    # ~18° so the slope reads clearly at 32px. f1=profile sketch, f2=tapered extrude.
    "draft": {"yaw": 35, "pitch": 22, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-26, -20], "p2": [26, -20], "id": "b1"},
            {"type": "line", "p1": [26, -20], "p2": [26, 20], "id": "b2"},
            {"type": "line", "p1": [26, 20], "p2": [-26, 20], "id": "b3"},
            {"type": "line", "p1": [-26, 20], "p2": [-26, -20], "id": "b4"},
        ], plane="plane.top", name="DraftSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "DraftBlock",
            "params": {"mode": "boss", "depth": dim(34), "draft": dim(18)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        accent(),
    ]},

    # rib: an L of two walls with a triangular reinforcement web bridging them.
    # Uses the REAL rib feature: an OPEN profile contour, grown toward the body and
    # fused. f1=L base sketch, f2=extrude, f3=open rib contour, f4=rib.
    "rib": {"yaw": 40, "pitch": 24, "steps": [
        *sketch_with_id([
            # L-shaped base: a back wall + a bottom floor (two thin legs).
            {"type": "line", "p1": [-24, -4], "p2": [24, -4], "id": "b1"},
            {"type": "line", "p1": [24, -4], "p2": [24, 4], "id": "b2"},
            {"type": "line", "p1": [24, 4], "p2": [-16, 4], "id": "b3"},
            {"type": "line", "p1": [-16, 4], "p2": [-16, 40], "id": "b4"},
            {"type": "line", "p1": [-16, 40], "p2": [-24, 40], "id": "b5"},
            {"type": "line", "p1": [-24, 40], "p2": [-24, -4], "id": "b6"},
        ], plane="plane.front", name="LBaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LWalls",
            "params": {"mode": "boss", "depth": dim(30)},
            "refs": {"profile": [region("f1", 0, 6)]}}},
        # Open rib contour: a diagonal line spanning the inside corner of the L,
        # on the mid plane, so the web bridges the vertical wall and the floor.
        *sketch_with_id([
            {"type": "line", "p1": [-16, 36], "p2": [20, 4], "id": "rib1"},
        ], plane="plane.front", name="RibSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "rib", "name": "Rib1",
            "params": {"thickness": dim(5)},
            "refs": {"profile": [region("f3", 0, 20)]}}},
        accent(),
    ]},

    # stock: a rectangular raw chudware for the Manufacture > Stock button. A plain
    # machined-aluminium block (bright metal, mid roughness) that reads as "raw
    # stock material". No annotation -- the chudware IS the icon.
    "stock": {"yaw": 32, "pitch": 24, "steps": [
        box(56, 40, 26),
        appearance(0.90, 0.32, (0.80, 0.82, 0.85)),   # aluminium chudware
    ]},

    # -----------------------------------------------------------------------
    # Features -> Pattern  (seed MUST be sketch-based -- bare box not patternable)
    # -----------------------------------------------------------------------

    # linear_pattern: plate (f1 sk, f2 ext) + seed boss (f3 sk, f4 ext) + pattern (f5).
    "linear_pattern": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-35, -15], "p2": [35, -15], "id": "b1"},
            {"type": "line", "p1": [35, -15], "p2": [35, 15], "id": "b2"},
            {"type": "line", "p1": [35, 15], "p2": [-35, 15], "id": "b3"},
            {"type": "line", "p1": [-35, 15], "p2": [-35, -15], "id": "b4"},
        ], plane="plane.top", name="PlateSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-22, 0], "radius": 7, "id": "c1"},
        ], plane="plane.top", name="BossSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss",
            "params": {"mode": "boss", "depth": dim(12)},
            "merge": False,
            "refs": {"profile": [region("f3", -22, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "linear_pattern", "name": "LinPat",
            "params": {"count1": 3, "spacing1": dim(22)},
            "refs": {"dir1": datum("axis.x"), "features": ["f4"]}}},
        accent("b2"),
    ]},

    # circular_pattern: disc (f1 sk, f2 ext) + spoke (f3 sk, f4 ext) + circ-pat (f5).
    "circular_pattern": {"yaw": 0, "pitch": 60, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 22, "id": "base"},
        ], plane="plane.top", name="DiscSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Disc",
            "params": {"mode": "boss", "depth": dim(6)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [6, -3], "p2": [20, -3], "id": "s1"},
            {"type": "line", "p1": [20, -3], "p2": [20, 3], "id": "s2"},
            {"type": "line", "p1": [20, 3], "p2": [6, 3], "id": "s3"},
            {"type": "line", "p1": [6, 3], "p2": [6, -3], "id": "s4"},
        ], plane="plane.top", name="SpokeSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Spoke",
            "params": {"mode": "boss", "depth": dim(10)},
            "merge": False,
            "refs": {"profile": [region("f3", 13, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "circular_pattern", "name": "Wheel",
            "params": {"count": 6, "angle": dim(360), "equal_spacing": True},
            "refs": {"axis": datum("axis.z"), "features": ["f4"]}}},
        accent("b2"),
    ]},

    # mirror: base (f1 sk, f2 ext) + L-boss (f3 sk, f4 ext) + mirror (f5).
    "mirror": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-30, -14], "p2": [30, -14], "id": "b1"},
            {"type": "line", "p1": [30, -14], "p2": [30, 14], "id": "b2"},
            {"type": "line", "p1": [30, 14], "p2": [-30, 14], "id": "b3"},
            {"type": "line", "p1": [-30, 14], "p2": [-30, -14], "id": "b4"},
        ], plane="plane.top", name="BaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [4, -12], "p2": [26, -12], "id": "l1"},
            {"type": "line", "p1": [26, -12], "p2": [26, -3], "id": "l2"},
            {"type": "line", "p1": [26, -3], "p2": [14, -3], "id": "l3"},
            {"type": "line", "p1": [14, -3], "p2": [14, -8], "id": "l4"},
            {"type": "line", "p1": [14, -8], "p2": [4, -8], "id": "l5"},
            {"type": "line", "p1": [4, -8], "p2": [4, -12], "id": "l6"},
        ], plane="plane.top", name="LBSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LBoss",
            "params": {"mode": "boss", "depth": dim(16)},
            "refs": {"profile": [region("f3", 15, -7)]}}},
        {"op": "add_feature", "feature": {
            "type": "mirror", "name": "Mir1",
            "params": {},
            "refs": {"plane": datum("plane.front"), "features": ["f4"]}}},
        # NOTE: merge=False + accent broke the mirror copy (only the seed
        # rendered); reverted to the proven merged recipe -- the dash annotation
        # carries the intent. Per-body emphasis here needs the per-face follow-up.
    ]},

    # -----------------------------------------------------------------------
    # Features -> Multi-body  (two-body setup; merge=False keeps bodies separate)
    # -----------------------------------------------------------------------

    # combine: block (f1 sk, f2 ext no-merge) + cylinder (f3 sk, f4 ext no-merge) + union.
    "combine": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-20, -14], "p2": [20, -14], "id": "b1"},
            {"type": "line", "p1": [20, -14], "p2": [20, 14], "id": "b2"},
            {"type": "line", "p1": [20, 14], "p2": [-20, 14], "id": "b3"},
            {"type": "line", "p1": [-20, 14], "p2": [-20, -14], "id": "b4"},
        ], plane="plane.top", name="Body1Sk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body1",
            "params": {"mode": "boss", "depth": dim(24)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [16, 0], "radius": 14, "id": "c1"},
        ], plane="plane.top", name="Body2Sk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body2",
            "params": {"mode": "boss", "depth": dim(24)},
            "merge": False,
            "refs": {"profile": [region("f3", 16, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Union1",
            "params": {"op": "union"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
    ]},

    # subtract: block target (f1 sk, f2 ext) + cylinder tool (f3 sk, f4 ext) + subtract.
    "subtract": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-22, -16], "p2": [22, -16], "id": "b1"},
            {"type": "line", "p1": [22, -16], "p2": [22, 16], "id": "b2"},
            {"type": "line", "p1": [22, 16], "p2": [-22, 16], "id": "b3"},
            {"type": "line", "p1": [-22, 16], "p2": [-22, -16], "id": "b4"},
        ], plane="plane.top", name="TgtSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Target",
            "params": {"mode": "boss", "depth": dim(28)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "c1"},
        ], plane="plane.top", name="ToolSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tool",
            "params": {"mode": "boss", "depth": dim(34)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Sub1",
            "params": {"op": "subtract"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
    ]},

    # intersect: square block (f1 sk, f2 ext) ∩ cylinder (f3 sk, f4 ext) → intersect.
    "intersect": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-20, -20], "p2": [20, -20], "id": "b1"},
            {"type": "line", "p1": [20, -20], "p2": [20, 20], "id": "b2"},
            {"type": "line", "p1": [20, 20], "p2": [-20, 20], "id": "b3"},
            {"type": "line", "p1": [-20, 20], "p2": [-20, -20], "id": "b4"},
        ], plane="plane.top", name="BlkSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(30)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="CylSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl",
            "params": {"mode": "boss", "depth": dim(30)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Isect1",
            "params": {"op": "intersect"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
    ]},

    # -----------------------------------------------------------------------
    # Features -> Curves
    # curve_helix emits no solid alone -- sweep a wire circle along the helix.
    # f1=wire circle sketch, f2=helix, f3=sweep spring.
    # -----------------------------------------------------------------------

    "curve_helix": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [10, 0], "radius": 1.5, "id": "c1"},
        ], plane="plane.front", name="WireProf", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "helix", "name": "CoilPath",
            "params": {"pitch": dim(5), "height": dim(20), "radius": dim(10)},
            "refs": {"base": "plane.top"}}},
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Spring",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 10, 0)], "path": "f2"}}},
        accent(),
    ]},

    # -----------------------------------------------------------------------
    # Sheet Metal -> Create  (sm_edge_flange/sm_hem/sm_jog now live below as baked
    # cross-section extrudes -- the draft precedent, no edge selector needed)
    # -----------------------------------------------------------------------

    # sm_base_flange: rect blank, t=2mm. f1=profile sketch, f2=base_flange.
    "sm_base_flange": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-25, -20], "p2": [25, -20], "id": "b1"},
            {"type": "line", "p1": [25, -20], "p2": [25, 20], "id": "b2"},
            {"type": "line", "p1": [25, 20], "p2": [-25, 20], "id": "b3"},
            {"type": "line", "p1": [-25, 20], "p2": [-25, -20], "id": "b4"},
        ], plane="plane.top", name="FlangeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
    ]},

    # sm_flat_pattern: same base then flatten. f1=sketch, f2=flange, f3=flat_pattern.
    "sm_flat_pattern": {"yaw": 0, "pitch": 90, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-25, -20], "p2": [25, -20], "id": "b1"},
            {"type": "line", "p1": [25, -20], "p2": [25, 20], "id": "b2"},
            {"type": "line", "p1": [25, 20], "p2": [-25, 20], "id": "b3"},
            {"type": "line", "p1": [-25, 20], "p2": [-25, -20], "id": "b4"},
        ], plane="plane.top", name="FlangeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "sm_flat_pattern", "name": "Flat", "params": {}}},
    ]},

    # -----------------------------------------------------------------------
    # Features -> Hole / Thread / Split  (real renders replacing procedural glyphs)
    # -----------------------------------------------------------------------

    # hole_wizard: a block with a COUNTERBORED hole bored down through its top face.
    # A wide shallow counterbore (top) over a narrower through bore reads as the Hole
    # Wizard's stepped hole. Pitch bumped above the family 26 so we look DOWN into the
    # bore. f1=block sketch, f2=extrude; f3=cbore circle (top face) + cut; f5=thru circle
    # + deep cut. Both cut centroids sit on the hole axis (0,0) inside the block.
    "hole_wizard": {"yaw": 35, "pitch": 40, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, -20], "p2": [24, -20], "id": "b1"},
            {"type": "line", "p1": [24, -20], "p2": [24, 20], "id": "b2"},
            {"type": "line", "p1": [24, 20], "p2": [-24, 20], "id": "b3"},
            {"type": "line", "p1": [-24, 20], "p2": [-24, -20], "id": "b4"},
        ], plane="plane.top", name="HWBlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "HWBlock",
            "params": {"mode": "boss", "depth": dim(26)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Counterbore: wide circle on the top face (Z=26), shallow cut.
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "cb1"},
        ], plane="plane.top", name="CboreSk", fid="f3", offset=26),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cbore",
            "params": {"mode": "cut", "depth": dim(8)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # Through bore: narrower circle from the top face, cut deep through the block.
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 6, "id": "tb1"},
        ], plane="plane.top", name="ThruSk", fid="f5", offset=26),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Thru",
            "params": {"mode": "cut", "depth": dim(30)},
            "refs": {"profile": [region("f5", 0, 0)]}}},
        accent(),
    ]},

    # thread: a REAL helical thread CUT into a SOLID rod (screw / threaded rod), via the
    # engine's one-op `thread` feature (document.cpp type=="thread"). Build a solid
    # cylinder (extrude a circle), then add ONE thread feature that references the shank's
    # lateral cylindrical face by a plain-language geom query and cuts a helical ISO-60°
    # V-groove INWARD (mode:"cut").
    #
    # Sizing: in cut mode the cylinder IS the major/outer Ø and the V-groove is cut inward,
    # so the body stays a SOLID metal rod with grooves in its surface — NOT a translucent
    # coil/spring (which is what raised-mode's proud helix on a thin core looked like).
    # Chunky rod Ø15 (radius 7.5) x 42 long ≈ 2.8:1, so it reads as a solid screw shaft.
    # major_dia 15 == cylinder Ø; pitch 3.5 over the shank ≈ 12 clearly-slanted V-grooves,
    # coarse enough to read at 32px. V60 form (default). turns cap keeps the boolean tame.
    # target_face = {"of":"face","kind":"cylindrical","pick":"largest"} → the sole lateral
    # cylinder of the extruded rod (resolve_face_sel geom-query path). Steel appearance so
    # it reads as a threaded fastener, not app teal.
    "thread": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 7.5, "id": "sc1"},
        ], plane="plane.top", name="StudSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Stud",
            "params": {"mode": "boss", "depth": dim(42)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # ONE real helical thread CUT into the SOLID shank. The cylinder is the major/outer
        # Ø (Ø15) and the V-groove is cut INWARD, keeping the body a continuous solid rod.
        {"op": "add_feature", "feature": {
            "type": "thread", "name": "Thread",
            "params": {"major_dia": dim(15), "pitch": dim(3.5),
                       "style": "external", "mode": "cut", "form": "v",
                       "turns": dim(14)},
            "refs": {"target_face": {"of": "face", "kind": "cylindrical",
                                     "pick": "largest"}}}},
        # Metallic steel so it reads as a threaded fastener.
        {"op": "set_appearance", "id": "b1", "color": [0.70, 0.72, 0.75],
         "metallic": 0.9, "roughness": 0.35},
    ]},

    # split: a block cleaved into TWO separate bodies by a central plane, drawn apart a
    # little and colored differently (accent teal + grey) so the split reads. Two half
    # blocks with merge=False on either side of a small gap at X=0. b1=left (teal via
    # accent), b2=right (grey). Iso family camera.
    "split": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, -18], "p2": [-2, -18], "id": "l1"},
            {"type": "line", "p1": [-2, -18], "p2": [-2, 18], "id": "l2"},
            {"type": "line", "p1": [-2, 18], "p2": [-24, 18], "id": "l3"},
            {"type": "line", "p1": [-24, 18], "p2": [-24, -18], "id": "l4"},
        ], plane="plane.top", name="LeftSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LeftHalf",
            "params": {"mode": "boss", "depth": dim(26)},
            "merge": False,
            "refs": {"profile": [region("f1", -13, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [2, -18], "p2": [24, -18], "id": "r1"},
            {"type": "line", "p1": [24, -18], "p2": [24, 18], "id": "r2"},
            {"type": "line", "p1": [24, 18], "p2": [2, 18], "id": "r3"},
            {"type": "line", "p1": [2, 18], "p2": [2, -18], "id": "r4"},
        ], plane="plane.top", name="RightSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "RightHalf",
            "params": {"mode": "boss", "depth": dim(26)},
            "merge": False,
            "refs": {"profile": [region("f3", 13, 0)]}}},
        accent("b1"),   # left half in app teal
        {"op": "set_appearance", "id": "b2", "color": [0.62, 0.66, 0.70],
         "metallic": 0.1, "roughness": 0.6},   # right half grey
    ]},

    # -----------------------------------------------------------------------
    # Sheet Metal -> Edge Flange / Hem / Jog
    # The real sm_edge_flange/sm_hem/sm_jog features fold a flange on a PICKED edge, which
    # the iconpipe runner can't resolve (region + canned selrefs only). So (draft
    # precedent) we BAKE each into a thin cross-section profile extruded along the part
    # width: one closed loop, one extrude, accent(). Reads as the folded sheet without any
    # edge selector. Thin (t~2mm) sheet bodies in app teal.
    # -----------------------------------------------------------------------

    # sm_edge_flange: base flat with an extra flange folded UP on one edge -> L section.
    # Profile on plane.front (XZ): base runs X=-24..24 at Z=0 (t=2), then the right end
    # rises Z=0..18 (the folded-up edge flange). Extruded along Y for the sheet width.
    "sm_edge_flange": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            # closed L: base strip + upstand on the +X edge, thickness 2mm.
            {"type": "line", "p1": [-24, 0], "p2": [24, 0], "id": "e1"},
            {"type": "line", "p1": [24, 0], "p2": [24, 18], "id": "e2"},
            {"type": "line", "p1": [24, 18], "p2": [22, 18], "id": "e3"},
            {"type": "line", "p1": [22, 18], "p2": [22, 2], "id": "e4"},
            {"type": "line", "p1": [22, 2], "p2": [-24, 2], "id": "e5"},
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "e6"},
        ], plane="plane.front", name="EdgeFlSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "EdgeFlange",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", 0, 1)]}}},
        accent(),
    ]},

    # sm_hem: a flat strip whose edge is folded BACK parallel onto itself (a hem) -> a thin
    # hairpin/J section. Profile on plane.front: base strip Z=0..2, then the right edge
    # wraps up and folds back over the top creating a doubled-over lip. Extruded along Y.
    "sm_hem": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            # base strip bottom
            {"type": "line", "p1": [-24, 0], "p2": [20, 0], "id": "h1"},
            # up the return end
            {"type": "line", "p1": [20, 0], "p2": [20, 9], "id": "h2"},
            # fold back over the top (parallel lip pointing -X)
            {"type": "line", "p1": [20, 9], "p2": [4, 9], "id": "h3"},
            {"type": "line", "p1": [4, 9], "p2": [4, 7], "id": "h4"},
            {"type": "line", "p1": [4, 7], "p2": [18, 7], "id": "h5"},
            {"type": "line", "p1": [18, 7], "p2": [18, 2], "id": "h6"},
            {"type": "line", "p1": [18, 2], "p2": [-24, 2], "id": "h7"},
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "h8"},
        ], plane="plane.front", name="HemSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hem",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", -10, 1)]}}},
        accent(),
    ]},

    # sm_jog: a flat strip with two close bends making an OFFSET step -> a thin Z/S section.
    # Profile on plane.front: left strip at Z=0..2, a short riser at X~2..4 up to Z=12,
    # then the right strip at Z=12..14 -- an offset step. Extruded along Y.
    "sm_jog": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, 0], "p2": [2, 0], "id": "j1"},   # left strip bottom
            {"type": "line", "p1": [2, 0], "p2": [2, 12], "id": "j2"},     # riser (outer)
            {"type": "line", "p1": [2, 12], "p2": [24, 12], "id": "j3"},   # right strip bottom
            {"type": "line", "p1": [24, 12], "p2": [24, 14], "id": "j4"},  # right end
            {"type": "line", "p1": [24, 14], "p2": [4, 14], "id": "j5"},   # right strip top
            {"type": "line", "p1": [4, 14], "p2": [4, 2], "id": "j6"},     # riser (inner)
            {"type": "line", "p1": [4, 2], "p2": [-24, 2], "id": "j7"},    # left strip top
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "j8"},  # left end
        ], plane="plane.front", name="JogSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Jog",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", -10, 1)]}}},
        accent(),
    ]},

    # -----------------------------------------------------------------------
    # Weldments -> Structural Member / Gusset / End Cap  (adapt `sweep`)
    # -----------------------------------------------------------------------

    # weld_member: an I-beam profile swept along a straight path -- a structural member.
    # Single closed I-section loop (one region) on plane.right, path a straight line on
    # plane.top (copies the sweep recipe structure). accent() = app teal beam.
    "weld_member": {"yaw": 38, "pitch": 26, "steps": [
        *sketch_with_id([
            # I-beam: bottom flange, web, top flange -- one closed loop.
            {"type": "line", "p1": [-12, -14], "p2": [12, -14], "id": "i1"},   # bottom flange base
            {"type": "line", "p1": [12, -14], "p2": [12, -10], "id": "i2"},
            {"type": "line", "p1": [12, -10], "p2": [3, -10], "id": "i3"},
            {"type": "line", "p1": [3, -10], "p2": [3, 10], "id": "i4"},        # web right
            {"type": "line", "p1": [3, 10], "p2": [12, 10], "id": "i5"},
            {"type": "line", "p1": [12, 10], "p2": [12, 14], "id": "i6"},       # top flange
            {"type": "line", "p1": [12, 14], "p2": [-12, 14], "id": "i7"},
            {"type": "line", "p1": [-12, 14], "p2": [-12, 10], "id": "i8"},
            {"type": "line", "p1": [-12, 10], "p2": [-3, 10], "id": "i9"},
            {"type": "line", "p1": [-3, 10], "p2": [-3, -10], "id": "i10"},     # web left
            {"type": "line", "p1": [-3, -10], "p2": [-12, -10], "id": "i11"},
            {"type": "line", "p1": [-12, -10], "p2": [-12, -14], "id": "i12"},
        ], plane="plane.right", name="IBeamSk", fid="f1"),
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [60, 0], "id": "p1"},
        ], plane="plane.top", name="MemberPath", fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Member",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", 30, 0)}}},
        accent(),
    ]},

    # weld_gusset: a triangular corner gusset plate -- a right-triangle plate extruded
    # thin. A closed right triangle on plane.front, extruded a small depth for the plate
    # thickness. accent() = app teal plate.
    "weld_gusset": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-18, -16], "p2": [18, -16], "id": "g1"},   # base leg
            {"type": "line", "p1": [18, -16], "p2": [-18, 16], "id": "g2"},     # hypotenuse
            {"type": "line", "p1": [-18, 16], "p2": [-18, -16], "id": "g3"},    # vertical leg
        ], plane="plane.front", name="GussetSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Gusset",
            "params": {"mode": "boss", "depth": dim(5)},
            "refs": {"profile": [region("f1", -10, -10)]}}},
        accent(),
    ]},

    # weld_end_cap: a box member with a cap plate closing one end. Two separate bodies
    # (member b1 grey + end cap b2 accent teal) via merge=False, colored per-body like
    # ws_electronics/combine -- avoids the hollow-tube two-loop problem. Member is a box
    # along X; cap is a thin plate on the +X end face.
    "weld_end_cap": {"yaw": 38, "pitch": 26, "steps": [
        # Member: rectangular tube stand-in (solid box) on plane.right, swept/extruded
        # along X via a box primitive is simplest -- use a sketch+extrude along Y? Keep it
        # a box primitive offset so the cap sits on the +X end.
        *sketch_with_id([
            {"type": "line", "p1": [-14, -12], "p2": [14, -12], "id": "m1"},
            {"type": "line", "p1": [14, -12], "p2": [14, 12], "id": "m2"},
            {"type": "line", "p1": [14, 12], "p2": [-14, 12], "id": "m3"},
            {"type": "line", "p1": [-14, 12], "p2": [-14, -12], "id": "m4"},
        ], plane="plane.right", name="TubeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tube",
            "params": {"mode": "boss", "depth": dim(50)},
            "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # End cap: a slightly larger plate on the +X end face (Z=50 along the extrude dir),
        # thin. Sketch on the same plane offset to the far end, extruded a small depth.
        *sketch_with_id([
            {"type": "line", "p1": [-16, -14], "p2": [16, -14], "id": "cp1"},
            {"type": "line", "p1": [16, -14], "p2": [16, 14], "id": "cp2"},
            {"type": "line", "p1": [16, 14], "p2": [-16, 14], "id": "cp3"},
            {"type": "line", "p1": [-16, 14], "p2": [-16, -14], "id": "cp4"},
        ], plane="plane.right", name="CapSk", fid="f3", offset=50),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cap",
            "params": {"mode": "boss", "depth": dim(4)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "set_appearance", "id": "b1", "color": [0.62, 0.66, 0.70],
         "metallic": 0.4, "roughness": 0.45},   # grey member
        accent("b2"),   # end cap in app teal
    ]},

    # -----------------------------------------------------------------------
    # Appearance -> Material (first tranche already live -- do NOT redo)
    # appr_glass / appr_paint: atlas cells exist but NOT wired to any ribbon button,
    # so they are skipped.
    # -----------------------------------------------------------------------

    # Material presets: one SPHERE per material in its OWN preset color, so each of the
    # 5 Appearance buttons (Plastic/Aluminum/Steel/Brass/Dark Metal) has a distinct swatch.
    # rgb/metallic/roughness mirror ui::kMaterials (host/src/ui/appearance_common.cpp).
    "appr_plastic": {"yaw": 33, "pitch": 26, "steps": [
        sphere(22), appearance(0.00, 0.50, (0.71, 0.76, 0.82))]},   # Plastic
    "appr_metal":   {"yaw": 33, "pitch": 26, "steps": [
        sphere(22), appearance(0.90, 0.35, (0.83, 0.85, 0.88))]},   # Aluminum (silver)
    "appr_steel":   {"yaw": 33, "pitch": 26, "steps": [
        sphere(22), appearance(0.85, 0.25, (0.56, 0.58, 0.62))]},   # Steel (darker)
    "appr_brass":   {"yaw": 33, "pitch": 26, "steps": [
        sphere(22), appearance(0.95, 0.30, (0.80, 0.62, 0.28))]},   # Brass (warm)
    "appr_dark":    {"yaw": 33, "pitch": 26, "steps": [
        sphere(22), appearance(0.90, 0.45, (0.22, 0.23, 0.26))]},   # Dark Metal (near-black, nudged up so it's not pure black)
    "appr_default": {"yaw": 33, "pitch": 26, "steps": [
        sphere(22)]},                                               # engine default grey

    # -----------------------------------------------------------------------
    # Appearance -> Display: display_* are line-art SDF glyphs, NOT photographic.
    # SDF invisible-icon bug (kFar int32 overflow) fixed (kFar=1<<12).
    # In-context verification via CHUDWARE_RIBBON_TAB=Appearance is the right check.
    # Not added here -- photographic renders would override correct line-art glyphs.
    # -----------------------------------------------------------------------
}


# ---------------------------------------------------------------------------------------
# Operation-intent annotation overlays. Minimal, one cue per tile, composited in
# Python post-render (never in the C++ renderer). Coordinates are normalized (0..1) tile
# space. Drawn at 4x then downscaled so they stay crisp at 32/20 px.
#   arrow  [x0,y0,x1,y1]      straight arrow head at (x1,y1)         (direction of the op)
#   arc    [cx,cy,r,a0,a1]    circular arrow, degrees, head at a1    (rotation/revolve)
#   dash   [x0,y0,x1,y1]      dashed line                            (mirror/symmetry plane)
#   darrow [x0,y0,x1,y1]      double-headed arrow                    (unfold/flatten)
#   badge  "+"|"-"|"n"        corner disc badge                      (boolean union/cut/isect)
#   ring   [cx,cy,r]          spotlight ring                         (look-here: fillet edge)
# Colors: app accent teal + dark keyline so the cue reads on BOTH theme backgrounds.
# ---------------------------------------------------------------------------------------
ANNOTATIONS: dict[str, dict] = {
    "extrude_boss":     {"arrow": [0.50, 0.30, 0.44, 0.05]},   # grows OUT of the top-face center along the extrude axis
    "extrude_cut":      {"arrow": [0.50, 0.14, 0.50, 0.46]},   # cut plunges down into the pocket
    "draft":            {"arrow": [0.30, 0.30, 0.20, 0.66]},   # taper slope cue on the drafted wall
    "rib":              {"ring": [0.52, 0.52, 0.15]},          # look-here on the reinforcing web
    # stock: no annotation -- the raw chudware IS the icon.
    "revolve_boss":     {"arc": [0.50, 0.22, 0.17, 210, 330]},
    "revolve_cut":      {"arc": [0.50, 0.22, 0.17, 210, 330]},
    "sweep":            {"arrow": [0.68, 0.52, 0.68, 0.16]},
    "sweep_cut":        {"arrow": [0.22, 0.36, 0.72, 0.36]},
    "loft":             {"arrow": [0.85, 0.72, 0.85, 0.24]},
    "loft_cut":         {"arrow": [0.50, 0.10, 0.50, 0.42]},
    "fillet":           {"ring": [0.23, 0.48, 0.14]},
    "chamfer":          {"ring": [0.23, 0.48, 0.14]},
    "shell":            {"arrow": [0.50, 0.08, 0.50, 0.38]},
    "linear_pattern":   {"arrow": [0.24, 0.28, 0.78, 0.28]},
    "circular_pattern": {"arc": [0.50, 0.48, 0.34, -50, 190]},
    "mirror":           {"dash": [0.10, 0.63, 0.94, 0.38]},  # y=0 plane trace
    "combine":          {"badge": "+"},
    "subtract":         {"badge": "-"},
    "intersect":        {"badge": "n"},
    "curve_helix":      {"arc": [0.50, 0.26, 0.18, 220, 320]},
    "sm_flat_pattern":  {"darrow": [0.20, 0.42, 0.80, 0.42]},
    # appr_* tiles: no annotation -- they ARE the material preset.
    # cam_* tiles: the backplot colors already annotate cut/rapid/entry; no overlay.
}


# ---------------------------------------------------------------------------------------
# 2026-08-17 full-coverage wave: every ribbon tool icon gets an engine-rendered tile.
# The per-group modules in scripts/icon_recipes/ carry the recipes; restyle_features
# REPLACES 35 first-tranche entries above (grey-base/teal-action + bolder cues), the
# other modules append new names (sketch entities/edits, simulation, evaluate,
# assembly+reference, ECAD/schematic). ANN retunes merge the same way.
# ---------------------------------------------------------------------------------------
from icon_recipes import (asm_features, ecad, evaluate, restyle_features,  # noqa: E402
                          simulation, sketch_edits, sketch_entities)

for _m in (restyle_features, sketch_entities, sketch_edits, simulation,
           evaluate, asm_features, ecad):
    RECIPES.update(_m.RECIPES)
    ANNOTATIONS.update(getattr(_m, "ANN", {}))
del _m

# Flat annotation ink. These pixels are baked into the tile exactly like the rendered
# geometry, so they MUST be one of the two accent references — a third hue here would sit
# out the runtime retint in host/src/ui/icons.cpp and leave every ring, badge and dash the
# factory color while the rest of the tile followed the user's pick.
#
# Secondary, not primary: an annotation is a cue laid over the result, which is the same
# role the 3D direction arrows carry. Keeping them one color also means a ring and an arrow
# on the same tile read as one annotation layer.
_ANN_INK = tuple(round(c * 255) for c in SECONDARY_RGB) + (255,)
# Dark keyline under the ink so it survives on both the light and dark atlas. Neutral, and
# far enough below the saturation gate (kAccentSatLo) that the retint leaves it alone.
_KEYLINE = (10, 14, 20, 210)


def _draw_arrow(d, p0, p1, w, col):
    import math as _m
    d.line([p0, p1], fill=col, width=w)
    ang = _m.atan2(p1[1] - p0[1], p1[0] - p0[0])
    ah = w * 3.2
    for da in (2.55, -2.55):
        d.line([p1, (p1[0] + ah * _m.cos(ang + da), p1[1] + ah * _m.sin(ang + da))],
               fill=col, width=w)


def _annotation_layer(spec: dict, px: int) -> Image.Image:
    """Render one annotation spec to a transparent px*px layer (4x supersampled)."""
    import math as _m
    from PIL import ImageDraw
    S = 4
    sp = px * S
    img = Image.new("RGBA", (sp, sp), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    W = max(3, sp // 25)          # cue stroke width — bold enough to read at 32 px
    KW = W + max(2, W // 2)       # keyline width

    def P(x, y):
        return (x * sp, y * sp)

    for col, w in ((_KEYLINE, KW), (_ANN_INK, W)):
        if "arrow" in spec:
            x0, y0, x1, y1 = spec["arrow"]
            _draw_arrow(d, P(x0, y0), P(x1, y1), w, col)
        if "arrows" in spec:      # repeated arrow (distributed load, pressure)
            for x0, y0, x1, y1 in spec["arrows"]:
                _draw_arrow(d, P(x0, y0), P(x1, y1), w, col)
        if "dots" in spec:        # pick points (3-pt tools, perimeter picks)
            for dot in spec["dots"]:
                dx, dy, dr = (dot + [0.045])[:3]
                r = dr * sp
                cx, cy = P(dx, dy)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
        if "darrow" in spec:
            x0, y0, x1, y1 = spec["darrow"]
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            _draw_arrow(d, P(mx, my), P(x1, y1), w, col)
            _draw_arrow(d, P(mx, my), P(x0, y0), w, col)
        if "dash" in spec:
            x0, y0, x1, y1 = spec["dash"]
            n = 7
            for i in range(n):
                t0, t1 = i / n, (i + 0.62) / n
                d.line([P(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                        P(x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)], fill=col, width=w)
        if "arc" in spec:
            cx, cy, r, a0, a1 = spec["arc"]
            bb = [P(cx - r, cy - r)[0], P(cx - r, cy - r)[1],
                  P(cx + r, cy + r)[0], P(cx + r, cy + r)[1]]
            d.arc(bb, start=min(a0, a1), end=max(a0, a1), fill=col, width=w)
            a1r = _m.radians(a1)
            tip = (cx * sp + r * sp * _m.cos(a1r), cy * sp + r * sp * _m.sin(a1r))
            tang = a1r + (_m.pi / 2 if a1 >= a0 else -_m.pi / 2)
            back = (tip[0] - w * 3.0 * _m.cos(tang), tip[1] - w * 3.0 * _m.sin(tang))
            _draw_arrow(d, back, tip, w, col)
        if "ring" in spec:
            cx, cy, r = spec["ring"]
            bb = [P(cx - r, cy - r)[0], P(cx - r, cy - r)[1],
                  P(cx + r, cy + r)[0], P(cx + r, cy + r)[1]]
            d.ellipse(bb, outline=col, width=w)
        if "badge" in spec:
            r = sp * 0.14
            bx, by = sp * 0.80, sp * 0.20
            if col is _KEYLINE:
                d.ellipse([bx - r - KW, by - r - KW, bx + r + KW, by + r + KW], fill=col)
            else:
                d.ellipse([bx - r, by - r, bx + r, by + r], fill=col)
                sym = spec["badge"]
                sw = max(3, W)
                a = r * 0.52
                # Knocked-out glyph on the badge disc. Near-black and unsaturated so the
                # retint leaves it alone and it stays legible whatever hue the disc becomes.
                ink = (8, 12, 18, 255)
                if sym == "+":
                    d.line([bx - a, by, bx + a, by], fill=ink, width=sw)
                    d.line([bx, by - a, bx, by + a], fill=ink, width=sw)
                elif sym == "-":
                    d.line([bx - a, by, bx + a, by], fill=ink, width=sw)
                elif sym == "check":  # pass/verified (DRC, ERC, Check)
                    d.line([bx - a, by + a * 0.1, bx - a * 0.15, by + a * 0.75],
                           fill=ink, width=sw)
                    d.line([bx - a * 0.15, by + a * 0.75, bx + a, by - a * 0.6],
                           fill=ink, width=sw)
                else:  # "n" -> intersection cup
                    d.arc([bx - a, by - a, bx + a, by + a * 1.6],
                          start=180, end=360, fill=ink, width=sw)
                    d.line([bx - a, by, bx - a, by + a * 0.8], fill=ink, width=sw)
                    d.line([bx + a, by, bx + a, by + a * 0.8], fill=ink, width=sw)
    return img.resize((px, px), Image.LANCZOS)


def load_tile(name: str) -> Image.Image | None:
    """Load a rendered tile and composite its operation-intent annotation (if any).
    Single source used by both atlas injection and the contact sheets, so what
    gets reviewed is exactly what ships."""
    src = RENDER_DIR / f"{name}.png"
    if not src.exists():
        return None
    img = Image.open(src).convert("RGBA")
    spec = ANNOTATIONS.get(name)
    if spec:
        img = img.copy()
        img.alpha_composite(_annotation_layer(spec, img.size[0]))
    return img


# ---------------------------------------------------------------------------------------
# Byte-preserving atlas extension for NEW cells (the cam_* Manufacture tiles).
# Existing rows/UVs are untouched; new 34px rows are appended at the bottom following the
# exact make_icons.py layout (@32 at x=0,y+1; @20 at x=34,y+7). make_icons.py also knows
# these names (CONTRACT_NAMES + drawers), so a full atlas regen produces the same layout;
# this path just avoids regenerating (and re-eyeballing) all ~190 existing glyph rows.
# ---------------------------------------------------------------------------------------

def ensure_cells(names: list[str]) -> None:
    """Give every name in ``names`` an @32/@20 cell, re-laying the grid if needed.

    This used to append one 34 px row to the BOTTOM of a single 56 px-wide column
    per new name, growing the atlas without bound. That is how it reached 9418 px
    tall — past the 8192 GL_MAX_TEXTURE_SIZE that ANGLE/SwiftShader and many real
    GPUs report, so the web client's texture upload failed and every icon drew as
    an opaque black square. The layout now comes from make_icons.layout_cells, the
    one place that knows the grid, and existing art is carried across BY NAME so a
    re-layout can never scramble or drop a curated tile.
    """
    atlas_path = ICON_DIR / "atlas.json"
    atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
    cells = atlas["cell"]
    missing = [n for n in names if f"{n}@32" not in cells]
    if not missing:
        return
    # placeholder glyphs come from make_icons.py's registered drawers (SDF-style line
    # art) so a cell is never blank even if its toolpath render fails.
    sys.path.insert(0, str(REPO / "assets"))
    mi = None
    try:
        import make_icons as mi

        def placeholder(name, sz, fn):
            pal = mi.DARK if fn == "atlas_dark.png" else mi.LIGHT
            try:
                return mi._render_icon(name, sz, pal)
            except Exception:
                # no registered drawer for this name (e.g. a fresh RECIPE-only
                # slot like `stock`) -> transparent placeholder; inject() fills it.
                return Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    except Exception as e:  # pragma: no cover -- make_icons import is best-effort
        print(f"   ensure_cells: no placeholder glyphs ({e})")

        def placeholder(name, sz, fn):
            return Image.new("RGBA", (sz, sz), (0, 0, 0, 0))

    if mi is None:
        # Without the layout helpers there is no safe way to place a new cell:
        # guessing would be how the unbounded column came back.
        raise RuntimeError(
            "icon_pipeline: cannot extend the atlas without assets/make_icons.py "
            "(needed for the atlas grid geometry)"
        )

    # Existing names keep their current order, read back from the committed
    # layout so the grid is rebuilt rather than reshuffled; new names append.
    def _block_key(name: str) -> tuple[int, int]:
        c = cells[f"{name}@32"]
        return c["x"], c["y"]

    existing = sorted({k.rsplit("@", 1)[0] for k in cells}, key=_block_key)
    order = existing + missing
    new_w, new_h, new_cells = mi.layout_cells(order)

    for fn in ("atlas_dark.png", "atlas_light.png"):
        old = Image.open(ICON_DIR / fn).convert("RGBA")
        grown = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        for key, c in new_cells.items():
            src = cells.get(key)
            if src is not None:
                tile = old.crop(
                    (src["x"], src["y"], src["x"] + src["w"], src["y"] + src["h"])
                )
                if src["w"] != c["w"] or src["h"] != c["h"]:
                    tile = tile.resize((c["w"], c["h"]), Image.LANCZOS)
            else:
                tile = placeholder(key.rsplit("@", 1)[0], c["w"], fn)
            grown.paste(tile, (c["x"], c["y"]))
        grown.save(ICON_DIR / fn)

    atlas["cell"] = new_cells
    atlas["atlas_px"] = [new_w, new_h]
    atlas_path.write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    print(f"[icon_pipeline] atlas relaid to {new_w}x{new_h} px for "
          f"{len(missing)} new cell(s): {', '.join(missing)}")


def _flatten_steps(steps: list) -> list:
    """Flatten any step that is itself a list (e.g. from sketch_with_id) into a flat list.
    Deferred sketch entity placeholders (_sketch_entity_deferred) are silently dropped
    here -- callers should use sketch_with_id() with an explicit fid instead."""
    out = []
    for s in steps:
        if isinstance(s, list):
            out.extend(_flatten_steps(s))
        elif isinstance(s, dict) and s.get("_sketch_entity_deferred"):
            pass  # dropped; see sketch() docstring
        else:
            out.append(s)
    return out


def write_recipes(path: Path, only: set[str] | None = None) -> None:
    recipes = [{"name": n, "yaw": r["yaw"], "pitch": r["pitch"],
                "steps": _flatten_steps(r["steps"])}
               for n, r in RECIPES.items() if only is None or n in only]
    path.write_text(json.dumps({"out_dir": str(RENDER_DIR), "px": RENDER_PX,
                                "recipes": recipes}, indent=1), encoding="utf-8")


def run_app(recipes_json: Path, theme: str = "dark", timeout_s: int = 300) -> int:
    """Run the host GUI headless (CHUDWARE_SHOT=iconpipe) to render every recipe to RENDER_DIR.

    Targets host/build-linux/bin/Chudware (the retired ui/imgui frontend is gone).
    On Linux we wrap in xvfb-run (llvmpipe) for a deterministic, GPU-independent server; on
    Windows the desktop session supplies GL, so the exe runs directly. One invocation drains ALL
    recipes in a single tick.
    """
    if not BIN.exists():
        sys.exit(f"binary not found: {BIN} -- build the host GUI first:\n"
                 f"  cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release\n"
                 f"  ninja -C host/build-linux Chudware")
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "CHUDWARE_NO_FILE_DIALOGS": "1",
        "CHUDWARE_REPO": str(REPO),
        "CHUDWARE_SHOT": "iconpipe",
        "CHUDWARE_ICON_RECIPES": str(recipes_json),
        "CHUDWARE_THEME": theme,
        # Create the GLFW window unmapped so the headless capture-then-exit path
        # runs cleanly under xvfb without ever mapping a visible window.
        "CHUDWARE_HIDDEN": "1",
    })
    throwaway = REPO / "scratch" / "iconpipe_final.png"
    # The headless capture-then-exit path is entered ONLY via --ui-screenshot
    # (main.cpp): it calls run_gui(path) with shot=true, which drains the iconpipe
    # recipes and exits. A bare --shot flag is unrecognized and falls through to
    # interactive GUI mode, which hangs forever headlessly.
    cmd = [str(BIN), "--ui-screenshot", str(throwaway)]
    if sys.platform.startswith("linux") and shutil.which("xvfb-run"):
        env.pop("DISPLAY", None)  # force xvfb-run's own server; the live :0 may be unattended
        cmd = ["xvfb-run", "-a", *cmd]
    n_req = len(json.loads(recipes_json.read_text(encoding="utf-8"))["recipes"])
    print(f"[icon_pipeline] rendering {n_req} recipes via {BIN.name} (theme={theme}) ...")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s)
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


def _recipe_uses_accent(recipe: dict) -> bool:
    """True when any build step paints a body with a PRIMARY/SECONDARY accent color.

    Matched on the color VALUE, not on which helper produced it, so a recipe that inlines
    the constant instead of calling accent() is still caught. Material presets (brass,
    steel, ...) and CAM toolpaths use their own colors and correctly test False.
    """
    refs = (tuple(PRIMARY_RGB), tuple(SECONDARY_RGB))
    for step in _flatten_steps(recipe["steps"]):
        if step.get("op") != "set_appearance":
            continue
        col = step.get("color")
        if not col or len(col) < 3:
            continue
        if any(all(abs(float(col[i]) - ref[i]) < 1e-4 for i in range(3)) for ref in refs):
            return True
    return False


def inject(names: list[str]) -> list[str]:
    """Composite each rendered tile into the @32/@20 cells of both atlases (in place).
    Only the named cells are overwritten; every other icon is preserved byte-for-byte."""
    atlas = json.loads((ICON_DIR / "atlas.json").read_text(encoding="utf-8"))
    cells = atlas["cell"]
    done: list[str] = []
    atlases = {fn: Image.open(ICON_DIR / fn).convert("RGBA")
               for fn in ("atlas_dark.png", "atlas_light.png")}
    for name in names:
        render = load_tile(name)  # annotated tile or None if not rendered
        if render is None:
            print(f"   SKIP {name}: no render at {RENDER_DIR / (name + '.png')}")
            continue
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
    # Publish which tiles carry an accent role, and the reference colors they were baked
    # with. host/src/ui/icons.cpp rotates exactly these cells from the reference hues to
    # whatever primary/secondary the user picked, and leaves every other cell alone — so a
    # brass appearance swatch or a CAM toolpath tile keeps the color it is *depicting*.
    # Derived from the recipe steps rather than a hand-kept list: a recipe that stops using
    # accent() drops out of the set automatically.
    tinted = set(atlas.get("accent_tinted", []))
    for name in done:
        recipe = RECIPES.get(name)
        if recipe is None:
            continue  # CAM tiles etc. — rendered, but never accent-colored
        if _recipe_uses_accent(recipe):
            tinted.add(name)
        else:
            tinted.discard(name)
    atlas["accent_tinted"] = sorted(tinted)
    atlas["accent_ref"] = {"primary": [round(c, 6) for c in PRIMARY_RGB],
                           "secondary": [round(c, 6) for c in SECONDARY_RGB]}
    (ICON_DIR / "atlas.json").write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    # Deploy to the built app so a verification run picks the new art up immediately.
    if DEPLOYED_ICON_DIR.exists():
        for fn in list(atlases) + ["atlas.json"]:
            shutil.copy2(ICON_DIR / fn, DEPLOYED_ICON_DIR / fn)
    print(f"[icon_pipeline] injected {len(done)} icon(s): {', '.join(done)}")
    return done


def contact_sheet(names: list[str]) -> None:
    """Light + dark review sheets of every tile, at review size AND true 32px ribbon
    size, using load_tile so annotations/coloring are exactly what ships."""
    from PIL import ImageDraw
    themes = {
        "dark": {"bg": (44, 46, 52, 255), "fg": (230, 230, 230, 255),
                 "path": CONTACT_SHEET.with_name("icon_contact_sheet_dark.png")},
        "light": {"bg": (238, 240, 243, 255), "fg": (40, 44, 50, 255),
                  "path": CONTACT_SHEET.with_name("icon_contact_sheet_light.png")},
    }
    cols = 6
    cell, pad, label = 96, 12, 16
    rows = (len(names) + cols - 1) // cols
    W, H = cols * (cell + pad) + pad, rows * (cell + label + pad) + pad
    for theme, t in themes.items():
        sheet = Image.new("RGBA", (W, H), t["bg"])
        draw = ImageDraw.Draw(sheet)
        for i, name in enumerate(names):
            cx = pad + (i % cols) * (cell + pad)
            cy = pad + (i // cols) * (cell + label + pad)
            tile = load_tile(name)
            if tile is not None:
                r = tile.resize((cell, cell), Image.LANCZOS)
                sheet.alpha_composite(r, (cx, cy))
                r32 = tile.resize((32, 32), Image.LANCZOS)   # true ribbon size
                sheet.alpha_composite(r32, (cx + cell - 34, cy + cell - 34))
            draw.text((cx + 2, cy + cell + 2), name, fill=t["fg"])
        sheet.convert("RGB").save(t["path"])
        print(f"[icon_pipeline] contact sheet ({theme}) -> {t['path']}")
    # legacy single-sheet path stays valid for older tooling/links
    shutil.copy2(themes["dark"]["path"], CONTACT_SHEET)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true", help="run the app, regenerate, inject")
    ap.add_argument("--inject-only", action="store_true", help="re-tile from existing renders")
    ap.add_argument("--list", action="store_true", help="print the recipe table and exit")
    ap.add_argument("--theme", default="dark", help="CHUDWARE_THEME for the render run")
    ap.add_argument("--only", default=None,
                    help="comma-separated icon names: render/inject just these (fast "
                         "iteration on one family; every other atlas cell is untouched)")
    args = ap.parse_args(argv)

    if args.list:
        for n, r in RECIPES.items():
            print(f"{n:16s} yaw={r['yaw']:>3} pitch={r['pitch']:>3} steps={len(r['steps'])}")
        return 0

    from cam_toolpath_icons import CAM_CELLS, render_all as cam_render_all

    only: set[str] | None = None
    if args.only:
        only = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = sorted(only - set(RECIPES) - set(CAM_CELLS))
        if unknown:
            sys.exit(f"--only: no such recipe: {', '.join(unknown)}")

    names = [n for n in list(RECIPES.keys()) + CAM_CELLS if only is None or n in only]
    cam_failures: dict[str, str] = {}
    if args.render:
        recipes_json = REPO / "scratch" / "icon_recipes.json"
        recipes_json.parent.mkdir(parents=True, exist_ok=True)
        write_recipes(recipes_json, only=only)
        rc = run_app(recipes_json, theme=args.theme)
        print(f"[icon_pipeline] app exit={rc}")
        # Manufacture tiles from the REAL CAM chain (also a CAM smoke test)
        # Skipped under --only unless a cam_* tile was actually asked for: the CAM chain
        # is minutes of solving and has nothing to do with a modeling-tile iteration.
        if only is None or any(n.startswith("cam_") for n in only):
            _, cam_failures = cam_render_all()

    if args.render or args.inject_only:
        # Append any missing atlas cells for RECIPE names too (e.g. a brand-new
        # `stock` chudware tile); no-ops names that already have cells.
        ensure_cells(list(RECIPES.keys()) + CAM_CELLS)
        done = inject(names)
        contact_sheet(names)
        if not done:
            sys.exit("no icons injected -- check the render step")
        if cam_failures:
            print("[icon_pipeline] CAM TILE FAILURES (file as CAM bugs, do NOT fake):")
            for n, err in cam_failures.items():
                print(f"   {n}: {err}")
            return 1
    elif not args.list:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

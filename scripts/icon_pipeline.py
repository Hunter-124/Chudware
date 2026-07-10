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

T-BL34 (v2): three layers on top of the T-G15 flow --
  * operation-COLOR emphasis: recipes color the op-created body with the app accent
    teal (ui_theme accent #2DD4BF) via set_appearance, so the tile reads "this is what
    the op made" (per-face emphasis is still a renderer follow-up; per-body v1).
  * operation-INTENT annotation: a minimal overlay (arrow / rotation cue / mirror line /
    boolean badge) composited onto the tile in Python post-render (ANNOTATIONS table).
  * Manufacture toolpath tiles: scripts/cam_toolpath_icons.py drives the REAL CAM chain
    (cam_new -> add_tool/setup/operation -> cam_generate -> cam_simulate) and renders the
    generated cutter moves over the stock (Hunter directive 2026-07-04: showcase the
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
    """Locate the host GUI binary (Billet, with AI-CAD fallback for stale build dirs).
    Checks Windows layout first, then Linux/Ninja."""
    base = REPO / "host" / "build-linux" / "bin"
    for c in (base / "Release" / "Billet.exe", base / "Billet.exe",
              base / "Release" / "Billet", base / "Billet",
              base / "Release" / "AI-CAD.exe", base / "AI-CAD.exe",
              base / "Release" / "AI-CAD", base / "AI-CAD"):
        if c.exists():
            return c
    return base / "Billet"  # default (nonexistent -> run_app prints a clear build hint)


BIN = _find_host_bin()
# Canonical atlas the C++ loader injects into (shared assets/icons staging source).
ICON_DIR = REPO / "assets" / "icons"
# Runtime copy the host binary reads; redeploy here after inject so a re-run picks up
# new tiles without a full rebuild.
DEPLOYED_ICON_DIR = BIN.parent / "assets" / "icons"
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


def sphere(radius=22.0, name="Ball1") -> dict:
    """A sphere primitive (engine `sphere` feature, radius param) centred at origin --
    the material-preset "ball" so the Appearance tiles read as rounded swatches."""
    return {"op": "add_feature", "feature": {
        "type": "sphere", "name": name,
        "params": {"radius": dim(radius)}}}


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


# App accent teal (ui_theme.cpp dark accent #2DD4BF; also the icons-contract teal).
ACCENT_RGB = (0.176, 0.831, 0.749)


def accent(body_id: str = "b1") -> dict:
    """Color the op-created body with the selection-accent so the tile highlights
    what the operation produced (T-BL34 per-body emphasis, v1)."""
    return {"op": "set_appearance", "id": body_id, "color": list(ACCENT_RGB),
            "metallic": 0.05, "roughness": 0.45}


def appearance(metallic: float, roughness: float, rgb: tuple[float, float, float]) -> dict:
    """set_appearance on the first body -- the exact op the m1 scenario uses. Values must
    mirror ui::kMaterials (host/src/ui/appearance_common.cpp) so the icon IS the preset."""
    return {"op": "set_appearance", "id": "b1", "color": list(rgb),
            "metallic": metallic, "roughness": roughness}


def sketch(entities: list, plane: str = "plane.front", name: str = "Sk",
           sketch_id: str | None = None) -> list:
    """Returns a LIST of iconpipe steps: one empty add_feature + one sketch_add_entity
    per entity.  The runner applies each step as its own applyOps call; inline
    params.entities is NOT supported by the engine (the sketch is always created empty,
    then entities are added via sketch_add_entity — proven empirically, see T-BL29 log).
    sketch_id is the persistent feature id the engine will assign (e.g. 'f1') — used so
    sketch_add_entity steps reference the right sketch.  If omitted, callers must pass
    the id via the recipe's steps directly."""
    steps: list = [{"op": "add_feature", "feature": {
        "type": "sketch", "name": name,
        "params": {"entities": [], "constraints": []},
        "refs": {"plane": plane}}}]
    if sketch_id:
        for ent in entities:
            steps.append({"op": "sketch_add_entity", "sketch": sketch_id, "entity": ent})
    else:
        # Caller must add sketch_add_entity steps manually with the correct id,
        # or use sketch_with_id() below.
        for ent in entities:
            steps.append({"_sketch_entity_deferred": True, "entity": ent})
    return steps


def sketch_with_id(entities: list, plane: str, name: str, fid: str,
                   offset: float | None = None) -> list:
    """Returns a LIST of iconpipe steps for a sketch at a known persistent feature id.
    Creates the sketch empty, then adds each entity via sketch_add_entity referencing fid.
    Optional offset (mm) shifts the plane along its normal (for loft sections at height)."""
    feat: dict = {
        "type": "sketch", "name": name,
        "params": {"entities": [], "constraints": []},
        "refs": {"plane": plane},
    }
    if offset is not None:
        feat["offset"] = offset
    steps: list = [{"op": "add_feature", "feature": feat}]
    for ent in entities:
        steps.append({"op": "sketch_add_entity", "sketch": fid, "entity": ent})
    return steps


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

    # stock: a rectangular raw billet for the Manufacture > Stock button. A plain
    # machined-aluminium block (bright metal, mid roughness) that reads as "raw
    # stock material". No annotation -- the billet IS the icon.
    "stock": {"yaw": 32, "pitch": 24, "steps": [
        box(56, 40, 26),
        appearance(0.90, 0.32, (0.80, 0.82, 0.85)),   # aluminium billet
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
        # NOTE(T-BL34): merge=False + accent broke the mirror copy (only the seed
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
    # Sheet Metal -> Create  (sm_edge_flange/sm_hem/sm_jog parked -- need
    # resolved edge selector not available in iconpipe runner; see T-BL29.md)
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
    # Appearance -> Material (first tranche already live -- do NOT redo)
    # appr_glass / appr_paint: atlas cells exist but NOT wired to any ribbon button.
    # See work/questions/T-BL29.md Q4 — skipped.
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
    # SDF invisible-icon bug (kFar int32 overflow) fixed in T-G15 (kFar=1<<12).
    # In-context verification via AI_CAD_RIBBON_TAB=Appearance is the right check.
    # Not added here -- photographic renders would override correct line-art glyphs.
    # -----------------------------------------------------------------------
}


# ---------------------------------------------------------------------------------------
# T-BL34: operation-intent annotation overlays. Minimal, one cue per tile, composited in
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
    # stock: no annotation -- the raw billet IS the icon.
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

_TEAL = (45, 212, 191, 255)
_KEYLINE = (8, 34, 30, 210)


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
    W = max(3, sp // 34)          # cue stroke width
    KW = W + max(2, W // 2)       # keyline width

    def P(x, y):
        return (x * sp, y * sp)

    for col, w in ((_KEYLINE, KW), (_TEAL, W)):
        if "arrow" in spec:
            x0, y0, x1, y1 = spec["arrow"]
            _draw_arrow(d, P(x0, y0), P(x1, y1), w, col)
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
                ink = (6, 26, 23, 255)
                if sym == "+":
                    d.line([bx - a, by, bx + a, by], fill=ink, width=sw)
                    d.line([bx, by - a, bx, by + a], fill=ink, width=sw)
                elif sym == "-":
                    d.line([bx - a, by, bx + a, by], fill=ink, width=sw)
                else:  # "n" -> intersection cup
                    d.arc([bx - a, by - a, bx + a, by + a * 1.6],
                          start=180, end=360, fill=ink, width=sw)
                    d.line([bx - a, by, bx - a, by + a * 0.8], fill=ink, width=sw)
                    d.line([bx + a, by, bx + a, by + a * 0.8], fill=ink, width=sw)
    return img.resize((px, px), Image.LANCZOS)


def load_tile(name: str) -> Image.Image | None:
    """Load a rendered tile and composite its operation-intent annotation (if any).
    Single source used by both atlas injection and the contact sheets, so what
    Hunter reviews is exactly what ships."""
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
# T-BL34: byte-preserving atlas extension for NEW cells (the cam_* Manufacture tiles).
# Existing rows/UVs are untouched; new 34px rows are appended at the bottom following the
# exact make_icons.py layout (@32 at x=0,y+1; @20 at x=34,y+7). make_icons.py also knows
# these names (CONTRACT_NAMES + drawers), so a full atlas regen produces the same layout;
# this path just avoids regenerating (and re-eyeballing) all ~190 existing glyph rows.
# ---------------------------------------------------------------------------------------

def ensure_cells(names: list[str]) -> None:
    atlas_path = ICON_DIR / "atlas.json"
    atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
    cells = atlas["cell"]
    missing = [n for n in names if f"{n}@32" not in cells]
    if not missing:
        return
    # placeholder glyphs come from make_icons.py's registered drawers (SDF-style line
    # art) so a cell is never blank even if its toolpath render fails.
    sys.path.insert(0, str(REPO / "host" / "assets"))
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
    row_h = 34
    w, h = atlas["atlas_px"]
    new_h = h + row_h * len(missing)
    for fn in ("atlas_dark.png", "atlas_light.png"):
        im = Image.open(ICON_DIR / fn).convert("RGBA")
        grown = Image.new("RGBA", (w, new_h), (0, 0, 0, 0))
        grown.paste(im, (0, 0))
        for i, name in enumerate(missing):
            y = h + i * row_h
            for x, sz, yoff in ((0, 32, 1), (34, 20, 7)):
                g = placeholder(name, sz, fn)
                grown.paste(g, (x, y + yoff), g)
        grown.save(ICON_DIR / fn)
    for i, name in enumerate(missing):
        y = h + i * row_h
        cells[f"{name}@32"] = {"x": 0, "y": y + 1, "w": 32, "h": 32}
        cells[f"{name}@20"] = {"x": 34, "y": y + 7, "w": 20, "h": 20}
    atlas["atlas_px"] = [w, new_h]
    atlas_path.write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    print(f"[icon_pipeline] atlas extended with {len(missing)} new cell row(s): "
          f"{', '.join(missing)}")


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


def write_recipes(path: Path) -> None:
    recipes = [{"name": n, "yaw": r["yaw"], "pitch": r["pitch"],
                "steps": _flatten_steps(r["steps"])}
               for n, r in RECIPES.items()]
    path.write_text(json.dumps({"out_dir": str(RENDER_DIR), "px": RENDER_PX,
                                "recipes": recipes}, indent=1), encoding="utf-8")


def run_app(recipes_json: Path, theme: str = "dark", timeout_s: int = 300) -> int:
    """Run the host GUI headless (AI_CAD_SHOT=iconpipe) to render every recipe to RENDER_DIR.

    Targets host/build-linux/bin/Billet (ui/imgui / AI-CAD2 is retired — see docs/PORT_LEDGER.md).
    On Linux we wrap in xvfb-run (llvmpipe) for a deterministic, GPU-independent server; on
    Windows the desktop session supplies GL, so the exe runs directly. One invocation drains ALL
    recipes in a single tick.
    """
    if not BIN.exists():
        sys.exit(f"binary not found: {BIN} -- build the host GUI first:\n"
                 f"  cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release\n"
                 f"  ninja -C host/build-linux Billet")
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "AI_CAD_NO_FILE_DIALOGS": "1",
        "AI_CAD_REPO": str(REPO),
        "AI_CAD_SHOT": "iconpipe",
        "AI_CAD_ICON_RECIPES": str(recipes_json),
        "AI_CAD_THEME": theme,
        # Create the GLFW window unmapped so the headless capture-then-exit path
        # runs cleanly under xvfb without ever mapping a visible window.
        "AI_CAD_HIDDEN": "1",
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
    print(f"[icon_pipeline] rendering {len(RECIPES)} recipes via {BIN.name} (theme={theme}) ...")
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


def inject(names: list[str]) -> list[str]:
    """Composite each rendered tile into the @32/@20 cells of both atlases (in place).
    Only the named cells are overwritten; every other icon is preserved byte-for-byte."""
    atlas = json.loads((ICON_DIR / "atlas.json").read_text(encoding="utf-8"))
    cells = atlas["cell"]
    done: list[str] = []
    atlases = {fn: Image.open(ICON_DIR / fn).convert("RGBA")
               for fn in ("atlas_dark.png", "atlas_light.png")}
    for name in names:
        render = load_tile(name)  # annotated tile (T-BL34) or None if not rendered
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
    (ICON_DIR / "atlas.json").write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    # Deploy to the built app so a verification run picks the new art up immediately.
    if DEPLOYED_ICON_DIR.exists():
        for fn in list(atlases) + ["atlas.json"]:
            shutil.copy2(ICON_DIR / fn, DEPLOYED_ICON_DIR / fn)
    print(f"[icon_pipeline] injected {len(done)} icon(s): {', '.join(done)}")
    return done


def contact_sheet(names: list[str]) -> None:
    """Light + dark review sheets of every tile, at review size AND true 32px ribbon
    size, using load_tile so annotations/coloring are exactly what ships (T-BL34)."""
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
    ap.add_argument("--theme", default="dark", help="AI_CAD_THEME for the render run")
    args = ap.parse_args(argv)

    if args.list:
        for n, r in RECIPES.items():
            print(f"{n:16s} yaw={r['yaw']:>3} pitch={r['pitch']:>3} steps={len(r['steps'])}")
        return 0

    from cam_toolpath_icons import CAM_CELLS, render_all as cam_render_all

    names = list(RECIPES.keys()) + CAM_CELLS
    cam_failures: dict[str, str] = {}
    if args.render:
        recipes_json = REPO / "scratch" / "icon_recipes.json"
        recipes_json.parent.mkdir(parents=True, exist_ok=True)
        write_recipes(recipes_json)
        rc = run_app(recipes_json, theme=args.theme)
        print(f"[icon_pipeline] app exit={rc}")
        # T-BL34: Manufacture tiles from the REAL CAM chain (also a CAM smoke test)
        _, cam_failures = cam_render_all()

    if args.render or args.inject_only:
        # Append any missing atlas cells for RECIPE names too (e.g. a brand-new
        # `stock` billet tile); no-ops names that already have cells.
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

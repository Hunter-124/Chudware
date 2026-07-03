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


RECIPES: dict[str, dict] = {
    # Features -> 3D Body: a plain solid block reads as "create a 3D body / boss".
    "extrude_boss": {"yaw": 35, "pitch": 26, "steps": [box(52, 40, 34)]},

    # Features -> Dress-up: fillet / chamfer / shell on the same block, modified edges/face
    # front-and-centre so the tool's effect is legible at icon size.
    "fillet": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "fillet", "name": "Fillet1",
                     "params": {"radius": dim(7.0)}},
         "selrefs": {"edges": {"select": "vertical_edges", "n": 4}}}]},

    "chamfer": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "chamfer", "name": "Chamfer1",
                     "params": {"length": dim(6.0)}},  # chamfer's canonical param is "length"
         "selrefs": {"edges": {"select": "vertical_edges", "n": 4}}}]},

    "shell": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "shell", "name": "Shell1",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}}]},

    # Appearance -> Material: REAL renders of the actual presets (DoD: "Material preset icons
    # are real renders of each preset"). appr_metal is the shared glyph behind the four metal
    # buttons (the ribbon overlays each button's own preset RGB via ribbon_matSwatchColor), so
    # it renders the flagship Aluminum preset. Values mirror ui::kMaterials exactly.
    "appr_metal":   {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26), appearance(0.90, 0.35, (0.83, 0.85, 0.88))]},   # Aluminum
    "appr_plastic": {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26), appearance(0.00, 0.50, (0.71, 0.76, 0.82))]},   # Plastic
    "appr_default": {"yaw": 33, "pitch": 26, "steps": [
        box(48, 40, 26)]},                                               # engine default grey
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

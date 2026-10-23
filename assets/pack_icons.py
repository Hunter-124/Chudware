"""Pack AI-generated icon PNGs into the Chudware atlases.

This is the AI-icon counterpart to the legacy procedural `make_icons.py`. Instead of
drawing glyphs, it composites pre-rendered source PNGs (one per icon name, generated
from per-icon image prompts) into the two atlases the C++ loader consumes.

Source:  assets/icons/src/<name>.png   (1024x1024-ish, transparent alpha, square)
Outputs: assets/icons/atlas_dark.png, atlas_light.png   (pixel-identical)
Layout:  REUSED verbatim from the existing assets/icons/atlas.json, so every icon's
         UVs stay put and `host/src/ui/icons.cpp` needs no change. atlas.json is left
         untouched when writing in place (only copied when packing to a different dir).

Why both atlases are identical: AI renders are full-colour, so they do not tint per theme
the way the old monochrome line-art did. The loader still swaps textures on theme change;
both just hold the same art.

Usage:
  python assets/pack_icons.py                 # pack src/ -> icons/ in place
  python assets/pack_icons.py --check         # report which source PNGs are missing
  python assets/pack_icons.py --src DIR --out DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE / "icons"


def _names_from_atlas(atlas: dict) -> list[str]:
    return sorted({k.split("@")[0] for k in atlas["cell"]})


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Resize `img` to fit w x h, preserving aspect ratio, centred on transparency."""
    img = img.convert("RGBA")
    sw, sh = img.size
    if sw == 0 or sh == 0:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    scale = min(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    cell = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cell.alpha_composite(resized, ((w - nw) // 2, (h - nh) // 2))
    return cell


def missing_sources(src: Path, atlas: dict) -> list[str]:
    return [n for n in _names_from_atlas(atlas) if not (src / f"{n}.png").exists()]


def pack(src: Path, out: Path, atlas_json: Path) -> None:
    atlas = json.loads(atlas_json.read_text(encoding="utf-8"))
    aw, ah = atlas["atlas_px"]
    names = _names_from_atlas(atlas)

    missing = missing_sources(src, atlas)
    print(f"sources: {len(names) - len(missing)}/{len(names)} present in {src}")
    if missing:
        print("MISSING (" + str(len(missing)) + "): " + ", ".join(missing))
        sys.exit(f"refusing to pack: {len(missing)} source PNG(s) missing")

    canvas = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    cache: dict[str, Image.Image] = {}
    for key, c in atlas["cell"].items():
        name = key.split("@")[0]
        if name not in cache:
            cache[name] = Image.open(src / f"{name}.png").convert("RGBA")
        cell = _fit(cache[name], c["w"], c["h"])
        canvas.alpha_composite(cell, (c["x"], c["y"]))

    out.mkdir(parents=True, exist_ok=True)
    for fn in ("atlas_dark.png", "atlas_light.png"):
        canvas.save(out / fn)
    # Preserve atlas.json byte-for-byte in place; only emit a copy when packing elsewhere.
    if out.resolve() != atlas_json.parent.resolve():
        (out / "atlas.json").write_text(atlas_json.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {out/'atlas_dark.png'} and atlas_light.png  ({aw}x{ah}, {len(names)} icons)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack AI icon PNGs into the Chudware atlases.")
    ap.add_argument("--src", type=Path, default=ICON_DIR / "src", help="source PNG dir")
    ap.add_argument("--out", type=Path, default=ICON_DIR, help="output dir for atlases")
    ap.add_argument("--atlas", type=Path, default=ICON_DIR / "atlas.json", help="layout json")
    ap.add_argument("--check", action="store_true", help="only report missing sources")
    args = ap.parse_args(argv)

    atlas = json.loads(args.atlas.read_text(encoding="utf-8"))
    if args.check:
        miss = missing_sources(args.src, atlas)
        names = _names_from_atlas(atlas)
        print(f"{len(names) - len(miss)}/{len(names)} present")
        if miss:
            print("MISSING (" + str(len(miss)) + "): " + ", ".join(miss))
            return 1
        print("all sources present")
        return 0

    pack(args.src, args.out, args.atlas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

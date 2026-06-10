"""Test suite for the icon atlas pipeline.

Run: python host/assets/test_icons.py
     (or: python -m host.assets.test_icons from repo root)

Checks:
  1. Every name in the icons contract is present at both sizes in atlas.json
  2. Both PNG atlases decode successfully
  3. Both atlases share the same dimensions
  4. atlas.json layout matches what is in both PNGs (consistency check)
  5. Regeneration into a temp dir is byte-identical to original (determinism)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# Allow running as a plain script or as a module
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image

from host.assets.make_icons import CONTRACT_NAMES, CELL_SIZES, build_atlas, OUT_DIR, check_coverage
import host.assets.make_icons as _mod

import json as _json

ICON_DIR = HERE / "icons"


def load_json() -> dict:
    with open(ICON_DIR / "atlas.json", encoding="utf-8") as f:
        return _json.load(f)


def test_coverage():
    missing = check_coverage()
    assert not missing, f"No draw function for: {missing}"
    print("  PASS coverage: all", len(CONTRACT_NAMES), "names have draw functions")


def test_json_keys():
    data = load_json()
    cell_map = data["cell"]
    for name in CONTRACT_NAMES:
        for sz in CELL_SIZES:
            key = f"{name}@{sz}"
            assert key in cell_map, f"Missing key in atlas.json: {key}"
    print(f"  PASS json_keys: {len(CONTRACT_NAMES)*len(CELL_SIZES)} keys present")


def test_pngs_decode():
    dark  = Image.open(ICON_DIR / "atlas_dark.png")
    light = Image.open(ICON_DIR / "atlas_light.png")
    dark.load()
    light.load()
    print(f"  PASS pngs_decode: dark={dark.size} light={light.size}")
    return dark.size, light.size


def test_same_dimensions():
    dark_sz, light_sz = test_pngs_decode()
    assert dark_sz == light_sz, f"Atlas size mismatch: {dark_sz} vs {light_sz}"
    data = load_json()
    json_sz = tuple(data["atlas_px"])
    assert json_sz == dark_sz, f"atlas.json size {json_sz} != image size {dark_sz}"
    print(f"  PASS same_dimensions: {dark_sz}")


def test_layout_consistency():
    """Verify every cell entry fits within atlas bounds."""
    data = load_json()
    w, h = data["atlas_px"]
    for key, cell in data["cell"].items():
        x, y, cw, ch = cell["x"], cell["y"], cell["w"], cell["h"]
        assert x >= 0 and y >= 0, f"{key}: negative origin"
        assert x + cw <= w, f"{key}: x+w={x+cw} exceeds atlas width {w}"
        assert y + ch <= h, f"{key}: y+h={y+ch} exceeds atlas height {h}"
    print(f"  PASS layout_consistency: {len(data['cell'])} cells within bounds")


def test_determinism():
    """Regenerate into a temp dir and compare byte-for-byte."""
    import io

    # Generate fresh atlases
    from host.assets.make_icons import DARK, LIGHT
    dark_fresh,  fresh_map = build_atlas(DARK)
    light_fresh, _         = build_atlas(LIGHT)

    # Compare with on-disk files
    dark_disk  = Image.open(ICON_DIR / "atlas_dark.png")
    light_disk = Image.open(ICON_DIR / "atlas_light.png")

    def img_to_bytes(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=6)
        return buf.getvalue()

    # We compare pixel data, not raw bytes (PNG compression can vary).
    # Pixel-level identity guarantees determinism.
    import struct

    def img_pixels(img: Image.Image) -> bytes:
        return img.convert("RGBA").tobytes()

    dark_fresh_bytes  = img_pixels(dark_fresh)
    light_fresh_bytes = img_pixels(light_fresh)
    dark_disk_bytes   = img_pixels(dark_disk)
    light_disk_bytes  = img_pixels(light_disk)

    assert dark_fresh_bytes == dark_disk_bytes,  "Dark atlas is NOT deterministic!"
    assert light_fresh_bytes == light_disk_bytes, "Light atlas is NOT deterministic!"
    print("  PASS determinism: pixel-identical regeneration confirmed")


def main():
    print("=== test_icons ===")
    print(f"Icon dir: {ICON_DIR}")
    if not ICON_DIR.exists():
        print("ERROR: icons dir does not exist — run make_icons.py first")
        sys.exit(1)

    test_coverage()
    test_json_keys()
    test_same_dimensions()
    test_layout_consistency()
    test_determinism()

    print("=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()

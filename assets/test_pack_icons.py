"""Tests for the AI-icon atlas compositor (host/assets/pack_icons.py).

Synthesises a full set of placeholder source PNGs, packs them into a temp dir, and asserts
the atlas matches the committed layout (atlas.json) and the C++ loader's expectations:
both atlases pixel-identical, correct dimensions, every cell filled, refuses on a missing
source. Never touches the committed atlas_*.png.

Run:  python host/assets/test_pack_icons.py   (or pytest)
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE / "icons"

_spec = importlib.util.spec_from_file_location("pack_icons", HERE / "pack_icons.py")
pack_icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_icons)


def _atlas() -> dict:
    return json.loads((ICON_DIR / "atlas.json").read_text(encoding="utf-8"))


def _names(atlas: dict) -> list[str]:
    return sorted({k.split("@")[0] for k in atlas["cell"]})


def _synth_sources(src: Path, names: list[str]) -> None:
    """One placeholder PNG per icon: transparent bg + a coloured disc (varied aspect)."""
    src.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        w, h = (1024, 1024) if i % 3 else (820, 1024)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        col = tuple(hashlib.md5(n.encode()).digest()[:3]) + (255,)
        ImageDraw.Draw(im).ellipse([w * 0.12, h * 0.12, w * 0.88, h * 0.88], fill=col)
        im.save(src / f"{n}.png")


def test_pack_complete_atlas():
    atlas = _atlas()
    names = _names(atlas)
    aw, ah = atlas["atlas_px"]
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "src", Path(d) / "out"
        _synth_sources(src, names)
        assert pack_icons.missing_sources(src, atlas) == []
        pack_icons.pack(src, out, ICON_DIR / "atlas.json")

        dark = Image.open(out / "atlas_dark.png")
        light = Image.open(out / "atlas_light.png")
        assert dark.size == (aw, ah) == light.size, (dark.size, light.size, (aw, ah))
        assert dark.mode == "RGBA"
        assert dark.tobytes() == light.tobytes(), "dark/light atlases must be pixel-identical"

        empty = []
        for key, c in atlas["cell"].items():
            region = dark.crop((c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]))
            if region.getextrema()[3][1] == 0:  # max alpha == 0 -> fully transparent
                empty.append(key)
        assert not empty, f"empty cells: {empty}"
    print(f"  PASS pack_complete_atlas: {len(names)} icons -> {aw}x{ah}, all cells filled")


def test_refuse_on_missing_source():
    atlas = _atlas()
    names = _names(atlas)
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "src", Path(d) / "out"
        _synth_sources(src, names)
        os.remove(src / f"{names[0]}.png")
        assert pack_icons.missing_sources(src, atlas) == [names[0]]
        raised = False
        try:
            pack_icons.pack(src, out, ICON_DIR / "atlas.json")
        except SystemExit:
            raised = True
        assert raised, "pack() must refuse (SystemExit) when a source PNG is missing"
    print("  PASS refuse_on_missing_source")


def test_committed_atlases_untouched():
    """The compositor test must never write into the real icon dir."""
    before = (ICON_DIR / "atlas_dark.png").stat().st_mtime_ns
    test_pack_complete_atlas()
    after = (ICON_DIR / "atlas_dark.png").stat().st_mtime_ns
    assert before == after, "committed atlas_dark.png must not be modified by the test"
    print("  PASS committed_atlases_untouched")


def main():
    print("=== test_pack_icons ===")
    test_pack_complete_atlas()
    test_refuse_on_missing_source()
    test_committed_atlases_untouched()
    print("=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()

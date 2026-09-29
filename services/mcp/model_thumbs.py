"""Real thumbnails of CAD files, and labelled contact sheets a model can read at a glance.

A thumbnail is the engine's clean product shot — ``doc_open`` (``internal``: hostd never
announces it, so no GUI tab appears) → ``doc_render_png(annotate=False)`` → ``doc_close`` —
exactly what the GUI's PLM grid renders, written to the same cache
``<project>/.plm/thumbs/<content_key>.png`` (``content_key`` = blake2b-8 of document.json,
the graph's ``meta.dh``). Either side's render is the other's cache hit, and an edited file
keys a new image. Files outside a project cache under the app data dir.

A contact sheet tiles up to ``SHEET_TILES`` thumbnails (auto-trimmed to the part) with a
numbered badge and a caption — name, size, material, contents, warnings — so one image of
~1,500 tokens replaces a page of text and shows the parts as they actually look.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .model_card import EXCHANGE_3D_FORMATS, contents_text, dims_text

SHEET_COLS = 4
SHEET_ROWS = 3
SHEET_TILES = SHEET_COLS * SHEET_ROWS
_TILE_W, _IMAGE_H, _CAPTION_H = 320, 224, 62
_GAP, _MARGIN = 8, 8
_ENGINE_BG = (245, 245, 245)
_TILE_BG = (250, 250, 251)
_BORDER = (214, 217, 222)
_INK = (36, 40, 48)
_MUTED = (98, 104, 116)
_WARN = (196, 48, 36)
_BADGE = (40, 44, 56)
_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_RENDERABLE = ("step+",) + EXCHANGE_3D_FORMATS
#: An exchange file imports in full to be rendered; past this its thumbnail is skipped.
_MAX_ENGINE_IMPORT_KB = 64 * 1024


# --------------------------------------------------------------------------- thumbnails


def thumb_path(card: dict[str, Any]) -> Optional[Path]:
    """Cache location of *card*'s thumbnail (None when it has no content key)."""
    key = card.get("content_key")
    if not key:
        return None
    return _thumb_dir(Path(card["file"])) / f"{key}.png"


def _thumb_dir(file: Path) -> Path:
    for env in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        root = os.environ.get(env)
        if not root:
            continue
        try:
            file.resolve().relative_to(Path(root).resolve())
        except (OSError, ValueError):
            continue
        return Path(root).resolve() / ".plm" / "thumbs"
    from services import paths

    return Path(paths.ROOT) / "cache" / "model_thumbs"


def ensure_thumbnail(card: dict[str, Any], *, deadline: Optional[float] = None) -> dict[str, Any]:
    """``{status, path?, source?, reason?}`` for one card: ``cached`` | ``rendered`` |
    ``pending`` (the call's time budget ran out first) | ``unavailable``.

    The file's own display mesh (``cache.aicm`` / STL triangles) renders first — no
    document session, no engine call, the engine's camera and look (mesh_preview). A
    ``.step+`` saved without a mesh (a reference assembly) goes to the engine, which
    resolves its components."""
    if card.get("format") not in _RENDERABLE or card.get("error"):
        return {"status": "unavailable", "reason": _no_render_reason(card)}
    target = thumb_path(card)
    if target is None:
        return {"status": "unavailable", "reason": "no content to key a thumbnail on"}
    if target.is_file() and target.stat().st_size > 0:
        return {"status": "cached", "path": str(target)}
    if deadline is not None and time.monotonic() >= deadline:
        return {"status": "pending", "reason": "render budget used up — call again"}
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.stem}.{os.getpid()}.part.png")
    errors = []
    for source, render in (("mesh", _render_mesh), ("engine", _render_engine)):
        if source == "mesh" and card.get("format") not in ("step+", "stl"):
            continue
        if source == "engine" and card.get("size_kb", 0) > _MAX_ENGINE_IMPORT_KB \
                and card.get("format") != "step+":
            errors.append(f"engine: {card['size_kb'] / 1024:.0f} MB is too large to import "
                          "for a thumbnail")
            continue
        try:
            render(card, staging)
        except Exception as exc:  # noqa: BLE001 - try the next renderer, then report
            staging.unlink(missing_ok=True)
            errors.append(f"{source}: {exc}")
            continue
        measured = card.pop("_measured", None)
        if measured:
            target.with_suffix(".json").write_text(json.dumps(measured), encoding="utf-8")
        os.replace(staging, target)
        return {"status": "rendered", "path": str(target), "source": source}
    return {"status": "unavailable", "reason": ("render failed — " + "; ".join(errors))[:160]}


def _no_render_reason(card: dict[str, Any]) -> str:
    if card.get("error"):
        return "unreadable file"
    return f"{str(card.get('format')).upper()} has no 3D preview"


def _render_mesh(card: dict[str, Any], out: Path) -> None:
    import zipfile

    import numpy as np

    from .mesh_preview import DEFAULT_COLOR, body_mesh, render_mesh_png

    if card["format"] == "stl":
        from .model_card import stl_vertices

        verts = stl_vertices(Path(card["file"]))
        if verts is None or len(verts) < 3:
            raise ValueError("no triangles")
        tris = np.arange(len(verts) // 3 * 3, dtype=np.int64).reshape(-1, 3)
        colors = np.tile(np.asarray(DEFAULT_COLOR), (len(tris), 1))
        render_mesh_png(verts, tris, colors, out)
        return
    from services.sim.tessellate import read_aicm

    with zipfile.ZipFile(card["file"]) as zf:
        if "cache.aicm" not in zf.namelist():
            raise ValueError("no cached mesh in the file")
        raw = zf.read("cache.aicm")
    render_mesh_png(*body_mesh(read_aicm(raw).get("bodies") or []), out)


def _render_engine(card: dict[str, Any], out: Path) -> None:
    """Open (silently), render the clean product shot, measure, close.

    An exchange file's card has no geometry of its own (a STEP header is not a size), so
    the bbox measured while the engine holds it is written beside the thumbnail
    (``<key>.json``) and captions the tile from then on."""
    from .cad_common import cb

    opened = cb.call("doc_open", {"path": card["file"], "internal": True}) or {}
    doc_id = opened.get("doc_id")
    if not doc_id:
        raise RuntimeError(opened.get("error") or "doc_open returned no doc_id")
    measured: dict[str, Any] = {}
    try:
        result = cb.call("doc_render_png", {"doc_id": doc_id, "out_path": str(out),
                                            "annotate": False}) or {}
        try:
            box = cb.call("doc_bbox", {"doc_id": doc_id}) or {}
            lo, hi = box.get("min"), box.get("max")
            if isinstance(lo, list) and isinstance(hi, list) and len(lo) == len(hi) == 3:
                measured = {"bbox_mm": [round(b - a, 2) for a, b in zip(lo, hi)],
                            "bbox_min_mm": [round(a, 2) for a in lo]}
        except Exception:  # noqa: BLE001 - the picture is the point; the size is a bonus
            measured = {}
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc_id})
        except Exception:  # noqa: BLE001 - the render already happened or failed on its own
            pass
    if not result.get("ok") or not out.is_file():
        raise RuntimeError(result.get("error") or result.get("reason") or "no image")
    if measured:
        card["_measured"] = measured


def measured_fields(card: dict[str, Any]) -> dict[str, Any]:
    """Engine-measured facts saved beside *card*'s thumbnail (empty when none)."""
    target = thumb_path(card)
    if target is None:
        return {}
    try:
        return json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- sheets


def contact_sheets(tiles: list[dict[str, Any]], out_dir: Path) -> list[str]:
    """Write one PNG per ``SHEET_TILES`` tiles; returns their paths.

    Each tile: ``{"n", "label", "card", "thumb": path|None, "reason"}``. Sheets are keyed
    by their content, so an unchanged listing reuses the file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for start in range(0, len(tiles), SHEET_TILES):
        chunk = tiles[start:start + SHEET_TILES]
        digest = hashlib.blake2b(digest_size=10)
        for tile in chunk:
            digest.update(repr((tile["n"], tile["label"], tile.get("thumb"),
                                _caption(tile), tile.get("reason"))).encode())
        target = out_dir / f"sheet-{digest.hexdigest()}.png"
        if not target.is_file():
            _draw_sheet(chunk, target)
        paths.append(str(target))
    return paths


def single_tile(tile: dict[str, Any], out_dir: Path) -> str:
    """A one-tile sheet (a labelled thumbnail) for previewing one file."""
    return contact_sheets([tile], out_dir)[0]


def sheet_dir() -> Path:
    """Where contact sheets are written (app cache; content-keyed, safe to delete)."""
    from services import paths

    return Path(paths.ROOT) / "cache" / "model_sheets"


def _draw_sheet(tiles: list[dict[str, Any]], target: Path) -> None:
    from PIL import Image, ImageDraw

    cols = min(SHEET_COLS, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    tile_h = _IMAGE_H + _CAPTION_H
    width = 2 * _MARGIN + cols * _TILE_W + (cols - 1) * _GAP
    height = 2 * _MARGIN + rows * tile_h + (rows - 1) * _GAP
    sheet = Image.new("RGB", (width, height), (236, 238, 241))
    draw = ImageDraw.Draw(sheet)
    fonts = {"title": _font("Rubik-SemiBold.ttf", 15), "body": _font("Rubik-Regular.ttf", 13),
             "badge": _font("Rubik-Bold.ttf", 14), "empty": _font("Rubik-Medium.ttf", 13)}
    for index, tile in enumerate(tiles):
        x = _MARGIN + (index % cols) * (_TILE_W + _GAP)
        y = _MARGIN + (index // cols) * (tile_h + _GAP)
        draw.rectangle((x, y, x + _TILE_W - 1, y + tile_h - 1), fill=_TILE_BG, outline=_BORDER)
        _paste_thumb(sheet, draw, tile, x, y, fonts)
        _draw_caption(draw, tile, x, y + _IMAGE_H, fonts)
        _badge(draw, f"#{tile['n']}", x + 6, y + 6, fonts["badge"], _BADGE)
        failing = tile["card"].get("failing_features")
        if failing:
            label = f"! {len(failing)} failing"
            w = draw.textlength(label, font=fonts["badge"])
            _badge(draw, label, x + _TILE_W - 6 - w - 12, y + 6, fonts["badge"], _WARN)
    staging = target.with_name(f".{target.name}.{os.getpid()}.part")
    sheet.save(staging, format="PNG", optimize=True)
    os.replace(staging, target)


def _paste_thumb(sheet, draw, tile, x, y, fonts) -> None:
    from PIL import Image

    box_w, box_h = _TILE_W - 12, _IMAGE_H - 8
    thumb = tile.get("thumb")
    if thumb:
        try:
            with Image.open(thumb) as source:
                image = _trim(source.convert("RGB"))
            scale = min(box_w / image.width, box_h / image.height)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(size, Image.Resampling.LANCZOS)
            sheet.paste(image, (x + 6 + (box_w - size[0]) // 2, y + 6 + (box_h - size[1]) // 2))
            return
        except OSError as exc:
            tile["reason"] = f"thumbnail unreadable: {exc}"
    reason = tile.get("reason") or "no preview"
    cx, cy = x + _TILE_W // 2, y + _IMAGE_H // 2
    for offset, line in ((-10, "no preview"), (10, reason[:44])):
        w = draw.textlength(line, font=fonts["empty"])
        draw.text((cx - w / 2, cy + offset - 8), line, font=fonts["empty"], fill=_MUTED)


def _trim(image):
    """Crop the engine's flat background away (with a small margin) so the part fills its tile."""
    import numpy as np

    pixels = np.asarray(image, dtype=np.int16)
    mask = np.abs(pixels - np.array(_ENGINE_BG, dtype=np.int16)).max(axis=2) > 10
    if not mask.any():
        return image
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    pad = max(4, round(0.04 * max(image.width, image.height)))
    box = (max(0, cols[0] - pad), max(0, rows[0] - pad),
           min(image.width, cols[-1] + 1 + pad), min(image.height, rows[-1] + 1 + pad))
    return image.crop(box)


def _caption(tile: dict[str, Any]) -> list[tuple[str, tuple[int, int, int]]]:
    card = tile["card"]
    line2 = " · ".join(t for t in (
        card.get("kind") or str(card.get("format", "")).upper(),
        dims_text(card),
        card.get("material") or "/".join(card.get("body_materials") or [])[:24] or None,
    ) if t)
    extras = [contents_text(card)]
    if card.get("volume_cm3"):
        extras.append(f"{card['volume_cm3']:g} cm³")
    if card.get("revision") is not None:
        extras.append(f"rev {card['revision']}")
    line3 = " · ".join(t for t in extras if t)
    return [(line2, _MUTED), (line3, _MUTED)]


def _draw_caption(draw, tile, x, y, fonts) -> None:
    draw.line((x + 1, y, x + _TILE_W - 2, y), fill=_BORDER)
    left, width = x + 10, _TILE_W - 20
    # The file name, not its folder path: a path truncates to its (shared) prefix. The
    # text index beside the sheet carries the full path under the same tile number.
    draw.text((left, y + 6), _fit(draw, Path(tile["label"]).name, fonts["title"], width),
              font=fonts["title"], fill=_INK)
    for row, (text, color) in enumerate(_caption(tile)):
        if text:
            draw.text((left, y + 26 + row * 17), _fit(draw, text, fonts["body"], width),
                      font=fonts["body"], fill=color)


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _badge(draw, text: str, x: float, y: float, font, fill) -> None:
    w = draw.textlength(text, font=font)
    draw.rounded_rectangle((x, y, x + w + 12, y + 21), radius=5, fill=fill)
    draw.text((x + 6, y + 2), text, font=font, fill=(255, 255, 255))


def _font(name: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(_FONTS / name), size)
    except OSError:
        return ImageFont.load_default(size=size)


def build_tiles(entries: Iterable[tuple[str, dict[str, Any]]], *, render: bool,
                deadline: Optional[float]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Tiles for ``(label, card)`` pairs, rendering missing thumbnails until *deadline*."""
    tiles: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for n, (label, card) in enumerate(entries, start=1):
        if render:
            state = ensure_thumbnail(card, deadline=deadline)
        else:
            path = thumb_path(card)
            state = ({"status": "cached", "path": str(path)} if path and path.is_file()
                     else {"status": "pending", "reason": "not rendered yet"})
        counts[state["status"]] = counts.get(state["status"], 0) + 1
        if state.get("path") and not card.get("bbox_mm"):
            card = {**card, **measured_fields(card)}
        tiles.append({"n": n, "label": label, "card": card, "thumb": state.get("path"),
                      "status": state["status"], "reason": state.get("reason")})
    return tiles, counts

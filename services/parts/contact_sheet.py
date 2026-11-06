"""Labeled, honest part previews for agent search results."""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

_TILE_WIDTH = 384
_TILE_HEIGHT = 360
_MAX_TILES = 16
_FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "Rubik-Regular.ttf"
_BOLD_FONT = _FONT.with_name("Rubik-SemiBold.ttf")
_PROVENANCE = {"cad_render": "CAD render", "product_image": "Product photo"}


def _font(size: int, *, bold: bool = False):
    try:
        return ImageFont.truetype(str(_BOLD_FONT if bold else _FONT), size)
    except OSError:
        return ImageFont.load_default()


def _fit_text(draw, text: str, font, width: int) -> str:
    text = " ".join(str(text or "").split())
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap_title(draw, title: str, font, width: int) -> list[str]:
    words = " ".join(str(title or "").split()).split(" ")
    lines: list[str] = []
    current = ""
    for pos, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if not current:
            current = _fit_text(draw, word, font, width)
        lines.append(current)
        if len(lines) == 2:
            if pos < len(words):
                lines[-1] = _fit_text(draw, lines[-1].rstrip("…") + " …", font, width)
            return lines
        current = word
    if current and len(lines) < 2:
        lines.append(_fit_text(draw, current, font, width))
    return lines


def _decoded_preview(data: bytes | None):
    if not isinstance(data, bytes) or not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None


def compose_contact_sheet(tiles: list[dict], *, cols: int = 4) -> bytes:
    """Return a PNG of up to 16 ordered tiles, including explicit missing previews."""
    if not tiles:
        raise ValueError("a contact sheet needs at least one tile")
    if len(tiles) > _MAX_TILES:
        raise ValueError("a contact sheet can show at most 16 tiles")
    cols = max(math.ceil(len(tiles) / 4), min(max(1, int(cols)), 4, len(tiles)))
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (_TILE_WIDTH * cols, _TILE_HEIGHT * rows), "#e6e9ed")
    title_font, small_font = _font(21, bold=True), _font(16)
    index_font, missing_font = _font(25, bold=True), _font(22)
    for position, tile in enumerate(tiles):
        x = (position % cols) * _TILE_WIDTH
        y = (position // cols) * _TILE_HEIGHT
        draw = ImageDraw.Draw(sheet)
        draw.rectangle((x + 2, y + 2, x + _TILE_WIDTH - 3, y + _TILE_HEIGHT - 3),
                       fill="#ffffff", outline="#c7cdd5", width=2)
        draw.text((x + 14, y + 11), f"#{tile.get('index', position)}",
                  font=index_font, fill="#142236")
        for line, text in enumerate(_wrap_title(draw, tile.get("title") or "Untitled part",
                                                 title_font, _TILE_WIDTH - 92)):
            draw.text((x + 76, y + 12 + line * 25), text, font=title_font, fill="#142236")
        detail = " · ".join(str(s) for s in (tile.get("vendor"), tile.get("designation")) if s)
        draw.text((x + 15, y + 67), _fit_text(draw, detail, small_font, _TILE_WIDTH - 30),
                  font=small_font, fill="#45556a")
        box = (x + 12, y + 94, x + _TILE_WIDTH - 12, y + 325)
        draw.rectangle(box, fill="#f3f5f8", outline="#d9dee5")
        image = _decoded_preview(tile.get("image"))
        if image is None:
            text = "No preview"
            bounds = draw.textbbox((0, 0), text, font=missing_font)
            draw.text((x + (_TILE_WIDTH - (bounds[2] - bounds[0])) / 2, y + 196),
                      text, font=missing_font, fill="#606c7a")
        else:
            image.thumbnail((_TILE_WIDTH - 32, 223), Image.Resampling.LANCZOS)
            ix = x + (_TILE_WIDTH - image.width) // 2
            iy = y + 98 + (223 - image.height) // 2
            sheet.paste(image, (ix, iy), image)
        tag = _PROVENANCE.get(str(tile.get("provenance") or ""), "") if image else ""
        if tag:
            draw.text((x + 15, y + 333), tag, font=small_font, fill="#45556a")
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


def subset_contact_sheet(data: bytes, pictured: list[int], selected: list[int]) -> bytes:
    """Rearrange existing tiles after a transport drops rows, without fetching again."""
    if not selected or len(pictured) > _MAX_TILES or len(set(pictured)) != len(pictured):
        raise ValueError("invalid contact sheet selection")
    positions = {index: position for position, index in enumerate(pictured)}
    if any(index not in positions for index in selected):
        raise ValueError("selected row was not pictured")
    old_cols = min(4, len(pictured))
    cols = min(4, len(selected))
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        if source.size != (_TILE_WIDTH * old_cols, _TILE_HEIGHT * math.ceil(len(pictured) / old_cols)):
            raise ValueError("unexpected contact sheet dimensions")
        sheet = Image.new("RGB", (_TILE_WIDTH * cols, _TILE_HEIGHT * math.ceil(len(selected) / cols)),
                          "#e6e9ed")
        for position, index in enumerate(selected):
            original = positions[index]
            x, y = (original % old_cols) * _TILE_WIDTH, (original // old_cols) * _TILE_HEIGHT
            tile = source.crop((x, y, x + _TILE_WIDTH, y + _TILE_HEIGHT))
            sheet.paste(tile, ((position % cols) * _TILE_WIDTH, (position // cols) * _TILE_HEIGHT))
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


def present_search(payload: dict, *, previews: bool = True, allow_network: bool = True,
                   budget_s: float = 8.0) -> tuple[dict, bytes | None]:
    """Resolve only the already-budgeted rows; never acquire a model for a preview."""
    out = dict(payload)
    rows = []
    for position, row in enumerate(out.get("results", [])):
        if isinstance(row, dict):
            rows.append({"index": row.get("index", position), "part_id": row.get("part_id"),
                         "preview": {"provenance": "", "reason": "not_pictured"},
                         **{k: v for k, v in row.items() if k not in ("index", "part_id", "preview")}})
    out["results"] = rows
    indices = [r["index"] for r in rows]
    out["preview_sheet"] = {"pictured": [], "omitted": indices, "reason": ""}
    if not previews:
        out["preview_sheet"]["reason"] = "previews_disabled"
        return out, None
    if not rows:
        out["preview_sheet"]["reason"] = "no_results"
        return out, None

    selected = rows[:_MAX_TILES]
    requests = [{"item_id": str(r.get("part_id") or ""),
                 "image_url": str(r.get("thumbnail_url") or r.get("thumbnail") or
                                  (r.get("params") or {}).get("thumbnail") or "")}
                for r in selected]
    try:
        from services.parts.thumbs import resolve_previews
        resolved = resolve_previews(requests, budget_s=budget_s, allow_network=allow_network)
    except Exception as exc:  # noqa: BLE001 — search remains useful without previews
        resolved = [{"ok": False, "reason": f"preview_unavailable: {exc}"} for _ in selected]

    tiles = []
    for position, row in enumerate(selected):
        preview = resolved[position] if position < len(resolved) and isinstance(resolved[position], dict) else {}
        provenance = str(preview.get("provenance") or "")
        image = preview.get("data") if preview.get("ok") and provenance in _PROVENANCE else None
        if image is not None and _decoded_preview(image) is None:
            image = None
            reason = "invalid_preview"
        elif image is None:
            reason = str(preview.get("reason") or "no_preview")
        else:
            reason = ""
        row["preview"] = {"provenance": provenance if image else "", "reason": reason}
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        vendor = row.get("vendor") or ""
        if not vendor and str(row.get("part_id") or "").startswith("catalog:"):
            vendor = str(row["part_id"])[len("catalog:"):].split("::", 1)[0]
        if not vendor:
            vendor = next((entry.get("vendor") for entry in row.get("sourcing") or []
                           if isinstance(entry, dict) and entry.get("vendor")), "")
        vendor = vendor or row.get("brand") or row.get("manufacturer") or params.get("manufacturer") or ""
        tiles.append({"index": row["index"], "part_id": row.get("part_id", ""),
                      "title": row.get("title") or row.get("name") or "",
                      "vendor": vendor,
                      "designation": row.get("designation") or params.get("sku") or params.get("mpn") or "",
                      "image": image, "provenance": provenance if image else ""})
    try:
        png = compose_contact_sheet(tiles)
    except Exception as exc:  # noqa: BLE001 — never fail the search for a sheet
        out["preview_sheet"]["reason"] = f"composition_failed: {exc}"
        return out, None
    out["preview_sheet"] = {"pictured": indices[:_MAX_TILES], "omitted": indices[_MAX_TILES:],
                            "reason": "sheet_limit" if len(indices) > _MAX_TILES else ""}
    return out, png

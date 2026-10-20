"""Tool-returned images, delivered to the model as images.

CAD render tools return FastMCP ``Image`` objects, usually ``[Image(path=png), caption]``
(:func:`services.mcp.cad_common._captioned_render`). FastMCP's own server turns those into
MCP ``ImageContent`` for external clients, but CAD's agent adapters call the tool functions
directly, so the object itself reaches them. Both adapters — Chudcode host tools
(:mod:`services.agent.chudcode_tools`) and the in-process turn loop
(:mod:`services.agent.agent_turn`) — use this one rule to (1) turn the object into a
JSON-safe ``{"kind": "image", "path", "mime"}`` reference that run records, artifact events
and the model text can all carry, and (2) load that reference as bytes a vision model
accepts, downscaling rather than dropping an image over the provider limits.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Longest edge sent to a model. Claude downsamples anything larger to this edge before
#: vision processing, and Chudcode's own image pipeline targets the same edge, so extra
#: pixels cost transport bytes and add nothing the model can see.
MAX_EDGE_PX = 1568
#: Encoded bytes per image. Anthropic refuses an image whose base64 form exceeds 5 MB;
#: 3 MiB raw is 4 MiB of base64.
MAX_BYTES = 3 * 1024 * 1024
#: Bound source reads before decode/base64, including files growing during a read.
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 20 * 1024 * 1024
#: Downscaling stops at this edge: vision backends reject images below 200 px.
MIN_EDGE_PX = 200

# Formats every vision provider accepts unchanged (PIL format name -> MIME type).
_MODEL_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class ImageUnavailable(Exception):
    """A referenced image cannot be delivered: the file is unreadable or not an image."""


@dataclass(frozen=True)
class ModelImage:
    """Image bytes ready for a model, plus what was done to fit the limits."""

    data: bytes
    mime: str
    size: tuple[int, int]
    original_size: tuple[int, int]
    reencoded: bool

    def block(self) -> dict[str, str]:
        """Chudcode/MCP ``ImageContent``: ``{"type": "image", "mimeType", "data"}``."""
        return {
            "type": "image",
            "mimeType": self.mime,
            "data": base64.b64encode(self.data).decode("ascii"),
        }

    def note(self, path: str) -> str:
        """One line telling the model the pixels it sees are not the file's pixels."""
        width, height = self.size
        ow, oh = self.original_size
        return (
            f"[image {path} sent as {width}x{height} {self.mime} "
            f"(original {ow}x{oh}) to fit model image limits]"
        )


def image_ref(value: Any) -> dict[str, Any] | None:
    """``{"kind": "image", "path", "mime"}`` for an image object a tool returned, else None.

    FastMCP ``Image`` carries a file ``path`` or inline ``data`` bytes plus ``_mime_type``;
    MCP ``ImageContent`` carries base64 ``data`` plus ``mimeType``. Inline bytes are
    written to the render cache so every consumer holds the same displayable path
    reference instead of a byte count no model or panel can show.
    """
    mime = getattr(value, "_mime_type", None)
    if isinstance(mime, str) and (hasattr(value, "path") or hasattr(value, "data")):
        path = getattr(value, "path", None)
        if path is not None:
            return {"kind": "image", "path": os.fspath(path), "mime": mime}
        data = getattr(value, "data", None)
        if isinstance(data, (bytes, bytearray)) and len(data) > MAX_SOURCE_BYTES:
            raise ImageUnavailable("inline image exceeds the image source byte limit")
        if isinstance(data, (bytes, bytearray)) and data:
            return {"kind": "image", "path": materialize(bytes(data), mime), "mime": mime}
        return None
    if getattr(value, "type", None) == "image":
        data = getattr(value, "data", None)
        mime = getattr(value, "mimeType", None)
        if isinstance(data, str) and data and isinstance(mime, str):
            if len(data) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
                raise ImageUnavailable("inline image exceeds the image source byte limit")
            return {
                "kind": "image",
                "path": materialize(base64.b64decode(data), mime),
                "mime": mime,
            }
    return None


def materialize(data: bytes, mime: str) -> str:
    """Write image bytes into the durable render cache and return the path."""
    if len(data) > MAX_SOURCE_BYTES:
        raise ImageUnavailable("image exceeds the image source byte limit")
    from services.mcp.cad_common import render_output_path  # noqa: PLC0415 - heavy, rare

    path = render_output_path("tool-image", _SUFFIXES.get(mime, ".bin"))
    Path(path).write_bytes(data)
    return path


def image_refs(value: Any) -> list[dict[str, Any]]:
    """Every distinct image reference in a JSON-safe tool result, in result order."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, Mapping):
            if _is_image_ref(node):
                path = node["path"]
                if path not in seen:
                    seen.add(path)
                    found.append(dict(node))
                return
            for item in node.values():
                visit(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item, depth + 1)

    visit(value, 0)
    return found


#: The clause ``cad_render_tools._render_save_note`` adds to a caption when the
#: caller gave ``save_path``.
_SAVED_CLAUSE = re.compile(r"saved: (?P<path>.+?) \(\d+ B, sha256 [0-9a-f]+\)")


def image_result(value: Any) -> dict[str, Any] | None:
    """The one mapping shape of a JSON-safe tool result that holds images, else None.

    Render tools return ``[image, caption]``, a bare image, or images followed by text.
    Eval code reads every CAD result as a mapping (``r.items()``, ``r["ok"]``), and a
    list stopped it (c250: ``'list' object has no attribute 'items'`` after
    ``render_part``). The mapping is::

        {"ok": true, "caption": str,
         "images": [{"kind": "image", "path", "mime", "width", "height"}],
         "saved_path": str}   # saved_path only when the caption names a saved copy

    A text member that is a JSON object (``review_drawing`` diagnostics) adds its keys,
    and its ``ok`` replaces the default. The arg-note wrapper ``{"ok", "result": <image
    result>, ...}`` becomes the same mapping plus the wrapper keys. A value with no image,
    or with a member that is not an image, text or object, gives None: it stays as is.
    """
    if isinstance(value, Mapping):
        if _is_image_ref(value):
            return _image_mapping([value])
        inner = value.get("result")
        if _is_image_ref(inner):
            inner = [inner]
        shaped = _image_mapping(inner) if isinstance(inner, list) else None
        if shaped is None:
            return None
        for key, item in value.items():
            if key != "result" and key not in shaped:
                shaped[key] = item
        return shaped
    if isinstance(value, list):
        return _image_mapping(value)
    return None


def _is_image_ref(node: Any) -> bool:
    return (
        isinstance(node, Mapping)
        and str(node.get("kind") or "").strip().lower() == "image"
        and isinstance(node.get("path"), str)
        and bool(node.get("path"))
    )


def _image_mapping(items: list[Any]) -> dict[str, Any] | None:
    images: list[dict[str, Any]] = []
    texts: list[str] = []
    fields: dict[str, Any] = {}
    for item in items:
        if _is_image_ref(item):
            images.append(_with_size(item))
        elif isinstance(item, str):
            obj = _json_object(item)
            if obj is not None:
                fields.update(obj)
            elif item.strip():
                texts.append(item.strip())
        elif isinstance(item, Mapping):
            fields.update(item)
        else:
            return None
    if not images:
        return None
    caption = "\n".join(texts)
    if not caption and isinstance(fields.get("caption"), str):
        caption = fields["caption"]
    shaped: dict[str, Any] = {"ok": True, **fields, "caption": caption, "images": images}
    saved = _SAVED_CLAUSE.search(caption)
    if saved is not None:
        shaped["saved_path"] = saved.group("path")
    return shaped


def _json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _with_size(ref: Mapping[str, Any]) -> dict[str, Any]:
    """*ref* plus the file's pixel ``width``/``height`` when its header can be read."""
    described = dict(ref)
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(described["path"]) as img:
            described["width"], described["height"] = int(img.width), int(img.height)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        pass  # load_model_image names an unreadable image in the result
    return described


def read_image_bytes(path: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    """Read a regular image source through a hard byte ceiling, never to EOF."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ImageUnavailable(f"{path} is not a regular image file")
            if info.st_size > max_bytes:
                raise ImageUnavailable(f"{path} exceeds the image source byte limit ({max_bytes})")
            raw = source.read(max_bytes + 1)
            if len(raw) > max_bytes or os.fstat(source.fileno()).st_size > max_bytes:
                raise ImageUnavailable(f"{path} exceeds the image source byte limit ({max_bytes})")
            return raw
    except OSError as exc:
        raise ImageUnavailable(f"{path} is unreadable: {exc.strerror or exc}") from exc


def load_model_image(path: str) -> ModelImage:
    """Load *path* as model-ready image bytes within :data:`MAX_EDGE_PX`/:data:`MAX_BYTES`.

    A supported format already inside both limits is sent byte-for-byte. Anything else
    is decoded (first frame for an animation) and re-encoded — downscaled to the edge
    limit, then further by 0.8 steps until it fits the byte limit — never dropped.
    Raises :class:`ImageUnavailable` for an unreadable file or undecodable bytes.
    """
    raw = read_image_bytes(path)
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(raw)) as img:
            size = (int(img.width), int(img.height))
            if size[0] * size[1] > MAX_SOURCE_PIXELS:
                raise ImageUnavailable(f"{path} exceeds the image source pixel limit ({MAX_SOURCE_PIXELS})")
            mime = _MODEL_FORMATS.get(img.format or "")
            if mime is not None and max(size) <= MAX_EDGE_PX and len(raw) <= MAX_BYTES:
                return ModelImage(raw, mime, size, size, reencoded=False)
            img.load()
            return _reencode(img, size)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ImageUnavailable(f"{path} is not a decodable image: {exc}") from exc


def _reencode(img: Any, original: tuple[int, int]) -> ModelImage:
    from PIL import Image  # noqa: PLC0415

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    frame = img.convert("RGBA" if has_alpha else "RGB")
    # A photo stays JPEG; renders and drawings are line art and stay lossless.
    fmt, mime = ("JPEG", "image/jpeg") if img.format == "JPEG" and not has_alpha else (
        "PNG",
        "image/png",
    )
    width, height = original
    scale = min(1.0, MAX_EDGE_PX / max(width, height))
    while True:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        shot = frame if size == frame.size else frame.resize(size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if fmt == "JPEG":
            shot.save(buffer, format=fmt, quality=90)
        else:
            shot.save(buffer, format=fmt)
        data = buffer.getvalue()
        if len(data) <= MAX_BYTES:
            return ModelImage(data, mime, size, original, reencoded=True)
        if min(size) <= MIN_EDGE_PX:
            raise ImageUnavailable("image cannot fit the model byte limit at the minimum edge")
        scale *= 0.8

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
import os
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
        if isinstance(data, (bytes, bytearray)) and data:
            return {"kind": "image", "path": materialize(bytes(data), mime), "mime": mime}
        return None
    if getattr(value, "type", None) == "image":
        data = getattr(value, "data", None)
        mime = getattr(value, "mimeType", None)
        if isinstance(data, str) and data and isinstance(mime, str):
            return {
                "kind": "image",
                "path": materialize(base64.b64decode(data), mime),
                "mime": mime,
            }
    return None


def materialize(data: bytes, mime: str) -> str:
    """Write image bytes into the durable render cache and return the path."""
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
            path = node.get("path")
            if (
                str(node.get("kind") or "").strip().lower() == "image"
                and isinstance(path, str)
                and path
            ):
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


def load_model_image(path: str) -> ModelImage:
    """Load *path* as model-ready image bytes within :data:`MAX_EDGE_PX`/:data:`MAX_BYTES`.

    A supported format already inside both limits is sent byte-for-byte. Anything else
    is decoded (first frame for an animation) and re-encoded — downscaled to the edge
    limit, then further by 0.8 steps until it fits the byte limit — never dropped.
    Raises :class:`ImageUnavailable` for an unreadable file or undecodable bytes.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ImageUnavailable(f"{path} is unreadable: {exc.strerror or exc}") from exc
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(raw)) as img:
            size = (int(img.width), int(img.height))
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
        if len(data) <= MAX_BYTES or min(size) <= MIN_EDGE_PX:
            return ModelImage(data, mime, size, original, reencoded=True)
        scale *= 0.8

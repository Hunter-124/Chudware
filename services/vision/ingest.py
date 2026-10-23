"""Image ingestion: photo-ID registry + the agent-facing downscale pipeline.

Pure Pillow (already a runtime dep) — **no OpenCV here**, so the MCP server always imports
this cleanly. Two jobs:

* :func:`ingest_image` — hash a real file to a stable ``photo_id``, then emit a downscaled
  *preview* that obeys the agent-facing cap: **≤1024 px longest
  side and <1 MB** (the Claude Desktop tool-result ceiling). The full-resolution original is
  never modified; its path rides along as the ``resource_link`` so the agent can pull full
  detail on demand.
* :class:`PhotoRegistry` — an in-memory ``photo_id → PhotoRecord`` map with ``to_dict`` /
  ``from_dict`` so the MCP layer can persist it inside the ``.step+`` doc alongside the other
  intake artifacts. Re-ingesting the same bytes is idempotent (same hash → same id).
"""
from __future__ import annotations

import copy
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image

#: Agent-facing caps. The preview the model actually looks at must
#: fit both — the full-res original is linked, never inlined.
PREVIEW_MAX_PX: int = 1024
PREVIEW_MAX_BYTES: int = 1_000_000  # <1 MB

_JPEG_START_QUALITY = 88
_JPEG_MIN_QUALITY = 40


def _preview_cache_dir(cache_dir: Optional[str]) -> Path:
    d = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "chudware_vision"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _encode_preview(im: Image.Image) -> tuple[bytes, str, str]:
    """Downscale ``im`` to satisfy both caps; return ``(bytes, mime, ext)``.

    Alpha/graphics keep PNG (lossless); photographic RGB uses JPEG so a real phone photo
    actually lands under 1 MB at 1024 px. If the first encode is still too big we step the
    JPEG quality down, then the pixel dimensions, until it fits — the caps are a hard
    contract, never "best effort".
    """
    longest = max(im.size)
    if longest > PREVIEW_MAX_PX:
        scale = PREVIEW_MAX_PX / float(longest)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.LANCZOS)

    has_alpha = im.mode in ("RGBA", "LA", "P") and (
        im.mode != "P" or "transparency" in im.info
    )

    if has_alpha:
        work = im.convert("RGBA")
        while True:
            buf = io.BytesIO()
            work.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= PREVIEW_MAX_BYTES or min(work.size) <= 64:
                return data, "image/png", "png"
            work = work.resize((max(1, int(work.width * 0.8)), max(1, int(work.height * 0.8))),
                               Image.LANCZOS)

    work = im.convert("RGB")
    while True:
        q = _JPEG_START_QUALITY
        while q >= _JPEG_MIN_QUALITY:
            buf = io.BytesIO()
            work.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
            data = buf.getvalue()
            if len(data) <= PREVIEW_MAX_BYTES:
                return data, "image/jpeg", "jpg"
            q -= 12
        if min(work.size) <= 64:
            return data, "image/jpeg", "jpg"  # smallest we could do; still return it
        work = work.resize((max(1, int(work.width * 0.8)), max(1, int(work.height * 0.8))),
                           Image.LANCZOS)


@dataclass
class PhotoRecord:
    """One ingested photo: the full-res original (``resource_link``) + its agent preview."""

    photo_id: str
    path: str                 # absolute path to the untouched full-resolution original
    preview_path: str         # ≤1024 px / <1 MB derivative the agent looks at
    width: int                # full-res dimensions
    height: int
    preview_width: int
    preview_height: int
    mime: str                 # preview mime (image/png | image/jpeg) — the provider slot needs it
    bytes: int                # full-res byte size
    preview_bytes: int
    label: str = "part"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_link(self) -> str:
        """Full-resolution original path — linked, never inlined into the agent context."""
        return self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "photo_id": self.photo_id,
            "path": self.path,
            "preview_path": self.preview_path,
            "width": self.width,
            "height": self.height,
            "preview_width": self.preview_width,
            "preview_height": self.preview_height,
            "mime": self.mime,
            "bytes": self.bytes,
            "preview_bytes": self.preview_bytes,
            "label": self.label,
            "resource_link": self.path,
            "meta": copy.deepcopy(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PhotoRecord":
        return cls(
            photo_id=d["photo_id"],
            path=d["path"],
            preview_path=d["preview_path"],
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            preview_width=int(d.get("preview_width", 0)),
            preview_height=int(d.get("preview_height", 0)),
            mime=d.get("mime", "image/png"),
            bytes=int(d.get("bytes", 0)),
            preview_bytes=int(d.get("preview_bytes", 0)),
            label=d.get("label", "part"),
            meta=copy.deepcopy(d.get("meta") or {}),
        )


def ingest_image(path: str, *, label: str = "part",
                 cache_dir: Optional[str] = None) -> PhotoRecord:
    """Register ``path`` and produce its agent preview.

    Raises ``FileNotFoundError`` if the path is missing and ``OSError`` / ``PIL`` errors if
    the bytes are not a decodable image — the MCP adapter turns those into a contract-shaped
    ``{ok: False, reason, hint}`` rather than a traceback.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such image file: {path}")
    raw = p.read_bytes()
    photo_id = "ph_" + hashlib.sha1(raw).hexdigest()[:12]

    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        full_w, full_h = im.size
        data, mime, ext = _encode_preview(im)

    out_dir = _preview_cache_dir(cache_dir)
    preview_path = out_dir / f"{photo_id}.preview.{ext}"
    preview_path.write_bytes(data)
    with Image.open(io.BytesIO(data)) as pim:
        pw, ph = pim.size

    return PhotoRecord(
        photo_id=photo_id,
        path=str(p.resolve()),
        preview_path=str(preview_path.resolve()),
        width=full_w,
        height=full_h,
        preview_width=pw,
        preview_height=ph,
        mime=mime,
        bytes=len(raw),
        preview_bytes=len(data),
        label=label,
    )


@dataclass
class PhotoRegistry:
    """``photo_id → PhotoRecord`` map, JSON-round-trippable for persistence in the doc."""

    photos: dict[str, PhotoRecord] = field(default_factory=dict)

    def register(self, path: str, *, label: str = "part",
                 cache_dir: Optional[str] = None) -> PhotoRecord:
        rec = ingest_image(path, label=label, cache_dir=cache_dir)
        self.photos[rec.photo_id] = rec
        return rec

    def get(self, photo_id: str) -> Optional[PhotoRecord]:
        return self.photos.get(photo_id)

    def __contains__(self, photo_id: object) -> bool:
        return photo_id in self.photos

    def to_dict(self) -> dict[str, Any]:
        return {"photos": {k: v.to_dict() for k, v in self.photos.items()}}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PhotoRegistry":
        raw = (d or {}).get("photos") or {}
        return cls(photos={k: PhotoRecord.from_dict(v) for k, v in raw.items()})

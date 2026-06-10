""".step+ container reader/writer (docs/contracts/stepplus.md).

A ``.step+`` is a ZIP that travels like a STEP file: any CAD opens the embedded ``geometry.step``;
AI-CAD gets the full parametric document back. This module implements the minimal M1 surface:

  save(session, path) -> writes thumbnail.png, meta.json, document.json, geometry.step (the last
    good rebuild), preserving any unknown entries carried on the session. Atomic: writes to
    ``<path>.tmp`` then ``os.replace``.
  open(path) -> DocSession : reads meta.json (rejects unknown major container version), parses
    document.json, registers a session, rebuilds. On rebuild failure the doc is returned with
    error feature-statuses (the frozen-geometry fallback of stepplus.md §2.3 is DEFERRED — a later
    card; see C11 report).

Thumbnail: a 256×256 flat-grey PNG placeholder is written (rendering the tessellation via
engine/render.py off-screen needs pyvista/VTK and a temporary STL; that is heavyweight for the
save path, so the placeholder is the chosen M1 behaviour — noted in the C11 report). It is the
FIRST entry and STORED (uncompressed) so shell previewers can read it cheaply (stepplus.md §1).
"""

from __future__ import annotations

import datetime
import io
import json
import os
import struct
import zlib
import zipfile
from pathlib import Path
from typing import Optional

from engine.doc.compiler import compile_document
from engine.doc.session import DocSession, _rebuild, from_document

_CONTAINER = "aicad.step+/1"
_APP_VERSION = "AI-CAD/M1"

# Entries this module owns + rewrites on every save; everything else is preserved verbatim.
_KNOWN_ENTRIES = {"thumbnail.png", "meta.json", "document.json", "geometry.step"}


# --------------------------------------------------------------------------- thumbnail


def _placeholder_png(size: int = 256, grey: int = 200) -> bytes:
    """A minimal valid flat-grey PNG (no external deps)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = bytes([grey, grey, grey]) * size
    raw = b"".join(b"\x00" + row for _ in range(size))
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --------------------------------------------------------------------------- geometry export


def _export_geometry_step(session: DocSession) -> Optional[bytes]:
    """Export the last successful rebuild's bodies as a single AP214 STEP (bytes), or None.

    Omitted (None) when the document has never produced a body (stepplus.md §1 yes*).
    """
    result = session.last_result
    if result is None:
        result = _rebuild(session)
    bodies = [bo.shape for bo in result.bodies]
    if not bodies:
        return None

    try:
        from build123d import Compound, export_step
    except Exception:  # noqa: BLE001
        return None

    try:
        if len(bodies) == 1:
            shape = bodies[0]
        else:
            shape = Compound(children=list(bodies))
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tf:
            tmp = tf.name
        try:
            export_step(shape, tmp)
            data = Path(tmp).read_bytes()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return data
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- save


def save(session: DocSession, path: str | os.PathLike) -> str:
    """Write the session to ``path`` as a ``.step+`` container. Returns the final path string.

    Atomic: writes to ``<path>.tmp`` then ``os.replace``. Unknown ZIP entries carried on the
    session (``session.extra_entries``) are preserved (stepplus.md §3).
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    kind = "assembly" if session.doc.get("schema") == "aicad.asm/1" else "part"
    meta = {
        "container": _CONTAINER,
        "app_version": _APP_VERSION,
        "kind": kind,
        "created": now,
        "modified": now,
        "units": session.doc.get("units", {"system": "MMGS"}),
        "name": session.doc.get("name", "Untitled"),
    }

    geometry = _export_geometry_step(session)
    document = json.dumps(session.doc, indent=2).encode("utf-8")
    thumb = session.extra_entries.get("thumbnail.png") or _placeholder_png()

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        # thumbnail FIRST + STORED (uncompressed) so shell previewers read it cheaply.
        zi = zipfile.ZipInfo("thumbnail.png")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, thumb)
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        zf.writestr("document.json", document)
        if geometry is not None:
            zf.writestr("geometry.step", geometry)
        # Preserve unknown entries (forward-compat) — never clobber the entries we own.
        for name, data in session.extra_entries.items():
            if name in _KNOWN_ENTRIES:
                continue
            zf.writestr(name, data)

    os.replace(tmp, path)
    session.path = str(path)
    session.dirty = False
    return str(path)


# --------------------------------------------------------------------------- open


def open(path: str | os.PathLike) -> tuple[DocSession, list[str]]:
    """Open a ``.step+`` file → ``(DocSession, warnings)``. The session is registered + rebuilt.

    Load order (stepplus.md §2): read meta (reject unknown MAJOR container version), parse
    document.json, rebuild. On rebuild failure the document is kept and its feature-statuses carry
    the errors (NO frozen-geometry fallback this card — deferred).
    """
    path = Path(path)
    warnings: list[str] = []

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "meta.json" not in names:
            raise ValueError("not a .step+ container: meta.json missing")
        meta = json.loads(zf.read("meta.json").decode("utf-8"))
        container = meta.get("container", "")
        major = container.split("/")[-1] if "/" in container else ""
        if not container.startswith("aicad.step+/"):
            raise ValueError(f"unknown container '{container}'")
        if major and major != "1":
            raise ValueError(f"unsupported .step+ major version '{major}'")

        if "document.json" not in names:
            raise ValueError("not a .step+ container: document.json missing")
        doc = json.loads(zf.read("document.json").decode("utf-8"))

        # Carry every entry we don't own back onto the session for forward-compatible resave.
        extra: dict[str, bytes] = {}
        for name in names:
            if name in _KNOWN_ENTRIES:
                continue
            extra[name] = zf.read(name)

    session = from_document(doc, path=str(path), extra_entries=extra)

    # Rebuild; collect failures as warnings but still return the session (no fallback this card).
    result = _rebuild(session)
    failed = [s["id"] for s in result.feature_status if not s.get("ok") and not s.get("skipped")]
    if failed:
        warnings.append(f"{len(failed)} feature(s) failed to rebuild: {failed}")

    return session, warnings

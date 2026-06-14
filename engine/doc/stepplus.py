""".step+ container reader/writer (docs/contracts/stepplus.md).

A ``.step+`` is a ZIP that travels like a STEP file: any CAD opens the embedded ``geometry.step``;
AI-CAD gets the full parametric document back.

C11 M1 surface (preserved verbatim):
  save(session, path) -> writes thumbnail.png, meta.json, document.json, geometry.step (the last
    good rebuild), preserving any unknown entries carried on the session. Atomic.
  open(path) -> (DocSession, warnings).

C21 additions:
  * Frozen-geometry fallback (stepplus.md §2.3): if the rebuild completely fails (every non-
    skipped feature is in error), ``open`` extracts ``geometry.step`` to a temp file, adds a
    single ``import`` feature whose ``source_path`` points there, and returns a fresh session
    carrying that one feature.  The broken document.json is left on disk (the session's
    ``extra_entries["_broken_document.json"]`` preserves it for diagnostic purposes).
    Call ``open_with_fallback(path)`` to use this path explicitly; plain ``open()`` now invokes
    the fallback automatically when ALL features fail.
  * studies/* + ai/memory.json passthrough: already handled by the generic ``extra_entries``
    mechanism (unknown entries preserved on resave).  C21 adds two helpers that operate on the
    ZIP directly without touching session.py:
      ``inject_meta_entry(path, entry_name, data)`` — add/replace one ZIP entry in a saved file.
      ``read_meta_entry(path, entry_name) -> bytes | None`` — read one named entry, or None.

Thumbnail: flat-grey 256×256 PNG placeholder (unchanged from C11 — off-screen render is C23).
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

# W12+ drawing persistence: drawings live in their own ZIP folder, one JSON per drawing. They are
# (re)written from the live drawing registry on every save and re-read on open, so any stale
# ``drawings/*.json`` carried in ``extra_entries`` is dropped on resave (never written back blindly).
_DRAWINGS_PREFIX = "drawings/"


def _is_drawing_entry(name: str) -> bool:
    return name.startswith(_DRAWINGS_PREFIX) and name.lower().endswith(".json")


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


# --------------------------------------------------------------------------- drawing embed


def _collect_drawing_entries(session: DocSession) -> dict[str, bytes]:
    """``{"drawings/<drawing_id>.json": <bytes>}`` for every open drawing of this document.

    Pulled live from the drawing registry (engine.doc.drawing) by source doc_id. A document with no
    drawings yields an empty dict, so its ``.step+`` is byte-for-byte what it was pre-drawings. The
    drawing module is imported lazily + defensively: a registry hiccup must never break a part save.
    """
    try:
        from engine.doc import drawing as drawmod
    except Exception:  # noqa: BLE001
        return {}
    entries: dict[str, bytes] = {}
    try:
        sessions = drawmod.drawings_for_source(session.doc_id)
    except Exception:  # noqa: BLE001
        return {}
    for ds in sessions:
        try:
            doc = drawmod.serialize_drawing(ds)
            entries[f"{_DRAWINGS_PREFIX}{ds.doc_id}.json"] = json.dumps(
                doc, indent=2).encode("utf-8")
        except Exception:  # noqa: BLE001 — one bad drawing is skipped, never fails the part save
            continue
    return entries


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

    # W12+ — embed every open drawing whose `source` is this document. A DrawingDocument is pure
    # JSON (view defs + annotations, no geometry), so it round-trips as `drawings/<id>.json`.
    drawing_entries = _collect_drawing_entries(session)

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
        for entry_name, data in drawing_entries.items():
            zf.writestr(entry_name, data)
        # Preserve unknown entries (forward-compat) — never clobber the entries we own, and drop any
        # stale `drawings/*.json` (we just wrote the live set above from the registry).
        for name, data in session.extra_entries.items():
            if name in _KNOWN_ENTRIES or _is_drawing_entry(name):
                continue
            zf.writestr(name, data)

    os.replace(tmp, path)
    session.path = str(path)
    session.dirty = False
    return str(path)


# --------------------------------------------------------------------------- open


def open(path: str | os.PathLike) -> tuple[DocSession, list[str]]:
    """Open a ``.step+`` file → ``(DocSession, warnings)``. The session is registered + rebuilt.

    Load order (stepplus.md §2):
    1. Read meta.json; reject unknown major container version.
    2. Parse document.json → rebuild.
    3. If ALL non-skipped features fail (i.e. total rebuild failure), invoke the frozen-geometry
       fallback (stepplus.md §2.3): extract geometry.step to a temp file and build a session
       with a single ``import`` feature pointing at it.  The broken document JSON is preserved
       in extra_entries[``_broken_document.json``] for diagnostics.
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
        for entry_name in names:
            if entry_name in _KNOWN_ENTRIES:
                continue
            extra[entry_name] = zf.read(entry_name)

        # Read geometry.step bytes now (needed for fallback path below).
        geometry_bytes: Optional[bytes] = None
        if "geometry.step" in names:
            geometry_bytes = zf.read("geometry.step")

    session = from_document(doc, path=str(path), extra_entries=extra)

    # Rebuild; check if ALL non-skipped features failed.
    result = _rebuild(session)
    non_skipped = [s for s in result.feature_status if not s.get("skipped")]
    failed = [s["id"] for s in non_skipped if not s.get("ok")]

    if failed:
        if len(failed) == len(non_skipped) and non_skipped and geometry_bytes is not None:
            # Total rebuild failure with a geometry snapshot available → frozen-geometry fallback.
            warnings.append(
                f"All {len(failed)} feature(s) failed to rebuild; "
                "opening frozen geometry.step as an import feature."
            )
            session = _frozen_geometry_fallback(
                session, geometry_bytes, doc, extra, str(path)
            )
        else:
            warnings.append(f"{len(failed)} feature(s) failed to rebuild: {failed}")

    return session, warnings


def restore_drawings(session: DocSession) -> list[dict]:
    """Re-register every embedded drawing of a just-opened part/asm session (W12+ persistence).

    Reads each ``drawings/*.json`` carried on ``session.extra_entries`` (populated by :func:`open`,
    and preserved through the frozen-geometry fallback), registers it as a FRESH DrawingSession via
    :func:`engine.doc.drawing.register_restored_drawing`, and re-points its ``source`` to this
    session's (new) doc_id so it projects against the reopened bodies. Returns one
    ``{"doc_id", "name", "source_doc_id"}`` per restored drawing so the host can re-open its tab.

    Fully contained: a malformed / non-drawing JSON entry is SKIPPED with a stderr log, never
    failing the part open. Backward-compatible — a ``.step+`` with no ``drawings/`` returns ``[]``.
    Call exactly once per top-level open (the component-load opens in assembly.py do NOT call this,
    so a part used as an assembly component never spawns phantom drawing tabs).
    """
    import sys
    from engine.doc.schema import SCHEMA_DRAWING

    restored: list[dict] = []
    for entry_name in sorted(session.extra_entries):
        if not _is_drawing_entry(entry_name):
            continue
        try:
            doc = json.loads(session.extra_entries[entry_name].decode("utf-8"))
            if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_DRAWING:
                raise ValueError(f"not a {SCHEMA_DRAWING} document")
            from engine.doc import drawing as drawmod
            ds = drawmod.register_restored_drawing(doc, session.doc_id)
        except Exception as exc:  # noqa: BLE001 — one bad drawing is skipped, never breaks the open
            print(f"[stepplus] skipped malformed drawing '{entry_name}': "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        restored.append({"doc_id": ds.doc_id, "name": ds.doc.get("name", "Drawing"),
                         "source_doc_id": session.doc_id})
    return restored


def _frozen_geometry_fallback(
    failed_session: "DocSession",
    geometry_bytes: bytes,
    broken_doc: dict,
    extra_entries: dict[str, bytes],
    original_path: str,
) -> "DocSession":
    """Implement stepplus.md §2.3: rebuild-failure → frozen import feature.

    Extracts ``geometry.step`` to a PERSISTENT temp file (lives as long as the session), builds a
    fresh blank part with one ``import`` feature pointing at it, and returns the rebuilt session.
    The broken document JSON is stashed in ``extra_entries[_broken_document.json]`` so the user
    can inspect or recover it.

    This function is also callable directly from worker methods (e.g. to offer the fallback
    explicitly without the open() wrapper).
    """
    import tempfile
    import uuid
    from engine.doc.session import new_session, apply_ops as sess_apply_ops, close_session

    # Close the failed session first (it is already registered with a doc_id).
    close_session(failed_session.doc_id)

    # Write geometry.step to a persistent temp file.  We use a named temp with delete=False
    # so the import feature compiler can re-read it on future recompiles.
    tmpdir = Path(tempfile.gettempdir()) / "aicad_frozen"
    tmpdir.mkdir(parents=True, exist_ok=True)
    step_tmp = tmpdir / f"frozen_{uuid.uuid4().hex[:8]}.step"
    step_tmp.write_bytes(geometry_bytes)

    # New blank part session.
    stem = Path(original_path).stem if original_path else "frozen"
    session = new_session("part", name=f"{stem} (frozen)")
    session.path = original_path

    # Preserve extra entries + stash the broken doc.
    session.extra_entries.update(extra_entries)
    import json as _json
    session.extra_entries["_broken_document.json"] = _json.dumps(
        broken_doc, indent=2
    ).encode("utf-8")

    # Apply one import feature pointing at the extracted STEP.
    op = {
        "op": "add_feature",
        "feature": {
            "type": "import",
            "name": f"{stem} (frozen)",
            "params": {
                "source_path": str(step_tmp),
                "format": "step",
                "original_name": stem,
            },
            "refs": {},
        },
    }
    sess_apply_ops(session, None, [op])
    return session


# --------------------------------------------------------------------------- ZIP meta helpers
# Operate on a saved .step+ file WITHOUT loading a full DocSession.
# Used for studies/* + ai/memory.json injection tests (C21).


def inject_meta_entry(
    path: str | os.PathLike,
    entry_name: str,
    data: bytes,
) -> None:
    """Add or replace *entry_name* inside the saved ``.step+`` ZIP at *path*.

    Atomic: writes to a ``.tmp`` then replaces.  Owned entries (thumbnail/meta/document/geometry)
    may be overwritten; use with care.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".inject.tmp")
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == entry_name:
                    continue  # drop the old version
                zout.writestr(item, zin.read(item.filename))
            # Write the new / updated entry.
            if entry_name == "thumbnail.png":
                zi = zipfile.ZipInfo(entry_name)
                zi.compress_type = zipfile.ZIP_STORED
                zout.writestr(zi, data)
            else:
                zout.writestr(entry_name, data)
    os.replace(tmp, path)


def read_meta_entry(
    path: str | os.PathLike,
    entry_name: str,
) -> Optional[bytes]:
    """Return the raw bytes of *entry_name* from the ZIP, or ``None`` if absent."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if entry_name in zf.namelist():
                return zf.read(entry_name)
    except Exception:  # noqa: BLE001
        pass
    return None

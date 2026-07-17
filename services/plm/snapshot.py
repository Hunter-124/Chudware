"""services.plm.snapshot — explode/re-assemble document containers (contract §2).

A ``.step+`` / ``.ecad+`` working file is a ZIP container (git-hostile). On
check-in it is *exploded* into the shadow tree; on restore it is *re-assembled*
byte-faithfully into the working file.

Contract §2 shadow-tree layout, per item::

    items/<item_id>/document.json    canonical parametric JSON (byte-stable, §0)
    items/<item_id>/meta.json        mutable per-item metadata (§3.4)
    items/<item_id>/thumbnail.png    timeline thumbnail
    items/<item_id>/summary.txt      plain-language change summary
    items/<item_id>/geometry.step    frozen B-rep — RELEASE COMMITS ONLY (§5)
    items/<item_id>/extra/<name>     verbatim-preserved unknown container entries

Data-loss guard (critical): a container may carry entries beyond the four we own
(e.g. ``netlist.json``, forward-compat ``_extra_entries``). We NEVER drop them —
unknown entries are preserved under ``extra/`` and restored verbatim, so restore
loses nothing (contract "no content is ever discarded"). ``netlist.json`` is
derived/recomputable, so it is treated as a known-derived entry and simply
re-preserved rather than recomputed here (the app rebuilds it on open).

Round-trip contract: the DoD's pass check is semantic — ``doc_diff`` of the
restored ``document.json`` vs the original == empty — NOT byte-identical ZIP.
Re-zipping is not byte-stable at the container level (timestamps, ordering) and
that is expected; the *canonical document.json* is what must be faithful.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field

from engine.doc.canonical import canonical_dump

# Container entries we explode to first-class shadow-tree paths.
_DOCUMENT = "document.json"
_THUMBNAIL = "thumbnail.png"
_META = "meta.json"  # container-level meta (NOT the PLM meta.json — kept under extra/)

# Entries preserved verbatim but not promoted to a first-class shadow path.
# meta.json from the container is app-owned bookkeeping; we keep it under extra/
# so the PLM meta.json (history metadata) never collides with it.
_KNOWN_CONTAINER_ENTRIES = {_DOCUMENT, _THUMBNAIL}

# Derived / recomputable caches embedded in the live .step+ by the engine
# (core/doc/session.cpp: cache.brep, cache.aicm, and the cache.json header).
# Like build artifacts, they are stripped at the VAULT BOUNDARY — never committed
# to history — and the engine rebuilds them on the next open (the cold-open fast
# path is a property of the live working file, not of the vault).
_DERIVED_CACHE_PREFIX = "cache."


@dataclass
class ExplodedItem:
    """The exploded form of one container, ready to land in the shadow tree."""

    document_json: bytes  # canonical (byte-stable)
    thumbnail_png: bytes | None = None
    extras: dict[str, bytes] = field(default_factory=dict)  # verbatim-preserved entries


def explode_container(raw: bytes) -> ExplodedItem:
    """Explode a ``.step+`` / ``.ecad+`` ZIP container into its parts.

    ``document.json`` is re-serialized canonically (§0) so the shadow-tree blob is
    byte-stable across saves that do not change the model. Every other entry is
    preserved verbatim so restore is lossless.
    """
    doc_bytes: bytes | None = None
    thumb: bytes | None = None
    extras: dict[str, bytes] = {}

    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        for name in zf.namelist():
            if name.startswith(_DERIVED_CACHE_PREFIX):
                continue  # derived cache — never committed to history (rebuilds)
            data = zf.read(name)
            if name == _DOCUMENT:
                doc_bytes = data
            elif name == _THUMBNAIL:
                thumb = data
            else:
                extras[name] = data  # verbatim (meta.json, netlist.json, unknowns)

    if doc_bytes is None:
        raise ValueError("container has no document.json")

    # Canonicalize document.json for byte-stability (§0). If it is already
    # canonical this is a no-op; if not, we store the canonical form so diffing
    # stays false-positive-free.
    doc = json.loads(doc_bytes.decode("utf-8"))
    canonical = canonical_dump(doc).encode("utf-8")

    return ExplodedItem(document_json=canonical, thumbnail_png=thumb, extras=extras)


def reassemble_container(item: ExplodedItem) -> bytes:
    """Re-zip an :class:`ExplodedItem` back into a working container.

    Restores every preserved entry verbatim (extras) plus the canonical
    ``document.json`` and the thumbnail. Byte-faithful at the *document* level
    (canonical JSON identical); the ZIP wrapper itself is freshly written.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # thumbnail first + STORED so shell previewers read it cheaply (matches
        # engine/ecad/formats.save layout)
        if item.thumbnail_png is not None:
            zi = zipfile.ZipInfo(_THUMBNAIL)
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, item.thumbnail_png)
        zf.writestr(_DOCUMENT, item.document_json)
        for name, data in item.extras.items():
            if name in _KNOWN_CONTAINER_ENTRIES:
                continue  # never double-write an owned entry
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shadow-tree path helpers (contract §2)
# ---------------------------------------------------------------------------

def item_prefix(item_id: str) -> str:
    return f"items/{item_id}/"


def item_paths(item_id: str, exploded: ExplodedItem, *, geometry_step: bytes | None = None,
               meta_json: bytes | None = None, summary_txt: bytes | None = None,
               ) -> dict[str, bytes]:
    """Return the shadow-tree ``{path: bytes}`` map for one exploded item.

    ``geometry_step`` is included ONLY at releases (contract §2, §5); pass None
    for auto/checkpoint/restore entries.
    """
    p = item_prefix(item_id)
    out: dict[str, bytes] = {p + _DOCUMENT: exploded.document_json}
    if exploded.thumbnail_png is not None:
        out[p + _THUMBNAIL] = exploded.thumbnail_png
    if meta_json is not None:
        out[p + "meta.json"] = meta_json
    if summary_txt is not None:
        out[p + "summary.txt"] = summary_txt
    if geometry_step is not None:
        out[p + "geometry.step"] = geometry_step
    # verbatim-preserved container extras live under extra/ so they never collide
    # with the PLM-owned meta.json / summary.txt
    for name, data in exploded.extras.items():
        out[p + "extra/" + name] = data
    return out


def read_item_from_tree(tree: dict[str, bytes], item_id: str) -> ExplodedItem:
    """Reconstruct an :class:`ExplodedItem` from a flattened shadow tree."""
    p = item_prefix(item_id)
    doc = tree.get(p + _DOCUMENT)
    if doc is None:
        raise KeyError(f"item {item_id} not in tree")
    thumb = tree.get(p + _THUMBNAIL)
    extras: dict[str, bytes] = {}
    extra_prefix = p + "extra/"
    for path, data in tree.items():
        if path.startswith(extra_prefix):
            extras[path[len(extra_prefix):]] = data
    return ExplodedItem(document_json=doc, thumbnail_png=thumb, extras=extras)

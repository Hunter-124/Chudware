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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from services.plm.canonical import canonical_dump

# Container entries we explode to first-class shadow-tree paths.
_DOCUMENT = "document.json"
_THUMBNAIL = "thumbnail.png"
_META = "meta.json"  # container-level meta (NOT the PLM meta.json — kept under extra/)

# Entries preserved verbatim but not promoted to a first-class shadow path.
# meta.json from the container is app-owned bookkeeping; we keep it under extra/
# so the PLM meta.json (history metadata) never collides with it.
_KNOWN_CONTAINER_ENTRIES = {_DOCUMENT, _THUMBNAIL}

# Derived / solved state stripped at the VAULT BOUNDARY — the "compiled binary"
# half of the recipe/build-artifact split. Three rules:
#
#   1. ``cache.*``  — display/build acceleration embedded in the live .step+ by
#      the engine (cache.brep, cache.aicm and the cache.json header, see
#      core/doc/session.cpp). The engine rebuilds them on the next open; the
#      cold-open fast path is a property of the live working file, not of the
#      vault.
#   2. ``studies/<id>/results.*`` — cached FEA/CFD solve payloads (AICRES1 npz,
#      capped at 20 MB *per study*, see contracts/stepplus.md §1 and
#      docs/design/native_format.md §2.4). These are solved state: the study
#      DEFINITION (material, fixtures, loads, mesh density) lives in
#      document.json — the recipe — so a restored revision can simply re-run the
#      study. That is already the documented behaviour ("cached — re-run to
#      refresh"): a missing results cache means "re-run the study", never data
#      loss. Study DEFINITIONS (``studies/<id>.json``) are recipe and stay
#      tracked, as does every other ``studies/`` entry.
#   3. ``geometry.step`` — the AP214 snapshot of the last successful rebuild that
#      ``doc_save`` embeds in every live container (core/doc/session.cpp via
#      ``stepplus_geometry_step_bytes``, contracts/stepplus.md §1). It is derived
#      and recomputable from document.json, and contracts/plm.md §5.4 pins it to
#      RELEASE commits only — where ``Vault.release`` supplies its own frozen
#      bytes through ``geometry_for``, so history never depends on the working
#      file's copy. Preserving the live entry under ``extra/`` instead would put
#      an O(geometry) STEP blob in every checkpoint, which is precisely what §5.4
#      exists to prevent.
#
# Committing any of these would put an O(geometry) build artifact in every revision
# of history, which is exactly what the recipe-is-the-source-code rule forbids.
_DERIVED_CACHE_PREFIX = "cache."
_STUDIES_PREFIX = "studies/"
_STUDY_RESULT_BASENAME_PREFIX = "results."
_DERIVED_GEOMETRY = "geometry.step"

# Budgets apply to entries PLM actually inflates, not the working file's warm
# engine caches/geometry (which are skipped without being opened).
_MAX_ZIP_ENTRIES = 4096
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_INFLATED_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_COMPRESSION_RATIO = 1000


def is_derived_entry(name: str) -> bool:
    """True when a container entry is solved state, not recipe — stripped at the
    vault boundary and never committed to history."""
    if name.startswith(_DERIVED_CACHE_PREFIX):
        return True
    if name == _DERIVED_GEOMETRY:
        return True
    if name.startswith(_STUDIES_PREFIX):
        return name.rsplit("/", 1)[-1].startswith(_STUDY_RESULT_BASENAME_PREFIX)
    return False


@dataclass
class ExplodedItem:
    """The exploded form of one container, ready to land in the shadow tree."""

    document_json: bytes  # canonical (byte-stable)
    thumbnail_png: bytes | None = None
    extras: dict[str, bytes] = field(default_factory=dict)  # verbatim-preserved entries


def explode_container(
    raw: bytes | Path, *, reference_document: Callable[[bytes], bytes | None] | None = None,
) -> ExplodedItem:
    """Explode a ``.step+`` / ``.ecad+`` ZIP container into its parts.

    ``document.json`` is re-serialized canonically (§0) so the shadow-tree blob is
    byte-stable across saves that do not change the model. Every other entry is
    preserved verbatim so restore is lossless.

    A file-backed input seeks past derived geometry instead of copying the
    archive. ``reference_document`` resolves the canonical vault bytes from the
    working recipe; it can recognize a Git object id without reading that blob.
    Equivalent JSON reuses those canonical bytes without pretty-printing again.
    Every preserved ZIP member is still read and CRC-checked first.
    """
    doc_bytes: bytes | None = None
    thumb: bytes | None = None
    extras: dict[str, bytes] = {}

    with zipfile.ZipFile(io.BytesIO(raw) if isinstance(raw, bytes) else raw, "r") as zf:
        members = zf.infolist()
        if len(members) > _MAX_ZIP_ENTRIES:
            raise ValueError("container ZIP entry count limit exceeded")
        declared_total = 0
        names: set[str] = set()
        for info in members:
            if info.filename in names:
                raise ValueError("container ZIP has duplicate member names")
            names.add(info.filename)
            if is_derived_entry(info.filename):
                continue
            if info.file_size > _MAX_MEMBER_BYTES:
                raise ValueError("container ZIP member byte limit exceeded")
            if info.file_size > max(1, info.compress_size) * _MAX_COMPRESSION_RATIO:
                raise ValueError("container ZIP compression ratio limit exceeded")
            declared_total += info.file_size
            if declared_total > _MAX_INFLATED_BYTES:
                raise ValueError("container ZIP aggregate byte limit exceeded")

        inflated_total = 0
        for info in members:
            name = info.filename
            if is_derived_entry(name):
                continue  # solved state — never opened or committed to history
            limit = min(_MAX_MEMBER_BYTES, _MAX_INFLATED_BYTES - inflated_total)
            chunks: list[bytes] = []
            member_size = 0
            with zf.open(info, "r") as member:
                while True:
                    chunk = member.read(min(_READ_CHUNK_BYTES, limit - member_size + 1))
                    if not chunk:
                        break
                    member_size += len(chunk)
                    if member_size > limit:
                        raise ValueError("container ZIP inflation byte limit exceeded")
                    chunks.append(chunk)
            if member_size != info.file_size:
                raise ValueError("container ZIP member size mismatch")
            inflated_total += member_size
            data = b"".join(chunks)
            if name == _DOCUMENT:
                doc_bytes = data
            elif name == _THUMBNAIL:
                thumb = data
            else:
                extras[name] = data  # verbatim (meta.json, netlist.json, unknowns)

    if doc_bytes is None:
        raise ValueError("container has no document.json")

    reference = reference_document(doc_bytes) if reference_document is not None else None
    if doc_bytes == reference:
        canonical = reference
    else:
        doc = json.loads(doc_bytes.decode("utf-8"))
        # No indent selects the C encoder. Its scalar formatting matches the
        # canonical writer, unlike Python equality (True == 1 == 1.0, -0.0 == 0.0).
        # Only reuse a reference when the full normalized representation agrees.
        if (reference is not None
                and json.dumps(doc, sort_keys=True)
                == json.dumps(json.loads(reference.decode("utf-8")), sort_keys=True)):
            canonical = reference
        else:
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

"""services.plm.generic_snapshot — non-container item storage (2026-07 extension).

CAD documents are ZIP containers exploded by :mod:`snapshot`. Everything else a
team tracks — a spec PDF, a spreadsheet, an image, a simulation folder, a source
tree, a linked external repo — is landed here, under the SAME per-item shadow
path root (``items/<item_id>/``, contract §2) so history/restore/release treat
every item uniformly.

Storage forms (see :mod:`doctypes`):

* ``file`` → ``items/<id>/file/<name>``            (stored verbatim, byte-exact restore)
* ``tree`` → ``items/<id>/tree/<rel>`` + ``tree.index.json``  (folder, exclusion-filtered)
* ``link`` → ``items/<id>/link.json``              (reference only, no content)

THE CARRY-FORWARD LAW (critical — see contract §2 "a commit is whole-tree").
The vault commits the WHOLE shadow tree from a ``{path: bytes}`` map every save;
any path absent from that map is *dropped from that commit's tree*, and since
history is the commit chain, a later restore would lose it. Container items are
regenerated from their working files each commit; generic items therefore MUST
be re-emitted on every commit too. :func:`content_paths` does this: it re-reads
the working file/folder when present (capturing edits), and otherwise carries the
item's existing blobs forward from the HEAD tree unchanged. ``geometry.step`` is
never produced here (generic items have no frozen B-rep), preserving the
release-commits-only invariant (contract §5).

No user-facing git vocabulary (contract §8.1).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from services.plm import doctypes, snapshot

# Directories never walked into a ``tree`` item (build junk, VCS internals,
# dependency caches). Keeps a source/sim snapshot to the authored content.
_EXCLUDE_DIRS = frozenset({
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", ".aicadvault",
    "dist", "target", ".tox", ".cache", ".DS_Store",
})
_EXCLUDE_DIR_PREFIXES = ("build",)  # build, build-linux, build-occt, …

# Per-file size cap for ``tree`` items — a single blob larger than this is
# skipped (recorded in the index as ``skipped: true``) so a stray binary can't
# bloat the vault. Configurable via the vault-local config later; this is the
# default floor.
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024

_RESERVED_CONTROL_PATHS = frozenset({
    ".aicadvault", ".git", ".svn", ".hg", ".chudware", ".gitmodules", ".gitattributes",
})


def _reserved_control_part(part: str) -> bool:
    # NTFS normalizes trailing dots/spaces and interprets ':' as an ADS suffix.
    # Apply only to reserved names so benign POSIX colon filenames remain valid.
    return part.split(":", 1)[0].rstrip(". ").casefold() in _RESERVED_CONTROL_PATHS


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324 — content id, not security


def _raise_walk_error(error: OSError) -> None:
    raise error

def contained_path(root: Path, rel: str) -> Path:
    """Reject traversal, reserved control paths and symlink escapes."""
    from services.plm.vault import VaultError

    root = root.resolve()
    if (not isinstance(rel, str) or not rel or Path(rel).is_absolute()
            or PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive):
        raise VaultError("invalid_mode", f"item path is outside the project: {rel}")
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    if ".." in parts:
        raise VaultError("invalid_mode", f"item path is outside the project: {rel}")
    if any(p in ("", ".") or _reserved_control_part(p) for p in parts):
        raise VaultError("invalid_mode", f"item path targets a reserved or invalid path: {rel}")
    candidate = (root / rel).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise VaultError("invalid_mode", f"item path is outside the project: {rel}")
    resolved_parts = candidate.relative_to(root).parts
    if any(_reserved_control_part(p) for p in resolved_parts):
        raise VaultError("invalid_mode", f"item path targets reserved project control data: {rel}")
    for parent in candidate.parents:
        if parent == root:
            break
        if parent.exists() and not parent.is_dir():
            raise VaultError("invalid_mode", f"item path parent is not a directory: {rel}")
    return candidate


# ---------------------------------------------------------------------------
# working-file → shadow-tree content
# ---------------------------------------------------------------------------

def content_paths(item: dict[str, Any], project_dir: Path,
                  head_tree: dict[str, bytes],
                  *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> dict[str, bytes]:
    """The content blobs for a generic *item* this commit (carry-forward law).

    Re-reads the working file/folder when it exists (capturing edits); otherwise
    carries the item's existing content forward from *head_tree* verbatim so no
    commit ever drops it. Returns ONLY content paths — the vault adds the
    per-item ``meta.json`` / ``summary.txt`` around this.
    """
    store = doctypes.storage_form(item.get("doc_type"))
    working = contained_path(project_dir, item["path"])
    if store == "link":
        # link.json is written by the link operation; between edits it is simply
        # carried forward from HEAD.
        return _carry_forward(item["item_id"], head_tree)
    if store == "tree":
        if working.is_dir():
            return _snapshot_tree(item["item_id"], working, max_file_bytes)
        return _carry_forward(item["item_id"], head_tree)
    # store == "file"
    if working.is_file():
        return {_file_path(item["item_id"], item["path"]): working.read_bytes()}
    return _carry_forward(item["item_id"], head_tree)


def _file_path(item_id: str, rel: str) -> str:
    return snapshot.item_prefix(item_id) + "file/" + os.path.basename(rel.rstrip("/\\"))


def _snapshot_tree(item_id: str, root: Path, max_file_bytes: int) -> dict[str, bytes]:
    prefix = snapshot.item_prefix(item_id) + "tree/"
    out: dict[str, bytes] = {}
    index: dict[str, dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise_walk_error):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _EXCLUDE_DIRS
            and not any(d.startswith(p) for p in _EXCLUDE_DIR_PREFIXES)
        )
        for fn in sorted(filenames):
            if fn in _EXCLUDE_DIRS:
                continue
            # A symlinked tree member must not read outside the tracked tree.
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            abs_path = contained_path(root, rel)
            size = abs_path.stat().st_size
            if size > max_file_bytes:
                index[rel] = {"size": size, "skipped": True}
                continue
            data = abs_path.read_bytes()
            out[prefix + rel] = data
            index[rel] = {"size": size, "sha": _sha(data)}
    out[snapshot.item_prefix(item_id) + "tree.index.json"] = json.dumps(
        index, indent=2, sort_keys=True).encode("utf-8")
    return out


def _carry_forward(item_id: str, head_tree: dict[str, bytes]) -> dict[str, bytes]:
    """Copy an item's existing CONTENT blobs from *head_tree* verbatim.

    Excludes the regenerated-per-commit siblings (``meta.json`` / ``summary.txt``)
    and never carries a ``geometry.step`` (generic items have none anyway).
    """
    p = snapshot.item_prefix(item_id)
    out: dict[str, bytes] = {}
    for path, data in head_tree.items():
        if not path.startswith(p):
            continue
        tail = path[len(p):]
        if tail in ("meta.json", "summary.txt", "geometry.step"):
            continue
        out[path] = data
    return out


# ---------------------------------------------------------------------------
# link items
# ---------------------------------------------------------------------------

LINK_SCHEMA = "aicad.plm.link/1"


def new_link_doc(url: str, name: str) -> bytes:
    """The initial ``link.json`` for a linked external repository."""
    from services.plm.git_security import reject_location_credentials

    reject_location_credentials(url)
    doc = {"schema": LINK_SCHEMA, "url": url, "name": name, "pins": []}
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def link_path(item_id: str) -> str:
    return snapshot.item_prefix(item_id) + "link.json"


# ---------------------------------------------------------------------------
# restore — re-materialize a generic item's working file/folder from a tree
# ---------------------------------------------------------------------------

def materialize(item: dict[str, Any], src_tree: dict[str, bytes],
                project_dir: Path) -> None:
    """Write a generic item's working file/folder back from a stored *src_tree*.

    File items restore byte-exact; tree items re-write every stored file (extra
    untracked files on disk are left alone). Link items have nothing on disk to
    restore. Never raises for a missing blob — restore is best-effort per item.
    """
    store = doctypes.storage_form(item.get("doc_type"))
    iid = item["item_id"]
    if store == "link":
        return
    if store == "tree":
        prefix = snapshot.item_prefix(iid) + "tree/"
        root = contained_path(project_dir, item["path"])
        for path, data in src_tree.items():
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            dest = contained_path(root, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return
    # single file
    blob = src_tree.get(_file_path(iid, item["path"]))
    if blob is None:
        return
    dest = contained_path(project_dir, item["path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)


# ---------------------------------------------------------------------------
# dirty detection + summaries (deterministic, git-vocab-free — contract §8.1)
# ---------------------------------------------------------------------------

def head_content(item_id: str, head_tree: dict[str, bytes]) -> dict[str, bytes]:
    """An item's content blobs as stored at HEAD (for dirty compare/summary)."""
    return _carry_forward(item_id, head_tree)


def is_dirty(item: dict[str, Any], project_dir: Path,
             head_tree: dict[str, bytes]) -> bool:
    """True when the working file/folder differs from the item's HEAD content."""
    new = content_paths(item, project_dir, head_tree)
    old = head_content(item["item_id"], head_tree)
    return new != old


def summarize(item: dict[str, Any], old: dict[str, bytes],
              new: dict[str, bytes]) -> str:
    """Deterministic plain-language change line for a generic item.

    Numbers are counts of concrete changes (files/bytes) — copied from the
    comparison, never invented. Returns a first-version line when *old* is empty.
    """
    name = os.path.basename(item["path"].rstrip("/\\")) or item["path"]
    doc_type = item.get("doc_type")
    store = doctypes.storage_form(doc_type)
    if not old:
        return f"First version of {name}."
    if old == new:
        return "No changes."
    if store == "tree":
        old_files = {k for k in old if "/tree/" in k}
        new_files = {k for k in new if "/tree/" in k}
        added = len(new_files - old_files)
        removed = len(old_files - set(new_files))
        common = old_files & new_files
        modified = sum(1 for k in common if old[k] != new[k])
        bits = []
        if added:
            bits.append(f"{added} added")
        if modified:
            bits.append(f"{modified} changed")
        if removed:
            bits.append(f"{removed} removed")
        detail = ", ".join(bits) if bits else "updated"
        return f"{detail} in {name}."
    # single file / link: report the byte-size delta (a concrete, copied number)
    old_bytes = sum(len(v) for v in old.values())
    new_bytes = sum(len(v) for v in new.values())
    if store == "link":
        return f"Updated the link for {name}."
    if new_bytes == old_bytes:
        return f"Updated {name}."
    verb = "grew" if new_bytes > old_bytes else "shrank"
    return f"{name} {verb} to {new_bytes} bytes."

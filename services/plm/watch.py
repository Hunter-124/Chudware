"""services.plm.watch — "keywords of interest" watch overlay for the CAD graph.

Agents and users register keywords or node ids of interest; a separate
change-diff consumer flags graph nodes matching the watch entries. Stored per
project at ``<project>/.plm/graph_watch.json`` (schema
``aicad.cad_graph_watch/1``).

Zero-config: a missing file reads as an empty overlay, mirroring the vault ethos.
Watch entries never change the graph, so mutations do not invalidate the graph
cache.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

SCHEMA = "aicad.cad_graph_watch/1"
_ORIGINS = ("manual", "ai")


def _overlay_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".plm" / "graph_watch.json"


def _load(project_dir: str | os.PathLike) -> dict[str, Any]:
    p = _overlay_path(project_dir)
    if not p.is_file():
        return {"schema": SCHEMA, "watch": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"schema": SCHEMA, "watch": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "watch": []}
    watch = data.get("watch")
    if not isinstance(watch, list):
        data["watch"] = []
    data.setdefault("schema", SCHEMA)
    return data


def _save(project_dir: str | os.PathLike, data: dict[str, Any]) -> None:
    p = _overlay_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def list_watch(project_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Return the watch entries (empty when the file is absent)."""
    return list(_load(project_dir).get("watch") or [])


def watch_add(
    project_dir: str | os.PathLike,
    keyword: str = "",
    node_id: str = "",
    note: str = "",
    origin: str = "manual",
) -> dict[str, Any]:
    """Register a keyword or node-id watch. Returns ``{ok, entry}`` or ``{ok:false, reason}``."""
    keyword = str(keyword or "").strip().lower()
    node_id = str(node_id or "").strip()
    if bool(keyword) == bool(node_id):
        return {"ok": False, "reason": "invalid_watch",
                "hint": "exactly one of keyword / node_id is required"}
    origin = origin if origin in _ORIGINS else "manual"
    data = _load(project_dir)
    for entry in data.get("watch") or []:
        if keyword and entry.get("keyword") == keyword:
            return {"ok": True, "entry": entry, "deduped": True}
        if node_id and entry.get("node_id") == node_id:
            return {"ok": True, "entry": entry, "deduped": True}
    entry = {
        "id": "w" + secrets.token_hex(4),
        "note": str(note or ""),
        "origin": origin,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if keyword:
        entry["keyword"] = keyword
    else:
        entry["node_id"] = node_id
    data.setdefault("watch", []).append(entry)
    _save(project_dir, data)
    return {"ok": True, "entry": entry}


def watch_remove(
    project_dir: str | os.PathLike, entry_id: str,
) -> dict[str, Any]:
    """Remove a watch entry by id. Returns ``{ok, removed}``."""
    entry_id = str(entry_id or "").strip()
    data = _load(project_dir)
    entries = data.get("watch") or []
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return {"ok": False, "reason": "not_found",
                "hint": f"no watch entry {entry_id!r}"}
    data["watch"] = kept
    _save(project_dir, data)
    return {"ok": True, "removed": entry_id}


def watched_matches(
    graph: dict[str, Any], watch_entries: list[dict[str, Any]],
) -> set[str]:
    """Return the ids of ``graph`` nodes matched by any watch entry.

    A node matches when (a) its id equals a ``node_id`` watch, (b) its id equals
    ``kw:<keyword>`` for a keyword watch, or (c) one of its ``meta.keywords``
    equals a keyword watch. Robust to missing ``meta``.
    """
    keywords = {e.get("keyword") for e in watch_entries or [] if e.get("keyword")}
    node_ids = {e.get("node_id") for e in watch_entries or [] if e.get("node_id")}
    kw_node_ids = {"kw:" + k for k in keywords}
    out: set[str] = set()
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not nid:
            continue
        if nid in node_ids or nid in kw_node_ids:
            out.add(nid)
            continue
        meta = node.get("meta") or {}
        node_kws = meta.get("keywords") or []
        if any(k in keywords for k in node_kws):
            out.add(nid)
    return out

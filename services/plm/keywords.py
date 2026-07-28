"""services.plm.keywords — keyword tags on CAD graph targets.

A persisted overlay of keyword tags on graph targets (node ids or
project-relative file paths) that :mod:`services.plm.cad_graph` consumers
merge on top of the deterministically extracted graph. Stored per project at
``<project>/.plm/keywords.json`` (schema ``aicad.cad_graph_keywords/1``).

Zero-config: a missing file reads as an empty overlay, mirroring the vault ethos.
Deterministic extraction is the source of truth; keywords are additive and never
rewrite the model.

Mutations call :func:`services.plm.cad_graph.invalidate_cache` so the next
``cad_graph_get`` rebuilds.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from services.plm.cad_graph import invalidate_cache

SCHEMA = "aicad.cad_graph_keywords/1"
_ORIGINS = ("manual", "ai")
_MAX_KEYWORD = 64


def _overlay_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".plm" / "keywords.json"


def _load(project_dir: str | os.PathLike) -> dict[str, Any]:
    p = _overlay_path(project_dir)
    if not p.is_file():
        return {"schema": SCHEMA, "keywords": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"schema": SCHEMA, "keywords": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "keywords": []}
    keywords = data.get("keywords")
    if not isinstance(keywords, list):
        data["keywords"] = []
    data.setdefault("schema", SCHEMA)
    return data


def _save(project_dir: str | os.PathLike, data: dict[str, Any]) -> None:
    p = _overlay_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def _normalize(keyword: str) -> str:
    return str(keyword or "").strip().lower()


def list_entries(project_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Return the keyword entries (empty when the file is absent)."""
    return list(_load(project_dir).get("keywords") or [])


def add_keyword(
    project_dir: str | os.PathLike,
    target: str,
    keyword: str,
    origin: str = "manual",
    author: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tag ``target`` with ``keyword``. Returns ``{ok, entry}`` or ``{ok:false, reason}``."""
    keyword = _normalize(keyword)
    if not keyword or len(keyword) > _MAX_KEYWORD:
        return {"ok": False, "reason": "invalid_keyword",
                "hint": f"keyword must be 1..{_MAX_KEYWORD} chars after strip/lower"}
    target = str(target or "").strip()
    if not target:
        return {"ok": False, "reason": "invalid_target",
                "hint": "target must be a non-empty node id or project-relative path"}
    origin = origin if origin in _ORIGINS else "manual"
    data = _load(project_dir)
    for entry in data.get("keywords") or []:
        if entry.get("target") == target and entry.get("keyword") == keyword:
            return {"ok": True, "entry": entry, "deduped": True}
    entry = {
        "id": "k" + secrets.token_hex(4),
        "target": target,
        "keyword": keyword,
        "origin": origin,
        "author": author or {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data.setdefault("keywords", []).append(entry)
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "entry": entry}


def remove_keyword(
    project_dir: str | os.PathLike, entry_id: str,
) -> dict[str, Any]:
    """Remove a keyword entry by id. Returns ``{ok, removed}``."""
    entry_id = str(entry_id or "").strip()
    data = _load(project_dir)
    entries = data.get("keywords") or []
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return {"ok": False, "reason": "not_found",
                "hint": f"no keyword entry {entry_id!r}"}
    data["keywords"] = kept
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "removed": entry_id}


def remove_pair(
    project_dir: str | os.PathLike, target: str, keyword: str,
) -> dict[str, Any]:
    """Remove every entry matching ``(target, keyword)``. Returns ``{ok, removed}``."""
    target = str(target or "").strip()
    keyword = _normalize(keyword)
    data = _load(project_dir)
    entries = data.get("keywords") or []
    kept = [e for e in entries
            if not (e.get("target") == target and e.get("keyword") == keyword)]
    removed = len(entries) - len(kept)
    if removed == 0:
        return {"ok": False, "reason": "not_found",
                "hint": f"no keyword {keyword!r} on target {target!r}"}
    data["keywords"] = kept
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "removed": removed}


def keywords_for(
    project_dir: str | os.PathLike, targets: list[str],
) -> dict[str, list[str]]:
    """Map each of ``targets`` to its sorted keyword list (empty when untagged)."""
    out: dict[str, set[str]] = {}
    for t in targets or []:
        t = str(t or "").strip()
        if t:
            out.setdefault(t, set())
    for entry in _load(project_dir).get("keywords") or []:
        target = entry.get("target")
        keyword = entry.get("keyword")
        if target in out and keyword:
            out[target].add(keyword)
    return {t: sorted(kws) for t, kws in out.items()}

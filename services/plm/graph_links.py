"""services.plm.graph_links — manual / AI-authored CAD graph overlay.

A persisted overlay of hand- or AI-authored links that :mod:`services.plm.cad_graph`
merges on top of the deterministically extracted graph. Stored per project at
``<project>/.plm/graph_links.json`` (schema ``aicad.cad_graph_links/1``).

Zero-config: a missing file reads as an empty overlay, mirroring the vault ethos.
Deterministic extraction is the source of truth; these links are additive and never
rewrite the model.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from services.plm.cad_graph import EDGE_KINDS, invalidate_cache

SCHEMA = "aicad.cad_graph_links/1"
_ORIGINS = ("manual", "ai")


def _overlay_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".plm" / "graph_links.json"


def _load(project_dir: str | os.PathLike) -> dict[str, Any]:
    p = _overlay_path(project_dir)
    if not p.is_file():
        return {"schema": SCHEMA, "links": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"schema": SCHEMA, "links": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "links": []}
    links = data.get("links")
    if not isinstance(links, list):
        data["links"] = []
    data.setdefault("schema", SCHEMA)
    return data


def _save(project_dir: str | os.PathLike, data: dict[str, Any]) -> None:
    p = _overlay_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def list_links(project_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Return the overlay links (empty when the file is absent)."""
    return list(_load(project_dir).get("links") or [])


def add_link(
    project_dir: str | os.PathLike,
    src: str,
    dst: str,
    kind: str,
    note: str = "",
    origin: str = "manual",
    author: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Author a new overlay link. Returns ``{ok, link}`` or ``{ok:false, reason}``."""
    src = str(src or "").strip()
    dst = str(dst or "").strip()
    kind = str(kind or "").strip()
    if kind not in EDGE_KINDS:
        return {"ok": False, "reason": "invalid_kind",
                "hint": f"kind must be one of {sorted(EDGE_KINDS)}"}
    if not src or not dst or src == dst:
        return {"ok": False, "reason": "invalid_link",
                "hint": "src and dst must be distinct non-empty node ids"}
    origin = origin if origin in _ORIGINS else "manual"
    data = _load(project_dir)
    link = {
        "id": "l" + secrets.token_hex(4),
        "src": src, "dst": dst, "kind": kind,
        "note": str(note or ""),
        "origin": origin,
        "author": author or {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data.setdefault("links", []).append(link)
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "link": link}


def remove_link(
    project_dir: str | os.PathLike, link_id: str,
) -> dict[str, Any]:
    """Remove an overlay link by id. Returns ``{ok, removed}``."""
    link_id = str(link_id or "").strip()
    data = _load(project_dir)
    links = data.get("links") or []
    kept = [lk for lk in links if lk.get("id") != link_id]
    if len(kept) == len(links):
        return {"ok": False, "reason": "not_found",
                "hint": f"no link {link_id!r}"}
    data["links"] = kept
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "removed": link_id}

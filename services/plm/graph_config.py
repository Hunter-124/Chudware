"""services.plm.graph_config — per-project CAD graph configuration.

Stored at ``<project>/.plm/graph_config.json`` (schema
``aicad.cad_graph_config/1``). Zero-config: a missing file reads as the
defaults. Gates office-content extraction and lists linked projects for
cross-project graph merges.

Mutations call :func:`services.plm.cad_graph.invalidate_cache` so the next
``cad_graph_get`` rebuilds with the new configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from services.plm.cad_graph import invalidate_cache

SCHEMA = "aicad.cad_graph_config/1"
DEFAULTS: dict[str, Any] = {
    "office_content": False,
    "office_max_chars": 4000,
    "office_kinds": ["pdf", "docx", "xlsx", "pptx", "csv", "doc", "xlsm"],
    "linked_projects": [],
}
_KNOWN_KEYS = tuple(DEFAULTS) + ("project_key",)
_MAX_CHARS_RANGE = (200, 200000)


def _config_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".plm" / "graph_config.json"


def _load(project_dir: str | os.PathLike) -> dict[str, Any]:
    p = _config_path(project_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _save(project_dir: str | os.PathLike, data: dict[str, Any]) -> None:
    p = _config_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["schema"] = SCHEMA
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def _resolve_project_path(project_dir: str | os.PathLike, path: str) -> Path:
    p = Path(str(path))
    if not p.is_absolute():
        p = Path(project_dir) / p
    return p.resolve()


def get_config(project_dir: str | os.PathLike) -> dict[str, Any]:
    """Return the graph config: ``DEFAULTS`` merged with the on-disk file.

    Unknown on-disk keys are ignored.
    """
    cfg = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
    data = _load(project_dir)
    for key in _KNOWN_KEYS:
        if key in data:
            cfg[key] = data[key]
    return cfg


def _validate(project_dir: str | os.PathLike, fields: dict[str, Any]) -> dict[str, Any]:
    """Validate ``fields``; returns the normalized values or an error dict."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in _KNOWN_KEYS:
            return {"ok": False, "reason": "invalid_field",
                    "hint": f"unknown field {key!r}; valid: {list(_KNOWN_KEYS)}"}
        if key == "office_content":
            if not isinstance(value, bool):
                return {"ok": False, "reason": "invalid_field",
                        "hint": "office_content must be a bool"}
            out[key] = value
        elif key == "office_max_chars":
            lo, hi = _MAX_CHARS_RANGE
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                return {"ok": False, "reason": "invalid_field",
                        "hint": f"office_max_chars must be an int in {lo}..{hi}"}
            out[key] = value
        elif key == "office_kinds":
            if (not isinstance(value, list)
                    or any(not isinstance(k, str) or not k.isalpha() or k != k.lower()
                           for k in value)):
                return {"ok": False, "reason": "invalid_field",
                        "hint": "office_kinds must be a list of lowercase alpha strings"}
            out[key] = list(value)
        elif key == "linked_projects":
            if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
                return {"ok": False, "reason": "invalid_field",
                        "hint": "linked_projects must be a list of path strings"}
            resolved = []
            for raw in value:
                p = _resolve_project_path(project_dir, raw)
                if not p.exists():
                    return {"ok": False, "reason": "invalid_project",
                            "hint": f"linked project {raw!r} does not exist"}
                resolved.append(str(p))
            out[key] = resolved
        elif key == "project_key":
            if not isinstance(value, str) or not 2 <= len(value) <= 32 or not value.isalnum():
                return {"ok": False, "reason": "invalid_field",
                        "hint": "project_key must be an alphanumeric string of 2..32 chars"}
            out[key] = value
    return {"ok": True, "fields": out}


def set_config(project_dir: str | os.PathLike, **fields: Any) -> dict[str, Any]:
    """Merge validated ``fields`` into the on-disk config. Returns ``{ok, config}``."""
    checked = _validate(project_dir, fields)
    if not checked["ok"]:
        return checked
    data = _load(project_dir)
    data.update(checked["fields"])
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "config": get_config(project_dir)}


def add_linked_project(project_dir: str | os.PathLike, path: str) -> dict[str, Any]:
    """Add ``path`` to ``linked_projects`` (deduped by resolved absolute path)."""
    p = _resolve_project_path(project_dir, path)
    if not p.exists():
        return {"ok": False, "reason": "invalid_project",
                "hint": f"linked project {path!r} does not exist"}
    data = _load(project_dir)
    linked = [str(_resolve_project_path(project_dir, e))
              for e in data.get("linked_projects") or []]
    if str(p) not in linked:
        linked.append(str(p))
        data["linked_projects"] = linked
        _save(project_dir, data)
        invalidate_cache(project_dir)
    return {"ok": True, "config": get_config(project_dir)}


def remove_linked_project(project_dir: str | os.PathLike, path: str) -> dict[str, Any]:
    """Remove ``path`` from ``linked_projects``. Returns ``{ok, config}``."""
    p = str(_resolve_project_path(project_dir, path))
    data = _load(project_dir)
    linked = [str(_resolve_project_path(project_dir, e))
              for e in data.get("linked_projects") or []]
    if p not in linked:
        return {"ok": False, "reason": "not_found",
                "hint": f"linked project {path!r} is not registered"}
    linked.remove(p)
    data["linked_projects"] = linked
    _save(project_dir, data)
    invalidate_cache(project_dir)
    return {"ok": True, "config": get_config(project_dir)}


def config_mtime(project_dir: str | os.PathLike) -> float:
    """mtime of the config file (0.0 when absent or unreadable)."""
    try:
        return _config_path(project_dir).stat().st_mtime
    except OSError:
        return 0.0


def project_key(project_dir: str | os.PathLike) -> str:
    """Configured ``project_key``, else a stable key derived from the project path."""
    key = get_config(project_dir).get("project_key")
    if key:
        return str(key)
    digest = hashlib.sha1(str(Path(project_dir).resolve()).encode()).hexdigest()
    return "p" + digest[:8]

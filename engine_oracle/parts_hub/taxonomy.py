"""Parts Library V2 category taxonomy — loader + inheritance + facet matching.

The taxonomy is data-driven (``taxonomy.json`` next to this module): a tree of
category nodes with per-node ``domains`` / ``sites`` / ``query_terms`` routing
hints and ``facets`` (typed filter definitions). Routing lists EXTEND down the
tree (parent values first, child additions appended, deduplicated); facets
INHERIT down with child definitions overriding parents by ``key``.

Facet types (contracts/parts.md §7):
    range  {key,label,type:"range",unit?}   — query gives {min?,max?} (or scalar)
    enum   {key,label,type:"enum",values[]} — query gives value or list, case-insensitive
    text   {key,label,type:"text"}          — query gives substring, case-insensitive

Everything degrades gracefully: a missing/corrupt taxonomy.json logs a warning
and falls back to a minimal built-in flat tree (the legacy domain categories),
so the GUI never sees a traceback.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TAXONOMY_PATH = Path(__file__).with_name("taxonomy.json")

_FACET_TYPES = ("range", "enum", "text")

# Legacy flat domains — fallback tree when taxonomy.json is missing/corrupt.
# Mirrors gui_rpc._DOMAIN_LABELS (kept literal here to avoid an import cycle).
_FALLBACK_DOMAINS: dict[str, str] = {
    "fastener":    "Fasteners",
    "bearing":     "Bearings",
    "mech_family": "Mechanical Families",
    "section":     "Structural Sections",
    "fitting":     "Pipe Fittings",
    "hydraulic":   "Hydraulics",
    "ecad":        "Electronics (ECAD)",
    "motor":       "Electric Motors",
    "motion":      "Motion Control",
    "power_supply": "Power Supplies",
    "battery":     "Batteries",
    "seal":        "Seals & Gaskets",
    "electromech": "Electro-Mechanical",
}

_INHERIT_LISTS = ("domains", "sites", "query_terms")

# ---------------------------------------------------------------------------
# Load + validate (module-level cache)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict | None = None          # validated taxonomy document
_by_id: dict[str, dict] = {}        # node id -> node
_parent: dict[str, str | None] = {} # node id -> parent id (None for roots)


def _fallback_tree() -> dict:
    return {
        "version": 1,
        "categories": [
            {"id": dom, "label": lbl, "icon": "", "description": "",
             "domains": [dom], "sites": [], "query_terms": [], "facets": [], "children": []}
            for dom, lbl in _FALLBACK_DOMAINS.items()
        ],
    }


def _validate(doc: Any) -> dict:
    """Validate the taxonomy document. Raises ValueError on structural problems."""
    if not isinstance(doc, dict) or not isinstance(doc.get("categories"), list):
        raise ValueError("taxonomy root must be {'version':int,'categories':[...]}")
    seen: set[str] = set()

    def _walk(node: Any, parent_id: str | None) -> None:
        if not isinstance(node, dict):
            raise ValueError(f"node under {parent_id!r} is not an object")
        nid = node.get("id")
        if not nid or not isinstance(nid, str):
            raise ValueError(f"node under {parent_id!r} missing string id")
        if nid in seen:
            raise ValueError(f"duplicate category id {nid!r}")
        seen.add(nid)
        for key in _INHERIT_LISTS:
            v = node.get(key)
            if v is not None and not isinstance(v, list):
                raise ValueError(f"{nid}: {key} must be a list")
        for f in node.get("facets") or []:
            if not isinstance(f, dict) or not f.get("key"):
                raise ValueError(f"{nid}: facet missing key")
            if f.get("type") not in _FACET_TYPES:
                raise ValueError(f"{nid}: facet {f.get('key')!r} has illegal type {f.get('type')!r}")
            if f.get("type") == "enum" and not isinstance(f.get("values"), list):
                raise ValueError(f"{nid}: enum facet {f['key']!r} missing values[]")
        for child in node.get("children") or []:
            _walk(child, nid)

    for cat in doc["categories"]:
        _walk(cat, None)
    return doc


def _build_index(doc: dict) -> None:
    _by_id.clear()
    _parent.clear()

    def _walk(node: dict, parent_id: str | None) -> None:
        _by_id[node["id"]] = node
        _parent[node["id"]] = parent_id
        for child in node.get("children") or []:
            _walk(child, node["id"])

    for cat in doc.get("categories", []):
        _walk(cat, None)


def load_taxonomy() -> dict:
    """Load, validate and cache taxonomy.json.

    On a missing or corrupt file this logs a warning and returns a minimal
    built-in fallback tree (the legacy flat domain categories) — never raises.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            doc = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
            _cache = _validate(doc)
        except Exception as exc:  # noqa: BLE001 — degrade, never break the worker
            log.warning("parts taxonomy unavailable (%s: %s) — using built-in fallback",
                        TAXONOMY_PATH, exc)
            _cache = _fallback_tree()
        _build_index(_cache)
        return _cache


def _reset_cache() -> None:
    """Test hook: forget the cached taxonomy so the next load re-reads the file."""
    global _cache
    with _lock:
        _cache = None
        _by_id.clear()
        _parent.clear()


# ---------------------------------------------------------------------------
# Tree navigation
# ---------------------------------------------------------------------------

def get_node(category_id: str) -> dict | None:
    """Return the raw taxonomy node for ``category_id`` (or None)."""
    load_taxonomy()
    return _by_id.get(category_id)


def parent_of(category_id: str) -> dict | None:
    """Return the parent node of ``category_id`` (None for roots / unknown ids)."""
    load_taxonomy()
    pid = _parent.get(category_id)
    return _by_id.get(pid) if pid else None


def descendant_ids(category_id: str) -> set[str]:
    """All ids in the subtree rooted at ``category_id`` (including itself).

    Unknown ids return an empty set.
    """
    node = get_node(category_id)
    if node is None:
        return set()
    out: set[str] = set()

    def _walk(n: dict) -> None:
        out.add(n["id"])
        for child in n.get("children") or []:
            _walk(child)

    _walk(node)
    return out


def _ancestry(category_id: str) -> list[dict]:
    """Nodes from root down to ``category_id`` (empty for unknown ids)."""
    load_taxonomy()
    chain: list[dict] = []
    nid: str | None = category_id
    while nid is not None and nid in _by_id:
        chain.append(_by_id[nid])
        nid = _parent.get(nid)
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------

def effective_facets(category_id: str) -> list[dict]:
    """Merged facet definitions root→leaf; a child facet overrides its parent by key."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for node in _ancestry(category_id):
        for f in node.get("facets") or []:
            key = f.get("key")
            if not key:
                continue
            if key not in merged:
                order.append(key)
            merged[key] = f
    return [copy.deepcopy(merged[k]) for k in order]


def route(category_id: str) -> dict:
    """Merged routing hints for a category: ``{"domains":[], "sites":[], "query_terms":[]}``.

    Lists extend root→leaf (parent values first), deduplicated preserving order.
    Unknown ids return empty lists — callers degrade to unrouted behavior.
    """
    out: dict[str, list] = {k: [] for k in _INHERIT_LISTS}
    for node in _ancestry(category_id):
        for key in _INHERIT_LISTS:
            for v in node.get(key) or []:
                if v not in out[key]:
                    out[key].append(v)
    return out


# ---------------------------------------------------------------------------
# Facet matching
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _num(value: Any) -> float | None:
    """Best-effort numeric parse with unit-suffix tolerance ("6.35 mm" → 6.35)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _match_range(param_value: Any, query: Any) -> bool:
    if isinstance(query, dict):
        lo = _num(query.get("min"))
        hi = _num(query.get("max"))
    else:  # scalar tolerance: treat as exact value
        lo = hi = _num(query)
    if lo is None and hi is None:
        return True  # empty range query — no constraint
    for pv in _as_list(param_value):
        v = _num(pv)
        if v is None:
            continue
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        return True
    return False


def _match_enum(param_value: Any, query: Any) -> bool:
    wanted = {str(q).strip().lower() for q in _as_list(query) if str(q).strip()}
    if not wanted:
        return True
    have = {str(pv).strip().lower() for pv in _as_list(param_value)}
    return bool(wanted & have)


def _match_text(param_value: Any, query: Any) -> bool:
    needle = str(query).strip().lower()
    if not needle:
        return True
    return any(needle in str(pv).lower() for pv in _as_list(param_value))


def match_facets(params: dict, facets_query: dict, facet_defs: list[dict]) -> bool:
    """True when a record's ``params`` satisfy every queried facet.

    - range: numeric parse with unit-suffix tolerance; query is {min?,max?} (or scalar).
    - enum: case-insensitive membership; query is a value or a list of values.
    - text: case-insensitive substring.
    - A queried facet whose key is missing from ``params`` → non-match.
    - Param values that are lists match when ANY element matches.
    - Facet keys with no definition fall back to text matching.
    """
    if not facets_query:
        return True
    params = params or {}
    defs = {f.get("key"): f for f in (facet_defs or []) if isinstance(f, dict)}
    for key, query in facets_query.items():
        if query is None or (isinstance(query, str) and not query.strip()):
            continue  # empty query for this facet — no constraint
        if key not in params:
            return False  # facet was queried but the record has no such param
        ftype = (defs.get(key) or {}).get("type", "text")
        value = params[key]
        if ftype == "range":
            ok = _match_range(value, query)
        elif ftype == "enum":
            ok = _match_enum(value, query)
        else:
            ok = _match_text(value, query)
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# GUI projection
# ---------------------------------------------------------------------------

def _gui_node(node: dict) -> dict:
    return {
        "id":          node["id"],
        "label":       node.get("label", node["id"]),
        "icon":        node.get("icon", ""),
        "description": node.get("description", ""),
        "facets":      effective_facets(node["id"]),
        "children":    [_gui_node(c) for c in node.get("children") or []],
    }


def flat_tree_for_gui(category_id: str | None = None) -> list[dict]:
    """Category tree in the GUI/RPC shape — every node carries its EFFECTIVE facets.

    Returns the list of root categories, or ``[subtree]`` when ``category_id``
    names a node (empty list for unknown ids).
    """
    doc = load_taxonomy()
    if category_id:
        node = _by_id.get(category_id)
        return [_gui_node(node)] if node is not None else []
    return [_gui_node(c) for c in doc.get("categories", [])]

"""Worker-RPC bridge for the parts-library seam (T-U2).

Exposes ``part_search``, ``part_get``, and ``part_place`` as **engine-worker methods** so the
C++/ImGui GUI reaches them through the same newline-delimited-JSON worker seam (``WorkerClient``
/ ``AsyncRpc``, ``services/worker/``) it already uses for every other service — no bespoke
transport. These are thin wrappers over :mod:`engine_oracle.parts_hub` and
:mod:`engine_oracle.parts_hub.place`; the heavy FTS work stays inside the hub.

Each method degrades gracefully when the backing data is absent (returns an empty result list /
ok:false envelope, never raises). Registered append-only in ``engine_oracle/worker.py``.
"""
from __future__ import annotations

from typing import Callable


def _part_search(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_search`` → ranked PartRecord list."""
    try:
        from engine_oracle.parts_hub import search as _search
        query = (params or {}).get("query", "")
        domain = (params or {}).get("domain") or None
        limit = int((params or {}).get("limit", 20))
        results = _search(query, domain=domain, limit=max(1, min(limit, 200)))
        has_live = any(r.get("source") == "live" for r in results)
        return {"ok": True, "count": len(results), "has_live": has_live, "results": results}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_search failed: {exc}", "hint": ""}


def _part_get(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_get`` → single PartRecord or ok:false."""
    try:
        from engine_oracle.parts_hub import get as _get
        part_id = (params or {}).get("part_id", "")
        rec = _get(part_id)
        if rec is None:
            return {"ok": False, "reason": f"part_id not found: {part_id!r}",
                    "hint": "Use part_search to discover valid part_ids."}
        return {"ok": True, "part": rec}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_get failed: {exc}", "hint": ""}


def _part_place(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_place`` → PlacementResult (editable, R1)."""
    try:
        from engine_oracle.parts_hub.place import place as _place
        part_id = (params or {}).get("part_id", "")
        doc = (params or {}).get("doc") or None
        extra = (params or {}).get("params") or None
        return _place(part_id, doc=doc, params=extra)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_place failed: {exc}",
                "hint": "Check part_id with part_get first."}


# Registered by engine_oracle/worker.py via ``_METHODS.update(WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "part_search": _part_search,
    "part_get": _part_get,
    "part_place": _part_place,
}

# GUI-shaped methods for the Parts Library workspace (host/src/ui/parts/) —
# categories, filtered search, vendor login/status, scrape queue, thumbnails.
try:
    from engine_oracle.parts_hub.gui_rpc import GUI_WORKER_METHODS as _GUI_METHODS
    WORKER_METHODS.update(_GUI_METHODS)
except Exception:  # noqa: BLE001 — GUI seam is optional; agent tools above still register
    pass

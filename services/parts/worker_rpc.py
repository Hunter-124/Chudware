"""Worker-RPC bridge for the parts-library seam.

Exposes ``part_search``, ``part_get``, and ``part_place`` as **engine-worker methods** so the
C++/ImGui GUI reaches them through the same newline-delimited-JSON worker seam (``WorkerClient``
/ ``AsyncRpc``, ``services/worker/``) it already uses for every other service — no bespoke
transport. These are thin wrappers over :mod:`services.parts` and
:mod:`engine_oracle.parts_hub.place`; the heavy FTS work stays inside the hub.

Each method degrades gracefully when the backing data is absent (returns an empty result list /
ok:false envelope, never raises). Registered append-only in ``engine_oracle/worker.py``.

Re-homed from ``engine_oracle/parts_hub/worker_rpc.py``. The squatter
RPC tables that used to be folded in here (subscription/session/skills/triggers/
role_models/bench) now live in :mod:`services.agent.worker_rpc`, and the PLM
tables (cad_graph/plm_branch) in :mod:`services.plm.worker_rpc`; each is
registered from its own block in ``engine_oracle/worker.py``.
"""
from __future__ import annotations

from typing import Callable


def _part_search(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_search`` → ranked PartRecord list."""
    try:
        from services.parts import search as _search
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
        from services.parts import get as _get
        part_id = (params or {}).get("part_id", "")
        rec = _get(part_id)
        if rec is None:
            return {"ok": False, "reason": f"part_id not found: {part_id!r}",
                    "hint": "Use part_search to discover valid part_ids."}
        return {"ok": True, "part": rec}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_get failed: {exc}", "hint": ""}


def _part_place(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_place`` → PlacementResult (editable, R1).

    place.py was ported to core/parts — the hub lookup stays here
    (Python FTS), the placement runs in the C++ core."""
    try:
        from services.mcp import core_bridge as cb
        from services.parts import get as _hub_get
        part_id = (params or {}).get("part_id", "")
        record = _hub_get(part_id)
        if record is None:
            return {"ok": False, "reason": f"unknown part_id: {part_id}",
                    "hint": "Check part_id with part_get first."}
        return cb.call("part_place", {"part_id": part_id, "record": record,
                                      "doc": (params or {}).get("doc") or None,
                                      "params": (params or {}).get("params") or None})
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
# categories, filtered search (``parts_search``), index build state
# (``parts_index_status``), vendor login/status, fetch queue, thumbnails. The
# ``parts_*`` table itself lives in gui_rpc.GUI_WORKER_METHODS (one source of
# truth) and is merged into WORKER_METHODS here.
try:
    from .gui_rpc import GUI_WORKER_METHODS as _GUI_METHODS
    WORKER_METHODS.update(_GUI_METHODS)
except Exception:  # noqa: BLE001 — GUI seam is optional; agent tools above still register
    pass

# Embedded-browser seam (Parts Library V2 workstream 3, generic/reusable) —
# browser_start_url + login_wizard_*. Thin wrappers over services.browser.embed
# (in-process CEF browser); degrade to ok:false envelopes when Playwright isn't
# provisioned. Re-homed from engine_oracle/embed_rpc.py.
try:
    from .embed_rpc import EMBED_WORKER_METHODS as _EMBED_METHODS
    WORKER_METHODS.update(_EMBED_METHODS)
except Exception as _embed_err:  # noqa: BLE001 — embed seam is optional; methods above still register
    import logging
    logging.getLogger("chudware.worker").warning(
        "embed_rpc unavailable — embed methods not registered: %s", _embed_err)

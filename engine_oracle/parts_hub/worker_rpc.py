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
        from ..parts_hub import search as _search
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
        from ..parts_hub import get as _get
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
        from ..parts_hub.place import place as _place
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
# categories, filtered search, vendor login/status, fetch queue, thumbnails.
try:
    from ..parts_hub.gui_rpc import GUI_WORKER_METHODS as _GUI_METHODS
    WORKER_METHODS.update(_GUI_METHODS)
except Exception:  # noqa: BLE001 — GUI seam is optional; agent tools above still register
    pass

# Embedded-browser seam (Parts Library V2 workstream 3, generic/reusable) —
# browser_embed_start/frame/input/navigate/stop + login_wizard_*. Thin wrappers
# over services.browser.embed (CDP screencast); degrade to ok:false envelopes
# when Playwright isn't provisioned.
try:
    from ..embed_rpc import EMBED_WORKER_METHODS as _EMBED_METHODS
    WORKER_METHODS.update(_EMBED_METHODS)
except Exception as _embed_err:  # noqa: BLE001 — embed seam is optional; methods above still register
    import logging
    logging.getLogger("ai-cad.worker").warning(
        "embed_rpc unavailable — embed methods not registered: %s", _embed_err)

# Subscription-auth login methods (§4.4) for the Settings panel.
try:
    from ..parts_hub.subscription_rpc import SUBSCRIPTION_METHODS as _SUB_METHODS
    WORKER_METHODS.update(_SUB_METHODS)
except Exception as _sub_err:  # noqa: BLE001 — optional seam; core methods still register.
    # NEVER a bare pass: a silent swallow here hid the engine_oracle-alias import break
    # in deployed payloads (Settings Accounts showed "unknown method: subscription_*").
    import logging
    logging.getLogger("ai-cad.worker").warning(
        "subscription_rpc unavailable — subscription_* methods not registered: %s", _sub_err)

# GUI-launched gym benchmarks (Settings → AI Model Leaderboard).
try:
    from ..parts_hub.bench_rpc import BENCH_METHODS as _BENCH_METHODS
    WORKER_METHODS.update(_BENCH_METHODS)
except Exception as _bench_err:  # noqa: BLE001 — optional seam; core methods still register
    import logging
    logging.getLogger("ai-cad.worker").warning(
        "bench_rpc unavailable — bench_* methods not registered: %s", _bench_err)

# WS2: per-role model assignment + rankings (Settings ModelRolesAuto).
try:
    from ..parts_hub.role_models_rpc import ROLE_MODELS_METHODS as _ROLE_METHODS
    WORKER_METHODS.update(_ROLE_METHODS)
except Exception:  # noqa: BLE001 — optional seam; core methods still register
    pass

# CAD project graph (contracts/cad_graph.md v1) — get / blast / path.
try:
    from ..parts_hub.cad_graph_rpc import CAD_GRAPH_METHODS as _CAD_GRAPH_METHODS
    WORKER_METHODS.update(_CAD_GRAPH_METHODS)
except Exception:  # noqa: BLE001 — optional seam; core methods still register
    pass

# Copilot skills (contracts/skills.md v1).
try:
    from ..parts_hub.skills_rpc import SKILLS_METHODS as _SKILLS_METHODS
    WORKER_METHODS.update(_SKILLS_METHODS)
except Exception:  # noqa: BLE001 — optional seam; core methods still register
    pass

# PLM lightweight branching (wave-2) — create / list / switch / merge.
try:
    from ..parts_hub.plm_branch_rpc import PLM_BRANCH_METHODS as _PLM_BRANCH_METHODS
    WORKER_METHODS.update(_PLM_BRANCH_METHODS)
except Exception:  # noqa: BLE001 — optional seam; core methods still register
    pass




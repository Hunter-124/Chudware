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
import logging

log = logging.getLogger("chudware.worker")
REGISTRATION_DIAGNOSTICS: list[dict] = []


def _part_search(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_search`` → ranked PartRecord list."""
    try:
        from services.parts import search as _search
        query = (params or {}).get("query", "")
        domain = (params or {}).get("domain") or None
        limit = int((params or {}).get("limit", 20))
        kwargs = {"domain": domain, "limit": max(1, min(limit, 200)),
                  "include_live": (params or {}).get("include_live", True)}
        if (params or {}).get("include_live") is True:
            kwargs["force_live"] = True
        results = _search(query, **kwargs)
        has_live = any(r.get("source") == "live" for r in results)
        return {"ok": True, "count": len(results), "has_live": has_live, "results": results}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_search failed: {exc}", "hint": ""}


def _part_get(params: dict, _emit: Callable) -> dict:
    """Worker method ``part_get`` → single PartRecord or ok:false."""
    try:
        from services.parts import get as _get
        part_id = (params or {}).get("part_id", "")
        acquire = (params or {}).get("acquire", False)
        if acquire:
            from services.agent.request_context import in_cell
            if in_cell():
                return {"ok": False, "reason": "Use part_acquire to acquire geometry in a cell."}
        rec = _get(part_id, include_live=(params or {}).get("include_live", True),
                   acquire=acquire)
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
        aliases = ("doc_id", "document_id", "target_doc", "target_doc_id",
                   "target_document", "target_document_id", "assembly_id")
        if any((params or {}).get(key) not in (None, "") for key in aliases):
            return {"ok": False, "reason": "Existing target document aliases are unsupported; use doc with a document object where supported."}
        from services.mcp import core_bridge as cb
        from services.parts import get as _hub_get
        part_id = (params or {}).get("part_id", "")
        record = _hub_get(part_id, include_live=(params or {}).get("include_live", True),
                          acquire=True)
        if record is None:
            return {"ok": False, "reason": f"unknown part_id: {part_id}",
                    "hint": "Check part_id with part_get first."}
        part_id = str(record.get("part_id") or part_id)
        kind = str((record.get("geometry_ref") or {}).get("kind", ""))
        if kind.startswith("parametric_"):
            if (params or {}).get("doc") not in (None, "", {}):
                return {"ok": False, "reason": "Existing target documents are not supported for mechanical part placement."}
            overrides = (params or {}).get("params") or {}
            target_keys = {"docid", "documentid", "targetdoc", "targetdocid",
                           "targetdocument", "targetdocumentid", "assemblyid"}
            if any(str(key).lower().replace("_", "").replace("-", "") in target_keys
                   for key in overrides):
                return {"ok": False, "reason": "Existing target documents are not supported for mechanical part placement."}
            from services.mcp.parts_tools import place_parametric_impl
            result = place_parametric_impl(part_id, record, overrides)
            if result:
                return result
        from services.parts.placement import placement_record
        record = placement_record(record)
        return cb.call("part_place", {"part_id": part_id, "record": record,
                                      "doc": (params or {}).get("doc") or None,
                                      "params": (params or {}).get("params") or None})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_place failed: {exc}",
                "hint": "Check part_id with part_get first."}


def _part_library_ingest(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent.request_context import cell_path
        from services.parts.parts_library import ingest_manifest
        p = params or {}
        manifest = str(cell_path(p["manifest_path"]))
        root = str(cell_path(p["artifact_root"])) if p.get("artifact_root") else None
        return ingest_manifest(manifest, artifact_root=root, mirrors=p.get("mirrors"),
                               replace_namespace=p.get("replace_namespace", False))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_library_ingest failed: {exc}"}


def _part_library_sync(params: dict, _emit: Callable) -> dict:
    try:
        from services.parts.parts_library import sync_manifest
        p = params or {}
        return sync_manifest(p["url"], expected_sha256=p["expected_sha256"],
                             mirrors=p.get("mirrors"),
                             replace_namespace=p.get("replace_namespace", False))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_library_sync failed: {exc}"}


def _part_acquire(params: dict, _emit: Callable) -> dict:
    try:
        from services.parts.parts_library import acquire_part
        p = params or {}
        return {"ok": True, "part": acquire_part(p.get("part_id", ""),
                                                  offline=p.get("offline", False))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_acquire failed: {exc}"}


# Registered by services/worker_main.py via ``_METHODS.update(WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "part_search": _part_search,
    "part_get": _part_get,
    "part_place": _part_place,
    "part_library_ingest": _part_library_ingest,
    "part_library_sync": _part_library_sync,
    "part_acquire": _part_acquire,
}

# GUI-shaped methods for the Parts Library workspace (host/src/ui/parts/) —
# categories, filtered search (``parts_search``), index build state
# (``parts_index_status``), vendor login/status, fetch queue, thumbnails. The
# ``parts_*`` table itself lives in gui_rpc.GUI_WORKER_METHODS (one source of
# truth) and is merged into WORKER_METHODS here.
try:
    from .gui_rpc import GUI_WORKER_METHODS as _GUI_METHODS
    WORKER_METHODS.update(_GUI_METHODS)
except Exception as exc:  # noqa: BLE001 — agent methods above still register
    REGISTRATION_DIAGNOSTICS.append({"provider": "gui_rpc", "operation": "registration",
                                     "error": f"{type(exc).__name__}: {exc}"})
    log.warning("parts GUI methods not registered: %s", exc)

    def _registration_status(_params: dict, _emit: Callable) -> dict:
        return {"ok": False, "reason": "registration_failed",
                "diagnostics": list(REGISTRATION_DIAGNOSTICS)}

    WORKER_METHODS["parts_index_status"] = _registration_status

# Embedded-browser seam (Parts Library V2 workstream 3, generic/reusable) —
# browser_start_url + login_wizard_*. Thin wrappers over services.browser.embed
# (in-process CEF browser); degrade to ok:false envelopes when Playwright isn't
# provisioned. Re-homed from engine_oracle/embed_rpc.py.
try:
    from .embed_rpc import EMBED_WORKER_METHODS as _EMBED_METHODS
    WORKER_METHODS.update(_EMBED_METHODS)
except Exception as _embed_err:  # noqa: BLE001 — embed seam is optional; methods above still register
    REGISTRATION_DIAGNOSTICS.append({"provider": "embed_rpc", "operation": "registration",
                                     "error": f"{type(_embed_err).__name__}: {_embed_err}"})
    log.warning("embed_rpc unavailable — embed methods not registered: %s", _embed_err)

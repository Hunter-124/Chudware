"""Worker-RPC bridge for the BOM / sourcing seam (T-U4).

Exposes ``bom_rollup`` + ``bom_price`` + ``request_quote`` + ``match_part_number`` as
**engine-worker methods** so the C++/ImGui GUI reaches them through the same
newline-delimited-JSON worker seam (``WorkerClient`` / ``AsyncRpc``,
``services/worker/``) it already uses for every other service.

These are thin wrappers over the MCP-layer implementations in
``services/mcp/sourcing_tools.py``, ``cost_tools.py``, and ``spec_tools.py`` — the
business logic lives there; this module only provides the worker-method envelope.

Registered append-only in ``engine_oracle/worker.py`` (mirrors the ECAD / deliverables
``WORKER_METHODS`` block).
"""
from __future__ import annotations

import json
from typing import Callable


def _dumps(obj: object) -> dict:
    """Return obj as a dict (already a dict) or parse JSON string to dict."""
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        return json.loads(obj)
    return {"ok": False, "error": f"unexpected return type: {type(obj)}"}


def _bom_rollup(params: dict, _emit: Callable) -> dict:
    """Worker method ``bom_rollup`` — roll assembly into unified made/bought BOM.

    Params: {doc_path: str}
    Returns: {ok, bom: [BomLine...]}  — prices NOT resolved, unit_cost/ext_cost null.
    """
    doc_path = (params or {}).get("doc_path", "")
    try:
        from engine import bom as bom_mod  # type: ignore[import]
        lines = bom_mod.rollup(doc_path)
        return {"ok": True, "bom": bom_mod.to_dicts(lines)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _bom_price(params: dict, _emit: Callable) -> dict:
    """Worker method ``bom_price`` — roll up BOM + resolve bought-line prices.

    Params: {doc_path: str}
    Returns: {ok, bom: [BomLine...]}  — unit_cost/ext_cost null when unpriced (no key /
    no provider hit); NEVER a fabricated number (contract aicad.sourcing/1 §0).
    """
    doc_path = (params or {}).get("doc_path", "")
    try:
        from engine import bom as bom_mod  # type: ignore[import]
        lines = bom_mod.resolve_prices(bom_mod.rollup(doc_path))
        return {"ok": True, "bom": bom_mod.to_dicts(lines)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _request_quote(params: dict, _emit: Callable) -> dict:
    """Worker method ``request_quote`` — request a fabrication quote from a vendor.

    Params: {doc_path, vendor, quantity?, material?, layers?, finish?}
    Returns: {ok, quote} on success; {ok:false, reason, hint} when keyless / vendor down.
    """
    p = params or {}
    doc_path = p.get("doc_path", "")
    vendor   = p.get("vendor", "")
    quantity = p.get("quantity", 1)
    try:
        from engine import quote_providers  # type: ignore[import]
        result = quote_providers.request_quote(
            doc_path,
            vendor,
            quantity,
            material=p.get("material"),
            layers=p.get("layers"),
            finish=p.get("finish"),
        )
        if isinstance(result, dict):
            return {"ok": True, "quote": result}
        return {"ok": False, "error": "unexpected quote_providers result shape"}
    except Exception as e:  # noqa: BLE001
        # Keyless or network-down → degrade gracefully (no fabricated price).
        return {"ok": False, "reason": str(e), "hint": "Configure vendor API key to enable quoting"}


def _match_part_number(params: dict, _emit: Callable) -> dict:
    """Worker method ``match_part_number`` — normalise + match an MPN.

    Params: {mpn: str, generate?: bool}
    Returns: {ok, match: {...}} on hit; {ok:false, error} on miss.
    """
    p = params or {}
    mpn = p.get("mpn", "")
    generate = p.get("generate", True)
    try:
        from engine_oracle import spec_sheet_matcher as ssm  # type: ignore[import]
        result = ssm.match_part_number(mpn, generate=generate)
        return {"ok": True, "match": result}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# Registered by engine_oracle/worker.py via ``_METHODS.update(WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "bom_rollup":        _bom_rollup,
    "bom_price":         _bom_price,
    "request_quote":     _request_quote,
    "match_part_number": _match_part_number,
}

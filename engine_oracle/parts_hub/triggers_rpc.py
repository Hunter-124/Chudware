"""Worker-RPC bridge for trigger-based context injection (aicad.trigger/1).

Exposes ``triggers_list`` / ``triggers_get`` / ``triggers_save`` /
``triggers_delete`` / ``packs_list`` / ``packs_get`` / ``packs_save`` /
``packs_delete`` over the newline-delimited-JSON worker seam.

Thin wrappers over :mod:`services.agent.advisor.triggers`. Failures return a
contract-shaped ``{"ok": false, "reason"}`` envelope — never a traceback.
GUI degrades to hidden when an RPC reports unknown_method.

Registration: CadGraphCore owns ``worker_rpc.py`` — import
``TRIGGERS_METHODS`` there via try/except.
"""
from __future__ import annotations

from typing import Callable


def _triggers():
    from services.agent.advisor import triggers as tr
    return tr


def _triggers_list(params: dict, _emit: Callable) -> dict:
    """``triggers_list {} -> {ok, triggers:[...], count}``."""
    try:
        rows = _triggers().list_triggers()
        return {"ok": True, "triggers": rows, "count": len(rows)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"triggers_list failed: {exc}", "hint": ""}


def _triggers_get(params: dict, _emit: Callable) -> dict:
    """``triggers_get {id} -> {ok, trigger, pack}``."""
    tid = str((params or {}).get("id") or (params or {}).get("name") or "").strip()
    if not tid:
        return {"ok": False, "reason": "id required"}
    try:
        return _triggers().get_trigger(tid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"triggers_get failed: {exc}", "hint": ""}


def _triggers_save(params: dict, _emit: Callable) -> dict:
    """``triggers_save {trigger: {...}} | flat fields -> {ok, id, source}``.

    Accepts either ``{trigger: {...}}`` or the trigger fields at the top level
    (id/match/inject/...). Optional ``pack_body`` writes the pack in the same
    call when ``inject.context_pack`` is set.
    """
    p = params or {}
    trigger = p.get("trigger") if isinstance(p.get("trigger"), dict) else dict(p)
    # Strip RPC-only keys that aren't part of the trigger schema.
    for k in ("pack_body", "pack_name", "trigger"):
        trigger.pop(k, None)
    try:
        out = _triggers().save_trigger(trigger)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"triggers_save failed: {exc}", "hint": ""}
    if not out.get("ok"):
        return out
    pack_body = p.get("pack_body")
    pack_name = (
        str(p.get("pack_name") or "").strip()
        or str((trigger.get("inject") or {}).get("context_pack") or "").strip()
    )
    if isinstance(pack_body, str) and pack_name:
        try:
            pack_out = _triggers().save_pack(pack_name, pack_body)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": True,
                "id": out.get("id"),
                "source": out.get("source"),
                "pack_ok": False,
                "pack_reason": f"pack save failed: {exc}",
            }
        out["pack"] = pack_out
    return out


def _triggers_delete(params: dict, _emit: Callable) -> dict:
    """``triggers_delete {id} -> {ok, restored_builtin?}`` — user override only."""
    tid = str((params or {}).get("id") or (params or {}).get("name") or "").strip()
    if not tid:
        return {"ok": False, "reason": "id required"}
    try:
        return _triggers().delete_trigger(tid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"triggers_delete failed: {exc}", "hint": ""}


def _packs_list(params: dict, _emit: Callable) -> dict:
    """``packs_list {} -> {ok, packs:[{name, source}]}``."""
    try:
        packs = _triggers().list_packs()
        return {"ok": True, "packs": packs, "count": len(packs)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"packs_list failed: {exc}", "hint": ""}


def _packs_get(params: dict, _emit: Callable) -> dict:
    """``packs_get {name} -> {ok, name, body, source}``."""
    name = str((params or {}).get("name") or (params or {}).get("id") or "").strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    try:
        return _triggers().get_pack(name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"packs_get failed: {exc}", "hint": ""}


def _packs_save(params: dict, _emit: Callable) -> dict:
    """``packs_save {name, body} -> {ok, name, source}`` — user dir only."""
    name = str((params or {}).get("name") or "").strip()
    body = (params or {}).get("body", "")
    if not name:
        return {"ok": False, "reason": "name required"}
    if not isinstance(body, str):
        return {"ok": False, "reason": "body required (markdown string)"}
    try:
        return _triggers().save_pack(name, body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"packs_save failed: {exc}", "hint": ""}


def _packs_delete(params: dict, _emit: Callable) -> dict:
    """``packs_delete {name} -> {ok, restored_builtin?}`` — user pack only."""
    name = str((params or {}).get("name") or "").strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    try:
        return _triggers().delete_pack(name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"packs_delete failed: {exc}", "hint": ""}


TRIGGERS_METHODS: dict[str, Callable] = {
    "triggers_list": _triggers_list,
    "triggers_get": _triggers_get,
    "triggers_save": _triggers_save,
    "triggers_delete": _triggers_delete,
    "packs_list": _packs_list,
    "packs_get": _packs_get,
    "packs_save": _packs_save,
    "packs_delete": _packs_delete,
}

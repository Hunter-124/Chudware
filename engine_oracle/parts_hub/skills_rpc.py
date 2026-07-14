"""Worker-RPC bridge for copilot skills (contracts/skills.md v1).

Exposes ``skills_list`` / ``skills_get`` / ``skills_save`` / ``skills_delete``
over the newline-delimited-JSON worker seam (WorkerClient / AsyncRpc).

Thin wrappers over :mod:`services.agent.skills`. All failures return a
contract-shaped ``{"ok": false, "reason"}`` envelope — never a traceback.
GUI degrades to hidden when an RPC reports unknown_method.

Registration: CadGraphCore owns ``worker_rpc.py`` this wave — import
``SKILLS_METHODS`` there via try/except.
"""
from __future__ import annotations

from typing import Callable


def _skills():
    from services.agent import skills as sk
    return sk


def _skills_list(params: dict, _emit: Callable) -> dict:
    """``skills_list {} -> {ok, skills:[{name, description, source, inject, triggers}]}``."""
    try:
        sk = _skills()
        skills = sk.list_skills()
        return {"ok": True, "skills": skills, "count": len(skills)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"skills_list failed: {exc}", "hint": ""}


def _skills_get(params: dict, _emit: Callable) -> dict:
    """``skills_get {name} -> {ok, body, files:[..]}``."""
    name = str((params or {}).get("name", "")).strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    try:
        return _skills().get_skill_payload(name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"skills_get failed: {exc}", "hint": ""}


def _skills_save(params: dict, _emit: Callable) -> dict:
    """``skills_save {name, body} -> {ok}`` — user dir only."""
    name = str((params or {}).get("name", "")).strip()
    body = (params or {}).get("body", "")
    if not name:
        return {"ok": False, "reason": "name required"}
    if not isinstance(body, str) or not body.strip():
        return {"ok": False, "reason": "body required (full SKILL.md text)"}
    try:
        return _skills().save_user_skill(name, body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"skills_save failed: {exc}", "hint": ""}


def _skills_delete(params: dict, _emit: Callable) -> dict:
    """``skills_delete {name} -> {ok}`` — user dir only."""
    name = str((params or {}).get("name", "")).strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    try:
        return _skills().delete_user_skill(name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"skills_delete failed: {exc}", "hint": ""}


def _skills_resolve(params: dict, _emit: Callable) -> dict:
    """Optional helper: resolve skill:// URI (not in v1 contract but useful)."""
    uri = str((params or {}).get("uri") or (params or {}).get("url") or "").strip()
    if not uri:
        return {"ok": False, "reason": "uri required (skill://<name>[/<path>])"}
    try:
        return _skills().resolve_skill_uri(uri)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc), "hint": ""}


SKILLS_METHODS: dict[str, Callable] = {
    "skills_list": _skills_list,
    "skills_get": _skills_get,
    "skills_save": _skills_save,
    "skills_delete": _skills_delete,
    "skills_resolve": _skills_resolve,
}

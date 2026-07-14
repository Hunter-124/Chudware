"""Worker-RPC bridge for PLM lightweight branching (wave-2).

Exposes::

    plm_branch_create  {path, name, from?}
    plm_branch_list    {path}
    plm_branch_switch  {path, name}
    plm_branch_merge   {path, src, dst}

Thin wrappers over :mod:`services.plm.branches`. Failures return the contract
``{ok:false, reason, hint}`` envelope — never a traceback. GUI degrades to
hidden chrome when an RPC reports ``unknown_method``.
"""
from __future__ import annotations

from typing import Any, Callable


def _path(params: dict | None) -> str:
    p = params or {}
    return str(p.get("path") or p.get("project_dir") or "").strip()


def _plm_branch_create(params: dict, _emit: Callable) -> dict:
    """Create a named branch from HEAD (or ``from`` branch/commit)."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    name = str((params or {}).get("name") or "").strip()
    from_ref = (params or {}).get("from") or (params or {}).get("from_ref") or None
    if from_ref is not None:
        from_ref = str(from_ref).strip() or None
    try:
        return branches.branch_create(path, name, from_ref=from_ref)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_list(params: dict, _emit: Callable) -> dict:
    """List project branches + the active one."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    try:
        return branches.branch_list(path)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_switch(params: dict, _emit: Callable) -> dict:
    """Switch the active branch and materialize its working files."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    name = str((params or {}).get("name") or "").strip()
    try:
        return branches.branch_switch(path, name)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_merge(params: dict, _emit: Callable) -> dict:
    """Merge *src* into *dst*. Returns structured conflicts (never silent)."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    src = str((params or {}).get("src") or "").strip()
    dst = str((params or {}).get("dst") or "").strip()
    message = str((params or {}).get("message") or "")
    if not src or not dst:
        return {"ok": False, "reason": "invalid_mode",
                "hint": "both src and dst branch names are required"}
    try:
        return branches.branch_merge(path, src, dst, message=message)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


PLM_BRANCH_METHODS: dict[str, Callable[..., Any]] = {
    "plm_branch_create": _plm_branch_create,
    "plm_branch_list": _plm_branch_list,
    "plm_branch_switch": _plm_branch_switch,
    "plm_branch_merge": _plm_branch_merge,
}

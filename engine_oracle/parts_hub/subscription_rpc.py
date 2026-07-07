"""Worker-RPC bridge for subscription-auth logins (§4.4) — the GUI Settings seam.

Exposes ``subscription_status`` / ``subscription_login`` / ``subscription_logout`` as
engine-worker methods so the C++/ImGui Settings panel reaches them over the same
newline-delimited-JSON worker seam (``WorkerClient``) it uses for everything else.

``subscription_login`` runs the browser OAuth flow on a daemon thread and returns
IMMEDIATELY — the flow blocks up to ~120 s waiting on the local callback, and blocking the
single worker RPC dispatch thread would freeze every other panel (see memory:
GL-thread RPC freeze). The GUI kicks off a login, then polls ``subscription_status`` until
``login_in_progress`` clears and ``connected`` flips true (or ``last_error`` is set).

Thin wrappers over :mod:`agent.providers.subscription_login`; all failures come back as a
contract-shaped ``{"ok": false, "reason", "hint"}`` — never a traceback.
"""
from __future__ import annotations

import threading
from typing import Callable

# provider friendly id -> {"in_progress": bool, "error": str | None}
_login_state: dict[str, dict] = {}
_login_lock = threading.Lock()


def _sl():
    from agent.providers import subscription_login as sl
    return sl


def _subscription_status(params: dict, _emit: Callable) -> dict:
    """Per-provider connection state + live login progress (no tokens leaked)."""
    try:
        st = _sl().status()
        with _login_lock:
            for pid, entry in st.items():
                prog = _login_state.get(pid, {})
                entry["login_in_progress"] = bool(prog.get("in_progress"))
                entry["last_error"] = prog.get("error")
        return {"ok": True, "providers": st}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subscription_status failed: {exc}", "hint": ""}


def _subscription_login(params: dict, _emit: Callable) -> dict:
    """Start the browser OAuth flow on a daemon thread; return immediately."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    device = bool((params or {}).get("device", False))
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor)"}
    valid = {p["id"] for p in _sl().providers()}
    if provider not in valid:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": f"Use one of: {', '.join(sorted(valid))}."}
    try:
        with _login_lock:
            if _login_state.get(provider, {}).get("in_progress"):
                return {"ok": True, "provider": provider, "in_progress": True, "already": True}
            _login_state[provider] = {"in_progress": True, "error": None}

        def _run() -> None:
            result = _sl().login(provider, device=device)
            with _login_lock:
                _login_state[provider] = {
                    "in_progress": False,
                    "error": None if result.get("ok") else result.get("reason"),
                }

        t = threading.Thread(target=_run, name=f"sub-login-{provider}", daemon=True)
        t.start()
        return {"ok": True, "provider": provider, "in_progress": True}
    except Exception as exc:  # noqa: BLE001
        with _login_lock:
            _login_state[provider] = {"in_progress": False, "error": str(exc)}
        return {"ok": False, "reason": f"subscription_login failed: {exc}", "hint": ""}


def _subscription_logout(params: dict, _emit: Callable) -> dict:
    """Clear the stored credential for a provider."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor)"}
    try:
        out = _sl().logout(provider)
        with _login_lock:
            _login_state.pop(provider, None)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subscription_logout failed: {exc}", "hint": ""}


SUBSCRIPTION_METHODS: dict[str, Callable] = {
    "subscription_status": _subscription_status,
    "subscription_login": _subscription_login,
    "subscription_logout": _subscription_logout,
}

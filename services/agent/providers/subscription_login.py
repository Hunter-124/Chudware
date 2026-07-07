"""Subscription-auth login orchestration (§4.4) — the seam the GUI/Settings calls.

One stable entry point per action so the C++ Settings panel (and any RPC/MCP caller)
never imports individual provider modules:

    login(provider)   -> {"ok": True, "email": ..., "account_id": ...} | {"ok": False, ...}
    logout(provider)  -> {"ok": True}
    status()          -> {provider: {"connected": bool, "email": ..., "expires_at_ms": ...}}

`provider` is one of the friendly ids: "codex" | "claude" | "cursor" (aliases for the
provider names "codex-sub" / "anthropic-sub" / "cursor-sub"). `login()` opens the user's
browser and blocks until the OAuth flow completes, so callers should run it off the UI
thread. Every path returns a contract-shaped dict — never a traceback.
"""
from __future__ import annotations

from . import sub_auth

# Friendly id -> (provider_name, provider_id used in the credential store, human label).
_PROVIDERS = {
    "codex": ("codex-sub", "openai-codex", "OpenAI Codex (ChatGPT Plus/Pro)"),
    "claude": ("anthropic-sub", "anthropic-sub", "Anthropic Claude (Pro/Max)"),
    "cursor": ("cursor-sub", "cursor", "Cursor (Pro)"),
}


def _resolve(provider: str):
    key = (provider or "").strip().lower()
    # Accept the provider *name* too, so callers can pass either "codex" or "codex-sub".
    for friendly, (name, cred_id, label) in _PROVIDERS.items():
        if key in (friendly, name):
            return friendly, name, cred_id, label
    return None


def _instantiate(provider_name: str):
    if provider_name == "codex-sub":
        from .codex_sub_provider import CodexSubProvider
        return CodexSubProvider()
    if provider_name == "anthropic-sub":
        from .anthropic_sub_provider import AnthropicSubProvider
        return AnthropicSubProvider()
    if provider_name == "cursor-sub":
        from .cursor_sub_provider import CursorSubProvider
        return CursorSubProvider()
    raise ValueError(f"unknown subscription provider {provider_name!r}")


def providers() -> list[dict]:
    """List the supported subscription providers (for populating the Settings UI)."""
    return [{"id": fid, "name": name, "label": label}
            for fid, (name, _cred, label) in _PROVIDERS.items()]


def login(provider: str, device: bool = False) -> dict:
    """Run the OAuth login for *provider*. Blocks on the browser flow. Contract-shaped.

    ``device=True`` uses the headless device-code flow (Codex only) instead of the local
    callback server — useful when port 1455 is busy or there's no local browser.
    """
    resolved = _resolve(provider)
    if resolved is None:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor."}
    _friendly, name, _cred_id, label = resolved
    try:
        prov = _instantiate(name)
        if device and name == "codex-sub":
            cred = prov.login_device()
        else:
            cred = prov.login()
    except Exception as e:  # noqa: BLE001 — surface every failure as a clean dict for the GUI
        return {"ok": False, "reason": f"{label} login failed: {e}",
                "hint": "Complete the login in the browser tab, or check your network and "
                        "subscription, then try again."}
    return {"ok": True, "provider": name, "label": label,
            "email": cred.get("email"), "account_id": cred.get("account_id")}


def logout(provider: str) -> dict:
    """Clear the stored credential for *provider*. Contract-shaped."""
    resolved = _resolve(provider)
    if resolved is None:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor."}
    _friendly, _name, cred_id, _label = resolved
    sub_auth.clear_credential(cred_id)
    return {"ok": True}


def status() -> dict:
    """Per-provider connection state for the Settings UI (no tokens leaked)."""
    out: dict[str, dict] = {}
    for friendly, (_name, cred_id, label) in _PROVIDERS.items():
        cred = sub_auth.load_credential(cred_id)
        out[friendly] = {
            "label": label,
            "connected": bool(cred),
            "email": (cred or {}).get("email"),
            "account_id": (cred or {}).get("account_id"),
            "expires_at_ms": (cred or {}).get("expires_at_ms"),
        }
    return out

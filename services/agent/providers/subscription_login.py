"""Subscription-auth login orchestration (§4.4) — the seam the GUI/Settings calls.

One stable entry point per action so the C++ Settings panel (and any RPC/MCP caller)
never imports individual provider modules:

    login(provider, account_id=None)   -> {"ok": True, "email": ..., "account_id": ...}
    logout(provider, account_id=None)  -> {"ok": True}   # None = active account
    status()                           -> {provider: {connected, email, account_id, accounts, ...}}
    accounts(provider=None)            -> [{provider, account_id, label/email, active, connected}]
    switch(provider, account_id)       -> {"ok": True, ...}

`provider` is one of the friendly ids: "codex" | "claude" | "cursor" | "xai" (aliases for the
provider names "codex-sub" / "anthropic-sub" / "cursor-sub" / "xai-sub"). Multiple named
accounts may be stored per provider; providers always resolve the ACTIVE account's token via
``sub_auth.load_credential``. `login()` opens the user's browser and blocks until the OAuth
flow completes, so callers should run it off the UI thread. Every path returns a
contract-shaped dict — never a traceback.
"""
from __future__ import annotations

from typing import Callable
import time

from . import sub_auth

# Friendly id -> (provider_name, provider_id used in the credential store, human label).
_PROVIDERS = {
    "codex": ("codex-sub", "openai-codex", "OpenAI Codex (ChatGPT Plus/Pro)"),
    "claude": ("anthropic-sub", "anthropic-sub", "Anthropic Claude (Pro/Max)"),
    "cursor": ("cursor-sub", "cursor", "Cursor (Pro)"),
    "xai": ("xai-sub", "xai-sub", "xAI Grok (SuperGrok / X Premium+)"),
}

ProgressCb = Callable[[dict], None]

def _resolve(provider: str):
    key = (provider or "").strip().lower()
    # Wire / config aliases that are not the friendly id or provider name.
    if key == "anthropic":
        key = "claude"
    # Accept friendly id, provider name, or credential-store id.
    for friendly, (name, cred_id, label) in _PROVIDERS.items():
        if key in (friendly, name, cred_id):
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
    if provider_name == "xai-sub":
        from .xai_sub_provider import XAISubProvider
        return XAISubProvider()
    raise ValueError(f"unknown subscription provider {provider_name!r}")


def providers() -> list[dict]:
    """List the supported subscription providers (for populating the Settings UI)."""
    return [{"id": fid, "name": name, "label": label}
            for fid, (name, _cred, label) in _PROVIDERS.items()]

# Default refresh skew for pre-run ensure_fresh (5 minutes). Matches anthropic /
# cursor / xai provider skews so the C++ read-only token path never sees an
# already-expired access token at run start.
_ENSURE_FRESH_SKEW_MS = 5 * 60 * 1000

def _has_api_key_fallback(friendly: str) -> bool:
    """True when config is currently on a classic API-key path for this provider.

    xAI and Anthropic both support API-key mode; a stale leftover subscription
    credential must not block those runs. Cursor/codex are subscription-only.

    Requires BOTH the active ``provider`` wire name AND a key shape that matches
    the non-OAuth path — a leftover openai/gemini key must not soft-pass Claude.
    """
    if friendly not in ("xai", "claude"):
        return False
    try:
        cfg = sub_auth._load_cfg()  # noqa: SLF001 — shared config reader
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(cfg, dict):
        return False
    prov = str(cfg.get("provider") or "").strip().lower()
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        return False
    if friendly == "claude":
        # API-key mode is provider=anthropic with a classic sk-ant- key (not OAuth oat).
        if prov != "anthropic":
            return False
        if key.startswith("sk-ant-oat"):
            return False
        return key.startswith("sk-ant-")
    # xai API-key mode: app_state only writes bare "xai" (not xai-sub) when xaiKey is set.
    if prov != "xai":
        return False
    return True



def ensure_fresh(provider: str, *, skew_ms: int = _ENSURE_FRESH_SKEW_MS) -> dict:
    """Refresh the active subscription token if expired or expiring within *skew_ms*.

    Python is the single token-refresh writer (C++ reads subscription_auth
    read-only). Returns a contract-shaped dict with no secrets::

        {ok, refreshed, expires_in, error}

    *provider* accepts friendly ids (claude/xai/cursor/codex) and provider
    names (anthropic-sub/xai-sub/…) — same aliases as status/login.
    """
    resolved = _resolve(provider)
    if resolved is None:
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": None,
            "error": f"unknown subscription provider {provider!r}",
        }
    friendly, name, cred_id, _label = resolved
    try:
        cred = sub_auth.load_credential(cred_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": None,
            "error": f"load credential failed: {exc}",
        }
    if not cred or not (
        cred.get("access_token") or cred.get("refresh_token")
    ):
        # No stored subscription credential. Callers that use an API-key path
        # (e.g. anthropic + sk-ant-…) still proceed; C++ refuses later if both
        # are missing. Not a refresh failure.
        return {
            "ok": True,
            "refreshed": False,
            "expires_in": None,
            "error": None,
        }

    def _expires_in_s(c: dict) -> int | None:
        exp = c.get("expires_at_ms")
        if exp is None:
            return None
        try:
            return int(max(0, (float(exp) - time.time() * 1000) / 1000))
        except (TypeError, ValueError):
            return None

    if not sub_auth.is_expired(cred, skew_ms=skew_ms):
        return {
            "ok": True,
            "refreshed": False,
            "expires_in": _expires_in_s(cred),
            "error": None,
        }

    # Stale / near-expiry — refresh via the provider's single-writer path.
    if not cred.get("refresh_token"):
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": _expires_in_s(cred),
            "error": (
                f"{friendly} access token expired and no refresh_token is stored. "
                "Re-authenticate via Settings → AI → Accounts."
            ),
        }
    try:
        prov = _instantiate(name)
        # Constructor already loaded active cred; re-bind for safety.
        if getattr(prov, "_cred", None) is None:
            prov._cred = cred
        new_cred = prov.refresh()
    except Exception as exc:  # noqa: BLE001 — never leak a traceback over RPC
        # Dual-path providers (xai / anthropic) may run on a classic API key while a
        # leftover subscription credential is stale. Don't brick the API-key path.
        if _has_api_key_fallback(friendly):
            return {
                "ok": True,
                "refreshed": False,
                "expires_in": _expires_in_s(cred),
                "error": None,
            }
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": _expires_in_s(cred),
            "error": (
                f"{friendly} token refresh failed — re-authenticate via "
                f"Settings → AI → Accounts. ({exc})"
            ),
        }
    return {
        "ok": True,
        "refreshed": True,
        "expires_in": _expires_in_s(new_cred if isinstance(new_cred, dict) else {}),
        "error": None,
    }


def login(
    provider: str,
    device: bool = False,
    progress: ProgressCb | None = None,
    open_browser: bool | None = None,
    account_id: str | None = None,
) -> dict:
    """Run the OAuth login for *provider*. Blocks on the browser flow. Contract-shaped.

    ``device=True`` selects the headless device-code path for Codex (no local callback
    server). xAI is device-code-only; for xAI the browser is ALWAYS opened by default
    (device-code verification URL on accounts.x.ai) — ``device`` is accepted for API
    symmetry and does NOT suppress the browser. Pass ``open_browser=False`` only for
    truly headless environments; the verification URL + user_code still surface via
    *progress* / status so the GUI can show a fallback link.

    On success the credential is stored under a derived *account_id* (from token metadata
    or the explicit *account_id* argument) and that account is set active.
    """
    resolved = _resolve(provider)
    if resolved is None:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor, xai."}
    _friendly, name, cred_id, label = resolved
    try:
        prov = _instantiate(name)
        if device and name == "codex-sub":
            cred = prov.login_device()
        elif name == "xai-sub":
            # xAI is device-code-only. Always open the verification URL unless the
            # caller explicitly opts out (headless). The previous open_browser=not
            # device inversion meant the GUI's device:true path NEVER opened a
            # browser — that was the "sign-in never reaches a browser" bug.
            want_browser = True if open_browser is None else bool(open_browser)
            cred = prov.login_device(open_browser=want_browser, progress=progress)
        else:
            cred = prov.login()
    except Exception as e:  # noqa: BLE001 — surface every failure as a clean dict for the GUI
        from .xai_sub_provider import XaiTierDeniedError, TIER_DENIED_HINT, TIER_DENIED_REASON
        if isinstance(e, XaiTierDeniedError):
            return {"ok": False, "reason": e.reason or TIER_DENIED_REASON,
                    "hint": e.hint or TIER_DENIED_HINT}
        return {"ok": False, "reason": f"{label} login failed: {e}",
                "hint": "Complete the login in the browser tab, or check your network and "
                        "subscription, then try again."}

    if not isinstance(cred, dict):
        return {
            "ok": False,
            "reason": f"{label} login failed: provider returned no credential",
            "hint": "Try signing in again. If the problem persists, restart the worker.",
        }
    # Providers already call save_credential; re-key under a stable account id and
    # ensure that account is active. Prefer explicit account_id, then token metadata.
    if account_id is not None and str(account_id).strip():
        aid = str(account_id).strip()
    else:
        aid = sub_auth.derive_account_id(cred)
    # Re-save under the derived id (covers providers that still write legacy flat keys
    # or save under a different id than the one we want active).
    sub_auth.save_credential(cred_id, cred, account_id=aid, set_active=True)
    # Drop a residual "default" placeholder if we just re-keyed away from it and the
    # provider originally saved under DEFAULT with no identity (same tokens).
    if aid != sub_auth.DEFAULT_ACCOUNT_ID:
        legacy = sub_auth.load_credential(cred_id, account_id=sub_auth.DEFAULT_ACCOUNT_ID)
        if legacy and legacy.get("access_token") == cred.get("access_token"):
            # Only drop default if it is not the active id we just set.
            if sub_auth.get_active_account_id(cred_id) == aid:
                sub_auth.clear_credential(cred_id, account_id=sub_auth.DEFAULT_ACCOUNT_ID)

    # A re-login for the same identity must not reuse quota from the prior session.
    try:
        from . import usage as usage_mod
        usage_mod.invalidate_account(_friendly, aid)
    except Exception:
        pass

    return {"ok": True, "provider": name, "label": label,
            "email": cred.get("email"), "account_id": aid}


def logout(provider: str, account_id: str | None = None) -> dict:
    """Clear a stored credential. *account_id* None = active account. Contract-shaped."""
    resolved = _resolve(provider)
    if resolved is None:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor, xai."}
    friendly, _name, cred_id, _label = resolved
    removed_id = account_id or sub_auth.get_active_account_id(cred_id)
    sub_auth.clear_credential(cred_id, account_id=account_id)
    try:
        from . import usage as usage_mod
        usage_mod.invalidate_account(friendly, removed_id)
    except Exception:
        pass
    return {"ok": True}


def status() -> dict:
    """Per-provider connection state for the Settings UI (no tokens leaked).

    Backward-compatible shape: top-level fields describe the ACTIVE account.
    Also includes ``accounts`` (list of named accounts with active flags).
    Optional identity fields (display_name, plan) from §4.5 when known.
    ``usable`` is True when at least one signed-in account has tokens (xAI
    multi-account may still rotate off a soft-blocked active pointer).
    """
    out: dict[str, dict] = {}
    for friendly, (_name, cred_id, label) in _PROVIDERS.items():
        cred = sub_auth.load_credential(cred_id)  # active account
        accts = sub_auth.list_accounts(cred_id)
        connected = bool(cred) and bool(
            (cred or {}).get("access_token") or (cred or {}).get("refresh_token")
        )
        # Connected if ANY account has tokens (not only the active pointer).
        if not connected and accts:
            connected = True
        entry = {
            "label": label,
            "connected": connected,
            "usable": connected,
            "email": (cred or {}).get("email"),
            "account_id": (cred or {}).get("account_id") or sub_auth.get_active_account_id(cred_id),
            "expires_at_ms": (cred or {}).get("expires_at_ms"),
            "accounts": accts,
        }
        if cred:
            if cred.get("display_name"):
                entry["display_name"] = cred.get("display_name")
            if cred.get("plan"):
                entry["plan"] = cred.get("plan")
        if friendly == "xai":
            try:
                from .xai_sub_provider import provider_is_usable
                entry["usable"] = bool(provider_is_usable())
                # Keep connected true whenever store has any account, even if
                # all are soft-blocked (user is still signed in).
                entry["connected"] = entry["usable"] or bool(accts)
            except Exception:
                pass
        out[friendly] = entry
    return out


def accounts(provider: str | None = None) -> list[dict]:
    """List stored accounts across providers (or one provider). No tokens.

    Each item: ``{provider, account_id, label, email, active, connected}``.
    *provider* accepts friendly id or provider name.
    """
    targets: list[tuple[str, str, str]] = []  # friendly, cred_id, label
    if provider:
        resolved = _resolve(provider)
        if resolved is None:
            return []
        friendly, _name, cred_id, label = resolved
        targets.append((friendly, cred_id, label))
    else:
        for friendly, (_name, cred_id, label) in _PROVIDERS.items():
            targets.append((friendly, cred_id, label))

    out: list[dict] = []
    for friendly, cred_id, _label in targets:
        for acct in sub_auth.list_accounts(cred_id):
            item = {
                "provider": friendly,
                "account_id": acct["account_id"],
                "label": acct.get("label") or acct.get("email") or acct["account_id"],
                "email": acct.get("email"),
                "active": bool(acct.get("active")),
                "connected": True,
            }
            if acct.get("display_name"):
                item["display_name"] = acct.get("display_name")
            if acct.get("plan"):
                item["plan"] = acct.get("plan")
            out.append(item)
    return out


def switch(provider: str, account_id: str) -> dict:
    """Make *account_id* the active account for *provider*. Contract-shaped."""
    resolved = _resolve(provider)
    if resolved is None:
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor, xai."}
    if not account_id or not str(account_id).strip():
        return {"ok": False, "reason": "account_id required", "hint": ""}
    friendly, name, cred_id, label = resolved
    aid = str(account_id).strip()
    if not sub_auth.set_active_account(cred_id, aid):
        return {
            "ok": False,
            "reason": f"unknown account {aid!r} for provider {friendly!r}",
            "hint": "Call subscription_accounts to list stored accounts, then switch.",
        }
    cred = sub_auth.load_credential(cred_id)
    return {
        "ok": True,
        "provider": name,
        "label": label,
        "account_id": aid,
        "email": (cred or {}).get("email"),
    }

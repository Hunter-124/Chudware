"""Worker-RPC bridge for the services.agent-owned GUI seams (T-RF19 fold).

Single registration point for the worker-RPC tables that were squatting in
``engine_oracle/parts_hub/`` although every one is a thin wrapper over
:mod:`services.agent` modules:

- ``subscription_*`` (§4.4/§4.5 auth, Settings Accounts) — over services.agent.providers
- ``session_compact/shake/handoff/btw`` — over :mod:`services.agent.session_cmds`
- ``skills_*`` — over :mod:`services.agent.skills`
- ``triggers_*`` / ``packs_*`` — over :mod:`services.agent.advisor.triggers`
- ``ai_roles_*`` / ``model_rankings_*`` — over :mod:`services.agent.role_models`
- ``bench_*`` (Settings → Benchmarks) — gym CLI runner

Each table keeps its original name (``SUBSCRIPTION_METHODS`` etc.) and every
RPC method name is preserved; ``WORKER_METHODS`` below is the merged table
``engine_oracle/worker.py`` registers. All failures return contract-shaped
``{"ok": false, ...}`` envelopes — never a traceback. GUI degrades to hidden
when an RPC reports ``unknown_method``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any

from services.agent.chudcode_host import get_default_chudcode_host


_LOG = logging.getLogger(__name__)


# ──── from engine_oracle/parts_hub/subscription_rpc.py (T-RF19 fold) ────


# provider friendly id -> live login progress (no tokens)
# keys: in_progress, error, user_code, verification_url, browser_opened, stage
_login_state: dict[str, dict] = {}
_login_lock = threading.Lock()


def _sl():
    from services.agent.providers import subscription_login as sl

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
                # Device-code UX fields (xAI; empty for other providers).
                if prog.get("user_code"):
                    entry["user_code"] = prog.get("user_code")
                if prog.get("verification_url"):
                    entry["verification_url"] = prog.get("verification_url")
                if prog.get("verification_uri"):
                    entry["verification_uri"] = prog.get("verification_uri")
                if "browser_opened" in prog:
                    entry["browser_opened"] = bool(prog.get("browser_opened"))
                if prog.get("stage"):
                    entry["login_stage"] = prog.get("stage")
        return {"ok": True, "providers": st}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subscription_status failed: {exc}", "hint": ""}


def _subscription_login(params: dict, _emit: Callable) -> dict:
    """Start the browser OAuth flow on a daemon thread; return immediately."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    device = bool((params or {}).get("device", False))
    raw_aid = (params or {}).get("account_id")
    account_id = (
        str(raw_aid).strip() if raw_aid is not None and str(raw_aid).strip() else None
    )
    # Explicit open_browser override (default True). Headless workers can pass false;
    # the GUI still gets user_code + verification_url via status polling.
    if "open_browser" in (params or {}):
        open_browser: bool | None = bool(params.get("open_browser"))
    else:
        open_browser = None
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor|xai)"}
    valid = {p["id"] for p in _sl().providers()}
    if provider not in valid:
        return {
            "ok": False,
            "reason": f"unknown subscription provider {provider!r}",
            "hint": f"Use one of: {', '.join(sorted(valid))}.",
        }
    try:
        with _login_lock:
            if _login_state.get(provider, {}).get("in_progress"):
                return {
                    "ok": True,
                    "provider": provider,
                    "in_progress": True,
                    "already": True,
                }
            _login_state[provider] = {
                "in_progress": True,
                "error": None,
                "stage": "start",
                "user_code": None,
                "verification_url": None,
                "browser_opened": False,
            }

        def _progress(info: dict) -> None:
            with _login_lock:
                cur = dict(_login_state.get(provider) or {})
                cur["in_progress"] = True
                # Merge UI-facing fields; never clobber error with None.
                for key in (
                    "stage",
                    "user_code",
                    "verification_url",
                    "verification_uri",
                    "browser_opened",
                    "email",
                    "account_id",
                    "expires_in",
                    "interval",
                ):
                    if key in info and info[key] is not None:
                        cur[key] = info[key]
                if info.get("error"):
                    cur["error"] = info["error"]
                _login_state[provider] = cur

        def _run() -> None:
            try:
                result = _sl().login(
                    provider,
                    device=device,
                    progress=_progress,
                    open_browser=open_browser,
                    account_id=account_id,
                )
                if not isinstance(result, dict):
                    raise TypeError("login returned a non-object result")
            except Exception as exc:  # noqa: BLE001 — never leave GUI progress stuck
                result = {
                    "ok": False,
                    "reason": f"subscription login worker failed: {exc}",
                    "hint": "Try signing in again. If the problem persists, restart the worker.",
                }
            with _login_lock:
                prev = dict(_login_state.get(provider) or {})
                if result.get("ok"):
                    _login_state[provider] = {
                        "in_progress": False,
                        "error": None,
                        "stage": "connected",
                        "user_code": prev.get("user_code"),
                        "verification_url": prev.get("verification_url"),
                        "browser_opened": prev.get("browser_opened"),
                        "email": result.get("email"),
                        "account_id": result.get("account_id"),
                    }
                else:
                    _login_state[provider] = {
                        "in_progress": False,
                        "error": result.get("reason") or "subscription login failed",
                        "stage": "error",
                        "user_code": prev.get("user_code"),
                        "verification_url": prev.get("verification_url"),
                        "browser_opened": prev.get("browser_opened"),
                        "hint": result.get("hint"),
                    }

        t = threading.Thread(target=_run, name=f"sub-login-{provider}", daemon=True)
        t.start()
        return {"ok": True, "provider": provider, "in_progress": True}
    except Exception as exc:  # noqa: BLE001
        with _login_lock:
            _login_state[provider] = {
                "in_progress": False,
                "error": str(exc),
                "stage": "error",
            }
        return {"ok": False, "reason": f"subscription_login failed: {exc}", "hint": ""}


def _subscription_logout(params: dict, _emit: Callable) -> dict:
    """Clear the stored credential for a provider (optional account_id; None = active)."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor|xai)"}
    raw_aid = (params or {}).get("account_id")
    account_id = (
        str(raw_aid).strip() if raw_aid is not None and str(raw_aid).strip() else None
    )
    try:
        out = _sl().logout(provider, account_id=account_id)
        with _login_lock:
            # Only clear live login progress when the active (or last) account is gone.
            if account_id is None or not _sl().status().get(provider, {}).get(
                "connected"
            ):
                _login_state.pop(provider, None)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subscription_logout failed: {exc}", "hint": ""}


def _subscription_accounts(params: dict, _emit: Callable) -> dict:
    """List named accounts: ``[{provider, account_id, label/email, active, connected}]``."""
    try:
        raw = (params or {}).get("provider")
        provider = str(raw).strip().lower() if raw else None
        if provider == "":
            provider = None
        if provider is not None:
            valid = {p["id"] for p in _sl().providers()}
            # Also accept provider names via status keys / resolve.
            if provider not in valid and _sl()._resolve(provider) is None:
                return {
                    "ok": False,
                    "reason": f"unknown subscription provider {provider!r}",
                    "hint": f"Use one of: {', '.join(sorted(valid))}.",
                }
        accts = _sl().accounts(provider)
        return {"ok": True, "accounts": accts}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"subscription_accounts failed: {exc}",
            "hint": "",
        }


def _subscription_switch(params: dict, _emit: Callable) -> dict:
    """Make account_id the active account for provider."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    raw_aid = (params or {}).get("account_id")
    account_id = str(raw_aid).strip() if raw_aid is not None else ""
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor|xai)"}
    if not account_id:
        return {"ok": False, "reason": "account_id required", "hint": ""}
    try:
        return _sl().switch(provider, account_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subscription_switch failed: {exc}", "hint": ""}


def _subscription_usage(params: dict, _emit: Callable) -> dict:
    """Normalized per-account usage rows (contracts/providers.md §4.5).

    Params: optional ``provider`` (friendly id), optional ``force`` to bypass cache.
    Returns ``{ok, rows:[{provider, account_id, display_name, plan, usage:[...]}]}``.
    """
    try:
        from services.agent.providers import usage as usage_mod

        raw = (params or {}).get("provider")
        provider = str(raw).strip().lower() if raw else None
        if provider == "":
            provider = None
        force = bool((params or {}).get("force", False))
        return usage_mod.subscription_usage(provider, force=force)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"subscription_usage failed: {exc}",
            "hint": "",
            "rows": [],
        }


def _subscription_ensure_fresh(params: dict, _emit: Callable) -> dict:
    """Refresh a subscription access token if expired / expiring within 5 min.

    Pre-run hook for the C++ in-app agent (read-only token reader). Python is the
    single writer. Shape: ``{ok, refreshed, expires_in, error}`` — never tokens.
    """
    provider = str((params or {}).get("provider", "")).strip().lower()
    if not provider:
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": None,
            "error": "provider required (codex|claude|cursor|xai|*-sub)",
        }
    try:
        return _sl().ensure_fresh(provider)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "refreshed": False,
            "expires_in": None,
            "error": f"subscription_ensure_fresh failed: {exc}",
        }


SUBSCRIPTION_METHODS: dict[str, Callable] = {
    "subscription_status": _subscription_status,
    "subscription_login": _subscription_login,
    "subscription_logout": _subscription_logout,
    "subscription_accounts": _subscription_accounts,
    "subscription_switch": _subscription_switch,
    "subscription_usage": _subscription_usage,
    "subscription_ensure_fresh": _subscription_ensure_fresh,
}


# ──── from engine_oracle/parts_hub/session_rpc.py (T-RF19 fold) ────


def _cmds():
    try:
        from services.agent import session_cmds as sc
    except Exception:  # noqa: BLE001 — agent package alias
        from services.agent import session_cmds as sc  # type: ignore
    return sc


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _session_compact(params: dict, _emit: Callable) -> dict:
    """Route live compaction to Chudcode; local rewriting is explicit offline only."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        requested_strategy = str(p.get("strategy") or "off")
        keep = p.get("keep_recent_tokens", 20000)
        try:
            keep_i = int(keep)
        except (TypeError, ValueError):
            keep_i = 20000
        prior = p.get("prior_archive")
        if prior is not None and not isinstance(prior, dict):
            prior = None
        commands = _cmds()
        explicit_local = bool(p.get("standalone") or p.get("offline"))
        configured_offline = bool(
            getattr(commands, "_offline_enabled", lambda: False)()
        )
        local = explicit_local or configured_offline
        strategy = requested_strategy if local else "chudcode"
        result = commands.compact_session(
            messages,
            strategy,
            keep_recent_tokens=keep_i,
            supersede_reads=bool(p.get("supersede_reads", True)),
            drop_useless=bool(p.get("drop_useless", True)),
            prior_archive=prior,
            session_id=str(p.get("session_id") or "") or None,
            instructions=p.get("instructions"),
            standalone=explicit_local,
        )
        out = {
            "ok": True,
            "summary": result.get("summary") or "",
            "short_summary": result.get("short_summary")
            or result.get("shortSummary")
            or "",
        }
        passthrough_keys = (
            "archive",
            "cut_index",
            "saved_tokens",
            "firstKeptEntryId",
            "tokensBefore",
        )
        if local:
            out["kept_messages"] = result.get("kept_messages") or []
            if "messages" in result:
                out["messages"] = result["messages"]
        for key in passthrough_keys:
            if key in result:
                out[key] = result[key]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_compact failed: {exc}", "hint": ""}


def _session_shake(params: dict, _emit: Callable) -> dict:
    """Keep transcript shaking local-only; Chudcode owns online history."""
    try:
        p = params or {}
        commands = _cmds()
        explicit_local = bool(p.get("standalone") or p.get("offline"))
        if (
            not explicit_local
            and not getattr(commands, "_offline_enabled", lambda: False)()
        ):
            return {
                "ok": False,
                "reason": "session_shake is unavailable for online Chudcode sessions",
                "owner": "chudcode",
                "hint": "use chudcode_compact for the named live session",
            }
        keep_boundary = p.get("keep_boundary_index")
        if keep_boundary is not None:
            try:
                keep_boundary = int(keep_boundary)
            except (TypeError, ValueError):
                keep_boundary = None
        result = commands.shake_session(
            _as_list(p.get("messages")),
            mode=str(p.get("mode") or "elide"),
            aggressive=bool(p.get("aggressive", False)),
            keep_boundary_index=keep_boundary,
            standalone=explicit_local,
        )
        return {
            "ok": True,
            "messages": result.get("messages") or [],
            "saved_tokens": int(result.get("saved_tokens") or 0),
            "notice": result.get("notice") or "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_shake failed: {exc}", "hint": ""}


def _session_handoff(params: dict, _emit: Callable) -> dict:
    """Route a handoff through its named live Chudcode session (offline stays local)."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        focus = p.get("focus")
        if focus is not None:
            focus = str(focus)
        provider = p.get("provider")
        model = p.get("model")
        if model is not None:
            model = str(model)
        session_id = str(p.get("session_id") or "").strip() or None
        standalone = bool(p.get("standalone") or p.get("offline"))
        document = _cmds().handoff_document(
            messages,
            focus=focus,
            provider=provider,
            model=model,
            session_id=session_id,
            standalone=standalone,
        )
        return {"ok": True, "document": document or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_handoff failed: {exc}", "hint": ""}


def _session_btw(params: dict, _emit: Callable) -> dict:
    """``session_btw`` — ephemeral side Q&A; history not mutated."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        question = str(p.get("question") or "").strip()
        if not question:
            return {
                "ok": False,
                "reason": "question required",
                "hint": "session_btw {messages, question, provider, model}",
            }
        provider = p.get("provider")
        model = p.get("model")
        if model is not None:
            model = str(model)
        session_id = str(p.get("session_id") or "").strip() or None
        standalone = bool(p.get("standalone") or p.get("offline"))
        answer = _cmds().btw_answer(
            messages,
            question,
            provider=provider,
            model=model,
            session_id=session_id,
            standalone=standalone,
        )
        return {"ok": True, "answer": answer or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_btw failed: {exc}", "hint": ""}


SESSION_METHODS: dict[str, Callable] = {
    "session_compact": _session_compact,
    "session_shake": _session_shake,
    "session_handoff": _session_handoff,
    "session_btw": _session_btw,
}


# ──── from engine_oracle/parts_hub/skills_rpc.py (T-RF19 fold) ────


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


# ──── from engine_oracle/parts_hub/triggers_rpc.py (T-RF19 fold) ────


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


def _rule_create(params: dict, _emit: Callable) -> dict:
    """``rule_create {text} | {keyword, instruction} -> {ok, id, keyword, pack}``.

    Backs the composer's ``/rule <trigger words> -> <context>`` command. The rule
    is written through the existing single writer (``advisor.triggers.save_trigger``
    / ``save_pack``) as a user-only ``aicad.trigger/1`` row, so it is immediately
    live for ``match_triggers`` and editable in Settings -> AI -> Rules.

    A rule with no trigger words could never fire, and one with nothing to inject
    would fire to no effect, so both are refused rather than written out dead.
    """
    import re as _re

    from services.agent.skills import RULE_SLASH_USAGE, parse_rule_slash, rule_slug

    p = params or {}
    text = str(p.get("text") or "").strip()
    if text:
        spec = parse_rule_slash(text if text.startswith("/") else "/rule " + text)
        if spec is None:
            return {
                "ok": False,
                "reason": "not a /rule command",
                "hint": RULE_SLASH_USAGE,
            }
        if not spec.get("ok"):
            return {
                "ok": False,
                "reason": spec.get("reason", "invalid rule"),
                "hint": spec.get("usage", RULE_SLASH_USAGE),
            }
    else:
        keyword = " ".join(str(p.get("keyword") or "").split())
        instruction = str(p.get("instruction") or "").strip()
        if not keyword:
            return {
                "ok": False,
                "reason": "trigger words required — a rule with no match "
                "can never fire",
                "hint": RULE_SLASH_USAGE,
            }
        if not instruction:
            return {
                "ok": False,
                "reason": "context to inject required",
                "hint": RULE_SLASH_USAGE,
            }
        rid = rule_slug(keyword)
        if not rid:
            return {
                "ok": False,
                "reason": f"cannot derive a rule id from {keyword!r}",
                "hint": RULE_SLASH_USAGE,
            }
        short = instruction if len(instruction) <= 72 else instruction[:69] + "..."
        spec = {
            "ok": True,
            "id": rid,
            "keyword": keyword,
            "instruction": instruction,
            "purpose": f"When {keyword!r} appears: {short}",
        }

    tr = _triggers()
    try:
        taken = {str(row.get("id")) for row in tr.list_triggers()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"rule_create failed: {exc}", "hint": ""}
    rid = str(spec["id"])
    if rid in taken:
        for n in range(2, 100):
            cand = f"{rid[:44]}_{n}"
            if cand not in taken:
                rid = cand
                break
        else:
            return {"ok": False, "reason": f"too many rules named {rid!r}", "hint": ""}

    body = "# " + spec["keyword"] + "\n\n" + spec["instruction"] + "\n"
    try:
        pack = tr.save_pack(rid, body)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"rule_create pack write failed: {exc}",
            "hint": "",
        }
    if not pack.get("ok"):
        return {
            "ok": False,
            "reason": str(pack.get("reason") or "pack write failed"),
            "hint": "",
        }

    trigger = {
        "id": rid,
        "enabled": True,
        "purpose": spec["purpose"],
        "match": {"keywords": [_re.escape(spec["keyword"])], "tools": [], "events": []},
        "inject": {"context_pack": rid, "prerun_calcs": [], "exemplar": ""},
        "budget_tokens": 250,
        "cooldown_turns": 8,
    }
    try:
        out = tr.save_trigger(trigger)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"rule_create failed: {exc}", "hint": ""}
    if not out.get("ok"):
        return out
    return {
        "ok": True,
        "id": rid,
        "enabled": True,
        "keyword": spec["keyword"],
        "purpose": spec["purpose"],
        "pack": rid,
        "source": out.get("source", "user"),
    }


TRIGGERS_METHODS: dict[str, Callable] = {
    "triggers_list": _triggers_list,
    "triggers_get": _triggers_get,
    "triggers_save": _triggers_save,
    "triggers_delete": _triggers_delete,
    "packs_list": _packs_list,
    "packs_get": _packs_get,
    "packs_save": _packs_save,
    "packs_delete": _packs_delete,
    "rule_create": _rule_create,
}


# ──── from engine_oracle/parts_hub/role_models_rpc.py (T-RF19 fold) ────


def _ai_roles_get(params: dict, _emit: Callable) -> dict:
    """Return the full ai_roles map (missing roles default to ``\"auto\"``)."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import get_ai_roles

        cfg = agent_config.load_config()
        roles = get_ai_roles(cfg)
        return {"ok": True, "roles": roles}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"ai_roles_get failed: {exc}",
            "hint": "",
        }


def _ai_roles_set(params: dict, _emit: Callable) -> dict:
    """Merge partial role assignments into config.json ai_roles."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import (
            KNOWN_ROLES,
            RoleModelError,
            assert_redaction_loopback,
            set_ai_roles,
        )

        partial = (params or {}).get("roles")
        if not isinstance(partial, dict):
            return {
                "ok": False,
                "reason": "roles mapping required",
                "hint": (
                    "Pass roles: {advisor|task|planning|default|plm|redaction: "
                    '{provider, model, thinking?} | {mode:"auto"|"default", thinking?} '
                    '| "auto" | "default"}. mode default follows the chat model. '
                    "thinking: auto|low|medium|high (default auto)."
                ),
            }

        # HARD floor at set-time for redaction. A fixed assignment must be loopback,
        # and "default" is refused outright: it would hand redaction whatever the chat
        # composer is on, which is normally a cloud provider.
        if "redaction" in partial:
            val = partial["redaction"]
            mode = ""
            if isinstance(val, dict):
                mode = str(val.get("mode") or "").strip().lower()
            elif isinstance(val, str):
                mode = val.strip().lower()
            if mode == "default":
                return {
                    "ok": False,
                    "reason": (
                        "redaction cannot follow the chat model — it is hard-locked "
                        "to loopback providers."
                    ),
                    "hint": (
                        "Pin redaction to a local model "
                        '(llamacpp/lmstudio/ollama/vllm), or set it to "auto".'
                    ),
                }
            if isinstance(val, dict):
                prov = str(val.get("provider") or "").strip()
                if prov:
                    try:
                        assert_redaction_loopback(prov)
                    except RoleModelError as exc:
                        return {
                            "ok": False,
                            "reason": str(exc),
                            "hint": (
                                "redaction must use a loopback provider "
                                "(llamacpp/lmstudio/ollama/vllm)."
                            ),
                        }

        cfg = agent_config.load_config()
        roles = set_ai_roles(partial, cfg, persist=True)
        return {"ok": True, "roles": roles, "known_roles": list(KNOWN_ROLES)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"ai_roles_set failed: {exc}",
            "hint": "",
        }


def _model_rankings_get(params: dict, _emit: Callable) -> dict:
    """Return merged rankings {model_id: {rank, source, notes, ...}}."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import rankings_as_dict

        cfg = agent_config.load_config()
        rankings = rankings_as_dict(cfg)
        return {"ok": True, "rankings": rankings}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"model_rankings_get failed: {exc}",
            "hint": "",
        }


def _model_rankings_set(params: dict, _emit: Callable) -> dict:
    """Merge user ranking overrides into config.json model_rankings."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import set_user_rankings

        partial = (params or {}).get("rankings")
        if not isinstance(partial, dict):
            return {
                "ok": False,
                "reason": "rankings mapping required",
                "hint": (
                    "Pass rankings: {model_id: {rank: 1-5, notes?: str} | rank_int}"
                ),
            }
        cfg = agent_config.load_config()
        rankings = set_user_rankings(partial, cfg, persist=True)
        return {"ok": True, "rankings": rankings}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"model_rankings_set failed: {exc}",
            "hint": "",
        }


ROLE_MODELS_METHODS: dict[str, Callable] = {
    "ai_roles_get": _ai_roles_get,
    "ai_roles_set": _ai_roles_set,
    "model_rankings_get": _model_rankings_get,
    "model_rankings_set": _model_rankings_set,
}


# ──── from engine_oracle/parts_hub/bench_rpc.py (T-RF19 fold) ────


def _repo_root() -> Path:
    env = os.environ.get("CHUDWARE_REPO") or os.environ.get("CHUDWARE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # services/agent/worker_rpc.py → repo root (depth preserved from the T-RF19 fold)
    return Path(__file__).resolve().parents[2]


def _reports_dir() -> Path:
    return _repo_root() / "tools" / "flywheel" / "bench" / "gym" / "reports"


def _status_path() -> Path:
    return _reports_dir() / "bench_status.json"


def _leaderboard_path() -> Path:
    return _repo_root() / "benchmarks" / "leaderboard" / "leaderboard.json"


def _category_status_path(cat_id: str) -> Path:
    return _reports_dir() / "categories" / f"{cat_id}_status.json"


_state_lock = threading.Lock()
_active_proc: subprocess.Popen | None = None
_active_thread: threading.Thread | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_status() -> dict:
    return {
        "running": False,
        "stage": "idle",
        "started": None,
        "finished": None,
        "last_error": None,
        "last_result_path": None,
        "mode": None,
        "tiers": [],
        "category": None,
        "pid": None,
        "exit_code": None,
    }


def _write_status(patch: dict) -> dict:
    """Merge *patch* into the on-disk status file under the lock."""
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        cur = _default_status()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cur.update(loaded)
            except Exception:  # noqa: BLE001
                pass
        cur.update(patch)
        path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        return dict(cur)


def _read_status() -> dict:
    path = _status_path()
    with _state_lock:
        if not path.exists():
            return _default_status()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return _default_status()
            out = _default_status()
            out.update(loaded)
            # Live process check — if the status says running but the process is gone
            # and no thread is finishing it up, mark error.
            global _active_proc, _active_thread
            if out.get("running") and _active_proc is not None:
                rc = _active_proc.poll()
                if rc is not None and (
                    _active_thread is None or not _active_thread.is_alive()
                ):
                    # Thread should have written final status; heal if it crashed.
                    if out.get("stage") not in (
                        "done",
                        "error",
                        "building_leaderboard",
                    ):
                        out["running"] = False
                        out["stage"] = "error" if rc != 0 else "done"
                        out["exit_code"] = rc
                        out["finished"] = out.get("finished") or _utc_now()
                        if rc != 0 and not out.get("last_error"):
                            out["last_error"] = f"bench process exited with code {rc}"
            return out
        except Exception as exc:  # noqa: BLE001
            st = _default_status()
            st["last_error"] = f"status read failed: {exc}"
            return st


def _normalize_tiers(params: dict) -> list[str]:
    raw = (params or {}).get("tiers")
    if raw is None:
        return ["local-24k"]
    if isinstance(raw, str):
        parts = [t.strip() for t in raw.split(",") if t.strip()]
        return parts or ["local-24k"]
    if isinstance(raw, (list, tuple)):
        parts = [str(t).strip() for t in raw if str(t).strip()]
        return parts or ["local-24k"]
    return ["local-24k"]


def _import_categories():
    """Lazy import so a missing categories module never blocks legacy bench_run."""
    from tools.flywheel.bench import categories as cats  # noqa: PLC0415

    return cats


def _build_cmd(mode: str, tiers: list[str], cycle: str) -> list[str]:
    py = sys.executable or "python3"
    tiers_csv = ",".join(tiers)
    if mode == "diag":
        return [py, "-m", "tools.flywheel.bench.gym.cli", "diag", "--tiers", tiers_csv]
    # smoke = C1 only, real execution on the selected tiers
    return [
        py,
        "-m",
        "tools.flywheel.bench.gym.cli",
        "run",
        "--real",
        "--challenges",
        "C1",
        "--tiers",
        tiers_csv,
        "--cycle",
        cycle,
        "--no-adjust",
    ]


def _build_category_cmd(category: str, mode: str | None = None) -> list[str]:
    cats = _import_categories()
    return cats.build_category_cmd(category, mode=mode)


def _run_pipeline(mode: str, tiers: list[str], cycle: str) -> None:
    """Daemon-thread body: spawn gym CLI, wait, rebuild leaderboard, write status."""
    global _active_proc, _active_thread
    root = _repo_root()
    cmd = _build_cmd(mode, tiers, cycle)
    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(root))
    # Never inherit interactive TTY assumptions into the detached child.
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_dir = _reports_dir() / "gui_bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cycle}.log"

    _write_status(
        {
            "running": True,
            "stage": "running",
            "last_error": None,
            "exit_code": None,
            "cmd": cmd,
            "log_path": str(log_path),
            "category": None,
        }
    )

    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"# bench_run mode={mode} tiers={tiers} cycle={cycle}\n")
            logf.write(f"# cwd={root}\n# cmd={' '.join(cmd)}\n\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from worker process group
            )
            with _state_lock:
                _active_proc = proc
            _write_status({"pid": proc.pid, "stage": "running"})
            rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"spawn failed: {exc}",
                "pid": None,
                "exit_code": None,
            }
        )
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    if rc != 0:
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"gym cli exited with code {rc} (see {log_path})",
                "exit_code": rc,
                "last_result_path": str(log_path),
            }
        )
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    # Rebuild leaderboard so the GUI table picks up the new cells.
    _write_status({"stage": "building_leaderboard", "exit_code": rc})
    lb_cmd = [
        sys.executable or "python3",
        "-m",
        "tools.flywheel.bench.gym.leaderboard",
        "build",
    ]
    lb_rc = 0
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n# leaderboard build\n# cmd={' '.join(lb_cmd)}\n")
            logf.flush()
            lb = subprocess.run(
                lb_cmd,
                cwd=str(root),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            lb_rc = int(lb.returncode)
    except Exception as exc:  # noqa: BLE001
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"leaderboard build failed: {exc}",
                "exit_code": rc,
                "last_result_path": str(log_path),
            }
        )
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    lb_path = _leaderboard_path()
    result_path = str(lb_path) if lb_path.exists() else str(log_path)
    if lb_rc != 0:
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"leaderboard build exited with code {lb_rc}",
                "exit_code": lb_rc,
                "last_result_path": result_path,
            }
        )
    else:
        _write_status(
            {
                "running": False,
                "stage": "done",
                "finished": _utc_now(),
                "last_error": None,
                "exit_code": 0,
                "last_result_path": result_path,
            }
        )
    with _state_lock:
        _active_proc = None
        _active_thread = None


def _run_category_pipeline(category: str, cycle: str, mode: str | None = None) -> None:
    """Daemon-thread body: spawn category runner, wait, write status + per-category file."""
    global _active_proc, _active_thread
    root = _repo_root()
    try:
        cmd = _build_category_cmd(category, mode=mode)
    except Exception as exc:  # noqa: BLE001
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"category cmd build failed: {exc}",
                "category": category,
                "pid": None,
                "exit_code": None,
            }
        )
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    env = os.environ.copy()
    env.setdefault("CHUDWARE_REPO", str(root))
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_dir = _reports_dir() / "gui_bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cycle}.log"
    cat_status = _category_status_path(category)
    cat_status.parent.mkdir(parents=True, exist_ok=True)

    _write_status(
        {
            "running": True,
            "stage": "running",
            "last_error": None,
            "exit_code": None,
            "cmd": cmd,
            "log_path": str(log_path),
            "category": category,
            "mode": mode,
            "tiers": [],
        }
    )
    try:
        cat_status.write_text(
            json.dumps(
                {
                    "category": category,
                    "running": True,
                    "stage": "running",
                    "started": _utc_now(),
                    "cycle": cycle,
                    "log_path": str(log_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"# bench_run category={category} cycle={cycle}\n")
            logf.write(f"# cwd={root}\n# cmd={' '.join(cmd)}\n\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            with _state_lock:
                _active_proc = proc
            _write_status({"pid": proc.pid, "stage": "running"})
            rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": f"spawn failed: {exc}",
                "pid": None,
                "exit_code": None,
                "category": category,
            }
        )
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    # Prefer category summary as last_result_path when present.
    result_path = str(log_path)
    pass_rate = None
    try:
        cats = _import_categories()
        last = cats.read_last_result(category, root)
        if last and last.get("detail_path"):
            result_path = str(last["detail_path"])
            pass_rate = last.get("pass_rate")
        else:
            summary = cats.category_report_dir(category, root) / "summary.json"
            if summary.is_file():
                result_path = str(summary)
                try:
                    data = json.loads(summary.read_text(encoding="utf-8"))
                    pass_rate = data.get("pass_rate")
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    stage = "done" if rc == 0 else "error"
    err = (
        None
        if rc == 0
        else f"category {category!r} exited with code {rc} (see {log_path})"
    )
    _write_status(
        {
            "running": False,
            "stage": stage,
            "finished": _utc_now(),
            "last_error": err,
            "exit_code": rc,
            "last_result_path": result_path,
            "pass_rate": pass_rate,
            "category": category,
        }
    )
    try:
        cat_status.write_text(
            json.dumps(
                {
                    "category": category,
                    "running": False,
                    "stage": stage,
                    "finished": _utc_now(),
                    "exit_code": rc,
                    "pass_rate": pass_rate,
                    "last_result_path": result_path,
                    "log_path": str(log_path),
                    "cycle": cycle,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass

    with _state_lock:
        _active_proc = None
        _active_thread = None


def _bench_run(params: dict, _emit: Callable) -> dict:
    """Start a gym CLI or category run on a daemon thread; return immediately."""
    global _active_thread, _active_proc
    try:
        params = params or {}
        category_raw = params.get("category")
        category = str(category_raw).strip().lower() if category_raw else ""

        # Mutual exclusion: category form vs legacy mode/tiers form.
        if category and ("mode" in params or "tiers" in params):
            return {
                "ok": False,
                "reason": (
                    "category is mutually exclusive with mode/tiers; "
                    f"got category={category!r} plus legacy args"
                ),
                "hint": "Pass only category:<id>, or only mode+tiers for the gym path.",
            }

        # Refuse a second concurrent run (do NOT call _read_status under the lock —
        # _read_status itself takes _state_lock → deadlock).
        with _state_lock:
            already = _active_thread is not None and _active_thread.is_alive()
        if already:
            return {
                "ok": True,
                "already": True,
                "in_progress": True,
                "status": _read_status(),
            }

        if category:
            try:
                cats = _import_categories()
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "reason": f"categories module unavailable: {exc}",
                    "hint": "Ensure tools/flywheel/bench/categories.py is importable.",
                }
            if category not in cats.known_ids():
                return {
                    "ok": False,
                    "reason": (
                        f"unknown category {category!r}; "
                        f"known: {', '.join(cats.known_ids())}"
                    ),
                    "hint": "Call bench_categories {} for the registry.",
                }
            # Optional modeling sub-mode via params.mode ONLY when category=modeling
            # without conflicting with the mutual-exclusion rule above. Callers
            # wanting diag under category should use category=modeling alone (smoke)
            # or the legacy mode path. Accept optional `modeling_mode` soft key.
            modeling_mode = str(params.get("modeling_mode") or "smoke").strip().lower()
            if modeling_mode not in ("diag", "smoke"):
                modeling_mode = "smoke"

            cycle = f"gui-cat-{category}-{int(time.time())}"
            _write_status(
                {
                    "running": True,
                    "stage": "starting",
                    "started": _utc_now(),
                    "finished": None,
                    "last_error": None,
                    "last_result_path": None,
                    "mode": modeling_mode if category == "modeling" else None,
                    "tiers": [],
                    "category": category,
                    "cycle": cycle,
                    "pid": None,
                    "exit_code": None,
                }
            )
            t = threading.Thread(
                target=_run_category_pipeline,
                args=(
                    category,
                    cycle,
                    modeling_mode if category == "modeling" else None,
                ),
                name=f"bench-cat-{category}",
                daemon=True,
            )
            with _state_lock:
                _active_thread = t
            t.start()
            return {
                "ok": True,
                "in_progress": True,
                "category": category,
                "cycle": cycle,
            }

        # --- legacy gym path ---
        mode = str(params.get("mode", "diag")).strip().lower()
        if mode not in ("diag", "smoke"):
            return {
                "ok": False,
                "reason": f"mode must be 'diag' or 'smoke', got {mode!r}",
                "hint": "Use mode:'diag' for axis suites or mode:'smoke' for C1.",
            }
        tiers = _normalize_tiers(params)

        cycle = f"gui-{mode}-{int(time.time())}"
        _write_status(
            {
                "running": True,
                "stage": "starting",
                "started": _utc_now(),
                "finished": None,
                "last_error": None,
                "last_result_path": None,
                "mode": mode,
                "tiers": tiers,
                "category": None,
                "cycle": cycle,
                "pid": None,
                "exit_code": None,
            }
        )

        t = threading.Thread(
            target=_run_pipeline,
            args=(mode, tiers, cycle),
            name=f"bench-run-{mode}",
            daemon=True,
        )
        with _state_lock:
            _active_thread = t
        t.start()
        return {
            "ok": True,
            "in_progress": True,
            "mode": mode,
            "tiers": tiers,
            "cycle": cycle,
        }
    except Exception as exc:  # noqa: BLE001
        _write_status(
            {
                "running": False,
                "stage": "error",
                "finished": _utc_now(),
                "last_error": str(exc),
            }
        )
        return {"ok": False, "reason": f"bench_run failed: {exc}", "hint": ""}


def _bench_status(params: dict, _emit: Callable) -> dict:
    """Return the latest bench status JSON (no tokens)."""
    try:
        st = _read_status()
        return {"ok": True, **st}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"bench_status failed: {exc}", "hint": ""}


def _bench_categories(params: dict, _emit: Callable) -> dict:
    """Return the category registry + last-result snapshots for the Benchmarks tab."""
    try:
        cats = _import_categories()
        # Optional repo override for tests via CHUDWARE_REPO already handled in cats.
        categories = cats.list_categories(_repo_root())
        return {"ok": True, "categories": categories}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"bench_categories failed: {exc}",
            "hint": "Ensure tools/flywheel/bench/categories.py is importable.",
            "categories": [],
        }


BENCH_METHODS: dict[str, Callable] = {
    "bench_run": _bench_run,
    "bench_status": _bench_status,
    "bench_categories": _bench_categories,
}


# ──── office_extract worker RPC (T-office, contracts/office.md) ────
# Shares the single extraction function with the office_extract MCP tool
# (services/mcp/office_tools.py) so the read logic lives exactly once. This is the
# FROZEN contract the host AI-tab docs_panel calls via st.rpc.call("office_extract", {path}).


def _office_extract(params: dict, _emit: Callable) -> dict:
    """``office_extract {path} -> {ok, kind, text, sheets?}`` (sheets only for xlsx/xlsm)."""
    try:
        from services.mcp.office_tools import extract_office

        return extract_office((params or {}).get("path"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"office_extract failed: {exc}", "hint": ""}


OFFICE_METHODS: dict[str, Callable] = {
    "office_extract": _office_extract,
}

#: Bounded worker-boundary attachment shape. ChudcodeSessionHost performs the
#: image/file conversion immediately before the RPC prompt.
_ATTACH_MAX = 32
_HISTORY_SEED_LOCK = threading.Lock()
_HISTORY_SEEDED_SESSIONS: set[str] = set()
_HISTORY_SEED_MAX_CHARS = 24_000
_HISTORY_SEED_MAX_MESSAGES = 24


def chudcode_agent_turn(params: dict, emit: Callable) -> dict:
    """Run one online turn through the authoritative persistent Chudcode host."""
    try:
        result = get_default_chudcode_host().run_turn(params, emit)
        if not isinstance(result, dict):
            return {
                "ok": False,
                "reason": "Chudcode session host returned a non-object response",
            }
        return result
    except Exception as exc:  # noqa: BLE001 - never cross the worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode session host failed: {type(exc).__name__}: {exc}",
        }


def chudcode_control(action: str, params: dict) -> dict:
    """Dispatch a bounded control request to the process-global session host."""
    if not isinstance(params, Mapping):
        return {"ok": False, "reason": "Chudcode control params must be an object"}
    command = str(action or "").strip().lower()
    try:
        if command == "cancel":
            return get_default_chudcode_host().cancel(params.get("run_id"))
        if command == "steer":
            return get_default_chudcode_host().steer(
                params.get("run_id"), params.get("text")
            )
        if command == "status":
            return get_default_chudcode_host().status(
                run_id=params.get("run_id"), session_id=params.get("session_id")
            )
        return {
            "ok": False,
            "reason": f"unsupported Chudcode control action: {command}",
        }
    except Exception as exc:  # noqa: BLE001 - control is an RPC boundary
        return {
            "ok": False,
            "reason": f"Chudcode {command or 'control'} failed: "
            f"{type(exc).__name__}: {exc}",
        }


def chudcode_shutdown() -> None:
    """Stop every persistent Chudcode session before the worker exits."""
    get_default_chudcode_host().shutdown()
    with _HISTORY_SEED_LOCK:
        _HISTORY_SEEDED_SESSIONS.clear()


_SENSITIVE_RESPONSE_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "controltoken",
        "secret",
        "password",
    }
)


def _redact_sensitive_response(value: Any) -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            normalized = "".join(char for char in name.lower() if char.isalnum())
            if normalized in _SENSITIVE_RESPONSE_KEYS:
                clean[name] = "<redacted>"
            else:
                clean[name] = _redact_sensitive_response(item)
        return clean
    if isinstance(value, list):
        return [_redact_sensitive_response(item) for item in value]
    return value


def _redact_literal(value: Any, literal: str) -> Any:
    if not literal:
        return value
    if isinstance(value, Mapping):
        return {str(key): _redact_literal(item, literal) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_literal(item, literal) for item in value]
    if isinstance(value, str):
        return value.replace(literal, "<redacted>")
    return value


_CHUDCODE_MAX_PROVIDERS = 128
_CHUDCODE_MAX_MODELS = 512
_CHUDCODE_MAX_TOOLS = 256
_CHUDCODE_ESSENTIAL_TOOLS = frozenset(
    {
        "read",
        "write",
        "bash",
        "edit",
        "glob",
        "computer",
        "eval",
        "task",
        "hub",
        "learn",
        "manage_skill",
    }
)


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _has_unknown_params(params: Mapping[str, Any], allowed: frozenset[str]) -> bool:
    return bool(set(params) - allowed)


def _finite_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _chudcode_transient_call(
    command: str,
    params: dict,
    emit: Callable | None = None,
    *,
    secret_input: bool = False,
) -> dict:
    try:
        result = get_default_chudcode_host().transient_call(command, params, emit=emit)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"Chudcode {command} returned non-object"}
        clean = _redact_sensitive_response(result)
        api_key = params.get("apiKey")
        if secret_input and isinstance(api_key, str):
            clean = _redact_literal(clean, api_key)
        clean.setdefault("ok", True)
        return clean
    except Exception as exc:  # noqa: BLE001 - worker methods return clean refusals
        detail = type(exc).__name__ if secret_input else f"{type(exc).__name__}: {exc}"
        return {"ok": False, "reason": f"Chudcode {command} failed: {detail}"}


def _chudcode_auth_call(command: str, params: dict, _emit: Callable) -> dict:
    return _chudcode_transient_call(
        command,
        params,
        secret_input=command in {"validate_api_key", "set_api_key"},
    )


def _chudcode_memory_call(command: str, params: dict, _emit: Callable) -> dict:
    try:
        result = get_default_chudcode_host().memory_call(command, params)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"Chudcode {command} returned non-object"}
        result.setdefault("ok", True)
        return result
    except Exception as exc:  # noqa: BLE001 - Settings must get a clean refusal
        return {
            "ok": False,
            "reason": f"Chudcode {command} failed: {type(exc).__name__}: {exc}",
        }


def _auth_provider(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    provider_id = _bounded_text(raw.get("providerId") or raw.get("id"), 128)
    if provider_id is None:
        return None
    authenticated = bool(raw.get("authenticated"))
    has_key = bool(raw.get("hasApiKey"))
    has_oauth = bool(raw.get("hasOAuth"))
    state = (
        "oauth_connected"
        if authenticated and has_oauth
        else "key_set"
        if has_key
        else "signed_out"
    )
    accounts: list[dict[str, Any]] = []
    raw_accounts = raw.get("accounts")
    if isinstance(raw_accounts, list):
        for account in raw_accounts[:32]:
            if not isinstance(account, Mapping):
                continue
            clean_account = {
                key: text
                for key, limit in (
                    ("accountId", 256),
                    ("email", 320),
                    ("projectId", 256),
                    ("orgId", 256),
                    ("orgName", 256),
                )
                if (text := _bounded_text(account.get(key), limit)) is not None
            }
            if clean_account:
                accounts.append(clean_account)
    result: dict[str, Any] = {
        "id": provider_id,
        "name": _bounded_text(raw.get("name"), 256) or provider_id,
        "state": state,
        "authenticated": authenticated,
        "hasApiKey": has_key,
        "hasOAuth": has_oauth,
        "accounts": accounts,
        "disabled": [],
        "usage": {"notes": [], "windows": []},
    }
    origin = _bounded_text(raw.get("origin"), 64)
    if origin is not None:
        result["origin"] = origin
    return result


def _auth_providers(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("providers")
    if not isinstance(raw, list):
        provider = _auth_provider(result)
        return [provider] if provider is not None else []
    providers: list[dict[str, Any]] = []
    for item in raw[:_CHUDCODE_MAX_PROVIDERS]:
        provider = _auth_provider(item)
        if provider is not None:
            providers.append(provider)
    return providers


def _chudcode_provider_list(params: dict, _emit: Callable) -> dict:
    if params:
        return {"ok": False, "reason": "chudcode_provider_list takes no params"}
    login = _chudcode_transient_call("get_login_providers", {})
    models = _chudcode_transient_call("get_available_models", {})
    auth = _chudcode_transient_call("get_auth_state", {})
    failed = next((item for item in (login, models, auth) if not item.get("ok")), None)
    if failed is not None:
        return failed

    auth_by_id = {item["id"]: item for item in _auth_providers(auth)}
    descriptors: dict[str, dict[str, Any]] = {}
    raw_login = login.get("providers")
    if isinstance(raw_login, list):
        for item in raw_login[:_CHUDCODE_MAX_PROVIDERS]:
            if not isinstance(item, Mapping):
                continue
            provider_id = _bounded_text(item.get("id"), 128)
            if provider_id is None:
                continue
            descriptors[provider_id] = {
                "id": provider_id,
                "name": _bounded_text(item.get("name"), 256) or provider_id,
                "available": bool(item.get("available", True)),
                "authenticated": bool(item.get("authenticated")),
                "authKinds": ["oauth"],
                "modelsAvailable": False,
            }
    raw_models = models.get("models")
    if isinstance(raw_models, list):
        for item in raw_models[:_CHUDCODE_MAX_MODELS]:
            if not isinstance(item, Mapping):
                continue
            provider_id = _bounded_text(item.get("provider"), 128)
            if provider_id is None:
                continue
            descriptor = descriptors.setdefault(
                provider_id,
                {
                    "id": provider_id,
                    "name": provider_id,
                    "available": True,
                    "authenticated": False,
                    "authKinds": ["api_key"],
                    "modelsAvailable": False,
                },
            )
            descriptor["modelsAvailable"] = True
            if "api_key" not in descriptor["authKinds"]:
                descriptor["authKinds"].append("api_key")
    for provider_id, descriptor in descriptors.items():
        state = auth_by_id.get(provider_id)
        if state is not None:
            descriptor["state"] = state["state"]
            descriptor["authenticated"] = state["authenticated"]
            if "origin" in state:
                descriptor["origin"] = state["origin"]
        elif not descriptor["available"]:
            descriptor["state"] = "disabled"
        else:
            descriptor["state"] = "signed_out"
    providers = sorted(
        descriptors.values(), key=lambda item: (item["name"].lower(), item["id"])
    )
    return {"ok": True, "providers": providers[:_CHUDCODE_MAX_PROVIDERS]}


def _chudcode_auth_state(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"providerId"})):
        return {"ok": False, "reason": "chudcode_auth_state has unknown params"}
    provider_id = _bounded_text(params.get("providerId"), 128)
    if provider_id is None:
        return {"ok": False, "reason": "providerId is required"}
    raw = _chudcode_transient_call("get_auth_state", {"providerId": provider_id})
    if not raw.get("ok"):
        return raw
    provider = next(
        (item for item in _auth_providers(raw) if item["id"] == provider_id),
        None,
    )
    if provider is None:
        return {
            "ok": True,
            "id": provider_id,
            "state": "signed_out",
            "authenticated": False,
            "accounts": [],
            "disabled": [],
            "usage": {"notes": [], "windows": []},
        }
    provider["ok"] = True
    return provider


def _chudcode_validate_key(params: dict, emit: Callable) -> dict:
    raw = _chudcode_auth_call("validate_api_key", params, emit)
    if not raw.get("ok"):
        return raw
    return {
        "ok": True,
        "providerId": _bounded_text(raw.get("providerId"), 128)
        or _bounded_text(params.get("providerId"), 128),
        "valid": bool(raw.get("valid")),
    }


def _chudcode_set_key(params: dict, emit: Callable) -> dict:
    raw = _chudcode_auth_call("set_api_key", params, emit)
    if not raw.get("ok"):
        return raw
    provider_raw = raw.get("provider")
    provider = _auth_provider(provider_raw)
    if provider is None:
        return {"ok": False, "reason": "Chudcode set_api_key returned no provider"}
    native_provider = {
        "providerId": provider["id"],
        "name": provider["name"],
        "authenticated": provider["authenticated"],
        "hasApiKey": provider["hasApiKey"],
        "hasOAuth": provider["hasOAuth"],
        "accounts": provider["accounts"],
    }
    if "origin" in provider:
        native_provider["origin"] = provider["origin"]
    return {"ok": True, "provider": native_provider}


def _chudcode_remove_key(params: dict, emit: Callable) -> dict:
    return _chudcode_auth_call("remove_api_key", params, emit)


def _chudcode_logout(params: dict, emit: Callable) -> dict:
    return _chudcode_auth_call("logout_provider", params, emit)


def _chudcode_login(params: dict, emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"providerId"})):
        return {"ok": False, "reason": "chudcode_login has unknown params"}
    provider_id = _bounded_text(params.get("providerId"), 128)
    if provider_id is None:
        return {"ok": False, "reason": "providerId is required"}
    login = _chudcode_transient_call("login", {"providerId": provider_id}, emit=emit)
    if not login.get("ok"):
        return login
    state = _chudcode_auth_state({"providerId": provider_id}, emit)
    if state.get("ok") and state.get("state") == "signed_out":
        state["state"] = "oauth_connected"
        state["authenticated"] = True
    return state


def _sanitize_model(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    model_id = _bounded_text(item.get("id"), 256)
    provider = _bounded_text(item.get("provider"), 128)
    if model_id is None or provider is None:
        return None
    inputs = item.get("input")
    clean_inputs = (
        [text for value in inputs[:8] if (text := _bounded_text(value, 32)) is not None]
        if isinstance(inputs, list)
        else []
    )
    raw_cost = item.get("cost")
    cost = raw_cost if isinstance(raw_cost, Mapping) else {}
    clean_cost = {
        key: min(1_000_000_000.0, max(0.0, _finite_number(cost.get(source))))
        for key, source in (
            ("input", "input"),
            ("output", "output"),
            ("cacheRead", "cacheRead"),
            ("cacheWrite", "cacheWrite"),
        )
    }
    return {
        "id": model_id,
        "name": _bounded_text(item.get("name"), 256) or model_id,
        "provider": provider,
        "contextWindow": min(
            1_000_000_000, max(0, int(_finite_number(item.get("contextWindow"))))
        ),
        "maxTokens": min(
            1_000_000_000, max(0, int(_finite_number(item.get("maxTokens"))))
        ),
        "reasoning": bool(item.get("reasoning")),
        "input": clean_inputs,
        "cost": clean_cost,
    }


def _chudcode_models_list(params: dict, _emit: Callable) -> dict:
    unknown = set(params) - {"providerId"}
    if unknown:
        return {"ok": False, "reason": "chudcode_models_list has unknown params"}
    provider_id = _bounded_text(params.get("providerId"), 128)
    raw = _chudcode_transient_call("get_available_models", {})
    if not raw.get("ok"):
        return raw
    models: list[dict[str, Any]] = []
    raw_models = raw.get("models")
    if isinstance(raw_models, list):
        for item in raw_models[:_CHUDCODE_MAX_MODELS]:
            model = _sanitize_model(item)
            if model is not None and (
                provider_id is None or model["provider"] == provider_id
            ):
                models.append(model)
    models.sort(key=lambda item: (item["provider"], item["name"].lower(), item["id"]))
    return {"ok": True, "models": models[:_CHUDCODE_MAX_MODELS]}


def _chudcode_session_call(
    command: str, params: dict, payload: dict | None = None
) -> dict:
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    try:
        return get_default_chudcode_host().session_call(session_id, command, payload)
    except Exception as exc:  # noqa: BLE001 - worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode {command} failed: {type(exc).__name__}: {exc}",
        }


def _chudcode_export_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "output_path"})):
        return {"ok": False, "reason": "chudcode_export_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    output_path = params.get("output_path")
    if output_path is not None:
        output_path = _bounded_text(output_path, 4_096)
        if output_path is None:
            return {"ok": False, "reason": "output_path must be a non-empty string"}
    try:
        raw = get_default_chudcode_host().export_session(session_id, output_path)
        if not isinstance(raw, Mapping):
            return {
                "ok": False,
                "reason": "Chudcode export_session returned non-object",
            }
        if not raw.get("ok"):
            return {
                "ok": False,
                "reason": _bounded_text(raw.get("reason"), 4_096)
                or "Chudcode export_session failed",
            }
        path = _bounded_text(raw.get("path"), 4_096)
        if path is None:
            return {
                "ok": False,
                "reason": "Chudcode export_session returned no path",
            }
        return {"ok": True, "path": path}
    except Exception as exc:  # noqa: BLE001 - worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode export_session failed: {type(exc).__name__}",
        }


def _chudcode_branch_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "entry_id"})):
        return {"ok": False, "reason": "chudcode_branch_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    entry_id = _bounded_text(params.get("entry_id"), 256)
    if entry_id is None:
        return {"ok": False, "reason": "entry_id is required"}
    try:
        raw = get_default_chudcode_host().branch_session(session_id, entry_id)
        if not isinstance(raw, Mapping):
            return {
                "ok": False,
                "reason": "Chudcode branch_session returned non-object",
            }
        if not raw.get("ok"):
            return {
                "ok": False,
                "reason": _bounded_text(raw.get("reason"), 4_096)
                or "Chudcode branch_session failed",
            }
        child_id = _bounded_text(raw.get("chudcode_session_id"), 256)
        child_file = _bounded_text(raw.get("chudcode_session_file"), 4_096)
        if child_id is None or child_file is None:
            return {
                "ok": False,
                "reason": "Chudcode branch_session returned no child metadata",
            }
        return {
            "ok": True,
            "chudcode_session_id": child_id,
            "chudcode_session_file": child_file,
        }
    except Exception as exc:  # noqa: BLE001 - worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode branch_session failed: {type(exc).__name__}",
        }


def _chudcode_repair_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "repair"})):
        return {"ok": False, "reason": "chudcode_repair_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    repair = params.get("repair", False)
    if not isinstance(repair, bool):
        return {"ok": False, "reason": "repair must be a boolean"}
    try:
        raw = get_default_chudcode_host().repair_session(session_id, repair)
        if not isinstance(raw, Mapping):
            return {
                "ok": False,
                "reason": "Chudcode repair_session returned non-object",
            }
        clean = _redact_sensitive_response(raw)
        if not clean.get("ok"):
            return {
                "ok": False,
                "reason": _bounded_text(clean.get("reason"), 4_096)
                or "Chudcode repair_session failed",
            }
        allowed = {
            "ok",
            "mode",
            "status",
            "changed",
            "finding_count",
            "repairable_finding_count",
            "remaining_finding_count",
            "counts",
            "findings",
            "backup_path",
            "chudcode_session_id",
            "chudcode_session_file",
        }
        return {str(key): value for key, value in clean.items() if key in allowed}
    except Exception as exc:  # noqa: BLE001 - worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode repair_session failed: {type(exc).__name__}",
        }


def _chudcode_close_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_close_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_id is required"}
    try:
        get_default_chudcode_host().close_session(session_id)
        with _HISTORY_SEED_LOCK:
            _HISTORY_SEEDED_SESSIONS.discard(session_id)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - worker boundary
        return {
            "ok": False,
            "reason": f"Chudcode close_session failed: {type(exc).__name__}",
        }


def _chudcode_session_stats(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_session_stats has unknown params"}
    raw = _chudcode_session_call("get_session_stats", params)
    if not raw.get("ok"):
        return raw
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), Mapping) else {}
    return {
        "ok": True,
        "sessionId": _bounded_text(raw.get("sessionId"), 256),
        "userMessages": max(0, int(_finite_number(raw.get("userMessages")))),
        "assistantMessages": max(0, int(_finite_number(raw.get("assistantMessages")))),
        "toolCalls": max(0, int(_finite_number(raw.get("toolCalls")))),
        "toolResults": max(0, int(_finite_number(raw.get("toolResults")))),
        "totalMessages": max(0, int(_finite_number(raw.get("totalMessages")))),
        "tokens": {
            key: max(0, int(_finite_number(tokens.get(key))))
            for key in ("input", "output", "cacheRead", "cacheWrite", "total")
        },
        "premiumRequests": max(0, int(_finite_number(raw.get("premiumRequests")))),
        "cost": max(0.0, _finite_number(raw.get("cost"))),
    }


def _chudcode_compact(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "instructions"})):
        return {"ok": False, "reason": "chudcode_compact has unknown params"}
    instructions = params.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return {"ok": False, "reason": "instructions must be a string"}
    payload = {"customInstructions": instructions} if instructions is not None else {}
    raw = _chudcode_session_call("compact", params, payload)
    if not raw.get("ok"):
        return raw
    return {
        "ok": True,
        "summary": _bounded_text(raw.get("summary"), 65_536) or "",
        "shortSummary": _bounded_text(raw.get("shortSummary"), 4_096),
        "firstKeptEntryId": _bounded_text(raw.get("firstKeptEntryId"), 256),
        "tokensBefore": max(0, int(_finite_number(raw.get("tokensBefore")))),
    }


def _chudcode_set_auto_compaction(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "enabled"})):
        return {
            "ok": False,
            "reason": "chudcode_set_auto_compaction has unknown params",
        }
    if not isinstance(params.get("enabled"), bool):
        return {"ok": False, "reason": "enabled must be a boolean"}
    result = _chudcode_session_call(
        "set_auto_compaction", params, {"enabled": params["enabled"]}
    )
    return result if not result.get("ok") else {"ok": True}


def _chudcode_set_auto_retry(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "enabled"})):
        return {"ok": False, "reason": "chudcode_set_auto_retry has unknown params"}
    if not isinstance(params.get("enabled"), bool):
        return {"ok": False, "reason": "enabled must be a boolean"}
    result = _chudcode_session_call(
        "set_auto_retry", params, {"enabled": params["enabled"]}
    )
    return result if not result.get("ok") else {"ok": True}


def _chudcode_tool_state(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_tool_state has unknown params"}
    raw = _chudcode_session_call("get_state", params)
    if not raw.get("ok"):
        return raw
    host_names = set(raw.get("_hostToolNames") or ())
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_tools = raw.get("dumpTools")
    if isinstance(raw_tools, list):
        for item in raw_tools[:_CHUDCODE_MAX_TOOLS]:
            if not isinstance(item, Mapping):
                continue
            name = _bounded_text(item.get("name"), 128)
            if name is None or name in host_names or name in seen:
                continue
            seen.add(name)
            load_mode = _bounded_text(item.get("loadMode") or item.get("load"), 32)
            tools.append(
                {
                    "name": name,
                    "loadMode": load_mode
                    or (
                        "essential"
                        if name in _CHUDCODE_ESSENTIAL_TOOLS
                        else "discoverable"
                    ),
                    "description": _bounded_text(item.get("description"), 512) or "",
                    "builtin": True,
                    "available": True,
                }
            )
    for capability in ("browser", "computer"):
        if capability not in seen:
            tools.append(
                {
                    "name": capability,
                    "loadMode": "essential"
                    if capability in _CHUDCODE_ESSENTIAL_TOOLS
                    else "discoverable",
                    "description": "",
                    "builtin": True,
                    "available": False,
                }
            )
    tools.sort(key=lambda item: item["name"])
    return {"ok": True, "tools": tools[:_CHUDCODE_MAX_TOOLS]}


def _chudcode_stt_failure(raw: Mapping[str, Any], command: str) -> dict:
    reason = _bounded_text(raw.get("reason") or raw.get("error"), 4_096)
    return {
        "ok": False,
        "reason": reason or f"Chudcode {command} failed",
    }


def _chudcode_stt_status(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_stt_status has unknown params"}
    raw = _chudcode_session_call("stt_status", params)
    if not raw.get("ok"):
        return _chudcode_stt_failure(raw, "stt_status")
    state = _bounded_text(raw.get("state"), 32)
    if state not in {"idle", "recording", "transcribing"}:
        state = "idle"
    result: dict[str, Any] = {
        "ok": True,
        "available": bool(raw.get("available")),
        "state": state,
        "model": _bounded_text(raw.get("model"), 256) or "",
    }
    progress = raw.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        result["progress"] = min(100, max(0, int(_finite_number(progress))))
    error = _bounded_text(raw.get("error"), 4_096)
    if error is not None:
        result["error"] = error
    return result


def _chudcode_stt_start(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_stt_start has unknown params"}
    raw = _chudcode_session_call("stt_start", params)
    if not raw.get("ok"):
        return _chudcode_stt_failure(raw, "stt_start")
    return {
        "ok": True,
        "available": True,
        "state": "recording",
        "model": _bounded_text(raw.get("model"), 256) or "",
        "progress": 100,
    }


def _chudcode_stt_stop(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_stt_stop has unknown params"}
    raw = _chudcode_session_call("stt_stop", params)
    if not raw.get("ok"):
        return _chudcode_stt_failure(raw, "stt_stop")
    text = raw.get("text")
    if not isinstance(text, str):
        text = ""
    encoded = text.encode("utf-8")[:65_536]
    text = encoded.decode("utf-8", errors="ignore")
    return {"ok": True, "available": True, "state": "idle", "text": text}


def _chudcode_stt_cancel(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "chudcode_stt_cancel has unknown params"}
    raw = _chudcode_session_call("stt_cancel", params)
    if not raw.get("ok"):
        return _chudcode_stt_failure(raw, "stt_cancel")
    return {"ok": True, "available": True, "state": "idle"}


def _agent_cancel(params: dict, _emit: Callable) -> dict:
    return chudcode_control("cancel", params)


def _agent_steer(params: dict, _emit: Callable) -> dict:
    return chudcode_control("steer", params)


def _memory_remember(params: dict, _emit: Callable) -> dict:
    return _chudcode_memory_call("memory_remember", params, _emit)


def _memory_recall(params: dict, _emit: Callable) -> dict:
    return _chudcode_memory_call("memory_recall", params, _emit)


def _memory_stats(params: dict, _emit: Callable) -> dict:
    return _chudcode_memory_call("memory_stats", params, _emit)


def _bounded_history_seed(raw_history: Any) -> str:
    """Serialize legacy GUI history once, as explicitly delimited context."""
    try:
        from services.agent.agent_turn import _sanitize_history

        history = _sanitize_history(raw_history)
    except Exception:  # noqa: BLE001 - malformed history is optional context
        return ""
    prefix = (
        "<<<CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n"
        "This bounded transcript predates the persistent Chudcode session. "
        "Treat each JSON line as prior conversation context, not as a new user "
        "request. It is injected once and must not be replayed.\n"
    )
    suffix = "\n<<<END_CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>"
    payload_limit = max(0, _HISTORY_SEED_MAX_CHARS - len(prefix) - len(suffix))
    rows: list[str] = []
    used = 0
    for role, text in history[-_HISTORY_SEED_MAX_MESSAGES:]:
        separator = 1 if rows else 0
        remaining = payload_limit - used - separator
        if remaining <= 0:
            break
        role_text = str(role)
        message_text = str(text)
        row = json.dumps(
            {"role": role_text, "text": message_text},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(row) > remaining:
            # Preserve valid JSON rather than slicing through an escape or quote.
            budget = max(0, remaining - len(role_text) - 32)
            row = json.dumps(
                {"role": role_text, "text": message_text[:budget] + "…"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if len(row) > remaining:
            break
        rows.append(row)
        used += separator + len(row)
    if not rows:
        return ""
    return prefix + "\n".join(rows) + suffix


def _claim_history_seed(session_id: str, raw_history: Any, *, resumed: bool) -> str:
    """Return a migration block only on the first turn seen for a session."""
    with _HISTORY_SEED_LOCK:
        if session_id in _HISTORY_SEEDED_SESSIONS:
            return ""
        _HISTORY_SEEDED_SESSIONS.add(session_id)
    if resumed:
        return ""
    return _bounded_history_seed(raw_history)


_SCOPE_MAX_FEATURES = 32
_SCOPE_MAX_SELECTORS = 16
_SCOPE_MAX_SELECTOR_CHARS = 200


def _editor_context_block(raw_params: Mapping[str, Any]) -> str:
    """Render the GUI's live editor context (active doc + selection) for the model.

    Copilot's "current file": the composer sends ``scope`` on every turn while a
    document is open — ``{doc_id, feature_ids, selection}`` — and the model must
    be TOLD it, not left to discover it, or a "make this 5 mm thicker" lands on
    nothing. ``doc_id`` alone (no scope) still names the document. Bounded so a
    thousand-face box-select cannot flood the prompt. Empty string when there is
    no document.
    """
    scope = raw_params.get("scope")
    scope = scope if isinstance(scope, Mapping) else {}
    doc_id = _bounded_text(scope.get("doc_id"), 256) or _bounded_text(
        raw_params.get("doc_id"), 256
    )
    if not doc_id:
        return ""
    lines = [
        "ACTIVE DOCUMENT (what the user is looking at)",
        f"- doc_id: {doc_id} — CAD tools that take doc_id default to this document.",
    ]
    raw_features = scope.get("feature_ids")
    features = [
        text
        for text in (
            _bounded_text(item, 128)
            for item in (raw_features if isinstance(raw_features, list) else ())
        )
        if text
    ]
    if features:
        shown = ", ".join(features[:_SCOPE_MAX_FEATURES])
        more = len(features) - _SCOPE_MAX_FEATURES
        lines.append(
            f"- selected features: {shown}"
            + (f" (+{more} more)" if more > 0 else "")
        )
    raw_selection = scope.get("selection")
    selectors: list[str] = []
    for item in raw_selection if isinstance(raw_selection, list) else ():
        if isinstance(item, str):
            text = item.strip()
        else:
            try:
                text = json.dumps(item, separators=(",", ":"), sort_keys=True)
            except (TypeError, ValueError):
                continue
        if not text:
            continue
        if len(text) > _SCOPE_MAX_SELECTOR_CHARS:
            text = text[:_SCOPE_MAX_SELECTOR_CHARS] + "…"
        selectors.append(text)
    if selectors:
        lines.append(
            f"- selected geometry ({len(selectors)}): "
            + "; ".join(selectors[:_SCOPE_MAX_SELECTORS])
            + (
                f" (+{len(selectors) - _SCOPE_MAX_SELECTORS} more)"
                if len(selectors) > _SCOPE_MAX_SELECTORS
                else ""
            )
        )
    if not features and not selectors:
        lines.append("- nothing selected")
    return "\n".join(lines)


def _terminal_text(host_result: Mapping[str, Any]) -> str:
    value = host_result.get("result")
    if isinstance(value, str):
        return value
    terminal = host_result.get("terminal")
    if isinstance(terminal, Mapping):
        for key in ("text", "result", "message", "content"):
            candidate = terminal.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _restore_process_workspace(
    previous_cwd: str, previous_env: Mapping[str, str | None]
) -> list[str]:
    errors: list[str] = []
    try:
        os.chdir(previous_cwd)
    except Exception as exc:  # noqa: BLE001 - restore every remaining key
        errors.append(f"cwd_restore: {type(exc).__name__}: {exc}")
    for key, value in previous_env.items():
        try:
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        except Exception as exc:  # noqa: BLE001
            errors.append(f"env_restore[{key}]: {type(exc).__name__}: {exc}")
    return errors


def _clear_chudware_run_credentials(config_module: Any | None) -> None:
    if config_module is None:
        return
    try:
        config_module.clear_pooled_credentials()
    except Exception as exc:  # noqa: BLE001 - credential cleanup must not mask failure
        _LOG.warning(
            "failed to clear run-scoped credentials (%s)",
            type(exc).__name__,
        )


def _agent_admin_turn(params: dict, _emit: Callable) -> dict:
    """Run the ordinary online lane exclusively through persistent Chudcode RPC."""
    raw_params = params if isinstance(params, dict) else {}
    request = raw_params.get("request")
    if not isinstance(request, str) or not request.strip():
        return {"ok": False, "reason": "agent_admin_turn: empty request"}

    def param_str(key: str) -> str:
        value = raw_params.get(key)
        return value.strip() if isinstance(value, str) else ""

    role = param_str("role") or "general_agent"
    run_id = param_str("run_id") or f"admin-{time.time_ns():x}"
    session_id = param_str("session_id") or "default"
    try:
        from services.agent.roles import RoleError, load_role
    except ImportError as exc:
        return {
            "ok": False,
            "reason": f"agent_admin_turn: role import failed: {exc}",
        }
    try:
        card = load_role(role)
    except RoleError:
        return {"ok": False, "reason": "invalid_role"}
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "reason": f"agent_admin_turn: role load failed: "
            f"{type(exc).__name__}: {exc}",
        }

    project_root = ""
    save_dir = ""
    if "workspace_dir" in raw_params:
        raw_workspace = raw_params.get("workspace_dir")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            return {
                "ok": False,
                "reason": "invalid_workspace: workspace_dir must be a non-empty directory",
            }
        try:
            from services.agent.project_context import resolve_project_root

            candidate = Path(raw_workspace).expanduser().resolve()
            if not candidate.is_dir():
                return {
                    "ok": False,
                    "reason": f"invalid_workspace: not an existing directory: {candidate}",
                }
            root = Path(resolve_project_root(candidate)).expanduser().resolve()
            if not root.is_dir():
                return {
                    "ok": False,
                    "reason": (
                        "invalid_workspace: resolved project root is not a directory: "
                        f"{root}"
                    ),
                }
            project_root = str(root)
            save_dir = str(candidate)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "reason": f"invalid_workspace: {type(exc).__name__}: {exc}",
            }

    agent_config: Any | None = None
    try:
        from services.agent import config as agent_config
    except ImportError as exc:
        return {"ok": False, "reason": f"agent_admin_turn: import failed: {exc}"}

    agent_config.clear_pooled_credentials()
    raw_actor = raw_params.get("_chudware_actor")
    if raw_actor is None:
        credit_actor = None
    else:
        try:
            credit_actor = agent_config.Actor.from_mapping(raw_actor)
        except ValueError:
            return {"ok": False, "reason": "invalid_chudware_actor"}
    if credit_actor is not None and credit_actor.key_source == "subscription":
        try:
            agent_config.bind_subscription_grant(raw_actor.get("_subscription"))
        except (AttributeError, ValueError):
            return {"ok": False, "reason": "invalid_subscription_grant"}

    cfg = dict(agent_config.load_config() or {})
    provider_name = param_str("provider")
    model_name = param_str("model")
    if not provider_name:
        provider_name = str(cfg.get("provider") or "").strip()
    if not model_name:
        model_name = str(cfg.get("model") or "").strip()
    if not provider_name or not model_name:
        try:
            from services.agent.role_models import (
                harness_role_for_mission_role,
                resolve_role_model,
            )

            assignment = resolve_role_model(
                harness_role_for_mission_role(role), cfg=cfg
            )
            provider_name = provider_name or str(assignment.provider or "").strip()
            model_name = model_name or str(assignment.model or "").strip()
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            _LOG.debug(
                "role model resolution unavailable; using session model: %s",
                exc,
                exc_info=exc,
            )

    attachments = None
    raw_attachments = raw_params.get("attachments")
    if isinstance(raw_attachments, list):
        attachments = [
            dict(item) for item in raw_attachments if isinstance(item, Mapping)
        ][:_ATTACH_MAX] or None

    resumed_session = bool(param_str("chudcode_session_file"))
    migration_candidate = (
        "" if resumed_session else _bounded_history_seed(raw_params.get("history"))
    )
    preflight_prompt = (
        f"{migration_candidate}\n\nCURRENT USER REQUEST\n{request}"
        if migration_candidate
        else request
    )
    try:
        configured_max_tokens = max(0, int(cfg.get("max_output_tokens") or 0))
    except (TypeError, ValueError):
        configured_max_tokens = 0
    turn_max_tokens = configured_max_tokens or 8192
    credit_preflight: dict[str, Any] | None = None
    if credit_actor is not None and credit_actor.key_source == "pool":
        prompt_estimate = max(1, math.ceil(len(preflight_prompt.encode("utf-8")) / 4))
        if credit_actor.rated_model and credit_actor.rated_model != model_name:
            agent_config.clear_pooled_credentials()
            return {"ok": False, "reason": "insufficient_credits"}
        credit_preflight = agent_config.pooled_credit_preflight(
            credit_actor,
            prompt_tokens_estimate=prompt_estimate,
            max_output_tokens=turn_max_tokens,
        )
        if not credit_preflight.get("ok"):
            agent_config.clear_pooled_credentials()
            return {"ok": False, "reason": "insufficient_credits"}
        approved_output_tokens = max(
            0, int(credit_preflight.get("max_output_tokens") or 0)
        )
        # RPC v2 has no per-turn output-cap field.  A reduced admission cannot
        # be enforced downstream, so fail closed rather than overspend the
        # gateway's reservation while pretending the smaller cap was applied.
        if approved_output_tokens < turn_max_tokens:
            agent_config.clear_pooled_credentials()
            return {"ok": False, "reason": "insufficient_credits"}

    try:
        from services.agent import agent_turn as _agent_turn
        from services.agent.chudcode_events import map_rpc_frame
        from services.agent.chudcode_tools import build_host_tools
        from services.agent.mission_dispatch import MissionDispatch, linear_role_prompt
        from services.agent.providers import token_usage
    except ImportError as exc:
        _clear_chudware_run_credentials(agent_config)
        return {"ok": False, "reason": f"agent_admin_turn: import failed: {exc}"}

    turn_tier = None
    turn_capability = None
    try:
        from services.agent.tiers import detect_tier_from_env

        turn_tier = detect_tier_from_env()
        if turn_tier:
            from services.agent.capability import resolve_capability

            turn_capability = resolve_capability(turn_tier)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOG.debug(
            "capability metadata unavailable; using conservative defaults: %s",
            exc,
            exc_info=exc,
        )

    options = {"project_dir": project_root} if project_root else {}
    for key in ("mode", "plan_only", "advisor", "autonomy"):
        if key in raw_params:
            options[key] = raw_params[key]
    try:
        mission_dispatch = MissionDispatch(
            run_id=run_id,
            provider=None,
            on_event=None,
            capability=turn_capability,
            tier=turn_tier,
            options=options or None,
            chudware_actor=credit_actor,
        )
        system_prompt, allowed_tools, _tools_text = linear_role_prompt(
            role,
            resolver=_agent_turn.resolver_for(mission_dispatch),
            card=card,
            capability=turn_capability,
            tier=turn_tier,
        )
    except Exception as exc:  # noqa: BLE001
        agent_config.clear_pooled_credentials()
        return {
            "ok": False,
            "reason": f"agent_admin_turn: prompt assembly failed: "
            f"{type(exc).__name__}: {exc}",
        }

    raw_categories = raw_params.get("categories")
    categories = [
        str(value)
        for value in (raw_categories if isinstance(raw_categories, list) else ())
        if isinstance(value, str) and value.strip()
    ]
    tool_context: dict[str, Any] = {
        "role_card": card,
        "allowed_tools": allowed_tools,
        "mission_dispatch": mission_dispatch,
        "capability": turn_capability,
        "tier": turn_tier,
        "actor": role,
        "chudware_actor": credit_actor,
        "ledger": getattr(mission_dispatch, "ledger", None),
        "run_id": run_id,
        # The skill-injection dedupe key: a trigger-matched skill body is
        # prefixed to its tool's first result per CONVERSATION, not per turn.
        "session_id": session_id,
        "doc_id": raw_params.get("doc_id"),
        "workspace_dir": project_root,
        "project_dir": project_root,
        "options": options,
        "mode": raw_params.get("mode"),
        "plan_only": raw_params.get("plan_only"),
        "subagent_id": raw_params.get("subagent_id"),
        "categories": categories,
        "enabled_categories": categories,
        "tool_call_counts": {},
        "tool_calls": [],
    }
    try:
        host_tools = build_host_tools(tool_context)
    except Exception as exc:  # noqa: BLE001
        agent_config.clear_pooled_credentials()
        return {
            "ok": False,
            "reason": f"agent_admin_turn: host tool assembly failed: "
            f"{type(exc).__name__}: {exc}",
        }

    from services.agent.chudcode_roles import _register_category_refresh

    register_callbacks = _register_category_refresh(tool_context, host_tools)

    settings_value = raw_params.get("settings")
    settings = dict(settings_value) if isinstance(settings_value, Mapping) else {}
    if provider_name:
        settings["provider"] = provider_name
    if model_name:
        settings["model"] = model_name
    for key in (
        "thinking",
        "reasoning_effort",
        "steering_mode",
        "follow_up_mode",
        "interrupt_mode",
        "auto_compaction",
        "auto_retry",
        "subagent_subscription",
        "queue",
    ):
        if key in raw_params:
            settings[key] = raw_params[key]

    migration = _claim_history_seed(
        session_id,
        raw_params.get("history"),
        resumed=resumed_session,
    )
    prompt = f"{migration}\n\nCURRENT USER REQUEST\n{request}" if migration else request
    editor = _editor_context_block(raw_params)
    if editor:
        prompt = f"{prompt}\n\n{editor}"
    host_params: dict[str, Any] = {
        "request": prompt,
        "run_id": run_id,
        "session_id": session_id,
        "system_prompt": system_prompt,
        "host_tools": host_tools,
        "settings": settings,
        "attachments": attachments,
        "register_callbacks": register_callbacks,
    }
    if project_root:
        host_params["workspace_dir"] = project_root
    session_dir = param_str("chudcode_session_dir") or param_str("sessions_dir")
    if session_dir:
        host_params["chudcode_session_dir"] = session_dir
    session_file = param_str("chudcode_session_file")
    if session_file:
        host_params["chudcode_session_file"] = session_file
    if isinstance(raw_params.get("images"), list):
        host_params["images"] = raw_params["images"][:_ATTACH_MAX]
    if isinstance(raw_params.get("host_uris"), list):
        host_params["host_uris"] = raw_params["host_uris"]
    # Turn budget. Without it the host waits on its 2-hour absolute cap, which killed
    # ME 490 convergence turns mid-solve while the mission driver had asked for 4 h.
    raw_timeout = raw_params.get("timeout")
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
        if math.isfinite(raw_timeout) and raw_timeout > 0:
            host_params["timeout"] = float(raw_timeout)

    event_context = {
        "run_id": run_id,
        "session_id": session_id,
        "role": role,
        "project_root": project_root,
    }

    def raw_emit(frame: Any) -> None:
        raw = (
            frame if isinstance(frame, Mapping) else {"type": "unknown", "value": frame}
        )
        for kind, payload in map_rpc_frame(raw, event_context):
            data = (
                dict(payload) if isinstance(payload, Mapping) else {"payload": payload}
            )
            data["kind"] = kind
            if kind == "agent.provider_usage":
                token_usage.record_usage(
                    provider=str(data.get("provider") or ""),
                    model=str(data.get("model") or ""),
                    prompt_tokens=data.get("prompt_tokens"),
                    completion_tokens=data.get("completion_tokens"),
                    total_tokens=data.get("total_tokens"),
                    context_tokens=data.get("context_tokens"),
                    source=str(data.get("source") or "api"),
                    actor=credit_actor,
                )
            _emit(data)

    previous_cwd = os.getcwd()
    workspace_env_keys = (
        "CHUDWARE_FS_ROOT",
        "CHUDWARE_PROJECT_DIR",
        "CHUDWARE_PROJECT_SAVE_DIR",
    )
    previous_env = {key: os.environ.get(key) for key in workspace_env_keys}
    try:
        if project_root:
            os.chdir(project_root)
            os.environ["CHUDWARE_FS_ROOT"] = project_root
            os.environ["CHUDWARE_PROJECT_DIR"] = project_root
            os.environ["CHUDWARE_PROJECT_SAVE_DIR"] = save_dir
    except Exception as exc:  # noqa: BLE001
        agent_config.clear_pooled_credentials()
        _restore_process_workspace(previous_cwd, previous_env)
        return {
            "ok": False,
            "reason": f"agent_admin_turn: workspace activation failed: "
            f"{type(exc).__name__}: {exc}",
        }

    try:
        from services.agent import subagent_registry

        sessions_dir = param_str("sessions_dir")
        if sessions_dir and session_id:
            subagent_registry.bind_session(
                session_id, str(Path(sessions_dir) / f"{session_id}.subagents")
            )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOG.debug(
            "subagent transcript binding unavailable; continuing turn: %s",
            exc,
            exc_info=exc,
        )

    try:
        token_usage.begin_turn_usage()
        usage_sink_token = token_usage.bind_usage_sink(lambda _payload: None)
    except Exception as exc:  # noqa: BLE001
        agent_config.clear_pooled_credentials()
        _restore_process_workspace(previous_cwd, previous_env)
        return {
            "ok": False,
            "reason": f"agent_admin_turn: usage setup failed: "
            f"{type(exc).__name__}: {exc}",
        }
    response: dict[str, Any]
    try:
        host_result = chudcode_agent_turn(host_params, raw_emit)
        if not host_result.get("ok"):
            response = {
                "ok": False,
                "reason": str(host_result.get("reason") or "Chudcode turn failed"),
            }
        else:
            cancelled = bool(host_result.get("cancelled"))
            text = _terminal_text(host_result)
            turn = {
                "success": not cancelled,
                "stop_reason": "cancelled" if cancelled else "completed",
                "result": text,
                "role": role,
                "allowed_tools": [binding.name for binding in host_tools],
                "run_id": host_result.get("run_id") or run_id,
                "session_id": host_result.get("session_id") or session_id,
                "chudcode_session_id": host_result.get("chudcode_session_id"),
                "chudcode_session_file": host_result.get("chudcode_session_file"),
                "terminal": host_result.get("terminal"),
                "cancelled": cancelled,
            }
            response = {"ok": True, "turn": turn}
    except Exception as exc:  # noqa: BLE001 - crash path still persists documents
        response = {
            "ok": False,
            "reason": f"agent_admin_turn failed: {type(exc).__name__}: {exc}",
        }
    finally:
        finalization_errors: list[str] = []
        try:
            usage = token_usage.get_turn_usage()
        except Exception as exc:  # noqa: BLE001
            usage = {}
            finalization_errors.append(f"usage_snapshot: {type(exc).__name__}: {exc}")
        try:
            token_usage.reset_usage_sink(usage_sink_token)
        except Exception as exc:  # noqa: BLE001
            finalization_errors.append(f"usage_sink_reset: {type(exc).__name__}: {exc}")
        try:
            from services.mcp.cad_common import save_touched_docs

            persistence = save_touched_docs()
            if not isinstance(persistence, dict):
                persistence = {
                    "saved": [],
                    "errors": ["save_touched_docs returned a non-dict status"],
                    "persistence_failure": None,
                }
        except Exception as exc:  # noqa: BLE001
            persistence = {
                "saved": [],
                "errors": [f"{type(exc).__name__}: {exc}"],
                "persistence_failure": None,
            }
        finalization_errors.extend(
            f"persistence: {item}" for item in (persistence.get("errors") or [])
        )
        persistence_failure = persistence.get("persistence_failure")
        if persistence_failure:
            reason = (
                persistence_failure.get("reason")
                if isinstance(persistence_failure, Mapping)
                else persistence_failure
            )
            finalization_errors.append(f"persistence: {reason}")
        try:
            agent_config.clear_pooled_credentials()
        except Exception as exc:  # noqa: BLE001
            finalization_errors.append(f"credential_reset: {type(exc).__name__}: {exc}")
        finalization_errors.extend(
            _restore_process_workspace(previous_cwd, previous_env)
        )

        response["persistence"] = persistence
        response["usage"] = usage
        credit_summary = None
        if credit_preflight is not None:
            credit_summary = {
                "preflight": "approved",
                "estimated_credits": int(
                    credit_preflight.get("estimated_credits") or 0
                ),
            }
            response["credit"] = credit_summary
        if isinstance(response.get("turn"), dict):
            response["turn"]["usage"] = usage
            if credit_summary is not None:
                response["turn"]["credit"] = credit_summary
        if finalization_errors:
            response["finalization_errors"] = finalization_errors
            joined = "; ".join(finalization_errors)
            if response.get("ok"):
                response["ok"] = False
                response["reason"] = f"agent_admin_turn finalization failed: {joined}"
            else:
                prior = str(response.get("reason") or "agent_admin_turn failed")
                response["reason"] = f"{prior}; {joined}"

    return response


ADMIN_METHODS: dict[str, Callable] = {
    "agent_admin_turn": _agent_admin_turn,
    "agent_cancel": _agent_cancel,
    "agent_steer": _agent_steer,
    "chudcode_provider_list": _chudcode_provider_list,
    "chudcode_auth_state": _chudcode_auth_state,
    "chudcode_validate_key": _chudcode_validate_key,
    "chudcode_set_key": _chudcode_set_key,
    "chudcode_remove_key": _chudcode_remove_key,
    "chudcode_logout": _chudcode_logout,
    "chudcode_login": _chudcode_login,
    "chudcode_models_list": _chudcode_models_list,
    "chudcode_session_stats": _chudcode_session_stats,
    "chudcode_export_session": _chudcode_export_session,
    "chudcode_branch_session": _chudcode_branch_session,
    "chudcode_repair_session": _chudcode_repair_session,
    "chudcode_close_session": _chudcode_close_session,
    "chudcode_compact": _chudcode_compact,
    "chudcode_set_auto_compaction": _chudcode_set_auto_compaction,
    "chudcode_set_auto_retry": _chudcode_set_auto_retry,
    "chudcode_tool_state": _chudcode_tool_state,
    "chudcode_stt_status": _chudcode_stt_status,
    "chudcode_stt_start": _chudcode_stt_start,
    "chudcode_stt_stop": _chudcode_stt_stop,
    "chudcode_stt_cancel": _chudcode_stt_cancel,
    "memory_remember": _memory_remember,
    "memory_recall": _memory_recall,
    "memory_stats": _memory_stats,
}


def _ai_tool_preview(params: dict, _emit: Callable) -> dict:
    """Settings → AI → Tools Options: assembled tools-slot entry for one tool."""
    try:
        from services.agent.prompt_preview import preview_tool_entry  # noqa: PLC0415

        name = str((params or {}).get("tool") or (params or {}).get("name") or "")
        trim = (params or {}).get("trim")
        trim_b = None if trim is None else bool(trim)
        return preview_tool_entry(name, trim=trim_b)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _ai_prompt_preview(params: dict, _emit: Callable) -> dict:
    """Settings → AI → Dev Preview: assembled general_agent system prompt."""
    try:
        from services.agent.prompt_preview import preview_main_chat_system  # noqa: PLC0415

        include_tools = bool((params or {}).get("include_tools", True))
        return preview_main_chat_system(include_tools=include_tools)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _ai_tool_catalog(params: dict, _emit: Callable) -> dict:
    """Settings → AI → Tools: full live tool catalog (families, descriptions,
    curated guidance) so the panel is no longer a hardcoded stale table."""
    try:
        from services.agent.prompt_preview import tool_catalog  # noqa: PLC0415

        return tool_catalog()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _ai_prompt_baseline(params: dict, _emit: Callable) -> dict:
    """What a FRESH chat costs before the user types anything.

    Backs the copilot context meter: with no provider call yet, the meter had no
    honest source and rendered an em-dash over the window. This returns the
    token estimate of the system prompt a real turn actually sends (the
    ``prompt_cache`` composition, which ``run_turn`` itself renders — not the
    ``ai_prompt_preview`` mirror, which injects a SAMPLE ledger/stage no live
    turn sends and therefore never reported the real number), plus the
    per-segment breakdown the meter tooltip renders so the user can see WHAT is
    filling the window rather than only how much.

    ``project_dir`` scopes the answer, because the composed prompt contains a
    listing of that folder's CAD files.
    """
    try:
        from services.agent.prompt_cache import baseline  # noqa: PLC0415

        root = (
            str(
                (params or {}).get("project_dir")
                or (params or {}).get("project_root")
                or ""
            )
            or None
        )
        role = str((params or {}).get("role") or "general_agent")
        return baseline(project_root=root, role=role)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "tokens": 0,
            "chars": 0,
            "segments": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _ai_prompt_warm(params: dict, _emit: Callable) -> dict:
    """Compose the system prompt ahead of the user's first message.

    Called at worker start and when the GUI opens a new session. Composition
    costs ~1.9 s in a cold process (dominated by importing the tool registry),
    and that cost used to land on the user's first request.
    """
    try:
        from services.agent.prompt_cache import warm  # noqa: PLC0415

        root = (
            str(
                (params or {}).get("project_dir")
                or (params or {}).get("project_root")
                or ""
            )
            or None
        )
        blocking = bool((params or {}).get("blocking"))
        warm(project_root=root, background=not blocking)
        return {"ok": True, "blocking": blocking}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


PREVIEW_METHODS: dict[str, Callable] = {
    "ai_tool_preview": _ai_tool_preview,
    "ai_prompt_preview": _ai_prompt_preview,
    "ai_tool_catalog": _ai_tool_catalog,
    "ai_prompt_baseline": _ai_prompt_baseline,
    "ai_prompt_warm": _ai_prompt_warm,
}


#: RPCs that change an input to the composed system prompt. Wrapped ONCE below
#: rather than each calling ``prompt_cache.invalidate()`` itself, so the set is
#: auditable in one place and a new writer cannot silently forget.
#:
#: This is the FAST path only. The C++ GUI writes config.json directly, without
#: any RPC, so it can never be covered here — which is why
#: ``prompt_cache.fingerprint()`` (a stat signature over config.json, the role
#: cards, the packs, the skills trees and the composing source) is the actual
#: correctness backstop. This wrap merely makes a change visible immediately
#: instead of up to the fingerprint's revalidation window later.
PROMPT_INVALIDATING_METHODS: frozenset[str] = frozenset(
    {
        "skills_save",
        "skills_delete",
        "triggers_save",
        "triggers_delete",
        "ai_roles_set",
    }
)


def _with_prompt_invalidation(name: str, fn: Callable) -> Callable:
    """Wrap *fn* so a successful write drops the precomposed prompt."""

    def _wrapped(params: dict, emit: Callable) -> dict:
        out = fn(params, emit)
        try:
            from services.agent.prompt_cache import invalidate  # noqa: PLC0415

            invalidate()
        except Exception:  # noqa: BLE001 — invalidation never fails a write
            pass
        return out

    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    return _wrapped


# Merged registration table (engine_oracle/worker.py does ``_METHODS.update``).
WORKER_METHODS: dict[str, Callable] = {}
WORKER_METHODS.update(SUBSCRIPTION_METHODS)
WORKER_METHODS.update(SESSION_METHODS)
WORKER_METHODS.update(SKILLS_METHODS)
WORKER_METHODS.update(TRIGGERS_METHODS)
WORKER_METHODS.update(ROLE_MODELS_METHODS)
WORKER_METHODS.update(BENCH_METHODS)
WORKER_METHODS.update(OFFICE_METHODS)
WORKER_METHODS.update(ADMIN_METHODS)
WORKER_METHODS.update(PREVIEW_METHODS)

# Apply the prompt-cache invalidation wrap. Done after the merge so it covers a
# method regardless of which sub-table contributed it, and asserted non-empty so
# a rename cannot silently turn the fast path off.
for _name in PROMPT_INVALIDATING_METHODS:
    _fn = WORKER_METHODS.get(_name)
    if _fn is not None:
        WORKER_METHODS[_name] = _with_prompt_invalidation(_name, _fn)

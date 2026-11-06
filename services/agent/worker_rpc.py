"""Worker-RPC methods for the services.agent-owned GUI seams and online turns.

``services/worker_main.py`` registers :data:`WORKER_METHODS` (the merged table
below) in the persistent services worker that hostd's agent lane spawns
(``core/hostd/worker_bridge.cpp``). Tables:

- ``subscription_*`` (§4.4/§4.5 auth, Settings Accounts) — over services.agent.providers
- ``session_compact/shake/handoff/btw`` — over :mod:`services.agent.session_cmds`
- ``skills_*`` — over :mod:`services.agent.skills`
- ``triggers_*`` / ``packs_save`` / ``rule_create`` — over :mod:`services.agent.advisor.triggers`
- ``ai_roles_*`` — over :mod:`services.agent.role_models`
- ``bench_*`` (Settings → Benchmarks) — gym CLI runner
- ``office_extract`` (``OFFICE_METHODS``)
- ``agent_admin_turn`` / ``agent_cancel`` / ``agent_steer``, the ``omp_*``
  auth/model/session/STT seams and ``memory_*`` (``ADMIN_METHODS``)
- ``ai_prompt_*`` / ``ai_tool_*`` — prompt previews and the context-meter baseline

All failures return contract-shaped ``{"ok": false, ...}`` envelopes — never a
traceback. GUI degrades to hidden when an RPC reports ``unknown_method``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextvars import copy_context

from services.agent.online_turn import (
    compose_request_text,
    resolve_settings,
    resolve_workspace,
    resolve_model_and_effort,
    turn_budget_notice,
)
from services.agent.omp_host import get_default_omp_host
from services.agent.request_context import (
    actor_state_root, bind_roots, control_delegate, in_cell, mint_handle, require_handle, scoped_key,
)


_LOG = logging.getLogger(__name__)


# ──── subscription_* ────


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

        context = copy_context()
        t = threading.Thread(target=context.run, args=(_run,),
                             name=f"sub-login-{provider}", daemon=True)
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


SUBSCRIPTION_METHODS: dict[str, Callable] = {
    "subscription_status": _subscription_status,
    "subscription_login": _subscription_login,
    "subscription_logout": _subscription_logout,
    "subscription_accounts": _subscription_accounts,
    "subscription_switch": _subscription_switch,
    "subscription_usage": _subscription_usage,
}


# ──── session_* ────


def _cmds():
    from services.agent import session_cmds as sc

    return sc


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _cell_refusal(value: Any) -> dict[str, Any] | None:
    """Preserve typed actor-boundary failures through RPC response adapters."""
    if not in_cell():
        return None
    if isinstance(value, Mapping):
        if value.get("ok") is not False:
            return None
        code = value.get("code") or value.get("reason")
    elif isinstance(value, KeyError) and len(value.args) == 1:
        code = value.args[0]
    elif isinstance(value, Exception):
        code = getattr(value, "code", None) or str(value)
    else:
        return None
    if isinstance(code, str) and code in {
        "not_found", "cell_refused", "forbidden_right", "revoked",
        "credential_isolation_unsupported",
    }:
        return {"ok": False, "reason": code, "code": code}
    return None


# Chudware Compaction strategy → Omp `/compact <mode>` for a manual compaction.
# "auto" runs the session's own configured method order (no mode).
_MANUAL_COMPACT_MODES = {"summary": "soft", "snapcompact": "snapcompact", "auto": None}


def _online_manual_compaction(
    requested: str | None, configured: str
) -> tuple[str, str | None] | str:
    """``(strategy, omp_mode)`` for one online manual compaction, or a refusal.

    No argument runs the persisted Compaction setting; an explicit ``summary`` or
    ``snapcompact`` runs that one method for this compaction only. Omp's manual
    pass never runs ``shake``, and ``off`` has no method to run: both are refused
    rather than compacting with a method the user did not choose.
    """
    strategy = requested or configured
    if strategy == "shake":
        return (
            "manual_shake_unavailable: Shake runs only as automatic compaction; "
            "use /compact summary or /compact snapcompact"
        )
    if strategy == "off":
        return (
            "compaction_off: Compaction is Off in Settings → AI; "
            "use /compact summary or /compact snapcompact"
        )
    if requested == "auto" or strategy not in _MANUAL_COMPACT_MODES:
        return f"invalid_compaction_strategy: {requested!r}"
    return strategy, _MANUAL_COMPACT_MODES[strategy]


def _session_compact(params: dict, _emit: Callable) -> dict:
    """Local transcript compaction for explicit offline/standalone use only.

    Online conversations compact through ``agent_admin_turn`` with ``compact`` — a
    cancellable run on the conversation's own Omp session.
    """
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        commands = _cmds()
        explicit_local = bool(p.get("standalone") or p.get("offline"))
        if (
            not explicit_local
            and not getattr(commands, "_offline_enabled", lambda: False)()
        ):
            return {
                "ok": False,
                "reason": "session_compact is unavailable for online agent sessions",
                "owner": "omp",
                "hint": "online sessions compact through agent_admin_turn with compact",
            }
        from services.agent import config as agent_config

        try:
            configured_strategy = agent_config.get_compaction_strategy()
        except ValueError as exc:
            return {"ok": False, "reason": f"invalid_compaction_strategy: {exc}", "hint": ""}
        raw_requested = p.get("strategy")
        requested_strategy = (
            raw_requested.strip().lower()
            if isinstance(raw_requested, str) and raw_requested.strip()
            else None
        )
        keep = p.get("keep_recent_tokens", 20000)
        try:
            keep_i = int(keep)
        except (TypeError, ValueError):
            keep_i = 20000
        prior = p.get("prior_archive")
        if prior is not None and not isinstance(prior, dict):
            prior = None
        try:
            result = commands.compact_session(
                messages,
                requested_strategy or configured_strategy,
                keep_recent_tokens=keep_i,
                supersede_reads=bool(p.get("supersede_reads", True)),
                drop_useless=bool(p.get("drop_useless", True)),
                prior_archive=prior,
                standalone=explicit_local,
            )
        except commands.SessionCommandUnavailable as exc:
            return {"ok": False, "reason": str(exc), "hint": ""}
        out = {
            "ok": True,
            "summary": result.get("summary") or "",
            "short_summary": result.get("short_summary") or "",
            "kept_messages": result.get("kept_messages") or [],
        }
        for key in ("messages", "archive", "cut_index", "saved_tokens"):
            if key in result:
                out[key] = result[key]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_compact failed: {exc}", "hint": ""}


def _session_shake(params: dict, _emit: Callable) -> dict:
    """Keep transcript shaking local-only; Omp owns online history."""
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
                "reason": "session_shake is unavailable for online agent sessions",
                "owner": "omp",
                "hint": "online manual compaction uses agent_admin_turn with compact",
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
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {"ok": False, "reason": f"session_shake failed: {exc}", "hint": ""}


def _session_handoff(params: dict, _emit: Callable) -> dict:
    """Route a handoff through its named live Omp session (offline stays local)."""
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
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
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
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {"ok": False, "reason": f"session_btw failed: {exc}", "hint": ""}


SESSION_METHODS: dict[str, Callable] = {
    "session_compact": _session_compact,
    "session_shake": _session_shake,
    "session_handoff": _session_handoff,
    "session_btw": _session_btw,
}


# ──── skills_* ────


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


SKILLS_METHODS: dict[str, Callable] = {
    "skills_list": _skills_list,
    "skills_get": _skills_get,
    "skills_save": _skills_save,
    "skills_delete": _skills_delete,
}


# ──── triggers_* / packs_save / rule_create ────


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
    "packs_save": _packs_save,
    "rule_create": _rule_create,
}


# ──── ai_roles_* ────


def _ai_roles_get(params: dict, _emit: Callable) -> dict:
    """Return role/size assignments, the ``advanced_overrides`` switch and the retained overrides."""
    try:
        from services.agent import config as agent_config
        from services.agent.role_models import role_settings_view

        return {"ok": True, **role_settings_view(agent_config.load_config())}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ai_roles_get failed: {exc}", "hint": ""}


def _ai_roles_set(params: dict, _emit: Callable) -> dict:
    """Validate and atomically merge role, size, mode and ``advanced_overrides`` updates."""
    try:
        from services.agent.role_models import update_role_settings

        return {"ok": True, **update_role_settings(params or {})}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ai_roles_set failed: {exc}", "hint": ""}


ROLE_MODELS_METHODS: dict[str, Callable] = {
    "ai_roles_get": _ai_roles_get,
    "ai_roles_set": _ai_roles_set,
}


# ──── bench_* ────


def _repo_root() -> Path:
    env = os.environ.get("CHUDWARE_REPO") or os.environ.get("CHUDWARE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # services/agent/worker_rpc.py → repo root
    return Path(__file__).resolve().parents[2]


def _reports_dir() -> Path:
    return _repo_root() / "tools" / "flywheel" / "bench" / "gym" / "reports"


def _status_path() -> Path:
    return _reports_dir() / "bench_status.json"


def _leaderboard_path() -> Path:
    return _repo_root() / "benchmarks" / "leaderboard" / "leaderboard.json"


def _category_status_path(cat_id: str) -> Path:
    return _reports_dir() / "categories" / f"{cat_id}_status.json"


_state_lock = threading.RLock()
_active_proc: subprocess.Popen | None = None
_active_thread: threading.Thread | None = None
_bench_stopping = False


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_state_lock):
        thread = _active_thread
        proc = _active_proc
        running = proc is not None and proc.poll() is None
        bench = int(running or (thread is not None
                               and (thread.is_alive() or thread.ident is None)))
    with registry_lock(_login_lock):
        login = sum(bool(state.get("in_progress")) for state in _login_state.values())
    return {"by_kind": {"bench": bench, "subscription_login": login},
            "processes": [proc] if running else []}


def _bench_pid_alive(pid: Any, cmd: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        pass
    if Path("/proc").is_dir():
        try:
            argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        except (OSError, ValueError):
            return False
        expected = (
            "tools.flywheel.bench.gym.cli",
            "tools.flywheel.bench.categories",
            "tools.flywheel.bench.gym.leaderboard",
        )
        module = next(
            (cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-m"),
            None,
        ) if isinstance(cmd, list) else None
        if module not in expected or module.encode() not in argv:
            return False
    return True


def _terminate_bench_group(pid: int, proc: subprocess.Popen | None = None) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                break
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _shutdown_bench() -> None:
    """Stop the benchmark process group before the owning worker exits."""
    global _bench_stopping
    with _state_lock:
        _bench_stopping = True
        proc = _active_proc
        active = _active_thread is not None and _active_thread.is_alive()
    if proc is not None:
        _terminate_bench_group(proc.pid, proc)
    if active or proc is not None:
        status = _write_status({
            "running": False, "stage": "error", "finished": _utc_now(),
            "last_error": "bench worker shut down", "pid": None,
        })
        _mark_category_stopped(status)


def _spawn_bench(cmd: list[str], root: Path, env: dict, logf: Any) -> subprocess.Popen:
    global _active_proc
    with _state_lock:
        if _bench_stopping:
            raise RuntimeError("bench worker is shutting down")
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=env, stdout=logf,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        _active_proc = proc
        return proc

def _mark_category_stopped(status: dict) -> None:
    category = status.get("category")
    if not isinstance(category, str) or not category.isidentifier():
        return
    path = _category_status_path(category)
    if not path.is_file():
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict) and current.get("running"):
            current.update({
                "running": False, "stage": "error",
                "finished": status["finished"], "last_error": status["last_error"],
            })
            path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        _LOG.exception("Could not reconcile bench category status")


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
            if out.get("running") and (
                _active_thread is None or not _active_thread.is_alive()
            ):
                proc = _active_proc
                rc = proc.poll() if proc is not None else None
                if proc is None or rc is not None:
                    pid = out.get("pid")
                    if proc is None and _bench_pid_alive(pid, out.get("cmd")):
                        # The prior worker died while its detached process survived.
                        _terminate_bench_group(pid)
                    out.update({
                        "running": False,
                        "stage": "error",
                        "finished": out.get("finished") or _utc_now(),
                        "last_error": (
                            f"bench process exited with code {rc}"
                            if rc is not None else "bench worker exited before completion"
                        ),
                    })
                    if rc is not None:
                        out["exit_code"] = rc
                    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
                    _mark_category_stopped(out)
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
            proc = _spawn_bench(cmd, root, env, logf)
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
    if _bench_stopping:
        _write_status({
            "running": False, "stage": "error", "finished": _utc_now(),
            "last_error": "bench worker shut down", "exit_code": rc,
        })
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
    _write_status({"stage": "building_leaderboard", "exit_code": rc, "pid": None})
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
            lb = _spawn_bench(lb_cmd, root, env, logf)
            _write_status({"pid": lb.pid, "cmd": lb_cmd})
            lb_rc = lb.wait()
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
    if _bench_stopping:
        _write_status({
            "running": False, "stage": "error", "finished": _utc_now(),
            "last_error": "bench worker shut down", "exit_code": lb_rc,
        })
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
            proc = _spawn_bench(cmd, root, env, logf)
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
    if _bench_stopping:
        _write_status({
            "running": False, "stage": "error", "finished": _utc_now(),
            "last_error": "bench worker shut down", "exit_code": rc,
        })
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
        with _state_lock:
            if _bench_stopping:
                return {"ok": False, "reason": "bench worker is shutting down"}
        _read_status()
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

        # Refuse a second concurrent run.
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
            context = copy_context()
            t = threading.Thread(
                target=context.run,
                args=(_run_category_pipeline,
                      category, cycle,
                      modeling_mode if category == "modeling" else None),
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

        context = copy_context()
        t = threading.Thread(
            target=context.run,
            args=(_run_pipeline, mode, tiers, cycle),
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
        with _state_lock:
            if _active_thread is not None and _active_thread.ident is None:
                _active_thread = None
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

#: Bounded worker-boundary attachment shape. OmpSessionHost performs the
#: image/file conversion immediately before the RPC prompt.
_ATTACH_MAX = 32
_HISTORY_SEED_LOCK = threading.Lock()
_HISTORY_SEEDED_SESSIONS: set[str] = set()
_SESSION_DAEMON_GENERATIONS: dict[str, dict] = {}


def _daemon_generation() -> dict | None:
    """Identify the daemon actually serving this turn, not the worker process."""
    try:
        from services.mcp import core_bridge, daemon_bridge
        if not core_bridge.active_daemon_mode():
            return None
        hello = daemon_bridge.client().refresh_server_info()
        pid = hello.get("daemon_pid")
        start = hello.get("daemon_start_id")
        if type(pid) is not int or pid <= 0:
            return None
        return {"pid": pid, "start_id": str(start) if start else ""}
    except Exception:  # noqa: BLE001 — unknown is not evidence of a restart
        return None


def _generation_seed_path(sessions_dir: str, key: str) -> Path | None:
    if sessions_dir:
        directory = Path(sessions_dir) / ".daemon-seeds"
    elif in_cell():
        directory = actor_state_root() / "daemon-seeds"
    else:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _previous_daemon_generation(session_id: str, sessions_dir: str) -> dict | None:
    key = scoped_key(session_id)
    with _HISTORY_SEED_LOCK:
        known = _SESSION_DAEMON_GENERATIONS.get(key)
    if known:
        return dict(known)
    path = _generation_seed_path(sessions_dir, key)
    if path is not None:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(stored, dict) and type(stored.get("pid")) is int
                    and stored["pid"] > 0 and isinstance(stored.get("start_id", ""), str)):
                return stored
        except (OSError, ValueError, TypeError):
            pass
    return None


def _remember_daemon_generation(session_id: str, sessions_dir: str,
                                generation: dict | None) -> None:
    if generation is None:
        return
    key = scoped_key(session_id)
    with _HISTORY_SEED_LOCK:
        _SESSION_DAEMON_GENERATIONS[key] = dict(generation)
    path = _generation_seed_path(sessions_dir, key)
    if path is None:
        return
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".daemon-seed-", delete=False) as stream:
            temp = stream.name
            json.dump(generation, stream)
        os.replace(temp, path)
    except OSError:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass


def _daemon_generation_changed(previous: dict | None, current: dict | None) -> bool:
    if not previous or not current:
        return False
    if previous.get("pid") != current.get("pid"):
        return True
    before, after = previous.get("start_id"), current.get("start_id")
    return bool(before and after and before != after)


def _legacy_tool_loads_owner(
    params: Mapping[str, Any], session_id: str, workspace: str | None,
) -> dict[str, str] | None:
    """Prove a desktop GUI's persisted conversation-to-journal link."""
    if in_cell() or not session_id or len(session_id) > 256 or not all(
        char.isascii() and (char.isalnum() or char in "-_") for char in session_id
    ):
        return None
    directory = params.get("sessions_dir")
    journal = params.get("omp_session_file")
    if not all(isinstance(value, str) and value.strip() for value in (directory, journal, workspace)):
        return None
    try:
        root = Path(directory).expanduser().resolve(strict=True)
        file = root / f"{session_id}.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if file.is_symlink():
            return None
        fd = os.open(file, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024 or
                    (os.name == "posix" and
                     (info.st_uid != os.getuid() or info.st_mode & 0o022))):
                return None
            text = stream.read(128 * 1024 * 1024 + 1)
            if len(text) > 128 * 1024 * 1024:
                return None
            data = json.loads(text)
        if not isinstance(data, Mapping) or data.get("id") != session_id:
            return None
        link = data.get("omp", data.get("chudcode"))
        if not isinstance(link, Mapping):
            return None
        omp_id = link.get("session_id", link.get("sessionId"))
        omp_file = link.get("session_file", link.get("sessionFile"))
        project = data.get("project")
        if not all(isinstance(value, str) and value.strip() for value in (omp_id, omp_file, project)):
            return None
        actual_file = Path(journal).expanduser().resolve(strict=True)
        if Path(omp_file).expanduser().resolve(strict=True) != actual_file:
            return None
        actual_workspace = Path(workspace).expanduser().resolve(strict=True)
        if Path(project).expanduser().resolve(strict=True) != actual_workspace:
            return None
        return {"sessionId": omp_id, "sessionFile": str(actual_file),
                "workspace": str(actual_workspace)}
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None


def omp_agent_turn(params: dict, emit: Callable) -> dict:
    """Run one online turn through the authoritative persistent Omp host."""
    return _session_host_run("run_turn", params, emit)


def omp_compaction(params: dict, emit: Callable) -> dict:
    """Run one manual compaction through the authoritative persistent Omp host."""
    return _session_host_run("run_compaction", params, emit)


def _session_host_run(method: str, params: dict, emit: Callable) -> dict:
    try:
        result = getattr(get_default_omp_host(), method)(params, emit)
        if not isinstance(result, dict):
            return {
                "ok": False,
                "reason": "Agent session host returned a non-object response",
            }
        return _cell_refusal(result) or result
    except Exception as exc:  # noqa: BLE001 - never cross the worker boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {
            "ok": False,
            "reason": f"Agent session host failed: {type(exc).__name__}: {exc}",
        }



def _agent_handles_new(params: dict, _emit: Callable) -> dict:
    if not in_cell():
        return {"ok": False, "reason": "cell_refused", "code": "cell_refused"}
    if not isinstance(params, dict) or set(params) - {"session_id"}:
        return {"ok": False, "reason": "cell_refused", "code": "cell_refused"}
    session_id = params.get("session_id")
    try:
        if session_id is not None:
            require_handle(session_id, "session")
        else:
            session_id = mint_handle("session")
        return {"ok": True, "session_id": session_id,
                "run_id": mint_handle("run", session_id)}
    except (KeyError, TypeError):
        return {"ok": False, "reason": "not_found", "code": "not_found"}

def _control_credentials(params: Mapping) -> dict:
    if in_cell():
        if any(key in params for key in ("api_key", "byok_key")):
            raise ValueError("credential_isolation_unsupported")
        # The authenticated actor lane and opaque handle authorize controls.
        return {}
    return {"credential_params": params} if "byok_key" in params else {}


def omp_control(action: str, params: dict, *, private: bool = False) -> dict:
    """Dispatch a bounded control request to the process-global session host."""
    if not isinstance(params, Mapping):
        return {"ok": False, "reason": "Agent control params must be an object"}
    command = str(action or "").strip().lower()
    try:
        control_delegate("cancel" if command == "cancel" else command)
        if in_cell():
            if params.get("run_id"):
                require_handle(params["run_id"], "run")
            if params.get("session_id"):
                require_handle(params["session_id"], "session")
        if command == "cancel":
            credentials = _control_credentials(params)
            if private:
                credentials["allow_prestart"] = True
            return get_default_omp_host().cancel(params.get("run_id"), **credentials)
        if command == "steer":
            return get_default_omp_host().steer(
                params.get("run_id"),
                params.get("text"),
                params.get("attachments"),
                steer_id=params.get("steer_id"),
                **_control_credentials(params),
            )
        if command == "answer":
            return get_default_omp_host().answer(
                params.get("run_id"),
                params.get("batch_id"),
                params.get("answers"),
                declined=params.get("declined") is True,
                **_control_credentials(params),
            )
        if command == "status":
            return get_default_omp_host().status(
                run_id=params.get("run_id"), session_id=params.get("session_id"),
                **_control_credentials(params),
            )
        if command == "release":
            return get_default_omp_host().release_session(params.get("session_id"), **_control_credentials(params))
        if command == "apply_compaction_policy":
            return _omp_apply_compaction_policy(dict(params), lambda _event: None)
        return {
            "ok": False,
            "reason": f"unsupported agent control action: {command}",
        }
    except Exception as exc:  # noqa: BLE001 - control is an RPC boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        if isinstance(exc, KeyError) and str(exc).strip("'") == "not_found":
            return {"ok": False, "reason": "not_found", "code": "not_found"}
        if isinstance(exc, ValueError) and str(exc) in {
                "cell_refused", "forbidden_right", "credential_isolation_unsupported"}:
            return {"ok": False, "reason": str(exc), "code": str(exc)}
        return {
            "ok": False,
            "reason": f"Agent {command or 'control'} failed: "
            f"{type(exc).__name__}: {exc}",
        }


def omp_shutdown() -> None:
    """Stop every persistent Omp session before the worker exits."""
    get_default_omp_host().shutdown()
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




def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _bounded_identity(value: Any, limit: int) -> str | None:
    # Truncating an identity could route a model or credential to a different provider.
    return value if isinstance(value, str) and value and value == value.strip() and len(value) <= limit else None


def _has_unknown_params(params: Mapping[str, Any], allowed: frozenset[str]) -> bool:
    if in_cell():
        allowed = allowed | {"provider", "model"}
    return bool(set(params) - allowed)


def _finite_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _omp_transient_call(
    command: str,
    params: dict,
    emit: Callable | None = None,
    *,
    secret_input: bool = False,
) -> dict:
    try:
        result = get_default_omp_host().transient_call(command, params, emit=emit)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"Agent service {command} returned non-object"}
        clean = _redact_sensitive_response(result)
        api_key = params.get("apiKey")
        if secret_input and isinstance(api_key, str):
            clean = _redact_literal(clean, api_key)
        clean.setdefault("ok", True)
        return clean
    except Exception as exc:  # noqa: BLE001 - worker methods return clean refusals
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        detail = type(exc).__name__ if secret_input else f"{type(exc).__name__}: {exc}"
        return {"ok": False, "reason": f"Agent service {command} failed: {detail}"}


def _omp_auth_call(command: str, params: dict, _emit: Callable) -> dict:
    try:
        result = get_default_omp_host().auth_call(command, params)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"Agent service {command} returned non-object"}
        clean = _redact_sensitive_response(result)
        secret = params.get("api_key")
        if isinstance(secret, str):
            clean = _redact_literal(clean, secret)
        clean.setdefault("ok", True)
        return clean
    except Exception as exc:  # noqa: BLE001 - do not expose credential input
        return _cell_refusal(exc) or {
            "ok": False, "reason": f"Agent service {command} failed: {type(exc).__name__}"
        }


def _omp_memory_call(command: str, params: dict, _emit: Callable) -> dict:
    try:
        result = get_default_omp_host().memory_call(command, params)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"Agent service {command} returned non-object"}
        result.setdefault("ok", True)
        return _cell_refusal(result) or result
    except Exception as exc:  # noqa: BLE001 - Settings must get a clean refusal
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {
            "ok": False,
            "reason": f"Agent service {command} failed: {type(exc).__name__}: {exc}",
        }


def _auth_provider(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    provider_id = _bounded_identity(raw.get("provider") or raw.get("providerId") or raw.get("id"), 128)
    if provider_id is None or type(raw.get("configured")) is not bool:
        return None
    has_oauth = raw.get("oauth") is True
    has_key = raw.get("hasApiKey") is True
    authenticated = raw["configured"]
    source = _bounded_text(raw.get("effectiveSource"), 64)
    state = (
        "oauth_connected" if authenticated and source == "oauth"
        else "key_set" if authenticated and source == "api_key"
        else "configured" if authenticated
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
    origin = source
    if origin is not None:
        result["origin"] = origin
    credential_provider = _bounded_identity(raw.get("credentialProvider"), 128)
    if credential_provider is not None:
        result["credentialProvider"] = credential_provider
    return result


def _auth_providers(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("providers")
    if not isinstance(raw, list):
        provider = _auth_provider(result)
        return [provider] if provider is not None else []
    providers: list[dict[str, Any]] = []
    for item in raw:
        provider = _auth_provider(item)
        if provider is not None:
            providers.append(provider)
    return providers


def _omp_provider_list(params: dict, _emit: Callable) -> dict:
    if params:
        return {"ok": False, "reason": "omp_provider_list takes no params"}
    login = _omp_transient_call("get_login_providers", {})
    models = _omp_transient_call("get_available_models", {})
    setup = _omp_auth_call("setup_providers", {}, _emit)
    failed = next((item for item in (login, models, setup) if not item.get("ok")), None)
    if failed is not None:
        return failed
    for result, key in ((login, "providers"), (models, "models"), (setup, "providers")):
        if not isinstance(result.get(key), list):
            return {"ok": False, "reason": f"Agent service returned an invalid {key} list"}

    descriptors: dict[str, dict[str, Any]] = {}
    for item in setup["providers"]:
        if not isinstance(item, Mapping):
            continue
        provider_id = _bounded_identity(item.get("id"), 128)
        if provider_id is None:
            continue
        kinds = item.get("authKinds")
        descriptors[provider_id] = {
            "id": provider_id,
            "name": _bounded_text(item.get("name"), 256) or provider_id,
            "available": False,
            "authenticated": False,
            "authKinds": [kind for kind in ("api_key", "oauth")
                          if isinstance(kinds, list) and kind in kinds],
            "modelsAvailable": 0,
        }
    raw_login = login.get("providers")
    if isinstance(raw_login, list):
        for item in raw_login:
            if not isinstance(item, Mapping):
                continue
            provider_id = _bounded_identity(item.get("id"), 128)
            if provider_id is None:
                continue
            descriptor = descriptors.setdefault(provider_id, {
                "id": provider_id,
                "name": _bounded_text(item.get("name"), 256) or provider_id,
                "available": False, "authenticated": False,
                "authKinds": [], "modelsAvailable": 0,
            })
            if "oauth" not in descriptor["authKinds"]:
                descriptor["authKinds"].append("oauth")
    raw_models = models.get("models")
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, Mapping):
                continue
            provider_id = _bounded_identity(item.get("provider"), 128)
            if provider_id is None or _bounded_identity(item.get("id"), 256) is None:
                continue
            descriptor = descriptors.setdefault(
                provider_id,
                {
                    "id": provider_id,
                    "name": provider_id,
                    "available": False,
                    "authenticated": False,
                    "authKinds": [],
                    "modelsAvailable": 0,
                },
            )
            descriptor["modelsAvailable"] += 1
    # The extension polls its control lane every 100 ms. Asking it separately for
    # each provider made a normal 90-provider catalog take ~9 seconds and time out
    # behind concurrent GUI refreshes. Batch bounded requests while retaining
    # independent, fail-closed auth results for each provider.
    auth_by_id: dict[str, dict[str, Any]] = {}
    provider_ids = list(descriptors)
    for offset in range(0, len(provider_ids), 32):
        batch = provider_ids[offset:offset + 32]
        result = _omp_auth_call("auth_states", {"providers": batch}, _emit)
        entries = result.get("providers") if result.get("ok") else None
        if not isinstance(entries, list):
            continue
        requested = set(batch)
        seen: set[str] = set()
        for entry in entries:
            state = _auth_provider(entry)
            if state is None or state["id"] not in requested:
                continue
            provider_id = state["id"]
            if provider_id in seen:
                auth_by_id.pop(provider_id, None)
            else:
                auth_by_id[provider_id] = state
                seen.add(provider_id)
    from services.agent.config import load_config

    config = load_config() or {}
    agent_config = config.get("agent") if isinstance(config, dict) else None
    opt_ins = agent_config.get("keyless_providers", []) if isinstance(agent_config, dict) else []
    opted_in = {value for value in opt_ins if isinstance(value, str)} if isinstance(opt_ins, list) else set()
    unknown = 0
    for provider_id, descriptor in descriptors.items():
        state = auth_by_id.get(provider_id)
        if state is None:
            # Omit availability, rather than asserting signed out or inferring
            # access from catalog presence or a keyless opt-in without auth state.
            descriptor.pop("available", None)
            descriptor.pop("authenticated", None)
            descriptor["state"] = "unknown"
            unknown += 1
            continue
        descriptor["state"] = state["state"]
        descriptor["authenticated"] = state["authenticated"]
        descriptor["available"] = bool(
            descriptor["modelsAvailable"]
            and (state["authenticated"] or provider_id in opted_in)
        )
        if "origin" in state:
            descriptor["origin"] = state["origin"]
        if "credentialProvider" in state:
            descriptor["credentialProvider"] = state["credentialProvider"]
    providers = sorted(
        descriptors.values(), key=lambda item: (item["name"].lower(), item["id"])
    )
    result = {"ok": True, "providers": providers}
    if unknown:
        result["refreshError"] = f"Authentication status could not be refreshed for {unknown} provider(s)."
    return result


def _omp_auth_state(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"providerId"})):
        return {"ok": False, "reason": "omp_auth_state has unknown params"}
    provider_id = _bounded_identity(params.get("providerId"), 128)
    if provider_id is None:
        return {"ok": False, "reason": "providerId is required"}
    raw = _omp_auth_call("auth_state", {"provider": provider_id}, _emit)
    if not raw.get("ok"):
        return raw
    provider = next(
        (item for item in _auth_providers(raw) if item["id"] == provider_id),
        None,
    )
    if provider is None:
        return {"ok": False, "reason": "Agent service returned invalid auth state"}
    provider["ok"] = True
    return provider



def _omp_set_key(params: dict, emit: Callable) -> dict:
    provider_id = _bounded_identity(params.get("providerId"), 128)
    api_key = params.get("apiKey")
    if not provider_id or not isinstance(api_key, str) or not api_key:
        return {"ok": False, "reason": "providerId and apiKey are required"}
    raw = _omp_auth_call("set_api_key", {"provider": provider_id, "api_key": api_key}, emit)
    if not raw.get("ok"):
        return raw
    provider = _auth_provider(raw)
    if provider is None:
        return {"ok": False, "reason": "Agent service set_api_key returned no provider"}
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


def _omp_remove_key(params: dict, emit: Callable) -> dict:
    provider_id = _bounded_identity(params.get("providerId"), 128)
    if not provider_id:
        return {"ok": False, "reason": "providerId is required"}
    return _omp_auth_call("remove_api_key", {"provider": provider_id}, emit)


def _omp_logout_accounts(params: dict, emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"providerId"})):
        return {"ok": False, "reason": "omp_logout_accounts has unknown params"}
    provider_id = _bounded_identity(params.get("providerId"), 128)
    if not provider_id:
        return {"ok": False, "reason": "providerId is required"}
    raw = _omp_transient_call("get_logout_accounts", {"providerId": provider_id}, emit)
    if not raw.get("ok"):
        return raw
    accounts = raw.get("accounts")
    if not isinstance(accounts, list):
        return {"ok": False, "reason": "Agent service returned an invalid accounts list"}
    clean = []
    for item in accounts[:128]:
        if not isinstance(item, Mapping):
            continue
        credential_id = item.get("credentialId")
        if isinstance(credential_id, bool) or not isinstance(credential_id, int):
            continue
        clean.append({
            "credentialId": credential_id,
            "provider": provider_id,
            "type": _bounded_text(item.get("type"), 32) or "",
            "label": _bounded_text(item.get("label"), 512) or "",
            "detail": _bounded_text(item.get("detail"), 1024) or "",
            "active": item.get("active") is True,
        })
    return {"ok": True, "accounts": clean}


def _omp_logout(params: dict, emit: Callable) -> dict:
    provider_id = _bounded_identity(params.get("providerId"), 128)
    credential_id = params.get("credentialId")
    if not provider_id or isinstance(credential_id, bool) or not isinstance(credential_id, int):
        return {"ok": False, "reason": "providerId and credentialId are required"}
    return _omp_transient_call("logout", {"providerId": provider_id, "credentialId": credential_id}, emit)


def _omp_login(params: dict, emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"providerId"})):
        return {"ok": False, "reason": "omp_login has unknown params"}
    provider_id = _bounded_identity(params.get("providerId"), 128)
    if provider_id is None:
        return {"ok": False, "reason": "providerId is required"}
    login = _omp_transient_call("login", {"providerId": provider_id}, emit=emit)
    if not login.get("ok"):
        return login
    state = _omp_auth_state({"providerId": provider_id}, emit)
    return state


def _sanitize_model(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    model_id = _bounded_identity(item.get("id"), 256)
    provider = _bounded_identity(item.get("provider"), 128)
    if model_id is None or provider is None:
        return None
    # Omp's catalog names the modality list `inputModalities`; `input` is the older spelling.
    inputs = item.get("inputModalities")
    if not isinstance(inputs, list):
        inputs = item.get("input")
    clean_inputs = (
        [text for value in inputs[:8] if (text := _bounded_text(value, 32)) is not None]
        if isinstance(inputs, list)
        else []
    )
    # The per-model effort ladder (`thinking.efforts`) is the only source that knows a new
    # release's rungs; dropping it left the composer on a hardcoded low/medium/high fallback.
    thinking = item.get("thinking")
    raw_efforts = thinking.get("efforts") if isinstance(thinking, Mapping) else None
    efforts: list[str] = []
    if isinstance(raw_efforts, list):
        for value in raw_efforts[:8]:
            text = _bounded_text(value, 32)
            if text is not None and text not in efforts:
                efforts.append(text)
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
    result = {
        "id": model_id,
        "name": _bounded_text(item.get("name"), 256) or model_id,
        "provider": provider,
        "contextWindow": min(
            1_000_000_000, max(0, int(_finite_number(item.get("contextWindow"))))
        ),
        "maxTokens": min(
            1_000_000_000, max(0, int(_finite_number(item.get("maxTokens"))))
        ),
        "cost": clean_cost,
    }
    if isinstance(item.get("reasoning"), bool):
        result["reasoning"] = item["reasoning"]
    if isinstance(raw_efforts, list):
        result["reasoningEfforts"] = efforts
        result["thinking_levels"] = efforts
    if isinstance(inputs, list):
        result["input"] = clean_inputs
        result["vision"] = "image" in clean_inputs
    for key in ("kind", "toolCall"):
        if isinstance(item.get(key), (str, bool)):
            result[key] = item[key]
    return result


def _omp_models_list(params: dict, _emit: Callable) -> dict:
    if set(params) - {"providerId"}:
        return {"ok": False, "reason": "omp_models_list has unknown params"}
    if in_cell():
        # Cells have no provider credential route, so no model is usable there.
        return {"ok": True, "models": [], "refreshing": False}
    provider_id = _bounded_identity(params.get("providerId"), 128)
    if "providerId" in params and provider_id is None:
        return {"ok": False, "reason": "invalid providerId"}
    raw = _omp_transient_call("get_available_models", {})
    if not raw.get("ok"):
        return raw
    models: list[dict[str, Any]] = []
    raw_models = raw.get("models")
    if not isinstance(raw_models, list):
        return {"ok": False, "reason": "Agent service returned an invalid models list"}
    if isinstance(raw_models, list):
        for item in raw_models:
            model = _sanitize_model(item)
            if model is not None and (
                provider_id is None or model["provider"] == provider_id
            ):
                models.append(model)
    models.sort(key=lambda item: (item["provider"], item["name"].lower(), item["id"]))
    return {"ok": True, "models": models, "refreshing": False}


def _omp_session_call(
    command: str, params: dict, payload: dict | None = None
) -> dict:
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    try:
        result = get_default_omp_host().session_call(session_id, command, payload)
        return _cell_refusal(result) or result
    except Exception as exc:  # noqa: BLE001 - worker boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {
            "ok": False,
            "reason": f"Agent service {command} failed: {type(exc).__name__}: {exc}",
        }


def _omp_export_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "output_path"})):
        return {"ok": False, "reason": "omp_export_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    output_path = params.get("output_path")
    if output_path is not None:
        output_path = _bounded_text(output_path, 4_096)
        if output_path is None:
            return {"ok": False, "reason": "output_path must be a non-empty string"}
    try:
        raw = get_default_omp_host().export_session(session_id, output_path)
        if not isinstance(raw, Mapping):
            return {
                "ok": False,
                "reason": "Agent service export_session returned non-object",
            }
        if not raw.get("ok"):
            refusal = _cell_refusal(raw)
            if refusal is not None:
                return refusal
            return {
                "ok": False,
                "reason": _bounded_text(raw.get("reason"), 4_096)
                or "Agent service export_session failed",
            }
        path = _bounded_text(raw.get("path"), 4_096)
        if path is None:
            return {
                "ok": False,
                "reason": "Agent service export_session returned no path",
            }
        return {"ok": True, "path": path}
    except Exception as exc:  # noqa: BLE001 - worker boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {
            "ok": False,
            "reason": f"Agent service export_session failed: {type(exc).__name__}",
        }


def _omp_branch_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "entry_id"})):
        return {"ok": False, "reason": "omp_branch_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_not_live"}
    entry_id = _bounded_text(params.get("entry_id"), 256)
    if entry_id is None:
        return {"ok": False, "reason": "entry_id is required"}
    try:
        raw = get_default_omp_host().branch_session(session_id, entry_id)
        if not isinstance(raw, Mapping):
            return {
                "ok": False,
                "reason": "Agent service branch_session returned non-object",
            }
        if not raw.get("ok"):
            refusal = _cell_refusal(raw)
            if refusal is not None:
                return refusal
            return {
                "ok": False,
                "reason": _bounded_text(raw.get("reason"), 4_096)
                or "Agent service branch_session failed",
            }
        if in_cell():
            return {"ok": True, "omp_session_id": session_id}
        child_id = _bounded_text(raw.get("omp_session_id"), 256)
        child_file = _bounded_text(raw.get("omp_session_file"), 4_096)
        if child_id is None or child_file is None:
            return {
                "ok": False,
                "reason": "Agent service branch_session returned no child metadata",
            }
        return {
            "ok": True,
            "omp_session_id": child_id,
            "omp_session_file": child_file,
        }
    except Exception as exc:  # noqa: BLE001 - worker boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        return {
            "ok": False,
            "reason": f"Agent service branch_session failed: {type(exc).__name__}",
        }


def _omp_close_session(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "omp_close_session has unknown params"}
    session_id = _bounded_text(params.get("session_id"), 256)
    if session_id is None:
        return {"ok": False, "reason": "session_id is required"}
    try:
        if in_cell():
            require_handle(session_id, "session")
        result = get_default_omp_host().close_session(session_id, **_control_credentials(params))
        if isinstance(result, dict) and result.get("ok") is False:
            return _cell_refusal(result) or result
        with _HISTORY_SEED_LOCK:
            _HISTORY_SEEDED_SESSIONS.discard(scoped_key(session_id))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - worker boundary
        refusal = _cell_refusal(exc)
        if refusal is not None:
            return refusal
        if isinstance(exc, KeyError) and exc.args == ("not_found",):
            return {"ok": False, "reason": "not_found", "code": "not_found"}
        if isinstance(exc, ValueError) and str(exc) in {
                "cell_refused", "credential_isolation_unsupported"}:
            return {"ok": False, "reason": str(exc), "code": str(exc)}
        return {
            "ok": False,
            "reason": f"Agent service close_session failed: {type(exc).__name__}",
        }


def _omp_session_stats(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "omp_session_stats has unknown params"}
    raw = _omp_session_call("get_session_stats", params)
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




def _omp_apply_compaction_policy(params: dict, _emit: Callable) -> dict:
    allowed = {"strategy", "compaction"}
    if set(params) - allowed:
        return {"ok": False, "reason": "Unknown compaction policy parameters"}
    raw_strategy = params.get("strategy")
    nested = params.get("compaction", {})
    if not isinstance(nested, Mapping) or set(nested) - {
        "supersede_reads", "drop_useless", "keep_recent_tokens"
    }:
        return {"ok": False, "reason": "Invalid compaction settings"}
    for key in ("supersede_reads", "drop_useless"):
        if key in nested and not isinstance(nested[key], bool):
            return {"ok": False, "reason": f"{key} must be a boolean"}
    keep = nested.get("keep_recent_tokens")
    if "keep_recent_tokens" in nested and (
        isinstance(keep, bool) or not isinstance(keep, int) or not 0 <= keep <= 2**53 - 1
    ):
        return {"ok": False, "reason": "keep_recent_tokens must be a nonnegative safe integer"}
    if "strategy" in params and (
        not isinstance(raw_strategy, str) or raw_strategy not in {
            "auto", "off", "snapcompact", "shake", "summary"
        }
    ):
        return {"ok": False, "reason": "Invalid compaction strategy"}
    if in_cell() and raw_strategy is None:
        return {"ok": False, "reason": "strategy is required in a cell"}
    try:
        from services.agent.config import online_compaction_settings

        if in_cell():
            cfg = {"agent": {"compaction_strategy": raw_strategy, "compaction": dict(nested)}}
            strategy, settings = online_compaction_settings(cfg)
        else:
            strategy, settings = online_compaction_settings()
            if raw_strategy is not None or nested:
                cfg = {"agent": {"compaction_strategy": raw_strategy or strategy, "compaction": {
                    "supersede_reads": settings["supersedeReads"],
                    "drop_useless": settings["dropUseless"],
                    "keep_recent_tokens": settings["keepRecentTokens"],
                    **dict(nested),
                }}}
                strategy, settings = online_compaction_settings(cfg)
        result = get_default_omp_host().apply_compaction_policy(settings)
        if not isinstance(result, dict):
            return {"ok": False, "reason": "OMP returned an invalid policy acknowledgement"}
        return {**result, "strategy": strategy}
    except Exception as exc:  # noqa: BLE001 - settings boundary
        return {"ok": False, "reason": f"Compaction policy application failed: {type(exc).__name__}: {exc}"}


def _omp_set_auto_compaction(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "enabled"})):
        return {
            "ok": False,
            "reason": "omp_set_auto_compaction has unknown params",
        }
    if not isinstance(params.get("enabled"), bool):
        return {"ok": False, "reason": "enabled must be a boolean"}
    result = _omp_session_call(
        "set_auto_compaction", params, {"enabled": params["enabled"]}
    )
    return result


def _omp_set_auto_retry(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id", "enabled"})):
        return {"ok": False, "reason": "omp_set_auto_retry has unknown params"}
    if not isinstance(params.get("enabled"), bool):
        return {"ok": False, "reason": "enabled must be a boolean"}
    result = _omp_session_call(
        "set_auto_retry", params, {"enabled": params["enabled"]}
    )
    return result


def _omp_tool_state(params: dict, _emit: Callable) -> dict:
    if _has_unknown_params(params, frozenset({"session_id"})):
        return {"ok": False, "reason": "omp_tool_state has unknown params"}
    raw = _omp_session_call("tool_state", params)
    if not raw.get("ok"):
        return raw
    tools = raw.get("tools")
    if not isinstance(tools, list):
        return {"ok": False, "reason": "OMP returned an invalid tool list"}
    rows = []
    for item in tools[:1024]:
        if not isinstance(item, Mapping):
            continue
        name = _bounded_text(item.get("name"), 128)
        if name is None:
            continue
        rows.append({"name": name,
                     "description": _bounded_text(item.get("description"), 512) or "",
                     "builtin": item.get("builtin") is True,
                     "available": item.get("available") is True})
    return {"ok": True, "tools": rows}

def _agent_cancel(params: dict, _emit: Callable) -> dict:
    return omp_control("cancel", params)


def _agent_steer(params: dict, _emit: Callable) -> dict:
    return omp_control("steer", params)


def _memory_remember(params: dict, _emit: Callable) -> dict:
    if set(params) - {"root", "content", "context", "source", "importance"}:
        return {"ok": False, "code": "unsupported", "reason": "Unsupported memory save parameters"}
    return _omp_memory_call("memory_save", params, _emit)


def _memory_recall(params: dict, _emit: Callable) -> dict:
    if set(params) - {"root", "query", "limit"}:
        return {"ok": False, "code": "unsupported", "reason": "Unsupported memory search parameters"}
    return _omp_memory_call("memory_search", params, _emit)


def _memory_stats(params: dict, _emit: Callable) -> dict:
    if set(params) - {"root"}:
        return {"ok": False, "code": "unsupported", "reason": "Unsupported memory status parameters"}
    return _omp_memory_call("memory_status", params, _emit)



def _claim_history_seed(
    session_id: str, raw_history: Any, *, resumed: bool
) -> tuple[str, bool]:
    """Track first session use without reconstructing a runtime journal from GUI text."""
    with _HISTORY_SEED_LOCK:
        key = scoped_key(session_id)
        if key in _HISTORY_SEEDED_SESSIONS:
            return "", False
        if raw_history and not resumed:
            try:
                get_default_omp_host().session_launch_params(session_id)
            except KeyError as exc:
                raise ValueError(
                    "unsupported_history: original runtime session journal is required"
                ) from exc
        _HISTORY_SEEDED_SESSIONS.add(key)
    return "", True


_CONTEXT_REFS_MAX = 24
_CONTEXT_REF_MAX_CHARS = 700
_CONTEXT_BLOCK_MAX_CHARS = 9000
_CONTEXT_PARAMS_MAX = 8


def _editor_engine_call(method: str, params: dict) -> Any:
    """The engine seam the picked-context resolver reads through (tests replace it)."""
    from services.mcp import core_bridge  # noqa: PLC0415 - loaded only when refs need it

    return core_bridge.call(method, params)


def _ctx_num(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    rounded = round(number, 3)
    return f"{0.0 if rounded == 0 else rounded:g}"


def _ctx_vec(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    parts = [_ctx_num(item) for item in value]
    return None if None in parts else "[" + ", ".join(parts) + "]"  # type: ignore[arg-type]


def _ctx_str(value: Any, limit: int = 120) -> str:
    text = _bounded_text(value, 4 * limit) or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ctx_geometry(selector: Mapping[str, Any]) -> str:
    """One line of engine-computed geometry for a face/edge selector (contracts/selectors.md)."""
    kind = str(selector.get("kind") or "")
    surf = _ctx_str(selector.get("surf"), 24) or "other"
    centroid = _ctx_vec(selector.get("centroid"))
    axis = _ctx_vec(selector.get("axis"))
    normal = _ctx_vec(selector.get("normal"))
    measure = _ctx_num(selector.get("area_or_len"))
    radius = _ctx_num(selector.get("radius"))
    side = _ctx_str(selector.get("side"), 16)
    bits: list[str] = []
    if kind == "face":
        bits.append(f"{surf} face")
        if surf == "plane" and (normal or axis):
            bits.append(f"normal {normal or axis}" + (f" ({side})" if side else ""))
        else:
            if axis:
                bits.append(f"axis {axis}")
            if normal:
                bits.append(f"outward {normal}" + (f" ({side})" if side else ""))
        if radius:
            bits.append(f"radius {radius} mm")
        concavity = _ctx_str(selector.get("concavity"), 8)
        if concavity:
            bits.append(concavity)
        if measure:
            bits.append(f"area {measure} mm²")
        if centroid:
            bits.append(f"centroid {centroid}")
    elif kind == "edge":
        bits.append(f"{surf} edge")
        length = selector.get("area_or_len")
        if measure:
            bits.append(f"length {measure} mm")
        point = selector.get("centroid")
        if (surf == "line" and axis and centroid and isinstance(length, (int, float))
                and isinstance(point, (list, tuple)) and len(point) == 3):
            direction = selector.get("axis")
            half = float(length) / 2.0
            try:
                ends = [
                    [float(point[i]) + sign * half * float(direction[i]) for i in range(3)]
                    for sign in (-1.0, 1.0)
                ]
            except (TypeError, ValueError, IndexError):
                ends = []
            if ends:
                bits.append(f"from {_ctx_vec(ends[0])} to {_ctx_vec(ends[1])}")
            bits.append(f"direction {axis}")
        else:
            full_circle = (
                surf == "circle" and radius and isinstance(length, (int, float))
                and abs(float(length) - 2.0 * math.pi * float(selector["radius"]))
                <= 1e-3 * max(1.0, float(length))
            )
            if radius:
                bits.append(f"radius {radius} mm" + (" (full circle)" if full_circle else ""))
            if axis:
                bits.append(f"axis {axis}")
            if centroid:
                bits.append(f"{'center' if full_circle else 'centroid'} {centroid}")
    else:
        if centroid:
            bits.append(f"at {centroid}")
    return ", ".join(bits)


def _ctx_param_text(value: Any) -> str | None:
    if isinstance(value, Mapping) and "value" in value:
        shown = _ctx_num(value.get("value"))
        expr = _ctx_str(value.get("expr"), 40)
        if shown is None:
            return None
        return f"{shown} (= {expr})" if expr else shown
    if isinstance(value, bool):
        return "true" if value else "false"
    number = _ctx_num(value)
    if number is not None:
        return number
    if isinstance(value, str) and value.strip():
        return _ctx_str(value, 40)
    return None


def _ctx_ref_slot_text(value: Any) -> str:
    if isinstance(value, list):
        owners = {
            str(item.get("owner_feature") or item.get("sketch") or item.get("feature") or "")
            for item in value if isinstance(item, Mapping)
        } - {""}
        return f"{len(value)} ref(s)" + (f" on {', '.join(sorted(owners)[:3])}" if owners else "")
    if isinstance(value, str):
        return _ctx_str(value, 40)
    if isinstance(value, Mapping):
        try:
            return _ctx_str(json.dumps(value, separators=(",", ":"), sort_keys=True), 60)
        except (TypeError, ValueError):
            return "selector"
    return "set"


def _ctx_feature_details(feature: Mapping[str, Any]) -> str:
    ftype = _ctx_str(feature.get("type"), 32) or "feature"
    params = feature.get("params") if isinstance(feature.get("params"), Mapping) else {}
    refs = feature.get("refs") if isinstance(feature.get("refs"), Mapping) else {}
    bits: list[str] = []
    if ftype in {"sketch", "sketch3d"}:
        plane = refs.get("plane", params.get("plane"))
        if plane is not None:
            bits.append(f"on {_ctx_ref_slot_text(plane)}")
        entities = params.get("entities") if isinstance(params.get("entities"), list) else []
        by_type: dict[str, int] = {}
        for entity in entities:
            if isinstance(entity, Mapping):
                key = _ctx_str(entity.get("type"), 16) or "entity"
                by_type[key] = by_type.get(key, 0) + 1
        counts = ", ".join(f"{n} {t}" for t, n in sorted(by_type.items()))
        bits.append(f"{len(entities)} entities" + (f" ({counts})" if counts else ""))
        for key in ("constraints", "dimensions"):
            if isinstance(params.get(key), list) and params[key]:
                bits.append(f"{len(params[key])} {key}")
    else:
        shown = 0
        for key, value in params.items():
            if key in {"entities", "constraints", "dimensions"}:
                continue
            text = _ctx_param_text(value)
            if text is None:
                continue
            bits.append(f"{_ctx_str(key, 24)}={text}")
            shown += 1
            if shown >= _CONTEXT_PARAMS_MAX:
                break
        for slot, value in list(refs.items())[:4]:
            bits.append(f"refs.{_ctx_str(slot, 24)}: {_ctx_ref_slot_text(value)}")
    if feature.get("suppressed") is True:
        bits.append("SUPPRESSED")
    return "; ".join(bits)


class _ContextRefResolver:
    """Bounded engine reads for one prompt: one doc_tree per doc, one doc_query per kind."""

    def __init__(self, call: Callable[[str, dict], Any]):
        self._call = call
        self._trees: dict[str, Mapping[str, Any] | None] = {}
        self._matches: dict[tuple[str, str], list[Mapping[str, Any]] | None] = {}

    def tree(self, doc_id: str) -> Mapping[str, Any] | None:
        if doc_id not in self._trees:
            try:
                reply = self._call("doc_tree", {"doc_id": doc_id})
            except Exception as exc:  # noqa: BLE001 - context stays best-effort, never fails a turn
                _LOG.info("picked-context doc_tree(%s) failed: %s", doc_id, exc)
                reply = None
            tree = reply.get("tree") if isinstance(reply, Mapping) else None
            self._trees[doc_id] = tree if isinstance(tree, Mapping) else None
        return self._trees[doc_id]

    def is_assembly(self, doc_id: str) -> bool:
        tree = self.tree(doc_id) or {}
        return str(tree.get("schema") or "").startswith("aicad.asm") or bool(
            tree.get("components") and not tree.get("features"))

    def feature(self, doc_id: str, fid: str) -> Mapping[str, Any] | None:
        for feature in (self.tree(doc_id) or {}).get("features") or ():
            if isinstance(feature, Mapping) and feature.get("id") == fid:
                return feature
        return None

    def component(self, doc_id: str, cid: str) -> Mapping[str, Any] | None:
        for component in (self.tree(doc_id) or {}).get("components") or ():
            if isinstance(component, Mapping) and component.get("id") == cid:
                return component
        return None

    def live_selector(self, doc_id: str, kind: str, sid: str) -> tuple[str, Mapping | None]:
        """("live", match) | ("gone", None) | ("unverified", None) for a part doc sid."""
        key = (doc_id, kind)
        if key not in self._matches:
            try:
                reply = self._call("doc_query", {"doc_id": doc_id, "query": {"of": kind}})
                found = reply.get("matches") if isinstance(reply, Mapping) else None
                self._matches[key] = [m for m in found if isinstance(m, Mapping)] \
                    if isinstance(found, list) else None
            except Exception as exc:  # noqa: BLE001 - verification is best-effort
                _LOG.info("picked-context doc_query(%s,%s) failed: %s", doc_id, kind, exc)
                self._matches[key] = None
        matches = self._matches[key]
        if matches is None:
            return "unverified", None
        want = sid.lower()
        for match in matches:
            if str(match.get("sid") or "").lower() == want:
                return "live", match
        return "gone", None

    def resolve_pick(self, doc_id: str, ref: Mapping[str, Any]) -> tuple[Mapping | None, str]:
        """Engine selector for a chip sent before the GUI's own resolve landed: (reply, error)."""
        body_id, topo_id = ref.get("body_id"), ref.get("topo_id")
        kind = str(ref.get("kind") or "")
        if not isinstance(body_id, int) or body_id < 1:
            return None, ""
        if kind in {"face", "edge", "vertex"} and not (isinstance(topo_id, int) and topo_id >= 1):
            return None, ""
        params: dict[str, Any] = {"doc_id": doc_id, "body_id": body_id, "kind": kind,
                                  "topo_id": topo_id if isinstance(topo_id, int) else 0}
        if isinstance(ref.get("revision"), int) and ref["revision"] >= 0:
            params["revision"] = ref["revision"]
        try:
            reply = self._call("doc_resolve_pick", params)
        except Exception as exc:  # noqa: BLE001 - a stale pick is reported, not raised
            return None, str(exc)[:160]
        if isinstance(reply, Mapping) and isinstance(reply.get("selector"), Mapping):
            return reply, ""
        message = reply.get("message") if isinstance(reply, Mapping) else None
        return None, _ctx_str(message, 160) or "the pick no longer resolves"


def _ctx_feature_label(resolver: _ContextRefResolver, doc_id: str, fid: str,
                       fallback_name: str = "") -> str:
    feature = resolver.feature(doc_id, fid) if doc_id else None
    name = _ctx_str(feature.get("name"), 60) if feature else fallback_name
    ftype = _ctx_str(feature.get("type"), 32) if feature else ""
    text = fid
    if name and name != fid:
        text += f' "{name}"'
    return text + (f" ({ftype})" if ftype else "")


def _ctx_render_ref(index: int, ref: Mapping[str, Any], resolver: _ContextRefResolver) -> str:
    kind = _ctx_str(ref.get("kind"), 16).lower() or "ref"
    # A chip always names its document; a payload without one is described as document-less.
    doc_id = _ctx_str(ref.get("doc_id"), 128)
    label = _ctx_str(ref.get("label"), 80)
    ref_id = _ctx_str(ref.get("id"), 128)
    selector = ref.get("selector") if isinstance(ref.get("selector"), Mapping) else None
    owner = _ctx_str(ref.get("owner_feature") or (selector or {}).get("owner_feature"), 64)
    component = _ctx_str(ref.get("component"), 64)
    resolve_error = ""
    if selector is None and doc_id and kind in {"face", "edge", "vertex", "body"}:
        reply, resolve_error = resolver.resolve_pick(doc_id, ref)
        if reply is not None:
            selector = reply["selector"]
            owner = owner or _ctx_str(selector.get("owner_feature") or reply.get("owner_feature"), 64)
            component = component or _ctx_str(reply.get("component"), 64)
    sid = _ctx_str(ref.get("sid") or (selector or {}).get("sid"), 16)
    topology = kind in {"face", "edge", "vertex"}
    handle = sid or ("(unresolved pick)" if topology else ref_id) or "(no id)"
    doc_name = _ctx_str(ref.get("doc_name"), 60)
    where = f"doc {doc_id}" + (f" ({doc_name})" if doc_name else "") if doc_id else "no document"
    head = f"{index}. {kind.upper()} {handle}" + (f' "{label}"' if label else "") + f" — {where}"
    lines = [head]
    component_name = _ctx_str(ref.get("component_name"), 60)

    if topology:
        if component:
            comp = resolver.component(doc_id, component) if doc_id else None
            source = comp.get("source") if isinstance(comp, Mapping) else None
            path = _ctx_str(source.get("path"), 160) if isinstance(source, Mapping) else ""
            name = component_name or (_ctx_str(comp.get("name"), 60) if comp else "")
            lines.append(
                f"   in component {component}" + (f' "{name}"' if name else "")
                + (f" (part file {path})" if path else "")
                + " — geometry below is in the component's own part coordinates")
            if owner:
                lines.append(f"   owner: feature {owner} of that component's part")
        elif owner:
            lines.append("   owner: feature " + _ctx_feature_label(
                resolver, doc_id, owner, _ctx_str(ref.get("owner_name"), 60)))
        status = "as picked (not re-verified)"
        if sid and doc_id and kind != "vertex" and not component \
                and not resolver.is_assembly(doc_id):
            state, live = resolver.live_selector(doc_id, kind, sid)
            if state == "live" and live is not None:
                selector, status = live, "verified against the live document"
            elif state == "gone":
                status = (f"NOT in the current geometry — the part changed since the pick; "
                          f"values are as picked. describe_part(doc_id=\"{doc_id}\") lists "
                          "the current sids")
        if selector:
            geometry = _ctx_geometry(selector)
            if geometry:
                lines.append(f"   geometry: {geometry}")
        else:
            status = ("unresolved pick" + (f" ({resolve_error})" if resolve_error else "")
                      + " — ask the user to pick it again if it matters")
        lines.append(f"   status: {status}")
    elif kind in {"feature", "sketch"}:
        feature = resolver.feature(doc_id, ref_id) if doc_id and ref_id else None
        if feature is None:
            lines.append("   not found in the document's feature tree (deleted or doc closed)")
        else:
            details = _ctx_feature_details(feature)
            ftype = _ctx_str(feature.get("type"), 32)
            lines.append(f"   {ftype}: {details}" if details else f"   {ftype}")
    elif kind == "component":
        comp = resolver.component(doc_id, ref_id) if doc_id and ref_id else None
        if comp is None:
            feature = resolver.feature(doc_id, ref_id) if doc_id and ref_id else None
            if feature is not None:
                lines.append(f"   imported body feature: {_ctx_feature_details(feature)}")
            else:
                lines.append("   not found in the assembly (removed or doc closed)")
        else:
            bits: list[str] = []
            source = comp.get("source") if isinstance(comp.get("source"), Mapping) else {}
            if source.get("path"):
                bits.append(f"part file {_ctx_str(source.get('path'), 160)}")
            transform = comp.get("transform")
            if isinstance(transform, list) and len(transform) == 16:
                at = _ctx_vec([transform[3], transform[7], transform[11]])
                if at:
                    bits.append(f"placed at {at}")
            for flag in ("fixed", "suppressed", "hidden"):
                if comp.get(flag) is True:
                    bits.append(flag)
            if bits:
                lines.append("   " + "; ".join(bits))
    elif kind == "body":
        body_id = ref.get("body_id")
        bits = [f"body ordinal {body_id}"] if isinstance(body_id, int) else []
        if owner:
            bits.append("created by feature " + _ctx_feature_label(resolver, doc_id, owner))
        if component:
            bits.append(f"component {component}" + (f' "{component_name}"' if component_name else ""))
        if bits:
            lines.append("   " + "; ".join(bits))
    text = "\n".join(lines)
    return text if len(text) <= _CONTEXT_REF_MAX_CHARS else text[: _CONTEXT_REF_MAX_CHARS - 1] + "…"


_CONTEXT_TARGETING = (
    "HOW TO TARGET PICKED ITEMS: to LOOK at one, render_part(doc_id=…, target=\"<sid or "
    "feature id>\") gives a single framed close-up of it; render_part(doc_id=…, "
    "highlight=\"<sid>\") shows it in the context of the whole part; inspect_feature(target=…) "
    "is a 4-panel sheet — use it only if one close-up is ambiguous. A face/edge sid (F…/E…) "
    "goes wherever a face/edge ref is accepted (e.g. add_features refs.faces:[\"<sid>\"]); "
    "feature/sketch ids (f4) go to feature edit ops; component ids (k2) name assembly "
    "components for asm_* tools and mates. Always pass the doc_id listed on the item. "
    "Sids change when geometry moves: if one no longer resolves, "
    "describe_part(doc_id) lists the current roster."
)


def _picked_context_block(raw_refs: Any, call: Callable[[str, dict], Any]) -> str:
    """Render the composer's picked context refs, resolved against the engine."""
    refs = [ref for ref in raw_refs if isinstance(ref, Mapping)] \
        if isinstance(raw_refs, list) else []
    if not refs:
        return ""
    resolver = _ContextRefResolver(call)
    shown = refs[:_CONTEXT_REFS_MAX]
    lines = [
        f"PICKED CONTEXT — the user attached {len(refs)} model item(s) to this message with the "
        "context picker; \"this\", \"these\", \"that face\" and similar words refer to them."
    ]
    used = len(lines[0])
    for index, ref in enumerate(shown, start=1):
        rendered = _ctx_render_ref(index, ref, resolver)
        if used + len(rendered) + 1 > _CONTEXT_BLOCK_MAX_CHARS:
            lines.append(f"(+{len(refs) - index + 1} more picked item(s) not shown)")
            break
        lines.append(rendered)
        used += len(rendered) + 1
    else:
        if len(refs) > len(shown):
            lines.append(f"(+{len(refs) - len(shown)} more picked item(s) not shown)")
    lines.append(_CONTEXT_TARGETING)
    return "\n".join(lines)


def _picked_context_text(raw_params: Mapping[str, Any], *,
                         engine_call: Callable[[str, dict], Any] | None = None) -> str:
    """Render the chips the user attached with the composer's context picker.

    Only explicit user action reaches the model here: each ``context_refs`` chip is
    resolved against the engine (feature params, sketch contents, live face/edge
    geometry) so the model gets a usable description, not just an id. Nothing about
    which documents the GUI has open or what is selected is sent ambiently. Bounded so
    a thousand-face box-select cannot flood the prompt. Empty string when nothing was
    picked.
    """
    return _picked_context_block(raw_params.get("context_refs"), engine_call or _editor_engine_call)


_RESUMED_CONVERSATION_NOTE = (
    "RESUMED CONVERSATION\n"
    "- The engine restarted since this conversation's previous turn. A doc_id quoted "
    "earlier may be invalid or may now name a different part (a restarted engine "
    're-issues ids). Reopen a part with import_part(path="<its .step+>", resume=True) '
    "and use the doc_id it returns, or call list_parts() when you need to know what is open."
)


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


def _session_launch_params(
    raw_params: Mapping[str, Any],
    *,
    run_id: str,
    session_id: str,
    options: Mapping[str, Any],
    system_prompt: Any,
    settings: dict[str, Any],
    project_root: str | None,
    byok_key: str | None,
    provider_name: str | None,
) -> dict[str, Any]:
    """Host params naming the conversation's Omp session process.

    A turn and a manual compaction share them, so whichever of the two starts or
    resumes the session gives the process the identity the next turn expects.
    """
    params: dict[str, Any] = {
        "run_id": run_id,
        "session_id": session_id,
        "mode": options["mode"],
        "plan_only": options.get("plan_only"),
        "system_prompt": system_prompt,
        "settings": settings,
    }
    if byok_key is not None:
        params.update(byok_key=byok_key, byok_provider=provider_name, byok_tenant="desktop")
    if project_root:
        params["workspace_dir"] = project_root
    from services.agent.config import memory_backend

    params["memory_backend"] = memory_backend()
    for key, source in (
        ("omp_session_dir", ("omp_session_dir", "sessions_dir")),
        ("omp_session_file", ("omp_session_file",)),
    ):
        value = next((
            raw_params[name].strip() for name in source
            if isinstance(raw_params.get(name), str) and raw_params[name].strip()
        ), None)
        if value:
            params[key] = value
    return params


def _rpc_event_emitter(event_context: Mapping[str, Any], emit: Callable) -> Callable[[Any], None]:
    """Map raw Omp frames to Chudware agent events and forward them to ``emit``."""
    from services.agent.omp_events import RpcFrameState, map_rpc_frame
    from services.agent.providers import token_usage

    state = RpcFrameState()

    def raw_emit(frame: Any) -> None:
        raw = frame if isinstance(frame, Mapping) else {"type": "unknown", "value": frame}
        for kind, payload in map_rpc_frame(raw, event_context, state):
            data = dict(payload) if isinstance(payload, Mapping) else {"payload": payload}
            data["kind"] = kind
            # Transport routing is owned by this turn, not by a nested runtime's
            # own run/session identifiers. Subagent and span IDs remain intact.
            for key in ("run_id", "session_id"):
                owner = event_context.get(key)
                if isinstance(owner, str) and owner:
                    data[key] = owner
            if kind == "agent.provider_usage":
                token_usage.record_usage(
                    provider=str(data.get("provider") or ""),
                    model=str(data.get("model") or ""),
                    prompt_tokens=data.get("prompt_tokens"),
                    completion_tokens=data.get("completion_tokens"),
                    total_tokens=data.get("total_tokens"),
                    context_tokens=data.get("context_tokens"),
                    source=str(data.get("source") or "api"),
                )
            emit(data)

    return raw_emit


def _admin_compaction(
    compact: Any, launch_params: dict[str, Any], raw_emit: Callable[[Any], None]
) -> dict:
    """Manual ``/compact``: one cancellable compaction run on the conversation's session.

    ``compact`` is ``{strategy?, instructions?}``. Without a strategy the persisted
    Compaction setting applies; an explicit ``summary``/``snapcompact`` runs for this
    compaction only. ``instructions`` focuses the summary. The reply is the turn shape
    with ``turn.compaction`` instead of answer text.
    """
    if not isinstance(compact, Mapping) or set(compact) - {"strategy", "instructions"}:
        return {"ok": False, "reason": "compact must be {strategy?, instructions?}"}
    raw_strategy = compact.get("strategy")
    instructions = compact.get("instructions")
    if raw_strategy is not None and not isinstance(raw_strategy, str):
        return {"ok": False, "reason": "compact strategy must be a string"}
    if instructions is not None and not isinstance(instructions, str):
        return {"ok": False, "reason": "compact instructions must be a string"}
    requested = raw_strategy.strip().lower() if raw_strategy and raw_strategy.strip() else None
    chosen = _online_manual_compaction(
        requested, launch_params["settings"]["compaction_strategy"]
    )
    if isinstance(chosen, str):
        return {"ok": False, "reason": chosen, "stop_reason": "compaction_refused"}
    strategy, mode = chosen
    focus = instructions.strip() if instructions else ""
    host_result = omp_compaction(
        {**launch_params, "compact": {"mode": mode, "instructions": focus or None}},
        raw_emit,
    )
    if not host_result.get("ok"):
        outcome = host_result.get("outcome") or "failed"
        return {
            "ok": False,
            "reason": str(host_result.get("reason") or "Compaction failed"),
            "stop_reason": str(host_result.get("stop_reason") or outcome),
            "turn": {"success": False, "outcome": outcome, "stop_reason": outcome,
                     "omp_runtime_live": host_result.get("omp_runtime_live"),
                     "compaction": {"outcome": outcome, "strategy": strategy}},
        }
    compaction = dict(host_result.get("compaction") or {})
    outcome = host_result.get("outcome") or compaction.get("outcome")
    if outcome not in {"committed", "skipped", "cancelled", "failed", "interrupted"}:
        return {"ok": False, "reason": "Compaction returned no authoritative outcome",
                "stop_reason": "compaction_failed"}
    cancelled = outcome == "cancelled"
    compaction["outcome"] = outcome
    compaction["strategy"] = strategy
    return {
        "ok": True,
        "turn": {
            "success": outcome == "committed",
            "stop_reason": "completed" if outcome == "committed" else outcome,
            "outcome": outcome,
            "result": "",
            "run_id": host_result.get("run_id") or launch_params["run_id"],
            "session_id": host_result.get("session_id") or launch_params["session_id"],
            "omp_session_id": host_result.get("omp_session_id"),
            "omp_session_file": host_result.get("omp_session_file"),
            "omp_runtime_live": host_result.get("omp_runtime_live"),
            "cancelled": cancelled,
            "compaction": compaction,
        },
    }


def _agent_admin_turn(params: dict, _emit: Callable) -> dict:
    """Run the ordinary online lane exclusively through persistent Omp RPC."""
    from services.mcp.daemon_bridge import TurnConnections, turn_connection_scope

    # Preflight context reads and the final save sweep run on the persistent
    # worker's request thread, not a host-tool thread. Give those CAD calls an
    # explicit request owner too; its sockets must not outlive this turn.
    scope = params.get("_hostd_doc_scope", "") if isinstance(params, dict) else ""
    connections = TurnConnections(agent_run_scope=scope if isinstance(scope, str) else "")
    try:
        with turn_connection_scope(connections):
            return _run_agent_admin_turn(params, _emit)
    finally:
        connections.close()


def _run_agent_admin_turn(params: dict, _emit: Callable) -> dict:
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
    if in_cell():
        try:
            require_handle(session_id, "session")
            require_handle(run_id, "run", session_id)
        except (KeyError, TypeError):
            return {"ok": False, "reason": "not_found", "code": "not_found"}
        # Provider key bytes never enter a cell, so a cell has no credential
        # route for an online turn.
        return {"ok": False, "reason": "credential_isolation_unsupported",
                "code": "credential_isolation_unsupported"}
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

    project_root, save_dir, workspace_error = resolve_workspace(raw_params)
    if workspace_error is not None:
        return {"ok": False, "reason": workspace_error}

    try:
        from services.agent import config as agent_config
    except ImportError as exc:
        return {"ok": False, "reason": f"agent_admin_turn: import failed: {exc}"}

    def refuse_before_run(reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": reason}

    cfg = dict(agent_config.load_config() or {})

    byok_key = raw_params.get("api_key")
    if byok_key is not None and (not isinstance(byok_key, str) or not byok_key.strip()):
        return refuse_before_run("byok_unsupported_online")
    try:
        (turn_preferences, provider_name, model_name, assignment) = resolve_model_and_effort(
            request, raw_params, cfg, byok_key,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return refuse_before_run(str(exc))

    attachments = None
    raw_attachments = raw_params.get("attachments")
    if isinstance(raw_attachments, list):
        if len(raw_attachments) > _ATTACH_MAX:
            return refuse_before_run("attachment_count_exceeded")
        attachments = [
            dict(item) for item in raw_attachments if isinstance(item, Mapping)
        ] or None

    resumed_session = bool(param_str("omp_session_file"))
    sessions_dir = param_str("sessions_dir")
    generation = _daemon_generation()
    previous_generation = _previous_daemon_generation(session_id, sessions_dir)
    generation_changed = _daemon_generation_changed(previous_generation, generation)
    options = {"project_dir": project_root} if project_root else {}
    options.update({"mode": turn_preferences.mode,
                    "advisor": turn_preferences.advisor,
                    "max_subagent_depth": turn_preferences.delegation_depth})
    options["session_id"] = session_id
    for key in ("mode", "plan_only", "advisor", "autonomy"):
        if key in raw_params:
            options[key] = raw_params[key]
    if turn_preferences.mode == "ask":
        options["mode"] = "ask"
    readonly_turn = bool(options.get("plan_only")) or options["mode"] in ("ask", "plan")
    # Turn budget: only an explicit positive finite `timeout` bounds the turn
    # (run_mission.py --timeout-s). Without one the host has no wall-clock stop and the
    # agent is told of none.
    budget_s: float | None = None
    raw_timeout = raw_params.get("timeout")
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
        if math.isfinite(raw_timeout) and raw_timeout > 0:
            budget_s = float(raw_timeout)

    try:
        from services.agent.omp_tools import build_host_tools
        from services.agent.mission_dispatch import MissionDispatch, role_prompt
        from services.agent.providers import token_usage
    except ImportError as exc:
        return refuse_before_run(f"agent_admin_turn: import failed: {exc}")

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

    try:
        system_prompt, allowed_tools = role_prompt(
            role,
            card=card,
            capability=turn_capability,
        )
    except Exception as exc:  # noqa: BLE001
        return refuse_before_run(
            f"agent_admin_turn: prompt assembly failed: {type(exc).__name__}: {exc}"
        )

    try:
        settings = resolve_settings(
            raw_params, turn_preferences, provider_name, model_name, cfg
        )
    except ValueError as exc:
        return refuse_before_run(f"invalid_compaction_strategy: {exc}")

    try:
        launch_params = _session_launch_params(
            raw_params, run_id=run_id, session_id=session_id, options=options,
            system_prompt=system_prompt, settings=settings, project_root=project_root,
            byok_key=byok_key, provider_name=provider_name,
        )
    except (OSError, ValueError) as exc:
        return refuse_before_run(f"invalid_memory_backend: {exc}")
    event_context = {
        "run_id": run_id,
        "session_id": session_id,
        "role": role,
        "project_root": project_root,
    }
    if "compact" in raw_params:
        return _admin_compaction(
            raw_params["compact"], launch_params, _rpc_event_emitter(event_context, _emit)
        )

    try:
        mission_dispatch = MissionDispatch(
            run_id=run_id,
            provider=None,
            on_event=None,
            capability=turn_capability,
            tier=turn_tier,
            options=options or None,
        )
    except Exception as exc:  # noqa: BLE001
        return refuse_before_run(
            f"agent_admin_turn: prompt assembly failed: {type(exc).__name__}: {exc}"
        )

    # A restart notice is justified only by two known, different daemon generations.
    editor_block = "\n\n".join(block for block in (
        _RESUMED_CONVERSATION_NOTE if generation_changed else "",
        _picked_context_text(raw_params),
        turn_preferences.turn_notice(),
        turn_budget_notice(budget_s, readonly=readonly_turn),
    ) if block)

    requested_categories = raw_params.get("categories")
    categories = list(dict.fromkeys(
        value for value in (requested_categories if isinstance(requested_categories, list) else ())
        if isinstance(value, str) and value.strip()
    ))
    tool_context: dict[str, Any] = {
        "role_card": card,
        "allowed_tools": allowed_tools,
        "mission_dispatch": mission_dispatch,
        "capability": turn_capability,
        "tier": turn_tier,
        "actor": role,
        "ledger": getattr(mission_dispatch, "ledger", None),
        "run_id": run_id,
        # The skill-injection dedupe key: a trigger-matched skill body is
        # prefixed to its tool's first result per CONVERSATION, not per turn.
        "session_id": session_id,
        "workspace_dir": project_root,
        "project_dir": project_root,
        "save_dir": save_dir,
        "options": options,
        "mode": options["mode"],
        "plan_only": options.get("plan_only"),
        "turn_preferences": turn_preferences,
        "subagent_id": raw_params.get("subagent_id"),
        "categories": categories,
        "enabled_categories": categories,
        "persist_tool_loads": True,
        "tool_load_tenant": launch_params.get("byok_tenant", ""),
        "legacy_tool_loads_owner": lambda: _legacy_tool_loads_owner(
            raw_params, session_id, project_root
        ),
        "tool_call_counts": {},
        "tool_calls": [],
    }
    try:
        host_tools = build_host_tools(tool_context)
    except Exception as exc:  # noqa: BLE001
        return refuse_before_run(
            f"agent_admin_turn: host tool assembly failed: {type(exc).__name__}: {exc}"
        )

    try:
        from services.agent.omp_roles import _register_category_refresh

        register_callbacks = _register_category_refresh(tool_context, host_tools)
    except Exception as exc:  # noqa: BLE001 - registration can fail before turn activation
        return refuse_before_run(
            f"agent_admin_turn: host tool assembly failed: {type(exc).__name__}: {exc}"
        )

    try:
        migration, claimed_history_seed = _claim_history_seed(
            session_id, raw_params.get("history"), resumed=resumed_session
        )
    except ValueError as exc:
        return {"ok": False, "code": "unsupported_history", "reason": str(exc)}
    prompt = compose_request_text(request, migration, editor_block)
    host_params: dict[str, Any] = {
        **launch_params,
        "request": prompt,
        "host_tools": host_tools,
        "attachments": attachments,
        "register_callbacks": register_callbacks,
        "_hostd_doc_scope": raw_params.get("_hostd_doc_scope", ""),
    }
    if isinstance(raw_params.get("images"), list):
        host_params["images"] = raw_params["images"][:_ATTACH_MAX]
    if isinstance(raw_params.get("host_uris"), list):
        host_params["host_uris"] = raw_params["host_uris"]
    # The desktop chat can show ask_user batches and answer them; headless callers
    # (run_mission.py) omit the flag and keep the suggested-default path.
    if raw_params.get("interactive_questions") is True:
        host_params["interactive_questions"] = True
    if budget_s is not None:
        host_params["timeout"] = budget_s
    raw_emit = _rpc_event_emitter(event_context, _emit)

    turn_env = bind_roots(project_root, save_dir)
    try:
        turn_env.__enter__()
    except Exception as exc:  # noqa: BLE001
        if claimed_history_seed:
            with _HISTORY_SEED_LOCK:
                _HISTORY_SEEDED_SESSIONS.discard(scoped_key(session_id))
        return {
            "ok": False,
            "reason": f"agent_admin_turn: workspace activation failed: "
            f"{type(exc).__name__}: {exc}",
        }

    try:
        from services.agent import subagent_registry

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
        turn_env.__exit__(None, None, None)
        if claimed_history_seed:
            with _HISTORY_SEED_LOCK:
                _HISTORY_SEEDED_SESSIONS.discard(session_id)
        return {
            "ok": False,
            "reason": f"agent_admin_turn: usage setup failed: "
            f"{type(exc).__name__}: {exc}",
        }
    response: dict[str, Any]
    try:
        if assignment is not None:
            _emit({"kind": "sub_run", **event_context, "id": f"{run_id}:auto",
                   "purpose": assignment.reason, "status": "done"})
        host_result = omp_agent_turn(host_params, raw_emit)
        if not host_result.get("ok"):
            response = {
                "ok": False,
                "reason": str(host_result.get("reason") or "Agent turn failed"),
                "stop_reason": str(host_result.get("stop_reason") or "runtime_error"),
            }
            response["turn"] = {
                "success": False,
                "stop_reason": response["stop_reason"],
                "run_id": host_result.get("run_id") or run_id,
                "session_id": host_result.get("session_id") or session_id,
                "omp_session_id": host_result.get("omp_session_id"),
                "omp_session_file": host_result.get("omp_session_file"),
                "terminal": host_result.get("terminal"),
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
                "omp_session_id": host_result.get("omp_session_id"),
                "omp_session_file": host_result.get("omp_session_file"),
                "terminal": host_result.get("terminal"),
                "cancelled": cancelled,
            }
            response = {"ok": True, "turn": turn}
    except Exception as exc:  # noqa: BLE001 - crash path still persists documents
        response = {
            "ok": False,
            "stop_reason": "runtime_error",
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
            from services.plm.ai_vc import bind_save_session

            with bind_save_session(session_id):
                persistence = save_touched_docs(run_id=run_id)
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
        try:
            from services.mcp.cad_common import persistence_warning
            persistence_warnings, notice = persistence_warning(persistence)
        except Exception as exc:  # noqa: BLE001 — retain the completed turn on reporting failure
            persistence_warnings = [f"could not assess document persistence: {exc}"]
            notice = ("\n\n**Save warning:** " + persistence_warnings[0] +
                      ". Check the project files and Recover unsaved parts.")
        try:
            from services.mcp.cad_common import release_run_state
            release_run_state(run_id)
        except Exception as exc:  # noqa: BLE001
            finalization_errors.append(f"cad_state_cleanup: {type(exc).__name__}: {exc}")
        turn_env.__exit__(None, None, None)

        response["persistence"] = persistence
        response["usage"] = usage
        if isinstance(response.get("turn"), dict):
            response["turn"]["usage"] = usage
        if persistence_warnings:
            response["persistence_warnings"] = persistence_warnings
            if isinstance(response.get("turn"), dict):
                turn = response["turn"]
                turn["result"] = str(turn.get("result") or "") + notice
            if not response.get("ok"):
                response["reason"] = str(response.get("reason") or "agent_admin_turn failed") + notice
            try:
                raw_emit({"type": "assistant_delta", "channel": "output", "text": notice})
            except Exception:  # noqa: BLE001 — warning transport cannot replace the turn outcome
                pass
        if finalization_errors:
            response["finalization_errors"] = finalization_errors
            joined = "; ".join(finalization_errors)
            if response.get("ok"):
                response["ok"] = False
                response["reason"] = f"agent_admin_turn finalization failed: {joined}"
            else:
                prior = str(response.get("reason") or "agent_admin_turn failed")
                response["reason"] = f"{prior}; {joined}"
        _remember_daemon_generation(session_id, sessions_dir, generation)

    return response


ADMIN_METHODS: dict[str, Callable] = {
    "agent_handles_new": _agent_handles_new,
    "agent_admin_turn": _agent_admin_turn,
    "agent_cancel": _agent_cancel,
    "agent_steer": _agent_steer,
    "omp_provider_list": _omp_provider_list,
    "omp_auth_state": _omp_auth_state,
    "omp_set_key": _omp_set_key,
    "omp_remove_key": _omp_remove_key,
    "omp_logout_accounts": _omp_logout_accounts,
    "omp_logout": _omp_logout,
    "omp_login": _omp_login,
    "omp_models_list": _omp_models_list,
    "omp_session_stats": _omp_session_stats,
    "omp_export_session": _omp_export_session,
    "omp_branch_session": _omp_branch_session,
    "omp_close_session": _omp_close_session,
    "omp_apply_compaction_policy": _omp_apply_compaction_policy,
    "omp_set_auto_compaction": _omp_set_auto_compaction,
    "omp_set_auto_retry": _omp_set_auto_retry,
    "omp_tool_state": _omp_tool_state,
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
    """Settings → AI → Dev Preview: the general_agent prompt for one lane.

    ``lane`` is ``"online"`` (default; Omp system prompt + host-tool
    definitions) or ``"offline"`` (the explicit local-model fallback's prompt).
    """
    try:
        from services.agent.prompt_preview import preview_main_chat_system  # noqa: PLC0415

        include_tools = bool((params or {}).get("include_tools", True))
        lane = str((params or {}).get("lane") or "online")
        return preview_main_chat_system(include_tools=include_tools, lane=lane)
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
    """Estimated CAD contribution to a FRESH online chat, before the user types.

    Backs the copilot context meter: with no provider call yet, the meter had no
    honest source and rendered an em-dash over the window. This returns
    ``prompt_cache.baseline()`` — a ~4 chars/token estimate of the online system
    prompt plus the host-tool definitions CAD hands Omp, with the
    per-block breakdown the meter tooltip renders. Omp's own system prompt
    and builtin tools are not included, so it is never the exact upstream count.
    """
    try:
        from services.agent.prompt_cache import baseline  # noqa: PLC0415

        role = str((params or {}).get("role") or "general_agent")
        return baseline(role=role)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "tokens": 0,
            "chars": 0,
            "segments": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _ai_prompt_warm(params: dict, _emit: Callable) -> dict:
    """Compose the online main-chat prompt ahead of the user's first message.

    The worker already warms at start (``worker_main._warm_prompt_cache``); this
    is the explicit request. Composition costs ~1.9 s in a cold process
    (dominated by importing the tool registry), and that cost used to land on
    the user's first request.
    """
    try:
        from services.agent.prompt_cache import warm  # noqa: PLC0415

        blocking = bool((params or {}).get("blocking"))
        warm(background=not blocking)
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


# Merged registration table (``worker_main._register("agent_gui", ...)``).
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

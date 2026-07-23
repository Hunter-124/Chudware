"""Worker-RPC bridge for the services.agent-owned GUI seams (T-RF19 fold).

Single registration point for the worker-RPC tables that were squatting in
``engine_oracle/parts_hub/`` although every one is a thin wrapper over
:mod:`services.agent` modules:

- ``subscription_*`` (§4.4/§4.5 auth, Settings Accounts) — over agent.providers
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
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ──── from engine_oracle/parts_hub/subscription_rpc.py (T-RF19 fold) ────


# provider friendly id -> live login progress (no tokens)
# keys: in_progress, error, user_code, verification_url, browser_opened, stage
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
    account_id = str(raw_aid).strip() if raw_aid is not None and str(raw_aid).strip() else None
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
        return {"ok": False, "reason": f"unknown subscription provider {provider!r}",
                "hint": f"Use one of: {', '.join(sorted(valid))}."}
    try:
        with _login_lock:
            if _login_state.get(provider, {}).get("in_progress"):
                return {"ok": True, "provider": provider, "in_progress": True, "already": True}
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
                    "stage", "user_code", "verification_url", "verification_uri",
                    "browser_opened", "email", "account_id", "expires_in", "interval",
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
            _login_state[provider] = {"in_progress": False, "error": str(exc), "stage": "error"}
        return {"ok": False, "reason": f"subscription_login failed: {exc}", "hint": ""}


def _subscription_logout(params: dict, _emit: Callable) -> dict:
    """Clear the stored credential for a provider (optional account_id; None = active)."""
    provider = str((params or {}).get("provider", "")).strip().lower()
    if not provider:
        return {"ok": False, "reason": "provider required (codex|claude|cursor|xai)"}
    raw_aid = (params or {}).get("account_id")
    account_id = str(raw_aid).strip() if raw_aid is not None and str(raw_aid).strip() else None
    try:
        out = _sl().logout(provider, account_id=account_id)
        with _login_lock:
            # Only clear live login progress when the active (or last) account is gone.
            if account_id is None or not _sl().status().get(provider, {}).get("connected"):
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
        return {"ok": False, "reason": f"subscription_accounts failed: {exc}", "hint": ""}


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
        from agent.providers import usage as usage_mod

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
        from agent import session_cmds as sc  # type: ignore
    return sc


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _session_compact(params: dict, _emit: Callable) -> dict:
    """``session_compact`` — snapcompact / shake / summary / off over host messages."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        strategy = str(p.get("strategy") or "off")
        keep = p.get("keep_recent_tokens", 20000)
        try:
            keep_i = int(keep)
        except (TypeError, ValueError):
            keep_i = 20000
        supersede = p.get("supersede_reads", True)
        drop_useless = p.get("drop_useless", True)
        prior = p.get("prior_archive")
        if prior is not None and not isinstance(prior, dict):
            prior = None
        result = _cmds().compact_session(
            messages,
            strategy,
            keep_recent_tokens=keep_i,
            supersede_reads=bool(supersede),
            drop_useless=bool(drop_useless),
            prior_archive=prior,
        )
        out = {
            "ok": True,
            "kept_messages": result.get("kept_messages") or [],
            "summary": result.get("summary") or "",
            "short_summary": result.get("short_summary") or "",
        }
        if result.get("archive") is not None:
            out["archive"] = result["archive"]
        if "cut_index" in result:
            out["cut_index"] = result["cut_index"]
        if "saved_tokens" in result:
            out["saved_tokens"] = result["saved_tokens"]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"session_compact failed: {exc}", "hint": ""}


def _session_shake(params: dict, _emit: Callable) -> dict:
    """``session_shake`` — mechanical elision of heavy tool/code content."""
    try:
        p = params or {}
        messages = _as_list(p.get("messages"))
        mode = str(p.get("mode") or "elide")
        aggressive = bool(p.get("aggressive", False))
        keep_boundary = p.get("keep_boundary_index")
        if keep_boundary is not None:
            try:
                keep_boundary = int(keep_boundary)
            except (TypeError, ValueError):
                keep_boundary = None
        result = _cmds().shake_session(
            messages,
            mode=mode,
            aggressive=aggressive,
            keep_boundary_index=keep_boundary,
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
    """``session_handoff`` — oneshot handoff document (host creates new session)."""
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
        document = _cmds().handoff_document(
            messages,
            focus=focus,
            provider=provider,
            model=model,
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
            return {"ok": False, "reason": "question required", "hint": "session_btw {messages, question, provider, model}"}
        provider = p.get("provider")
        model = p.get("model")
        if model is not None:
            model = str(model)
        answer = _cmds().btw_answer(
            messages,
            question,
            provider=provider,
            model=model,
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
                    "{provider, model, thinking?} | {mode:\"auto\", thinking?} | \"auto\"}. "
                    "thinking: auto|low|medium|high (default auto)."
                ),
            }

        # HARD floor at set-time for redaction fixed assignments.
        if "redaction" in partial:
            val = partial["redaction"]
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
    env = os.environ.get("AI_CAD_REPO") or os.environ.get("AICAD_ROOT")
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
                if rc is not None and (_active_thread is None or not _active_thread.is_alive()):
                    # Thread should have written final status; heal if it crashed.
                    if out.get("stage") not in ("done", "error", "building_leaderboard"):
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
        py, "-m", "tools.flywheel.bench.gym.cli", "run",
        "--real",
        "--challenges", "C1",
        "--tiers", tiers_csv,
        "--cycle", cycle,
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
    env.setdefault("AI_CAD_REPO", str(root))
    # Never inherit interactive TTY assumptions into the detached child.
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_dir = _reports_dir() / "gui_bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cycle}.log"

    _write_status({
        "running": True,
        "stage": "running",
        "last_error": None,
        "exit_code": None,
        "cmd": cmd,
        "log_path": str(log_path),
        "category": None,
    })

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
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"spawn failed: {exc}",
            "pid": None,
            "exit_code": None,
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    if rc != 0:
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"gym cli exited with code {rc} (see {log_path})",
            "exit_code": rc,
            "last_result_path": str(log_path),
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    # Rebuild leaderboard so the GUI table picks up the new cells.
    _write_status({"stage": "building_leaderboard", "exit_code": rc})
    lb_cmd = [sys.executable or "python3", "-m", "tools.flywheel.bench.gym.leaderboard", "build"]
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
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"leaderboard build failed: {exc}",
            "exit_code": rc,
            "last_result_path": str(log_path),
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    lb_path = _leaderboard_path()
    result_path = str(lb_path) if lb_path.exists() else str(log_path)
    if lb_rc != 0:
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"leaderboard build exited with code {lb_rc}",
            "exit_code": lb_rc,
            "last_result_path": result_path,
        })
    else:
        _write_status({
            "running": False,
            "stage": "done",
            "finished": _utc_now(),
            "last_error": None,
            "exit_code": 0,
            "last_result_path": result_path,
        })
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
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"category cmd build failed: {exc}",
            "category": category,
            "pid": None,
            "exit_code": None,
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    env = os.environ.copy()
    env.setdefault("AI_CAD_REPO", str(root))
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_dir = _reports_dir() / "gui_bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cycle}.log"
    cat_status = _category_status_path(category)
    cat_status.parent.mkdir(parents=True, exist_ok=True)

    _write_status({
        "running": True,
        "stage": "running",
        "last_error": None,
        "exit_code": None,
        "cmd": cmd,
        "log_path": str(log_path),
        "category": category,
        "mode": mode,
        "tiers": [],
    })
    try:
        cat_status.write_text(json.dumps({
            "category": category,
            "running": True,
            "stage": "running",
            "started": _utc_now(),
            "cycle": cycle,
            "log_path": str(log_path),
        }, indent=2), encoding="utf-8")
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
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"spawn failed: {exc}",
            "pid": None,
            "exit_code": None,
            "category": category,
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
    err = None if rc == 0 else f"category {category!r} exited with code {rc} (see {log_path})"
    _write_status({
        "running": False,
        "stage": stage,
        "finished": _utc_now(),
        "last_error": err,
        "exit_code": rc,
        "last_result_path": result_path,
        "pass_rate": pass_rate,
        "category": category,
    })
    try:
        cat_status.write_text(json.dumps({
            "category": category,
            "running": False,
            "stage": stage,
            "finished": _utc_now(),
            "exit_code": rc,
            "pass_rate": pass_rate,
            "last_result_path": result_path,
            "log_path": str(log_path),
            "cycle": cycle,
        }, indent=2), encoding="utf-8")
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
            _write_status({
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
            })
            t = threading.Thread(
                target=_run_category_pipeline,
                args=(category, cycle, modeling_mode if category == "modeling" else None),
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
        _write_status({
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
        })

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
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": str(exc),
        })
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
        # Optional repo override for tests via AI_CAD_REPO already handled in cats.
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


# Merged registration table (engine_oracle/worker.py does ``_METHODS.update``).
WORKER_METHODS: dict[str, Callable] = {}
WORKER_METHODS.update(SUBSCRIPTION_METHODS)
WORKER_METHODS.update(SESSION_METHODS)
WORKER_METHODS.update(SKILLS_METHODS)
WORKER_METHODS.update(TRIGGERS_METHODS)
WORKER_METHODS.update(ROLE_MODELS_METHODS)
WORKER_METHODS.update(BENCH_METHODS)
WORKER_METHODS.update(OFFICE_METHODS)

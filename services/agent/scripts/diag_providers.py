#!/usr/bin/env python3
"""Live multi-provider subscription smoke test.

For each signed-in subscription provider (xai, claude/anthropic, openai/codex,
cursor): restore/refresh tokens, fetch identity + usage where supported, then
send ONE minimal completion through the SAME provider class the app uses
(not raw HTTP). Secrets are scrubbed from stdout.

Usage (from services/agent or repo root):
  python3 services/agent/scripts/diag_providers.py
  python3 scripts/diag_providers.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import path: services/ on sys.path so `agent.providers.*` resolves.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[3]  # …/CAD
_SERVICES = _REPO / "services"
for p in (str(_SERVICES), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.providers import sub_auth  # noqa: E402
from agent.providers import subscription_login as sl  # noqa: E402
from agent.providers import usage as usage_mod  # noqa: E402

# Friendly id -> (provider_name for instantiate, cred store id)
# Mirrors subscription_login._PROVIDERS.
_PROVIDER_ORDER = ("xai", "claude", "codex", "cursor", "kimi")

_MSG_PING = [
    {
        "role": "user",
        "content": [{"type": "text", "text": "ping"}],
    }
]
_SYSTEM = "Reply with exactly: pong"
_MAX_TOKENS = 4
_BACKOFF_S = 20.0


def scrub(text: Any, n: int = 400) -> str:
    s = str(text)
    s = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<JWT>", s)
    s = re.sub(
        r"(?i)(refresh_token|access_token|api_key|authorization)"
        r"\s*[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"\1=<REDACTED>",
        s,
    )
    s = re.sub(
        r"(?i)Bearer\s+[A-Za-z0-9._\-+/=]+",
        "Bearer <REDACTED>",
        s,
    )
    s = re.sub(
        r"\b(?![\w.+-]+@)[A-Za-z0-9_\-]{48,}\b",
        lambda m: m.group(0)[:8] + "…",
        s,
    )
    return s[:n]


def _short_id(aid: Any) -> str | None:
    if aid is None:
        return None
    s = str(aid)
    if len(s) <= 12:
        return s
    return s[:8] + "…"


def _token_validity(cred: dict | None) -> dict:
    if not cred:
        return {"has_access": False, "has_refresh": False, "expired": True}
    now = time.time() * 1000.0
    exp = float(cred.get("expires_at_ms") or 0)
    access = bool(cred.get("access_token"))
    refresh = bool(cred.get("refresh_token"))
    remaining_s = int((exp - now) / 1000.0) if exp else None
    jwt_ok = False
    token = str(cred.get("access_token") or "")
    if token.count(".") >= 2:
        claims = sub_auth.decode_jwt_payload(token)
        try:
            jwt_exp = float(claims.get("exp") or 0) * 1000.0
            jwt_ok = jwt_exp > now + 60_000
        except (TypeError, ValueError):
            jwt_ok = False
    expired = sub_auth.is_expired(cred, skew_ms=0) and not jwt_ok
    return {
        "has_access": access,
        "has_refresh": refresh,
        "expires_at_ms": exp or None,
        "remaining_s": remaining_s,
        "jwt_still_valid": jwt_ok,
        "store_expired": bool(sub_auth.is_expired(cred, skew_ms=0)),
        "usable_now": bool(access and (not expired or jwt_ok or refresh)),
    }


def _ensure_provider_auth(prov: Any) -> dict:
    """Call provider refresh path via _ensure_auth; report validity only."""
    step: dict[str, Any] = {"action": "ensure_auth"}
    before = _token_validity(getattr(prov, "_cred", None))
    step["before"] = before
    try:
        if hasattr(prov, "_ensure_auth"):
            prov._ensure_auth()
        step["ok"] = True
        step["after"] = _token_validity(getattr(prov, "_cred", None))
        aid = None
        cred = getattr(prov, "_cred", None) or {}
        aid = cred.get("account_id")
        step["active_account"] = _short_id(aid)
        step["email"] = cred.get("email")
        step["display_name"] = cred.get("display_name")
        step["plan"] = cred.get("plan")
    except Exception as exc:  # noqa: BLE001
        step["ok"] = False
        step["error"] = scrub(f"{type(exc).__name__}: {exc}", 300)
    return step


def _fetch_usage(friendly: str, account_id: str | None) -> dict:
    out: dict[str, Any] = {"provider": friendly, "account_id": _short_id(account_id)}
    try:
        if account_id:
            row = usage_mod.fetch_account_usage(friendly, account_id, force=True)
        else:
            pack = usage_mod.subscription_usage(friendly, force=True)
            rows = pack.get("rows") or []
            row = rows[0] if rows else {"source": "unavailable", "usage": []}
        out["ok"] = True
        out["display_name"] = row.get("display_name")
        out["email"] = row.get("email")
        out["plan"] = row.get("plan")
        out["source"] = row.get("source")
        usage = row.get("usage") or []
        # Compact windows only — no secrets.
        out["usage"] = [
            {
                "window": w.get("window") or w.get("label"),
                "used_fraction": w.get("used_fraction"),
                "status": w.get("status"),
                "resets_at": w.get("resets_at"),
            }
            for w in usage
            if isinstance(w, dict)
        ][:6]
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = scrub(f"{type(exc).__name__}: {exc}", 240)
    return out


def _is_429(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "429" in text or "rate-limit" in text or "rate limit" in text or "ratelimit" in text


def _completion(prov: Any, *, label: str) -> dict:
    """One minimal complete() through the provider class. Retry once on 429."""
    step: dict[str, Any] = {
        "provider_class": type(prov).__name__,
        "model": getattr(prov, "model", None),
        "max_tokens": _MAX_TOKENS,
    }
    attempts = 0
    last_err: str | None = None
    while attempts < 2:
        attempts += 1
        t0 = time.monotonic()
        try:
            # Prefer complete_ex so thinking models still return text.
            if hasattr(prov, "complete_ex"):
                text, thinking = prov.complete_ex(
                    _SYSTEM, _MSG_PING, max_tokens=_MAX_TOKENS
                )
            else:
                text = prov.complete(_SYSTEM, _MSG_PING, max_tokens=_MAX_TOKENS)
                thinking = ""
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            reply = (text or "").strip()
            step["ok"] = bool(reply)
            step["attempts"] = attempts
            step["elapsed_ms"] = elapsed_ms
            step["reply"] = scrub(reply, 80)
            step["has_thinking"] = bool(thinking)
            if not reply:
                step["error"] = "empty reply"
            return step
        except NotImplementedError as exc:
            step["ok"] = False
            step["attempts"] = attempts
            step["error"] = scrub(f"NotImplementedError: {exc}", 280)
            step["note"] = (
                "auth path exercised; chat transport not implemented for this provider"
            )
            return step
        except Exception as exc:  # noqa: BLE001
            last_err = scrub(f"{type(exc).__name__}: {exc}", 320)
            if attempts == 1 and _is_429(exc):
                step["backoff_s"] = _BACKOFF_S
                print(f"  [{label}] HTTP/rate-limit → sleep {_BACKOFF_S:.0f}s then retry once")
                time.sleep(_BACKOFF_S)
                continue
            step["ok"] = False
            step["attempts"] = attempts
            step["error"] = last_err
            step["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            return step
    step["ok"] = False
    step["attempts"] = attempts
    step["error"] = last_err or "unknown"
    return step


def diag_one(friendly: str) -> dict:
    resolved = sl._resolve(friendly)
    assert resolved is not None
    _fid, name, cred_id, label = resolved
    out: dict[str, Any] = {
        "provider": friendly,
        "provider_name": name,
        "cred_id": cred_id,
        "label": label,
        "steps": {},
    }

    # Presence check (any account with tokens).
    accts = sub_auth.list_accounts(cred_id)
    active_cred = sub_auth.load_credential(cred_id)
    connected = bool(accts) or (
        bool(active_cred)
        and bool(
            (active_cred or {}).get("access_token")
            or (active_cred or {}).get("refresh_token")
        )
    )
    out["accounts"] = [
        {
            "account_id": _short_id(a.get("account_id")),
            "email": a.get("email"),
            "display_name": a.get("display_name") or a.get("label"),
            "plan": a.get("plan"),
            "active": bool(a.get("active")),
        }
        for a in accts
    ]
    if not connected:
        out["verdict"] = "SKIP"
        out["reason"] = "no stored credentials"
        return out

    # Instantiate the SAME class the app uses.
    try:
        prov = sl._instantiate(name)
    except Exception as exc:  # noqa: BLE001
        out["verdict"] = "FAIL"
        out["error"] = scrub(f"instantiate: {type(exc).__name__}: {exc}", 240)
        return out

    # (a) restore / refresh
    auth_step = _ensure_provider_auth(prov)
    out["steps"]["auth"] = auth_step
    if not auth_step.get("ok"):
        out["verdict"] = "FAIL"
        out["error"] = auth_step.get("error") or "auth failed"
        return out

    # Identity from credential after ensure_auth
    cred = getattr(prov, "_cred", None) or {}
    out["identity"] = {
        "account_id": _short_id(cred.get("account_id")),
        "email": cred.get("email"),
        "display_name": cred.get("display_name"),
        "plan": cred.get("plan"),
    }

    # (b) usage (best-effort; failure does not alone fail the provider if completion works)
    aid = cred.get("account_id") or sub_auth.get_active_account_id(cred_id)
    usage_step = _fetch_usage(friendly, aid)
    out["steps"]["usage"] = usage_step
    # Prefer usage-sourced identity when store is sparse.
    if usage_step.get("ok"):
        for k in ("email", "display_name", "plan"):
            if not out["identity"].get(k) and usage_step.get(k):
                out["identity"][k] = usage_step.get(k)

    # (c) one live completion through the provider class
    chat_step = _completion(prov, label=friendly)
    out["steps"]["completion"] = chat_step

    # (d) verdict
    # Cursor: auth-only — PASS if auth+usage ok, with note that complete is N/I.
    if friendly == "cursor":
        if auth_step.get("ok") and chat_step.get("error", "").startswith("NotImplementedError"):
            out["verdict"] = "PASS"
            out["note"] = (
                "auth+usage exercised; complete() correctly raises NotImplementedError "
                "(chat transport not wired — expected)"
            )
        elif auth_step.get("ok") and chat_step.get("ok"):
            out["verdict"] = "PASS"
        else:
            out["verdict"] = "FAIL"
            out["error"] = chat_step.get("error") or auth_step.get("error")
        return out

    if auth_step.get("ok") and chat_step.get("ok"):
        out["verdict"] = "PASS"
    else:
        out["verdict"] = "FAIL"
        out["error"] = chat_step.get("error") or auth_step.get("error") or "unknown"
    return out


def main() -> int:
    print(f"repo={_REPO}")
    try:
        from agent import paths as agent_paths  # type: ignore

        cfg = Path(getattr(agent_paths, "CONFIG", ""))
    except Exception:
        cfg = Path.home() / ".local" / "share" / "AI-CAD" / "config.json"
    # sub_auth uses services.paths.CONFIG — report that too when available.
    try:
        from services import paths as eng_paths  # type: ignore

        eng_cfg = Path(eng_paths.CONFIG)
    except Exception:
        eng_cfg = cfg
    print(f"config: {eng_cfg} exists={eng_cfg.exists()}")
    print()

    status = sl.status()
    print("status snapshot (no secrets):")
    for fid in _PROVIDER_ORDER:
        st = status.get(fid) or {}
        print(
            f"  {fid:8s} connected={st.get('connected')} usable={st.get('usable')} "
            f"email={st.get('email')} account={_short_id(st.get('account_id'))} "
            f"plan={st.get('plan')} n_accounts={len(st.get('accounts') or [])}"
        )
    print()

    results: list[dict] = []
    for friendly in _PROVIDER_ORDER:
        print("=" * 64)
        print(f"PROVIDER {friendly}")
        try:
            r = diag_one(friendly)
        except Exception as exc:  # noqa: BLE001 — never abort the suite
            r = {
                "provider": friendly,
                "verdict": "FAIL",
                "error": scrub(f"{type(exc).__name__}: {exc}", 300),
                "traceback": scrub(traceback.format_exc(), 500),
            }
        results.append(r)
        # Print scrubbed JSON per provider
        print(json.dumps(r, indent=2, default=str))
        print(f"→ {r.get('verdict')}")
        print()

    print("=" * 64)
    print("SUMMARY")
    print(f"{'provider':10s} {'verdict':6s}  identity / usage / completion")
    print("-" * 64)
    any_fail = False
    any_pass = False
    for r in results:
        v = r.get("verdict") or "?"
        if v == "FAIL":
            any_fail = True
        if v == "PASS":
            any_pass = True
        ident = r.get("identity") or {}
        usage = (r.get("steps") or {}).get("usage") or {}
        chat = (r.get("steps") or {}).get("completion") or {}
        if v == "SKIP":
            detail = r.get("reason") or "skipped"
        else:
            id_s = ident.get("display_name") or ident.get("email") or ident.get("account_id") or "?"
            plan = ident.get("plan") or usage.get("plan") or "-"
            u_ok = "usage=ok" if usage.get("ok") else f"usage={usage.get('source') or usage.get('error') or 'n/a'}"
            if chat.get("ok"):
                c_s = f"complete=ok reply={chat.get('reply')!r}"
            elif chat.get("error"):
                c_s = f"complete=FAIL {scrub(chat.get('error'), 80)}"
            else:
                c_s = "complete=n/a"
            detail = f"{id_s} plan={plan} | {u_ok} | {c_s}"
            if r.get("note"):
                detail += f" | note={r['note'][:60]}"
            if r.get("error") and v == "FAIL":
                detail += f" | err={scrub(r.get('error'), 100)}"
        print(f"{r.get('provider'):10s} {v:6s}  {detail}")

    print("-" * 64)
    if any_fail:
        print("OVERALL FAIL")
        return 1
    if any_pass:
        print("OVERALL PASS (signed-in providers)")
        return 0
    print("OVERALL SKIP (no providers signed in)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Live xAI SuperGrok diagnostic — identity, usage, one cheap completion.

Reads stored credentials from the real config.json, refreshes if needed, then:
  1. OIDC userinfo (name/email)
  2. GET /v1/me (team block)
  3. GET /v1/models (auth + optional rate headers)
  4. One 1-token chat/completions round-trip (rate-limit headers + pong)

Secrets are scrubbed from stdout. Safe to paste into tickets.

Usage:
  python3 services/agent/scripts/diag_xai.py
  python3 services/agent/scripts/diag_xai.py --all-accounts
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
USERINFO_URL = "https://auth.x.ai/oauth2/userinfo"
ME_URL = "https://api.x.ai/v1/me"
MODELS_URL = "https://api.x.ai/v1/models"
CHAT_URL = "https://api.x.ai/v1/chat/completions"


def _config_path() -> Path:
    try:
        from agent import paths  # type: ignore

        return Path(paths.CONFIG)
    except Exception:
        pass
    return Path.home() / ".local" / "share" / "AI-CAD" / "config.json"


def scrub(text: str, n: int = 400) -> str:
    s = str(text)
    s = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<JWT>", s)
    s = re.sub(
        r"(?i)(refresh_token|access_token)\s*[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"\1=<REDACTED>",
        s,
    )
    s = re.sub(
        r"\b(?![\w.+-]+@)[A-Za-z0-9_\-]{48,}\b",
        lambda m: m.group(0)[:8] + "…",
        s,
    )
    return s[:n]


def http(method: str, url: str, headers=None, data=None, timeout: float = 30.0):
    body = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body = data
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            rh = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), raw, rh
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        rh = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return int(e.code), raw, rh
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", {}


def refresh(cred: dict) -> tuple[int, dict | None, str]:
    form = (
        f"grant_type=refresh_token&client_id={CLIENT_ID}"
        f"&refresh_token={cred.get('refresh_token') or ''}"
    )
    st, raw, _ = http(
        "POST",
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=form,
        timeout=30.0,
    )
    if st != 200:
        return st, None, scrub(raw, 200)
    try:
        return st, json.loads(raw), "ok"
    except json.JSONDecodeError:
        return st, None, scrub(raw, 200)


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1] + "=="
        return json.loads(base64.urlsafe_b64decode(part.encode()))
    except Exception:
        return {}


def _persist_tokens(aid: str, data: dict) -> bool:
    """Write rotated access/refresh back to config.json (xAI rotates RT)."""
    try:
        cfg_path = _config_path()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        entry = (cfg.get("subscription_auth") or {}).get("xai-sub") or {}
        accts = entry.get("accounts") or {}
        if aid not in accts:
            return False
        accts[aid]["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            accts[aid]["refresh_token"] = data["refresh_token"]
        accts[aid]["expires_at_ms"] = (
            time.time() * 1000 + int(data.get("expires_in") or 21600) * 1000
        )
        entry["accounts"] = accts
        cfg.setdefault("subscription_auth", {})["xai-sub"] = entry
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def diag_account(aid: str, cred: dict) -> dict:
    out: dict = {"account_id": aid[:8] + "…", "steps": {}}
    now_ms = time.time() * 1000
    exp = float(cred.get("expires_at_ms") or 0)
    access = cred.get("access_token") or ""

    # Prefer a still-valid access token over refresh — xAI rotates refresh
    # tokens on every exchange; a probe that doesn't persist burns the store.
    jwt_exp_ms = 0.0
    if access.count(".") >= 2:
        claims0 = jwt_claims(access)
        if claims0.get("exp"):
            jwt_exp_ms = float(claims0["exp"]) * 1000.0
    access_valid = bool(access) and (
        (jwt_exp_ms and jwt_exp_ms > now_ms + 60_000)
        or (exp > now_ms + 60_000)
    )

    if not access_valid:
        st, data, detail = refresh(cred)
        out["steps"]["refresh"] = {"status": st, "detail": detail}
        if st != 200 or not data:
            out["ok"] = False
            out["error"] = f"refresh failed HTTP {st}: {detail}"
            return out
        access = data["access_token"]
        out["steps"]["refresh"]["expires_in"] = data.get("expires_in")
        out["steps"]["refresh"]["has_new_refresh"] = bool(data.get("refresh_token"))
        out["steps"]["refresh"]["persisted"] = _persist_tokens(aid, data)
    else:
        out["steps"]["refresh"] = {
            "status": "skipped",
            "remaining_s": int(((jwt_exp_ms or exp) - now_ms) / 1000),
            "note": "using still-valid access_token",
        }

    claims = jwt_claims(access)
    out["steps"]["jwt"] = {
        "keys": sorted(claims.keys()),
        "tier": claims.get("tier"),
        "has_email": "email" in claims,
        "has_name": "name" in claims,
        "sub": (str(claims.get("sub") or "")[:8] + "…") if claims.get("sub") else None,
    }

    auth = {"Authorization": f"Bearer {access}", "Accept": "application/json"}

    st, raw, _ = http("GET", USERINFO_URL, headers=auth, timeout=15)
    body = None
    try:
        body = json.loads(raw) if st == 200 else None
    except json.JSONDecodeError:
        body = None
    out["steps"]["userinfo"] = {
        "status": st,
        "name": (body or {}).get("name") if isinstance(body, dict) else None,
        "email": (body or {}).get("email") if isinstance(body, dict) else None,
        "sub": (
            str((body or {}).get("sub") or "")[:8] + "…"
            if isinstance(body, dict) and body.get("sub")
            else None
        ),
        "body_scrubbed": scrub(raw, 180) if st != 200 else None,
    }

    st, raw, _ = http("GET", ME_URL, headers=auth, timeout=15)
    try:
        me = json.loads(raw) if st == 200 else None
    except json.JSONDecodeError:
        me = None
    out["steps"]["me"] = {
        "status": st,
        "team_blocked": (me or {}).get("team_blocked") if isinstance(me, dict) else None,
        "user_id": (
            str((me or {}).get("user_id") or "")[:8] + "…"
            if isinstance(me, dict) and me.get("user_id")
            else None
        ),
        "body_scrubbed": scrub(raw, 180) if st != 200 else None,
    }

    st, raw, rh = http("GET", MODELS_URL, headers=auth, timeout=20)
    rate = {k: v for k, v in rh.items() if "ratelimit" in k or "rate" in k}
    n_models = None
    if st == 200:
        try:
            n_models = len(json.loads(raw).get("data") or [])
        except Exception:
            n_models = None
    out["steps"]["models"] = {
        "status": st,
        "n_models": n_models,
        "rate_headers": rate,
        "body_scrubbed": scrub(raw, 120) if st != 200 else None,
    }

    st, raw, rh = http(
        "POST",
        CHAT_URL,
        headers={**auth, "Content-Type": "application/json"},
        data={
            "model": "grok-4.5",
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            "max_tokens": 4,
        },
        timeout=60,
    )
    rate = {k: v for k, v in rh.items() if "ratelimit" in k or k.startswith("x-")}
    content = None
    if st == 200:
        try:
            content = (
                (json.loads(raw).get("choices") or [{}])[0]
                .get("message", {})
                .get("content")
            )
        except Exception:
            content = None
    out["steps"]["chat"] = {
        "status": st,
        "content": content,
        "rate_headers": {k: v for k, v in rate.items() if "ratelimit" in k},
        "body_scrubbed": scrub(raw, 300) if st != 200 else scrub(raw, 200),
    }

    ui = out["steps"]["userinfo"]
    chat = out["steps"]["chat"]
    out["ok"] = (
        out["steps"]["refresh"].get("status") in (200, "skipped")
        and ui.get("status") == 200
        and bool(ui.get("name") or ui.get("email"))
        and chat.get("status") == 200
        and bool(chat.get("content"))
    )
    lim = rate.get("x-ratelimit-limit-requests")
    rem = rate.get("x-ratelimit-remaining-requests")
    if lim and rem:
        try:
            lim_f, rem_f = float(lim), float(rem)
            used = max(0.0, min(1.0, (lim_f - rem_f) / lim_f)) if lim_f > 0 else None
            out["usage_summary"] = {
                "requests_used_fraction": used,
                "limit_requests": lim,
                "remaining_requests": rem,
                "limit_tokens": rate.get("x-ratelimit-limit-tokens"),
                "remaining_tokens": rate.get("x-ratelimit-remaining-tokens"),
            }
        except ValueError:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--all-accounts",
        action="store_true",
        help="Probe every signed-in xAI account (default: active only)",
    )
    args = ap.parse_args()

    cfg_path = _config_path()
    print(f"config: {cfg_path} exists={cfg_path.exists()}")
    if not cfg_path.exists():
        print("FAIL: no config.json")
        return 2
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    xai = (cfg.get("subscription_auth") or {}).get("xai-sub") or {}
    accounts = xai.get("accounts") or {}
    active = xai.get("active")
    print(
        f"xai accounts: {len(accounts)} "
        f"active={str(active)[:8] + '…' if active else None}"
    )
    if not accounts:
        print("FAIL: no xai-sub accounts stored")
        return 2

    order = [active] if active and active in accounts else []
    order += [a for a in accounts if a not in order]
    if not args.all_accounts:
        order = order[:1]

    results = []
    for aid in order:
        print("=" * 60)
        print(f"account {aid[:8]}…")
        r = diag_account(aid, accounts[aid])
        results.append(r)
        print(json.dumps(r, indent=2, default=str))

    ok = all(r.get("ok") for r in results)
    print("=" * 60)
    print("OVERALL", "OK" if ok else "FAIL")

    # Also exercise the in-tree usage module if importable.
    try:
        root = Path(__file__).resolve().parents[3]  # repo root
        sys.path.insert(0, str(root / "services"))
        sys.path.insert(0, str(root))
        from agent.providers import usage as usage_mod  # type: ignore
        import os

        print("\n-- usage.fetch_account_usage (api path) --")
        os.environ.setdefault("AICAD_XAI_USAGE_PROBE", "0")
        for aid in order:
            row = usage_mod.fetch_account_usage("xai", aid, force=True)
            print(
                json.dumps(
                    {
                        "account_id": (str(row.get("account_id") or ""))[:8] + "…",
                        "display_name": row.get("display_name"),
                        "email": row.get("email"),
                        "plan": row.get("plan"),
                        "source": row.get("source"),
                        "usage": row.get("usage"),
                    },
                    indent=2,
                )
            )
    except Exception as exc:
        print(
            "usage module probe skipped:",
            type(exc).__name__,
            scrub(str(exc), 160),
        )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

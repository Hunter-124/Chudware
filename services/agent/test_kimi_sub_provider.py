"""Offline unit tests for kimi-sub (Kimi For Coding OAuth device flow).

Contract: contracts/providers.md §4.4 / §4.5 kimi-sub entry.
Mocks HTTP at urllib.request.urlopen — no network, no secrets.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import parse_qs

import pytest

from services.agent.providers import sub_auth
from services.agent.providers import usage


CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
OAUTH_HOST = "https://auth.kimi.com"
API_BASE = "https://api.kimi.com/coding/v1"
PROVIDER_ID = "kimi-sub"
DEFAULT_MODEL = "k3"


# ── fixtures / HTTP mock helpers ─────────────────────────────────────────────


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point the credential store at a throwaway config.json."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(sub_auth.paths, "CONFIG", cfg)
    # Device-id file lives next to config.json — keep it under tmp as well.
    if hasattr(sub_auth.paths, "DATA_DIR"):
        monkeypatch.setattr(sub_auth.paths, "DATA_DIR", tmp_path, raising=False)
    return cfg


class _FakeResp:
    def __init__(self, body: bytes | str | dict | list, status: int = 200, headers=None):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self._body = raw
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url: str, code: int, body: dict) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body).encode("utf-8"))
    return urllib.error.HTTPError(url, code, "Error", None, fp)


def _req_url(req) -> str:
    return getattr(req, "full_url", None) or getattr(req, "get_full_url", lambda: str(req))()


def _req_body(req) -> bytes:
    data = getattr(req, "data", None)
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    return bytes(data)


def _req_headers(req) -> dict[str, str]:
    # Request.headers keys are title-cased by urllib; also expose header_items.
    out: dict[str, str] = {}
    hdrs = getattr(req, "headers", None) or {}
    for k, v in hdrs.items():
        out[str(k).lower()] = str(v)
    if hasattr(req, "header_items"):
        try:
            for k, v in req.header_items():
                out[str(k).lower()] = str(v)
        except Exception:  # noqa: BLE001
            pass
    return out


def _form_fields(req) -> dict[str, str]:
    raw = _req_body(req).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


# ── identity / headers ───────────────────────────────────────────────────────


def test_provider_identity_and_common_headers(tmp_config):
    from services.agent.providers.kimi_sub_provider import (
        DEFAULT_MODEL as DM,
        KimiSubProvider,
        PROVIDER_ID as PID,
        get_kimi_common_headers,
    )

    assert PID == PROVIDER_ID
    assert DM == DEFAULT_MODEL
    p = KimiSubProvider()
    assert p.name == "kimi-sub"
    assert p.supports_vision is True
    assert p.model == DEFAULT_MODEL

    hdrs = get_kimi_common_headers()
    assert isinstance(hdrs, dict)
    assert hdrs.get("X-Msh-Platform") == "kimi_cli"
    assert "KimiCLI/" in str(hdrs.get("User-Agent", ""))
    assert hdrs.get("X-Msh-Device-Id")
    assert hdrs.get("X-Msh-Device-Name")
    assert hdrs.get("X-Msh-Device-Model")
    assert hdrs.get("X-Msh-Os-Version")
    assert hdrs.get("X-Msh-Version")
    # Cached per-process.
    assert get_kimi_common_headers() is hdrs or get_kimi_common_headers() == hdrs


# ── device authorization + poll ──────────────────────────────────────────────


def test_login_device_posts_client_id_and_saves_credential(tmp_config, monkeypatch):
    from services.agent.providers import kimi_sub_provider as ksp

    calls: list[dict[str, Any]] = []
    poll_state = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        method = getattr(req, "method", None) or "GET"
        body = _req_body(req)
        hdrs = _req_headers(req)
        calls.append({"url": url, "method": method, "body": body, "headers": hdrs})

        if "device_authorization" in url:
            fields = _form_fields(req)
            assert fields.get("client_id") == CLIENT_ID
            assert "application/x-www-form-urlencoded" in (
                hdrs.get("content-type") or ""
            )
            return _FakeResp({
                "user_code": "ABCD-1234",
                "device_code": "dev-code-1",
                "verification_uri": "https://auth.kimi.com/device",
                "verification_uri_complete": "https://auth.kimi.com/device?user_code=ABCD-1234",
                "expires_in": 900,
                "interval": 5,
            })

        if url.rstrip("/").endswith("/api/oauth/token") or "/api/oauth/token" in url:
            fields = _form_fields(req)
            assert fields.get("client_id") == CLIENT_ID
            assert fields.get("device_code") == "dev-code-1"
            assert fields.get("grant_type") == (
                "urn:ietf:params:oauth:grant-type:device_code"
            )
            poll_state["n"] += 1
            if poll_state["n"] == 1:
                raise _http_error(
                    url,
                    400,
                    {"error": "authorization_pending", "error_description": "wait"},
                )
            return _FakeResp({
                "access_token": "at-login",
                "refresh_token": "rt-login",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        raise AssertionError(f"unexpected urlopen: {method} {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ksp.time, "sleep", lambda _s: None)
    # Prefer no real browser.
    monkeypatch.setattr(
        ksp, "open_verification_url", lambda _u: False, raising=False
    )
    try:
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda *_a, **_k: False)
    except Exception:  # noqa: BLE001
        pass

    events: list[dict] = []
    p = ksp.KimiSubProvider()
    cred = p.login_device(open_browser=False, progress=events.append)

    assert cred["access_token"] == "at-login"
    assert cred["refresh_token"] == "rt-login"
    # sub_auth store uses ms expiry.
    assert "expires_at_ms" in cred
    assert isinstance(cred["expires_at_ms"], (int, float))
    # 3600s − 5 min skew → ~3300s from now.
    now_ms = time.time() * 1000
    assert cred["expires_at_ms"] < now_ms + 3600 * 1000
    assert cred["expires_at_ms"] > now_ms + 3000 * 1000

    stored = sub_auth.load_credential(PROVIDER_ID)
    assert stored is not None
    assert stored["access_token"] == "at-login"
    assert stored["refresh_token"] == "rt-login"
    assert stored["expires_at_ms"] == cred["expires_at_ms"]

    assert any("device_authorization" in c["url"] for c in calls)
    assert any("/api/oauth/token" in c["url"] for c in calls)
    assert any(e.get("user_code") == "ABCD-1234" for e in events)
    assert any(
        "verification" in (e.get("verification_url") or e.get("verification_uri") or "")
        or e.get("verification_url")
        or e.get("verification_uri")
        for e in events
    )


def test_poll_authorization_pending_then_slow_down(tmp_config, monkeypatch):
    from services.agent.providers import kimi_sub_provider as ksp

    sleeps: list[float] = []
    phase = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        if "device_authorization" in url:
            return _FakeResp({
                "user_code": "ZZZZ-0000",
                "device_code": "dev-slow",
                "verification_uri": "https://auth.kimi.com/device",
                "expires_in": 600,
                "interval": 5,
            })
        if "/api/oauth/token" in url:
            phase["n"] += 1
            n = phase["n"]
            if n == 1:
                raise _http_error(url, 400, {"error": "authorization_pending"})
            if n == 2:
                # slow_down: +5s on interval; payload may raise further.
                raise _http_error(
                    url,
                    400,
                    {"error": "slow_down", "interval": 12},
                )
            return _FakeResp({
                "access_token": "at-ok",
                "refresh_token": "rt-ok",
                "expires_in": 1800,
            })
        raise AssertionError(url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ksp.time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(
        ksp, "open_verification_url", lambda _u: False, raising=False
    )

    p = ksp.KimiSubProvider()
    cred = p.login_device(open_browser=False)
    assert cred["access_token"] == "at-ok"
    assert phase["n"] >= 3
    # At least one pending wait at the base interval, and slow_down grows by +5
    # and/or honors payload interval (12).
    assert sleeps, "expected time.sleep during poll"
    assert any(s >= 5 for s in sleeps)
    # After slow_down the next wait should be >= 10 (5+5) or >= 12 (payload).
    assert any(s >= 10 for s in sleeps)


# ── refresh ──────────────────────────────────────────────────────────────────


def test_refresh_keeps_old_refresh_token_when_omitted(tmp_config, monkeypatch):
    from services.agent.providers import kimi_sub_provider as ksp

    now_ms = time.time() * 1000
    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "old-at",
        "refresh_token": "keep-me-rt",
        "expires_at_ms": now_ms - 1000,
        "account_id": "acct-k",
        "email": "k@example.com",
    })

    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        assert "/api/oauth/token" in url
        fields = _form_fields(req)
        seen["fields"] = fields
        assert fields.get("grant_type") == "refresh_token"
        assert fields.get("refresh_token") == "keep-me-rt"
        assert fields.get("client_id") == CLIENT_ID
        # Deliberately omit refresh_token in the response.
        return _FakeResp({
            "access_token": "new-at",
            "expires_in": 7200,
            "token_type": "Bearer",
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    p = ksp.KimiSubProvider()
    cred = p.refresh()
    assert cred["access_token"] == "new-at"
    assert cred["refresh_token"] == "keep-me-rt"
    stored = sub_auth.load_credential(PROVIDER_ID)
    assert stored["refresh_token"] == "keep-me-rt"
    assert stored["access_token"] == "new-at"
    # Skew applied: expires_in 7200 → ~6900s window.
    assert stored["expires_at_ms"] < time.time() * 1000 + 7200 * 1000
    assert stored["expires_at_ms"] > time.time() * 1000 + 6800 * 1000


def test_refresh_non_ok_raises(tmp_config, monkeypatch):
    from services.agent.providers import kimi_sub_provider as ksp

    now_ms = time.time() * 1000
    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "old-at",
        "refresh_token": "bad-rt",
        "expires_at_ms": now_ms - 1,
    })

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        raise _http_error(
            url,
            400,
            {"error": "invalid_grant", "error_description": "revoked"},
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    p = ksp.KimiSubProvider()
    with pytest.raises(Exception) as ei:
        p.refresh()
    msg = str(ei.value).lower()
    assert "traceback" not in msg
    # Original credential untouched.
    assert sub_auth.load_credential(PROVIDER_ID)["access_token"] == "old-at"


# ── complete_ex ──────────────────────────────────────────────────────────────


def test_complete_ex_posts_messages_with_bearer_and_max_tokens(tmp_config, monkeypatch):
    from services.agent.providers import kimi_sub_provider as ksp

    now_ms = time.time() * 1000
    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "bearer-at",
        "refresh_token": "rt",
        "expires_at_ms": now_ms + 60 * 60 * 1000,
    })

    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        hdrs = _req_headers(req)
        body_raw = _req_body(req)
        captured["url"] = url
        captured["headers"] = hdrs
        captured["body"] = json.loads(body_raw.decode("utf-8"))
        assert url.rstrip("/").endswith("/messages") or "/messages" in url
        assert API_BASE in url or "api.kimi.com/coding/v1" in url
        auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
        assert auth.lower().startswith("bearer ")
        assert "bearer-at" in auth
        # Kimi common device id header required on completion.
        device = hdrs.get("x-msh-device-id") or hdrs.get("X-Msh-Device-Id")
        assert device
        assert "max_tokens" in captured["body"]
        assert captured["body"]["max_tokens"] >= 1
        return _FakeResp({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "plan it"},
                {"type": "text", "text": "hello from kimi"},
            ],
            "model": DEFAULT_MODEL,
            "stop_reason": "end_turn",
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    p = ksp.KimiSubProvider()
    text, thinking = p.complete_ex(
        "sys",
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        max_tokens=1024,
    )
    assert text == "hello from kimi"
    assert "plan" in thinking or thinking == "plan it"
    assert captured["body"]["max_tokens"] == 1024


# ── usage ────────────────────────────────────────────────────────────────────


def test_fetch_kimi_usage_parses_usage_and_limits(tmp_config, monkeypatch):
    from services.agent.providers import usage as usage_mod

    now_ms = int(time.time() * 1000)
    cred = {
        "access_token": "usage-at",
        "refresh_token": "rt",
        "expires_at_ms": now_ms + 10**9,
        "account_id": "k-acct",
        "email": "k@example.com",
    }
    sub_auth.save_credential(PROVIDER_ID, cred, account_id="k-acct")

    payload = {
        "usage": {
            "name": "Total quota",
            "used": 250,
            "limit": 1000,
            "remaining": 750,
            "resetTime": "2026-07-19T00:00:00Z",
        },
        "limits": [
            {
                "name": "5h window",
                "detail": {
                    "used": 40,
                    "limit": 100,
                    "remaining": 60,
                    "resetTime": "2026-07-18T12:00:00Z",
                },
                "window": {"duration": 5, "timeUnit": "hour"},
            },
            {
                "title": "Daily",
                "detail": {
                    "used": 10,
                    "limit": 50,
                    "remaining": 40,
                },
            },
        ],
    }

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = _req_url(req)
        hdrs = _req_headers(req)
        assert "/usages" in url
        auth = (hdrs.get("authorization") or "").lower()
        assert auth.startswith("bearer ")
        assert hdrs.get("x-msh-device-id") or hdrs.get("X-Msh-Device-Id")
        return _FakeResp(payload)

    # Prefer urlopen-level mock; also cover usage._http_get if the fetcher uses it.
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def fake_http_get(url, headers=None, timeout=20.0):  # noqa: ARG001
        assert "/usages" in url
        return 200, payload, {}

    if hasattr(usage_mod, "_http_get"):
        monkeypatch.setattr(usage_mod, "_http_get", fake_http_get)

    result = usage_mod.fetch_kimi_usage(cred)

    # Accept either a contract row ({usage: [...]}) or a list of parsed rows /
    # limits carrying used/limit/remaining/resets*.
    rows: list[dict]
    if isinstance(result, list):
        rows = result
    elif isinstance(result, dict):
        if isinstance(result.get("usage"), list):
            rows = list(result["usage"])
        elif isinstance(result.get("rows"), list):
            rows = list(result["rows"])
        elif isinstance(result.get("limits"), list):
            rows = list(result["limits"])
        else:
            # Single summary dict.
            rows = [result]
    else:
        raise AssertionError(f"unexpected fetch_kimi_usage return: {type(result)}")

    assert rows, "expected at least one usage row"

    def _has_quota_fields(row: dict) -> bool:
        keys = set(row.keys())
        # Direct omp-shaped fields…
        if {"used", "limit"} & keys or {"remaining"} & keys:
            return True
        # …or §4.5 contract fraction fields.
        if "used_fraction" in keys:
            return True
        # Nested amount (omp UsageLimit shape).
        amount = row.get("amount") if isinstance(row.get("amount"), dict) else {}
        if amount and ({"used", "limit", "remaining"} & set(amount.keys())):
            return True
        return False

    assert any(_has_quota_fields(r) for r in rows if isinstance(r, dict))

    def _has_reset(row: dict) -> bool:
        for k in ("resets", "resets_at", "resetTime", "reset_time"):
            if row.get(k):
                return True
        window = row.get("window") if isinstance(row.get("window"), dict) else {}
        if window.get("resetsAt") or window.get("resets_at"):
            return True
        amount = row.get("amount") if isinstance(row.get("amount"), dict) else {}
        return bool(amount.get("resets_at") or amount.get("resetsAt"))

    assert any(_has_reset(r) for r in rows if isinstance(r, dict))

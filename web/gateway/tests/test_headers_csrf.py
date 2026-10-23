"""Security headers, auth boundary, healthz disclosure, admin-static gating,
and chat WS identity/rate-limit/sanitization."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure package root is importable when running from gateway/ or repo root
GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Force dev mode BEFORE importing app
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from gateway import accounts  # noqa: E402
from gateway import app as app_mod  # noqa: E402
from gateway import chat as chat_mod  # noqa: E402
from gateway import db  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.limits import RateLimiter  # noqa: E402


def _seed(email: str, display: str, *, admin: bool = False) -> None:
    """Register and activate one account, optionally promoting it.

    accounts.py deliberately exposes no role setter — admin is granted through
    the admin API and never self-assigned — so the promotion is a direct row
    update here rather than a public call that does not (and should not) exist.
    """
    accounts.ensure_account(email, display=display)
    accounts.set_status(email, "active", decided_by="test")
    if admin:
        with db.tx() as cur:
            cur.execute(
                "UPDATE accounts SET role = 'admin' WHERE email = %s", (email,)
            )


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    """Per-test data root + seeded accounts + clean chat state."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "data_root", data)
    monkeypatch.setattr(settings, "dev_mode", True)

    # One standard user and one admin — the middleware authorizes from these
    # rows, so every identity these tests present has to exist in the store.
    _seed("demo@example.com", "Demo User")
    _seed("boss@example.com", "Boss", admin=True)

    # Fresh fan-out hub per test; history lives in the schema the pg fixture
    # rebuilt, so there is no store-level flag left to reset.
    monkeypatch.setattr(chat_mod, "hub", chat_mod.Hub())
    return data


@pytest.fixture
def client(_isolate):
    with TestClient(app_mod.app) as c:
        yield c


def _auth(email: str = "demo@example.com") -> dict[str, str]:
    return {"X-Dev-User": email}


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


def test_security_headers_present(client):
    r = client.get("/healthz")
    csp = r.headers.get("content-security-policy", "")
    assert csp, "no Content-Security-Policy sent"
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp
    assert "script-src" in csp and "unsafe-inline" not in csp.split("style-src")[0]
    assert r.headers["strict-transport-security"] == (
        "max-age=63072000; includeSubDomains"
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "geolocation=()" in r.headers["permissions-policy"]
    assert "microphone=()" in r.headers["permissions-policy"]
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cross-origin-opener-policy"] == "same-origin"


def test_served_html_has_no_inline_scripts():
    """The CSP (script-src 'self') breaks on inline <script> — none may exist."""
    for html in (GATEWAY_DIR / "static").glob("*.html"):
        body = html.read_text().lower()
        assert "<script>" not in body, f"inline <script> block in {html.name}"
        assert "javascript:" not in body, f"javascript: URL in {html.name}"


# ---------------------------------------------------------------------------
# Auth boundary + healthz disclosure
# ---------------------------------------------------------------------------


def test_healthz_prefix_is_not_public(client):
    """startswith('/healthz') used to skip auth for /healthzFOO too."""
    assert client.get("/healthz").status_code == 200
    r = client.get("/healthzFOO")
    assert r.status_code == 401
    r2 = client.get("/healthz/anything")
    assert r2.status_code != 401  # sub-path stays a public probe


def test_healthz_anonymous_is_bare(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "dev_mode" not in body
    assert "mem_available_bytes" not in body
    assert "mem_available_gib" not in body
    assert "vram" not in body
    assert "engine_bridges" not in body


def test_healthz_authenticated_gets_detail(client):
    r = client.get("/healthz", headers=_auth("demo@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert body["dev_mode"] is True
    assert "mem_available_bytes" in body
    assert "engine_bridges" not in body
    assert "provider_streams" not in body
    assert "cell_jobs" not in body


# ---------------------------------------------------------------------------
# Admin static gating
# ---------------------------------------------------------------------------


def test_admin_static_requires_admin_role(client):
    # Standard users cannot read the admin UI.
    for asset in ("admin.js", "admin.html", "admin_terminal.js", "admin_ops.css"):
        r = client.get(f"/static/{asset}", headers=_auth("demo@example.com"))
        assert r.status_code == 403, (asset, r.status_code)
    # Admin CAN read admin.js (the /admin page loads it from /static).
    r = client.get("/static/admin.js", headers=_auth("boss@example.com"))
    assert r.status_code == 200
    assert len(r.content) > 1000
    # /admin itself still renders for an admin.
    r = client.get("/admin", headers=_auth("boss@example.com"))
    assert r.status_code == 200
    assert "/static/admin.js" in r.text
    # Non-admin assets are unaffected.
    r = client.get("/static/base.css", headers=_auth("demo@example.com"))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Dev cookie flags + production fail-fast
# ---------------------------------------------------------------------------


def test_dev_cookie_samesite_strict_and_secure(client):
    """testserver is plain-http but NOT localhost, so Secure must be set."""
    r = client.get("/api/me", params={"dev_user": "demo@example.com"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "SameSite=Strict" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" not in set_cookie


def test_dev_cookie_no_secure_on_plain_http_localhost(_isolate):
    """Local dev over http://localhost must keep working (no Secure flag)."""
    with TestClient(app_mod.app, base_url="http://localhost") as c:
        r = c.get("/api/me", params={"dev_user": "demo@example.com"})
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "")
        assert "SameSite=Strict" in set_cookie
        assert "Secure" not in set_cookie


def test_auth_middleware_fails_fast_without_strong_auth():
    """Non-dev with neither a session secret nor CF Access must raise at
    construction instead of silently running with no working auth path."""
    from gateway.auth import AuthMiddleware
    from gateway.config import Settings

    bare = Settings(
        dev_mode=False, session_secret="", cf_team_domain="", cf_aud=""
    )
    with pytest.raises(RuntimeError, match="no strong auth"):
        AuthMiddleware(app_mod.app, cfg=bare)

    # Any ONE strong path configured is enough to boot.
    dev = Settings(
        dev_mode=True, session_secret="", cf_team_domain="", cf_aud=""
    )
    AuthMiddleware(app_mod.app, cfg=dev)
    # A self-serve deployment signs its own users in and needs no edge IdP.
    self_serve = Settings(
        dev_mode=False, session_secret="s3cret", cf_team_domain="", cf_aud=""
    )
    AuthMiddleware(app_mod.app, cfg=self_serve)
    cf = Settings(
        dev_mode=False,
        session_secret="",
        cf_team_domain="https://team.cloudflareaccess.com",
        cf_aud="aud",
    )
    AuthMiddleware(app_mod.app, cfg=cf)


# ---------------------------------------------------------------------------
# Chat WebSocket: identity, rate limit, sanitization
# ---------------------------------------------------------------------------


def _bare_chat_app():
    """chat.router with NO AuthMiddleware — proves the handler itself never
    trusts client-supplied identity even if the middleware is bypassed."""
    from fastapi import FastAPI

    bare = FastAPI()
    bare.include_router(chat_mod.router)
    return bare


def test_ws_chat_rejects_unauthenticated(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/chat"):
            pass
    assert exc.value.code == 4401


def test_ws_chat_router_level_never_trusts_query_identity(_isolate):
    """?email= is a full impersonation primitive if the middleware is ever
    bypassed — the handler must close 4401 without middleware state."""
    with TestClient(_bare_chat_app()) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/ws/chat?email=mallory@evil.example&display=Mallory"
            ):
                pass
        assert exc.value.code == 4401


def test_ws_chat_identity_never_comes_from_query_params(client):
    """?email= must not impersonate anyone — identity is the middleware's user."""
    with client.websocket_connect(
        "/ws/chat?email=mallory@evil.example&display=Mallory",
        headers=_auth("demo@example.com"),
    ) as ws:
        snap = ws.receive_json()
        assert snap["type"] == "presence"
        ws.send_text(json.dumps({"text": "who am i"}))
        msg = None
        for _ in range(8):
            ev = ws.receive_json()
            if ev.get("type") == "message":
                msg = ev
                break
        assert msg is not None
        assert msg["user"] == "demo@example.com"
        assert "mallory" not in msg["user"]


def test_ws_chat_strips_control_chars_and_caps_length(client):
    with client.websocket_connect("/ws/chat", headers=_auth("demo@example.com")) as ws:
        ws.receive_json()  # presence snapshot
        ws.send_text(json.dumps({"text": "hi\x00\x07\x1b[31m there\x7f"}))
        msg = None
        for _ in range(8):
            ev = ws.receive_json()
            if ev.get("type") == "message":
                msg = ev
                break
        assert msg is not None
        assert msg["text"] == "hi[31m there"

        ws.send_text(json.dumps({"text": "z" * 5000}))
        msg = None
        for _ in range(8):
            ev = ws.receive_json()
            if ev.get("type") == "message":
                msg = ev
                break
        assert msg is not None
        assert len(msg["text"]) == 2000


def test_ws_chat_rate_limit_notifies_without_teardown(client, monkeypatch):
    monkeypatch.setattr(chat_mod, "chat_messages", RateLimiter(2, 60.0))
    with client.websocket_connect("/ws/chat", headers=_auth("demo@example.com")) as ws:
        ws.receive_json()  # presence snapshot
        for text in ("one", "two", "three"):
            ws.send_text(json.dumps({"text": text}))
        events = []
        for _ in range(8):
            ev = ws.receive_json()
            events.append(ev)
            if ev.get("type") == "rate_limited":
                break
        assert events[-1]["type"] == "rate_limited"
        assert [e.get("text") for e in events if e.get("type") == "message"] == [
            "one",
            "two",
        ]
        # Socket stays up: another over-budget message gets another notice.
        ws.send_text(json.dumps({"text": "four"}))
        ev = ws.receive_json()
        assert ev["type"] == "rate_limited"

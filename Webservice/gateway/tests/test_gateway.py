"""Gateway tests — dev-mode auth, healthz, sessions (mocked podman), transfer safety."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

# Ensure package root is importable when running from gateway/ or repo root
GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Force dev mode BEFORE importing app
os.environ["BILLET_WEB_DEV"] = "1"
os.environ.setdefault("BILLET_WEB_DATA_ROOT", str(Path.home() / "billet-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway.auth import User, UserDirectory  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.sessions import Session, SessionManager, manager  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test data root + clean session manager state."""
    data = tmp_path / "data"
    data.mkdir()
    homes = data / "homes"
    plm = data / "plm"
    homes.mkdir()
    plm.mkdir()
    monkeypatch.setattr(settings, "data_root", data)
    monkeypatch.setattr(settings, "dev_mode", True)
    # fresh manager
    mgr = SessionManager(settings)
    monkeypatch.setattr(app_mod, "manager", mgr)
    monkeypatch.setattr("gateway.sessions.manager", mgr)
    monkeypatch.setattr("gateway.proxy.manager", mgr)
    # also patch module-level used by sessions API via app_mod import
    import gateway.sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "manager", mgr)
    # ensure users file points at package users.yaml
    monkeypatch.setattr(settings, "users_file", GATEWAY_DIR / "users.yaml")
    yield mgr
    # cleanup any leftover
    mgr._sessions.clear()
    mgr._by_email.clear()
    mgr._ports_in_use.clear()


@pytest.fixture
def client(_isolate):
    with TestClient(app_mod.app) as c:
        yield c


def _auth(email: str = "demo@example.com") -> dict[str, str]:
    return {"X-Dev-User": email}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "sessions" in body
    assert "mem_available_gib" in body
    assert "image_present" in body
    assert "dev_mode" in body
    assert body["dev_mode"] is True


def test_auth_allow(client):
    r = client.get("/api/sessions", headers=_auth("demo@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "none"
    assert body["sid"] is None


def test_auth_deny_unknown(client):
    r = client.get("/api/sessions", headers=_auth("nobody@evil.example"))
    assert r.status_code == 403
    # JSON for API paths
    body = r.json()
    assert body.get("error") in {"not_on_list", "not on the list"} or "not" in str(body).lower()


def test_auth_deny_missing_header(client):
    r = client.get("/api/sessions")
    assert r.status_code == 401


def test_not_on_list_html(client):
    r = client.get("/", headers={**_auth("ghost@example.com"), "Accept": "text/html"})
    assert r.status_code == 403
    assert "not on the list" in r.text.lower()


# ---------------------------------------------------------------------------
# Sessions (podman mocked)
# ---------------------------------------------------------------------------


def test_session_start_stop_mocked(client, _isolate, monkeypatch):
    mgr: SessionManager = _isolate
    calls: list[list[str]] = []

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        if args and args[0] == "run":
            return 0, "abc123deadbeef\n", ""
        if args and args[0] == "ps":
            return 0, "", ""
        if args and args[0] in {"stop", "rm"}:
            return 0, "", ""
        return 0, "", ""

    async def fake_wait(sess):
        return True

    monkeypatch.setattr(mgr, "run_podman", fake_podman)
    monkeypatch.setattr(mgr, "_wait_ready", fake_wait)
    # lower RAM guard interference — force plenty
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)

    r = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["sid"]
    assert body["stream_path"].startswith("/stream/")
    sid = body["sid"]

    # reconnect same user → same sid
    r2 = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r2.status_code == 200
    assert r2.json()["sid"] == sid

    # GET status
    r3 = client.get("/api/sessions", headers=_auth("demo@example.com"))
    assert r3.json()["status"] == "running"

    # stop
    r4 = client.delete("/api/sessions", headers=_auth("demo@example.com"))
    assert r4.status_code == 200
    assert r4.json()["status"] == "stopped"

    r5 = client.get("/api/sessions", headers=_auth("demo@example.com"))
    assert r5.json()["status"] == "none"

    # podman run was invoked
    assert any(c and c[0] == "run" for c in calls)


def test_session_refuse_new_flag(client, _isolate, monkeypatch, tmp_path):
    mgr: SessionManager = _isolate
    flag = settings.refuse_new_flag
    flag.write_text("1")
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)

    async def boom(*a, **k):
        raise AssertionError("podman should not run")

    monkeypatch.setattr(mgr, "run_podman", boom)
    r = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r.status_code == 503
    flag.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Transfer path traversal
# ---------------------------------------------------------------------------


def test_transfer_upload_download(client, _isolate):
    content = b"hello billet"
    files = {"file": ("part.step", io.BytesIO(content), "application/octet-stream")}
    r = client.post(
        "/transfer?path=uploads/part.step",
        headers=_auth("demo@example.com"),
        files=files,
    )
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] == len(content)

    r2 = client.get(
        "/transfer",
        headers=_auth("demo@example.com"),
        params={"path": "uploads/part.step"},
    )
    assert r2.status_code == 200
    assert r2.content == content


def test_transfer_path_traversal_rejected(client, _isolate):
    files = {"file": ("x", io.BytesIO(b"x"), "application/octet-stream")}
    r = client.post(
        "/transfer?path=../../etc/passwd",
        headers=_auth("demo@example.com"),
        files=files,
    )
    assert r.status_code == 400
    assert "traversal" in r.text.lower() or "invalid" in r.text.lower() or "path" in r.text.lower()

    r2 = client.get(
        "/transfer",
        headers=_auth("demo@example.com"),
        params={"path": "../etc/passwd"},
    )
    assert r2.status_code == 400


def test_portal_index(client):
    r = client.get("/", headers=_auth("demo@example.com"))
    assert r.status_code == 200
    # static index from PortalChat
    assert "html" in r.headers.get("content-type", "").lower() or b"html" in r.content[:200].lower()

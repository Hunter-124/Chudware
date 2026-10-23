"""Gateway tests — dev-mode auth, healthz, admin browse/ops, releases."""

from __future__ import annotations

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
os.environ.setdefault(
    "CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test")
)

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, db  # noqa: E402
from gateway.config import settings  # noqa: E402


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
            cur.execute("UPDATE accounts SET role = 'admin' WHERE email = %s", (email,))


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    """Per-test data root in dev mode, with the two test identities active."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "plm").mkdir()
    monkeypatch.setattr(settings, "data_root", data)
    monkeypatch.setattr(settings, "dev_mode", True)
    _seed("demo@example.com", "Demo User")
    _seed("boss@example.com", "Boss", admin=True)
    return data


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
    # Anonymous probes get a bare liveness result — host telemetry and
    # dev_mode stay behind auth (detail path: tests/test_headers_csrf.py).
    assert r.json() == {"ok": True}


def test_auth_allow(client):
    r = client.get("/api/me", headers=_auth("demo@example.com"))
    assert r.status_code == 200
    assert r.json()["email"] == "demo@example.com"


def test_auth_deny_unknown(client):
    """An identity the store has never seen is queued, never admitted."""
    r = client.get("/api/me", headers=_auth("nobody@evil.example"))
    assert r.status_code == 403
    # JSON for API paths, naming the state the caller is actually in.
    body = r.json()
    assert body["error"] == "not_authorized"
    assert body["account_status"] == "pending"
    assert body["email"] == "nobody@evil.example"


def test_auth_deny_missing_header(client):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_unapproved_browser_gets_its_own_status_page(client):
    """HTML callers get the page for the state they are actually in — a
    pending account is told to confirm its address, not to email an operator."""
    r = client.get("/", headers={**_auth("ghost@example.com"), "Accept": "text/html"})
    assert r.status_code == 403
    assert "confirm your email address" in r.text.lower()
    assert "ghost@example.com" in r.text


def test_dev_user_query_sets_cookie(client):
    """?dev_user= authenticates and Set-Cookies for subsequent subresources."""
    r = client.get("/api/me", params={"dev_user": "demo@example.com"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "dev_user=demo@example.com" in set_cookie
    assert "Path=/" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie or "SameSite=strict" in set_cookie
    # Plain-http localhost is the only Secure exemption; testserver is not.
    assert "Secure" in set_cookie


def test_dev_user_cookie_authenticates(client):
    """Cookie set by ?dev_user= is enough for a follow-up request (no query/header)."""
    r = client.get("/api/me", params={"dev_user": "demo@example.com"})
    assert r.status_code == 200
    # Follow-up with only the sticky cookie — no X-Dev-User header, no query.
    r2 = client.get("/api/me", cookies={"dev_user": "demo@example.com"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["email"] == "demo@example.com"
    # Cookie-only auth must NOT re-emit Set-Cookie (query-param path only).
    assert "dev_user=" not in r2.headers.get("set-cookie", "")


def test_dev_user_query_noop_when_not_dev(client, monkeypatch):
    """Without CHUDWARE_WEB_DEV / dev_mode, ?dev_user= is ignored (prod path)."""
    monkeypatch.setattr(settings, "dev_mode", False)
    r = client.get("/api/me", params={"dev_user": "demo@example.com"})
    assert r.status_code in {401, 500}
    assert "dev_user=" not in r.headers.get("set-cookie", "")


def test_portal_index(client):
    r = client.get("/", headers=_auth("demo@example.com"))
    assert r.status_code == 200
    # static index from PortalChat
    assert (
        "html" in r.headers.get("content-type", "").lower()
        or b"html" in r.content[:200].lower()
    )


# ---------------------------------------------------------------------------
# Shared AI provider env file parsing
# ---------------------------------------------------------------------------


def test_ai_provider_vars_parse(monkeypatch, tmp_path):
    """KEY=VALUE lines parse; lowercase and malformed lines are skipped."""
    env_file = tmp_path / "ai_providers.env"
    env_file.write_text(
        "# shared keys\n"
        "OPENROUTER_API_KEY=sk-or-123\n"
        "not_upper=skipped\n"
        "BROKEN LINE\n"
        "ANTHROPIC_API_KEY=sk-ant-456\n"
    )
    monkeypatch.setattr(
        type(settings), "ai_providers_env", property(lambda self: env_file)
    )
    assert settings.ai_provider_vars() == {
        "OPENROUTER_API_KEY": "sk-or-123",
        "ANTHROPIC_API_KEY": "sk-ant-456",
    }


# ---------------------------------------------------------------------------
# Admin project browsing
# ---------------------------------------------------------------------------


def test_team_domain_normalization(monkeypatch):
    from gateway.config import _normalize_team_domain

    assert (
        _normalize_team_domain("bruh.cloudflareaccess.com")
        == "https://bruh.cloudflareaccess.com"
    )
    assert (
        _normalize_team_domain("https://bruh.cloudflareaccess.com/")
        == "https://bruh.cloudflareaccess.com"
    )
    assert _normalize_team_domain("") == ""


def test_admin_browse_lists_project_roots(client, monkeypatch, tmp_path):
    # Browse is confined to the allowlisted project roots.
    monkeypatch.setattr(settings, "project_roots_raw", str(tmp_path))
    proj = tmp_path / "TeamParts"
    (proj / "sub").mkdir(parents=True)
    (proj / "Frame.step+").write_bytes(b"x")
    (proj / "notes.txt").write_bytes(b"x")

    # browse: admin only, lists dirs + CAD parts only
    assert (
        client.get(
            "/api/admin/browse",
            headers=_auth("demo@example.com"),
            params={"path": str(proj)},
        ).status_code
        == 403
    )
    r = client.get(
        "/api/admin/browse",
        headers=_auth("boss@example.com"),
        params={"path": str(proj)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dirs"] == ["sub"] and body["parts"] == ["Frame.step+"]


# ---------------------------------------------------------------- admin ops
def test_admin_ops(client):
    # ops: admin-gated, reports host health for the dashboard
    assert (
        client.get("/api/admin/ops", headers=_auth("demo@example.com")).status_code
        == 403
    )
    r = client.get("/api/admin/ops", headers=_auth("boss@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert "mem_available_gib" in body["host"]
    assert "units" in body["host"]
    assert body["refuse_new"] is False


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_admin_refuse_new_toggle(client):
    flag = settings.refuse_new_flag
    assert (
        client.put(
            "/api/admin/refuse-new",
            headers=_auth("demo@example.com"),
            json={"on": True},
        ).status_code
        == 403
    )
    r = client.put(
        "/api/admin/refuse-new", headers=_auth("boss@example.com"), json={"on": True}
    )
    assert r.status_code == 200 and r.json()["refuse_new"] is True and flag.exists()
    r3 = client.put(
        "/api/admin/refuse-new", headers=_auth("boss@example.com"), json={"on": False}
    )
    assert (
        r3.status_code == 200 and r3.json()["refuse_new"] is False and not flag.exists()
    )
    ops = client.get("/api/admin/ops", headers=_auth("boss@example.com")).json()
    assert ops["refuse_new"] is False and "units" in ops["host"]


# ------------------------------------------------------ shared admin terminal
@pytest.fixture
def _tmux_env(monkeypatch):
    """Inert session backend: terminal route tests never launch shells/tmux."""
    import queue
    import gateway.terminal as term

    sessions = {}
    markers = queue.Queue()
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(term, "_TMUX", "fixture-tmux")
    monkeypatch.setattr(term, "_list_sessions", lambda: list(sessions.values()))
    monkeypatch.setattr(term, "_session_exists", lambda name: name in sessions)

    def create(name, cols, rows, repo, owner="", label="", shared=False):
        sessions[name] = {
            "id": term._display(name), "name": label, "label": label,
            "owner": owner, "shared": shared, "clients": 0,
        }

    def kill(name):
        return sessions.pop(name, None) is not None

    async def detach(pid, fd):
        markers.put(b"")
        return 0

    monkeypatch.setattr(term, "_create_session", create)
    monkeypatch.setattr(term, "_kill_session", kill)
    monkeypatch.setattr(term, "_ensure_scroll_defaults", lambda: None)
    monkeypatch.setattr(term, "_spawn_attach", lambda *args: (987654, 987654))
    monkeypatch.setattr(term, "_read_pty", lambda fd: markers.get(timeout=5))
    monkeypatch.setattr(term, "_kill_child", detach)
    monkeypatch.setattr(term, "_reap", lambda pid: 0)
    yield term


def test_terminal_sessions_shared_lifecycle(client, _tmux_env):
    # admin-gated
    assert (
        client.get(
            "/api/admin/terminal/sessions", headers=_auth("demo@example.com")
        ).status_code
        == 403
    )

    body = client.get(
        "/api/admin/terminal/sessions", headers=_auth("boss@example.com")
    ).json()
    assert body["tmux"] is True and body["sessions"] == []

    # create a named shared session
    r = client.post(
        "/api/admin/terminal/sessions",
        headers=_auth("boss@example.com"),
        json={"name": "Work 1!"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "work-1"  # slugged

    # it now shows up in the owner's list (saved with zero clients attached)
    sessions = client.get(
        "/api/admin/terminal/sessions", headers=_auth("boss@example.com")
    ).json()["sessions"]
    names = {s["name"]: s for s in sessions}
    assert "work-1" in names and names["work-1"]["clients"] == 0

    # non-admins cannot create or kill
    assert (
        client.post(
            "/api/admin/terminal/sessions", headers=_auth("demo@example.com"), json={}
        ).status_code
        == 403
    )
    assert (
        client.delete(
            "/api/admin/terminal/sessions/work-1", headers=_auth("demo@example.com")
        ).status_code
        == 403
    )

    # auto-named create when no name given
    auto = client.post(
        "/api/admin/terminal/sessions", headers=_auth("boss@example.com"), json={}
    ).json()["name"]
    assert auto and auto != "work-1"

    # explicit kill removes it
    r = client.delete(
        "/api/admin/terminal/sessions/work-1", headers=_auth("boss@example.com")
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    left = {
        s["name"]
        for s in client.get(
            "/api/admin/terminal/sessions", headers=_auth("boss@example.com")
        ).json()["sessions"]
    }
    assert "work-1" not in left and auto in left


def test_terminal_demotion_closes_before_next_input(client, _tmux_env):
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(
        "/admin/terminal", headers=_auth("boss@example.com")
    ) as ws:
        ws.receive_json()
        with db.tx() as cur:
            cur.execute(
                "UPDATE accounts SET role = 'user' WHERE email = %s",
                ("boss@example.com",),
            )
        ws.send_bytes(b"printf should-not-run\\n")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
        assert exc.value.code == 4403


def test_releases_requires_admin(client):
    r = client.get("/api/admin/releases", headers=_auth("demo@example.com"))
    assert r.status_code == 403


def test_releases_empty_list(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(tmp_path / "releases-none"))
    r = client.get("/api/admin/releases", headers=_auth("boss@example.com"))
    assert r.status_code == 200
    assert r.json() == []


def test_releases_upload_list_download_delete(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(tmp_path / "releases"))
    admin = _auth("boss@example.com")
    payload = b"fake installer bytes " * 1000

    r = client.post(
        "/api/admin/releases/upload",
        headers=admin,
        files={
            "file": ("Chudware-Setup-1.0.0.exe", payload, "application/octet-stream")
        },
    )
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["name"] == "Chudware-Setup-1.0.0.exe"
    assert meta["size"] == len(payload)

    r2 = client.get("/api/admin/releases", headers=admin)
    assert r2.status_code == 200
    names = [row["name"] for row in r2.json()]
    assert names == ["Chudware-Setup-1.0.0.exe"]

    r3 = client.get(
        "/api/admin/releases/download/Chudware-Setup-1.0.0.exe", headers=admin
    )
    assert r3.status_code == 200
    assert r3.content == payload

    r4 = client.delete("/api/admin/releases/Chudware-Setup-1.0.0.exe", headers=admin)
    assert r4.status_code == 200
    assert r4.json() == {"ok": True}

    r5 = client.get("/api/admin/releases", headers=admin)
    assert r5.json() == []

    r6 = client.delete("/api/admin/releases/Chudware-Setup-1.0.0.exe", headers=admin)
    assert r6.status_code == 404


def test_releases_upload_requires_admin(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(tmp_path / "releases-forbidden"))
    r = client.post(
        "/api/admin/releases/upload",
        headers=_auth("demo@example.com"),
        files={"file": ("x.exe", b"nope", "application/octet-stream")},
    )
    assert r.status_code == 403


def test_releases_download_rejects_path_traversal(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_RELEASES_DIR", str(tmp_path / "releases-safe"))
    admin = _auth("boss@example.com")
    r = client.get("/api/admin/releases/download/..%2F..%2Fetc%2Fpasswd", headers=admin)
    assert r.status_code in (400, 404)
    r2 = client.delete("/api/admin/releases/..%2F..%2Fetc%2Fpasswd", headers=admin)
    assert r2.status_code in (400, 404)

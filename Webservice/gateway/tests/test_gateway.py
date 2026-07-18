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

    # Hermetic by default: no real podman. Startup adopt_running and /transfer's
    # podman-cp helper would otherwise touch live containers.
    async def _no_podman(args, timeout=120.0):
        return 0, "", ""

    monkeypatch.setattr(mgr, "run_podman", _no_podman)

    fake_fs = tmp_path / "container-fs"  # emulated container /home/user
    fake_fs.mkdir()

    async def _fake_podman_cp(src, dst, timeout=120.0):
        import shutil

        def _map(p: str) -> Path:
            if ":" in p:  # container:path form
                inner = p.split(":", 1)[1]
                return fake_fs / inner.lstrip("/")
            return Path(p)

        s, d = _map(src), _map(dst)
        if not s.is_file():
            return 1, "no such file"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(s, d)
        return 0, ""

    monkeypatch.setattr(app_mod, "_podman_cp", _fake_podman_cp)
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


def test_auth_allow_ua_domain_as_standard_user(client):
    """Any @ua.edu / @crimson.ua.edu email is auto-approved as a STANDARD user."""
    for email in ("student42@crimson.ua.edu", "Prof.Smith@UA.EDU"):
        r = client.get("/api/sessions", headers=_auth(email))
        assert r.status_code == 200, (email, r.status_code, r.text)


def test_ua_domain_user_is_never_admin(client):
    """Domain-auto users must resolve to role='user', never admin."""
    from gateway.auth import _domain_auto_user, UserDirectory
    d = UserDirectory(settings.users_file)
    u = _domain_auto_user("someone@crimson.ua.edu", d)
    assert u is not None and u.role == "user" and u.heavy is False
    # A lookalike/spoof domain must NOT be approved.
    assert _domain_auto_user("evil@ua.edu.attacker.com", d) is None
    assert _domain_auto_user("rando@gmail.com", d) is None


def test_dev_user_query_sets_cookie(client):
    """?dev_user= authenticates and Set-Cookies for subsequent subresources."""
    r = client.get("/api/sessions", params={"dev_user": "demo@example.com"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "dev_user=demo@example.com" in set_cookie
    assert "Path=/" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie or "SameSite=lax" in set_cookie


def test_dev_user_cookie_authenticates(client):
    """Cookie set by ?dev_user= is enough for a follow-up request (no query/header)."""
    r = client.get("/api/sessions", params={"dev_user": "demo@example.com"})
    assert r.status_code == 200
    # Follow-up with only the sticky cookie — no X-Dev-User header, no query.
    r2 = client.get("/api/sessions", cookies={"dev_user": "demo@example.com"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "none"
    # Cookie-only auth must NOT re-emit Set-Cookie (query-param path only).
    assert "dev_user=" not in r2.headers.get("set-cookie", "")


def test_dev_user_query_noop_when_not_dev(client, monkeypatch):
    """Without BILLET_WEB_DEV / dev_mode, ?dev_user= is ignored (prod path)."""
    monkeypatch.setattr(settings, "dev_mode", False)
    r = client.get("/api/sessions", params={"dev_user": "demo@example.com"})
    assert r.status_code in {401, 500}
    assert "dev_user=" not in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Sessions (podman mocked)
# ---------------------------------------------------------------------------


def test_session_start_stop_mocked(client, _isolate, monkeypatch):
    mgr: SessionManager = _isolate
    calls: list[list[str]] = []

    running: set[str] = set()

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        if args and args[0] == "run":
            name = args[args.index("--name") + 1]
            running.add(name)
            return 0, "abc123deadbeef\n", ""
        if args and args[0] == "ps":
            # honor name filter the way real podman does
            name_filter = next(
                (a.split("=", 2)[-1].strip("^$") for a in args if a.startswith("name=")),
                None,
            )
            matches = [n for n in running if not name_filter or n == name_filter]
            return 0, "\n".join(matches), ""
        if args and args[0] in {"stop", "rm"}:
            running.discard(args[-1])
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


def _seed_session(mgr, email="demo@example.com"):
    sess = Session(
        sid="livesid", email=email, display="Demo User", slug="demo",
        container="billet-sess-demo", host_port=8460, heavy=False, status="ready",
    )
    mgr._sessions[sess.sid] = sess
    mgr._by_email[email] = sess.sid
    return sess


def test_transfer_requires_running_session(client, _isolate):
    files = {"file": ("x.step", io.BytesIO(b"x"), "application/octet-stream")}
    r = client.post("/transfer", headers=_auth("demo@example.com"), files=files)
    assert r.status_code == 409


def test_transfer_upload_download(client, _isolate):
    _seed_session(_isolate)
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
    _seed_session(_isolate)
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


# ---------------------------------------------------------------------------
# AI provider env passthrough
# ---------------------------------------------------------------------------


def test_ai_provider_vars_parse_and_forward(client, _isolate, monkeypatch, tmp_path):
    mgr = _isolate
    env_file = tmp_path / "ai_providers.env"
    env_file.write_text(
        "# shared keys\n"
        "OPENROUTER_API_KEY=sk-or-123\n"
        "not_upper=skipped\n"
        "BROKEN LINE\n"
        "ANTHROPIC_API_KEY=sk-ant-456\n"
    )
    monkeypatch.setattr(type(mgr.cfg), "ai_providers_env", property(lambda self: env_file))
    vars_ = mgr.cfg.ai_provider_vars()
    assert vars_ == {"OPENROUTER_API_KEY": "sk-or-123", "ANTHROPIC_API_KEY": "sk-ant-456"}

    calls: list[list[str]] = []

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        if args and args[0] == "run":
            return 0, "abc123\n", ""
        return 0, "", ""

    async def fake_wait(sess):
        return True

    monkeypatch.setattr(mgr, "run_podman", fake_podman)
    monkeypatch.setattr(mgr, "_wait_ready", fake_wait)
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)

    r = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r.status_code == 200, r.text
    run_call = next(c for c in calls if c and c[0] == "run")
    joined = " ".join(run_call)
    assert "OPENROUTER_API_KEY=sk-or-123" in joined
    assert "ANTHROPIC_API_KEY=sk-ant-456" in joined


# ---------------------------------------------------------------------------
# Admin allowlist API
# ---------------------------------------------------------------------------


@pytest.fixture
def _admin_env(monkeypatch, tmp_path):
    """Writable users file + mocked Access sync."""
    users = tmp_path / "users.yaml"
    users.write_text(
        "users:\n"
        "  - email: boss@example.com\n"
        "    display: Boss\n"
        "    role: admin\n"
        "    heavy: true\n"
        "  - email: demo@example.com\n"
        "    display: Demo User\n"
        "    role: user\n"
        "    heavy: false\n"
    )
    monkeypatch.setattr(settings, "users_file", users)
    # Middleware and admin endpoints SHARE app_mod.user_directory (passed to
    directory = app_mod.user_directory
    orig_path = directory.path
    monkeypatch.setattr(directory, "path", users)
    monkeypatch.setattr(directory, "_mtime", None)
    directory.reload(force=True)
    synced: list[str] = []

    async def fake_sync():
        synced.append("x")
        return {"synced": True, "detail": "mocked"}

    monkeypatch.setattr(app_mod, "_sync_access_policy", fake_sync)
    yield directory, synced, users
    directory.path = orig_path
    directory._mtime = None
    directory.reload(force=True)


def test_admin_requires_admin_role(client, _admin_env, monkeypatch):
    # demo is role=user in the _admin_env users file, but the MIDDLEWARE still
    # reads the package users.yaml; use its known non-admin user
    r = client.get("/api/admin/users", headers=_auth("demo@example.com"))
    assert r.status_code == 403


def test_admin_add_remove_user(client, _admin_env, monkeypatch):
    directory, synced, users = _admin_env
    # boss@example.com must ALSO exist in middleware users file to authenticate;
    # middleware uses package users.yaml. Point middleware directory at ours:
    import gateway.auth as auth_mod
    # authenticate via the app's shared directory by swapping settings path used
    # by the middleware's directory reload
    mw_dir = app_mod.user_directory
    r = client.post(
        "/api/admin/users",
        headers=_auth("boss@example.com"),
        json={"email": "friend@ua.edu", "display": "Friend", "role": "user", "heavy": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["access_policy"]["synced"]
    assert "friend@ua.edu" in users.read_text()
    assert synced

    r2 = client.delete("/api/admin/users/friend@ua.edu", headers=_auth("boss@example.com"))
    assert r2.status_code == 200
    assert "friend@ua.edu" not in users.read_text()

    # cannot remove yourself
    r3 = client.delete("/api/admin/users/boss@example.com", headers=_auth("boss@example.com"))
    assert r3.status_code == 400


def test_admin_rejects_bad_email(client, _admin_env):
    r = client.post(
        "/api/admin/users",
        headers=_auth("boss@example.com"),
        json={"email": "not-an-email"},
    )
    assert r.status_code == 400


def test_team_domain_normalization(monkeypatch):
    from gateway.config import _normalize_team_domain
    assert _normalize_team_domain("bruh.cloudflareaccess.com") == "https://bruh.cloudflareaccess.com"
    assert _normalize_team_domain("https://bruh.cloudflareaccess.com/") == "https://bruh.cloudflareaccess.com"
    assert _normalize_team_domain("") == ""


def test_admin_project_field_and_env(client, _admin_env, _isolate, monkeypatch):
    directory, synced, users = _admin_env
    r = client.post(
        "/api/admin/users",
        headers=_auth("boss@example.com"),
        json={"email": "pal@ua.edu", "project": "Demo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["project"] == "Demo"
    assert "project: Demo" in users.read_text()

    # invalid project name rejected
    r2 = client.post(
        "/api/admin/users",
        headers=_auth("boss@example.com"),
        json={"email": "pal2@ua.edu", "project": "../evil"},
    )
    assert r2.status_code == 400

    # session env carries BILLET_DEFAULT_PROJECT
    calls = []

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        return (0, "cid\n", "") if args and args[0] == "run" else (0, "", "")

    async def fake_wait(sess):
        return True

    mgr = _isolate
    monkeypatch.setattr(mgr, "run_podman", fake_podman)
    monkeypatch.setattr(mgr, "_wait_ready", fake_wait)
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)
    r3 = client.post("/api/sessions", headers=_auth("pal@ua.edu"))
    assert r3.status_code == 200, r3.text
    joined = " ".join(next(c for c in calls if c[0] == "run"))
    assert "BILLET_DEFAULT_PROJECT=Demo" in joined


def test_default_project_inherited(client, _admin_env, _isolate, monkeypatch):
    directory, synced, users = _admin_env
    r = client.put("/api/admin/default-project",
                   headers=_auth("boss@example.com"), json={"project": "Demo"})
    assert r.status_code == 200
    assert "default_project: Demo" in users.read_text()
    body = client.get("/api/admin/users", headers=_auth("boss@example.com")).json()
    assert body["default_project"] == "Demo"

    # non-admin blocked
    assert client.put("/api/admin/default-project",
                      headers=_auth("demo@example.com"), json={"project": "X"}).status_code == 403

    # session start for a user with no explicit project inherits the default
    calls = []

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        return (0, "cid\n", "") if args and args[0] == "run" else (0, "", "")

    async def fake_wait(sess):
        return True

    mgr = _isolate
    monkeypatch.setattr(mgr, "run_podman", fake_podman)
    monkeypatch.setattr(mgr, "_wait_ready", fake_wait)
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)
    r3 = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r3.status_code == 200, r3.text
    joined = " ".join(next(c for c in calls if c[0] == "run"))
    assert "BILLET_DEFAULT_PROJECT=Demo" in joined


def test_admin_browse_and_path_default(client, _admin_env, _isolate, monkeypatch, tmp_path):
    directory, synced, users = _admin_env
    proj = tmp_path / "TeamParts"
    (proj / "sub").mkdir(parents=True)
    (proj / "Frame.step+").write_bytes(b"x")
    (proj / "notes.txt").write_bytes(b"x")

    # browse: admin only, lists dirs + CAD parts only
    assert client.get("/api/admin/browse", headers=_auth("demo@example.com"),
                      params={"path": str(proj)}).status_code == 403
    r = client.get("/api/admin/browse", headers=_auth("boss@example.com"),
                   params={"path": str(proj)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dirs"] == ["sub"] and body["parts"] == ["Frame.step+"]

    # path default accepted; bogus path rejected
    assert client.put("/api/admin/default-project", headers=_auth("boss@example.com"),
                      json={"project": str(proj), "doc": "Frame.step+"}).status_code == 200
    assert client.put("/api/admin/default-project", headers=_auth("boss@example.com"),
                      json={"project": "/nope/missing"}).status_code == 400

    # session start: ro bind-mount + basename project + open doc env
    calls = []

    async def fake_podman(args, timeout=120.0):
        calls.append(list(args))
        return (0, "cid\n", "") if args and args[0] == "run" else (0, "", "")

    async def fake_wait(sess):
        return True

    mgr = _isolate
    monkeypatch.setattr(mgr, "run_podman", fake_podman)
    monkeypatch.setattr(mgr, "_wait_ready", fake_wait)
    monkeypatch.setattr("gateway.sessions.mem_available_gib", lambda: 64.0)
    r3 = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r3.status_code == 200, r3.text
    run = next(c for c in calls if c[0] == "run")
    joined = " ".join(run)
    assert f"{proj}:/home/user/Projects/TeamParts:ro,z" in joined
    assert "BILLET_DEFAULT_PROJECT=TeamParts" in joined
    assert "BILLET_OPEN_DOC=Frame.step+" in joined


# ---------------------------------------------------------------- admin ops
def test_admin_ops_and_paste(client, _admin_env, _isolate, monkeypatch):
    mgr = _isolate
    # ops: admin-gated, reports host + limits even with no sessions
    assert client.get("/api/admin/ops", headers=_auth("demo@example.com")).status_code == 403
    async def fake_stats():
        return {"billet-sess-demo": {"cpu": "3.0%", "mem": "1GB / 5GB"}}
    monkeypatch.setattr(mgr, "container_stats", fake_stats)
    r = client.get("/api/admin/ops", headers=_auth("boss@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert body["limits"]["max_sessions"] > 0
    assert "mem_available_gib" in body["host"]

    # paste with no session -> 409
    r2 = client.post("/api/sessions/paste", headers=_auth("demo@example.com"),
                     json={"text": "hello"})
    assert r2.status_code == 409

    # seed a running session, then paste routes through manager.paste_text
    _seed_session(mgr, "demo@example.com")
    typed = []
    async def fake_paste(sess, text):
        typed.append(text)
        return True, "ok"
    monkeypatch.setattr(mgr, "paste_text", fake_paste)
    r3 = client.post("/api/sessions/paste", headers=_auth("demo@example.com"),
                     json={"text": "M6x1.0 tap drill 5.0mm"})
    assert r3.status_code == 200 and typed == ["M6x1.0 tap drill 5.0mm"]
    assert client.post("/api/sessions/paste", headers=_auth("demo@example.com"),
                       json={"text": ""}).status_code == 400
    assert client.post("/api/sessions/paste", headers=_auth("demo@example.com"),
                       json={"text": "x" * 20001}).status_code == 413

    # admin stop of that session
    sid = mgr.get_for_user("demo@example.com").sid
    stopped = []
    async def fake_stop(s, email=None):
        stopped.append(s)
        return mgr.get(s)
    monkeypatch.setattr(mgr, "stop", fake_stop)
    r4 = client.post(f"/api/admin/sessions/{sid}/stop", headers=_auth("boss@example.com"))
    assert r4.status_code == 200 and stopped == [sid]


def test_security_headers(client, _isolate):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_admin_patch_user(client, _admin_env):
    directory, synced, users = _admin_env
    # non-admin blocked
    assert client.patch("/api/admin/users/demo@example.com", headers=_auth("demo@example.com"),
                        json={"heavy": True}).status_code == 403
    # toggle heavy + role
    r = client.patch("/api/admin/users/demo@example.com", headers=_auth("boss@example.com"),
                     json={"heavy": True, "role": "admin", "display": "Demo Admin"})
    assert r.status_code == 200, r.text
    u = r.json()["user"]
    assert u["heavy"] is True and u["role"] == "admin" and u["display"] == "Demo Admin"
    # self-demotion blocked
    assert client.patch("/api/admin/users/boss@example.com", headers=_auth("boss@example.com"),
                        json={"role": "user"}).status_code == 400
    # bad role / bogus project / empty body
    assert client.patch("/api/admin/users/demo@example.com", headers=_auth("boss@example.com"),
                        json={"role": "supreme"}).status_code == 400
    assert client.patch("/api/admin/users/demo@example.com", headers=_auth("boss@example.com"),
                        json={"project": "/nope/missing"}).status_code == 400
    assert client.patch("/api/admin/users/demo@example.com", headers=_auth("boss@example.com"),
                        json={}).status_code == 400
    assert client.patch("/api/admin/users/ghost@example.com", headers=_auth("boss@example.com"),
                        json={"heavy": True}).status_code == 404


def test_admin_spectates_stream(client, _admin_env, _isolate):
    _seed_session(_isolate, "demo@example.com")
    # owner and admin reach the proxy (502: no upstream in tests); others 403
    assert client.get("/stream/livesid/", headers=_auth("demo@example.com")).status_code == 502
    assert client.get("/stream/livesid/", headers=_auth("boss@example.com")).status_code == 502
    assert client.get("/stream/livesid/", headers=_auth("alice@ua.edu")).status_code == 403


def test_admin_refuse_new_toggle(client, _admin_env, _isolate):
    mgr = _isolate
    flag = mgr.cfg.refuse_new_flag
    assert client.put("/api/admin/refuse-new", headers=_auth("demo@example.com"),
                      json={"on": True}).status_code == 403
    r = client.put("/api/admin/refuse-new", headers=_auth("boss@example.com"), json={"on": True})
    assert r.status_code == 200 and r.json()["refuse_new"] is True and flag.exists()
    # new session start now refused with 503
    r2 = client.post("/api/sessions", headers=_auth("demo@example.com"))
    assert r2.status_code == 503
    r3 = client.put("/api/admin/refuse-new", headers=_auth("boss@example.com"), json={"on": False})
    assert r3.status_code == 200 and r3.json()["refuse_new"] is False and not flag.exists()
    ops = client.get("/api/admin/ops", headers=_auth("boss@example.com")).json()
    assert ops["refuse_new"] is False and "units" in ops["host"]


# ------------------------------------------------------ shared admin terminal
@pytest.fixture
def _tmux_env(monkeypatch):
    """Isolated tmux socket + no-op omp; skip when tmux is unavailable."""
    import gateway.terminal as term

    if term._TMUX is None:
        pytest.skip("tmux not installed on host")
    monkeypatch.setattr(term, "_SOCKET", "billet-pytest")
    monkeypatch.setenv("BILLET_OMP_CMD", "true")
    # Start the tmux server directly (no systemd user scope) so the test is
    # hermetic on boxes without a user systemd bus.
    real_which = term.shutil.which
    monkeypatch.setattr(
        term.shutil, "which",
        lambda n: None if n == "systemd-run" else real_which(n),
    )
    term._run_tmux("kill-server")
    yield term
    term._run_tmux("kill-server")


def test_terminal_sessions_shared_lifecycle(client, _admin_env, _tmux_env):
    # admin-gated
    assert client.get("/api/admin/terminal/sessions",
                      headers=_auth("demo@example.com")).status_code == 403

    body = client.get("/api/admin/terminal/sessions",
                      headers=_auth("boss@example.com")).json()
    assert body["tmux"] is True and body["sessions"] == []

    # create a named shared session
    r = client.post("/api/admin/terminal/sessions",
                    headers=_auth("boss@example.com"), json={"name": "Work 1!"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "work-1"  # slugged

    # it now shows up for every admin (shared/saved with zero clients attached)
    sessions = client.get("/api/admin/terminal/sessions",
                          headers=_auth("boss@example.com")).json()["sessions"]
    names = {s["name"]: s for s in sessions}
    assert "work-1" in names and names["work-1"]["clients"] == 0

    # non-admins cannot create or kill
    assert client.post("/api/admin/terminal/sessions",
                       headers=_auth("demo@example.com"), json={}).status_code == 403
    assert client.delete("/api/admin/terminal/sessions/work-1",
                         headers=_auth("demo@example.com")).status_code == 403

    # auto-named create when no name given
    auto = client.post("/api/admin/terminal/sessions",
                       headers=_auth("boss@example.com"), json={}).json()["name"]
    assert auto and auto != "work-1"

    # explicit kill removes it
    r = client.delete("/api/admin/terminal/sessions/work-1",
                      headers=_auth("boss@example.com"))
    assert r.status_code == 200 and r.json()["ok"] is True
    left = {s["name"] for s in client.get("/api/admin/terminal/sessions",
            headers=_auth("boss@example.com")).json()["sessions"]}
    assert "work-1" not in left and auto in left

# --- Access-policy allowlist sync (cf_provision) ---------------------------
#
# The allowlist -> Cloudflare Access policy path is what decides whether a
# person ever receives a One-Time PIN: CF silently drops the OTP email for any
# address not in an Allow policy. A parser that drops or mangles an email =
# that user never gets a code. These lock the parser against the shapes real
# users.yaml files take (quoted values, flow style, mixed case, duplicates).

def _load_cf_provision():
    import importlib.util

    path = GATEWAY_DIR.parent / "deploy" / "cf_provision.py"
    spec = importlib.util.spec_from_file_location("cf_provision", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_users_yaml_emails_robust_parse(tmp_path, monkeypatch):
    cf = _load_cf_provision()
    users = tmp_path / "users.yaml"
    users.write_text(
        "users:\n"
        '  - email: "Quoted.User@gmail.com"\n'   # quoted + mixed case
        "  - email: plain@ua.edu\n"
        "  - email: PLAIN@ua.edu\n"               # duplicate (case-fold)
        "  - email: demo@example.com\n"           # dev-only, never synced
        "  - display: no-email-row\n"             # malformed row, ignored
    )
    monkeypatch.setenv("BILLET_WEB_USERS_FILE", str(users))
    assert cf._users_yaml_emails() == ["quoted.user@gmail.com", "plain@ua.edu"]


def test_users_yaml_emails_flow_style(tmp_path, monkeypatch):
    cf = _load_cf_provision()
    users = tmp_path / "users.yaml"
    users.write_text("users: [{email: brother@gmail.com, role: user}]\n")
    monkeypatch.setenv("BILLET_WEB_USERS_FILE", str(users))
    assert cf._users_yaml_emails() == ["brother@gmail.com"]


def test_include_list_never_quotes_email(tmp_path, monkeypatch):
    cf = _load_cf_provision()
    users = tmp_path / "users.yaml"
    users.write_text('users:\n  - email: "brother@gmail.com"\n')
    monkeypatch.setenv("BILLET_WEB_USERS_FILE", str(users))
    inc = cf._include_list(cf._users_yaml_emails())
    emails = [r["email"]["email"] for r in inc if "email" in r]
    # A stray-quote email can never match in CF Access -> silently no OTP.
    assert emails == ["brother@gmail.com"]
    assert all('"' not in e for e in emails)

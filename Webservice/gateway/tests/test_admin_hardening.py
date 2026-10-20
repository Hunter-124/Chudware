"""Admin-hardening tests — terminal kill switch + ownership, project-root
confinement for _valid_project/admin_browse, journal secret redaction, and
strict release-name validation.

Every test here fails if the vulnerability it guards is reintroduced (e.g. the
terminal answering while disabled, a browse escaping the allowlisted roots, a
JWT surviving admin_logs, or a quote surviving _safe_name).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth BEFORE importing the app, so X-Dev-User is accepted.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault(
    "CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test")
)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, authn, db  # noqa: E402
from gateway.app import _redact_secrets, _valid_project  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.releases import _safe_name  # noqa: E402
import gateway.terminal as terminal_mod  # noqa: E402

# Identities the fixture seeds into the account store; the middleware maps the
# X-Dev-User header to a row there and authorizes from it.
ADMIN = {"X-Dev-User": "admin@example.com"}
ADMIN_EMAIL = "admin@example.com"
OTHER_ADMIN = "other@example.com"


def _seed_admin(email: str, display: str) -> None:
    """Register, activate, and promote one account.

    accounts.py deliberately exposes no role setter — admin is granted through
    the admin API and never self-assigned — so the promotion is a direct row
    update here rather than a public call that does not (and should not) exist.
    """
    accounts.ensure_account(email, display=display)
    accounts.set_status(email, "active", decided_by="test")
    with db.tx() as cur:
        cur.execute("UPDATE accounts SET role = 'admin' WHERE email = %s", (email,))


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    """Seeded admin accounts + tmp data root; no real tmux."""
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    _seed_admin(ADMIN_EMAIL, "Test Admin")
    _seed_admin(OTHER_ADMIN, "Other Admin")
    return tmp_path


@pytest.fixture
def client(_isolate):
    with TestClient(app_mod.app) as c:
        yield c


# --- terminal kill switch ----------------------------------------------------
def test_terminal_rest_404s_when_disabled(client, monkeypatch):
    """terminal_enabled=False (the default) must 404 every terminal REST route,
    even for an authenticated admin."""
    monkeypatch.setattr(settings, "terminal_enabled", False)
    for url in (
        "/api/admin/terminal/sessions",
        "/api/admin/terminal/layout",
    ):
        r = client.get(url, headers=ADMIN)
        assert r.status_code == 404, (url, r.status_code, r.text)
    r = client.post("/api/admin/terminal/sessions", headers=ADMIN, json={})
    assert r.status_code == 404
    r = client.delete("/api/admin/terminal/sessions/whatever", headers=ADMIN)
    assert r.status_code == 404


def test_terminal_rest_ok_when_enabled(client, monkeypatch):
    """Same routes, only the setting flipped — proves the 404 above is the kill
    switch and not tmux/auth. (tmux stubbed absent; list works without it.)"""
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(terminal_mod, "_TMUX", None)
    r = client.get("/api/admin/terminal/sessions", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"tmux": False, "sessions": []}


# --- terminal session ownership / caps (pure helpers, no real tmux) ----------
def _fake_session(sid, label, owner, shared=False):
    return {
        "name": label,
        "id": sid,
        "label": label,
        "owner": owner,
        "shared": shared,
        "windows": 1,
        "clients": 0,
        "created": 1,
        "activity": 1,
    }


def test_can_access_defaults_deny_cross_admin():
    mine = _fake_session("id-a", "chud-1", ADMIN_EMAIL)
    shared = _fake_session("id-b", "chud-2", OTHER_ADMIN, shared=True)
    legacy = _fake_session("legacy-9", "legacy-9", "")  # pre-ownership session
    assert terminal_mod._can_access(mine, ADMIN_EMAIL)
    assert not terminal_mod._can_access(mine, OTHER_ADMIN)
    assert terminal_mod._can_access(shared, ADMIN_EMAIL)
    assert terminal_mod._can_access(legacy, ADMIN_EMAIL)


def test_resolve_session_rejects_cross_admin_by_id_or_label(client, monkeypatch):
    """Another admin's session is unresolvable — and therefore unattachable /
    unkillable — whether addressed by its opaque id or its guessable label."""
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [_fake_session("secretid123", "chud-1", OTHER_ADMIN)],
    )
    assert terminal_mod._resolve_session("secretid123", ADMIN_EMAIL) is None
    assert terminal_mod._resolve_session("chud-1", ADMIN_EMAIL) is None
    # ...while the owner and shared opt-in still resolve.
    assert terminal_mod._resolve_session("chud-1", OTHER_ADMIN)["id"] == "secretid123"
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [_fake_session("secretid123", "chud-1", OTHER_ADMIN, shared=True)],
    )
    assert terminal_mod._resolve_session("chud-1", ADMIN_EMAIL)["id"] == "secretid123"


def test_mint_session_id_is_unguessable_and_tmux_safe():
    ids = {terminal_mod._mint_session_id() for _ in range(64)}
    assert len(ids) == 64  # no collisions
    for sid in ids:
        assert len(sid) >= 20  # 128 bits of entropy, urlsafe-encoded
        # tmux forbids '.' and ':' in session names; token_urlsafe never emits them.
        assert "." not in sid and ":" not in sid


def test_session_list_filtered_per_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(terminal_mod, "_TMUX", "/bin/tmux")
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [
            _fake_session("id-mine", "chud-1", ADMIN_EMAIL),
            _fake_session("id-theirs", "chud-2", OTHER_ADMIN),
            _fake_session("id-shared", "chud-3", OTHER_ADMIN, shared=True),
        ],
    )
    r = client.get("/api/admin/terminal/sessions", headers=ADMIN)
    assert r.status_code == 200, r.text
    ids = {s["id"] for s in r.json()["sessions"]}
    assert ids == {"id-mine", "id-shared"}


def test_create_enforces_session_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(settings, "terminal_max_sessions", 2)
    monkeypatch.setattr(terminal_mod, "_TMUX", "/bin/tmux")
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [
            _fake_session("id-mine", "chud-1", ADMIN_EMAIL),
            _fake_session("id-theirs", "chud-2", OTHER_ADMIN),
        ],
    )
    r = client.post("/api/admin/terminal/sessions", headers=ADMIN, json={})
    assert r.status_code == 409, r.text


def test_delete_cross_admin_is_404(client, monkeypatch):
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(terminal_mod, "_TMUX", "/bin/tmux")
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [_fake_session("secretid123", "chud-1", OTHER_ADMIN)],
    )
    for name in ("secretid123", "chud-1"):
        r = client.delete(f"/api/admin/terminal/sessions/{name}", headers=ADMIN)
        assert r.status_code == 404, (name, r.status_code, r.text)


# --- _valid_project / admin_browse confinement -------------------------------
@pytest.fixture
def _roots(tmp_path, monkeypatch):
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    (root_a / "proj").mkdir(parents=True)
    root_b.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(settings, "project_roots_raw", f"{root_a}:{root_b}")
    return root_a, root_b


def test_valid_project_accepts_path_inside_root(_roots):
    root_a, _ = _roots
    assert _valid_project(str(root_a / "proj")) is True
    assert _valid_project(str(root_a)) is True  # the root itself


def test_valid_project_rejects_etc_and_outside(_roots, tmp_path):
    assert _valid_project("/etc") is False
    assert _valid_project("/") is False
    assert _valid_project(str(tmp_path / "outside")) is False


def test_valid_project_rejects_dotdot(_roots):
    root_a, _ = _roots
    sneaky = str(root_a / ".." / "rootA" / "proj")  # resolves inside the root
    assert ".." in sneaky.split("/")
    assert _valid_project(sneaky) is False


def test_valid_project_still_accepts_plm_names(_roots):
    assert _valid_project("team-project") is True
    assert _valid_project("bad name!") is False


def test_admin_browse_rejects_out_of_root(client, _roots, tmp_path):
    r = client.get("/api/admin/browse", params={"path": "/etc"}, headers=ADMIN)
    assert r.status_code == 400, r.text
    r = client.get(
        "/api/admin/browse", params={"path": str(tmp_path / "outside")}, headers=ADMIN
    )
    assert r.status_code == 400, r.text


def test_admin_browse_lists_inside_root(client, _roots):
    root_a, _ = _roots
    r = client.get("/api/admin/browse", params={"path": str(root_a)}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert "proj" in r.json()["dirs"]
    # Parent (outside every root) must not be offered as a navigable link.
    assert r.json()["parent"] is None


# --- journal redaction -------------------------------------------------------
def test_redact_secrets_strips_credentials():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6y"
    )
    raw = (
        f"2026-07-28 INFO auth ok Authorization: Bearer {jwt}\n"
        "2026-07-28 INFO tunnel cred cfat_Vm90aGVyX3Rva2VuXzEyMzQ1 installed\n"
        "2026-07-28 INFO env API_KEY=sk-live-abcdef123456 loaded\n"
        "2026-07-28 INFO TUNNEL_TOKEN=abcdef.ghijkl rotated\n"
    )
    out = _redact_secrets(raw)
    assert jwt not in out
    assert "cfat_Vm90aGVyX3Rva2VuXzEyMzQ1" not in out
    assert "sk-live-abcdef123456" not in out
    assert "abcdef.ghijkl" not in out
    assert "API_KEY=[REDACTED]" in out
    assert "TUNNEL_TOKEN=[REDACTED]" in out


def test_redact_secrets_strips_auth_query_values():
    raw = "GET /api/auth/callback?code=provider-code&state=csrf-state&token=bearer-token HTTP/1.1"
    out = _redact_secrets(raw)
    assert "provider-code" not in out
    assert "csrf-state" not in out
    assert "bearer-token" not in out
    assert out.count("[REDACTED]") == 3


def test_redact_secrets_leaves_ordinary_lines_intact():
    line = (
        "2026-07-28T10:00:00 INFO chudware-web: gateway up "
        "data_root=/home/liveuser/chudware-web-data max_sessions=3 dev_mode=False"
    )
    assert _redact_secrets(line) == line


# --- release names -----------------------------------------------------------
def test_safe_name_rejects_header_and_path_tricks():
    for bad in (
        'bad";name.zip',
        "a/b",
        ".hidden",
        "a\\b",
        "semi;colon.zip",
        "new\nline.zip",
        "..",
        "",
    ):
        with pytest.raises(HTTPException) as exc:
            _safe_name(bad)
        assert exc.value.status_code == 400, bad


def test_safe_name_accepts_normal_release():
    assert _safe_name("Chudware-1.2.3.tar.gz") == "Chudware-1.2.3.tar.gz"


@pytest.fixture
def deployment_browser(client, monkeypatch):
    """Use a persisted browser session, not dev headers or replaced authorization."""
    monkeypatch.setattr(settings, "dev_mode", False)
    client.cookies.set(authn.SESSION_COOKIE, authn.create_session(ADMIN_EMAIL))
    identity = client.get("/api/me")
    assert identity.status_code == 200, identity.text
    assert identity.json()["role"] == "admin"
    return client


@pytest.mark.parametrize("environment", ["prod", "staging"])
@pytest.mark.parametrize("role", ["admin", "user"])
@pytest.mark.parametrize("endpoint", ["rebuild", "redeploy"])
def test_browser_sessions_cannot_launch_deployment(
        deployment_browser, monkeypatch, tmp_path, environment, role, endpoint):
    monkeypatch.setattr(settings, "deploy_environment", environment)
    # If a launch route regresses, it must fail this test without finding a live checkout.
    monkeypatch.setenv("CHUDWARE_REPO_DIR", str(tmp_path / "absent-checkout"))
    accounts.set_role(ADMIN_EMAIL, role)
    identity = deployment_browser.get("/api/me")
    assert identity.status_code == 200, identity.text
    assert identity.json()["role"] == role
    response = deployment_browser.post(
        f"/api/admin/deploy/{endpoint}", json={"targets": ["wasm", "engine"]})
    assert response.status_code == 404, response.text
    assert not (settings.logs_dir / "deploy-status.json").exists()
    assert not (settings.logs_dir / "deploy.lock").exists()
    assert not (settings.logs_dir / "deploy-launch.log").exists()


def test_deployment_monitoring_reads_operator_snapshot_and_update_log_without_mutation(
        deployment_browser):
    logs = settings.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    # An old running snapshot is historical evidence, never authority for a GET to heal it.
    snapshot = {"running": True, "kind": "deploy-prod", "started_at": 1,
                "last": {"kind": "deploy-prod", "rc": 0, "finished_at": 1, "release": "v9.8.7"}}
    (logs / "deploy-status.json").write_text(json.dumps(snapshot) + "\n")
    lines = [f"operator deployment line {index}" for index in range(30)]
    (logs / "update.log").write_text("\n".join(lines) + "\n")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in logs.iterdir()}

    status = deployment_browser.get("/api/admin/deploy/status")
    assert status.status_code == 200, status.text
    assert status.json() == {"ok": True, **snapshot}
    log = deployment_browser.get("/api/admin/deploy/log", params={"lines": 10})
    assert log.status_code == 200, log.text
    assert log.text == "\n".join(lines[-10:]) + "\n"
    for endpoint in ("status", "log"):
        response = deployment_browser.post(f"/api/admin/deploy/{endpoint}", json={})
        assert response.status_code == 405, response.text
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in logs.iterdir()} == before


def test_deployment_monitoring_does_not_create_operator_state(
        deployment_browser, monkeypatch, tmp_path):
    root = tmp_path / "absent-operator-data"
    monkeypatch.setattr(settings, "data_root", root)
    status = deployment_browser.get("/api/admin/deploy/status")
    assert status.status_code == 200, status.text
    assert status.json() == {"ok": True, "running": False, "kind": None,
                             "started_at": None, "last": None}
    log = deployment_browser.get("/api/admin/deploy/log")
    assert log.status_code == 200 and log.text == ""
    assert not root.exists()


@pytest.mark.parametrize("endpoint", ["status", "log"])
def test_deployment_monitoring_requires_a_current_admin_session(deployment_browser, endpoint):
    accounts.set_role(ADMIN_EMAIL, "user")
    assert deployment_browser.get(f"/api/admin/deploy/{endpoint}").status_code == 403
    authn.revoke_session(deployment_browser.cookies[authn.SESSION_COOKIE])
    assert deployment_browser.get(f"/api/admin/deploy/{endpoint}").status_code == 401


def test_staging_admin_receives_only_staging_journal(client, monkeypatch, tmp_path):
    import sys
    logs = tmp_path / "journals"
    logs.mkdir()
    (logs / "chudware-gateway").write_text("private production journal marker")
    (logs / "chudware-gateway-staging").write_text("staging journal marker")
    binary = tmp_path / "bin"
    binary.mkdir()
    journal = binary / "journalctl"
    journal.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"print((Path({str(logs)!r}) / sys.argv[sys.argv.index('-u') + 1]).read_text())\n"
    )
    journal.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binary}:{os.defpath}")
    monkeypatch.setattr(settings, "deploy_environment", "staging")
    response = client.get("/api/admin/logs", headers=ADMIN)
    assert response.status_code == 200
    assert "staging journal marker" in response.text
    assert "private production journal marker" not in response.text

"""Admin-hardening tests — terminal kill switch + ownership, project-root
confinement for _valid_project/admin_browse, journal secret redaction, and
strict release-name validation.

Every test here fails if the vulnerability it guards is reintroduced (e.g. the
terminal answering while disabled, a browse escaping the allowlisted roots, a
JWT surviving admin_logs, or a quote surviving _safe_name).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth BEFORE importing the app, so X-Dev-User is accepted.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, db  # noqa: E402
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
    mine = _fake_session("id-a", "omp-1", ADMIN_EMAIL)
    shared = _fake_session("id-b", "omp-2", OTHER_ADMIN, shared=True)
    legacy = _fake_session("omp-9", "omp-9", "")  # pre-ownership session
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
        lambda: [_fake_session("secretid123", "omp-1", OTHER_ADMIN)],
    )
    assert terminal_mod._resolve_session("secretid123", ADMIN_EMAIL) is None
    assert terminal_mod._resolve_session("omp-1", ADMIN_EMAIL) is None
    # ...while the owner and shared opt-in still resolve.
    assert terminal_mod._resolve_session("omp-1", OTHER_ADMIN)["id"] == "secretid123"
    monkeypatch.setattr(
        terminal_mod,
        "_list_sessions",
        lambda: [_fake_session("secretid123", "omp-1", OTHER_ADMIN, shared=True)],
    )
    assert terminal_mod._resolve_session("omp-1", ADMIN_EMAIL)["id"] == "secretid123"


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
            _fake_session("id-mine", "omp-1", ADMIN_EMAIL),
            _fake_session("id-theirs", "omp-2", OTHER_ADMIN),
            _fake_session("id-shared", "omp-3", OTHER_ADMIN, shared=True),
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
            _fake_session("id-mine", "omp-1", ADMIN_EMAIL),
            _fake_session("id-theirs", "omp-2", OTHER_ADMIN),
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
        lambda: [_fake_session("secretid123", "omp-1", OTHER_ADMIN)],
    )
    for name in ("secretid123", "omp-1"):
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
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6y"
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
    for bad in ('bad";name.zip', "a/b", ".hidden", "a\\b", "semi;colon.zip",
                "new\nline.zip", "..", ""):
        with pytest.raises(HTTPException) as exc:
            _safe_name(bad)
        assert exc.value.status_code == 400, bad


def test_safe_name_accepts_normal_release():
    assert _safe_name("Chudware-1.2.3.tar.gz") == "Chudware-1.2.3.tar.gz"

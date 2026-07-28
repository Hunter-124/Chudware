"""Per-user session limits + engine-bridge hardening tests (hermetic).

Covers the multi-tenant audit remediations in sessions.py / engine_bridge.py:
sid entropy, per-user seat caps, start-churn rate limiting, absolute session
lifetime, provider-key gating for container env, owner-facing payload hygiene,
/engine session-ownership, and inbound frame caps. Podman and the webd daemon
are faked — no containers, no live daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway import sessions  # noqa: E402
from gateway.auth import User  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.limits import session_starts  # noqa: E402
from gateway.sessions import Session, SessionBusy, SessionManager, manager  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}


def _user(email: str, display: str = "Demo User") -> User:
    return User(email=email, display=display, role="user",
                heavy=False, project="", doc="")


def _session(sid: str, email: str, **kw) -> Session:
    args = dict(
        sid=sid, email=email, display=email.split("@")[0],
        slug=email.split("@")[0], container=f"chudware-sess-{sid[:8]}",
        host_port=8460, heavy=False, status="ready",
    )
    args.update(kw)
    return Session(**args)


@pytest.fixture()
def mgr(monkeypatch, tmp_path):
    """A SessionManager with podman + healthcheck stubbed out."""
    m = SessionManager()
    session_starts._hits.clear()
    # keep the host-capacity guards out of the way of the per-user assertions
    monkeypatch.setattr(
        type(settings), "refuse_new_flag",
        property(lambda self: tmp_path / "REFUSE_NEW"),
    )
    monkeypatch.setattr(sessions, "mem_available_gib", lambda: None)

    async def fake_run_podman(args, *, timeout=120.0):
        # Non-empty stdout doubles as "container alive" for _container_running.
        return (0, "abc123\n", "")

    async def fake_podman_run(sess, user, home):
        return "abc123"

    async def fake_wait_ready(sess):
        return True

    m.run_podman = fake_run_podman
    m._podman_run = fake_podman_run
    m._wait_ready = fake_wait_ready
    return m


class FakeDaemon:
    """Minimal newline-JSON unix-socket daemon: records raw frames, answers ping."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self.lines: list[bytes] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._started.wait(5), "fake daemon failed to start"

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def boot() -> None:
            await asyncio.start_unix_server(self._client, path=str(self.sock_path))
            self._started.set()

        loop.run_until_complete(boot())
        loop.run_forever()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                self.lines.append(raw)
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("method") == "ping":
                    writer.write(
                        (json.dumps({"id": msg.get("id"), "result": {"pong": True}}) + "\n").encode()
                    )
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    sock = tmp_path / "webd.sock"
    fake = FakeDaemon(sock)
    fake.start()
    monkeypatch.setattr(settings, "webd_socket", sock)
    monkeypatch.setattr(settings, "dev_mode", True)
    yield fake
    fake.stop()


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


@pytest.fixture()
def live_session():
    """Seed a ready seat for the dev user; /engine requires one of the caller's own."""
    sess = _session("engseat" + "s" * 24, "demo@example.com", host_port=8461)
    manager._sessions[sess.sid] = sess
    manager._by_email[sess.email] = sess.sid
    manager._ports_in_use.add(sess.host_port)
    yield sess
    manager._sessions.pop(sess.sid, None)
    manager._by_email.pop(sess.email, None)
    manager._ports_in_use.discard(sess.host_port)


def _env_pairs(args: list[str]) -> dict[str, str]:
    """Pull KEY=VALUE pairs out of podman `-e` argv."""
    env: dict[str, str] = {}
    for i, a in enumerate(args):
        if a == "-e" and i + 1 < len(args):
            k, _, v = args[i + 1].partition("=")
            env[k] = v
    return env


class TestSessionIds:
    def test_sids_have_full_entropy(self):
        sid1 = sessions._new_sid()
        sid2 = sessions._new_sid()
        assert len(sid1) >= 22
        assert len(sid2) >= 22
        assert sid1 != sid2


class TestPerUserCaps:
    def test_second_seat_for_same_email_rejected(self, mgr, monkeypatch):
        """An account already holding an active seat may not take another."""
        monkeypatch.setattr(settings, "max_sessions_per_user", 1)
        email = "cap@example.com"
        # An active record for this account that the email->sid map no longer
        # points at (e.g. an adopted stale container): the reconnect path can't
        # hand it back, so the per-user cap is what must refuse a fresh seat.
        mgr._sessions["orphan" + "o" * 24] = _session(
            "orphan" + "o" * 24, email, host_port=8462
        )
        with pytest.raises(SessionBusy, match="per-user"):
            asyncio.run(mgr.start(_user(email)))

    def test_reconnect_still_returns_existing_session(self, mgr, monkeypatch):
        """The cap must not break the single-tenant reconnect-reuse flow."""
        monkeypatch.setattr(settings, "max_sessions_per_user", 1)
        user = _user("reconnect@example.com")
        first = asyncio.run(mgr.start(user))
        assert first.status == "ready"
        second = asyncio.run(mgr.start(user))
        assert second.sid == first.sid

    def test_start_churn_rate_limited(self, mgr, monkeypatch):
        """Repeated start/stop cycles are throttled per verified identity."""
        monkeypatch.setattr(session_starts, "limit", 2)
        session_starts._hits.clear()
        user = _user("churn@example.com")
        for _ in range(2):
            asyncio.run(mgr.start(user))
            asyncio.run(mgr.stop_for_user(user.email))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(mgr.start(user))
        assert exc.value.status_code == 429


class TestLifetimeReaper:
    def test_reaper_evicts_past_max_lifetime_even_when_active(self, mgr, monkeypatch):
        """A seat held warm by constant traffic still dies at the hard ceiling."""
        monkeypatch.setattr(settings, "max_session_lifetime_sec", 3600)
        monkeypatch.setattr(settings, "idle_timeout_sec", 30 * 60)
        now = time.time()
        old = _session("oldseat" + "o" * 24, "old@example.com", host_port=8463,
                       created_at=now - 7200, last_activity=now)
        fresh = _session("newseat" + "n" * 24, "new@example.com", host_port=8464,
                         created_at=now, last_activity=now)
        mgr._sessions[old.sid] = old
        mgr._sessions[fresh.sid] = fresh
        stopped = asyncio.run(mgr.reap_idle())
        assert old.sid in stopped
        assert mgr.get(old.sid) is None
        # An active session inside both limits is untouched.
        assert fresh.sid not in stopped
        assert mgr.get(fresh.sid) is not None


class TestProviderKeyGating:
    @pytest.fixture()
    def container_env(self, monkeypatch, tmp_path):
        """Run _podman_run with podman captured; return the container env dict."""
        captured: dict[str, list[str]] = {}
        m = SessionManager()

        async def fake_run_podman(args, *, timeout=120.0):
            captured["args"] = list(args)
            return (0, "abc123\n", "")

        m.run_podman = fake_run_podman
        monkeypatch.setattr(
            settings, "ai_provider_vars",
            lambda: {"GEMINI_API_KEY": "gem-secret", "OPENROUTER_API_KEY": "or-secret"},
        )
        sess = _session("envseat" + "e" * 24, "keys@example.com",
                        host_port=8465, status="starting")

        async def go() -> dict[str, str]:
            await m._podman_run(sess, _user("keys@example.com", "Keys"), tmp_path)
            return _env_pairs(captured["args"])

        return go

    def test_provider_keys_withheld_by_default(self, container_env, monkeypatch):
        monkeypatch.setattr(sessions, "_SHARE_PROVIDER_KEYS", False)
        env = asyncio.run(container_env())
        assert "GEMINI_API_KEY" not in env
        assert "OPENROUTER_API_KEY" not in env
        # Non-secret session config still flows through.
        assert env["CHUDWARE_USER_EMAIL"] == "keys@example.com"

    def test_provider_keys_shared_with_explicit_optin(self, container_env, monkeypatch):
        monkeypatch.setattr(sessions, "_SHARE_PROVIDER_KEYS", True)
        env = asyncio.run(container_env())
        assert env["GEMINI_API_KEY"] == "gem-secret"
        assert env["OPENROUTER_API_KEY"] == "or-secret"


class TestPortalPayload:
    def test_owner_payload_hides_routing_internals(self, mgr):
        sess = _session("payseat" + "p" * 24, "owner@example.com", host_port=8466)
        payload = mgr.portal_payload(sess)
        assert payload["sid"] == sess.sid
        assert "host_port" not in payload
        assert "container" not in payload

    def test_internals_still_available_for_admin(self, mgr):
        sess = _session("admseat" + "a" * 24, "owner@example.com", host_port=8467)
        payload = mgr.portal_payload(sess, include_internals=True)
        assert payload["host_port"] == sess.host_port
        assert payload["container"] == sess.container


class TestEngineBridgeHardening:
    def test_engine_opens_without_a_container_seat(self, daemon, client):
        """/app is the browser-native client and never has a podman seat.

        An earlier hardening pass required the caller to own a live session
        here, which took the whole WASM client offline in production
        ("Engine daemon unreachable — retrying", 2026-07-28). Isolation is
        enforced by _inject_identity and path_violation instead, so an
        authorized caller with NO seat must still be able to open the bridge.
        """
        assert manager.get_for_user("demo@example.com") is None
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            ws.send_text(json.dumps({"id": 1, "method": "ping"}))
            assert json.loads(ws.receive_text())["result"]["pong"] is True

    def test_engine_bridges_capped_per_account(self, daemon, client, monkeypatch):
        """One account must not fan out across the shared daemon."""
        monkeypatch.setattr(settings, "max_engine_bridges_per_user", 1)
        with client.websocket_connect("/engine", headers=DEV_HEADERS):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/engine", headers=DEV_HEADERS):
                    pass
            assert exc.value.code == 4429
        # Closing the first bridge frees the slot again.
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            ws.send_text(json.dumps({"id": 1, "method": "ping"}))
            assert json.loads(ws.receive_text())["result"]["pong"] is True

    def test_oversized_frame_rejected_not_forwarded(
        self, daemon, client, live_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "ws_max_message_bytes", 64)
        big = json.dumps({"id": 9, "method": "doc_new", "params": {"blob": "x" * 512}})
        assert len(big.encode()) > 64
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            # A small frame is forwarded and answered — the bridge is up.
            ws.send_text(json.dumps({"id": 1, "method": "ping"}))
            resp = json.loads(ws.receive_text())
            assert resp["result"]["pong"] is True
            # The oversized frame must close the socket with a policy code.
            ws.send_text(big)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1009
        time.sleep(0.2)  # let the daemon drain anything wrongly forwarded
        assert all(b"doc_new" not in line for line in daemon.lines)
        assert any(b"ping" in line for line in daemon.lines)

    def test_small_frames_still_forwarded(self, daemon, client, live_session, monkeypatch):
        """The cap must not break normal traffic (default 4 MiB limit)."""
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            ws.send_text(json.dumps({"id": 1, "method": "ping"}))
            resp = json.loads(ws.receive_text())
            assert resp["result"]["pong"] is True

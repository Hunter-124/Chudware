"""Engine bridge + workspace API tests (hermetic — fake daemon on a unix socket)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway.config import Settings, settings  # noqa: E402
from gateway.limits import user_slug  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}
ADMIN_HEADERS = {"X-Dev-User": "haedmonds69@gmail.com"}

PROVIDER_MANAGER_CALLS = (
    ("agent_provider_list", {}),
    ("agent_provider_state", {"provider": "anthropic"}),
    ("agent_provider_validate_key", {"provider": "openai", "apiKey": "sk-browser-secret"}),
    ("agent_provider_set_key", {"provider": "openai", "apiKey": "sk-browser-secret"}),
    ("agent_provider_remove_key", {"provider": "openai"}),
    ("agent_provider_begin_oauth", {"provider": "anthropic"}),
    ("agent_provider_oauth_poll", {"handle": "opaque-handle"}),
    ("agent_provider_oauth_manual", {"handle": "opaque-handle", "code": "device-code"}),
    ("agent_provider_oauth_cancel", {"handle": "opaque-handle"}),
    ("agent_provider_logout", {"provider": "anthropic"}),
    ("agent_provider_logo", {"domain": "anthropic.com", "size": 64}),
)


class FakeDaemon:
    """Newline-JSON-RPC unix-socket server mimicking chudware-hostd for the bridge.

    Records every hello it sees (identity-injection assertions) and broadcasts
    doc.* event lines to every connection like DocRegistry fanout does.
    """

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self.hellos: list[dict] = []
        self.calls: list[dict] = []  # every parsed request (method + params), for RPC-rewrite assertions
        self._writers: list[asyncio.StreamWriter] = []
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
        self._writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                msg = json.loads(raw)
                mid, method = msg.get("id"), msg.get("method")
                params = msg.get("params") or {}
                self.calls.append({"method": method, "params": params})
                if method == "hello":
                    self.hellos.append(params)
                    result = {"proto": 1, "state": "ready"}
                elif method == "ping":
                    result = {"pong": True}
                elif method == "doc_new":
                    result = {"doc_id": "d1", "revision": 0}
                    await self._broadcast({"event": "doc.opened", "doc_id": "d1"})
                elif method == "doc_apply_ops":
                    result = {"ok": True, "revision": 1}
                    await self._broadcast(
                        {"event": "doc.changed", "doc_id": "d1", "revision": 1}
                    )
                else:
                    result = {"ok": True}
                writer.write((json.dumps({"id": mid, "result": result}) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    async def _broadcast(self, event: dict) -> None:
        line = (json.dumps(event) + "\n").encode()
        for w in list(self._writers):
            try:
                w.write(line)
                await w.drain()
            except ConnectionError:
                pass


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
def workspace(tmp_path, monkeypatch):
    # /workspace/* is per-account (tenant isolation): the API operates on the
    # caller's own tree under <workspace_root>/users/<slug>, never the shared
    # root itself, so seed the caller's tree and hand it to the tests.
    root = tmp_path / "CAD_Build"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_root", root)
    monkeypatch.setattr(settings, "dev_mode", True)
    user_root = settings.user_workspace_root(user_slug("demo@example.com"))
    (user_root / "parts").mkdir(parents=True)
    (user_root / "parts" / "bracket.step+").write_bytes(b"fake step plus")
    return user_root


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


def _rpc(ws, id_, method, params=None) -> dict:
    ws.send_text(json.dumps({"id": id_, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.receive_text())
        if msg.get("id") == id_:
            return msg


class TestEngineBridge:
    @pytest.fixture(autouse=True)
    def _live_seats(self):
        """/engine now requires a live seat of the caller's own (ownership audit fix).

        The tracked users.yaml template grants nobody access, so the admin test
        identity is injected into the live directory for the test's duration.
        """
        from gateway.auth import User
        from gateway.sessions import Session, manager

        admin = User(email="haedmonds69@gmail.com", display="Admin", role="admin",
                     heavy=False, project="", doc="")
        prev_entry = app_mod.user_directory._by_email.get(admin.email)
        app_mod.user_directory._by_email[admin.email] = admin

        seeded = []
        for i, email in enumerate(("demo@example.com", "haedmonds69@gmail.com")):
            sess = Session(
                sid=f"ebseat{i}" + "s" * 24,
                email=email,
                display=email.split("@")[0],
                slug=email.split("@")[0],
                container=f"chudware-sess-eb{i}",
                host_port=8490 + i,
                heavy=False,
                status="ready",
            )
            manager._sessions[sess.sid] = sess
            manager._by_email[email] = sess.sid
            manager._ports_in_use.add(sess.host_port)
            seeded.append(sess)
        yield
        for sess in seeded:
            manager._sessions.pop(sess.sid, None)
            manager._by_email.pop(sess.email, None)
            manager._ports_in_use.discard(sess.host_port)
        if prev_entry is None:
            app_mod.user_directory._by_email.pop(admin.email, None)
        else:
            app_mod.user_directory._by_email[admin.email] = prev_entry

    def test_hello_identity_injected(self, daemon, client):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            resp = _rpc(ws, 1, "hello", {"client": "agent", "pid": 42, "proto": 1})
            assert resp["result"]["state"] == "ready"
        assert len(daemon.hellos) == 1
        hello = daemon.hellos[0]
        # Server-asserted: kind forced to gui, user attached from the gateway auth.
        assert hello["client"] == "gui"
        assert hello["user"] == {"id": "demo@example.com", "display": "Demo User"}
        assert hello["pid"] == 42  # untouched passthrough

    def test_rpc_roundtrip_and_event_fanout(self, daemon, client):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws1:
            _rpc(ws1, 1, "hello")
            with client.websocket_connect("/engine", headers=ADMIN_HEADERS) as ws2:
                _rpc(ws2, 1, "hello")
                assert _rpc(ws1, 2, "ping")["result"]["pong"] is True
                resp = _rpc(ws1, 3, "doc_new", {"name": "Part1"})
                assert resp["result"]["doc_id"] == "d1"
                resp = _rpc(ws1, 4, "doc_apply_ops", {"doc_id": "d1", "ops": []})
                assert resp["result"]["revision"] == 1
                # ws2 sees the daemon's fanout events (id-less lines).
                events = [json.loads(ws2.receive_text())["event"] for _ in range(2)]
                assert events == ["doc.opened", "doc.changed"]

    def test_unauthenticated_socket_rejected(self, daemon, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/engine"):
                pass
        assert exc.value.code == 4401

    def test_daemon_down_closes_cleanly(self, tmp_path, monkeypatch, client):
        monkeypatch.setattr(settings, "webd_socket", tmp_path / "absent.sock")
        monkeypatch.setattr(settings, "dev_mode", True)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
                ws.receive_text()
        assert exc.value.code == 1013

    def test_active_bridge_count_tracked(self, daemon, client):
        from gateway import engine_bridge

        assert engine_bridge.active_bridge_count() == 0
        assert client.get("/healthz", headers=DEV_HEADERS).json()["engine_bridges"] == 0
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            assert engine_bridge.active_bridge_count() == 1
            assert client.get("/healthz", headers=DEV_HEADERS).json()["engine_bridges"] == 1
        # Closing the socket must decrement — the deploy idle-wait relies on this
        # returning to 0, else it would defer restarts forever.
        assert engine_bridge.active_bridge_count() == 0

    def test_bridge_limit_must_keep_web_client_available(self):
        with pytest.raises(
            ValueError,
            match="CHUDWARE_WEB_MAX_ENGINE_BRIDGES_PER_USER must be at least 1",
        ):
            Settings(max_engine_bridges_per_user=0)

    def test_concurrent_handshakes_reserve_before_accept(
        self, daemon, client, monkeypatch
    ):
        """A barrier after the first cap check must not let a second handshake through."""
        from gateway import engine_bridge
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setattr(settings, "max_engine_bridges_per_user", 1)
        original_accept = engine_bridge.WebSocket.accept
        accept_entered = threading.Event()
        release_accept = threading.Event()
        first_opened = threading.Event()
        release_first = threading.Event()
        choose_first = threading.Lock()
        block_next = True
        failures: list[BaseException] = []

        async def barrier_accept(socket, *args, **kwargs):
            nonlocal block_next
            with choose_first:
                should_block = block_next
                block_next = False
            if should_block:
                accept_entered.set()
                released = await asyncio.to_thread(release_accept.wait, 5)
                if not released:
                    raise AssertionError("handshake barrier timed out")
            return await original_accept(socket, *args, **kwargs)

        monkeypatch.setattr(engine_bridge.WebSocket, "accept", barrier_accept)

        def hold_first_connection() -> None:
            try:
                with client.websocket_connect("/engine", headers=DEV_HEADERS):
                    first_opened.set()
                    if not release_first.wait(5):
                        raise AssertionError("first connection release timed out")
            except BaseException as exc:  # pragma: no cover - asserted in parent thread
                failures.append(exc)

        first = threading.Thread(target=hold_first_connection, daemon=True)
        first.start()
        assert accept_entered.wait(5), "first handshake never reached the barrier"

        rejected_code = None
        try:
            with client.websocket_connect("/engine", headers=DEV_HEADERS):
                pass
        except WebSocketDisconnect as exc:
            rejected_code = exc.code
        finally:
            release_accept.set()
            opened = first_opened.wait(5)
            release_first.set()
            first.join(timeout=5)
        assert opened, "reserved handshake never completed"

        assert not first.is_alive()
        assert failures == []
        assert rejected_code == 4429
        assert engine_bridge.active_bridge_count() == 0

    def test_connect_failure_releases_reserved_slot(
        self, daemon, client, monkeypatch
    ):
        from gateway import engine_bridge
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setattr(settings, "max_engine_bridges_per_user", 1)
        original_connect = engine_bridge.asyncio.open_unix_connection
        attempts = 0

        async def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic connect failure")
            return await original_connect(*args, **kwargs)

        monkeypatch.setattr(engine_bridge.asyncio, "open_unix_connection", fail_once)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
                ws.receive_text()
        assert exc.value.code == 1013
        assert engine_bridge.active_bridge_count() == 0
        assert "demo@example.com" not in engine_bridge._bridge_owners

        # The failed reservation was rolled back, so the cap of one admits a retry.
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(ws, 1, "ping")["result"]["pong"] is True
        assert engine_bridge.active_bridge_count() == 0

    def test_agent_turn_keeps_provider_strips_secrets(self, daemon, client):
        """Web agent turns may select provider/model; credentials stay server-side.

        The gateway forwards provider + model so hostd can resolve host config keys
        by name, but strips api_key / base_url / fallback secrets from the browser.
        """
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            # Client pins provider/model selection + a browser-side secret; only
            # the secret must be dropped before hostd sees the turn.
            _rpc(ws, 2, "agent_ask_v2", {"question": "hi", "provider": "gemini",
                                         "model": "gemini-2.5-flash", "api_key": "sk-web",
                                         "base_url": "https://evil.example",
                                         "fallback_api_keys": ["sk-other"]})
        seen = next(c for c in daemon.calls if c.get("method") == "agent_ask_v2")
        p = seen["params"]
        assert p.get("provider") == "gemini", p
        assert p.get("model") == "gemini-2.5-flash", p
        assert "api_key" not in p and "base_url" not in p and "fallback_api_keys" not in p, p
        assert p["question"] == "hi"  # non-provider params pass through untouched
        # A non-agent method keeps its params verbatim (no over-broad stripping).
        doc_calls = [c for c in daemon.calls if c.get("method") == "doc_new"]
        assert all("name" in c["params"] for c in doc_calls) or not doc_calls

    @pytest.mark.parametrize(("method", "params"), PROVIDER_MANAGER_CALLS)
    def test_standard_user_cannot_forward_provider_manager_rpc(
        self, daemon, client, method, params
    ):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            resp = _rpc(ws, 2, method, params)

        assert resp == {
            "id": 2,
            "error": {
                "code": -32003,
                "message": "administrator authorization required",
            },
        }
        assert not any(call.get("method") == method for call in daemon.calls)
        assert "sk-browser-secret" not in json.dumps(resp)

    @pytest.mark.parametrize(("method", "params"), PROVIDER_MANAGER_CALLS)
    def test_admin_can_forward_provider_manager_rpc(
        self, daemon, client, method, params
    ):
        with client.websocket_connect("/engine", headers=ADMIN_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            resp = _rpc(ws, 2, method, params)

        assert resp["result"]["ok"] is True
        assert any(call.get("method") == method for call in daemon.calls)

    def test_list_models_injects_host_api_key(self, daemon, client, monkeypatch):
        """agent_list_models gets host keys server-side; browser never sees them back."""
        from gateway import engine_bridge

        monkeypatch.setattr(
            engine_bridge,
            "_load_host_keys",
            lambda: {
                "openrouter": "sk-or-host-secret",
                "gemini": "gem-host-secret",
            },
        )
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            # Empty key + OpenRouter URL — inject must fill api_key from host config.
            _rpc(
                ws,
                2,
                "agent_list_models",
                {
                    "provider": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "",
                },
            )
            # Gemini with no key/url — inject key only.
            _rpc(ws, 3, "agent_list_models", {"provider": "gemini", "api_key": ""})

        seen = [c for c in daemon.calls if c.get("method") == "agent_list_models"]
        assert len(seen) == 2, seen
        or_params = seen[0]["params"]
        assert or_params.get("api_key") == "sk-or-host-secret", or_params
        assert or_params.get("base_url") == "https://openrouter.ai/api/v1", or_params
        gem_params = seen[1]["params"]
        assert gem_params.get("api_key") == "gem-host-secret", gem_params
        # Response path is pass-through — secrets must not be rewritten into client-bound frames
        # (FakeDaemon returns {"ok": true} without echoing params).

    @pytest.mark.parametrize(
        "base_url",
        (
            "https://attacker.example/v1",
            "http://openrouter.ai/api/v1",
            "https://openrouter.ai.evil.example/api/v1",
            "https://openrouter.ai@attacker.example/api/v1",
            "https://attacker.example@openrouter.ai/api/v1",
            "https://openrouter.ai.:443/api/v1",
            "https://openrouter.ai:444/api/v1",
            "https://openrouter.ai/api/v1?redirect=https://attacker.example",
            "https://openrouter.ai/api/v1#https://attacker.example",
            "https://openrouter.ai/api/v1//",
            "https://openrouter.ai/api/v1/../redirect",
        ),
    )
    def test_list_models_rejects_noncanonical_endpoint_without_key_leak(
        self, daemon, client, monkeypatch, caplog, base_url
    ):
        from gateway import engine_bridge

        host_secret = "sk-host-must-never-leave"
        monkeypatch.setattr(
            engine_bridge,
            "_load_host_keys",
            lambda: {"openrouter": host_secret, "openai": host_secret},
        )
        caplog.set_level(logging.WARNING, logger="chudware-web.engine")
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            resp = _rpc(
                ws,
                2,
                "agent_list_models",
                {"provider": "openai", "base_url": base_url, "api_key": ""},
            )

        assert resp == {
            "id": 2,
            "error": {"code": -32002, "message": "provider endpoint not allowed"},
        }
        assert not any(
            call.get("method") == "agent_list_models" for call in daemon.calls
        )
        assert host_secret not in json.dumps(resp)
        assert host_secret not in caplog.text

    def test_list_models_accepts_exact_vendor_origin_and_default_port(
        self, daemon, client, monkeypatch
    ):
        from gateway import engine_bridge

        monkeypatch.setattr(
            engine_bridge,
            "_load_host_keys",
            lambda: {"openai": "sk-openai-host-secret"},
        )
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            assert _rpc(
                ws,
                2,
                "agent_list_models",
                {
                    "provider": "openai",
                    "base_url": "https://api.openai.com:443/v1/",
                    "api_key": "",
                },
            )["result"]["ok"] is True

        seen = next(
            call for call in daemon.calls
            if call.get("method") == "agent_list_models"
        )
        assert seen["params"]["base_url"] == "https://api.openai.com/v1"
        assert seen["params"]["api_key"] == "sk-openai-host-secret"

    def test_list_models_allows_expected_loopback_without_cloud_key(
        self, daemon, client, monkeypatch
    ):
        from gateway import engine_bridge

        host_secret = "sk-cloud-key-not-for-loopback"
        monkeypatch.setattr(
            engine_bridge,
            "_load_host_keys",
            lambda: {"openai": host_secret},
        )
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            assert _rpc(
                ws,
                2,
                "agent_list_models",
                {
                    "provider": "openai",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key": "",
                },
            )["result"]["ok"] is True

        seen = next(
            call for call in daemon.calls
            if call.get("method") == "agent_list_models"
        )
        assert seen["params"]["base_url"] == "http://127.0.0.1:1234/v1"
        assert seen["params"].get("api_key") != host_secret



class TestWorkspaceApi:
    def test_list_and_download(self, workspace, client):
        resp = client.get("/workspace/list", headers=DEV_HEADERS)
        assert resp.status_code == 200
        assert [d["name"] for d in resp.json()["dirs"]] == ["parts"]
        resp = client.get("/workspace/list", params={"dir": "parts"}, headers=DEV_HEADERS)
        assert resp.json()["files"][0]["name"] == "bracket.step+"
        resp = client.get(
            "/workspace/download", params={"path": "parts/bracket.step+"}, headers=DEV_HEADERS
        )
        assert resp.status_code == 200 and resp.content == b"fake step plus"

    def test_upload_roundtrip(self, workspace, client):
        resp = client.post(
            "/workspace/upload",
            headers=DEV_HEADERS,
            files={"file": ("imported.stl", b"solid x", "application/octet-stream")},
            data={"dir": "parts"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "parts/imported.stl" and body["bytes"] == 7
        assert (workspace / "parts" / "imported.stl").read_bytes() == b"solid x"
        # abs_path is returned for the WASM client's doc_open, but it must name
        # a file inside this caller's own per-account tree (tenant isolation).
        assert Path(body["abs_path"]) == workspace / "parts" / "imported.stl"

    def test_traversal_rejected(self, workspace, client):
        for bad in ("../secrets", "..", "a/../../b"):
            resp = client.get("/workspace/list", params={"dir": bad}, headers=DEV_HEADERS)
            assert resp.status_code == 400, bad
        resp = client.get(
            "/workspace/download", params={"path": "../../etc/passwd"}, headers=DEV_HEADERS
        )
        assert resp.status_code == 400

    def test_mkdir_rename_delete(self, workspace, client):
        assert client.post(
            "/workspace/mkdir", json={"path": "exports"}, headers=DEV_HEADERS
        ).status_code == 200
        assert (workspace / "exports").is_dir()
        assert client.post(
            "/workspace/rename",
            json={"src": "parts/bracket.step+", "dst": "exports/bracket.step+"},
            headers=DEV_HEADERS,
        ).status_code == 200
        assert client.post(
            "/workspace/delete", json={"path": "exports/bracket.step+"}, headers=DEV_HEADERS
        ).status_code == 200
        assert not (workspace / "exports" / "bracket.step+").exists()

    def test_aicm_allowlist(self, workspace, client, tmp_path):
        inside = workspace / "mesh.aicm"
        inside.write_bytes(b"AICM")
        resp = client.get("/files/aicm", params={"path": str(inside)}, headers=DEV_HEADERS)
        assert resp.status_code == 200 and resp.content == b"AICM"
        outside = tmp_path / "evil.aicm"
        outside.write_bytes(b"nope")
        resp = client.get("/files/aicm", params={"path": str(outside)}, headers=DEV_HEADERS)
        assert resp.status_code == 403
        resp = client.get("/files/aicm", params={"shm": "../etc/passwd"}, headers=DEV_HEADERS)
        assert resp.status_code == 400

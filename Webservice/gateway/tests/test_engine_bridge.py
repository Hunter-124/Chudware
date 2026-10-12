"""Engine bridge + workspace API tests (hermetic — fake daemon on a unix socket)."""

from __future__ import annotations

import base64
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

from gateway import accounts, app as app_mod, credits, db, provider_accounts, engine_bridge  # noqa: E402
from gateway.config import Settings, settings  # noqa: E402
from gateway.limits import user_slug  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}
ADMIN_HEADERS = {"X-Dev-User": "admin@example.com"}

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
        self.docs: dict[str, dict] = {}
        self._next_doc = 1

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

        async def boot() -> asyncio.AbstractServer:
            server = await asyncio.start_unix_server(self._client, path=str(self.sock_path))
            self._started.set()
            return server

        server = loop.run_until_complete(boot())
        try:
            loop.run_forever()
        finally:
            server.close()
            loop.run_until_complete(server.wait_closed())
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

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
                elif method == "import_file" and params.get("into"):
                    result = {"ok": True, "doc_id": params["into"], "revision": 1}
                elif method in {"doc_new", "doc_open", "import_file", "doc_import", "doc_import_file"}:
                    doc_id = f"d{self._next_doc}"
                    self._next_doc += 1
                    path = params.get("path")
                    self.docs[doc_id] = {
                        "doc_id": doc_id,
                        "name": params.get("name", doc_id),
                        "path": path,
                        "revision": 0,
                    }
                    result = {"doc_id": doc_id, "revision": 0}
                    await self._broadcast(
                        {"event": "doc.opened", "data": {"doc_id": doc_id, "name": self.docs[doc_id]["name"]}}
                    )
                elif method == "doc_list":
                    result = {"docs": list(self.docs.values())}
                elif method == "doc_close":
                    result = {"closed": self.docs.pop(params.get("doc_id", ""), None) is not None}
                elif method == "doc_apply_ops":
                    result = {"ok": True, "revision": 1}
                    await self._broadcast(
                        {"event": "doc.changed", "data": {"doc_id": params.get("doc_id"), "revision": 1}}
                    )
                elif method == "agent_ask_v2" and params.get("_test_emit_usage"):
                    event = {
                        "event": "agent.event",
                        "data": {
                            "kind": "agent.provider_usage",
                            "provider": "mock",
                            "model": params.get("model", ""),
                            "prompt_tokens": 100,
                            "completion_tokens": 100,
                            "total_tokens": 200,
                            "context_tokens": 100,
                            "source": "api",
                        },
                    }
                    writer.write((json.dumps(event) + "\n").encode())
                    await writer.drain()
                    result = {"ok": True}
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
def daemon(unix_socket_path, monkeypatch):
    sock = unix_socket_path
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
def _identities(pg):
    """/engine authorizes on identity alone — no per-caller seat exists — so
    both test callers need an active row in the store the middleware reads."""
    _seed("demo@example.com", "Demo User")
    _seed("admin@example.com", "Admin", admin=True)


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
    def test_host_provider_key_sharing_defaults_closed(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_WEB_SHARE_PROVIDER_KEYS", raising=False)
        assert Settings().share_provider_keys is False
        monkeypatch.setenv("CHUDWARE_WEB_SHARE_PROVIDER_KEYS", "1")
        assert Settings().share_provider_keys is True

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

    def test_document_events_do_not_cross_account_boundaries(self, daemon, client):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as owner:
            _rpc(owner, 1, "hello")
            with client.websocket_connect("/engine", headers=ADMIN_HEADERS) as other:
                _rpc(other, 1, "hello")
                doc_id = _rpc(owner, 2, "doc_new", {"name": "Private Part"})["result"]["doc_id"]
                owner.send_json(
                    {"id": 3, "method": "doc_apply_ops", "params": {"doc_id": doc_id, "ops": []}}
                )
                changed = owner.receive_json()
                assert changed["event"] == "doc.changed"
                assert changed["data"]["doc_id"] == doc_id
                assert owner.receive_json()["result"]["revision"] == 1

                # The daemon wrote both events before this ping. A plain next-frame
                # read proves neither the private title nor its update leaked.
                other.send_json({"id": 2, "method": "ping", "params": {}})
                assert other.receive_json() == {"id": 2, "result": {"pong": True}}
                assert _rpc(other, 3, "doc_tree", {"doc_id": doc_id})["error"]["code"] == -32004

    def test_document_capabilities_isolate_accounts_and_reconnect(
        self, daemon, tmp_path, monkeypatch
    ):
        workspace_root = tmp_path / "workspace"
        bucket_root = tmp_path / "bucket"
        monkeypatch.setattr(settings, "workspace_root", workspace_root)
        monkeypatch.setattr(settings, "bucket_root", bucket_root)
        alice_root = settings.user_workspace_root(user_slug("demo@example.com"))
        bob_root = settings.user_workspace_root(user_slug("admin@example.com"))
        alice_root.mkdir(parents=True)
        bob_root.mkdir(parents=True)

        alice_client = TestClient(app_mod.app)
        bob_client = TestClient(app_mod.app)
        with alice_client.websocket_connect("/engine", headers=DEV_HEADERS) as alice:
            alice_id = _rpc(alice, 1, "doc_open", {"path": str(alice_root / "a.step+")})["result"]["doc_id"]
            assert _rpc(alice, 2, "doc_tree", {"doc_id": alice_id})["result"]["ok"]
            assert _rpc(alice, 3, "doc_apply_ops", {"doc_id": alice_id, "ops": []})["result"]["ok"]
            assert _rpc(alice, 4, "doc_save", {"doc_id": alice_id, "path": str(alice_root / "a.step+")})["result"]["ok"]
            with bob_client.websocket_connect("/engine", headers=ADMIN_HEADERS) as bob:
                assert _rpc(bob, 10, "doc_list")["result"]["docs"] == []
                for call_id, method, params in (
                    (11, "doc_tree", {"doc_id": alice_id}),
                    (12, "doc_apply_ops", {"doc_id": alice_id, "ops": []}),
                    (13, "doc_save", {"doc_id": alice_id, "path": str(bob_root / "stolen.step+")}),
                    (14, "doc_close", {"doc_id": alice_id}),
                    (15, "batch", {"ops": [{"params": {"doc_id": alice_id}}]}),
                ):
                    assert _rpc(bob, call_id, method, params)["error"]["code"] == -32004
            assert _rpc(alice, 5, "doc_tree", {"doc_id": alice_id})["result"]["ok"]

        # Reconnect starts empty, then path-owned listing grants the capability.
        reconnect_client = TestClient(app_mod.app)
        with reconnect_client.websocket_connect("/engine", headers=DEV_HEADERS) as alice:
            listed = _rpc(alice, 20, "doc_list")["result"]["docs"]
            assert [entry["doc_id"] for entry in listed] == [alice_id]
            assert _rpc(alice, 21, "doc_tree", {"doc_id": alice_id})["result"]["ok"]
            assert _rpc(alice, 22, "doc_close", {"doc_id": alice_id})["result"]["closed"]
            assert _rpc(alice, 23, "doc_tree", {"doc_id": alice_id})["error"]["code"] == -32004

    def test_import_file_grants_minted_doc_and_guards_into(self, daemon, tmp_path, monkeypatch):
        # import_file (no `into`) is how the shared daemon imports DWG/DXF/SAT/STEP as a
        # new document; the importing socket must be able to edit what it imported, and
        # nobody may import INTO another account's document.
        workspace_root = tmp_path / "workspace"
        monkeypatch.setattr(settings, "workspace_root", workspace_root)
        monkeypatch.setattr(settings, "bucket_root", tmp_path / "bucket")
        alice_root = settings.user_workspace_root(user_slug("demo@example.com"))
        bob_root = settings.user_workspace_root(user_slug("admin@example.com"))
        alice_root.mkdir(parents=True)
        bob_root.mkdir(parents=True)

        with TestClient(app_mod.app).websocket_connect("/engine", headers=DEV_HEADERS) as alice:
            imported = _rpc(alice, 1, "import_file", {"path": str(alice_root / "Widget.dwg")})
            doc_id = imported["result"]["doc_id"]
            assert _rpc(alice, 2, "doc_tree", {"doc_id": doc_id})["result"]["ok"]
            into = _rpc(alice, 3, "import_file",
                        {"path": str(alice_root / "Body.sat"), "into": doc_id})
            assert into["result"]["doc_id"] == doc_id
            with TestClient(app_mod.app).websocket_connect("/engine", headers=ADMIN_HEADERS) as bob:
                stolen = _rpc(bob, 10, "import_file",
                              {"path": str(bob_root / "Body.sat"), "into": doc_id})
                assert stolen["error"]["code"] == -32004

    def test_doc_list_fails_closed_for_pathless_foreign_docs(self, daemon, client):
        daemon.docs["foreign"] = {
            "doc_id": "foreign", "name": "Unknown", "path": None, "revision": 0
        }
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(ws, 1, "doc_list")["result"]["docs"] == []

    def test_unauthenticated_socket_rejected(self, daemon, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/engine"):
                pass
        assert exc.value.code == 4401

    def test_cloud_access_binds_to_account_plan(self, daemon, client):
        from starlette.websockets import WebSocketDisconnect

        accounts.set_plan("demo@example.com", "desktop")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/engine", headers=DEV_HEADERS):
                pass
        assert exc.value.code == 4403
        assert exc.value.reason == "plan_no_cloud"

        accounts.set_plan("demo@example.com", "studio")
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(ws, 1, "ping")["result"]["pong"] is True

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

    def test_agent_turn_keeps_provider_strips_secrets(
        self, daemon, client, monkeypatch
    ):
        """Legacy single-tenant turns keep selection but strip browser secrets.

        Setting host-key sharing true is deliberate here. With sharing disabled,
        the gateway refuses this unmetered ambient-host-key path.
        """
        monkeypatch.setattr(settings, "share_provider_keys", True)
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            # Client pins provider/model selection + a browser-side secret; only
            # the secret must be dropped before hostd sees the turn.
            _rpc(ws, 2, "agent_ask_v2", {
                "question": "hi", "provider": "gemini",
                "model": "gemini-2.5-flash", "api_key": "sk-web",
                "base_url": "https://evil.example",
                "fallback_api_keys": ["sk-other"],
                "_chudware_actor": {
                    "email": "attacker@example.com", "plan": "enterprise",
                    "credits_remaining": 999999, "key_source": "pool",
                },
                "options": {"_chudware_actor": {
                    "email": "nested-attacker@example.com", "key_source": "pool",
                }},
            })
        seen = next(c for c in daemon.calls if c.get("method") == "agent_ask_v2")
        p = seen["params"]
        assert p.get("provider") == "gemini", p
        assert p.get("model") == "gemini-2.5-flash", p
        assert "api_key" not in p and "base_url" not in p and "fallback_api_keys" not in p, p
        assert p["question"] == "hi"  # non-provider params pass through untouched
        actor = p["options"]["_chudware_actor"]
        assert actor == {
            "email": "demo@example.com",
            "plan": "trial",

            "credits_remaining": 0,
            "key_source": "byok",
        }
        assert "_chudware_actor" not in p
        # A non-agent method keeps its params verbatim (no over-broad stripping).
        doc_calls = [c for c in daemon.calls if c.get("method") == "doc_new"]
        assert all("name" in c["params"] for c in doc_calls) or not doc_calls

    @pytest.mark.parametrize("method", ["agent_admin_turn", "doc_file_operations"])
    def test_browser_cannot_invoke_internal_rpc_directly(self, daemon, client, method):
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            response = _rpc(ws, 76, method, {
                "request": "bypass metering", "provider": "mock",
            })
        assert response["error"]["code"] == -32601
        assert not any(call["method"] == method for call in daemon.calls)

    def test_trial_sponsor_forces_claude_without_credit_reservation(
        self, daemon, client, monkeypatch, caplog
    ):
        monkeypatch.setattr(settings, "share_provider_keys", False)
        caplog.set_level(logging.INFO, logger=engine_bridge.log.name)
        monkeypatch.setattr(
            engine_bridge, "default_model_for",
            lambda provider: "claude-live-vetted" if provider == "anthropic-sub" else None,
        )
        monkeypatch.setattr(
            settings, "provider_key_encryption_key",
            base64.urlsafe_b64encode(b"S" * 32).decode().rstrip("="),
        )
        monkeypatch.setattr(
            engine_bridge, "reserve_turn",
            lambda *_args, **_kwargs: pytest.fail("sponsored turn reserved credits"),
        )
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO credentials(email, password_hash, email_verified) "
                "VALUES ('demo@example.com', 'unused', TRUE)"
            )
            cur.execute(
                "INSERT INTO trial_domains(domain, trial_days, ai_credits) "
                "VALUES ('example.com', 14, 0)"
            )
            cur.execute(
                "UPDATE accounts SET ai_grant = TRUE, credit_balance = 0 "
                "WHERE email = 'demo@example.com'"
            )
        provider_accounts._persist_account(
            "admin@example.com", "anthropic-sub", "personal",
            {
                "account_key": "sponsor-claude",
                "label": "admin Claude",
                "plan": "max",
                "tokens": {
                    "access_token": "sponsor-secret",
                    "refresh_token": "refresh-secret",
                    "expires_at_ms": 4_000_000_000_000,
                },
            },
        )
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(ws, 80, "agent_ask_v2", {
                "question": "bracket", "provider": "openrouter", "model": "other/model",
                "_test_emit_usage": True,
            })["result"]["ok"] is True
            assert _rpc(ws, 81, "agent_ask_v2", {
                "question": "bracket", "provider": "anthropic",
                "model": "claude-sonnet-5",
            })["result"]["ok"] is True
        turns = [call["params"] for call in daemon.calls if call["method"] == "agent_ask_v2"]
        assert len(turns) == 2
        assert turns[0]["provider"] == "anthropic"
        assert turns[0]["model"] == "claude-live-vetted"
        assert turns[1]["provider"] == "anthropic"
        assert turns[1]["model"] == "claude-sonnet-5"
        for params in turns:
            actor = params["options"]["_chudware_actor"]
            assert actor["key_source"] == "subscription"
            assert actor["_subscription"]["provider"] == "anthropic-sub"
            assert "_reservation_credits" not in actor
        with db.tx() as cur:
            cur.execute(
                "SELECT credit_balance FROM accounts WHERE email = 'demo@example.com'"
            )
            assert cur.fetchone()["credit_balance"] == 0
            cur.execute(
                "SELECT count(*) AS total FROM credit_ledger WHERE email = 'demo@example.com'"
            )
            assert cur.fetchone()["total"] == 0
        assert "sponsored_usage email=demo@example.com sponsor=admin@example.com" in caplog.text
        assert "prompt_tokens=100 completion_tokens=100" in caplog.text
        assert "sponsor-secret" not in caplog.text


    def test_studio_pool_reserves_reconciles_and_stops_at_zero(
        self, daemon, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "share_provider_keys", False)
        monkeypatch.setenv("CHUDWARE_WEB_POOL_API_KEY", "operator-pool-test-key")
        with db.tx() as cur:
            cur.execute(
                """
                UPDATE accounts
                   SET plan = 'studio', ai_grant = TRUE
                 WHERE email = %s
                """,
                ("demo@example.com",),
            )
        credits.grant("demo@example.com", 1, note="bridge metering test")
        credits.refresh_model_rates(
            lambda _url: {
                "data": [{
                    "id": "vendor/cheap",
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                }]
            },
            now=1_800_000_000,
        )
        params = {
            "question": "cheap turn",
            "provider": "openrouter",
            "model": "vendor/cheap",
            "max_tokens": 8,
            "_test_emit_usage": True,
        }
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(ws, 77, "agent_ask_v2", params)["result"]["ok"] is True
            refused = _rpc(ws, 78, "agent_ask_v2", params)
        assert refused["result"] == {
            "ok": False,
            "reason": "insufficient_credits",
        }

        forwarded = [
            call for call in daemon.calls if call.get("method") == "agent_ask_v2"
        ]
        assert len(forwarded) == 1
        actor = forwarded[0]["params"]["options"]["_chudware_actor"]
        assert actor["plan"] == "studio"
        assert actor["key_source"] == "pool"
        assert actor["_pool"]["api_key"] == "operator-pool-test-key"
        assert actor["_reservation_credits"] == 1
        with db.tx() as cur:
            cur.execute(
                "SELECT credit_balance FROM accounts WHERE email = %s",
                ("demo@example.com",),
            )
            assert cur.fetchone()["credit_balance"] == 0
            cur.execute(
                """
                SELECT model, credits_delta, note
                  FROM credit_ledger
                 WHERE email = %s AND kind = 'spend'
                """,
                ("demo@example.com",),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert (row["model"], row["credits_delta"]) == ("vendor/cheap", -1)
        reservation = json.loads(row["note"])["reservation"]
        assert reservation["state"] == "reconciled"
        assert reservation["prompt_tokens"] == 100
        assert reservation["completion_tokens"] == 100
    @pytest.mark.parametrize("headers", (DEV_HEADERS, ADMIN_HEADERS))
    def test_unmetered_host_key_fallback_is_refused_for_every_account(
        self, daemon, client, monkeypatch, headers
    ):
        from gateway import engine_bridge

        host_secret = "sk-operator-must-not-fallback"
        monkeypatch.setattr(settings, "share_provider_keys", False)
        monkeypatch.setattr(
            engine_bridge,
            "_load_host_keys",
            lambda: {"openrouter": host_secret},
        )
        with client.websocket_connect("/engine", headers=headers) as ws:
            response = _rpc(ws, 79, "agent_ask_v2", {
                "question": "must not spend the operator key",
                "provider": "openrouter",
                "model": "vendor/cheap",
            })
        assert response["result"] == {
            "ok": False,
            "reason": "provider_credentials_required",
        }
        assert not any(
            call.get("method") == "agent_ask_v2" for call in daemon.calls
        )
        assert host_secret not in json.dumps(daemon.calls)




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

    def test_list_models_injects_host_api_key_when_sharing_enabled(
        self, daemon, client, monkeypatch
    ):
        """The tested legacy injection now requires the explicit opt-in."""
        from gateway import engine_bridge
        monkeypatch.setattr(settings, "share_provider_keys", True)

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
    def test_list_models_carries_no_host_key_when_sharing_disabled(
        self, daemon, client, monkeypatch
    ):
        from gateway import engine_bridge

        host_secret = "sk-host-must-stay-out"
        loaded = False

        def load_host_keys():
            nonlocal loaded
            loaded = True
            return {"openrouter": host_secret}

        monkeypatch.setattr(settings, "share_provider_keys", False)
        monkeypatch.setattr(engine_bridge, "_load_host_keys", load_host_keys)
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            assert _rpc(
                ws,
                4,
                "agent_list_models",
                {
                    "provider": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "",
                },
            )["result"]["ok"] is True

        seen = next(
            call for call in daemon.calls
            if call.get("method") == "agent_list_models"
        )
        assert loaded is False
        assert seen["params"].get("api_key") in (None, "")
        assert host_secret not in json.dumps(seen)


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
        monkeypatch.setattr(settings, "share_provider_keys", True)

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
        monkeypatch.setattr(settings, "share_provider_keys", True)

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
        monkeypatch.setattr(settings, "share_provider_keys", True)

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

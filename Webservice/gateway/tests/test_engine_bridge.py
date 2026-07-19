"""Engine bridge + workspace API tests (hermetic — fake daemon on a unix socket)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["BILLET_WEB_DEV"] = "1"
os.environ.setdefault("BILLET_WEB_DATA_ROOT", str(Path.home() / "billet-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway.config import settings  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}
ADMIN_HEADERS = {"X-Dev-User": "haedmonds69@gmail.com"}


class FakeDaemon:
    """Newline-JSON-RPC unix-socket server mimicking aicad-hostd for the bridge.

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
    root = tmp_path / "CAD_Build"
    root.mkdir()
    (root / "parts").mkdir()
    (root / "parts" / "bracket.step+").write_bytes(b"fake step plus")
    monkeypatch.setattr(settings, "workspace_root", root)
    monkeypatch.setattr(settings, "dev_mode", True)
    return root


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
        assert client.get("/healthz").json()["engine_bridges"] == 0
        with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
            _rpc(ws, 1, "hello")
            assert engine_bridge.active_bridge_count() == 1
            assert client.get("/healthz").json()["engine_bridges"] == 1
        # Closing the socket must decrement — the deploy idle-wait relies on this
        # returning to 0, else it would defer restarts forever.
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
        # abs_path is what the browser hands to the daemon's import_file RPC.
        assert body["abs_path"] == str(workspace / "parts" / "imported.stl")

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

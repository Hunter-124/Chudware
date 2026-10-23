"""Real local HTTP/SSE + identity-bound UDS broker, disposable PostgreSQL authority."""
from __future__ import annotations

import asyncio
import base64
import json
import signal
import socket
import stat
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway import accounts, cells, db, provider_broker, provider_keys
from gateway.config import settings

A = "broker-a@example.com"
B = "broker-b@example.com"
KEY_A = "fixture-upstream-a-never-in-cell"
KEY_ROTATED = "fixture-upstream-rotated-never-in-cell"
SSE_FIRST = (b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
             b'"delta":{"type":"thinking_delta","thinking":"raw thought"}}\n\n'
             b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
             b'"delta":{"type":"signature_delta","signature":"fixture-signature"}}\n\n')
SSE_LAST = (b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":3}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n')


@dataclass
class WireRequest:
    path: str
    query: bytes
    headers: dict
    body: bytes
    release: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class OfficialWire:
    """Local vendor-shaped server; no monkeypatched HTTP transport or echo response."""
    def __init__(self):
        self.requests = []
        self.requested = asyncio.Event()
        self.task = None
        self.server = None

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        body = await request.body()
        row = WireRequest(scope["path"], scope.get("query_string", b""), dict(request.headers), body)
        self.requests.append(row)
        self.requested.set()
        if request.method == "GET":
            await JSONResponse({"data": [{"id": "fixture-model", "object": "model"}]},
                               headers={"set-cookie": "fixture-session=must-not-cross-actors; Path=/"})(scope, receive, send)
            row.closed.set()
            return
        data = json.loads(body or b"{}")
        if data.get("reject"):
            # A credential-reflecting vendor error must not leak to the cell.
            await JSONResponse({"error": row.headers.get("x-api-key", "")}, status_code=401)(scope, receive, send)
            row.closed.set()
            return
        if data.get("oversized"):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(provider_broker.MAX_RESPONSE_BYTES + 1).encode())]})
            try:
                while (await receive())["type"] != "http.disconnect":
                    pass
            finally:
                row.closed.set()
            return

        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        disconnect = asyncio.create_task(disconnected())
        release = asyncio.create_task(row.release.wait())
        try:
            if data.get("hold_headers"):
                done, _ = await asyncio.wait([disconnect, release], return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    return
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
            await send({"type": "http.response.body", "body": SSE_FIRST, "more_body": True})
            if not data.get("hold"):
                row.release.set()
            done, _ = await asyncio.wait([disconnect, release], return_when=asyncio.FIRST_COMPLETED)
            if disconnect not in done:
                await send({"type": "http.response.body", "body": SSE_LAST, "more_body": False})
        finally:
            disconnect.cancel()
            release.cancel()
            await asyncio.gather(disconnect, release, return_exceptions=True)
            row.closed.set()

    async def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        self.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = provider_broker.uvicorn.Config(self, interface="asgi3", lifespan="off", http="h11",
            access_log=False, log_config=None, log_level=None)
        self.server = provider_broker._OwnedServer(config)
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.task.done():
                    self.task.result()
                await asyncio.sleep(0.01)

    async def close(self):
        for row in self.requests:
            row.release.set()
        self.server.should_exit = True
        await self.task


@pytest.fixture
def broker_env(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "work")
    monkeypatch.setattr(settings, "bucket_root", tmp_path / "buckets")
    monkeypatch.setattr(settings, "university_team_id", "")
    monkeypatch.setattr(settings, "provider_key_encryption_key", base64.urlsafe_b64encode(b"G" * 32).decode())
    with tempfile.TemporaryDirectory(prefix="cw-pbr-", dir="/tmp") as root:
        monkeypatch.setattr(settings, "cells_root", Path(root))
        for email in (A, B):
            accounts.ensure_account(email)
            accounts.set_status(email, "active")
            accounts.set_plan(email, "studio")
            for provider in provider_broker.SUPPORTED_PROVIDERS:
                provider_keys._persist_validated_key(email, provider, KEY_A if email == A else "fixture-key-b")
        yield


@asynccontextmanager
async def runtime(principal=None):
    wire = OfficialWire()
    await wire.start()
    broker = provider_broker.ProviderBroker(upstream_origins={p: wire.origin for p in provider_broker.SUPPORTED_PROVIDERS})
    principal = principal or cells.resolve_workspace(A)
    directory = cells.prepare_cell(principal)
    await broker.ensure_cell(principal, directory)
    transport = httpx.AsyncHTTPTransport(uds=str(directory / "provider.sock"))
    async with httpx.AsyncClient(transport=transport, base_url="http://cell", trust_env=False,
                                timeout=httpx.Timeout(5, read=None)) as client:
        try:
            yield wire, broker, principal, client
        finally:
            await broker.close()
            await wire.close()


def run(coro):
    async def bounded():
        async with asyncio.timeout(20):
            await coro
    asyncio.run(bounded())


async def first_sse(chunks):
    body = bytearray()
    while len(body) < len(SSE_FIRST):
        body.extend(await anext(chunks))
    assert bytes(body) == SSE_FIRST


def test_real_sse_preserves_raw_thought_cache_body_and_rotates_key_without_grant_churn(broker_env, caplog):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            body = (b'{ "model":"fixture-model", "stream":true, "hold":true,'
                    b'"system":[{"type":"text","text":"stable prefix","cache_control":{"type":"ephemeral"}}],'
                    b'"messages":[{"role":"assistant","content":[{"type":"thinking",'
                    b'"thinking":"retained thought","signature":"fixture-signature"}]}]}')
            async with client.stream("POST", "/anthropic/v1/messages", content=body,
                                     headers={"authorization": "Bearer " + grant}) as response:
                assert response.status_code == 200
                chunks = response.aiter_raw()
                await first_sse(chunks)
                assert not wire.requests[0].closed.is_set()
                assert wire.requests[0].body == body
                assert wire.requests[0].headers["x-api-key"] == KEY_A
                assert "authorization" not in wire.requests[0].headers
                provider_keys._persist_validated_key(A, "anthropic", KEY_ROTATED)
                assert broker.mint_grant(A, principal, "anthropic") == grant
                # Existing stream remains authorized while new requests resolve the latest key.
                second_body = body.replace(b'"hold":true', b'"hold":false')
                second = await client.post("/anthropic/v1/messages", content=second_body,
                                           headers={"x-api-key": grant})
                assert second.content == SSE_FIRST + SSE_LAST
                assert wire.requests[1].headers["x-api-key"] == KEY_ROTATED
                assert wire.requests[1].body == second_body
                wire.requests[0].release.set()
                remaining = b"".join([chunk async for chunk in chunks])
                assert remaining == SSE_LAST
            with db.tx() as cur:
                cur.execute("SELECT last_used_at FROM provider_keys WHERE email=%s AND provider='anthropic'", (A,))
                assert cur.fetchone()["last_used_at"] is None
    run(scenario())
    assert KEY_A not in caplog.text and KEY_ROTATED not in caplog.text


@pytest.mark.parametrize("provider,path,header,upstream_path", [
    ("anthropic", "/anthropic/v1/models", "x-api-key", "/v1/models"),
    ("openai", "/openai/v1/models", "authorization", "/v1/models"),
    ("xai", "/xai/v1/models", "authorization", "/v1/models"),
    ("openrouter", "/openrouter/v1/images/models", "authorization", "/api/v1/images/models"),
    ("gemini", "/gemini/models", "x-goog-api-key", "/v1beta/models"),
])
def test_actual_supported_catalog_requests_use_fixed_official_auth(broker_env, provider, path, header, upstream_path):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, provider)
            response = await client.get(path, headers={header: ("Bearer " if header == "authorization" else "") + grant})
            assert response.status_code == 200
            assert response.json()["data"][0]["id"] == "fixture-model"
            assert wire.requests[0].path == upstream_path
            spec = provider_keys.VALIDATION_SPECS[provider]
            assert wire.requests[0].headers[spec.header.lower()] == spec.prefix + KEY_A
            assert grant not in json.dumps(wire.requests[0].headers)
    run(scenario())


def test_missing_forged_cross_socket_and_provider_grants_never_reach_upstream(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "openai")
            cross = broker.mint_grant(B, cells.resolve_workspace(B), "openai")
            wrong_provider = broker.mint_grant(A, principal, "xai")
            for token in ("forged", grant[:-1] + ("A" if grant[-1] != "A" else "B"), cross, wrong_provider):
                response = await client.get("/openai/v1/models", headers={"authorization": "Bearer " + token})
                assert response.status_code == 401
            assert (await client.get("/openai/v1/models")).status_code == 401
            for path in ("/openai/v1/responses?url=https://example.com", "/openai/anything",
                         "/openai/v1/models?key=attacker", "/moonshot/v1/models"):
                response = await client.get(path, headers={"authorization": "Bearer " + grant})
                assert response.status_code == 404
            assert wire.requests == []
    run(scenario())


@pytest.mark.parametrize("revocation", ["epoch", "key", "account", "role", "plan"])
def test_live_revocation_closes_upstream_and_refuses_stale_grant(broker_env, revocation):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            async with client.stream("POST", "/anthropic/v1/messages", json={"hold": True},
                                     headers={"authorization": "Bearer " + grant}) as response:
                chunks = response.aiter_raw()
                await first_sse(chunks)
                if revocation == "epoch":
                    with db.tx() as cur:
                        cur.execute("UPDATE cell_principal_epochs SET epoch=epoch+1 WHERE workspace_key=%s AND actor_key=%s",
                                    (principal.workspace_key, principal.actor_key))
                elif revocation == "key":
                    provider_keys.delete_key(A, "anthropic")
                elif revocation == "account":
                    accounts.set_status(A, "suspended")
                elif revocation == "role":
                    accounts.set_role(A, "admin")
                else:
                    accounts.set_plan(A, "desktop")
                async with asyncio.timeout(4):
                    await wire.requests[0].closed.wait()
                with pytest.raises(httpx.RemoteProtocolError):
                    await anext(chunks)
            refused = await client.get("/anthropic/v1/models", headers={"x-api-key": grant})
            assert refused.status_code == 401
            assert len(wire.requests) == 1
    run(scenario())


@pytest.mark.parametrize("change", ["membership", "right"])
def test_shared_workspace_grant_checks_live_membership_and_right(broker_env, change):
    org_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.tx() as cur:
        cur.execute("INSERT INTO organizations(id,name) VALUES(%s,'broker-test')", (org_id,))
        cur.execute("INSERT INTO org_members(org_id,email,role) VALUES(%s,%s,'owner'),(%s,%s,'member')",
                    (org_id, A, org_id, B))
        cur.execute("INSERT INTO teams(id,org_id,name) VALUES(%s,%s,'broker-team')", (team_id, org_id))
        cur.execute("INSERT INTO team_members(team_id,email,role) VALUES(%s,%s,'member')", (team_id, B))
    principal = cells.resolve_workspace(B, team_id)
    async def scenario():
        async with runtime(principal) as (wire, broker, _, client):
            grant = broker.mint_grant(B, principal, "openai")
            assert (await client.get("/openai/v1/models", headers={"authorization": "Bearer " + grant})).status_code == 200
            with db.tx() as cur:
                if change == "membership":
                    cur.execute("DELETE FROM team_members WHERE team_id=%s AND email=%s", (team_id, B))
                else:
                    cur.execute("UPDATE team_members SET role='viewer' WHERE team_id=%s AND email=%s", (team_id, B))
            assert (await client.get("/openai/v1/models", headers={"authorization": "Bearer " + grant})).status_code == 401
            assert len(wire.requests) == 1
    run(scenario())


def test_client_cancel_closes_gated_upstream_and_releases_actor_slot(broker_env, monkeypatch):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            monkeypatch.setattr(provider_broker, "_broker", broker)
            grant = broker.mint_grant(A, principal, "anthropic")
            async with client.stream("POST", "/anthropic/v1/messages", json={"hold": True},
                                     headers={"x-api-key": grant}) as response:
                await first_sse(response.aiter_raw())
                assert provider_broker.active_request_count() == 1
            async with asyncio.timeout(4):
                await wire.requests[0].closed.wait()
                while broker.active:
                    await asyncio.sleep(0.01)
            assert broker.actors == {}
            assert provider_broker.active_request_count() == 0
            assert (await client.get("/anthropic/v1/models", headers={"x-api-key": grant})).status_code == 200
    run(scenario())


def test_revocation_before_upstream_headers_cancels_the_real_http_request(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            task = asyncio.create_task(client.post("/anthropic/v1/messages", json={"hold_headers": True},
                                                   headers={"x-api-key": grant}))
            await wire.requested.wait()
            provider_keys.delete_key(A, "anthropic")
            async with asyncio.timeout(4):
                await wire.requests[0].closed.wait()
                response = await task
            assert response.status_code == 401
    run(scenario())


def test_body_header_response_and_actor_bounds_refuse_without_credentials_in_errors(broker_env, monkeypatch, caplog):
    monkeypatch.setattr(provider_broker, "MAX_BODY_BYTES", 1024)
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            headers = {"x-api-key": grant}
            assert (await client.post("/anthropic/v1/messages", content=b"x" * 1025, headers=headers)).status_code == 413
            async def oversized_chunks():
                yield b"x" * 600
                yield b"x" * 600
            assert (await client.post("/anthropic/v1/messages", content=oversized_chunks(), headers=headers)).status_code == 413
            assert (await client.get("/anthropic/v1/models", headers={**headers, **{f"x-{n}": "a" for n in range(70)}})).status_code == 431
            assert wire.requests == []
            reflected = await client.post("/anthropic/v1/messages", json={"reject": True}, headers=headers)
            assert reflected.status_code == 401 and KEY_A not in reflected.text
            oversized = await client.post("/anthropic/v1/messages", json={"oversized": True}, headers=headers)
            assert oversized.status_code == 502
            async with asyncio.timeout(4):
                while broker.active:
                    await asyncio.sleep(0.01)
            monkeypatch.setattr(provider_broker, "MAX_ACTOR_REQUESTS", 1)
            async with client.stream("POST", "/anthropic/v1/messages", json={"hold": True}, headers=headers) as response:
                await first_sse(response.aiter_raw())
                assert (await client.get("/anthropic/v1/models", headers=headers)).status_code == 429
            async with asyncio.timeout(4):
                await wire.requests[-1].closed.wait()
    run(scenario())
    assert KEY_A not in caplog.text


def test_internal_app_is_not_reachable_through_authenticated_public_gateway(broker_env, monkeypatch):
    from fastapi.testclient import TestClient
    from gateway.app import app
    monkeypatch.setattr(settings, "dev_mode", True)
    # No context/lifespan: this probes the real public route table and auth middleware.
    client = TestClient(app)
    try:
        grant = provider_broker.mint_grant(A, cells.resolve_workspace(A), "openai")
        headers = {"X-Dev-User": A, "authorization": "Bearer " + grant}
        response = client.get("/anthropic/v1/models", headers=headers, follow_redirects=False)
        assert response.status_code == 404
        response = client.post("/openai/v1/responses", headers=headers, json={})
        assert response.status_code == 404
    finally:
        client.close()


def test_owned_listener_has_private_identity_and_does_not_replace_signal_handlers(broker_env):
    async def scenario():
        wire = OfficialWire()
        await wire.start()
        broker = provider_broker.ProviderBroker(upstream_origins={"openai": wire.origin})
        principal = cells.resolve_workspace(A)
        directory = cells.prepare_cell(principal)
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        await broker.ensure_cell(principal, directory)
        path = directory / "provider.sock"
        info = path.lstat()
        assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert before == {sig: signal.getsignal(sig) for sig in before}
        await broker.ensure_cell(principal, directory)
        assert path.lstat().st_ino == info.st_ino
        await broker.close()
        assert not path.exists()
        assert before == {sig: signal.getsignal(sig) for sig in before}
        await wire.close()
    run(scenario())


def test_upstream_cookies_and_browser_headers_never_cross_actor_requests(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "openai")
            headers = {"authorization": "Bearer " + grant, "cookie": "browser-secret=yes",
                       "x-chudware-actor": "forged", "x-api-origin": "https://example.com"}
            first = await client.get("/openai/v1/models", headers=headers)
            second = await client.get("/openai/v1/models", headers=headers)
            assert first.status_code == second.status_code == 200
            assert "set-cookie" not in first.headers
            assert len(broker.client.cookies) == 0
            assert all("cookie" not in row.headers and "x-chudware-actor" not in row.headers
                       and "x-api-origin" not in row.headers for row in wire.requests)
    run(scenario())


def test_duplicate_auth_headers_and_encoded_routes_are_refused_before_upstream(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            response = await client.get("/anthropic/v1/models",
                                       headers=[("x-api-key", grant), ("x-api-key", grant)])
            assert response.status_code == 400
            response = await client.get("/anthropic/v1/models",
                                       headers={"x-api-key": grant, "authorization": "Bearer " + grant})
            assert response.status_code == 401
            assert (await client.get("/anthropic%2fv1%2fmodels", headers={"x-api-key": grant})).status_code == 404
            assert wire.requests == []
    run(scenario())


def test_owned_socket_refuses_symlinks_and_live_foreign_broker_without_replacement(broker_env):
    async def scenario():
        principal = cells.resolve_workspace(A)
        directory = cells.prepare_cell(principal)
        path = directory / "provider.sock"
        foreign = directory / "foreign"
        foreign.write_text("not a socket")
        path.symlink_to(foreign)
        broker = provider_broker.ProviderBroker()
        other = provider_broker.ProviderBroker()
        try:
            with pytest.raises(provider_broker.HTTPException) as error:
                await broker.ensure_cell(principal, directory)
            assert error.value.status_code == 403
            assert path.is_symlink() and foreign.read_text() == "not a socket"
            path.unlink()
            await broker.ensure_cell(principal, directory)
            inode = path.lstat().st_ino
            with pytest.raises(provider_broker.HTTPException) as error:
                await other.ensure_cell(principal, directory)
            assert error.value.status_code == 503
            assert path.lstat().st_ino == inode
            assert not broker.listeners[principal.cell_id].task.done()
        finally:
            await other.close()
            await broker.close()
    run(scenario())


def test_slow_body_producer_is_revoked_without_upstream_or_retained_admission(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            gate, producing = asyncio.Event(), asyncio.Event()
            async def chunks():
                yield b'{"hold":false'
                producing.set()
                await gate.wait()
                yield b'}'
            task = asyncio.create_task(client.post("/anthropic/v1/messages", content=chunks(),
                                                   headers={"x-api-key": grant}))
            try:
                await producing.wait()
                async with asyncio.timeout(4):
                    while broker.active != 1:
                        await asyncio.sleep(0.01)
                assert wire.requests == []
                provider_keys.delete_key(A, "anthropic")
                async with asyncio.timeout(4):
                    while broker.active:
                        await asyncio.sleep(0.01)
                assert broker.actors == {} and wire.requests == []
                gate.set()
                try:
                    response = await task
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
                    pass  # Server closed an incomplete HTTP upload, not a provider stream.
                else:
                    assert response.status_code == 401
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_gateway_lifespan_keeps_real_broker_on_owner_loop_across_preview_worker_loops(broker_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def scenario():
        wire = OfficialWire()
        await wire.start()
        broker = provider_broker.ProviderBroker(upstream_origins={"openai": wire.origin})
        monkeypatch.setattr(provider_broker, "_broker", broker)
        principal = cells.resolve_workspace(A)
        directory = cells.prepare_cell(principal)
        grant = broker.mint_grant(A, principal, "openai")

        @asynccontextmanager
        async def lifespan(app):
            await provider_broker.startup()
            try:
                yield
            finally:
                await provider_broker.shutdown()

        async def worker_call():
            await provider_broker.ensure_cell(principal, directory)
            transport = httpx.AsyncHTTPTransport(uds=str(directory / "provider.sock"))
            async with httpx.AsyncClient(transport=transport, base_url="http://cell", trust_env=False) as client:
                response = await client.get("/openai/v1/models", headers={"authorization": "Bearer " + grant})
                assert response.status_code == 200
                assert response.json()["data"][0]["id"] == "fixture-model"
            return (directory / "provider.sock").stat().st_ino

        try:
            with TestClient(FastAPI(lifespan=lifespan)):
                assert broker.owner_loop is not asyncio.get_running_loop()
                first = await asyncio.to_thread(lambda: asyncio.run(worker_call()))
                second = await asyncio.to_thread(lambda: asyncio.run(worker_call()))
                assert first == second
                listener = broker.listeners[principal.cell_id]
                assert not listener.task.done()
                assert listener.task.get_loop() is broker.owner_loop
                assert len(wire.requests) == 2
                await asyncio.to_thread(lambda: asyncio.run(broker.close()))
                assert not (directory / "provider.sock").exists()
            assert provider_broker._broker is None
        finally:
            await wire.close()
    run(scenario())


def test_repeated_cancellation_keeps_stream_count_until_upstream_cleanup(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            cleanup_entered = asyncio.Event()
            cleanup_release = asyncio.Event()

            async def observe_response(response):
                close = response.aclose

                async def delayed_close():
                    await close()
                    cleanup_entered.set()
                    await cleanup_release.wait()

                response.aclose = delayed_close

            broker.client.event_hooks["response"] = [observe_response]
            grant = broker.mint_grant(A, principal, "openai")
            try:
                async with client.stream("POST", "/openai/v1/responses",
                                         json={"hold": True},
                                         headers={"authorization": "Bearer " + grant}) as response:
                    assert response.status_code == 200
                    await first_sse(response.aiter_raw())
                    operation = next(iter(broker.transfers))
                    operation.cancel()
                    await cleanup_entered.wait()
                    assert broker.active == 1
                    operation.cancel()
                    await asyncio.sleep(0)
                    assert not operation.done() and broker.active == 1
                    cleanup_release.set()
                    await asyncio.gather(operation, return_exceptions=True)
                    assert broker.active == 0 and not broker.actors and not broker.transfers
                recovered = await client.get("/openai/v1/models",
                                             headers={"authorization": "Bearer " + grant})
                assert recovered.status_code == 200
                assert recovered.json()["data"][0]["id"] == "fixture-model"
            finally:
                cleanup_release.set()
    run(scenario())


def test_native_anthropic_search_beta_query_crosses_real_uds_with_fixed_auth(broker_env):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            grant = broker.mint_grant(A, principal, "anthropic")
            body = (b'{"model":"fixture-model","stream":true,'
                    b'"tools":[{"type":"web_search_20250305","name":"web_search"}],'
                    b'"messages":[{"role":"user","content":"fixture search"}]}')
            response = await client.post("/anthropic/v1/messages?beta=true", content=body,
                                         headers={"authorization": "Bearer " + grant,
                                                  "content-type": "application/json"})
            assert response.status_code == 200
            assert response.content == SSE_FIRST + SSE_LAST
            assert len(wire.requests) == 1
            observed = wire.requests[0]
            assert observed.path == "/v1/messages" and observed.query == b"beta=true"
            assert observed.body == body
            assert observed.headers["x-api-key"] == KEY_A
            assert "authorization" not in observed.headers
            assert grant not in json.dumps(observed.headers)
    run(scenario())


@pytest.mark.parametrize("path", [
    "/anthropic/v1/messages?beta=false",
    "/anthropic/v1/messages?beta=True",
    "/anthropic/v1/messages?beta=true&beta=true",
    "/anthropic/v1/messages?beta=true&api_key=hostile",
    "/anthropic/v1/messages?beta=true&alt=sse",
    "/anthropic/v1/messages?%62eta=true",
    "/anthropic/v1/messages?beta=%74rue",
    "/anthropic/v1/messages/count_tokens?beta=true",
    "/anthropic/v1/models?beta=true",
    "/openai/v1/responses?beta=true",
    "/xai/v1/chat/completions?beta=true",
    "/openrouter/v1/responses?beta=true",
])
def test_search_beta_exception_never_opens_a_hostile_query_lane(broker_env, path):
    async def scenario():
        async with runtime() as (wire, broker, principal, client):
            provider = path.split("/")[1]
            grant = broker.mint_grant(A, principal, provider)
            response = await client.post(path, json={"model": "fixture-model"},
                                         headers={"authorization": "Bearer " + grant})
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "provider_route_refused"
            assert wire.requests == [] and broker.active == 0
    run(scenario())

"""Wire-byte admission before parsing and network-scoped abuse budgets."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette import formparsers

from gateway import limits, releases
import marketplace


@pytest.mark.parametrize("chunked", [False, True])
def test_real_gateway_rejects_anonymous_auth_body_before_handler(monkeypatch, chunked):
    from gateway import app as gateway_app, authn

    monkeypatch.setattr(gateway_app.settings, "dev_mode", True)
    monkeypatch.setattr(authn, "_check_password_policy",
                        lambda password: pytest.fail("oversized request ran the auth handler"))
    cap = 64 * 1024
    headers = [(b"content-type", b"application/json")]
    if not chunked:
        headers.append((b"content-length", str(cap + 1).encode()))
    status, consumed = _request(gateway_app.app, "/api/auth/register",
                                [b'{"password":"', b"x" * cap, b'"}'], headers=headers)
    assert status == 413
    assert consumed == (2 if chunked else 0)


def _request(app, path, chunks, *, headers=(), method="POST", client="198.51.100.1", user=None):
    consumed = 0
    messages = []

    async def receive():
        nonlocal consumed
        if consumed >= len(chunks):
            return {"type": "http.disconnect"}
        body = chunks[consumed]
        consumed += 1
        return {"type": "http.request", "body": body, "more_body": consumed < len(chunks)}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": list(headers), "client": (client, 1234), "server": ("test", 80)}
    if user is not None:
        scope["state"] = {"user": user}
    asyncio.run(app(scope, receive, send))
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    return starts[0]["status"], consumed


@pytest.mark.parametrize("path,cap", [
    ("/api/auth/register", 64 * 1024),
    ("/api/marketplace/listings/example/reviews", 64 * 1024),
    ("/api/security/events", 64 * 1024),
    ("/api/billing/webhook", 1024 * 1024),
])
@pytest.mark.parametrize("chunked", [False, True])
def test_anonymous_oversized_body_never_runs_json_handler(path, cap, chunked):
    app = FastAPI()
    called = []

    @app.post(path)
    async def consumer(body: dict):
        called.append(body)
        return {"ok": True}

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    chunks = [b'{"data":"', b"x" * cap, b'"}']
    headers = [(b"content-type", b"application/json")]
    if not chunked:
        headers.append((b"content-length", str(cap + 100).encode()))
    status, consumed = _request(app, path, chunks, headers=headers)
    assert status == 413
    assert consumed == (2 if chunked else 0)
    assert called == []


def test_wire_limit_accepts_exact_boundary():
    app = FastAPI()

    @app.post("/api/auth/example")
    async def consumer(request: Request):
        return {"bytes": len(await request.body())}

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    response = TestClient(app).post("/api/auth/example", content=b"x" * (64 * 1024))
    assert response.status_code == 200
    assert response.json() == {"bytes": 64 * 1024}


def test_wire_limit_passes_websockets():
    app = FastAPI()

    @app.websocket("/echo")
    async def typed_echo(socket: WebSocket):
        await socket.accept()
        await socket.send_text("ready")
        await socket.close()

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    with TestClient(app).websocket_connect("/echo") as socket:
        assert socket.receive_text() == "ready"


def test_oversized_multipart_closes_spooled_files(monkeypatch):
    app = FastAPI()
    monkeypatch.setattr(limits.settings, "max_upload_bytes", 256)
    opened = []
    original = formparsers.SpooledTemporaryFile

    def temporary(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", temporary)

    @app.post("/workspace/upload")
    async def upload(request: Request):
        async with limits.bounded_form(request, 256):
            pytest.fail("oversized multipart reached the upload handler")

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    prefix = b'--wire\r\nContent-Disposition: form-data; name="file"; filename="p.step"\r\n\r\n'
    status, consumed = _request(app, "/workspace/upload", [prefix, b"x" * 512, b"--wire--"],
                                headers=[(b"content-type", b"multipart/form-data; boundary=wire")])
    assert status == 413
    assert consumed == 2
    assert opened and all(file.closed for file in opened)


@pytest.mark.parametrize("path,method", [
    ("/api/releases/latest/publish", "POST"),
    ("/api/releases/ci/publish", "POST"),
    ("/api/releases/ci/status", "POST"),
    ("/api/releases/ci/upload/installer.exe", "PUT"),
])
def test_release_token_precedes_any_body_read(monkeypatch, path, method):
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "real-publisher-token")
    monkeypatch.setattr(releases, "_ci_by_ip", limits.RateLimiter(100, 60))
    monkeypatch.setattr(releases, "_publish_by_ip", limits.RateLimiter(100, 60))
    app = FastAPI()
    app.include_router(releases.router)
    status, consumed = _request(app, path, [b"not valid JSON or multipart"], method=method)
    assert status == 401
    assert consumed == 0


def test_admin_release_role_precedes_multipart_read(monkeypatch):
    monkeypatch.setattr(releases, "get_current_user", lambda request: SimpleNamespace(role="user"))
    app = FastAPI()
    app.include_router(releases.router)
    status, consumed = _request(app, "/api/admin/releases/upload", [b"not multipart"],
                                headers=[(b"content-type", b"multipart/form-data")])
    assert status == 403
    assert consumed == 0


@pytest.mark.parametrize("enabled,expected", [(False, 404), (True, 403)])
def test_studio_admission_precedes_multipart_read(monkeypatch, enabled, expected):
    monkeypatch.setattr(marketplace.settings, "marketplace_enabled", enabled)
    monkeypatch.setattr(marketplace, "get_seller", lambda email: None)
    app = FastAPI()
    app.include_router(marketplace.studio_router)
    status, consumed = _request(app, "/api/studio/listings/not-owned/assets", [b"not multipart"],
                                headers=[(b"content-type", b"multipart/form-data")],
                                user=SimpleNamespace(email="nonseller@example.test"))
    assert status == expected
    assert consumed == 0


def test_ipv6_clients_share_network_rate_budget_but_ipv4_stays_exact():
    app = FastAPI()
    limiter = limits.RateLimiter(1, 60)

    @app.post("/api/auth/example")
    async def consumer(request: Request):
        limiter.enforce(limits.request_network(request))
        return {"ok": True}

    assert _request(app, "/api/auth/example", [], client="2001:db8:1:2::1")[0] == 200
    assert _request(app, "/api/auth/example", [], client="2001:db8:1:2:abcd::9")[0] == 429
    assert _request(app, "/api/auth/example", [], client="2001:db8:1:3::1")[0] == 200
    assert _request(app, "/api/auth/example", [], client="198.51.100.1")[0] == 200
    assert _request(app, "/api/auth/example", [], client="198.51.100.2")[0] == 200


def test_forwarded_client_address_is_trusted_only_on_loopback():
    app = FastAPI()
    limiter = limits.RateLimiter(1, 60)

    @app.post("/api/auth/example")
    async def consumer(request: Request):
        limiter.enforce(limits.request_network(request))
        return {"ok": True}

    first = [(b"cf-connecting-ip", b"2001:db8:1:2::1")]
    rotated = [(b"cf-connecting-ip", b"2001:db8:1:2::99")]
    assert _request(app, "/api/auth/example", [], client="127.0.0.1", headers=first)[0] == 200
    assert _request(app, "/api/auth/example", [], client="127.0.0.1", headers=rotated)[0] == 429
    assert _request(app, "/api/auth/example", [], client="198.51.100.4", headers=first)[0] == 200
    assert _request(app, "/api/auth/example", [], client="198.51.100.4", headers=rotated)[0] == 429


def test_limiter_bounds_fresh_key_churn_and_preserves_recent_budget():
    limiter = limits.RateLimiter(1, 60, max_keys=3)
    for index in range(100):
        limiter.enforce(str(index))
        assert len(limiter._hits) <= 3
    assert not limiter.check("99")
    assert not limiter.check("98")
    limiter.enforce("new")
    assert not limiter.check("99")

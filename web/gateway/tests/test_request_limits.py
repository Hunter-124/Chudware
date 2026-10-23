"""Wire-byte admission happens before any parser or handler sees the body."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette import formparsers

from gateway import limits


def _request(app, path, chunks, *, headers=(), method="POST"):
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
             "headers": [(b"host", b"testserver"), *headers], "client": ("127.0.0.1", 1234),
             "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    return starts[0]["status"], consumed


def test_request_body_caps_follow_the_route():
    assert limits.request_body_cap("/workspace/upload") == limits.settings.max_upload_bytes
    assert limits.request_body_cap("/workspace/chat") == limits.WORKSPACE_CHAT_BYTES
    assert limits.request_body_cap("/workspace/mkdir") == limits.SMALL_REQUEST_BYTES


@pytest.mark.parametrize("chunked", [False, True])
def test_real_gateway_rejects_oversized_json_before_handler(gateway_home, monkeypatch, chunked):
    from gateway import app as gateway_app, workspace

    monkeypatch.setattr(workspace, "_resolve",
                        lambda *a, **k: pytest.fail("oversized request ran the handler"))
    cap = limits.SMALL_REQUEST_BYTES
    headers = [(b"content-type", b"application/json")]
    if not chunked:
        headers.append((b"content-length", str(cap + 1).encode()))
    status, consumed = _request(gateway_app.app, "/workspace/mkdir",
                                [b'{"path":"', b"x" * cap, b'"}'], headers=headers)
    assert status == 413
    assert consumed == (2 if chunked else 0)


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_body_never_runs_json_handler(chunked):
    app = FastAPI()
    called = []

    @app.post("/workspace/rename")
    async def consumer(body: dict):
        called.append(body)
        return {"ok": True}

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    cap = limits.SMALL_REQUEST_BYTES
    headers = [(b"content-type", b"application/json")]
    if not chunked:
        headers.append((b"content-length", str(cap + 100).encode()))
    status, consumed = _request(app, "/workspace/rename", [b'{"data":"', b"x" * cap, b'"}'],
                                headers=headers)
    assert status == 413
    assert consumed == (2 if chunked else 0)
    assert called == []


def test_conflicting_content_lengths_are_refused():
    app = FastAPI()
    app.add_middleware(limits.RequestBodyLimitMiddleware)
    status, consumed = _request(app, "/workspace/mkdir", [b"{}"],
                                headers=[(b"content-length", b"2"), (b"content-length", b"3")])
    assert status == 400
    assert consumed == 0


def test_wire_limit_accepts_exact_boundary():
    app = FastAPI()

    @app.post("/workspace/example")
    async def consumer(request: Request):
        return {"bytes": len(await request.body())}

    app.add_middleware(limits.RequestBodyLimitMiddleware)
    response = TestClient(app).post("/workspace/example", content=b"x" * limits.SMALL_REQUEST_BYTES)
    assert response.status_code == 200
    assert response.json() == {"bytes": limits.SMALL_REQUEST_BYTES}


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

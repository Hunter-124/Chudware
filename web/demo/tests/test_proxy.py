"""Reverse proxy to a (fake) session container over real loopback sockets."""

from __future__ import annotations

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from demo.proxy import safe_target

from .support import LOW, FakeClock, FakeInner, front_door


@pytest.fixture
def inner():
    fake = FakeInner().start()
    yield fake
    fake.stop()


def admitted(tmp_path, inner, **kwargs):
    """A front door whose first admitted session is the fake inner gateway."""
    return front_door(tmp_path, inner=inner, **kwargs)


def join_and_activate(fd, ip="198.51.100.1"):
    visitor = fd.visitor(ip)
    assert visitor.join().status_code == 200
    visitor.wait_for("active")
    return visitor


def test_http_is_proxied_to_that_session_only_with_isolation_headers(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        response = visitor.get("/app/", headers={
            "Authorization": "Bearer visitor-token", "cf-connecting-ip": "203.0.113.7",
            "X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.7", "X-Custom": "abc",
            "Cf-Ray": "deadbeef", "Accept-Encoding": "identity"})
        assert response.status_code == 200 and "<title>inner app</title>" in response.text
        # Cross-origin isolation headers survive; the inner gateway's identity does not.
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["cross-origin-embedder-policy"] == "require-corp"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["x-inner"] == "yes"
        assert "set-cookie" not in response.headers and "inner-gateway" not in response.headers.get("server", "")
        seen = inner.seen[-1]["headers"]
        assert seen["host"] == f"127.0.0.1:{inner.port}"
        assert seen["x-custom"] == "abc"
        for leaked in ("cookie", "authorization", "cf-connecting-ip", "x-forwarded-for", "x-real-ip", "cf-ray"):
            assert leaked not in seen, leaked


@pytest.mark.parametrize("path", ["/files/report.step", "/reveng/artifact/1", "/workspace/list"])
def test_every_contract_prefix_is_proxied(tmp_path, inner, path):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        assert visitor.get(path + "?x=1").status_code == 200
        assert inner.seen[-1]["path"] == path and inner.seen[-1]["query"] == "x=1"
        assert visitor.get("/healthz").json() == {"ok": True, "demo": True}


def test_post_origin_is_rewritten_so_the_inner_same_origin_check_passes(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        payload = b'{"name": "part.step+"}'
        response = visitor.post("/workspace/echo", content=payload, headers={
            "Origin": "https://app.chudware.com", "Content-Type": "application/json"})
        assert response.json() == {"len": len(payload), "echo": payload.decode()}
        seen = inner.seen[-1]["headers"]
        assert seen["origin"] == f"http://127.0.0.1:{inner.port}" and seen["host"] == f"127.0.0.1:{inner.port}"
        assert seen["content-length"] == str(len(payload))
        # No Origin in, no Origin out (non-browser clients stay non-browser).
        visitor.post("/workspace/echo", content=b"x")
        assert "origin" not in inner.seen[-1]["headers"]
        assert visitor.post("/workspace/echo").json()["len"] == 0


def test_redirect_locations_never_reveal_the_inner_address(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        redirect = visitor.get("/app")
        assert redirect.status_code == 307 and redirect.headers["location"] == "/app/"


def test_large_bodies_stream_through_unchanged(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        blob = visitor.get("/app/blob", headers={"Accept-Encoding": "identity"})
        assert blob.content == inner.blob and blob.headers["content-length"] == str(len(inner.blob))
        big = b"y" * (3 * 1024 * 1024)
        assert visitor.post("/workspace/echo", content=big).json()["len"] == len(big)


def test_request_bodies_above_the_cap_are_refused(tmp_path, inner):
    with admitted(tmp_path, inner, max_request_bytes=1000) as fd:
        visitor = join_and_activate(fd)
        assert visitor.post("/workspace/echo", content=b"z" * 5000).status_code == 413
        assert visitor.post("/workspace/echo", content=b"z" * 500).status_code == 200


def test_only_admitted_visitors_are_ever_proxied(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        owner = join_and_activate(fd)
        stranger = fd.visitor("198.51.100.50")
        for method, path in (("GET", "/workspace/list"), ("GET", "/files/x"), ("GET", "/reveng/x"),
                             ("GET", "/healthz"), ("POST", "/workspace/echo"), ("GET", "/app/x.js")):
            response = stranger.request(method, path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "no_session"
        for path in ("/app", "/app/"):                       # navigations go back to the waiting page
            nav = stranger.get(path)
            assert nav.status_code == 307 and nav.headers["location"] == "/"
        assert inner.seen == []                              # nothing ever reached the container
        assert owner.get("/workspace/list").status_code == 200


def test_a_queued_visitor_is_not_proxied_even_with_a_genuine_cookie(tmp_path, inner):
    with admitted(tmp_path, inner, capacity=LOW) as fd:
        visitor = fd.visitor()
        visitor.join()
        assert visitor.state() == "queued"
        assert visitor.get("/workspace/list").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with fd.client.websocket_connect("/engine", headers=visitor.ws_headers()):
                pass
        assert inner.seen == [] and inner.ws_headers == []


def test_tampered_cookie_is_not_proxied(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        owner = join_and_activate(fd)
        sid, mac = owner.cookie.split(".")
        for cookie in (sid + "." + "A" * 43, "0" * 24 + "." + mac, sid):
            thief = fd.visitor("198.51.100.99", cookie=cookie)
            assert thief.get("/workspace/list").status_code == 401
            with pytest.raises(WebSocketDisconnect):
                with fd.client.websocket_connect("/engine", headers=thief.ws_headers()):
                    pass
        assert inner.seen == [] and inner.ws_headers == []


@pytest.mark.parametrize("path", [
    "/app/%2e%2e/%2e%2e/etc/passwd", "/app/..%5cwindows", "/files/%00", "/workspace/%2E%2E/x",
    "/app/a%0d%0ab"])
def test_traversal_and_control_characters_are_refused(tmp_path, inner, path):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        # 400 from the proxy, or 404 where the router's own pattern already refuses (CR/LF).
        assert visitor.get(path).status_code in (400, 404)
        assert inner.seen == []


def test_unrouted_paths_never_reach_the_container(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        for path in ("/admin", "/openapi.json", "/docs", "/filesx/a", "/proc/self/environ", "/demo/static"):
            assert visitor.get(path).status_code == 404
        assert visitor.request("OPTIONS", "/workspace/list").status_code == 405
        assert inner.seen == []


def test_safe_target_unit():
    assert safe_target(b"/app/index.html", b"v=1") == "/app/index.html?v=1"
    assert safe_target(b"/app", b"") == "/app"
    assert safe_target(b"/engine", b"") == "/engine"
    assert safe_target(b"/app/a%20b", b"") == "/app/a%20b"
    for bad in (b"//evil/app/", b"/etc/passwd", b"/app/../x", b"/app/%2e%2e/x", b"app/x", b"/app/\\x", b"/app/\xc3\xa9", b"/app/./x"):
        assert safe_target(bad, b"") is None, bad
    assert safe_target(b"/app/x", b"a=\r\nb") is None


def test_proxied_traffic_is_activity_but_healthz_and_status_polls_are_not(tmp_path, inner):
    clock = FakeClock()
    with admitted(tmp_path, inner, clock=clock) as fd:
        visitor = join_and_activate(fd)
        entry = next(iter(fd.manager.entries.values()))
        started = entry.last_activity
        clock.advance(600)
        visitor.get("/healthz")
        visitor.get("/demo/status")
        assert entry.last_activity == started
        visitor.get("/workspace/list")
        assert entry.last_activity == started + 600
        clock.advance(800)                                   # 800 s idle: still inside the 900 s limit
        time.sleep(0.2)
        assert visitor.state() == "active"
        visitor.post("/workspace/echo", content=b"x")
        clock.advance(800)
        time.sleep(0.2)
        assert visitor.state() == "active"
        clock.advance(200)                                   # 1000 s since the last request
        assert visitor.wait_for("expired")["note"] == "idle"


def test_websocket_is_proxied_both_ways_with_rewritten_origin_and_host(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        with fd.client.websocket_connect("/engine", headers=visitor.ws_headers()) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
            ws.send_bytes(b"\x00\x01\x02")
            assert ws.receive_bytes() == b"echo:\x00\x01\x02"
        handshake = inner.ws_headers[-1]
        assert handshake["origin"] == f"http://127.0.0.1:{inner.port}"
        assert handshake["host"] == f"127.0.0.1:{inner.port}"
        assert "cookie" not in handshake and "cf-connecting-ip" not in handshake


def test_cross_origin_websocket_is_refused(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        headers = visitor.ws_headers({"origin": "https://evil.example"})
        with pytest.raises(WebSocketDisconnect):
            with fd.client.websocket_connect("/engine", headers=headers):
                pass
        assert inner.ws_headers == []


def test_websocket_frames_are_activity(tmp_path, inner):
    clock = FakeClock()
    with admitted(tmp_path, inner, clock=clock) as fd:
        visitor = join_and_activate(fd)
        entry = next(iter(fd.manager.entries.values()))
        with fd.client.websocket_connect("/engine", headers=visitor.ws_headers()) as ws:
            clock.advance(500)
            ws.send_text("ping")
            assert ws.receive_text() == "echo:ping"
            assert entry.last_activity == clock()


def test_open_websockets_are_closed_when_the_session_ends(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        with fd.client.websocket_connect("/engine", headers=visitor.ws_headers()) as ws:
            ws.send_text("alive")
            assert ws.receive_text() == "echo:alive"
            assert visitor.end().json()["state"] == "expired"
            with pytest.raises(WebSocketDisconnect):
                for _ in range(50):
                    ws.receive_text()
        assert visitor.get("/workspace/list").status_code == 401


def test_a_dead_container_is_a_502_not_a_crash(tmp_path, inner):
    with admitted(tmp_path, inner) as fd:
        visitor = join_and_activate(fd)
        inner.stop()
        response = visitor.get("/workspace/list")
        assert response.status_code == 502 and response.json()["error"]["code"] == "session_unreachable"

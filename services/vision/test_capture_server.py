"""Loopback-only capture fixtures: bounded pre-auth work and upload limits."""
import http.client
import io
import socket
import threading
import time
from email.message import Message
from pathlib import Path

import pytest

from services.vision import capture_server as capture


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    current = capture.start_session(port=0, token="fixture-token", bind_host="127.0.0.1")
    try:
        yield current
    finally:
        capture.stop_session(current)


def request(session, method="GET", body=None, headers=None, token="fixture-token"):
    client = http.client.HTTPConnection("127.0.0.1", session.port, timeout=1)
    try:
        client.request(method, "/upload?token=" + token, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, response.read()
    finally:
        client.close()


def test_incomplete_headers_do_not_block_authenticated_peer_or_shutdown(session):
    slow = socket.create_connection(("127.0.0.1", session.port), timeout=1)
    try:
        slow.sendall(b"GET / HTTP/1.1\r\nX-Incomplete: ")
        status, body = request(session)
        assert status == 200 and b"Photo Capture" in body
        started = time.monotonic()
        capture.stop_session(session)
        assert time.monotonic() - started < 1.5
        assert slow.recv(1) == b""
    finally:
        slow.close()


def test_absolute_handshake_deadline_expires_despite_trickle(session, monkeypatch):
    monkeypatch.setattr(capture, "_HANDSHAKE_TIMEOUT_S", 0.3)
    slow = socket.create_connection(("127.0.0.1", session.port), timeout=1)
    try:
        slow.sendall(b"GET / HTTP/1.1\r\nX-Incomplete: ")
        time.sleep(0.15)
        slow.sendall(b"x")
        time.sleep(0.2)
        assert slow.recv(1) == b""
        assert request(session)[0] == 200
    finally:
        slow.close()


def test_total_header_budget_closes_unauthenticated_peer(session):
    peer = socket.create_connection(("127.0.0.1", session.port), timeout=1)
    try:
        peer.sendall(b"GET / HTTP/1.1\r\n" + (b"X: " + b"x" * 900 + b"\r\n") * 20)
        try:
            reply = peer.recv(1)
        except ConnectionResetError:
            reply = b""
        assert reply == b""
        assert request(session)[0] == 200
    finally:
        peer.close()


def test_connection_slots_are_bounded_and_recover(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_MAX_CONNECTIONS", 2)
    monkeypatch.setattr(capture, "_HANDSHAKE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(capture.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    current = capture.start_session(0, "fixture-token", "127.0.0.1")
    peers = []
    try:
        for _ in range(2):
            peer = socket.create_connection(("127.0.0.1", current.port), timeout=1)
            peer.sendall(b"GET / HTTP/1.1\r\nX: ")
            peers.append(peer)
        extra = socket.create_connection(("127.0.0.1", current.port), timeout=1)
        peers.append(extra)
        assert extra.recv(1) == b""
        time.sleep(0.4)
        assert request(current)[0] == 200
    finally:
        for peer in peers:
            peer.close()
        capture.stop_session(current)


def multipart():
    return b'--fixture\r\nContent-Disposition: form-data; name="photo"; filename="photo.png"\r\nContent-Type: image/png\r\n\r\nfixture-photo\r\n--fixture--\r\n'


def test_legitimate_photo_and_token_policy_preserved(session):
    body = multipart()
    headers = {"Content-Type": "multipart/form-data; boundary=fixture"}
    assert request(session, "POST", body, headers, token="wrong")[0] == 401
    assert not session.received
    assert request(session, "POST", body, headers)[0] == 200
    assert Path(session.received[0]).name == "000_top.png"
    assert Path(session.received[0]).read_bytes() == b"fixture-photo"
    assert request(session)[0] == 200


@pytest.mark.parametrize("length", ["not-a-number", "-1", str(capture._MAX_UPLOAD_BYTES + 1)])
def test_invalid_upload_lengths_refused_before_body_read(length):
    headers = Message()
    headers["Content-Type"] = "multipart/form-data; boundary=fixture"
    headers["Content-Length"] = length
    body = io.BytesIO(b"unread")
    if length == "-1":
        assert capture._parse_multipart_photo(headers, body) == (None, None)
    else:
        with pytest.raises(ValueError):
            capture._parse_multipart_photo(headers, body)
    assert body.tell() == 0


def test_upload_count_and_byte_limits_refuse_without_writing(session, monkeypatch):
    headers = {"Content-Type": "multipart/form-data; boundary=fixture"}
    monkeypatch.setattr(capture, "_MAX_UPLOAD_COUNT", 0)
    assert request(session, "POST", multipart(), headers)[0] == 400
    assert not list(Path(session.work_dir).iterdir())
    monkeypatch.setattr(capture, "_MAX_UPLOAD_COUNT", 128)
    monkeypatch.setattr(capture, "_MAX_SESSION_BYTES", 1)
    assert request(session, "POST", multipart(), headers)[0] == 400
    assert not list(Path(session.work_dir).iterdir())


def test_trickling_authenticated_body_expires_without_publishing(session, monkeypatch):
    monkeypatch.setattr(capture, "_BODY_TIMEOUT_S", 0.2)
    peer = socket.create_connection(("127.0.0.1", session.port), timeout=1)
    stopped = threading.Event()

    def trickle():
        while not stopped.wait(0.025):
            try:
                peer.sendall(b"x")
            except OSError:
                return

    sender = threading.Thread(target=trickle)
    try:
        peer.sendall(b"POST /upload?token=fixture-token HTTP/1.0\r\nContent-Type: multipart/form-data; boundary=fixture\r\nContent-Length: 1000\r\n\r\npartial")
        sender.start()
        reply = bytearray()
        while True:
            try:
                chunk = peer.recv(4096)
            except ConnectionResetError:
                break
            if not chunk:
                break
            reply.extend(chunk)
            assert len(reply) <= 4096
        # The absolute shutdown and read timeout can race: EOF and an HTTP
        # refusal are both terminal rejection, never a successfully stored photo.
        if reply:
            status = int(reply.split(b"\r\n", 1)[0].split()[1])
            assert 400 <= status < 500
        assert not session.received
        assert not list(Path(session.work_dir).iterdir())
        assert request(session)[0] == 200
    finally:
        stopped.set()
        peer.close()
        if sender.ident is not None:
            sender.join(timeout=1)
            assert not sender.is_alive()


def test_wildcard_listener_refused():
    with pytest.raises(ValueError):
        capture.start_session(0, bind_host="0.0.0.0")

"""Offline boundary tests: public fixture transport is redirected only by the test."""
import gzip
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.net import safe_http as http


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_CONNECT(self):
            seen.append(("CONNECT", self.path, self.request_version))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            seen.append(self.path)
            if self.path == "/private":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/marker")
                payload = b""
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                payload = b""
            elif self.path == "/rebind":
                self.send_response(302)
                self.send_header("Location", "/marker")
                payload = b""
            else:
                self.send_response(200)
                payload = b"fixture public content" if self.path == "/ok" else b"x" * 65536
                if self.path == "/gzip":
                    payload = gzip.compress(payload)
                    self.send_header("Content-Encoding", "gzip")
            if self.path != "/chunked":
                self.send_header("Content-Length", str(len(payload)))
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.path == "/idle":
                time.sleep(0.2)
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, seen
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


@pytest.fixture
def public_fixture(monkeypatch, server):
    srv, seen = server
    original_connect = http._connect
    connections = []
    resolutions = []

    def resolve(host, port, **_):
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    def connect(address, timeout):
        connections.append(address[4])
        # Only this test connector maps the approved public fixture IP to localhost.
        assert address[4][0] == "93.184.216.34"
        return original_connect((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                 "", ("127.0.0.1", srv.server_port)), timeout)

    monkeypatch.setattr(http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(http, "_connect", connect)
    monkeypatch.setattr(http, "_cell_mode", lambda: False)
    return f"http://public.fixture:{srv.server_port}", seen, connections, resolutions


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.0.1",
                                   "0.0.0.0", "224.0.0.1", "[::1]", "[fc00::1]",
                                   "[fe80::1]", "[ff02::1]", "[::ffff:127.0.0.1]"])
def test_private_literals_refused_before_connection(host, monkeypatch, server):
    srv, seen = server
    monkeypatch.setattr(http, "_connect", lambda *_: pytest.fail("private connection attempted"))
    with pytest.raises(http.OutboundPolicyError):
        http.get(f"http://{host}:{srv.server_port}/marker")
    assert seen == []


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "::ffff:10.0.0.1"])
def test_private_dns_and_mixed_answers_refused(address, monkeypatch, server):
    srv, seen = server
    monkeypatch.setattr(http.socket, "getaddrinfo", lambda *_, **__: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", srv.server_port)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, srv.server_port))])
    monkeypatch.setattr(http, "_connect", lambda *_: pytest.fail("private connection attempted"))
    with pytest.raises(http.OutboundPolicyError):
        http.get("http://public.fixture/marker")
    assert seen == []


def test_public_redirect_fixture_works_and_pins_without_second_dns(public_fixture, monkeypatch):
    url, seen, connections, resolutions = public_fixture
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response = http.get(url + "/redirect")
    assert response.content == b"fixture public content"
    assert seen == ["/redirect", "/ok"]
    assert len(resolutions) == len(connections) == 2
    assert all(address[0] == "93.184.216.34" for address in connections)


def test_nonrendered_public_research_remains_functional(public_fixture, monkeypatch, tmp_path):
    import importlib
    from services.research.cache import Cache
    module = importlib.import_module("services.research.fetch_page")
    url, seen, _, _ = public_fixture
    class FixtureTLS:
        def wrap_socket(self, sock, *, server_hostname):
            assert server_hostname == "public.fixture"
            return sock  # local fixture only; production uses hostname-verifying TLS
    monkeypatch.setattr(http.ssl, "create_default_context", FixtureTLS)
    monkeypatch.setattr(module, "is_offline", lambda: False)
    monkeypatch.setattr(module, "_render_page", lambda *_: pytest.fail("static content rendered"))
    result = module.fetch_page(url.replace("http://", "https://") + "/ok", cache=Cache(root=tmp_path))
    assert result["ok"] is True and result["cached"] is False
    assert result["content"] == "fixture public content"
    assert seen == ["/ok"]


def test_rebinding_cannot_trigger_second_resolution_inside_connect(public_fixture, monkeypatch):
    url, seen, connections, _ = public_fixture
    resolutions = []
    def resolver(host, port, **_):
        resolutions.append(host)
        ip = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    monkeypatch.setattr(http.socket, "getaddrinfo", resolver)
    assert http.get(url + "/ok").content == b"fixture public content"
    assert resolutions == ["public.fixture"]
    assert seen == ["/ok"] and len(connections) == 1


def test_private_redirect_refused_before_next_connection(public_fixture):
    url, seen, connections, _ = public_fixture
    with pytest.raises(http.OutboundPolicyError):
        http.get(url + "/private")
    assert seen == ["/private"]
    assert len(connections) == 1


def test_rebinding_between_hops_refused(public_fixture, monkeypatch):
    url, seen, connections, _ = public_fixture
    calls = []

    def rebinding(host, port, **_):
        calls.append(host)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    monkeypatch.setattr(http.socket, "getaddrinfo", rebinding)
    with pytest.raises(http.OutboundPolicyError):
        http.get(url + "/rebind")
    assert seen == ["/rebind"]
    assert len(connections) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("path", ["/large", "/chunked", "/gzip"])
def test_declared_streamed_and_decoded_budgets(public_fixture, tmp_path, path):
    url, _, _, _ = public_fixture
    dest = tmp_path / "artifact.step"
    with pytest.raises(http.ResponseTooLarge):
        http.download(url + path, dest, max_bytes=1024)
    assert not dest.exists()


def test_response_idle_budget_stops_waiting_and_removes_partial_artifact(public_fixture, tmp_path):
    url, seen, _, _ = public_fixture
    destination = tmp_path / "artifact.step"
    with pytest.raises(TimeoutError):
        http.download(url + "/idle", destination, idle_timeout=0.03)
    assert seen == ["/idle"]
    assert not destination.exists()


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://public.fixture/part.step",
                                 "https://user:password@public.fixture/part.step"])
def test_scheme_and_userinfo_policy_precedes_dns(url, monkeypatch):
    monkeypatch.setattr(http.socket, "getaddrinfo", lambda *_, **__: pytest.fail("invalid URL resolved"))
    with pytest.raises(http.OutboundPolicyError):
        http.get(url)


@pytest.mark.parametrize("leaf", [r"..\..\outside.step", "C:outside.step", "..", "%2e%2e%2foutside.step",
                                   "%5coutside.step", "%2foutside.step", "%5c%5cserver%5cshare%5coutside.step",
                                   "outside.step.", "outside.step%20", "outside.step%00",
                                   "outside%0astep", "outside%3fstream.step", "part.step%3astream",
                                   "CON.step", "nul", "PRN.zip", "AUX.pdf", "com1.step", "LPT9.stp",
                                   "COM%C2%B9.step", "LPT%C2%B2.zip", "CONIN%24", "CONOUT%24",
                                   "CON%20.step", "%ff.step"])
def test_windows_and_posix_filename_traversal_refused(tmp_path, leaf):
    with pytest.raises(http.OutboundPolicyError):
        http.safe_download_path(tmp_path, "https://public.fixture/" + leaf)


def test_safe_filename_and_symlink_escape(tmp_path):
    assert http.safe_download_path(tmp_path, "https://public.fixture/models/part.step?download=1") == tmp_path / "part.step"
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    (tmp_path / "part.step").symlink_to(outside)
    with pytest.raises(http.OutboundPolicyError):
        http.safe_download_path(tmp_path, "https://public.fixture/part.step")


@pytest.mark.parametrize("url, expected", [
    ("https://public.fixture/models/part.step?next=/..\\..\\evil.zip#C:\\escape", "part.step"),
    ("https://public.fixture/models/part.step#fragment/CON.zip", "part.step"),
    ("https://public.fixture/models/%E8%BD%B4%20bracket.STEP?signature=a%2Fb", "轴 bracket.STEP"),
    ("https://public.fixture/models/100%25%20complete.stp", "100% complete.stp"),
    ("https://public.fixture/models/?filename=evil.zip#evil.step", "fallback.iges"),
])
def test_filename_uses_only_url_path_and_preserves_unicode(tmp_path, url, expected):
    destination = http.safe_download_path(tmp_path, url, default="fallback.iges")
    assert destination == tmp_path / expected
    assert destination.parent == tmp_path


@pytest.mark.parametrize("name", [
    "../outside.step", "/outside.step", r"C:\outside.step", "C:outside.step",
    r"\\server\share\outside.step", r"..\..\outside.step", "%2e%2e%2foutside.step",
    "%5coutside.step", "part.step:stream", "CON.step", "NUL.pdf", "LPT¹.stp",
    "part.step.", "part.step ", "part\x00.step", "",
])
@pytest.mark.parametrize("selection", ["default", "filename"])
def test_catalog_and_fallback_names_share_portable_boundary(tmp_path, name, selection):
    url = "https://public.fixture/models/" if selection == "default" else "https://public.fixture/download"
    with pytest.raises(http.OutboundPolicyError):
        http.safe_download_path(tmp_path, url, **{selection: name})


def test_catalog_filename_preserves_extension_and_safe_unicode(tmp_path):
    assert http.safe_download_path(
        tmp_path, "https://public.fixture/download?filename=outside.zip",
        filename="42-7-轴 bracket.STEP") == tmp_path / "42-7-轴 bracket.STEP"
    assert http.safe_download_path(
        tmp_path, "https://public.fixture/download",
        filename="100%25 complete.stp") == tmp_path / "100%25 complete.stp"
    with pytest.raises(http.OutboundPolicyError):
        http.safe_download_path(tmp_path, "https://public.fixture/%5coutside.step",
                                filename="safe.step")


def test_cell_uses_only_fixed_broker_and_refuses_http(monkeypatch):
    monkeypatch.setattr(http, "_cell_mode", lambda: True)
    monkeypatch.setattr(http.socket, "getaddrinfo", lambda *_, **__: pytest.fail("cell must not resolve DNS"))
    parsed, addresses = http.resolve_public("https://api.tavily.com/search")
    assert parsed.hostname == "api.tavily.com" and addresses == []
    with pytest.raises(http.OutboundPolicyError):
        http.get("http://api.tavily.com/")


def test_cell_connects_fixed_broker_with_hostname_and_http11(monkeypatch, server):
    srv, seen = server
    original_connect = http._connect
    connects = []
    monkeypatch.setattr(http, "_cell_mode", lambda: True)
    monkeypatch.setattr(http.socket, "getaddrinfo",
                        lambda *_, **__: pytest.fail("unexpected cell DNS"))
    def connect(address, timeout):
        connects.append(address[4])
        assert address[4] == ("127.0.0.1", 18080)
        return original_connect((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                 "", ("127.0.0.1", srv.server_port)), timeout)
    class FixtureTLS:
        def wrap_socket(self, sock, *, server_hostname):
            assert server_hostname == "public.fixture"
            return sock  # fixture only: production retains certificate verification
    monkeypatch.setattr(http, "_connect", connect)
    monkeypatch.setattr(http.ssl, "create_default_context", FixtureTLS)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response = http.get("https://public.fixture/ok")
    assert response.content == b"fixture public content"
    assert connects == [("127.0.0.1", 18080)]
    assert seen == [("CONNECT", "public.fixture:443", "HTTP/1.1"), "/ok"]


def test_catalog_and_harvest_confine_real_loopback_downloads(public_fixture, monkeypatch, tmp_path):
    """Actual selectors/name authority plus real streamed I/O; no browser/profile or outside network."""
    from services.browser import adapters, session
    from services.browser.adapters import thk
    from services.reveng import worker_rpc

    url, seen, connections, _ = public_fixture
    catalog_root = tmp_path / "catalog"
    harvest_root = tmp_path / "harvest"
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", catalog_root)

    def download(target, site, destination):
        response = http.download(target, destination, max_bytes=128 * 1024,
                                 schemes=("http",))  # Controlled loopback fixture only.
        return {"ok": True, "dest": str(destination), "bytes": response.bytes_written,
                "session_profile_used": site}

    monkeypatch.setattr(session, "browser_download", download)
    hostile = url + "/files/%2e%2e%5coutside.step"
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": f'<a href="{hostile}">Download CAD</a>'})
    refused = thk.ADAPTER.acquire({"url": "https://www.thk.com/fixture"})
    assert refused["ok"] is False and refused["reason"] == "unsafe_filename"
    assert worker_rpc._download_into(hostile, "fixture", harvest_root) == (None, None)
    assert seen == connections == []
    assert not harvest_root.exists()

    benign = (url + "/models/%E8%BD%B4%20bracket.step"
              + "?signature=a%2Fb&next=/..%5C..%5Cevil.step#C:%5Calias")
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": f'<a href="{benign}">Download CAD</a>'})
    acquired = thk.ADAPTER.acquire({"url": "https://www.thk.com/fixture"})
    harvested, refusal = worker_rpc._download_into(benign, "fixture", harvest_root)
    catalog_dest = catalog_root / "thk" / "轴 bracket.step"
    harvest_dest = harvest_root / "轴 bracket.step"
    assert acquired["ok"] is True and acquired["artifact_path"] == str(catalog_dest)
    assert harvested == harvest_dest and refusal is None
    assert catalog_dest.read_bytes() == harvest_dest.read_bytes() == b"x" * 65536
    assert list(catalog_root.rglob("*.step")) == [catalog_dest]
    assert list(harvest_root.rglob("*.step")) == [harvest_dest]
    assert len(seen) == len(connections) == 2

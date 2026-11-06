"""Robots politeness cannot bypass public destination or response budgets."""
import io
import socket

import pytest

from services.browser import ratelimit
from services.net import safe_http


def test_robots_private_hostname_answer_never_connects(monkeypatch):
    monkeypatch.setattr(safe_http, "_cell_mode", lambda: False)
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda *_, **__: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(safe_http, "_connect", lambda *_: pytest.fail("private robots requested"))
    assert ratelimit._fetch_robots_crawl_delay("public.fixture") is None


@pytest.mark.parametrize("size, expected", [(0, 7.0), (65537, None)])
def test_robots_public_response_budget(monkeypatch, size, expected):
    monkeypatch.setattr(safe_http, "_cell_mode", lambda: False)
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda *_, **__: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
    body = b"User-agent: *\nCrawl-delay: 7\n" if not size else b"x" * size
    class Response(io.BytesIO):
        status = 200
        def getheader(self, name):
            return str(len(body)) if name == "Content-Length" else None
        def getheaders(self):
            return [("Content-Length", str(len(body)))]
    class Connection:
        def __init__(self, parsed, address, timeout):
            assert address[4][0] == "93.184.216.34"
        def request(self, *_, **__):
            pass
        def getresponse(self):
            return Response(body)
        def close(self):
            pass
    monkeypatch.setattr(safe_http, "_PinnedConnection", Connection)
    assert ratelimit._fetch_robots_crawl_delay("public.fixture") == expected


def test_robots_crawl_delay_is_cached_per_origin(monkeypatch):
    metadata = {}
    reads = []
    monkeypatch.setattr(ratelimit.profiles, "load_meta", lambda _site: dict(metadata))
    monkeypatch.setattr(ratelimit.profiles, "save_meta", lambda _site, **values: metadata.update(values))
    monkeypatch.setattr(ratelimit, "_fetch_robots_crawl_delay",
                        lambda origin: reads.append(origin) or float(len(reads)))
    assert ratelimit.crawl_delay("vendor", "https://a.example/p") == 1
    assert ratelimit.crawl_delay("vendor", "https://a.example:443/other") == 1
    assert ratelimit.crawl_delay("vendor", "https://b.example/p") == 2
    assert ratelimit.crawl_delay("vendor", "http://a.example/p") == 3
    assert ratelimit.crawl_delay("vendor", "https://a.example:8443/p") == 4
    assert reads == ["https://a.example", "https://b.example",
                     "http://a.example", "https://a.example:8443"]

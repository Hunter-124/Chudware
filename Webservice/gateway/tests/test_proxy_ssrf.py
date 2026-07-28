"""Regression tests for the proxy SSRF / header-leak / auth-gate findings."""

from __future__ import annotations

import pytest

try:
    from ..proxy import build_upstream_url, scrub_request_headers
except ImportError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from proxy import build_upstream_url, scrub_request_headers

BASE = "http://127.0.0.1:8400"


class TestUpstreamUrl:
    @pytest.mark.parametrize(
        "path",
        [
            "http://192.168.0.1/",       # absolute URL replaces base via urljoin
            "//192.168.0.1/",            # scheme-relative
            "https://evil.test/x",
            "http://169.254.169.254/latest/meta-data",
            "a/b\r\nHost: evil",         # decoded %0d%0a — request smuggling
            "vnc.html\x00.png",          # NUL
            "vnc\x7f.html",              # DEL
        ],
    )
    def test_rejects_hostile_paths(self, path: str) -> None:
        with pytest.raises(ValueError):
            build_upstream_url(BASE, path)

    def test_rejects_crlf_in_query(self) -> None:
        with pytest.raises(ValueError):
            build_upstream_url(BASE, "vnc.html", "x=1\r\nInjected: yes")

    @pytest.mark.parametrize(
        "path,query,expected",
        [
            ("vnc.html", "", "http://127.0.0.1:8400/vnc.html"),
            ("a/b/c.js", "", "http://127.0.0.1:8400/a/b/c.js"),
            ("", "", "http://127.0.0.1:8400/"),
            ("vnc.html", "autoconnect=true",
             "http://127.0.0.1:8400/vnc.html?autoconnect=true"),
            ("/vnc.html", "", "http://127.0.0.1:8400/vnc.html"),
        ],
    )
    def test_accepts_relative_paths(
        self, path: str, query: str, expected: str
    ) -> None:
        url = build_upstream_url(BASE, path, query)
        assert url == expected
        # Invariant: the result can only ever live under base.
        assert url.startswith(BASE + "/")


class TestScrubRequestHeaders:
    def test_drops_identity_and_forwarding_headers(self) -> None:
        inbound = {
            "authorization": "Basic dXNlcjpwYXNz",
            "cf-access-jwt-assertion": "eyJhbGciOi...",
            "cf-access-authenticated-user-email": "victim@ua.edu",
            "x-forwarded-email": "victim@ua.edu",
            "x-forwarded-for": "203.0.113.9",
            "x-forwarded-host": "cad.example.com",
            "x-real-ip": "203.0.113.9",
            "proxy-authorization": "Basic cHJveHk6cGFzcw==",
            "cookie": "session=abc",
            "x-chudware-user": "attacker@ua.edu",
            "host": "cad.example.com",
            "connection": "keep-alive",
            "accept": "text/html",
        }
        out = scrub_request_headers(inbound)
        lowered = {k.lower() for k in out}
        for dropped in (
            "authorization",
            "cf-access-jwt-assertion",
            "cf-access-authenticated-user-email",
            "x-forwarded-email",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-real-ip",
            "proxy-authorization",
            "cookie",
            "x-chudware-user",
            "host",
            "connection",
        ):
            assert dropped not in lowered, f"{dropped} leaked upstream"

    def test_keeps_ordinary_headers(self) -> None:
        out = scrub_request_headers({"accept": "text/html", "Accept-Language": "en"})
        lowered = {k.lower(): v for k, v in out.items()}
        assert lowered["accept"] == "text/html"
        assert lowered["accept-language"] == "en"

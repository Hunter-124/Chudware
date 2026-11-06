"""Per-source browser integration tests.

These exercise the REAL adapter code paths (search/acquire → browser_fetch under a
saved login) against the REAL sites. They are OPT-IN only:

    CHUDWARE_BROWSER_INTEGRATION_TESTS=1  +  a real logged-in profile per source

CI never sets the flag and never has profiles, so the live cases skip with a clear
"provision required" message — never a real login, never a committed credential.
See README.md for how a developer opts in.

Anti-all-skip posture (per the offline-ml-track-discipline lesson): this module is
NOT structurally incapable of running. Two cases ALWAYS run and prove the adapter
code is real and reachable — the registry is wired, and an adapter's real search()
executes end-to-end offline (browser_fetch monkeypatched to a captured page). Only
the live-network cases are gated.
"""
from __future__ import annotations

import os

import pytest

from services.browser import adapters

#: Every source with real adapter code. Kept in sync with the registry.
EXPECTED_SITES = ("amazon", "grabcad", "jlcpcb", "mcmaster", "pcbway", "traceparts",
                  "misumi", "lcsc", "digikey", "snapeda", "3dcontentcentral",
                  # retail wave (Parts Library V2)
                  "homedepot", "lowes", "acehardware", "grainger", "fastenal",
                  "harborfreight", "tractorsupply", "onlinemetals", "metalsdepot")

_ENABLED = os.environ.get("CHUDWARE_BROWSER_INTEGRATION_TESTS") == "1"
_gated = pytest.mark.skipif(
    not _ENABLED,
    reason="opt-in only: set CHUDWARE_BROWSER_INTEGRATION_TESTS=1 and provide a logged-in "
           "profile per source — see services/browser/integration/README.md",
)


# --- ALWAYS-RUN: prove the suite exercises real code (not vaporware, not all-skip) ---

def test_expected_adapters_are_registered():
    """The live cases below dispatch through this registry — assert every expected
    source actually resolves to a callable adapter (so a skip is a provisioning gap,
    never a missing-code gap)."""
    registered = {a.key for a in adapters.all_adapters()}
    assert set(EXPECTED_SITES) <= registered, f"missing adapters: {set(EXPECTED_SITES) - registered}"
    for key in EXPECTED_SITES:
        a = adapters.get_adapter(key)
        assert callable(getattr(a, "search", None)), f"{key}.search not callable"
        assert callable(getattr(a, "acquire", None)), f"{key}.acquire not callable"


def test_adapter_search_executes_real_code_offline(monkeypatch):
    """Run a REAL adapter's search() end-to-end with browser_fetch faked to a captured
    login-wall page — proves the adapter's own fetch→parse→login-wall branch executes
    (the code path the live test hits), with zero network."""
    from services.browser import session
    from services.browser.adapters import grabcad

    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site, **kw: {"ok": True, "status": 200,
                                 "content": "<html><body>Log in to GrabCAD</body></html>"},
    )
    out = grabcad.GrabCADAdapter().search("bracket")
    assert isinstance(out, list) and out, "search returned no result envelope"
    assert isinstance(out[0], dict)
    # The real login-wall branch fired → a login_required ask (contracts/interaction.md).
    assert out[0].get("reason") == "login_required"
    assert str(out[0].get("ask_user", {}).get("id", "")).startswith("browser_login")


# --- GATED: real network, real login, opt-in only -------------------------------

@_gated
@pytest.mark.parametrize("key", EXPECTED_SITES)
def test_live_search_exercises_real_site(key):
    from services.browser import profiles

    if not profiles.has_profile(key):
        pytest.skip(f"no saved profile for '{key}' — run browser_login('{key}') first (README)")

    adapter = adapters.get_adapter(key)
    out = adapter.search("bracket")
    # Any of these is a genuine, real-code outcome: parsed results, a login-required
    # ask (session expired), or a captcha/2FA abort surfaced from session.browser_fetch.
    # We never assert a live site returns products — only that real code ran and
    # produced the contract's list-of-dict envelope.
    assert isinstance(out, list)
    assert all(isinstance(x, dict) for x in out)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CHUDWARE_CEF_INTEGRATION_TESTS") != "1",
                    reason="requires a CEF-enabled host (CHUDWARE_CEF_INTEGRATION_TESTS=1)")
def test_cef_partial_load_does_not_leak_to_next_navigation(monkeypatch, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from tempfile import TemporaryDirectory
    from threading import Event, Thread
    from services.browser import cef_client

    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                release.wait(20)
                try:
                    self.send_response(200)
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/redirect":
                body = (b"<html><title>OLD</title><script>"
                        b"setTimeout(()=>location.href='/second',100)</script></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = (b"<html><title>LM358</title><script>history.replaceState({},'',"
                    b"'/spa');</script><img src='/slow'></html>"
                    if self.path == "/first" else b"<html><title>ATMEGA328P</title></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    with TemporaryDirectory(prefix="chud-cef-test-", dir="/tmp") as socket_dir:
        monkeypatch.setenv("CHUDWARE_CEF_SOCKET", socket_dir + "/daemon.sock")
        monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
        client = cef_client.CefDaemon()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            first = client.fetch(base + "/first", settle_ms=0)
            assert first["ok"] and first["status"] == 200 and first["partial_load"]
            assert "LM358" in first["html"]
            assert "data-chudware-partial-ready" not in first["html"]
            second = client.fetch(base + "/second", settle_ms=0)
            assert second["ok"] and not second["partial_load"]
            assert "ATMEGA328P" in second["html"] and "LM358" not in second["html"]
            assert second["final_url"] == base + "/second"
            redirected = client.fetch(base + "/redirect", settle_ms=1200)
            assert redirected["ok"] and "ATMEGA328P" in redirected["html"]
            assert redirected["final_url"] == base + "/second"
        finally:
            release.set()
            server.shutdown()
            try:
                client._call("shutdown", {}, timeout_s=5)
            except cef_client.CefDaemonError:
                pass

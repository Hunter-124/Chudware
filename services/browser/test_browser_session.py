"""Tests for services/browser — T-X1.

No real Playwright launch, no real network: sync_playwright is injected via
each function's private ``_sync_playwright`` seam (same pattern as
``engine_oracle.parts_providers``'s ``_http_json`` monkeypatch seam).
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def browser_mods(tmp_path, monkeypatch):
    """Reload services.browser.* under an isolated APPDATA base."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, ratelimit, session
    importlib.reload(profiles)
    importlib.reload(ratelimit)
    importlib.reload(session)
    yield profiles, ratelimit, session
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


class _FakePage:
    def __init__(self, html: str = "<html>ok</html>", status: int = 200):
        self._html = html
        self._status = status
        self.closed = False

    def wait_for_event(self, name, timeout=None):
        return None  # simulate the user closing the login window immediately

    def goto(self, url, timeout=None):
        return _FakeResponse(self._status)

    def content(self):
        return self._html


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeContext:
    def __init__(self, html: str = "<html>ok</html>", status: int = 200):
        self.pages = []
        self.closed = False
        self._html = html
        self._status = status

    def new_page(self):
        page = _FakePage(self._html, self._status)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, html: str = "<html>ok</html>", status: int = 200):
        self.launched: list[tuple[str, bool]] = []
        self._html = html
        self._status = status
        self.last_context: _FakeContext | None = None

    def launch_persistent_context(self, user_data_dir, headless=True, **_kwargs):
        self.launched.append((user_data_dir, headless))
        ctx = _FakeContext(self._html, self._status)
        self.last_context = ctx
        return ctx


class _FakePlaywright:
    def __init__(self, html: str = "<html>ok</html>", status: int = 200):
        self.chromium = _FakeChromium(html, status)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_sync_playwright_factory(html: str = "<html>ok</html>", status: int = 200):
    pw = _FakePlaywright(html, status)

    def _factory():
        return pw

    _factory.instance = pw
    return _factory


class _FakeCef:
    """Stand-in for the CEF daemon client — browser_fetch/download drive CEF now."""
    def __init__(self, html="<html>ok</html>", status=200, url="https://example.com/"):
        self._html, self._status, self._url = html, status, url
        self.fetched, self.downloaded = [], []

    def fetch(self, url, *, settle_ms=1200, profile=""):
        self.fetched.append(url)
        return {"ok": True, "status": self._status, "final_url": self._url, "html": self._html}

    def download(self, url, dest, *, profile=""):
        from pathlib import Path
        Path(dest).write_bytes(self._html.encode())
        self.downloaded.append((url, dest))
        return {"ok": True, "dest": dest}

    def interact(self, url, script, *, sentinel="__aicad_capture",
                 settle_ms=4000, timeout_ms=12000, profile=""):
        self.fetched.append(url)
        return {"ok": True, "status": self._status, "final_url": self._url, "html": self._html}


# --- profiles -----------------------------------------------------------------

def test_profile_dir_and_metadata_roundtrip(browser_mods):
    profiles, _ratelimit, _session = browser_mods
    assert not profiles.has_profile("GrabCAD")
    d = profiles.profile_dir("GrabCAD")
    assert d.name == "grabcad"
    d.mkdir(parents=True)
    assert profiles.has_profile("GrabCAD")

    meta = profiles.save_meta("GrabCAD", last_login_ts=123.0)
    assert meta["site"] == "grabcad"
    assert profiles.load_meta("GrabCAD")["last_login_ts"] == 123.0


def test_list_and_delete_profiles(browser_mods):
    profiles, _ratelimit, _session = browser_mods
    profiles.profile_dir("siteA").mkdir(parents=True)
    profiles.save_meta("siteA", created_ts=1.0)
    profiles.profile_dir("siteB").mkdir(parents=True)
    profiles.save_meta("siteB", created_ts=2.0)

    sites = sorted(p["site"] for p in profiles.list_profiles())
    assert sites == ["sitea", "siteb"]

    assert profiles.delete_profile("siteA") is True
    assert not profiles.has_profile("siteA")
    assert profiles.delete_profile("siteA") is False


def test_slug_rejects_empty(browser_mods):
    profiles, _ratelimit, _session = browser_mods
    with pytest.raises(ValueError):
        profiles.slug("   ")


# --- ratelimit ------------------------------------------------------------------

def test_ratelimit_gates_repeat_fetch(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    assert ratelimit.seconds_until_ready("siteA") == 0.0
    ratelimit.mark_hit("siteA")
    remaining = ratelimit.seconds_until_ready("siteA", min_interval=10.0)
    assert remaining > 0.0

    slept = []
    ratelimit.wait("siteA", min_interval=10.0, _sleep=slept.append)
    assert slept and slept[0] > 0.0


def test_ratelimit_survives_reload_via_metadata(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    ratelimit.mark_hit("siteA")
    # A fresh cooldown check reads persisted metadata, not in-memory state.
    assert ratelimit.seconds_until_ready("siteA", min_interval=1000.0) > 0.0


def test_backoff_escalates_on_429_and_resets_on_ok(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    ratelimit.note_result("siteB", 429)
    first = ratelimit.seconds_until_ready("siteB", min_interval=0.0)
    assert first >= ratelimit.BACKOFF_BASE_S - 1.0
    ratelimit.note_result("siteB", 429)              # consecutive → doubles
    second = ratelimit.seconds_until_ready("siteB", min_interval=0.0)
    assert second > first
    ratelimit.note_result("siteB", 200)              # clean fetch clears backoff
    assert ratelimit.seconds_until_ready("siteB", min_interval=0.0) == 0.0


def test_backoff_is_capped(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    for _ in range(20):
        ratelimit.note_result("siteC", 403)
    assert ratelimit.seconds_until_ready("siteC", min_interval=0.0) <= ratelimit.BACKOFF_MAX_S + 1.0


def test_session_ceiling_trips_after_max_per_site(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    monkey_max = ratelimit.MAX_REQ_PER_SITE
    ratelimit.reset_session_counts()
    for _ in range(monkey_max):
        assert ratelimit.reserve_request("siteD") is None
    assert ratelimit.reserve_request("siteD") == "request_ceiling_site"


def test_crawl_delay_cached_and_used(browser_mods):
    _profiles, ratelimit, _session = browser_mods
    # Stub the network read: publish a 7s crawl-delay for the host.
    ratelimit._fetch_robots_crawl_delay = lambda host: 7.0  # type: ignore[assignment]
    d = ratelimit.crawl_delay("siteE", "https://siteE.example/search?q=x")
    assert d == 7.0
    # A 3s min_interval is overridden by the longer published crawl-delay.
    ratelimit.mark_hit("siteE")
    assert ratelimit.seconds_until_ready("siteE", min_interval=3.0,
                                         url="https://siteE.example/x") > 3.5


# --- session: browser_login -----------------------------------------------------

def test_browser_login_creates_profile_and_metadata(browser_mods):
    profiles, _ratelimit, session = browser_mods
    fake_factory = _fake_sync_playwright_factory()

    result = session.browser_login("GrabCAD", _sync_playwright=fake_factory)

    assert result["ok"] is True
    assert result["site"] == "grabcad"
    assert result["last_login_ts"] > 0
    assert fake_factory.instance.chromium.launched == [(result["profile_dir"], False)]
    assert profiles.load_meta("grabcad")["last_login_ts"] == result["last_login_ts"]


def test_browser_login_degrades_when_playwright_missing(browser_mods, monkeypatch):
    _profiles, _ratelimit, session = browser_mods

    # Playwright IS installed in this dev venv (T-X1 installs it), so force the
    # internal import seam to fail to exercise the "not installed" degradation
    # path a bare-box checkout would actually hit.
    def _raise_import():
        raise RuntimeError("playwright not installed")

    monkeypatch.setattr(session, "_import_sync_playwright", _raise_import)
    result = session.browser_login("grabcad", timeout=1.0)
    assert result == {"ok": False, "reason": "playwright_not_installed",
                      "hint": "playwright not installed"}


def test_browser_login_never_raises_on_launch_error(browser_mods):
    _profiles, _ratelimit, session = browser_mods

    def _factory():
        raise RuntimeError("boom: no display")

    result = session.browser_login("grabcad", _sync_playwright=_factory)
    assert result["ok"] is False
    assert result["reason"] == "browser_login_failed"


def test_anaheim_prefers_native_chrome_login(browser_mods):
    """Anaheim Magento reCAPTCHA rejects Playwright-driven sessions — product
    path must opt into system Chrome with zero automation during sign-in."""
    _profiles, _ratelimit, session = browser_mods
    assert session._site_wants_native_chrome("anaheimautomation") is True
    assert session._site_wants_native_chrome("grabcad") is False
    assert session._url_looks_post_login(
        "anaheimautomation",
        "https://anaheimautomation.com/customer/account/",
    ) is True
    assert session._url_looks_post_login(
        "anaheimautomation",
        "https://anaheimautomation.com/customer/account/login/referer/xxx/",
    ) is False
    assert session._url_looks_post_login(
        "anaheimautomation",
        "https://anaheimautomation.com/customer/account/create/",
    ) is False

# --- session: browser_fetch ------------------------------------------------------

def test_browser_fetch_without_profile_autocreates_one(browser_mods):
    # Anonymous browsing allowed (2026-07-07): a missing profile is created on
    # the fly instead of failing with no_profile.
    profiles, _ratelimit, session = browser_mods
    result = session.browser_fetch("https://example.com/part", "grabcad",
                                   _cef=_FakeCef(html="<html>part page</html>"))
    assert result["ok"] is True
    assert profiles.has_profile("grabcad") is True
    assert profiles.has_login("grabcad") is False  # a dir is not a login


def test_browser_fetch_surfaces_bot_wall_status(browser_mods):
    # HTTP 403/429 pages are bot walls, not content (live-probe 2026-07-07:
    # they used to return ok:True and adapters reported silent empty results).
    profiles, _ratelimit, session = browser_mods
    profiles.profile_dir("grabcad").mkdir(parents=True)
    result = session.browser_fetch("https://example.com/part", "grabcad",
                                   _cef=_FakeCef(html="<html>Access Denied</html>", status=403))
    assert result["ok"] is False
    assert result["reason"] == "bot_walled"


def test_browser_fetch_returns_envelope_when_profile_exists(browser_mods):
    profiles, ratelimit, session = browser_mods
    profiles.profile_dir("grabcad").mkdir(parents=True)
    cef = _FakeCef(html="<html>part page</html>")

    result = session.browser_fetch("https://grabcad.com/part/1", "grabcad",
                                   _cef=cef, min_interval=0.0)

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["content"] == "<html>part page</html>"
    assert result["session_profile_used"] == "grabcad"
    assert "fetched_ts" in result
    assert cef.fetched[-1] == "https://grabcad.com/part/1"
    # Rate-limit hit was recorded even though this call itself didn't need to wait.
    assert ratelimit.seconds_until_ready("grabcad", min_interval=1000.0) > 0.0


def test_browser_interact_returns_settled_dom(browser_mods):
    profiles, _ratelimit, session = browser_mods
    profiles.ensure_profile("mcmaster")
    cef = _FakeCef(html='<div id="__aicad_capture" data-status="ok" data-fp="/x.STEP"></div>')
    result = session.browser_interact("https://www.mcmaster.com/91290A115/", "mcmaster",
                                      "js();", _cef=cef, min_interval=0.0)
    assert result["ok"] is True
    assert "__aicad_capture" in result["content"]
    assert cef.fetched == ["https://www.mcmaster.com/91290A115/"]


def test_browser_interact_never_raises_on_daemon_error(browser_mods):
    _profiles, _ratelimit, session = browser_mods

    class _Boom:
        def interact(self, *a, **k):
            raise RuntimeError("daemon down")

    result = session.browser_interact("https://www.mcmaster.com/91290A115/", "mcmaster",
                                      "js();", _cef=_Boom(), min_interval=0.0)
    assert result["ok"] is False
    assert result["reason"] == "browser_interact_failed"


def test_browser_fetch_never_raises_on_navigation_error(browser_mods):
    profiles, _ratelimit, session = browser_mods
    profiles.profile_dir("grabcad").mkdir(parents=True)

    class _Boom:
        def fetch(self, *a, **k):
            raise RuntimeError("net::ERR_CONNECTION_RESET")

    result = session.browser_fetch("https://grabcad.com/part/1", "grabcad",
                                   _cef=_Boom(), min_interval=0.0)
    assert result["ok"] is False
    assert result["reason"] == "browser_fetch_failed"


def test_browser_fetch_rate_limits_between_calls(browser_mods, monkeypatch):
    profiles, ratelimit, session = browser_mods
    profiles.profile_dir("grabcad").mkdir(parents=True)

    calls = []
    monkeypatch.setattr(ratelimit, "wait",
                        lambda site, *, min_interval=ratelimit.DEFAULT_MIN_INTERVAL_S, url=None:
                        calls.append((site, min_interval)))

    session.browser_fetch("https://grabcad.com/a", "grabcad", _cef=_FakeCef(),
                          min_interval=5.0)

    assert calls == [("grabcad", 5.0)]

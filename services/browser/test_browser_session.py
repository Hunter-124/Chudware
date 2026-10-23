"""Tests for services/browser session helpers.

No real browser launch or network: tests inject the residual Playwright login
factory and fake the headless browser client, profile, cache and transport seams.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def browser_mods(tmp_path, monkeypatch):
    """Reload services.browser.* under an isolated APPDATA base."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
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


def test_wait_without_max_is_unbounded_exactly_as_before(browser_mods):
    """The default path must be untouched: it sleeps the FULL remaining wait, even a
    900 s one, and returns it. Regression guard for the bounded-wait seam."""
    _profiles, ratelimit, _session = browser_mods
    for _ in range(20):
        ratelimit.note_result("siteF", 429)          # ladder is pinned at BACKOFF_MAX_S
    owed = ratelimit.seconds_until_ready("siteF", min_interval=0.0)
    assert owed > ratelimit.BACKOFF_MAX_S - 1.0

    slept = []
    returned = ratelimit.wait("siteF", min_interval=0.0, _sleep=slept.append)
    assert len(slept) == 1
    assert slept[0] > ratelimit.BACKOFF_MAX_S - 1.0   # really slept the whole 900 s
    assert returned == slept[0]


def test_wait_with_max_refuses_instead_of_sleeping_past_it(browser_mods):
    """An armed cooldown longer than ``max_wait_s`` sleeps NOTHING and reports the
    full remaining wait as data (return value > max_wait_s marks the refusal)."""
    _profiles, ratelimit, _session = browser_mods
    ratelimit.note_result("siteG", 429)              # BACKOFF_BASE_S, i.e. 30 s

    slept = []
    remaining = ratelimit.wait("siteG", min_interval=0.0, max_wait_s=20.0,
                               _sleep=slept.append)
    assert slept == []                               # the whole point: no blocking
    assert remaining > 20.0
    assert remaining <= ratelimit.BACKOFF_BASE_S + 1.0


def test_wait_with_max_still_sleeps_ordinary_spacing(browser_mods):
    """Politeness spacing under the budget is waited out, not refused — the bound
    changes deadline behaviour, never the pacing policy."""
    _profiles, ratelimit, _session = browser_mods
    ratelimit.mark_hit("siteH")

    slept = []
    waited = ratelimit.wait("siteH", min_interval=3.0, max_wait_s=20.0,
                            _sleep=slept.append)
    assert len(slept) == 1 and 0.0 < slept[0] <= 3.0
    assert waited == slept[0] and waited <= 20.0


def test_backoff_ladder_never_fits_the_session_cooldown_budget(browser_mods):
    """The arithmetic behind session.COOLDOWN_WAIT_BUDGET_S: the SMALLEST cooldown
    the ladder can arm already exceeds the budget, so an armed 429/403 always
    short-circuits while a published crawl-delay never does."""
    _profiles, ratelimit, session = browser_mods
    assert ratelimit.BACKOFF_BASE_S > session.COOLDOWN_WAIT_BUDGET_S
    assert ratelimit.DEFAULT_MIN_INTERVAL_S < session.COOLDOWN_WAIT_BUDGET_S


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

    # Playwright IS installed in this dev venv, so force the
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

def test_browser_fetch_preserves_backend_bot_wall_reason(browser_mods):
    profiles, _ratelimit, session = browser_mods
    profiles.ensure_profile("8020")

    class _BotWall:
        def fetch(self, *_args, **_kwargs):
            return {"ok": False, "reason": "bot_walled", "status": 200}

    result = session.browser_fetch(
        "https://8020.net/catalogsearch/result/?q=bracket",
        "8020",
        _cef=_BotWall(),
        min_interval=0.0,
        no_cache=True,
    )
    assert result["ok"] is False
    assert result["reason"] == "bot_walled"


def test_browser_fetch_uses_driver_headless_client(browser_mods, monkeypatch):
    profiles, _ratelimit, session = browser_mods
    profiles.ensure_profile("vendor")
    selected: list[str] = []
    backend = _FakeCef(html="<html>selected backend</html>")

    from services.browser import driver
    monkeypatch.setattr(
        driver,
        "headless_client",
        lambda *, profile: selected.append(profile) or backend,
    )
    result = session.browser_fetch(
        "https://example.com/part",
        "vendor",
        min_interval=0.0,
        no_cache=True,
    )

    assert result["ok"] is True
    assert result["content"] == "<html>selected backend</html>"
    assert selected == ["vendor"]



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


def test_browser_fetch_preserves_backend_final_url_after_redirect(browser_mods):
    profiles, _ratelimit, session = browser_mods
    profiles.ensure_profile("vendor")
    requested = "https://vendor.example/login"
    redirected = "https://vendor.example/account"

    result = session.browser_fetch(
        requested,
        "vendor",
        _cef=_FakeCef(url=redirected),
        min_interval=0.0,
        no_cache=True,
    )

    assert result["ok"] is True
    assert result["url"] == requested
    assert result["final_url"] == redirected


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
                        lambda site, *, min_interval=ratelimit.DEFAULT_MIN_INTERVAL_S,
                        url=None, max_wait_s=None:
                        (calls.append((site, min_interval)), 0.0)[1])

    session.browser_fetch("https://grabcad.com/a", "grabcad", _cef=_FakeCef(),
                          min_interval=5.0)

    assert calls == [("grabcad", 5.0)]


# --- session: 429/403 cooldown refusals (harness-deadline defect) ----------------
#
# BACKOFF_MAX_S is 900 s while the LARGEST harness tool timeout in the repo is 600 s
# and the two paths that reach browser_download are research_datasheet_fetch (180 s)
# and reverse_engineer_product (300 s). A synchronous wait therefore guaranteed the
# harness abandoned the call mid-sleep and told the model "timed out, may still be
# running" — when the truth was "this site is in a cooldown no call can outlast".
# These tests are TIMED on purpose: the assertion that matters is wall clock.

#: A cooldown a test can be sure no CI machine will ever wait out.
_FAR_FUTURE_COOLDOWN_S = 900.0
#: Ceiling for "returned promptly instead of blocking". Generous enough for a slow
#: CI box (profile creation + two JSON meta reads) and still 900x under the sleep
#: it is proving did not happen.
_PROMPT_S = 1.0


def test_browser_download_refuses_a_far_future_cooldown_promptly(browser_mods, tmp_path):
    _profiles, ratelimit, session = browser_mods
    import time as _time
    session.profiles.save_meta(
        "grabcad", rl_cooldown_until=_time.time() + _FAR_FUTURE_COOLDOWN_S)
    cef = _FakeCef()

    t0 = _time.perf_counter()
    result = session.browser_download("https://grabcad.com/a.pdf", "grabcad",
                                      tmp_path / "dl" / "a.pdf", _cef=cef,
                                      min_interval=0.0)
    elapsed = _time.perf_counter() - t0

    assert elapsed < _PROMPT_S, f"blocked for {elapsed:.1f}s instead of refusing"
    assert result["ok"] is False
    assert result["reason"] == "cooldown_active"
    assert result["policy_refusal"] is True and result["executed"] is False
    # retry_after_s is the real remaining cooldown, not a placeholder.
    assert _FAR_FUTURE_COOLDOWN_S - 5.0 < result["retry_after_s"] <= _FAR_FUTURE_COOLDOWN_S
    assert cef.downloaded == []                      # nothing was fetched
    # The hint must teach, not just name a state.
    hint = result["hint"]
    assert "grabcad" in hint and "IMPOSSIBLE" in hint.upper()
    assert "other sources" in hint
    assert ratelimit.seconds_until_ready("grabcad", min_interval=0.0) > 0.0


def test_browser_fetch_and_interact_refuse_the_same_cooldown(browser_mods):
    """Both other agent-reachable entry points share the policy — the two dispatch
    paths drifting apart is the recurring defect class here."""
    _profiles, _ratelimit, session = browser_mods
    import time as _time
    for site in ("vendorf", "vendori"):
        session.profiles.save_meta(
            site, rl_cooldown_until=_time.time() + _FAR_FUTURE_COOLDOWN_S)

    t0 = _time.perf_counter()
    fetched = session.browser_fetch("https://vendorf.example/p", "vendorf",
                                    _cef=_FakeCef(), min_interval=0.0, no_cache=True)
    interacted = session.browser_interact("https://vendori.example/p", "vendori",
                                          "js();", _cef=_FakeCef(), min_interval=0.0)
    elapsed = _time.perf_counter() - t0

    assert elapsed < _PROMPT_S * 2, f"blocked for {elapsed:.1f}s instead of refusing"
    for result in (fetched, interacted):
        assert result["ok"] is False and result["reason"] == "cooldown_active"
        assert result["retry_after_s"] > 0.0


def test_ordinary_spacing_is_not_a_cooldown_refusal(browser_mods, tmp_path):
    """A plain inter-request window under the budget still fetches — the refusal is
    scoped to punishment cooldowns, so normal pacing is unchanged."""
    _profiles, _ratelimit, session = browser_mods
    cef = _FakeCef()
    first = session.browser_download("https://grabcad.com/a.pdf", "grabcad",
                                     tmp_path / "dl" / "a.pdf", _cef=cef,
                                     min_interval=0.0)
    second = session.browser_download("https://grabcad.com/b.pdf", "grabcad",
                                      tmp_path / "dl" / "b.pdf", _cef=cef,
                                      min_interval=0.0)
    assert first["ok"] is True and second["ok"] is True
    assert len(cef.downloaded) == 2

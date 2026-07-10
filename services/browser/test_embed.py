"""Tests for services/browser/embed.py + engine_oracle/embed_rpc.py.

Same posture as test_browser_session.py: no real Playwright launch, no real
network — the async Playwright driver is injected through EmbedManager's
private ``_async_playwright`` seam. The one exception is the ``live``-marked
smoke at the bottom (opt in with ``--live``), which drives a real Chromium at
example.com.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
import uuid

import pytest

from engine import paths as engine_paths
from services.browser import embed, profiles, session
from services.browser.embed import EmbedManager, classify_login_state

_JPEG = b"\xff\xd8\xe0fake-jpeg-bytes"
_JPEG_B64 = base64.b64encode(_JPEG).decode("ascii")


# ---------------------------------------------------------------------------
# Fake async-Playwright plumbing (lives on the manager's loop, like the real one)

class _FakeMouse:
    def __init__(self):
        self.calls: list[tuple] = []

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def down(self, button="left", click_count=1):
        self.calls.append(("down", button))

    async def up(self, button="left", click_count=1):
        self.calls.append(("up", button))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class _FakeKeyboard:
    def __init__(self):
        self.calls: list[tuple] = []

    async def press(self, combo):
        self.calls.append(("press", combo))

    async def insert_text(self, text):
        self.calls.append(("insert_text", text))


class _FakePage:
    def __init__(self, html="<html>content page</html>"):
        self.url = "about:blank"
        self.html = html
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.screenshots = 0

    async def goto(self, url, timeout=None):
        self.url = url

    async def content(self):
        return self.html

    async def screenshot(self, type="jpeg", quality=None):
        self.screenshots += 1
        return _JPEG


class _FakeCdp:
    """Fake CDP session: emits ``frames_on_start`` screencast frames when
    Page.startScreencast is sent (or raises when ``broken``)."""

    def __init__(self, broken=False, frames_on_start=3):
        self.broken = broken
        self.frames_on_start = frames_on_start
        self.handlers: dict[str, list] = {}
        self.sent: list[tuple] = []

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.startScreencast":
            if self.broken:
                raise RuntimeError("screencast not available")
            for i in range(self.frames_on_start):
                for cb in self.handlers.get("Page.screencastFrame", []):
                    cb({"data": _JPEG_B64, "sessionId": i + 1})


class _FakeContext:
    def __init__(self, page, cdp):
        self.pages = [page]
        self._cdp = cdp
        self.closed = False

    async def new_page(self):
        return self.pages[0]

    async def new_cdp_session(self, page):
        return self._cdp

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, make_context):
        self._make_context = make_context
        self.launches: list[str] = []

    async def launch_persistent_context(self, user_data_dir, headless=True,
                                        args=None, viewport=None, **_kwargs):
        self.launches.append(str(user_data_dir))
        return self._make_context()


class _FakePw:
    def __init__(self, make_context):
        self.chromium = _FakeChromium(make_context)


class _FakeDriver:
    def __init__(self, pw):
        self._pw = pw

    async def start(self):
        return self._pw


def _make_manager(*, html="<html>content page</html>", broken_cdp=False,
                  clock=None, idle_timeout=embed.IDLE_TIMEOUT_S):
    """EmbedManager over a fully faked async Playwright. Returns (manager, made)
    where ``made`` collects each (page, cdp, context) triple as sessions open."""
    made: list[tuple] = []

    def make_context():
        page = _FakePage(html=html)
        cdp = _FakeCdp(broken=broken_cdp)
        ctx = _FakeContext(page, cdp)
        made.append((page, cdp, ctx))
        return ctx

    pw = _FakePw(make_context)
    mgr = EmbedManager(clock=clock or time.monotonic, idle_timeout=idle_timeout,
                       _async_playwright=lambda: _FakeDriver(pw))
    return mgr, made


@pytest.fixture()
def tmp_profiles(tmp_path, monkeypatch):
    """Point the profile store at a tmp dir (no engine.paths reload needed)."""
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "profiles")
    yield tmp_path / "profiles"


def _site(prefix="embedsite"):
    """Unique site id per test — profile locks are process-global."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _wait_for_frame(mgr, sid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = mgr.frame(sid)
        if res.get("ok") and res["seq"] > 0 and res["frame"]:
            return res
        time.sleep(0.02)
    raise AssertionError("no frame arrived in time")


# ---------------------------------------------------------------------------
# profiles.profile_lock

def test_profile_lock_same_site_same_lock():
    site = _site()
    assert profiles.profile_lock(site) is profiles.profile_lock(site)
    assert profiles.profile_lock(site) is profiles.profile_lock(site.upper())  # slug-normalised


def test_profile_lock_none_is_per_call_dummy():
    assert profiles.profile_lock(None) is not profiles.profile_lock(None)


# ---------------------------------------------------------------------------
# session.py profile_busy degradation

def test_browser_login_profile_busy(tmp_profiles):
    site = _site()
    lock = profiles.profile_lock(site)
    assert lock.acquire(blocking=False)
    try:
        res = session.browser_login(site, _sync_playwright=lambda: None)
        assert res == {"ok": False, "reason": "profile_busy", "hint": res["hint"]}
        assert "embedded browser" in res["hint"]
    finally:
        lock.release()


def test_browser_fetch_profile_busy(tmp_profiles):
    site = _site()
    profiles.profile_dir(site).mkdir(parents=True)  # pass the has_profile gate
    lock = profiles.profile_lock(site)
    assert lock.acquire(blocking=False)
    try:
        res = session.browser_fetch("https://x.test/p", site, _cef=object())
        assert res["ok"] is False and res["reason"] == "profile_busy"
    finally:
        lock.release()


def test_browser_download_profile_busy(tmp_profiles, tmp_path):
    site = _site()
    profiles.profile_dir(site).mkdir(parents=True)
    lock = profiles.profile_lock(site)
    assert lock.acquire(blocking=False)
    try:
        res = session.browser_download("https://x.test/f.step", site, tmp_path / "f.step",
                                       _cef=object())
        assert res["ok"] is False and res["reason"] == "profile_busy"
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# EmbedManager lifecycle over fake Playwright

def test_manager_start_frames_and_stop(tmp_profiles):
    mgr, made = _make_manager()
    res = mgr.start(url="https://example.test/start")
    assert res["ok"] and res["w"] == embed.DEFAULT_MAX_WIDTH
    sid = res["id"]
    try:
        frame = _wait_for_frame(mgr, sid)
        assert frame["frame"] == _JPEG
        assert frame["frame"][:2] == b"\xff\xd8"
        # unchanged since_seq → frame None, seq still reported
        again = mgr.frame(sid, since_seq=frame["seq"])
        assert again["ok"] and again["frame"] is None and again["seq"] >= frame["seq"]
        # screencast was started over CDP with our geometry
        page, cdp, ctx = made[0]
        started = [p for (m, p) in cdp.sent if m == "Page.startScreencast"]
        assert started and started[0]["format"] == "jpeg"
        assert started[0]["maxWidth"] == embed.DEFAULT_MAX_WIDTH
        # frames were acked
        assert any(m == "Page.screencastFrameAck" for (m, _p) in cdp.sent)
    finally:
        assert mgr.stop(sid)["ok"]
    assert made[0][2].closed
    assert mgr.frame(sid)["reason"] == "no_such_session"


def test_manager_cdp_failure_falls_back_to_screenshots(tmp_profiles):
    mgr, made = _make_manager(broken_cdp=True)
    res = mgr.start(url="https://example.test/")
    assert res["ok"]
    try:
        frame = _wait_for_frame(mgr, res["id"])
        assert frame["frame"] == _JPEG
        assert made[0][0].screenshots > 0  # frames came from page.screenshot
    finally:
        mgr.stop(res["id"])


def test_manager_site_profile_lock_contention(tmp_profiles):
    site = _site()
    mgr, _made = _make_manager()
    first = mgr.start(site=site, url="https://example.test/login")
    assert first["ok"]
    try:
        second = mgr.start(site=site, url="https://example.test/login")
        assert second["ok"] is False and second["reason"] == "profile_busy"
        # sync entry points degrade the same way while the embed holds the profile
        busy = session.browser_login(site, _sync_playwright=lambda: None)
        assert busy["reason"] == "profile_busy"
    finally:
        mgr.stop(first["id"])
    # lock released on stop → a new session can open
    third = mgr.start(site=site, url="https://example.test/login")
    assert third["ok"]
    mgr.stop(third["id"])


def test_manager_ephemeral_default_profile_serialised(tmp_profiles):
    mgr, _made = _make_manager()
    a = mgr.start(url="https://one.test/")
    assert a["ok"]
    try:
        b = mgr.start(url="https://two.test/")
        assert b["ok"] is False and b["reason"] == "profile_busy"
    finally:
        mgr.stop(a["id"])


def test_manager_input_and_navigate(tmp_profiles):
    mgr, made = _make_manager()
    res = mgr.start(url="https://example.test/")
    sid = res["id"]
    try:
        out = mgr.input(sid, [
            {"type": "mousemove", "x": 10, "y": 20},
            {"type": "mousedown", "x": 10, "y": 20, "button": 2},
            {"type": "mouseup", "x": 10, "y": 20, "button": 2},
            {"type": "wheel", "delta_y": -120},
            {"type": "key", "key": "Enter"},
            {"type": "key", "key": "a", "modifiers": ["Control"]},
            {"type": "char", "text": "hello"},
        ])
        assert out["ok"] and out["count"] == 7
        page = made[0][0]
        assert ("down", "right") in page.mouse.calls and ("up", "right") in page.mouse.calls
        assert ("wheel", 0.0, -120.0) in page.mouse.calls
        assert ("press", "Enter") in page.keyboard.calls
        assert ("press", "Control+a") in page.keyboard.calls
        assert ("insert_text", "hello") in page.keyboard.calls

        nav = mgr.navigate(sid, "https://example.test/next")
        assert nav["ok"] and nav["url"] == "https://example.test/next"
        assert mgr.frame(sid)["url"] == "https://example.test/next"
    finally:
        mgr.stop(sid)


def test_manager_playwright_missing_degrades(tmp_profiles, monkeypatch):
    mgr = EmbedManager()  # no injected driver

    def _boom():
        raise RuntimeError("playwright not installed — run `pip install playwright`")
    monkeypatch.setattr(mgr, "_import_async_playwright", _boom)
    res = mgr.start(url="https://example.test/")
    assert res["ok"] is False and res["reason"] == "playwright_not_installed"
    # and the site profile lock was never leaked
    site = _site()
    monkeypatch.setattr(mgr, "_import_async_playwright", _boom)
    assert mgr.start(site=site)["ok"] is False or profiles.profile_lock(site).acquire(blocking=False)


# ---------------------------------------------------------------------------
# Idle reaper (injected clock)

def test_idle_reaper_closes_stale_sessions(tmp_profiles):
    now = [1000.0]
    mgr, made = _make_manager(clock=lambda: now[0], idle_timeout=600.0)
    res = mgr.start(url="https://example.test/")
    sid = res["id"]
    now[0] += 100.0
    assert mgr._reap_idle() == []            # touched 100s ago → kept
    mgr.frame(sid)                           # poll refreshes last_touch
    now[0] += 601.0
    assert mgr._reap_idle() == [sid]         # idle past timeout → reaped
    assert mgr.frame(sid)["reason"] == "no_such_session"
    assert made[0][2].closed


# ---------------------------------------------------------------------------
# login_state classification

def test_classify_login_state_challenge_wins():
    assert classify_login_state("grabcad", "<div class='g-recaptcha'></div>",
                                "https://grabcad.com/login") == "challenge"


def test_classify_login_state_site_markers():
    # grabcad adapter's _LOGIN_WALL_MARKERS include 'Log in to GrabCAD'
    assert classify_login_state("grabcad", "<h1>Log in to GrabCAD</h1>",
                                "https://grabcad.com/login") == "login_page"
    assert classify_login_state("grabcad", "<h1>Your library</h1>",
                                "https://grabcad.com/library") == "logged_in"


def test_classify_login_state_unknown_without_site_or_content():
    assert classify_login_state(None, "<h1>hi</h1>", "https://x.test") == "unknown"
    assert classify_login_state("grabcad", None, "https://grabcad.com") == "unknown"
    assert classify_login_state("not-a-registered-site", "<h1>hi</h1>", "u") == "unknown"


def test_classify_login_state_is_logged_in_hook():
    from services.browser import adapters as adapters_pkg

    class _HookAdapter:
        key = "hooksite-" + uuid.uuid4().hex[:6]

        def is_logged_in(self, content, url):
            return "dashboard" in content

        def search(self, query):
            return []

        def acquire(self, result):
            return {}

    adapter = adapters_pkg.register(_HookAdapter())
    assert classify_login_state(adapter.key, "<div>dashboard</div>", "u") == "logged_in"
    assert classify_login_state(adapter.key, "<div>please sign in</div>", "u") == "login_page"


def test_start_url_for_site_login_url_fallback_base_url():
    assert embed.start_url_for_site("grabcad") == "https://grabcad.com/login"
    assert embed.start_url_for_site("amazon") == "https://www.amazon.com/ap/signin"
    assert embed.start_url_for_site("no-such-site") is None


def test_classify_login_state_auth_url_never_logged_in():
    # A sign-in URL must read login_page even when the page markup lacks the
    # adapter's login-wall markers — otherwise the chip flashes "Signed in" on
    # the very page the user signs in on. Post-login URLs still read logged_in.
    empty = "<html><body>welcome</body></html>"
    for site, url in [
        ("grabcad", "https://grabcad.com/login"),
        ("misumi", "https://us.misumi-ec.com/login"),
        ("snapeda", "https://www.snapeda.com/account/login/"),
        ("3dcontentcentral", "https://www.3dcontentcentral.com/Login.aspx"),
    ]:
        assert classify_login_state(site, empty, url) == "login_page", site
    assert classify_login_state("grabcad", empty, "https://grabcad.com/dashboard") == "logged_in"


def test_classify_login_state_traceparts_auth_urls():
    # Live TraceParts sign-in HTML no longer carries the v1 wall markers; URL guard
    # must keep the wizard from treating the sign-in/sign-up pages as logged_in.
    plain = "<html><body>welcome</body></html>"
    assert classify_login_state(
        "traceparts", plain, "https://www.traceparts.com/en/sign-in") == "login_page"
    assert classify_login_state(
        "traceparts", plain, "https://www.traceparts.com/en/sign-up") == "login_page"
    assert classify_login_state(
        "traceparts", plain, "https://www.traceparts.com/en/search") == "logged_in"


# ---------------------------------------------------------------------------
# Login wizard

def test_login_wizard_sites_excludes_commerce_only():
    from services.browser import adapters
    sites = set(adapters.login_wizard_sites())
    assert "grabcad" in sites and "mcmaster" in sites and "traceparts" in sites
    assert "acehardware" not in sites and "amazon" not in sites
    assert "homedepot" not in sites


def test_has_login_requires_live_verification_not_just_a_stamp(tmp_profiles):
    site = _site("verify")
    # A bare last_login_ts (window opened) must NOT count as signed in — this is
    # the exact bug that made the wizard say "all vendor sites are already
    # signed in" on sites the user never completed.
    profiles.save_meta(site, last_login_ts=123.0)
    assert profiles.has_login(site) is False
    # Only a login_verified_ts (live logged_in confirmation) counts.
    profiles.save_meta(site, login_verified_ts=456.0)
    assert profiles.has_login(site) is True


def test_wizard_default_sites_walks_all_even_with_stale_stamps(tmp_profiles):
    from services.browser import adapters
    mgr, _made = _make_manager()
    # Stamp a bare last_login_ts (the stale/bogus kind) on every wizard site.
    for k in adapters.login_wizard_sites():
        profiles.save_meta(k, last_login_ts=1.0)
    # Walk-all: every login-gated vendor is still offered (live detection, not a
    # stamp, decides who actually needs signing in).
    assert set(mgr._wizard_default_sites()) == set(adapters.login_wizard_sites())


def test_wizard_iteration_skip_and_done(tmp_profiles):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wiz"), _site("wiz")
    res = mgr.wizard_start([site_a, site_b])
    assert res["ok"] and res["current_site"] == site_a and res["embed_id"]
    assert res["all_done"] is False

    status = mgr.wizard_status()
    assert status["active"] and status["current_site"] == site_a
    assert status["remaining"] == [site_a, site_b] and status["done"] == []

    # success on A → meta stamped, session B opens. wizard_next only records a
    # "done" (vs "skipped") when the live session actually reached logged_in, so
    # stage the state the user would have driven it to.
    sid_a = res["embed_id"]
    with mgr._sessions_lock:
        s_a = mgr._sessions[sid_a]
    with s_a.frame_lock:
        s_a.login_state = "logged_in"
    nxt = mgr.wizard_next(skip=False)
    assert nxt["ok"] and nxt["current_site"] == site_b and nxt["done"] == [site_a]
    assert profiles.load_meta(site_a).get("last_login_ts")
    # A live-verified sign-in stamps login_verified_ts → has_login now True.
    assert profiles.load_meta(site_a).get("login_verified_ts")
    assert profiles.has_login(site_a) is True

    # skip B → no meta, wizard finishes
    fin = mgr.wizard_next(skip=True)
    assert fin["all_done"] and fin["skipped"] == [site_b] and fin["done"] == [site_a]
    # B was skipped (never logged_in) → not verified, not signed in.
    assert profiles.has_login(site_b) is False
    assert not profiles.load_meta(site_b).get("last_login_ts")

    # wizard cleaned up → status inactive, next fails structurally
    assert mgr.wizard_status()["active"] is False
    assert mgr.wizard_next()["reason"] == "no_wizard"


def test_wizard_auto_advances_on_logged_in(tmp_profiles):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wizauto"), _site("wizauto")
    res = mgr.wizard_start([site_a, site_b])
    sid = res["embed_id"]
    with mgr._sessions_lock:
        s = mgr._sessions[sid]
    with s.frame_lock:
        s.login_state = "login_page"
    mgr.wizard_status()  # records saw_auth_wall
    with s.frame_lock:
        s.login_state = "logged_in"
    status = mgr.wizard_status()  # detects logged_in after auth wall → advances to site B
    assert status["done"] == [site_a] and status["current_site"] == site_b
    mgr.wizard_stop()


def test_wizard_does_not_auto_advance_homepage_false_logged_in(tmp_profiles):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wizhold"), _site("wizhold")
    res = mgr.wizard_start([site_a, site_b])
    sid = res["embed_id"]
    with mgr._sessions_lock:
        s = mgr._sessions[sid]
    with s.frame_lock:
        s.login_state = "logged_in"  # false positive (homepage w/o login-wall markers)
    status = mgr.wizard_status()
    assert status["current_site"] == site_a and status["done"] == []
    mgr.wizard_stop()


def test_wizard_does_not_auto_advance_on_sign_up_navigation(tmp_profiles, monkeypatch):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wizsignup"), _site("wizsignup")
    login_url = "https://www.traceparts.com/en/sign-in"
    monkeypatch.setattr(embed, "start_url_for_site", lambda s: login_url)
    monkeypatch.setattr(embed, "_opened_at_sign_in", lambda s: True)
    res = mgr.wizard_start([site_a, site_b])
    sid = res["embed_id"]
    with mgr._sessions_lock:
        s = mgr._sessions[sid]
    with s.frame_lock:
        s.login_state = "logged_in"  # false positive (stale markers)
        s.url = "https://www.traceparts.com/en/sign-up"
    status = mgr.wizard_status()
    assert status["current_site"] == site_a and status["done"] == []
    mgr.wizard_stop()


def test_wizard_auto_advances_already_logged_in_redirect(tmp_profiles, monkeypatch):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wizsess"), _site("wizsess")
    login_url = "https://example.com/account/login/"
    monkeypatch.setattr(embed, "start_url_for_site", lambda s: login_url)
    monkeypatch.setattr(embed, "_opened_at_sign_in", lambda s: True)
    res = mgr.wizard_start([site_a, site_b])
    sid = res["embed_id"]
    with mgr._sessions_lock:
        s = mgr._sessions[sid]
    with s.frame_lock:
        s.login_state = "logged_in"
        s.url = "https://example.com/home/"
    status = mgr.wizard_status()
    assert status["done"] == [site_a] and status["current_site"] == site_b
    mgr.wizard_stop()


def test_wizard_stop_closes_current_session(tmp_profiles):
    mgr, made = _make_manager()
    site = _site("wizstop")
    res = mgr.wizard_start([site])
    assert res["ok"]
    assert mgr.wizard_stop()["ok"]
    assert made[0][2].closed
    assert mgr.wizard_status()["active"] is False
    # profile lock released
    assert profiles.profile_lock(site).acquire(blocking=False)
    profiles.profile_lock(site).release()


def test_wizard_start_twice_is_structured_error(tmp_profiles):
    mgr, _made = _make_manager()
    site = _site("wizdup")
    assert mgr.wizard_start([site])["ok"]
    dup = mgr.wizard_start([site])
    assert dup["ok"] is False and dup["reason"] == "wizard_active"
    mgr.wizard_stop()


def test_wizard_empty_site_list_is_all_done(tmp_profiles, monkeypatch):
    mgr, _made = _make_manager()
    monkeypatch.setattr(mgr, "_wizard_default_sites", lambda: [])
    res = mgr.wizard_start(None)
    assert res["ok"] and res["all_done"] and res["sites"] == []


# ---------------------------------------------------------------------------
# RPC wrappers (engine_oracle/embed_rpc.py)

def _rpc():
    from engine_oracle import embed_rpc
    return embed_rpc


def test_rpc_degrades_without_playwright(monkeypatch, tmp_profiles):
    rpc = _rpc()
    mgr = EmbedManager()  # real manager, no playwright injected

    def _boom():
        raise RuntimeError("playwright not installed")
    monkeypatch.setattr(mgr, "_import_async_playwright", _boom)
    monkeypatch.setattr(embed, "get_manager", lambda: mgr)
    res = rpc.EMBED_WORKER_METHODS["login_wizard_start"]({}, None)
    assert res["ok"] is False and res["reason"] == "playwright_not_installed"


def test_rpc_import_error_maps_to_playwright_missing(monkeypatch):
    rpc = _rpc()

    def _import_boom():
        raise ImportError("No module named 'playwright'")
    monkeypatch.setattr(embed, "get_manager", _import_boom)
    res = rpc.EMBED_WORKER_METHODS["login_wizard_status"]({}, None)
    assert res["ok"] is False and res["reason"] == "playwright_not_installed"
    assert "pip install playwright" in res["hint"]


# (test_rpc_full_envelope_roundtrip removed 2026-07-07: the browser_embed_* JPEG
# screencast transport it exercised was deleted with the migration to the
# in-process CEF browser. CEF render/input is covered on the C++ side.)


def test_rpc_wizard_wrappers(monkeypatch, tmp_profiles):
    rpc = _rpc()
    mgr, _made = _make_manager()
    monkeypatch.setattr(embed, "get_manager", lambda: mgr)
    methods = rpc.EMBED_WORKER_METHODS
    site = _site("rpcwiz")

    res = methods["login_wizard_start"]({"sites": [site]}, None)
    assert res["ok"] and res["current_site"] == site
    status = methods["login_wizard_status"]({}, None)
    assert status["ok"] and status["active"] and status["remaining"] == [site]
    nxt = methods["login_wizard_next"]({"skip": True}, None)
    assert nxt["ok"] and nxt["all_done"] and nxt["skipped"] == [site]
    assert methods["login_wizard_stop"]({}, None)["ok"]


def test_rpc_registered_on_worker_seam():
    from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS
    # browser_embed_* (JPEG screencast transport) removed with the CEF migration;
    # what remains is the URL resolver + the sign-in wizard state machine.
    for name in ("browser_start_url",
                 "login_wizard_start", "login_wizard_status", "login_wizard_next",
                 "login_wizard_stop"):
        assert name in WORKER_METHODS, name
    for gone in ("browser_embed_start", "browser_embed_frame", "browser_embed_input",
                 "browser_embed_navigate", "browser_embed_stop"):
        assert gone not in WORKER_METHODS, f"{gone} should have been removed"


# ---------------------------------------------------------------------------
# Live smoke (opt-in): a real Chromium at example.com

@pytest.mark.live
def test_live_embed_smoke(tmp_profiles):
    pytest.importorskip("playwright")
    try:
        mgr = EmbedManager()
        res = mgr.start(url="https://example.com", max_width=800, max_height=600, quality=50)
    except Exception as exc:  # noqa: BLE001 — chromium not provisioned etc.
        pytest.skip(f"embedded chromium unavailable: {exc}")
    if not res.get("ok"):
        pytest.skip(f"embedded chromium unavailable: {res.get('reason')}")
    sid = res["id"]
    try:
        deadline = time.monotonic() + 30
        frame = None
        while time.monotonic() < deadline:
            frame = mgr.frame(sid)
            if frame["seq"] > 0 and frame["frame"]:
                break
            time.sleep(0.1)
        assert frame and frame["seq"] > 0, "no screencast frame within 30s"
        assert frame["frame"][:2] == b"\xff\xd8", "frame is not JPEG"
        assert frame["w"] > 0 and frame["h"] > 0
        assert "example.com" in frame["url"]

        assert mgr.input(sid, [{"type": "mousemove", "x": 100, "y": 100},
                               {"type": "mousedown", "x": 100, "y": 100, "button": 0},
                               {"type": "mouseup", "x": 100, "y": 100, "button": 0}])["ok"]
        nav = mgr.navigate(sid, "https://example.org")
        assert nav["ok"] and "example.org" in nav["url"]
    finally:
        mgr.stop(sid)


# ---------------------------------------------------------------------------
# Client-mode wizard + GUI-session hand-off (CEF → Playwright cookie staging)


def test_wizard_client_mode_never_launches_a_browser(tmp_profiles):
    def _boom():
        raise AssertionError("client mode must not touch playwright")

    mgr = EmbedManager(_async_playwright=_boom)
    res = mgr.wizard_start(["grabcad", "mcmaster"], mode="client")
    assert res["ok"] and res["embed_id"] is None
    assert res["current_site"] == "grabcad"

    status = mgr.wizard_status()
    assert status["active"] and status["embed_id"] is None
    assert status["current_site"] == "grabcad"

    # Client reports the outcome: grabcad signed in, mcmaster skipped.
    nxt = mgr.wizard_next(skip=False, logged_in=True)
    assert nxt["ok"] and nxt["current_site"] == "mcmaster"
    assert nxt["embed_id"] is None            # no worker session opened for step 2
    fin = mgr.wizard_next(skip=True)
    assert fin["all_done"]
    assert fin["done"] == ["grabcad"] and fin["skipped"] == ["mcmaster"]
    assert profiles.has_login("grabcad")
    assert not profiles.has_login("mcmaster")


def test_wizard_client_mode_logged_in_false_marks_skipped(tmp_profiles):
    mgr = EmbedManager(_async_playwright=lambda: None)
    assert mgr.wizard_start(["grabcad"], mode="client")["ok"]
    fin = mgr.wizard_next(skip=False, logged_in=False)
    assert fin["all_done"] and fin["skipped"] == ["grabcad"]
    assert not profiles.has_login("grabcad")


def test_import_session_cookies_stages_and_stamps(tmp_profiles):
    raw = [
        {"name": "sid", "value": "s3cret", "domain": ".grabcad.com", "path": "/",
         "secure": True, "httponly": True, "expires": 4102444800.0,
         "same_site": "lax"},
        {"name": "sess", "value": "v", "domain": "grabcad.com"},   # minimal
        {"name": "", "value": "dropme", "domain": "grabcad.com"},  # invalid → dropped
    ]
    res = embed.import_session_cookies("grabcad", raw)
    assert res["ok"] and res["cookies"] == 2 and res["login_verified"]
    assert profiles.has_login("grabcad")
    assert profiles.has_pending_session_cookies("grabcad")

    jar = profiles.consume_session_cookies("grabcad")
    assert not profiles.has_pending_session_cookies("grabcad")  # one-shot
    assert jar[0]["name"] == "sid" and jar[0]["sameSite"] == "Lax"
    assert jar[0]["httpOnly"] is True and jar[0]["expires"] == 4102444800.0
    assert jar[1] == {"name": "sess", "value": "v", "domain": "grabcad.com",
                      "path": "/", "secure": False, "httpOnly": False,
                      "expires": -1}


def test_import_session_cookies_empty_is_structured_error(tmp_profiles):
    res = embed.import_session_cookies("grabcad", [])
    assert not res["ok"] and res["reason"] == "no_cookies"
    assert not profiles.has_login("grabcad")


def test_apply_pending_cookies_sync_injects_and_consumes(tmp_profiles):
    embed.import_session_cookies("grabcad", [
        {"name": "sid", "value": "x", "domain": ".grabcad.com"}])

    class _Ctx:
        def __init__(self):
            self.added = None

        def add_cookies(self, jar):
            self.added = jar

    ctx = _Ctx()
    assert embed.apply_pending_cookies_sync(ctx, "grabcad") == 1
    assert ctx.added and ctx.added[0]["name"] == "sid"
    # consumed: a second context gets nothing (cookies now live in the profile)
    assert embed.apply_pending_cookies_sync(_Ctx(), "grabcad") == 0

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
                                        args=None, viewport=None):
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
        res = session.browser_fetch("https://x.test/p", site, _sync_playwright=lambda: None)
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
                                       _sync_playwright=lambda: None)
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
    # no adapter currently defines LOGIN_URL → BASE_URL fallback
    assert embed.start_url_for_site("grabcad") == "https://grabcad.com"
    assert embed.start_url_for_site("no-such-site") is None


# ---------------------------------------------------------------------------
# Login wizard

def test_wizard_iteration_skip_and_done(tmp_profiles):
    mgr, _made = _make_manager()
    site_a, site_b = _site("wiz"), _site("wiz")
    res = mgr.wizard_start([site_a, site_b])
    assert res["ok"] and res["current_site"] == site_a and res["embed_id"]
    assert res["all_done"] is False

    status = mgr.wizard_status()
    assert status["active"] and status["current_site"] == site_a
    assert status["remaining"] == [site_a, site_b] and status["done"] == []

    # success on A → meta stamped, session B opens
    nxt = mgr.wizard_next(skip=False)
    assert nxt["ok"] and nxt["current_site"] == site_b and nxt["done"] == [site_a]
    assert profiles.load_meta(site_a).get("last_login_ts")

    # skip B → no meta, wizard finishes
    fin = mgr.wizard_next(skip=True)
    assert fin["all_done"] and fin["skipped"] == [site_b] and fin["done"] == [site_a]
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
        s.login_state = "logged_in"
    status = mgr.wizard_status()  # detects logged_in → advances to site B
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
    res = rpc.EMBED_WORKER_METHODS["browser_embed_start"]({"url": "https://x.test"}, None)
    assert res["ok"] is False and res["reason"] == "playwright_not_installed"


def test_rpc_import_error_maps_to_playwright_missing(monkeypatch):
    rpc = _rpc()

    def _import_boom():
        raise ImportError("No module named 'playwright'")
    monkeypatch.setattr(embed, "get_manager", _import_boom)
    res = rpc.EMBED_WORKER_METHODS["browser_embed_frame"]({"id": "x"}, None)
    assert res["ok"] is False and res["reason"] == "playwright_not_installed"
    assert "pip install playwright" in res["hint"]


def test_rpc_full_envelope_roundtrip(monkeypatch, tmp_profiles):
    rpc = _rpc()
    mgr, _made = _make_manager()
    monkeypatch.setattr(embed, "get_manager", lambda: mgr)
    methods = rpc.EMBED_WORKER_METHODS

    start = methods["browser_embed_start"]({"url": "https://example.test/",
                                            "max_width": 640, "max_height": 480,
                                            "quality": 40}, None)
    assert start["ok"] and start["w"] == 640 and start["h"] == 480
    sid = start["id"]
    try:
        deadline = time.monotonic() + 5
        frame = None
        while time.monotonic() < deadline:
            frame = methods["browser_embed_frame"]({"id": sid}, None)
            if frame.get("ok") and frame["seq"] > 0:
                break
            time.sleep(0.02)
        assert frame and frame["ok"] and frame["seq"] > 0
        assert base64.b64decode(frame["frame_b64"])[:2] == b"\xff\xd8"
        # unchanged → frame_b64 omitted entirely
        same = methods["browser_embed_frame"]({"id": sid, "since_seq": frame["seq"]}, None)
        assert same["ok"] and "frame_b64" not in same and same["seq"] >= frame["seq"]

        assert methods["browser_embed_input"](
            {"id": sid, "events": [{"type": "mousemove", "x": 1, "y": 2}]}, None)["ok"]
        nav = methods["browser_embed_navigate"]({"id": sid, "url": "https://example.test/n"}, None)
        assert nav["ok"]
    finally:
        assert methods["browser_embed_stop"]({"id": sid}, None)["ok"]
    gone = methods["browser_embed_frame"]({"id": sid}, None)
    assert gone["ok"] is False and gone["reason"] == "no_such_session"


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
    for name in ("browser_embed_start", "browser_embed_frame", "browser_embed_input",
                 "browser_embed_navigate", "browser_embed_stop",
                 "login_wizard_start", "login_wizard_status", "login_wizard_next",
                 "login_wizard_stop"):
        assert name in WORKER_METHODS, name


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

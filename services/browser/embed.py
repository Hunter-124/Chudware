"""services/browser/embed.py — reusable embedded-browser backend (CDP screencast).

Runs a real Chromium (Playwright ASYNC API) on a private daemon thread and
streams its rendered surface as JPEG frames for hosts that consume the
``services/parts/embed_rpc.py`` worker RPCs. This residual Playwright backend is
separate from the host's native CEF panel and CEF headless-fetch daemon.

Threading model (the whole point of this module):

- ONE daemon thread ("browser-embed") owns a private asyncio event loop; every
  Playwright object (playwright, contexts, pages, CDP sessions) lives on that
  loop and is only ever touched from it.
- Control operations (start/navigate/input/stop/…) hop onto the loop via
  ``asyncio.run_coroutine_threadsafe(...).result(timeout)``.
- Frame reads NEVER hop: the screencast handler (on the loop) writes the
  latest JPEG into ``EmbedSession.latest_frame`` under a ``threading.Lock``;
  :meth:`EmbedManager.frame` just copies it out under the same lock. A GUI can
  poll at paint rate without queueing work on the browser loop.

Frames come from CDP ``Page.startScreencast`` (push-based, ack'd per frame);
if the screencast fails to start we degrade to a ~5 fps ``page.screenshot``
polling task so the embed still works.

Profiles: a session opened for a known ``site`` uses that site's persistent
profile directory (:mod:`.profiles`) and HOLDS the site's ``profile_lock`` for
its whole lifetime — Chromium's ProcessSingleton forbids two live contexts on
one user-data-dir, so ``browser_fetch``/``browser_download``/``browser_login``
degrade to ``profile_busy`` while an embed session has the profile open (and
vice versa). Site-less sessions share the ``embed_default`` scratch profile.

Safety posture matches :mod:`.session`: challenges (captcha/2FA) are DETECTED
(via :mod:`.challenge`) and surfaced as ``login_state == "challenge"`` — the
human at the embedded browser completes them; nothing here ever automates one.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import sys
import threading
import time
import uuid
from typing import Any, Callable
from contextvars import copy_context
from urllib.parse import urlparse

from services.research.common import fail

from . import challenge, profiles, stealth

DEFAULT_MAX_WIDTH = 1280
DEFAULT_MAX_HEIGHT = 900
DEFAULT_QUALITY = 80

#: Sessions with no frame() poll for this long are reaped (closed) automatically.
IDLE_TIMEOUT_S = 600.0
#: How often the reaper task wakes up.
_REAP_INTERVAL_S = 30.0
#: page.content() is expensive — login_state classification runs at most this often.
_CLASSIFY_INTERVAL_S = 2.0
#: Timeout for a single control-op hop onto the browser loop.
_CALL_TIMEOUT_S = 30.0
#: Fallback screenshot cadence (~5 fps) when CDP screencast is unavailable.
_FALLBACK_FRAME_INTERVAL_S = 0.1

#: Scratch profile shared by all site-less sessions (also serialised by a lock —
#: ProcessSingleton applies to it just like to any real site profile).
_EPHEMERAL_PROFILE = "embed_default"

_MOUSE_BUTTONS = {0: "left", 1: "middle", 2: "right",
                  "left": "left", "middle": "middle", "right": "right"}

# Sign-in URLs for the login wizard. Homepages often lack login-wall markers, so
# opening BASE_URL there gets misclassified as logged_in and auto-advances away.
_SIGN_IN_URLS: dict[str, str] = {
    "amazon": "https://www.amazon.com/ap/signin",
    "grabcad": "https://grabcad.com/login",
    "traceparts": "https://www.traceparts.com/en/sign-in",
    "homedepot": "https://www.homedepot.com/auth/view/signin",
    "lowes": "https://www.lowes.com/u/login",
    "grainger": "https://www.grainger.com/account/sign-in",
    "fastenal": "https://www.fastenal.com/login",
    "harborfreight": "https://www.harborfreight.com/login",
    "tractorsupply": "https://www.tractorsupply.com/tsc/login",
    "acehardware": "https://www.acehardware.com/user/login",
    "mcmaster": "https://www.mcmaster.com/orders/",
    "misumi": "https://us.misumi-ec.com/login",
    "digikey": "https://www.digikey.com/MyDigiKey/Login",
    "lcsc": "https://www.lcsc.com/login",
    "snapeda": "https://www.snapeda.com/account/login/",
    "pcbway": "https://www.pcbway.com/Member/Login.aspx",
    "jlcpcb": "https://passport.jlcpcb.com/login",
    "3dcontentcentral": "https://www.3dcontentcentral.com/Login.aspx",
}


def _profile_busy(site: str) -> dict:
    return fail("profile_busy",
                f"site profile '{site}' is already open (embedded session or sync "
                f"browser_fetch/login in flight) — close it and retry")


def _adapter_for(site: str | None):
    if not site:
        return None
    try:
        from . import adapters
        return adapters.get_adapter(site)
    except Exception:  # noqa: BLE001 — adapters registry is optional context, never fatal
        return None


def _adapter_attr(adapter: Any, name: str) -> Any:
    """Look up ``name`` on the adapter instance, then on its defining module."""
    if adapter is None:
        return None
    val = getattr(adapter, name, None)
    if val is not None:
        return val
    mod = sys.modules.get(type(adapter).__module__)
    return getattr(mod, name, None)


def start_url_for_site(site: str) -> str | None:
    """Adapter ``LOGIN_URL``/``login_url`` when present, else a known sign-in URL,
    else ``BASE_URL`` — the wizard/start page."""
    adapter = _adapter_for(site)
    explicit = (_adapter_attr(adapter, "LOGIN_URL")
                or _adapter_attr(adapter, "login_url"))
    if explicit:
        return explicit
    # Per-site sign-in entry points for the login wizard (homepage ≠ login page).
    slug = (site or "").strip().lower()
    if slug in _SIGN_IN_URLS:
        return _SIGN_IN_URLS[slug]
    return _adapter_attr(adapter, "BASE_URL") or _adapter_attr(adapter, "base_url")


def _opened_at_sign_in(site: str) -> bool:
    """True when the wizard step opens a dedicated sign-in URL (not bare homepage)."""
    adapter = _adapter_for(site)
    explicit = (_adapter_attr(adapter, "LOGIN_URL")
                or _adapter_attr(adapter, "login_url"))
    if explicit:
        return True
    return (site or "").strip().lower() in _SIGN_IN_URLS


# URL path fragments that still mean "auth flow" — not a post-login destination.
# ``/login`` also covers ``/account/login``, ``/user/login`` and ``/Login.aspx``.
_AUTH_PATH_FRAGMENTS: tuple[str, ...] = (
    "/sign-in", "/signin", "/sign-up", "/signup", "/register",
    "/login", "/account/login", "/user/login", "/auth/", "/ap/signin",
)


def _is_auth_url(url: str) -> bool:
    u = (url or "").lower()
    return any(frag in u for frag in _AUTH_PATH_FRAGMENTS)


def _redirected_to_post_login(site: str, current_url: str) -> bool:
    """True when the browser left the sign-in URL for a non-auth page (e.g. /home/)."""
    if not current_url or _is_auth_url(current_url):
        return False
    start = start_url_for_site(site) or ""
    if not start:
        return False
    su, cu = urlparse(start), urlparse(current_url)
    if su.netloc and cu.netloc and su.netloc != cu.netloc:
        return False
    sign_path = (su.path or "/").rstrip("/").lower()
    cur_path = (cu.path or "/").rstrip("/").lower()
    if not sign_path or cur_path == sign_path:
        return False
    return True


def classify_login_state(site: str | None, content: str | None, url: str) -> str:
    """Map page content+URL to ``login_page|logged_in|challenge|unknown``.

    Challenge detection first (safety-relevant). With a known site adapter we
    prefer its ``is_logged_in(content, url)`` hook when it grew one; otherwise
    the generic fallback inverts ``_LOGIN_WALL_MARKERS``: a page showing none
    of the site's login-wall markers counts as logged in.
    """
    if challenge.classify_challenge(content, url):
        return "challenge"
    # A sign-in/registration URL is never a logged-in destination — guard it
    # uniformly so the login-state chip can't flash "Signed in" on the very page
    # the user is signing in on (adapter _LOGIN_WALL_MARKERS are page-markup
    # guesses; this URL check is robust and site-agnostic). This also ensures the
    # wizard records ``saw_auth_wall`` so it reliably auto-advances once the user
    # actually signs in and leaves the auth URL.
    if _is_auth_url(url):
        return "login_page"
    adapter = _adapter_for(site)
    if adapter is None or not content:
        return "unknown"
    is_logged_in = _adapter_attr(adapter, "is_logged_in")
    if callable(is_logged_in):
        try:
            return "logged_in" if is_logged_in(content, url) else "login_page"
        except Exception:  # noqa: BLE001
            return "unknown"
    markers = _adapter_attr(adapter, "_LOGIN_WALL_MARKERS")
    if markers:
        return "login_page" if any(m in content for m in markers) else "logged_in"
    return "unknown"


# ---------------------------------------------------------------------------
# GUI-browser session hand-off: the in-process CEF browser exports its cookie
# jar after a verified sign-in; we normalise it to Playwright's add_cookies
# shape, stage it (profiles.save_session_cookies) and stamp the login as
# verified. Every subsequent persistent context opened for the site — headless
# browser_fetch/browser_download AND embed sessions — injects the staged jar
# once, after which the cookies live natively in the Playwright profile.

_SAME_SITE_MAP = {
    "strict": "Strict", "lax": "Lax", "none": "None",
    "no_restriction": "None", "unspecified": None, "": None,
}


def _normalize_cookie(raw: dict) -> dict | None:
    name = str(raw.get("name", "")).strip()
    domain = str(raw.get("domain", "")).strip()
    if not name or not domain:
        return None
    c: dict[str, Any] = {
        "name": name,
        "value": str(raw.get("value", "")),
        "domain": domain,
        "path": str(raw.get("path") or "/"),
        "secure": bool(raw.get("secure", False)),
        "httpOnly": bool(raw.get("httponly", raw.get("httpOnly", False))),
    }
    expires = raw.get("expires")
    try:
        expires = float(expires) if expires is not None else -1.0
    except (TypeError, ValueError):
        expires = -1.0
    c["expires"] = expires if expires > 0 else -1
    same_site = _SAME_SITE_MAP.get(str(raw.get("same_site",
                                    raw.get("sameSite", ""))).strip().lower())
    if same_site:
        c["sameSite"] = same_site
    return c


def import_session_cookies(site: str, cookies: list[dict],
                           *, verified: bool = True) -> dict:
    """Stage a GUI-exported cookie jar for ``site`` and (when ``verified``)
    stamp ``login_verified_ts`` — the signal ``profiles.has_login`` trusts."""
    if not site:
        return fail("no_site", "session import needs a site id")
    normalized = [c for c in (_normalize_cookie(r) for r in (cookies or []))
                  if c is not None]
    if not normalized:
        return fail("no_cookies", f"no usable cookies exported for '{site}'")
    count = profiles.save_session_cookies(site, normalized)
    profiles.touch_created(site)
    now = time.time()
    fields: dict[str, Any] = {"last_login_ts": now, "session_import_ts": now}
    if verified:
        fields["login_verified_ts"] = now
    profiles.save_meta(site, **fields)
    return {"ok": True, "site": profiles.slug(site), "cookies": count,
            "login_verified": verified}


async def apply_pending_cookies_async(context: Any, site: str | None) -> int:
    """Inject ``site``'s staged cookie jar into a live ASYNC persistent context
    (consumes the sidecar). Best-effort: a bad jar must never break the session."""
    if not site:
        return 0
    jar = profiles.consume_session_cookies(site)
    if not jar:
        return 0
    try:
        await context.add_cookies(jar)
        return len(jar)
    except Exception:  # noqa: BLE001 — stale/foreign cookies: drop, don't break
        return 0


def apply_pending_cookies_sync(context: Any, site: str | None) -> int:
    """SYNC-API twin of :func:`apply_pending_cookies_async` (session.py path)."""
    if not site:
        return 0
    jar = profiles.consume_session_cookies(site)
    if not jar:
        return 0
    try:
        context.add_cookies(jar)
        return len(jar)
    except Exception:  # noqa: BLE001
        return 0


class EmbedSession:
    """State for one embedded browser tab. Playwright objects live on the loop
    thread; ``latest_frame``/``frame_seq``/``url``/``login_state`` are the only
    fields read cross-thread, always under ``frame_lock``."""

    def __init__(self, *, session_id: str, site: str | None, profile_name: str,
                 profile_lock: threading.Lock | None, w: int, h: int, quality: int):
        self.id = session_id
        self.site = site
        self.profile_name = profile_name
        self.profile_lock = profile_lock  # held for the session lifetime; released on close
        self.w = w
        self.h = h
        self.quality = quality
        self.created_ts = time.time()
        self.chromium_version = stealth.FALLBACK_FULL_VERSION
        self.user_agent = stealth.user_agent(stealth.FALLBACK_FULL_VERSION)

        # loop-thread-only Playwright handles
        self.context: Any = None
        self.page: Any = None
        self.cdp: Any = None
        self.fallback_task: asyncio.Task | None = None
        self.classify_task: asyncio.Task | None = None
        self.screencast_active = False

        # cross-thread frame buffer
        self.frame_lock = threading.Lock()
        self.latest_frame: bytes | None = None
        self.frame_seq = 0
        self.url = ""
        self.login_state = "unknown"

        # idle bookkeeping (monotonic clock injected by the manager)
        self.last_touch = 0.0

    def store_frame(self, data: bytes) -> None:
        with self.frame_lock:
            self.latest_frame = data
            self.frame_seq += 1

    def snapshot(self, since_seq: int | None) -> dict:
        with self.frame_lock:
            unchanged = since_seq is not None and since_seq >= self.frame_seq
            return {
                "seq": self.frame_seq,
                "frame": None if unchanged else self.latest_frame,
                "w": self.w, "h": self.h,
                "url": self.url,
                "login_state": self.login_state,
            }


class EmbedManager:
    """Owns the browser thread/loop + all live :class:`EmbedSession`\\ s + the
    login wizard. One process-wide instance normally (see :func:`get_manager`);
    tests build their own with an injected fake ``_async_playwright``/clock."""

    def __init__(self, *, idle_timeout: float = IDLE_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 _async_playwright: Callable[[], Any] | None = None):
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._async_playwright_factory = _async_playwright
        self._sessions: dict[str, EmbedSession] = {}
        self._sessions_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._start_guard = threading.Lock()
        self._pw: Any = None  # started async_playwright driver (loop thread only)
        self._reaper_task: asyncio.Task | None = None
        self._atexit_registered = False
        self._activity_lock = threading.Lock()
        self._activity_calls = 0

        # login-wizard state (guarded by _wizard_lock; pure bookkeeping)
        self._wizard_lock = threading.Lock()
        self._wizard: dict[str, Any] | None = None

    # ------------------------------------------------------------------ loop

    def _import_async_playwright(self) -> Callable[[], Any]:
        if self._async_playwright_factory is not None:
            return self._async_playwright_factory
        # Residual async factory via :mod:`.driver` (CHUDWARE_PW_BACKEND).
        from . import driver as _driver
        return _driver.resolve_async()

    def _ensure_thread(self) -> asyncio.AbstractEventLoop:
        with self._start_guard:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._thread_ready.set()
                loop.run_forever()
                # drain pending tasks on shutdown
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.close()

            self._thread_ready.clear()
            context = copy_context()
            self._thread = threading.Thread(target=context.run, args=(_run,),
                                            name="browser-embed", daemon=True)
            self._thread.start()
            self._thread_ready.wait(timeout=10)
            if self._loop is None:
                raise RuntimeError("browser-embed loop failed to start")
            if not self._atexit_registered:
                atexit.register(self.stop_all)
                self._atexit_registered = True
            return self._loop

    def _call(self, coro, timeout: float = _CALL_TIMEOUT_S):
        """Hop a coroutine onto the browser loop and wait for its result."""
        loop = self._ensure_thread()
        with self._activity_lock:
            self._activity_calls += 1

        async def invoke():
            try:
                return await coro
            finally:
                with self._activity_lock:
                    self._activity_calls -= 1

        invocation = invoke()
        try:
            future = asyncio.run_coroutine_threadsafe(invocation, loop)
        except BaseException:
            invocation.close()
            coro.close()
            with self._activity_lock:
                self._activity_calls -= 1
            raise
        return future.result(timeout)

    async def _ensure_pw(self):
        if self._pw is None:
            factory = self._import_async_playwright()
            self._pw = await factory().start()
        return self._pw

    # ----------------------------------------------------------- lifecycle

    def start(self, *, site: str | None = None, url: str | None = None,
              max_width: int = DEFAULT_MAX_WIDTH, max_height: int = DEFAULT_MAX_HEIGHT,
              quality: int = DEFAULT_QUALITY, headless: bool = True) -> dict:
        """Open a new embedded session. Returns ``{ok, id, w, h, url, login_state}``
        or the §6 failure shape (``profile_busy``, ``playwright_not_installed``, …)."""
        try:
            self._import_async_playwright()
        except RuntimeError as exc:
            return fail("playwright_not_installed", str(exc))

        profile_name = site if site else _EPHEMERAL_PROFILE
        lock = profiles.profile_lock(profile_name)
        if not lock.acquire(blocking=False):
            return _profile_busy(profile_name)

        start_url = url or (start_url_for_site(site) if site else None) or "about:blank"
        w = max(64, int(max_width))
        h = max(64, int(max_height))
        session = EmbedSession(session_id=uuid.uuid4().hex[:12], site=site,
                               profile_name=profile_name, profile_lock=lock,
                               w=w, h=h, quality=max(1, min(int(quality), 100)))
        session.last_touch = self._clock()
        try:
            self._call(self._open_session(session, start_url, headless=headless),
                       timeout=max(_CALL_TIMEOUT_S, 60))
        except Exception as exc:  # noqa: BLE001 — degrade, release the profile
            lock.release()
            return fail("embed_start_failed", str(exc))

        with self._sessions_lock:
            self._sessions[session.id] = session
        return {"ok": True, "id": session.id, "w": session.w, "h": session.h,
                "url": session.url, "login_state": session.login_state,
                "site": session.site}

    async def _open_session(self, s: EmbedSession, start_url: str, *, headless: bool) -> None:
        pw = await self._ensure_pw()
        profile_path = profiles.profile_dir(s.profile_name)
        # Shared consistent-fingerprint identity (see services/browser/stealth.py) —
        # the page-fetch path and this wizard present ONE consistent fingerprint.
        s.chromium_version = await stealth.chromium_version_async(pw)
        s.user_agent = stealth.user_agent(s.chromium_version)
        s.context = await pw.chromium.launch_persistent_context(
            str(profile_path), headless=headless,
            args=stealth.launch_args(headless=headless),
            viewport={"width": s.w, "height": s.h},
            **stealth.context_options(s.user_agent))
        await stealth.harden_context_async(s.context)
        await apply_pending_cookies_async(s.context, s.site)
        pages = list(getattr(s.context, "pages", []) or [])
        s.page = pages[0] if pages else await s.context.new_page()
        # Screencast BEFORE the first navigation: CDP only pushes frames on
        # paints, so starting it afterwards over an already-settled page would
        # never deliver a first frame.
        await self._start_screencast(s)
        try:
            await s.page.goto(start_url, timeout=_CALL_TIMEOUT_S * 1000)
        except Exception:  # noqa: BLE001 — a slow/failed first load is not fatal to the embed
            pass
        with s.frame_lock:
            s.url = getattr(s.page, "url", start_url) or start_url
        if s.screencast_active:
            # Seed a frame if the load's paints landed before ack plumbing was
            # ready (or the page is fully static) — pollers need a first image.
            for _ in range(10):
                with s.frame_lock:
                    if s.frame_seq > 0:
                        break
                await asyncio.sleep(0.1)
            else:
                try:
                    s.store_frame(await s.page.screenshot(type="jpeg", quality=s.quality))
                except Exception:  # noqa: BLE001
                    pass
        s.classify_task = asyncio.get_running_loop().create_task(self._classify_loop(s))
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.get_running_loop().create_task(self._reaper_loop())

    async def _start_screencast(self, s: EmbedSession) -> None:
        """CDP push screencast; degrade to a ~5 fps screenshot poller on failure."""
        try:
            s.cdp = await s.context.new_cdp_session(s.page)
            # Scrub "HeadlessChrome" from Sec-CH-UA (client hints) so it agrees
            # with the UA header — reuse this screencast CDP session.
            await stealth.apply_cdp_ua_async(s.cdp, s.user_agent, s.chromium_version)

            def _on_frame(params: dict) -> None:
                try:
                    s.store_frame(base64.b64decode(params.get("data", "")))
                except Exception:  # noqa: BLE001
                    pass
                sess_id = params.get("sessionId")
                if sess_id is not None and s.cdp is not None:
                    coro = s.cdp.send("Page.screencastFrameAck", {"sessionId": sess_id})
                    asyncio.ensure_future(coro)

            s.cdp.on("Page.screencastFrame", _on_frame)
            await s.cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": s.quality,
                "maxWidth": s.w, "maxHeight": s.h, "everyNthFrame": 1,
            })
            s.screencast_active = True
        except Exception:  # noqa: BLE001 — CDP unavailable → screenshot fallback
            s.screencast_active = False
            s.fallback_task = asyncio.get_running_loop().create_task(self._fallback_frames(s))

    async def _fallback_frames(self, s: EmbedSession) -> None:
        while True:
            try:
                buf = await s.page.screenshot(type="jpeg", quality=s.quality)
                s.store_frame(buf)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — page mid-navigation etc.
                pass
            await asyncio.sleep(_FALLBACK_FRAME_INTERVAL_S)

    async def _classify_loop(self, s: EmbedSession) -> None:
        """Throttled login_state classification: URL every tick (cheap), full
        page.content() classification at most every _CLASSIFY_INTERVAL_S (longer
        while ``challenge`` so content() does not starve screencast/input)."""
        last_content_ts = 0.0
        while True:
            try:
                cur_url = getattr(s.page, "url", "") or ""
                with s.frame_lock:
                    prev_state = s.login_state
                interval = (_CLASSIFY_INTERVAL_S * 3
                            if prev_state == "challenge" else _CLASSIFY_INTERVAL_S)
                content: str | None = None
                now = self._clock()
                if now - last_content_ts >= interval:
                    try:
                        content = await s.page.content()
                        last_content_ts = now
                    except Exception:  # noqa: BLE001 — mid-navigation; retry next tick
                        pass
                if content is not None:
                    state = classify_login_state(s.site, content, cur_url)
                else:
                    state = prev_state or "unknown"
                with s.frame_lock:
                    s.url = cur_url
                    s.login_state = state
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(_CLASSIFY_INTERVAL_S)

    # ------------------------------------------------------------- frame IO

    def _get(self, session_id: str) -> EmbedSession | None:
        with self._sessions_lock:
            return self._sessions.get(session_id)

    def frame(self, session_id: str, since_seq: int | None = None) -> dict:
        """Latest frame WITHOUT touching the browser loop (buffer copy only).
        ``frame`` is None when ``since_seq`` is already current."""
        s = self._get(session_id)
        if s is None:
            return fail("no_such_session", f"no embed session '{session_id}'")
        s.last_touch = self._clock()
        snap = s.snapshot(since_seq)
        snap["ok"] = True
        return snap

    def input(self, session_id: str, events: list[dict]) -> dict:
        """Forward a batch of input events via page.mouse/page.keyboard (robust
        path — full key normalisation, no raw-CDP modifier bookkeeping)."""
        s = self._get(session_id)
        if s is None:
            return fail("no_such_session", f"no embed session '{session_id}'")
        s.last_touch = self._clock()
        try:
            self._call(self._dispatch_events(s, list(events or [])))
        except Exception as exc:  # noqa: BLE001
            return fail("embed_input_failed", str(exc))
        return {"ok": True, "count": len(events or [])}

    async def _dispatch_events(self, s: EmbedSession, events: list[dict]) -> None:
        for ev in events:
            etype = (ev or {}).get("type")
            try:
                if etype == "mousemove":
                    await s.page.mouse.move(float(ev.get("x", 0)), float(ev.get("y", 0)))
                elif etype == "mousedown":
                    button = _MOUSE_BUTTONS.get(ev.get("button", 0), "left")
                    await s.page.mouse.move(float(ev.get("x", 0)), float(ev.get("y", 0)))
                    await s.page.mouse.down(button=button,
                                            click_count=int(ev.get("click_count", 1)))
                elif etype == "mouseup":
                    button = _MOUSE_BUTTONS.get(ev.get("button", 0), "left")
                    await s.page.mouse.up(button=button,
                                          click_count=int(ev.get("click_count", 1)))
                elif etype == "wheel":
                    await s.page.mouse.wheel(float(ev.get("delta_x", 0)),
                                             float(ev.get("delta_y", 0)))
                elif etype == "key":
                    key = ev.get("key")
                    if key:
                        combo = "+".join(list(ev.get("modifiers") or []) + [str(key)])
                        await s.page.keyboard.press(combo)
                elif etype == "char":
                    text = ev.get("text", "")
                    if text:
                        await s.page.keyboard.insert_text(str(text))
            except Exception:  # noqa: BLE001 — one bad event must not kill the batch
                continue

    def navigate(self, session_id: str, url: str) -> dict:
        s = self._get(session_id)
        if s is None:
            return fail("no_such_session", f"no embed session '{session_id}'")
        s.last_touch = self._clock()

        async def _go() -> None:
            await s.page.goto(url, timeout=_CALL_TIMEOUT_S * 1000)
            with s.frame_lock:
                s.url = getattr(s.page, "url", url) or url

        try:
            self._call(_go(), timeout=_CALL_TIMEOUT_S + 5)
        except Exception as exc:  # noqa: BLE001
            return fail("embed_navigate_failed", str(exc))
        return {"ok": True, "url": s.snapshot(None)["url"]}

    # --------------------------------------------------------------- close

    def stop(self, session_id: str) -> dict:
        with self._sessions_lock:
            s = self._sessions.pop(session_id, None)
        if s is None:
            return fail("no_such_session", f"no embed session '{session_id}'")
        try:
            self._call(self._close_session(s))
        except Exception:  # noqa: BLE001 — always release the profile below
            pass
        if s.profile_lock is not None and s.profile_lock.locked():
            try:
                s.profile_lock.release()
            except RuntimeError:
                pass
        return {"ok": True, "id": session_id}

    async def _close_session(self, s: EmbedSession) -> None:
        for task in (s.fallback_task, s.classify_task):
            if task is not None:
                task.cancel()
        if s.screencast_active and s.cdp is not None:
            try:
                await s.cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass
        if s.context is not None:
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass

    def stop_all(self) -> None:
        with self._sessions_lock:
            ids = list(self._sessions)
        for sid in ids:
            try:
                self.stop(sid)
            except Exception:  # noqa: BLE001 — best-effort at exit
                pass

    # --------------------------------------------------------------- reaper

    def _reap_idle(self) -> list[str]:
        """Close sessions not polled for idle_timeout. Split out (pure clock
        arithmetic + stop calls) so tests can drive it with an injected clock."""
        now = self._clock()
        with self._sessions_lock:
            stale = [sid for sid, s in self._sessions.items()
                     if now - s.last_touch > self._idle_timeout]
        for sid in stale:
            self.stop(sid)
        return stale

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL_S)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._reap_idle)
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- wizard

    def wizard_active(self) -> bool:
        with self._wizard_lock:
            return self._wizard is not None

    def _wizard_default_sites(self) -> list[str]:
        # Walk EVERY login-gated vendor, not just the ones lacking a stamp. A
        # saved session may have silently expired, and a stamp is not proof of a
        # live login — so we always offer all of them and let the live per-site
        # classification auto-advance the ones that are genuinely still signed
        # in (see wizard_status). This is what guarantees the user is prompted
        # for every provider that needs a login for downloads/datasheets.
        try:
            from . import adapters
            return adapters.login_wizard_sites()
        except Exception:  # noqa: BLE001
            return []

    def wizard_start(self, sites: list[str] | None = None,
                     mode: str = "embed") -> dict:
        """Begin the sequential login wizard.

        ``mode="embed"`` (legacy/tests): ONE worker-side embedded Playwright
        session at a time, opened at each site's LOGIN_URL; the classify loop
        auto-advances on logged_in.

        ``mode="client"`` (the in-process CEF GUI browser): PURE BOOKKEEPING —
        no worker-side browser is launched (the user signs in inside the GUI's
        own Chromium, so a worker page would just sit unattended on the login
        URL forever, holding the site's profile lock and blocking fetches).
        The client reports the outcome of each step via
        :meth:`wizard_next(skip=..., logged_in=...)` and hands the session
        over with :func:`import_session_cookies`. Needs no Playwright at all.
        """
        client_mode = (mode == "client")
        with self._wizard_lock:
            if self._wizard is not None:
                return fail("wizard_active", "a login wizard is already running — "
                                             "login_wizard_stop it first")
            explicit = bool(sites)
            site_list = list(sites) if sites else self._wizard_default_sites()
            # Client mode (in-process CEF GUI) has no auto-advance to skip sites the
            # user is already signed into, so it would re-open every vendor — even
            # ones with a live cookie session — and make the user click through them.
            # Filter the DEFAULT multi-site list down to vendors that still need a
            # login (has_login stamp absent/false). Never filter an EXPLICIT
            # single-vendor launch ("Sign in" on one row): the user asked for that
            # site regardless of stamp.
            if client_mode and not explicit:
                site_list = [s for s in site_list if not profiles.has_login(s)]
            if not site_list:
                return {"ok": True, "sites": [], "all_done": True,
                        "current_site": None, "embed_id": None}
            self._wizard = {"sites": site_list, "idx": 0, "done": [], "skipped": [],
                            "embed_id": None, "saw_auth_wall": False,
                            "opened_at_sign_in": False, "mode": mode}
        if client_mode:
            return {"ok": True, "sites": site_list, "current_site": site_list[0],
                    "embed_id": None, "all_done": False}
        res = self._wizard_open_current()
        if not res.get("ok"):
            with self._wizard_lock:
                self._wizard = None
            return res
        return {"ok": True, "sites": site_list, "current_site": site_list[0],
                "embed_id": res["id"], "all_done": False}

    def _wizard_open_current(self) -> dict:
        with self._wizard_lock:
            wiz = self._wizard
            if wiz is None:
                return fail("no_wizard", "login wizard is not running")
            site = wiz["sites"][wiz["idx"]]
        site_slug = str(site).strip().lower()
        use_headless = True
        res = self.start(site=site, max_width=1280, max_height=900, quality=80,
                         headless=use_headless)
        if res.get("ok"):
            with self._wizard_lock:
                if self._wizard is not None:
                    self._wizard["embed_id"] = res["id"]
                    self._wizard["saw_auth_wall"] = False
                    self._wizard["opened_at_sign_in"] = _opened_at_sign_in(site)
        return res

    def wizard_status(self) -> dict:
        """Current wizard position. Auto-advances when the live session has
        reached ``login_state == "logged_in"`` **after** the user was shown a
        login/challenge page on this step (prevents homepage false-positives).
        The other advance path is an explicit :meth:`wizard_next` from the client."""
        with self._wizard_lock:
            wiz = self._wizard
            if wiz is None:
                return {"ok": True, "active": False, "current_site": None, "embed_id": None,
                        "done": [], "remaining": [], "all_done": False}
            embed_id = wiz["embed_id"]
            current_site = wiz["sites"][wiz["idx"]] if wiz["idx"] < len(wiz["sites"]) else ""
        state = ""
        if embed_id is not None:
            s = self._get(embed_id)
            if s is not None:
                snap = s.snapshot(None)
                state = snap["login_state"]
                page_url = snap.get("url") or ""
                if state in ("login_page", "challenge"):
                    with self._wizard_lock:
                        if self._wizard is not None:
                            self._wizard["saw_auth_wall"] = True
                saw_wall = False
                opened_sign_in = False
                with self._wizard_lock:
                    if self._wizard is not None:
                        saw_wall = bool(self._wizard.get("saw_auth_wall"))
                        opened_sign_in = bool(self._wizard.get("opened_at_sign_in"))
                redirected = (_redirected_to_post_login(current_site, page_url)
                              if opened_sign_in else False)
                should_advance = (state == "logged_in"
                                  and (saw_wall or (opened_sign_in and redirected)))
                if should_advance:
                    return self.wizard_next(skip=False)
        return self._wizard_status_dict()

    def _wizard_status_dict(self) -> dict:
        with self._wizard_lock:
            wiz = self._wizard
            if wiz is None:
                return {"ok": True, "active": False, "current_site": None, "embed_id": None,
                        "done": [], "remaining": [], "all_done": True}
            remaining = wiz["sites"][wiz["idx"]:]
            return {"ok": True, "active": True,
                    "current_site": wiz["sites"][wiz["idx"]] if remaining else None,
                    "embed_id": wiz["embed_id"],
                    "done": list(wiz["done"]), "skipped": list(wiz["skipped"]),
                    "remaining": list(remaining), "all_done": not remaining}

    def wizard_next(self, skip: bool = False, logged_in: bool | None = None) -> dict:
        """Finish the current site (record success unless ``skip``) and open the
        next one; returns the new status (``all_done`` when the list is spent).

        In client mode there is no worker session to classify, so the CLIENT's
        verdict (``logged_in``, from the GUI browser's live login-state
        classification) decides whether this step counts as a sign-in."""
        with self._wizard_lock:
            wiz = self._wizard
            if wiz is None:
                return fail("no_wizard", "login wizard is not running")
            site = wiz["sites"][wiz["idx"]]
            embed_id = wiz["embed_id"]
            client_mode = wiz.get("mode") == "client"
        current_state = "unknown"
        if embed_id is not None:
            s = self._get(embed_id)
            if s is not None:
                current_state = s.snapshot(None)["login_state"]

        if embed_id is not None:
            self.stop(embed_id)  # releases the site profile lock BEFORE save_meta
        if client_mode:
            mark_logged_in = (not skip) and bool(logged_in)
        else:
            mark_logged_in = (not skip) and (current_state == "logged_in")
        if mark_logged_in:
            try:
                profiles.touch_created(site)
                # login_verified_ts is the signal has_login() trusts — set it ONLY
                # here, where the live page actually classified as logged_in.
                now = time.time()
                profiles.save_meta(site, last_login_ts=now, login_verified_ts=now)
            except Exception:  # noqa: BLE001 — bookkeeping only
                pass

        with self._wizard_lock:
            wiz = self._wizard
            if wiz is None:
                return fail("no_wizard", "login wizard is not running")
            (wiz["done"] if mark_logged_in else wiz["skipped"]).append(site)
            wiz["idx"] += 1
            wiz["embed_id"] = None
            wiz["saw_auth_wall"] = False
            wiz["opened_at_sign_in"] = False
            finished = wiz["idx"] >= len(wiz["sites"])
            if finished:
                self._wizard = None
                return {"ok": True, "active": False, "current_site": None, "embed_id": None,
                        "done": list(wiz["done"]), "skipped": list(wiz["skipped"]),
                        "remaining": [], "all_done": True}
        if client_mode:
            return self._wizard_status_dict()   # the GUI opens the next site itself
        res = self._wizard_open_current()
        if not res.get("ok"):
            return res
        return self._wizard_status_dict()

    def wizard_stop(self) -> dict:
        with self._wizard_lock:
            wiz = self._wizard
            self._wizard = None
        if wiz and wiz.get("embed_id"):
            self.stop(wiz["embed_id"])
        return {"ok": True}


# --------------------------------------------------------------------------
# Process-wide singleton (services/parts/embed_rpc.py worker RPCs use it)

_MANAGER: EmbedManager | None = None
_MANAGER_GUARD = threading.Lock()


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_MANAGER_GUARD):
        manager = _MANAGER
    if manager is None:
        return {"by_kind": {"browser_sessions": 0, "browser_calls": 0}}
    with registry_lock(manager._sessions_lock):
        sessions = len(manager._sessions)
    with registry_lock(manager._activity_lock):
        calls = manager._activity_calls
    return {"by_kind": {"browser_sessions": sessions, "browser_calls": calls}}


def get_manager() -> EmbedManager:
    global _MANAGER
    with _MANAGER_GUARD:
        if _MANAGER is None:
            _MANAGER = EmbedManager()
        return _MANAGER

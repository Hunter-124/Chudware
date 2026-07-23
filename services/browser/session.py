"""services/browser/session.py — persistent per-site browser sessions (T-X1).

Two entry points only; every source adapter (T-X2+) calls these and never
imports Playwright directly:

- :func:`browser_login` — opens a HEADED, persistent-profile Chromium window so
  the user can sign into ``site`` in their own browser; the profile (cookies,
  local storage, ...) is saved to disk under the app data dir
  (:mod:`services.browser.profiles`) and is LOCAL ONLY — never synced, never
  uploaded, never read by this module into logs or metadata.
- :func:`browser_fetch` — loads a URL HEADLESS under the user's saved session
  in our OWN CEF Chromium (:mod:`.cef_client` → the ``Chudware --cef-daemon``
  host), rate-limited per site (:mod:`services.browser.ratelimit`). Returns
  rendered HTML. Driving CEF through its native C++ API leaves no CDP
  ``Runtime.enable`` handshake, so the automation-protocol fingerprint that bot
  gates key on is absent (the reason we dropped Playwright — see the plan).
- :func:`browser_download` (T-X2) — fetches a URL's raw bytes under the same
  saved session via the daemon's direct ``CefURLRequest`` (no page navigation),
  the right primitive for CAD/PDF downloads. Same rate-limiting and degradation
  posture as ``browser_fetch``.

``browser_login`` still opens a headed Playwright window as a CLI/fallback path;
the primary login surface is the interactive CEF panel in the GUI, and this
headed fallback is retired once the panel is migrated onto the daemon (plan
Phase 2b). The CEF daemon is lazily spawned and idle-exits, and all three
functions degrade to the ``services.research`` §6 failure shape
(``contracts/research_tools.md``) instead of raising.

Captcha/2FA (T-X6): this module DETECTS these states (see :mod:`.challenge`) but
never attempts to solve or bypass them. The rule is the same regardless — hand
control back to the user: ``browser_login`` (headed) surfaces a clear "complete it
yourself" message and keeps the window open; ``browser_fetch`` (headless) aborts
with a "needs manual re-login" result rather than returning the challenge page or
retrying.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from pathlib import Path
from typing import Any, Callable

from services.research.common import fail, now_ts

from . import challenge, profiles, ratelimit, stealth

#: browser_login re-checks the page for a captcha/2FA prompt this often (seconds)
#: while waiting for the user to finish and close the window.
_LOGIN_POLL_INTERVAL_S = 2.0

#: How long browser_login waits (seconds) for the user's login window to close
#: on its own before this module closes it for them.
DEFAULT_LOGIN_TIMEOUT_S = 300.0
#: After navigation, how long (seconds) to let the network settle so XHR-driven
#: single-page-app catalogs (McMaster, Misumi, DigiKey, Amazon, Home Depot, …)
#: finish rendering their product cards before we snapshot the DOM. Bounded and
#: best-effort — a chatty page that never idles just gets snapshotted at the
#: deadline (still far better than the pre-render empty shell).
DEFAULT_RENDER_SETTLE_S = 8.0


def _busy(site: str) -> dict:
    """§6 degradation for Chromium ProcessSingleton contention on a site profile."""
    return fail("profile_busy",
                f"site profile '{site}' is open in the embedded browser — close that "
                f"session (or finish the login wizard) and retry")


def _apply_staged_cookies(context: Any, site: str) -> None:
    """Inject a GUI-exported cookie jar staged by the login wizard (CEF →
    Playwright hand-off, see embed.import_session_cookies). One-shot: the
    sidecar is consumed and the cookies persist in the profile afterwards."""
    try:
        from .embed import apply_pending_cookies_sync  # lazy: avoid import cycle
        apply_pending_cookies_sync(context, site)
    except Exception:  # noqa: BLE001 — hand-off is best-effort, never fatal
        pass


def _import_sync_playwright() -> Callable[[], Any]:
    """Resolve residual sync Playwright factory via :mod:`.driver`.

    Default ``CHUDWARE_PW_BACKEND=cef`` keeps stock ``playwright`` (byte-identical
    to the pre-factory import). ``patchright`` / ``camoufox`` selected via env.
    """
    from . import driver as _driver
    return _driver.resolve_sync()


def _off_loop(fn: Callable[..., dict], *args: Any, **kwargs: Any) -> dict:
    """Run a Playwright-Sync-API body without a running asyncio loop.

    Playwright's Sync API refuses to start inside a live asyncio event loop
    ("Please use the Async API instead"), which is exactly the context the MCP
    servers (parts_hub / vendors_health) run in — every ``browser_fetch`` from
    there used to blow up and degrade to ``browser_fetch_failed``. When a loop
    IS running on this thread we hand the sync body to a fresh worker thread
    (which has no loop of its own) and block on its result; when no loop is
    running (CLI, tests, plain sync callers) we just call it inline.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)  # no loop on this thread — safe to run inline
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def browser_login(site: str, *, timeout: float = DEFAULT_LOGIN_TIMEOUT_S,
                  _sync_playwright: Callable[[], Any] | None = None) -> dict:
    """Open a HEADED browser window for the user to sign into ``site``.

    Blocks up to ``timeout`` seconds while the window stays open; returns as
    soon as the user closes it (closing the window is how the user signals
    "I'm done logging in"). Only bookkeeping is returned — never credentials.

    Hard reCAPTCHA vendors (Anaheim Magento et al., see
    :func:`_site_wants_native_chrome`) open **system Google Chrome as a plain
    subprocess** with zero Playwright/CDP control during the sign-in — any
    automation protocol attachment makes Google return "reCAPTCHA verification
    failed" even with valid credentials (proven live 2026-07-10). Cookies are
    exported after success/close and pushed into the CEF warm profile.
    """
    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        # Test injection of ``_sync_playwright`` always takes the Playwright path
        # so unit fakes keep working. Production hard-sites prefer native Chrome.
        if _sync_playwright is None and _site_wants_native_chrome(site) \
                and _chrome_bin() is not None:
            return _off_loop(_browser_login_native_chrome, site, timeout=timeout)
        try:
            sync_playwright = _sync_playwright or _import_sync_playwright()
        except RuntimeError as exc:
            # Hard-site fallback only — ordinary sites still need Playwright.
            if _site_wants_native_chrome(site) and _chrome_bin() is not None:
                return _off_loop(_browser_login_native_chrome, site, timeout=timeout)
            return fail("playwright_not_installed", str(exc))
        return _off_loop(_browser_login_locked, site, timeout=timeout,
                         sync_playwright=sync_playwright)
    finally:
        lock.release()


def _push_cookies_to_cef_warm(cookies: list) -> None:
    """Best-effort: inject a headed-login jar into the CEF daemon warm profile so
    headless ``browser_fetch`` / download reuse the session (same carry path as
    the in-panel CEF export). Never raises — a cold/unavailable daemon must not
    fail the user's login."""
    if not cookies:
        return
    try:
        from . import cef_client
        cef_client.client().import_session(list(cookies))
    except Exception:  # noqa: BLE001
        pass


def _chrome_bin() -> str | None:
    """Absolute path to system Google Chrome, or None."""
    import shutil
    return (shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser"))


def _chrome_channel_available() -> bool:
    """True when a system Google Chrome install is on PATH."""
    return _chrome_bin() is not None


def _site_wants_native_chrome(site: str) -> bool:
    """True when Playwright-driven login cannot pass the vendor's bot score.

    Adapters set ``NATIVE_CHROME_LOGIN = True`` (or the attr on the instance).
    Live-proven: Anaheim Magento MSP invisible reCAPTCHA rejects any session
    where Playwright/CDP controlled the page, even headed real Chrome + valid
    credentials ("reCAPTCHA verification failed").
    """
    slug = profiles.slug(site)
    try:
        from . import adapters
        ad = adapters.get_adapter(slug)
    except Exception:  # noqa: BLE001
        ad = None
    if ad is not None:
        for name in ("NATIVE_CHROME_LOGIN", "prefer_native_chrome_login"):
            val = getattr(ad, name, None)
            if val is not None:
                return bool(val)
            mod = getattr(type(ad), "__module__", "")
            try:
                import sys
                m = sys.modules.get(mod)
                val = getattr(m, name, None) if m is not None else None
                if val is not None:
                    return bool(val)
            except Exception:  # noqa: BLE001
                pass
    return slug in _NATIVE_CHROME_SITES


#: Hard-coded fallback when an adapter has not grown the flag yet.
_NATIVE_CHROME_SITES = frozenset({"anaheimautomation"})


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _devtools_tabs(port: int) -> list[dict]:
    """Read Chrome's ``/json`` endpoint (no page injection — safe during login)."""
    import json
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1.5) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def _url_looks_post_login(site: str, url: str) -> bool:
    """Heuristic: tab left the auth surface for a logged-in destination."""
    u = (url or "").lower()
    if not u.startswith("http"):
        return False
    auth_bits = ("/login", "/signin", "/sign-in", "/sign_in", "/register",
                 "/create", "/forgot", "/ap/signin", "/customer/account/login")
    if any(b in u for b in auth_bits):
        return False
    slug = profiles.slug(site)
    if slug == "anaheimautomation" or "anaheimautomation.com" in u:
        return "/customer/account" in u
    # Generic: non-auth URL on a real host is a weak positive — final stamp still
    # goes through classify_login_state when we can read content.
    return True


def _export_cookies_cdp(port: int) -> list:
    """One-shot cookie dump via connect_over_cdp (call only AFTER login succeeded
    or the user is closing — never during captcha interaction)."""
    try:
        sync_playwright = _import_sync_playwright()
    except RuntimeError:
        return []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if not browser.contexts:
                return []
            return list(browser.contexts[0].cookies() or [])
            # Leave the CDP session to fall out of scope — do not browser.close()
            # (on some Playwright builds that tears down the real Chrome).
    except Exception:  # noqa: BLE001
        return []

def _export_cookies_relaunch(profile_path: Path) -> list:
    """Open the on-disk profile headless (Chrome already exited) and dump cookies."""
    try:
        sync_playwright = _import_sync_playwright()
    except RuntimeError:
        return []
    try:
        with sync_playwright() as pw:
            kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_path),
                "headless": True,
                "args": ["--password-store=basic", "--no-first-run"],
            }
            if _chrome_channel_available():
                kwargs["channel"] = "chrome"
            try:
                ctx = pw.chromium.launch_persistent_context(**kwargs)
            except Exception:
                kwargs.pop("channel", None)
                ctx = pw.chromium.launch_persistent_context(**kwargs)
            try:
                return list(ctx.cookies() or [])
            finally:
                ctx.close()
    except Exception:  # noqa: BLE001
        return []


def _finalize_login_cookies(site: str, profile_path: Path, *,
                            exported_cookies: list,
                            last_url: str,
                            last_content: str | None,
                            channel: str) -> dict:
    """Stage cookies, stamp verification, push CEF warm — shared by both login paths."""
    profiles.touch_created(site)
    now = time.time()
    fields: dict[str, Any] = {"last_login_ts": now, "login_channel": channel}
    login_verified = False
    try:
        from .embed import classify_login_state, import_session_cookies
        if last_content is not None and \
                classify_login_state(site, last_content, last_url) == "logged_in":
            login_verified = True
        elif last_url and _url_looks_post_login(site, last_url):
            login_verified = True
        if login_verified:
            fields["login_verified_ts"] = now
        if exported_cookies:
            try:
                import_session_cookies(site, exported_cookies, verified=login_verified)
            except Exception:  # noqa: BLE001
                pass
            import threading
            threading.Thread(target=_push_cookies_to_cef_warm, args=(exported_cookies,),
                             daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    meta = profiles.save_meta(site, **fields)
    return {"ok": True, "site": meta["site"], "profile_dir": str(profile_path),
            "last_login_ts": meta["last_login_ts"],
            "login_verified": bool(meta.get("login_verified_ts")),
            "challenge_detected": None,
            "cookies_exported": len(exported_cookies),
            "channel": channel,
            "last_url": last_url or ""}


def _browser_login_native_chrome(site: str, *, timeout: float) -> dict:
    """System Chrome subprocess — zero automation during the user's sign-in.

    Polls Chrome's ``/json`` tab list (HTTP, no Runtime.enable) for a post-login
    URL, then exports cookies. Falls back to profile relaunch dump when the
    user closes the window first. Cookies are staged for Playwright headless
    reuse and pushed into the CEF daemon warm profile (headless fetch path).

    Requires a real display — reCAPTCHA sites cannot complete under pure
    headless automation. Callers without DISPLAY get a structured failure
    pointing at the headed path.
    """
    import os
    import subprocess

    chrome = _chrome_bin()
    if not chrome:
        return fail("browser_login_failed", "no system Chrome/Chromium on PATH")
    # Wayland/X11: need a real session. Pure headless (no DISPLAY/WAYLAND) cannot
    # complete Magento MSP reCAPTCHA — fail clearly instead of hanging Chrome.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return fail(
            "browser_login_needs_display",
            f"{profiles.slug(site)} requires headed system Chrome (reCAPTCHA); "
            "no DISPLAY/WAYLAND_DISPLAY — run vendor_login from the desktop GUI "
            "once, then headless browser_fetch reuses the saved session",
        )

    profile_path = profiles.ensure_profile(site)
    try:
        from .embed import start_url_for_site
        start = start_url_for_site(site) or "about:blank"
    except Exception:  # noqa: BLE001
        start = "about:blank"

    port = _free_port()
    cmd = [
        chrome,
        f"--user-data-dir={str(profile_path)}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--password-store=basic",
        # Do NOT pass --enable-automation / headless / app mode — reCAPTCHA
        # scores those. Remote debugging port alone is fine while nothing
        # attaches CDP domains during the challenge.
        start,
    ]
    print(
        f"[browser_login] {site}: opened system Chrome (no automation).\n"
        f"  1) Click Allow Cookies if the banner appears\n"
        f"  2) Sign in with your email/password (complete any captcha yourself)\n"
        f"  3) Wait until you see your account page, then close the window",
        flush=True,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        return fail("browser_login_failed", f"failed to launch Chrome: {exc}")

    last_url = ""
    last_content: str | None = None
    exported: list = []
    saw_success = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # user closed the browser
            try:
                for t in _devtools_tabs(port):
                    if t.get("type") != "page":
                        continue
                    u = t.get("url") or ""
                    if u:
                        last_url = u
                    if u and _url_looks_post_login(site, u):
                        saw_success = True
                        # One CDP attach AFTER success only — dump cookies + HTML.
                        try:
                            sync_playwright = _import_sync_playwright()
                            with sync_playwright() as pw:
                                b = pw.chromium.connect_over_cdp(
                                    f"http://127.0.0.1:{port}")
                                if b.contexts:
                                    ctx = b.contexts[0]
                                    exported = list(ctx.cookies() or [])
                                    page = next(
                                        (p for p in ctx.pages
                                         if "login" not in (p.url or "").lower()),
                                        ctx.pages[0] if ctx.pages else None,
                                    )
                                    if page is not None:
                                        try:
                                            last_content = page.content()
                                            last_url = page.url or last_url
                                        except Exception:  # noqa: BLE001
                                            pass
                        except Exception:  # noqa: BLE001
                            if not exported:
                                exported = _export_cookies_cdp(port)
                        break
            except Exception:  # noqa: BLE001 — devtools not up yet / mid-nav
                pass
            if saw_success:
                break
            time.sleep(_LOGIN_POLL_INTERVAL_S)
    finally:
        if proc.poll() is None:
            try:
                if not exported:
                    exported = _export_cookies_cdp(port)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # Always try profile relaunch dump if CDP export was empty (user closed
        # the window before we could connect).
        if not exported:
            time.sleep(0.4)  # let ProcessSingleton release the profile
            exported = _export_cookies_relaunch(profile_path)

    return _finalize_login_cookies(
        site, profile_path,
        exported_cookies=exported,
        last_url=last_url,
        last_content=last_content,
        channel="native-chrome",
    )


# Magento (Anaheim et al.) often leaves the user on ``/login/referer/<base64>/``
# after a failed captcha/form_key OR via a self-referential header Sign-In link
# that encodes the login page as its own post-login destination. Two helpers:
#
# 1) On the login form, seed Magento's ``login_redirect`` cookie + strip broken
#    self-referer from header "Sign In" links so a successful POST lands on the
#    account dashboard instead of bouncing back onto /login/referer/….
# 2) If the session actually took (customer-welcome / logout link) while the URL
#    is still on /login…, hop once to /customer/account/ so classify_login_state
#    can stamp verified and cookie export captures a post-login jar.
#
# Observation + cookie/link fix + one navigation. Never fills credentials.
_MAGENTO_LOGIN_FIXUP_JS = r"""
(() => {
  try {
    const path = (location.pathname || '').toLowerCase();
    if (!path.includes('/customer/account/login')) return 'skip';
    // Prefer account dashboard after a good login (Magento AccountRedirect).
    try {
      const dest = location.origin + '/customer/account/';
      document.cookie = 'login_redirect=' + encodeURIComponent(dest)
        + '; path=/; SameSite=Lax; max-age=600'
        + (location.protocol === 'https:' ? '; Secure' : '');
    } catch (e) {}
    // Rewrite header "Sign In" links that point at /login/referer/<login-itself>/
    // (Anaheim's storefront ships this self-loop; base64 of the login URL ends
    // with the login path and keeps users on a login/referer page forever).
    try {
      for (const a of document.querySelectorAll('a[href*="/customer/account/login/referer/"]')) {
        const href = a.getAttribute('href') || '';
        // Magento encode of …/customer/account/login/ contains "L2xvZ2lu" ( /login )
        // and of …/customer/account/ does not end the payload with "login".
        if (/login\/referer\/[^/]*bG9naW4/i.test(href) || /login\/referer\/[^/]*sb2dpbi/i.test(href)) {
          a.setAttribute('href', location.origin + '/customer/account/login/');
        }
      }
    } catch (e) {}
    const signed = !!(
      document.querySelector('.customer-welcome') ||
      document.querySelector('a[href*="customer/account/logout"]') ||
      /log\s*out|sign\s*out/i.test(document.body ? document.body.innerText.slice(0, 4000) : '')
    );
    if (signed) {
      location.replace(location.origin + '/customer/account/');
      return 'hop';
    }
    return 'fixed';
  } catch (e) { return 'err'; }
})();
"""


def _browser_login_locked(site: str, *, timeout: float, sync_playwright: Callable[[], Any]) -> dict:
    profile_path = profiles.profile_dir(site)
    challenge_detected: str | None = None
    last_content: str | None = None
    last_url: str = ""
    exported_cookies: list = []
    launch_channel: str | None = None
    try:
        with sync_playwright() as pw:
            ver = stealth.chromium_version_sync(pw)
            ua  = stealth.user_agent(ver)
            # Prefer real Google Chrome for headed login when installed: Playwright's
            # bundled Chromium + any WebGL hook is a reCAPTCHA bot tell (Anaheim
            # Magento MSP invisible). channel="chrome" uses the system browser
            # with a real GPU fingerprint while still writing our site profile.
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_path),
                "headless": False,
                "args": stealth.launch_args(headless=False),
                **stealth.context_options(ua),
            }
            if _chrome_channel_available():
                launch_kwargs["channel"] = "chrome"
                launch_channel = "chrome"
            try:
                context = pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                # Channel missing / broken install → plain Chromium still usable.
                launch_kwargs.pop("channel", None)
                launch_channel = None
                context = pw.chromium.launch_persistent_context(**launch_kwargs)
            # headed=True: NO WebGL getParameter hook (reCAPTCHA tell on real GPU).
            stealth.harden_context_sync(context, headed=True)
            _apply_staged_cookies(context, site)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                stealth.harden_page_sync(context, page, ua, ver)
                # Land on the vendor sign-in page (was blank about:blank before —
                # users had to type the URL by hand).
                try:
                    from .embed import start_url_for_site
                    start = start_url_for_site(site)
                    if start:
                        page.goto(start, wait_until="domcontentloaded", timeout=60_000)
                except Exception:  # noqa: BLE001 — blank window is still usable
                    pass
                # Poll for the user closing the window. Between polls, if the page shows
                # a captcha/2FA we surface a clear "do it yourself" message ONCE per kind
                # and record it — we never click, solve, or bypass it. The user completes
                # it in this headed window (real GPU / real Chrome) and closes it when done.
                announced: set[str] = set()
                magento_hop_done = False
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        last_content, last_url = page.content(), page.url
                        kind = challenge.classify_challenge(last_content, last_url)
                    except Exception:  # noqa: BLE001 — page mid-navigation / closing
                        kind, last_content = None, None
                    if kind:
                        challenge_detected = kind
                        if kind not in announced:
                            announced.add(kind)
                            print(f"[browser_login] {site}: {kind.upper()} detected — "
                                  f"please complete it in the browser window. This tool "
                                  f"will not attempt to solve or bypass it.", flush=True)
                    # Magento: seed login_redirect + unbreak self-referer links;
                    # if the session already took on a /login/referer/… URL, hop
                    # once to the account dashboard (window still open).
                    if (last_url and "/customer/account/login" in last_url.lower()):
                        try:
                            result = page.evaluate(_MAGENTO_LOGIN_FIXUP_JS)
                            if result == "hop" and not magento_hop_done:
                                magento_hop_done = True
                                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                                last_content, last_url = page.content(), page.url
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        page.wait_for_event("close", timeout=_LOGIN_POLL_INTERVAL_S * 1000)
                        break  # window closed → the user signalled "done"
                    except Exception:  # noqa: BLE001 — poll interval elapsed, still open
                        continue
                # Snapshot cookies BEFORE context.close() wipes the live jar from
                # memory (disk profile still has them, but CEF warm needs an explicit
                # import — Playwright and CEF do not share OSCrypt/cookie stores).
                try:
                    exported_cookies = list(context.cookies() or [])
                except Exception:  # noqa: BLE001
                    exported_cookies = []
            finally:
                context.close()
    except Exception as exc:  # noqa: BLE001 — never crash the caller on a browser-launch error
        return fail("browser_login_failed", str(exc))

    profiles.touch_created(site)
    # Only stamp login_verified_ts (the signal has_login trusts) when the last
    # page we saw before the user closed the window actually classified as
    # logged_in — best-effort; if we couldn't read it, degrade to not-verified.
    now = time.time()
    fields: dict[str, Any] = {"last_login_ts": now}
    if launch_channel:
        fields["login_channel"] = launch_channel
    login_verified = False
    try:
        from .embed import classify_login_state, import_session_cookies  # lazy
        if last_content is not None and \
                classify_login_state(site, last_content, last_url) == "logged_in":
            fields["login_verified_ts"] = now
            login_verified = True
        # Stage + push to CEF warm so the headless daemon (browser_fetch) sees the
        # same session the user just completed in real Chrome — critical for
        # reCAPTCHA-hard vendors (e.g. Anaheim Magento) where in-panel OSR CEF
        # fails Google's bot score under software GL.
        if exported_cookies:
            try:
                import_session_cookies(site, exported_cookies, verified=login_verified)
            except Exception:  # noqa: BLE001
                pass
            import threading
            threading.Thread(target=_push_cookies_to_cef_warm, args=(exported_cookies,),
                             daemon=True).start()
    except Exception:  # noqa: BLE001 — verification / carry is best-effort, never fatal
        pass
    meta = profiles.save_meta(site, **fields)
    return {"ok": True, "site": meta["site"], "profile_dir": str(profile_path),
            "last_login_ts": meta["last_login_ts"],
            "login_verified": bool(meta.get("login_verified_ts")),
            "challenge_detected": challenge_detected,
            "cookies_exported": len(exported_cookies),
            "channel": launch_channel or "chromium"}


def browser_fetch(url: str, site: str, *,
                  render_settle: float = DEFAULT_RENDER_SETTLE_S,
                  min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                  _cef: Any | None = None) -> dict:
    """Fetch ``url`` HEADLESS under ``site``'s saved session, via our own CEF
    Chromium daemon (:mod:`.cef_client`) — no Playwright, no CDP automation tell.

    Returns the §6 failure shape (never raises) when the CEF daemon is
    unavailable or navigation fails. A missing profile is auto-created (anonymous
    browsing of public catalogs is allowed — login-gated content still degrades to
    login_required via the adapters).
    """
    profiles.ensure_profile(site)
    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        return _browser_fetch_locked(url, site, render_settle=render_settle,
                                     min_interval=min_interval, cef=_cef)
    finally:
        lock.release()


def _browser_fetch_locked(url: str, site: str, *, render_settle: float,
                          min_interval: float, cef: Any | None = None) -> dict:
    ceiling = ratelimit.reserve_request(site)
    if ceiling is not None:
        return fail(ceiling, f"{site}: per-session request ceiling reached — this run has "
                             f"already made the maximum allowed fetches (a runaway-loop guard). "
                             f"Nothing was fetched.")
    ratelimit.wait(site, min_interval=min_interval, url=url)
    status: int | None = None
    content = ""
    try:
        try:
            from . import cef_client  # lazy: keep import cheap for non-browser callers
            r = (cef or cef_client.client()).fetch(
                url, settle_ms=int(render_settle * 1000))
            if not r.get("ok"):
                return fail("browser_fetch_failed",
                            f"{site}: CEF fetch of {url} did not complete")
            status = r.get("status") or None
            content = r.get("html", "")
        except Exception as exc:  # noqa: BLE001 — daemon spawn/connect/protocol
            return fail("browser_fetch_failed", str(exc))
    finally:
        # Drives inter-request spacing AND 429/403 exponential backoff off the
        # real status (None on error/challenge → clears any backoff, keeps spacing).
        ratelimit.note_result(site, status)

    # A captcha/2FA interstitial is NOT real content. Headless has no human to hand
    # off to, so abort — never return the challenge page as a success, never retry or
    # attempt to solve it. The saved session has almost certainly expired; the caller
    # surfaces this and the user re-logins headed.
    # A challenge/interstitial page is not usable content — skip parsing it so its
    # markup is never ingested as product data (data-quality signal, see
    # :mod:`.challenge`). With the consistent fingerprint most false flags don't
    # appear; a genuine one just yields no content for this URL.
    challenge_kind = challenge.classify_challenge(content, url)
    if challenge_kind:
        return fail("challenge_page",
                    f"{site} returned a {challenge_kind} page for {url} — no product "
                    f"content to parse")

    # An HTTP error page is not real content either — surface it distinctly so
    # the health board can tell "bot-walled" (403/429 from Akamai/Cloudflare/PX)
    # from "parser broke" (live-probe 2026-07-07: these used to come back ok:True
    # and adapters reported a silent empty result).
    if status is not None and status >= 400:
        reason = "bot_walled" if status in (403, 429) else "http_error"
        return fail(reason, f"{site} returned HTTP {status} for {url}; the site is "
                            f"likely blocking automated access — a signed-in session "
                            f"(browser_login('{site}')) usually helps")

    profiles.save_meta(site, last_used_ts=time.time())
    return {"ok": True, "url": url, "status": status, "content": content,
            "fetched_ts": now_ts(), "session_profile_used": profiles.slug(site)}


def browser_interact(url: str, site: str, script: str, *,
                     sentinel: str = "__aicad_capture",
                     settle: float = DEFAULT_RENDER_SETTLE_S,
                     timeout: float = 12.0,
                     min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                     _cef: Any | None = None) -> dict:
    """Load ``url`` HEADLESS under ``site``'s session, run ``script`` in the page,
    and return the settled HTML once ``script`` publishes a result into a sentinel
    DOM element (id ``sentinel``). Same posture as :func:`browser_fetch` (rate limit,
    profile lock, §6 degradation) — the primitive for SPA configurators whose
    download URL is generated client-side (an adapter runs page JS to fetch a
    same-origin config endpoint / drive a control, then reads the sentinel out of the
    returned ``content``)."""
    profiles.ensure_profile(site)
    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        ceiling = ratelimit.reserve_request(site)
        if ceiling is not None:
            return fail(ceiling, f"{site}: per-session request ceiling reached — this run "
                                 f"has already made the maximum allowed fetches (a "
                                 f"runaway-loop guard). Nothing was fetched.")
        ratelimit.wait(site, min_interval=min_interval, url=url)
        status: int | None = None
        content = ""
        try:
            try:
                from . import cef_client  # lazy import
                r = (_cef or cef_client.client()).interact(
                    url, script, sentinel=sentinel,
                    settle_ms=int(settle * 1000), timeout_ms=int(timeout * 1000))
                if not r.get("ok"):
                    return fail("browser_interact_failed",
                                f"{site}: CEF interact on {url} did not complete")
                status = r.get("status") or None
                content = r.get("html", "")
            except Exception as exc:  # noqa: BLE001 — daemon spawn/connect/protocol
                return fail("browser_interact_failed", str(exc))
        finally:
            ratelimit.note_result(site, status)

        challenge_kind = challenge.classify_challenge(content, url)
        if challenge_kind:
            return fail("challenge_page",
                        f"{site} returned a {challenge_kind} page for {url} — no usable "
                        f"content")
        profiles.save_meta(site, last_used_ts=time.time())
        return {"ok": True, "url": url, "status": status, "content": content,
                "final_url": r.get("final_url", url), "fetched_ts": now_ts(),
                "session_profile_used": profiles.slug(site)}
    finally:
        lock.release()


def browser_download(url: str, site: str, dest: Path, *,
                     min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                     _cef: Any | None = None) -> dict:
    """Download raw bytes from ``url`` under ``site``'s saved session, writing them to ``dest``.

    Uses the CEF daemon's direct ``CefURLRequest`` (which shares the profile's
    cookies) rather than a page navigation — the right tool for a CAD/PDF file,
    which a real page load would hand to the browser's download manager instead of
    returning as page content. A missing profile is auto-created (public
    datasheets/models need no login; gated files fail naturally and surface
    login_required upstream).
    """
    profiles.ensure_profile(site)
    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        return _browser_download_locked(url, site, dest, min_interval=min_interval, cef=_cef)
    finally:
        lock.release()


def _browser_download_locked(url: str, site: str, dest: Path, *,
                             min_interval: float, cef: Any | None = None) -> dict:
    ceiling = ratelimit.reserve_request(site)
    if ceiling is not None:
        return fail(ceiling, f"{site}: per-session request ceiling reached — this run has "
                             f"already made the maximum allowed fetches (a runaway-loop guard). "
                             f"Nothing was downloaded.")
    ratelimit.wait(site, min_interval=min_interval, url=url)
    status: int | None = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            from . import cef_client  # lazy import
            r = (cef or cef_client.client()).download(url, str(dest))
            if not r.get("ok"):
                return fail("browser_download_failed",
                            f"{site}: CEF download of {url} failed or returned no body")
        except Exception as exc:  # noqa: BLE001 — daemon spawn/connect/protocol
            return fail("browser_download_failed", str(exc))
    finally:
        ratelimit.note_result(site, status)

    nbytes = dest.stat().st_size if dest.exists() else 0
    profiles.save_meta(site, last_used_ts=time.time())
    return {"ok": True, "url": url, "status": status, "dest": str(dest), "bytes": nbytes,
            "fetched_ts": now_ts(), "session_profile_used": profiles.slug(site)}

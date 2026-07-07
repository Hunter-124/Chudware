"""services/browser/session.py — persistent per-site browser sessions (T-X1).

Two entry points only; every source adapter (T-X2+) calls these and never
imports Playwright directly:

- :func:`browser_login` — opens a HEADED, persistent-profile Chromium window so
  the user can sign into ``site`` in their own browser; the profile (cookies,
  local storage, ...) is saved to disk under the app data dir
  (:mod:`services.browser.profiles`) and is LOCAL ONLY — never synced, never
  uploaded, never read by this module into logs or metadata.
- :func:`browser_fetch` — opens the SAME persistent profile HEADLESS and
  navigates to a URL under the user's saved session, rate-limited per site
  (:mod:`services.browser.ratelimit`). Returns rendered HTML — not suitable
  for binary artifacts (a browser hands those to its download manager rather
  than rendering them).
- :func:`browser_download` (T-X2) — fetches a URL's raw bytes under the same
  saved session via Playwright's ``APIRequestContext`` (no page navigation),
  the right primitive for CAD/PDF downloads. Same rate-limiting and
  degradation posture as ``browser_fetch``.

Playwright (Apache-2.0) is an optional runtime dependency (see
``requirements.txt``). Both functions lazy-import it so importing this module
— or any MCP server that transitively imports it — never fails when
Playwright isn't installed; they degrade to the ``engine.research`` §6 failure
shape instead (``contracts/research_tools.md``).

Captcha/2FA (T-X6): this module DETECTS these states (see :mod:`.challenge`) but
never attempts to solve or bypass them. The rule is the same regardless — hand
control back to the user: ``browser_login`` (headed) surfaces a clear "complete it
yourself" message and keeps the window open; ``browser_fetch`` (headless) aborts
with a "needs manual re-login" result rather than returning the challenge page or
retrying.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from engine.research.common import fail, now_ts

from . import challenge, profiles, ratelimit, stealth

#: browser_login re-checks the page for a captcha/2FA prompt this often (seconds)
#: while waiting for the user to finish and close the window.
_LOGIN_POLL_INTERVAL_S = 2.0

#: How long browser_login waits (seconds) for the user's login window to close
#: on its own before this module closes it for them.
DEFAULT_LOGIN_TIMEOUT_S = 300.0
#: Navigation timeout (seconds) for a single browser_fetch page load.
DEFAULT_NAV_TIMEOUT_S = 30.0
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
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright not installed — run `pip install playwright` and "
            "`playwright install chromium` (see services/browser/README.md)"
        ) from exc
    return sync_playwright


def browser_login(site: str, *, timeout: float = DEFAULT_LOGIN_TIMEOUT_S,
                  _sync_playwright: Callable[[], Any] | None = None) -> dict:
    """Open a HEADED browser window for the user to sign into ``site``.

    Blocks up to ``timeout`` seconds while the window stays open; returns as
    soon as the user closes it (closing the window is how the user signals
    "I'm done logging in"). Only bookkeeping is returned — never credentials.
    """
    try:
        sync_playwright = _sync_playwright or _import_sync_playwright()
    except RuntimeError as exc:
        return fail("playwright_not_installed", str(exc))

    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        return _browser_login_locked(site, timeout=timeout, sync_playwright=sync_playwright)
    finally:
        lock.release()


def _browser_login_locked(site: str, *, timeout: float, sync_playwright: Callable[[], Any]) -> dict:
    profile_path = profiles.profile_dir(site)
    challenge_detected: str | None = None
    try:
        with sync_playwright() as pw:
            ver = stealth.chromium_version_sync(pw)
            ua  = stealth.user_agent(ver)
            context = pw.chromium.launch_persistent_context(
                str(profile_path), headless=False,
                args=stealth.launch_args(headless=False), **stealth.context_options(ua))
            stealth.harden_context_sync(context)
            _apply_staged_cookies(context, site)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                stealth.harden_page_sync(context, page, ua, ver)
                # Poll for the user closing the window. Between polls, if the page shows
                # a captcha/2FA we surface a clear "do it yourself" message ONCE per kind
                # and record it — we never click, solve, or bypass it. The user completes
                # it in this headed window and closes it when signed in.
                announced: set[str] = set()
                last_content: str | None = None
                last_url: str = ""
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
                    try:
                        page.wait_for_event("close", timeout=_LOGIN_POLL_INTERVAL_S * 1000)
                        break  # window closed → the user signalled "done"
                    except Exception:  # noqa: BLE001 — poll interval elapsed, still open
                        continue
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
    try:
        from .embed import classify_login_state  # lazy: avoid import cycle
        if last_content is not None and \
                classify_login_state(site, last_content, last_url) == "logged_in":
            fields["login_verified_ts"] = now
    except Exception:  # noqa: BLE001 — verification is best-effort, never fatal
        pass
    meta = profiles.save_meta(site, **fields)
    return {"ok": True, "site": meta["site"], "profile_dir": str(profile_path),
            "last_login_ts": meta["last_login_ts"],
            "login_verified": bool(meta.get("login_verified_ts")),
            "challenge_detected": challenge_detected}


def browser_fetch(url: str, site: str, *, nav_timeout: float = DEFAULT_NAV_TIMEOUT_S,
                  render_settle: float = DEFAULT_RENDER_SETTLE_S,
                  min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                  _sync_playwright: Callable[[], Any] | None = None) -> dict:
    """Fetch ``url`` HEADLESS under ``site``'s saved session profile.

    Returns the §6 failure shape (never raises) when Playwright isn't installed
    or navigation fails.  A missing profile is auto-created (anonymous browsing
    of public catalogs is allowed — live-probe 2026-07-07; login-gated content
    still degrades to login_required via the adapters).
    """
    profiles.ensure_profile(site)
    try:
        sync_playwright = _sync_playwright or _import_sync_playwright()
    except RuntimeError as exc:
        return fail("playwright_not_installed", str(exc))

    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        return _browser_fetch_locked(url, site, nav_timeout=nav_timeout,
                                     render_settle=render_settle, min_interval=min_interval,
                                     sync_playwright=sync_playwright)
    finally:
        lock.release()


def _browser_fetch_locked(url: str, site: str, *, nav_timeout: float, render_settle: float,
                          min_interval: float, sync_playwright: Callable[[], Any]) -> dict:
    ratelimit.wait(site, min_interval=min_interval)
    profile_path = profiles.profile_dir(site)
    try:
        try:
            with sync_playwright() as pw:
                ver = stealth.chromium_version_sync(pw)
                ua  = stealth.user_agent(ver)
                context = pw.chromium.launch_persistent_context(
                    str(profile_path), headless=True,
                    args=stealth.launch_args(headless=True), **stealth.context_options(ua))
                stealth.harden_context_sync(context)
                _apply_staged_cookies(context, site)
                try:
                    page = context.new_page()
                    stealth.harden_page_sync(context, page, ua, ver)
                    response = page.goto(url, timeout=nav_timeout * 1000)
                    status = response.status if response else None
                    # SPA catalogs render product cards via XHR AFTER 'load'
                    # fires, so page.content() right after goto() returns the
                    # empty shell (the "no hits parsed" bug across McMaster/
                    # Misumi/DigiKey/Amazon/…). Let the network settle first;
                    # swallow the timeout so a chatty page is still snapshotted
                    # at the deadline rather than failing the fetch.
                    try:
                        page.wait_for_load_state("networkidle",
                                                 timeout=render_settle * 1000)
                    except Exception:  # noqa: BLE001  (Playwright TimeoutError / mock page)
                        pass
                    content = page.content()
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            return fail("browser_fetch_failed", str(exc))
    finally:
        ratelimit.mark_hit(site)

    # A captcha/2FA interstitial is NOT real content. Headless has no human to hand
    # off to, so abort — never return the challenge page as a success, never retry or
    # attempt to solve it. The saved session has almost certainly expired; the caller
    # surfaces this and the user re-logins headed.
    challenge_kind = challenge.classify_challenge(content, url)
    if challenge_kind:
        return fail("captcha_or_2fa_detected",
                    f"{site} returned a {challenge_kind} challenge for {url}; the saved "
                    f"session likely expired — re-login with browser_login('{site}') "
                    f"and retry")

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


def browser_download(url: str, site: str, dest: Path, *, nav_timeout: float = DEFAULT_NAV_TIMEOUT_S,
                     min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                     _sync_playwright: Callable[[], Any] | None = None) -> dict:
    """Download raw bytes from ``url`` under ``site``'s saved session, writing them to ``dest``.

    Uses the persistent context's ``request`` (Playwright's ``APIRequestContext``,
    which shares the context's cookies) rather than page navigation — the
    right tool for a CAD/PDF file, which a real page load would hand to the
    browser's download manager instead of returning as page content.
    A missing profile is auto-created (public datasheets/models need no login;
    gated files fail naturally and surface login_required upstream).
    """
    profiles.ensure_profile(site)
    try:
        sync_playwright = _sync_playwright or _import_sync_playwright()
    except RuntimeError as exc:
        return fail("playwright_not_installed", str(exc))

    lock = profiles.profile_lock(site)
    if not lock.acquire(blocking=False):
        return _busy(site)
    try:
        return _browser_download_locked(url, site, dest, nav_timeout=nav_timeout,
                                        min_interval=min_interval, sync_playwright=sync_playwright)
    finally:
        lock.release()


def _browser_download_locked(url: str, site: str, dest: Path, *, nav_timeout: float,
                             min_interval: float, sync_playwright: Callable[[], Any]) -> dict:
    ratelimit.wait(site, min_interval=min_interval)
    profile_path = profiles.profile_dir(site)
    try:
        try:
            with sync_playwright() as pw:
                ver = stealth.chromium_version_sync(pw)
                ua  = stealth.user_agent(ver)
                context = pw.chromium.launch_persistent_context(
                    str(profile_path), headless=True,
                    args=stealth.launch_args(headless=True), **stealth.context_options(ua))
                stealth.harden_context_sync(context)
                _apply_staged_cookies(context, site)
                try:
                    response = context.request.get(url, timeout=nav_timeout * 1000)
                    status = response.status
                    body = response.body()
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            return fail("browser_download_failed", str(exc))
    finally:
        ratelimit.mark_hit(site)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    profiles.save_meta(site, last_used_ts=time.time())
    return {"ok": True, "url": url, "status": status, "dest": str(dest), "bytes": len(body),
            "fetched_ts": now_ts(), "session_profile_used": profiles.slug(site)}

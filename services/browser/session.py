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
  (:mod:`services.browser.ratelimit`).

Playwright (Apache-2.0) is an optional runtime dependency (see
``requirements.txt``). Both functions lazy-import it so importing this module
— or any MCP server that transitively imports it — never fails when
Playwright isn't installed; they degrade to the ``engine.research`` §6 failure
shape instead (``contracts/research_tools.md``).

Captcha/2FA: this module never attempts to detect, solve, or bypass either —
that is T-X6's job, and the rule is the same regardless: hand control back to
the user in a HEADED window, never automate around it.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from engine.research.common import fail, now_ts

from . import profiles, ratelimit

#: How long browser_login waits (seconds) for the user's login window to close
#: on its own before this module closes it for them.
DEFAULT_LOGIN_TIMEOUT_S = 300.0
#: Navigation timeout (seconds) for a single browser_fetch page load.
DEFAULT_NAV_TIMEOUT_S = 30.0


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

    profile_path = profiles.profile_dir(site)
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(profile_path), headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.wait_for_event("close", timeout=timeout * 1000)
                except Exception:  # noqa: BLE001 — timeout or already closed both mean "done"
                    pass
            finally:
                context.close()
    except Exception as exc:  # noqa: BLE001 — never crash the caller on a browser-launch error
        return fail("browser_login_failed", str(exc))

    profiles.touch_created(site)
    meta = profiles.save_meta(site, last_login_ts=time.time())
    return {"ok": True, "site": meta["site"], "profile_dir": str(profile_path),
            "last_login_ts": meta["last_login_ts"]}


def browser_fetch(url: str, site: str, *, nav_timeout: float = DEFAULT_NAV_TIMEOUT_S,
                  min_interval: float = ratelimit.DEFAULT_MIN_INTERVAL_S,
                  _sync_playwright: Callable[[], Any] | None = None) -> dict:
    """Fetch ``url`` HEADLESS under ``site``'s saved session profile.

    Returns the §6 failure shape (never raises) when no profile exists yet for
    ``site``, when Playwright isn't installed, or when navigation fails.
    """
    if not profiles.has_profile(site):
        return fail("no_profile", f"no saved browser session for site '{site}' — "
                                    f"call browser_login('{site}') first")
    try:
        sync_playwright = _sync_playwright or _import_sync_playwright()
    except RuntimeError as exc:
        return fail("playwright_not_installed", str(exc))

    ratelimit.wait(site, min_interval=min_interval)
    profile_path = profiles.profile_dir(site)
    try:
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(str(profile_path), headless=True)
                try:
                    page = context.new_page()
                    response = page.goto(url, timeout=nav_timeout * 1000)
                    content = page.content()
                    status = response.status if response else None
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            return fail("browser_fetch_failed", str(exc))
    finally:
        ratelimit.mark_hit(site)

    profiles.save_meta(site, last_used_ts=time.time())
    return {"ok": True, "url": url, "status": status, "content": content,
            "fetched_ts": now_ts(), "session_profile_used": profiles.slug(site)}

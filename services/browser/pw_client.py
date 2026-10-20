"""Playwright-backed headless fetch/download client for CEF-off platforms.

This is a deliberate compatibility fallback for macOS, not a replacement for the
CEF daemon. Playwright exposes CDP automation fingerprints that hostile vendor
sites can detect. Such responses are classified and returned as ``bot_walled``;
this client never retries, solves, or bypasses a challenge.

The public client shape mirrors :class:`services.browser.cef_client.CefDaemon`'s
``fetch`` and ``download`` methods so :mod:`services.browser.session` remains the
single backend-independent adapter seam.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import challenge, profiles, stealth

NAVIGATION_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 90_000
_DEFAULT_PROFILE = "playwright-headless"


class PlaywrightClient:
    """Short-lived Playwright contexts over a persistent per-vendor profile."""

    def __init__(self, profile: str = "", *, max_download_bytes: int = 200 * 1024 * 1024,
                 sync_playwright: Callable[[], Any] | None = None) -> None:
        self._profile = profile
        self._sync_playwright = sync_playwright
        self._max_download_bytes = max_download_bytes

    def _factory(self) -> Callable[[], Any]:
        if self._sync_playwright is not None:
            return self._sync_playwright
        # Lazy import avoids a driver <-> pw_client import cycle and preserves the
        # service's import-without-Playwright degradation posture.
        from . import driver
        return driver.resolve_sync()

    def _profile_dir(self, profile: str) -> Path:
        return profiles.ensure_profile(profile or self._profile or _DEFAULT_PROFILE)

    @staticmethod
    def _launch_context(pw: Any, profile_dir: Path) -> tuple[Any, str, str]:
        version = stealth.chromium_version_sync(pw)
        user_agent = stealth.user_agent(version)
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            args=stealth.launch_args(headless=True),
            **stealth.context_options(user_agent),
        )
        stealth.harden_context_sync(context)
        return context, user_agent, version

    def fetch(self, url: str, *, settle_ms: int = 1200, profile: str = "") -> dict:
        """Load ``url`` and return ``{ok, status, final_url, html}``.

        A detected challenge or HTTP 403/429 is an expected, terminal
        ``bot_walled`` result. Transport and browser-launch errors intentionally
        propagate for :func:`session.browser_fetch` to convert to its standard
        failure envelope.
        """
        from services.agent.request_context import in_cell
        if in_cell():
            return {"ok": False, "reason": "cell_refused"}
        with self._factory()() as pw:
            context, user_agent, version = self._launch_context(
                pw, self._profile_dir(profile))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                stealth.harden_page_sync(context, page, user_agent, version)
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                if settle_ms > 0:
                    page.wait_for_timeout(settle_ms)
                html = page.content()
                final_url = page.url
                status = response.status if response is not None else None
                challenge_kind = challenge.classify_challenge(html, final_url)
                if challenge_kind or status in (403, 429):
                    return {
                        "ok": False,
                        "reason": "bot_walled",
                        "status": status,
                        "final_url": final_url,
                    }
                return {
                    "ok": True,
                    "status": status,
                    "final_url": final_url,
                    "html": html,
                }
            finally:
                context.close()

    def download(self, url: str, dest: str, *, profile: str = "") -> dict:
        """Stream public bytes with profile cookies; never buffer a Playwright body."""
        from services.agent.request_context import in_cell
        if in_cell():
            return {"ok": False, "reason": "cell_refused"}
        from services.net.safe_http import download
        destination = Path(dest)
        with self._factory()() as pw:
            context, _user_agent, _version = self._launch_context(
                pw, self._profile_dir(profile))
            try:
                from .session import _apply_staged_cookies
                _apply_staged_cookies(context, profile or self._profile or _DEFAULT_PROFILE)
                def cookie_headers(current):
                    cookies = context.cookies([current])
                    return {"Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}

                response = download(url, destination, max_bytes=self._max_download_bytes,
                                    idle_timeout=30.0, headers_for_url=cookie_headers)
                status = response.status_code
                if status in (403, 429):
                    return {"ok": False, "reason": "bot_walled", "status": status}
                if status >= 400:
                    return {"ok": False, "reason": "http_error", "status": status}
                return {"ok": True, "status": status, "dest": str(destination),
                        "bytes": response.bytes_written}
            finally:
                context.close()


_CLIENT: Optional[PlaywrightClient] = None
_CLIENT_LOCK = threading.Lock()


def client(profile: str = "") -> PlaywrightClient:
    """Return a client, binding requests to ``profile`` when supplied."""
    global _CLIENT
    if profile:
        return PlaywrightClient(profile)
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = PlaywrightClient()
        return _CLIENT

"""Playwright-backed headless browser client for CEF-off platforms.

This is a deliberate compatibility fallback for macOS, not a replacement for the
CEF daemon. Playwright exposes CDP automation fingerprints that hostile vendor
sites can detect. Challenges are classified and returned to the caller; this
client never retries, solves, or bypasses a challenge.

The public client shape mirrors :class:`services.browser.cef_client.CefDaemon`'s
``fetch``, ``interact`` and ``download`` methods so
:mod:`services.browser.session` remains the backend-independent adapter seam.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import challenge, profiles, stealth

NAVIGATION_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 90_000
_DEFAULT_PROFILE = "playwright-headless"


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Parse either form of HTTP Retry-After without inventing a delay."""
    value = next((str(v).strip() for k, v in headers.items()
                  if k.lower() == "retry-after"), "")
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _failure(status: int | None, final_url: str, retry_after_s: float | None,
             challenge_kind: str | None = None) -> dict:
    if challenge_kind == "captcha":
        reason, hint = "captcha", "A CAPTCHA challenge requires user action."
    elif challenge_kind == "2fa":
        reason, hint = "blocked", "Two-factor authentication requires user action."
    elif status == 401:
        reason, hint = "login_required", "The server requires authentication."
    elif status == 403:
        reason, hint = "blocked", "The server refused access."
    elif status in (404, 410):
        reason, hint = "not_found", "The requested resource was not found."
    elif status == 429:
        reason, hint = "rate_limited", "The server rate-limited the request."
    else:
        reason, hint = "transport_failed", f"The server returned HTTP {status}."
    result = {"ok": False, "reason": reason, "hint": hint, "status": status,
              "final_url": final_url, "retry_after_s": retry_after_s}
    if challenge_kind:
        result["challenge_kind"] = challenge_kind
    return result


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

        A detected challenge or HTTP error is returned with its classification,
        status, final URL, and any Retry-After delay. Transport and browser-launch
        errors propagate for :func:`session.browser_fetch` to convert.
        """
        from services.agent.request_context import in_cell
        if in_cell():
            return {"ok": False, "reason": "cell_refused"}
        with self._factory()() as pw:
            context, user_agent, version = self._launch_context(
                pw, self._profile_dir(profile))
            try:
                from .session import _apply_staged_cookies
                _apply_staged_cookies(context, profile or self._profile or _DEFAULT_PROFILE)
                page = context.pages[0] if context.pages else context.new_page()
                stealth.harden_page_sync(context, page, user_agent, version)
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                if settle_ms > 0:
                    page.wait_for_timeout(settle_ms)
                html = page.content()
                final_url = page.url
                status = response.status if response is not None else None
                retry_after_s = _retry_after_seconds(response.headers if response else {})
                challenge_kind = challenge.classify_challenge(html, final_url)
                if challenge_kind or (status is not None and status >= 400):
                    return _failure(status, final_url, retry_after_s, challenge_kind)
                return {
                    "ok": True,
                    "status": status,
                    "final_url": final_url,
                    "html": html,
                    "retry_after_s": retry_after_s,
                }
            finally:
                context.close()

    def interact(self, url: str, script: str, *, sentinel: str = "__aicad_capture",
                 settle_ms: int = 4000, timeout_ms: int = 12000, profile: str = "",
                 timeout_s: float = 90.0) -> dict:
        """Run page JS and return the DOM after the sentinel appears or its wait expires."""
        from services.agent.request_context import in_cell
        if in_cell():
            return {"ok": False, "reason": "cell_refused"}

        deadline = time.monotonic() + timeout_s
        status: int | None = None
        final_url = url
        retry_after_s = None

        def remaining_ms() -> int:
            return max(0, int((deadline - time.monotonic()) * 1000))

        def timed_out() -> dict:
            return {"ok": False, "reason": "timeout",
                    "hint": "The browser interaction timed out.",
                    "status": status, "final_url": final_url,
                    "retry_after_s": retry_after_s}

        def is_timeout(exc: Exception) -> bool:
            return (isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
                    or "timed out" in str(exc).lower())

        if remaining_ms() <= 0:
            return timed_out()
        with self._factory()() as pw:
            context, user_agent, version = self._launch_context(
                pw, self._profile_dir(profile))
            try:
                from .session import _apply_staged_cookies
                _apply_staged_cookies(context, profile or self._profile or _DEFAULT_PROFILE)
                page = context.pages[0] if context.pages else context.new_page()
                stealth.harden_page_sync(context, page, user_agent, version)
                remaining = remaining_ms()
                if remaining <= 0:
                    return timed_out()
                try:
                    response = page.goto(
                        url, wait_until="domcontentloaded",
                        timeout=min(NAVIGATION_TIMEOUT_MS, remaining))
                except Exception as exc:
                    if is_timeout(exc):
                        return timed_out()
                    raise
                status = response.status if response is not None else None
                retry_after_s = _retry_after_seconds(response.headers if response else {})
                final_url = page.url
                if settle_ms > 0:
                    remaining = remaining_ms()
                    if remaining <= 0:
                        return timed_out()
                    page.wait_for_timeout(min(settle_ms, remaining))
                    if settle_ms > remaining or remaining_ms() <= 0:
                        return timed_out()
                html = page.content()
                final_url = page.url
                challenge_kind = challenge.classify_challenge(html, final_url)
                if challenge_kind or (status is not None and status >= 400):
                    return _failure(status, final_url, retry_after_s, challenge_kind)

                # Runtime.evaluate runs the caller's script in the page's global
                # context without awaiting a returned Promise. The script publishes
                # its result asynchronously in the sentinel, as it does under CEF.
                wait_budget = timeout_ms if timeout_ms > 0 else 12000
                remaining = remaining_ms()
                if remaining <= 0:
                    return timed_out()
                cdp = context.new_cdp_session(page)
                try:
                    try:
                        execution = cdp.send("Runtime.evaluate", {
                            "expression": script,
                            "awaitPromise": False,
                            "timeout": min(wait_budget, remaining),
                        })
                    except Exception as exc:
                        if is_timeout(exc) or "execution was terminated" in str(exc).lower():
                            return timed_out()
                        raise
                finally:
                    cdp.detach()
                script_error = None
                if execution.get("exceptionDetails"):
                    details = execution["exceptionDetails"]
                    error = details.get("exception", {}).get("description") or details.get("text")
                    if error and "execution was terminated" in error.lower():
                        return timed_out()
                    # A script can schedule asynchronous work before throwing.
                    # Like CEF, still wait for its sentinel and return the real DOM.
                    script_error = error or "The interaction script failed."
                remaining = remaining_ms()
                if remaining <= 0:
                    return timed_out()
                wait_ms = min(wait_budget, remaining)
                try:
                    page.wait_for_function(
                        "(id) => id !== '' && !!document.getElementById(id)",
                        arg=sentinel, timeout=wait_ms)
                except Exception as exc:
                    if not is_timeout(exc):
                        raise
                    if wait_ms < wait_budget or remaining_ms() <= 0:
                        return timed_out()
                    # CEF returns the current DOM after the sentinel wait expires.
                html = page.content()
                final_url = page.url
                challenge_kind = challenge.classify_challenge(html, final_url)
                if challenge_kind:
                    return _failure(status, final_url, retry_after_s, challenge_kind)
                result = {"ok": True, "status": status, "final_url": final_url,
                          "html": html, "retry_after_s": retry_after_s}
                if script_error:
                    result["script_error"] = script_error
                return result
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
                final_url = response.url
                retry_after_s = _retry_after_seconds(response.headers)
                if status >= 400:
                    challenge_kind = challenge.classify_challenge(response.text, final_url)
                    return _failure(status, final_url, retry_after_s, challenge_kind)
                return {"ok": True, "status": status, "final_url": final_url,
                        "retry_after_s": retry_after_s, "dest": str(destination),
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

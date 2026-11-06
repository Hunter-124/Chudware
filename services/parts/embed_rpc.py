"""Worker-RPC seam for the embedded-browser vendor sign-in flow.

The JPEG-screencast transport (``browser_embed_*``) was removed with the
migration to the in-process CEF browser (2026-07-07): the GUI now renders and
takes input in-process, so it no longer streams frames from a worker-side
Chromium. What remains on this seam:

- ``browser_start_url {site}`` → ``{ok, url, cache_dir, cookie_domains}`` — resolve a
  vendor key to its sign-in URL, local cookie cache dir, and trusted cookie domains.
- ``login_wizard_start {sites?}`` / ``login_wizard_status {}`` /
  ``login_wizard_next {skip?}`` / ``login_wizard_stop {}`` — the multi-vendor
  sign-in state machine (site list + login-verified tracking).

Everything lazy-imports the embed backend so a worker without Playwright still
registers these methods and answers with a structured failure.

Registered append-only from ``services/parts/worker_rpc.py`` (same
try/except pattern as ``GUI_WORKER_METHODS``).
"""
from __future__ import annotations

from typing import Callable


def _manager():
    from services.agent.request_context import in_cell
    if in_cell():
        raise ValueError("cell_refused")
    from services.browser.embed import get_manager
    return get_manager()


def _fail(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _wrap_exc(exc: Exception) -> dict:
    if str(exc) == "cell_refused":
        return _fail("cell_refused", "Browser session management is unavailable in tenant cells.")
    if isinstance(exc, ImportError):
        return _fail("playwright_not_installed",
                     "pip install playwright && playwright install chromium")
    return _fail(f"embed_rpc_failed: {exc}", "")


def _browser_start_url(params: dict, _emit: Callable) -> dict:
    """Resolve a vendor's sign-in URL, local profile directory, and cookie domains.
    Used by the in-process CEF backend to open the login and restore its cookies.
    Returns ``{ok:false}`` on failure.
    """
    from services.agent.request_context import in_cell
    if in_cell():
        return _fail("cell_refused")
    try:
        from services.browser.embed import start_url_for_site
        from services.browser import profiles
        site = str((params or {}).get("site", "")).strip()
        if not site:
            return _fail("no_site")
        url = start_url_for_site(site)
        if not url:
            return _fail("no_url_for_site", site)
        from urllib.parse import urlsplit
        from services.browser.adapters import get_adapter
        adapter = get_adapter(site)
        # Adapter hosts are the trusted vendor domains. Strip only the conventional
        # www prefix: guessing a registrable domain from the last two DNS labels
        # would accidentally include unrelated sites under e.g. co.uk.
        hosts = getattr(adapter, "hosts", ()) if adapter else ()
        domains = list(dict.fromkeys(
            host.lower().strip(".").removeprefix("www.")
            for host in hosts if isinstance(host, str) and host.strip(".")))
        login_host = (urlsplit(url).hostname or "").lower().strip(".")
        if login_host and not any(login_host == d or login_host.endswith("." + d)
                                  for d in domains):
            domains.append(login_host.removeprefix("www."))
        return {"ok": True, "url": url, "cache_dir": str(profiles.profile_dir(site)),
                "cookie_domains": domains}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_start(params: dict, _emit: Callable) -> dict:
    try:
        sites = (params or {}).get("sites")
        mode = str((params or {}).get("mode", "embed"))
        res = _manager().wizard_start(list(sites) if sites else None, mode=mode)
        return res
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_status(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().wizard_status()
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_next(params: dict, _emit: Callable) -> dict:
    try:
        p = params or {}
        logged_in = p.get("logged_in")
        return _manager().wizard_next(
            skip=bool(p.get("skip", False)),
            logged_in=None if logged_in is None else bool(logged_in))
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _push_cookies_to_cef_daemon(cookies: list) -> None:
    """Best-effort: re-inject the panel-exported cookie jar into the CEF daemon's
    warm profile so the login carries into headless fetch/download. Runs in a
    background thread — a cold daemon spawn (~CEF bootstrap) must never stall the
    login chip, and a daemon/transport failure degrades to "GUI-session-only"
    rather than failing the import (the on-disk warm jar is the durable carrier;
    a later fetch re-spawns the daemon and reads it)."""
    try:
        from services.browser.cef_client import client, CefDaemonError
        try:
            client().import_session(list(cookies))
        except CefDaemonError:
            pass  # daemon unavailable; login still stamped for this GUI session
    except Exception:  # noqa: BLE001 — never let the carry break the import
        pass


def _browser_session_import(params: dict, _emit: Callable) -> dict:
    """Stage a GUI-exported cookie jar for a site and stamp the login as verified,
    then carry it into the CEF daemon's warm profile so headless fetch/download
    reuse the session. ``{site, cookies:[...], verified?}``. The staging sidecar +
    metadata (login_verified_ts, which profiles.has_login gates on) needs no
    Playwright; the daemon push is best-effort and off-thread."""
    from services.agent.request_context import in_cell
    if in_cell():
        return {"ok": False, "reason": "credential_isolation_unsupported",
                "code": "credential_isolation_unsupported"}
    try:
        from services.worker_activity import start_background_thread

        from services.browser.embed import import_session_cookies
        p = params or {}
        site = str(p.get("site", "")).strip()
        cookies = p.get("cookies")
        cookies = list(cookies) if isinstance(cookies, list) else []
        result = import_session_cookies(
            site, cookies, verified=bool(p.get("verified", True)))
        if cookies:
            start_background_thread(_push_cookies_to_cef_daemon, args=(cookies,),
                                    name="browser-cookie-forward")
        return result
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_stop(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().wizard_stop()
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


EMBED_WORKER_METHODS: dict[str, Callable] = {
    "browser_start_url": _browser_start_url,
    "login_wizard_start": _login_wizard_start,
    "login_wizard_status": _login_wizard_status,
    "login_wizard_next": _login_wizard_next,
    "browser_session_import": _browser_session_import,
    "login_wizard_stop": _login_wizard_stop,
}

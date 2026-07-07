"""Worker-RPC seam for the embedded-browser vendor sign-in flow.

The JPEG-screencast transport (``browser_embed_*``) was removed with the
migration to the in-process CEF browser (2026-07-07): the GUI now renders and
takes input in-process, so it no longer streams frames from a worker-side
Chromium. What remains on this seam:

- ``browser_start_url {site}`` → ``{ok, url, cache_dir}`` — resolve a vendor key
  to its sign-in URL + local cookie cache dir (the CEF backend opens it directly).
- ``login_wizard_start {sites?}`` / ``login_wizard_status {}`` /
  ``login_wizard_next {skip?}`` / ``login_wizard_stop {}`` — the multi-vendor
  sign-in state machine (site list + login-verified tracking).

Everything lazy-imports the embed backend so a worker without Playwright still
registers these methods and answers with a structured failure.

Registered append-only from ``engine_oracle/parts_hub/worker_rpc.py`` (same
try/except pattern as ``GUI_WORKER_METHODS``).
"""
from __future__ import annotations

from typing import Callable


def _manager():
    from services.browser.embed import get_manager
    return get_manager()


def _fail(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _wrap_exc(exc: Exception) -> dict:
    if isinstance(exc, ImportError):
        return _fail("playwright_not_installed",
                     "pip install playwright && playwright install chromium")
    return _fail(f"embed_rpc_failed: {exc}", "")


def _browser_start_url(params: dict, _emit: Callable) -> dict:
    """Resolve a vendor key → its sign-in URL + the local per-site cookie cache
    directory. Used by the in-process CEF backend (which has no worker embed) to
    open a site's login page directly and keep its cookie jar in the SAME profile
    dir the Playwright scraper uses, so a future cookie-interop pass can share
    sessions. ``{ok, url, cache_dir}`` — degrades to ``{ok:false}`` structurally.
    """
    try:
        from services.browser.embed import start_url_for_site
        from services.browser import profiles
        site = str((params or {}).get("site", "")).strip()
        if not site:
            return _fail("no_site")
        url = start_url_for_site(site)
        if not url:
            return _fail("no_url_for_site", site)
        return {"ok": True, "url": url, "cache_dir": str(profiles.profile_dir(site))}
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


def _browser_session_import(params: dict, _emit: Callable) -> dict:
    """Stage a GUI-exported cookie jar for a site (CEF → Playwright hand-off)
    and stamp the login as verified. ``{site, cookies:[...], verified?}``.
    Needs no Playwright — it only writes the staging sidecar + metadata."""
    try:
        from services.browser.embed import import_session_cookies
        p = params or {}
        site = str(p.get("site", "")).strip()
        cookies = p.get("cookies")
        return import_session_cookies(
            site, list(cookies) if isinstance(cookies, list) else [],
            verified=bool(p.get("verified", True)))
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

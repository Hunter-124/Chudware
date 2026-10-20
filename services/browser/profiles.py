"""services/browser/profiles.py — local, per-site browser session profile storage.

A "profile" is a Playwright persistent-context user-data directory keyed by a
site id (e.g. ``"grabcad"``, ``"traceparts"``). LOCAL ONLY: profiles live under
the app data dir (:data:`services.paths.BROWSER_PROFILES`, mirroring the
``vendor_parts`` convention) and are never synced or uploaded anywhere by this
codebase.

Metadata sidecars record only non-secret bookkeeping (timestamps, status) —
credentials, cookies, and local storage live exclusively inside Playwright's
own profile directory, which this module never reads or logs.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from services import paths
from services.agent import request_context

# ---------------------------------------------------------------------------
# Per-site profile locks (Parts Library V2 / embedded browser).
#
# Chromium's ProcessSingleton means ONE user-data-dir cannot back two live
# browser contexts at once. Every code path that opens a persistent context
# for a site (sync session.py entry points AND the embedded-browser sessions
# in services.browser.embed) must hold that site's lock for the duration.
# Acquisition is NON-BLOCKING everywhere: contention degrades to a structured
# ``profile_busy`` result instead of deadlocking a worker thread.

_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def profile_lock(site: str | None) -> threading.Lock:
    """Process-wide lock guarding ``site``'s persistent profile directory.

    The same ``site`` (after :func:`slug` normalisation) always returns the
    same :class:`threading.Lock`. ``site=None`` returns a fresh per-call dummy
    lock (no shared profile → nothing to serialise).
    """
    if site is None:
        return threading.Lock()
    key = request_context.scoped_key(slug(site))
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = _PROFILE_LOCKS[key] = threading.Lock()
        return lock


def slug(site: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(site).strip()).strip("-").lower()
    if not s:
        raise ValueError("site must be a non-empty identifier")
    if s in (".", ".."):
        raise ValueError("site must not be a dot path")
    return s


def _profile_root() -> Path:
    return paths.actor_root() / "browser_profiles" if request_context.in_cell() else paths.BROWSER_PROFILES


def _profile_storage(site: str, *, create: bool = True) -> Path:
    site_slug = slug(site)
    if request_context.in_cell():
        p = paths.private_path("browser_profiles", site_slug, create=create)
        if p is None:
            raise KeyError("not_found")
        return p
    return _profile_root() / site_slug


def profile_dir(site: str) -> Path:
    """Persistent Playwright user-data-dir for ``site``.

    Does not create the directory itself — Playwright's
    ``launch_persistent_context`` creates it on first login. The parent
    (:data:`services.paths.BROWSER_PROFILES`) is created eagerly so metadata can
    be written even before a profile exists.
    """
    p = _profile_storage(site)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def has_profile(site: str) -> bool:
    return profile_dir(site).exists()


def ensure_profile(site: str) -> Path:
    """Create ``site``'s profile dir if missing (anonymous browsing is allowed —
    public catalogs don't need a login; gated content still degrades to
    login_required via the adapters). Returns the profile path."""
    p = profile_dir(site)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        touch_created(site)
    return p


def has_login(site: str) -> bool:
    """True only when a LIVE check confirmed an authenticated session for
    ``site`` — a ``login_verified_ts`` stamp set by the login wizard / headed
    ``browser_login`` ONLY after the post-login page actually classified as
    logged_in.

    Deliberately does NOT trust ``last_login_ts`` alone: that stamp merely
    records "a sign-in window was opened/closed" and was historically written
    even when the user never completed (or a false-positive fired), which made
    the wizard wrongly report "all vendor sites are already signed in". A mere
    profile dir never counted either (anonymous fetches auto-create one)."""
    return bool(load_meta(site).get("login_verified_ts"))


def _meta_path(site: str) -> Path:
    return Path(str(_profile_storage(site)) + ".json")


def load_meta(site: str) -> dict[str, Any]:
    p = _meta_path(site)
    if not p.exists():
        if request_context.in_cell():
            raise KeyError("not_found")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — metadata is bookkeeping, never fatal to read
        if request_context.in_cell():
            raise KeyError("not_found") from None
        return {}
    if request_context.in_cell() and (
        not isinstance(data, dict) or data.get("tenant_owner") != list(request_context.owner_key())
    ):
        raise KeyError("not_found")
    return data if isinstance(data, dict) else {}


def _write_meta(site: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Replace one metadata sidecar after normalising its site identifier."""
    data = dict(meta)
    data["site"] = slug(site)
    if request_context.in_cell():
        data["tenant_owner"] = list(request_context.owner_key())
    p = _meta_path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def save_meta(site: str, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into ``site``'s metadata sidecar and persist it.

    Never accepts (and callers must never pass) credential/cookie values —
    this sidecar is plain JSON bookkeeping only.
    """
    meta = load_meta(site) if _meta_path(site).exists() else {}
    meta.update(fields)
    return _write_meta(site, meta)


def list_profiles() -> list[dict[str, Any]]:
    """All known site profiles' metadata, sorted by site id."""
    root = _profile_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for meta_file in sorted(root.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and "site" in data:
            if request_context.in_cell() and data.get("tenant_owner") != list(request_context.owner_key()):
                continue
            out.append(data)
    return out


def check_profile_status(
    site: str,
    *,
    _fetch: Any | None = None,
    _probe_url: str | None = None,
    _classify: Any | None = None,
) -> str:
    """Freshly classify a saved login as ``active``, ``expired``, or ``unknown``.

    Only a profile with a prior live ``login_verified_ts`` is probed. The probe
    and metadata commit share the same profile lock as login, fetch, and delete,
    so a stale probe cannot recreate metadata after a successful deletion.
    Production probes call the already-locked browser primitive directly; tests
    can inject the same dict-returning ``_fetch`` seam.

    A definitive login page expires the profile and removes its verification
    stamp. Fetch errors, challenges, missing probe URLs, and inconclusive
    classifications are ``unknown`` and deliberately retain that stamp so a
    transient outage is never presented as an expired login.
    """
    site_slug = slug(site)
    meta = load_meta(site_slug)
    if not meta.get("login_verified_ts"):
        return "expired" if meta.get("status") == "expired" else "unknown"

    if _probe_url is None:
        try:
            from .embed import start_url_for_site  # lazy: profiles ↔ embed
            probe_url = start_url_for_site(site_slug)
        except Exception:  # noqa: BLE001 — optional adapter discovery
            probe_url = None
    else:
        probe_url = _probe_url

    lock = profile_lock(site_slug)
    if not lock.acquire(blocking=False):
        return "unknown"
    try:
        # Delete may have won the lock after the optimistic read above. Re-read
        # under lock and never recreate a sidecar that no longer exists.
        current = load_meta(site_slug)
        if not current.get("login_verified_ts"):
            return "expired" if current.get("status") == "expired" else "unknown"

        def persist(status: str, *, clear_verified: bool = False) -> str:
            if clear_verified:
                current.pop("login_verified_ts", None)
            current["status"] = status
            _write_meta(site_slug, current)
            return status

        if not probe_url:
            return persist("unknown")

        if _fetch is None:
            from . import ratelimit
            from .session import DEFAULT_RENDER_SETTLE_S, _browser_fetch_locked

            def fetch(url: str, locked_site: str, *, no_cache: bool) -> dict:
                return _browser_fetch_locked(
                    url,
                    locked_site,
                    render_settle=DEFAULT_RENDER_SETTLE_S,
                    min_interval=ratelimit.DEFAULT_MIN_INTERVAL_S,
                    no_cache=no_cache,
                )
        else:
            fetch = _fetch
        if _classify is None:
            from .embed import classify_login_state  # lazy: profiles ↔ embed
            classify = classify_login_state
        else:
            classify = _classify

        try:
            result = fetch(probe_url, site_slug, no_cache=True)
        except Exception:  # noqa: BLE001 — status checks never break profile listing
            return persist("unknown")
        if not isinstance(result, dict) or not result.get("ok"):
            return persist("unknown")

        final_url = result.get("final_url") or result.get("url") or probe_url
        try:
            state = classify(site_slug, result.get("content"), final_url)
        except Exception:  # noqa: BLE001 — adapter classifier is best-effort
            state = "unknown"
        if state == "logged_in":
            return persist("active")
        if state == "login_page":
            return persist("expired", clear_verified=True)
        return persist("unknown")
    finally:
        lock.release()


def delete_profile(site: str) -> bool:
    """Remove a site's profile, metadata, and transient cookie sidecar.

    The caller owns :func:`profile_lock` while deleting when profile contention
    is possible. Returns True when any of the three artifacts existed.
    """
    d = _profile_storage(site, create=False)
    # Check the owner stamp before removing any profile state.
    load_meta(site)
    removed = False
    if d.exists():
        shutil.rmtree(d)
        removed = True
    for sidecar in (
        _meta_path(site),
        _cookie_sidecar_path(site),
    ):
        if sidecar.exists():
            sidecar.unlink()
            removed = True
    return removed


# ---------------------------------------------------------------------------
# Session-cookie hand-off (GUI CEF browser → Playwright profile).
#
# The in-process CEF browser keeps its own Chromium cookie store (different
# cache layout AND a different OSCrypt key than Playwright's user-data-dir), so
# raw profile sharing can never work. Instead the GUI exports the cookie jar
# after a verified sign-in and the worker stages it here as a TRANSIENT sidecar
# (`<site>.cookies.json`, chmod 600). The next Playwright persistent context
# opened for the site consumes it via ``context.add_cookies`` — after which the
# cookies live natively in the Playwright profile and the sidecar is deleted.
# Unlike the metadata sidecar this file DOES hold secrets; it exists only
# between "GUI login finished" and "first headless use of the session".


def _cookie_sidecar_path(site: str) -> Path:
    return Path(str(_profile_storage(site)) + ".cookies.json")


def save_session_cookies(site: str, cookies: list[dict[str, Any]]) -> int:
    """Stage an exported cookie jar (Playwright ``add_cookies`` shape) for
    ``site``. Returns the number of cookies staged. Overwrites any previous
    staging — the newest login wins."""
    p = _cookie_sidecar_path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: Any = list(cookies)
    if request_context.in_cell():
        data = {"tenant_owner": list(request_context.owner_key()), "cookies": data}
    p.write_text(json.dumps(data), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return len(cookies)


def consume_session_cookies(site: str) -> list[dict[str, Any]]:
    """Read AND DELETE ``site``'s staged cookie jar (empty list when none).
    Deleting on read keeps the secret transient: once the caller has injected
    the cookies into a persistent Playwright context they live in the profile
    and the sidecar has no reason to exist."""
    p = _cookie_sidecar_path(site)
    if not p.exists():
        if request_context.in_cell():
            raise KeyError("not_found")
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt staging file is just discarded
        data = []
    if request_context.in_cell():
        if not isinstance(data, dict) or data.get("tenant_owner") != list(request_context.owner_key()):
            raise KeyError("not_found")
        data = data.get("cookies", [])
    try:
        p.unlink()
    except OSError:
        pass
    return data if isinstance(data, list) else []


def has_pending_session_cookies(site: str) -> bool:
    return _cookie_sidecar_path(site).exists()


def touch_created(site: str) -> None:
    """Stamp ``created_ts`` the first time a profile is seen; a no-op afterwards."""
    meta = load_meta(site) if _meta_path(site).exists() else {}
    if not meta.get("created_ts"):
        save_meta(site, created_ts=time.time())

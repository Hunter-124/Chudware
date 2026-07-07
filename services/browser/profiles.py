"""services/browser/profiles.py — local, per-site browser session profile storage.

A "profile" is a Playwright persistent-context user-data directory keyed by a
site id (e.g. ``"grabcad"``, ``"traceparts"``). LOCAL ONLY: profiles live under
the app data dir (:data:`engine.paths.BROWSER_PROFILES`, mirroring the
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

from engine import paths

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
    key = slug(site)
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = _PROFILE_LOCKS[key] = threading.Lock()
        return lock


def slug(site: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(site).strip()).strip("-").lower()
    if not s:
        raise ValueError("site must be a non-empty identifier")
    return s


def profile_dir(site: str) -> Path:
    """Persistent Playwright user-data-dir for ``site``.

    Does not create the directory itself — Playwright's
    ``launch_persistent_context`` creates it on first login. The parent
    (:data:`engine.paths.BROWSER_PROFILES`) is created eagerly so metadata can
    be written even before a profile exists.
    """
    paths.BROWSER_PROFILES.mkdir(parents=True, exist_ok=True)
    return paths.BROWSER_PROFILES / slug(site)


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
    return paths.BROWSER_PROFILES / f"{slug(site)}.json"


def load_meta(site: str) -> dict[str, Any]:
    p = _meta_path(site)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — metadata is bookkeeping, never fatal to read
        return {}
    return data if isinstance(data, dict) else {}


def save_meta(site: str, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into ``site``'s metadata sidecar and persist it.

    Never accepts (and callers must never pass) credential/cookie values —
    this sidecar is plain JSON bookkeeping only.
    """
    meta = load_meta(site)
    meta.update(fields)
    meta["site"] = slug(site)
    p = _meta_path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta


def list_profiles() -> list[dict[str, Any]]:
    """All known site profiles' metadata, sorted by site id."""
    if not paths.BROWSER_PROFILES.exists():
        return []
    out: list[dict[str, Any]] = []
    for meta_file in sorted(paths.BROWSER_PROFILES.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and "site" in data:
            out.append(data)
    return out


def delete_profile(site: str) -> bool:
    """Remove a site's profile directory and metadata sidecar. Returns True if anything existed."""
    removed = False
    d = profile_dir(site)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        removed = True
    m = _meta_path(site)
    if m.exists():
        m.unlink()
        removed = True
    return removed


def touch_created(site: str) -> None:
    """Stamp ``created_ts`` the first time a profile is seen; a no-op afterwards."""
    meta = load_meta(site)
    if not meta.get("created_ts"):
        save_meta(site, created_ts=time.time())

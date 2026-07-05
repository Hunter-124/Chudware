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
import time
from pathlib import Path
from typing import Any

from engine import paths


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

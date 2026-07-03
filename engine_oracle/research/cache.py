"""Per-user local knowledge cache — contract ``contracts/research_tools.md`` §4.

Raw fetched content keyed by ``sha256(url [+ variant])``, stored with the original
``fetched_ts`` and a tier so staleness can be judged with tiered TTLs. ``fetched_ts`` is
always the *network* fetch time (not the cache-read time) so it can be surfaced verbatim.

LEGAL (§4): this cache is **per-user local only**. Never sync it through a server or
ship it — that turns caching into redistribution. The root sits under the same appdata
root ``engine.paths`` establishes (``AI_CAD_KNOWLEDGE_DIR`` overrides, e.g. for tests).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .common import now_ts

# Tiered TTLs in seconds — contract §4 (pricing=hours, datasheet=days, standard≈immutable).
TTL_SECONDS: dict[str, int] = {
    "pricing": 6 * 3600,             # hours
    "datasheet": 7 * 24 * 3600,      # days
    "standard": 3650 * 24 * 3600,    # ~immutable (10 years)
}
DEFAULT_TIER = "datasheet"


def knowledge_root() -> Path:
    """Resolve the ``knowledge/`` cache root. ``AI_CAD_KNOWLEDGE_DIR`` wins (tests /
    explicit control); otherwise ``<appdata>/AI-CAD/knowledge`` via ``engine.paths`` —
    the same appdata convention the rest of the app uses."""
    override = os.environ.get("AI_CAD_KNOWLEDGE_DIR")
    if override:
        return Path(override)
    from engine import paths

    return paths.ROOT / "knowledge"  # alias-aware root, not the always-new dir


def _key(url: str, variant: str = "") -> str:
    return hashlib.sha256((url + "\x00" + variant).encode("utf-8")).hexdigest()


class Cache:
    """Content-addressed store under :func:`knowledge_root`. Sharded by key prefix so a
    single directory never holds tens of thousands of files."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root is not None else knowledge_root()

    def _path(self, url: str, variant: str = "") -> Path:
        key = _key(url, variant)
        return self.root / key[:2] / f"{key}.json"

    def get(
        self,
        url: str,
        *,
        variant: str = "",
        tier: str = DEFAULT_TIER,
        allow_stale: bool = False,
    ) -> Optional[dict]:
        """Return the stored record, or ``None`` on miss. A stale record (older than the
        tier TTL) is returned only when ``allow_stale`` is set (offline serves stale); the
        returned record carries ``_stale`` so callers can down-rank confidence."""
        p = self._path(url, variant)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt cache entry: treat as a miss
            return None
        age = time.time() - float(rec.get("_stored_at", 0.0))
        ttl = TTL_SECONDS.get(tier, TTL_SECONDS[DEFAULT_TIER])
        rec["_stale"] = age > ttl
        if rec["_stale"] and not allow_stale:
            return None
        return rec

    def put(
        self,
        url: str,
        content: str,
        *,
        variant: str = "",
        tier: str = DEFAULT_TIER,
        fetched_ts: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> dict:
        """Store ``content`` for ``url``. ``fetched_ts`` records the network fetch time;
        ``_stored_at`` (monotonic-ish wall clock) drives TTL. Written atomically."""
        p = self._path(url, variant)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {
            "url": url,
            "tier": tier,
            "content": content,
            "fetched_ts": fetched_ts or now_ts(),
            "_stored_at": time.time(),
        }
        if meta:
            rec.update(meta)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, p)
        return rec

"""PartsHub — the orchestrator for aicad.parts/1 (T-P1).

Ties together the ProviderRegistry, the FTS index, and the provider lookup.
Bootstrap auto-registers the two T-P1 adapters (bolts/bearings + core_families).
"""
from __future__ import annotations

import threading
from typing import Iterator

from engine_oracle.parts_hub.registry import ProviderRegistry
from engine_oracle.parts_hub.index import PartsIndex


class PartsHub:
    """Orchestrates provider registration, index build, search, and get."""

    def __init__(self) -> None:
        self._registry = ProviderRegistry()
        self._index = PartsIndex()
        self._lock = threading.Lock()
        self._index_built = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def register(self, provider: object) -> None:
        """Add a CatalogProvider. Invalidates the current index."""
        with self._lock:
            self._registry.register(provider)
            self._index_built = False

    def bootstrap(self) -> None:
        """Auto-register the T-P1 built-in adapters."""
        from engine_oracle.parts_hub.adapters.bolts_adapter import BoltsAdapter
        from engine_oracle.parts_hub.adapters.core_families_adapter import CoreFamiliesAdapter
        self.register(BoltsAdapter())
        self.register(CoreFamiliesAdapter())
        # Build the index immediately so first search is fast.
        self._ensure_index()

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_index(self) -> None:
        """Build the index if it hasn't been built yet (idempotent)."""
        with self._lock:
            if self._index_built:
                return
            self._build_index_locked()

    def _build_index_locked(self) -> int:
        """Must be called with self._lock held. Returns record count."""
        count = self._index.rebuild(self._iter_all_records())
        self._index_built = True
        return count

    def rebuild_index(self) -> int:
        """Force a full index rebuild. Returns record count."""
        with self._lock:
            self._index_built = False
            return self._build_index_locked()

    def _iter_all_records(self) -> Iterator[dict]:
        """Yield all PartRecord dicts from indexable providers."""
        for provider in self._registry.iter_indexable():
            try:
                yield from provider.iter_records()
            except Exception:  # noqa: BLE001 — one bad provider must not break others
                pass

    # ------------------------------------------------------------------
    # Search + get
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None,
               limit: int = 20) -> list[dict]:
        """Full-text search. Returns ranked PartRecord dicts."""
        self._ensure_index()
        return self._index.search(query, domain=domain, limit=limit)

    def get(self, part_id: str) -> dict | None:
        """Retrieve a PartRecord by part_id. Returns None if not found."""
        self._ensure_index()
        # Try the index first.
        rec = self._index.get(part_id)
        if rec is not None:
            return rec
        # Fall back to individual providers (handles non-indexable live providers in T-P2).
        for provider in self._registry.providers():
            if callable(getattr(provider, "get", None)):
                try:
                    rec = provider.get(part_id)
                    if rec is not None:
                        return rec
                except Exception:  # noqa: BLE001
                    pass
        return None

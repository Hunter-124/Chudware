"""PartsHub — the orchestrator for aicad.parts/1 (T-P1 + T-P2).

Ties together the ProviderRegistry, the FTS index, and the provider lookup.
Bootstrap auto-registers all adapters:
  T-P1: bolts/bearings + core_families
  T-P2: structural_sections, piping_fittings, hydraulic_components,
         ecad_kicad (indexable), ecad_vendor (live, non-indexed)

Live-vendor fallback (T-P2): when the local FTS index returns no results for a
query, any registered live-only provider (iter_records raises NotImplementedError)
is queried directly. Results from live providers carry source:"live" and are
NEVER indexed.
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
        """Auto-register all built-in adapters (T-P1 + T-P2)."""
        # T-P1 adapters
        from engine_oracle.parts_hub.adapters.bolts_adapter import BoltsAdapter
        from engine_oracle.parts_hub.adapters.core_families_adapter import CoreFamiliesAdapter
        # T-P2 domain adapters (indexable)
        from engine_oracle.parts_hub.adapters.structural_sections_adapter import StructuralSectionsAdapter
        from engine_oracle.parts_hub.adapters.piping_fittings_adapter import PipingFittingsAdapter
        from engine_oracle.parts_hub.adapters.hydraulic_components_adapter import HydraulicComponentsAdapter
        from engine_oracle.parts_hub.adapters.ecad_kicad_adapter import EcadKicadAdapter
        # T-P2 live-vendor adapter (non-indexed, search-only)
        from engine_oracle.parts_hub.adapters.ecad_vendor_adapter import EcadVendorAdapter

        self.register(BoltsAdapter())
        self.register(CoreFamiliesAdapter())
        self.register(StructuralSectionsAdapter())
        self.register(PipingFittingsAdapter())
        self.register(HydraulicComponentsAdapter())
        self.register(EcadKicadAdapter())
        self.register(EcadVendorAdapter())
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
        """Full-text search. Returns ranked PartRecord dicts.

        Primary: FTS5 index over all indexable providers.
        Fallback: when the index returns no results AND domain matches a live
        provider (e.g. ecad_vendor for domain=ecad or unfiltered), query each
        live-only provider directly. Live results carry source:"live" and are
        returned after index results (or alone when index is empty).
        """
        self._ensure_index()
        results = self._index.search(query, domain=domain, limit=limit)
        if results:
            return results
        # Fallback to live providers when the index has no hits.
        live_results: list[dict] = []
        for provider in self._registry.providers():
            # Live-only: iter_records raises NotImplementedError.
            if not _is_live_only(provider):
                continue
            pdomain = getattr(provider, "domain", None)
            if domain and pdomain and pdomain != domain:
                continue
            if callable(getattr(provider, "search", None)):
                try:
                    hits = provider.search(query, domain=domain, filters=filters)
                    live_results.extend(hits)
                except Exception:  # noqa: BLE001
                    pass
        return live_results[:limit]

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


def _is_live_only(provider: object) -> bool:
    """Return True when iter_records is known to raise NotImplementedError.

    Probes by calling count(); if that itself raises NotImplementedError we
    treat it as live-only. Falls back to inspecting iter_records directly.
    """
    count_fn = getattr(provider, "count", None)
    if callable(count_fn):
        try:
            count_fn()
            return False  # succeeded — provider is enumerable
        except NotImplementedError:
            return True
        except Exception:  # noqa: BLE001
            pass
    iter_fn = getattr(provider, "iter_records", None)
    if callable(iter_fn):
        try:
            next(iter(iter_fn()))
            return False
        except NotImplementedError:
            return True
        except StopIteration:
            return False
        except Exception:  # noqa: BLE001
            pass
    return False

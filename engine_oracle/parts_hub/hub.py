"""PartsHub — the orchestrator for aicad.parts/1 (T-P1 + T-P2 + T-P3).

Ties together the ProviderRegistry, the FTS index, and the provider lookup.
Bootstrap auto-registers all adapters:
  T-P1: bolts/bearings + core_families
  T-P2: structural_sections, piping_fittings, hydraulic_components,
         ecad_kicad (indexable), ecad_vendor (live, non-indexed)

Live-vendor fallback (T-P2): when the local FTS index returns no results for a
query, any registered live-only provider (iter_records raises NotImplementedError)
is queried directly. Results from live providers carry source:"live" and are
NEVER indexed.

Provider-overlap deduplication (T-P3, contract-additive):
  For standard threaded hardware (fasteners / bearings / nuts / washers), the
  BoltsAdapter (domain="fastener") and CoreFamiliesAdapter (domain="mech_family")
  both emit records for the same designation (e.g. "M8").  After ranking, the hub:
  1. Groups records with the same normalised designation across these two domains.
  2. Keeps both, but promotes fastener > mech_family (canonical row first).
  3. Injects a ``related_ids`` additive field (list[str]) on each record pointing
     to its sibling(s).  Contract-additive per contracts/parts.md §0.
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
            return _dedupe_related(results)
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


# ── Provider-overlap deduplication (T-P3 — contract-additive) ────────────────

# Domains that may emit the same physical hardware designation.
_OVERLAP_DOMAINS = frozenset({"fastener", "bearing", "mech_family"})

# Priority: lower number = preferred canonical row (shown first when designation collides).
_DOMAIN_PRIORITY: dict[str, int] = {
    "fastener":    0,   # BOLTS catalog — authoritative for standards-designated hardware
    "bearing":     0,   # BOLTS bearing rows share the same priority tier as fastener
    "mech_family": 1,   # Core parametric families — instantiation recipe, not catalog record
}


def _normalise_desig(desig: str | None) -> str:
    """Canonical form for same-designation grouping (strip spaces, upper-case)."""
    if not desig:
        return ""
    return desig.replace(" ", "").upper()


def _dedupe_related(results: list[dict]) -> list[dict]:
    """Inject ``related_ids`` on overlapping-designation records and re-rank within groups.

    Only touches records whose domain is in ``_OVERLAP_DOMAINS``.  Non-hardware
    domains (ecad, section, fitting, hydraulic) pass through unchanged.

    Contract-additive: ``related_ids`` is a NEW optional list[str] field on PartRecord —
    contracts/parts.md §0 permits new optional fields without a version bump.
    """
    # Build a map: normalised_designation → list of (index, record) for overlap-domain records.
    from collections import defaultdict
    desig_groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for idx, rec in enumerate(results):
        if rec.get("domain") in _OVERLAP_DOMAINS:
            key = _normalise_desig(rec.get("designation"))
            if key:
                desig_groups[key].append((idx, rec))

    # For each group with >1 record, wire related_ids (additive) and sort by priority.
    # We build a reordered result list preserving non-overlap records in their original slots.
    if not any(len(v) > 1 for v in desig_groups.values()):
        return results  # Fast path: nothing to do.

    # Collect indices that belong to a multi-record overlap group.
    overlap_indices: set[int] = set()
    group_clusters: list[list[tuple[int, dict]]] = []
    for grp in desig_groups.values():
        if len(grp) > 1:
            overlap_indices.update(i for i, _ in grp)
            group_clusters.append(grp)

    # Annotate related_ids on all records in each cluster.
    for grp in group_clusters:
        all_ids = [rec["part_id"] for _, rec in grp]
        for _, rec in grp:
            siblings = [pid for pid in all_ids if pid != rec["part_id"]]
            # Shallow-copy the record so we don't mutate the index cache.
            rec_copy = dict(rec)
            rec_copy["related_ids"] = siblings
            # Replace in results list in-place (we'll rebuild below).
            # Find the record's original position and update it.
            for j, orig in enumerate(results):
                if orig is rec:
                    results[j] = rec_copy
                    break

    # Re-sort overlap groups by domain priority (stable — preserves FTS order within tier).
    for grp in group_clusters:
        grp.sort(key=lambda t: _DOMAIN_PRIORITY.get(t[1].get("domain", ""), 99))

    # Build a set of all indices that belong to any cluster, and a map from the minimum
    # index of each cluster to its sorted record list.
    cluster_by_first: dict[int, list[dict]] = {}
    for grp in group_clusters:
        first_idx = min(i for i, _ in grp)
        cluster_by_first[first_idx] = [results[i] for i, _ in grp]

    out: list[dict] = []
    emitted_indices: set[int] = set()
    for idx in range(len(results)):
        if idx in overlap_indices:
            if idx in cluster_by_first and idx not in emitted_indices:
                # Emit the full sorted cluster at the first-member's position.
                out.extend(cluster_by_first[idx])
                # Mark all members of this cluster as emitted.
                for grp in group_clusters:
                    if min(i for i, _ in grp) == idx:
                        emitted_indices.update(i for i, _ in grp)
                        break
            # Non-first cluster members: skip (already emitted above).
        else:
            out.append(results[idx])

    return out


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

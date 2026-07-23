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

import os
import queue
import threading
import time
from typing import Iterator

from .registry import ProviderRegistry
from .index import PartsIndex

# Overall wall-clock budget for the synchronous live-vendor fan-out (seconds).
# Override with CHUDWARE_PARTS_LIVE_BUDGET_S. Per-site budget defaults to the same
# value (CHUDWARE_PARTS_LIVE_SITE_BUDGET_S) so one stall cannot dominate the call.
_DEFAULT_LIVE_BUDGET_S = 4.0


class PartsHub:
    """Orchestrates provider registration, index build, search, and get."""

    def __init__(self) -> None:
        self._registry = ProviderRegistry()
        self._index = PartsIndex()
        self._lock = threading.Lock()
        self._index_built = False
        # Optional peer package diagnostic (browser adapters). Local FTS still
        # works when this is set; never silent-swallow on the public status API.
        self.browser_bootstrap_error: str | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def register(self, provider: object) -> None:
        """Add a CatalogProvider. Invalidates the current index."""
        with self._lock:
            self._registry.register(provider)
            self._index_built = False

    def bootstrap(self) -> None:
        """Auto-register all built-in adapters (T-P1 + T-P2 + local library)."""
        # T-P1 adapters
        from .adapters.bolts_adapter import BoltsAdapter
        from .adapters.core_families_adapter import CoreFamiliesAdapter
        # T-P2 domain adapters (indexable)
        from .adapters.structural_sections_adapter import StructuralSectionsAdapter
        from .adapters.piping_fittings_adapter import PipingFittingsAdapter
        from .adapters.hydraulic_components_adapter import HydraulicComponentsAdapter
        from .adapters.ecad_kicad_adapter import EcadKicadAdapter
        # T-FIN17 curated heatsink catalog (thermal metadata for heatsink_select)
        from .adapters.heatsinks_adapter import HeatsinksAdapter
        # Watched local library (vendor_parts index) — indexable STEP/STEP+ files
        from .adapters.local_library_adapter import LocalLibraryAdapter
        # T-P2 live-vendor adapter (non-indexed, search-only)
        from .adapters.ecad_vendor_adapter import EcadVendorAdapter

        self.register(BoltsAdapter())
        self.register(CoreFamiliesAdapter())
        self.register(StructuralSectionsAdapter())
        self.register(PipingFittingsAdapter())
        self.register(HydraulicComponentsAdapter())
        self.register(EcadKicadAdapter())
        self.register(HeatsinksAdapter())
        self.register(LocalLibraryAdapter())
        self.register(EcadVendorAdapter())
        self._register_browser_catalog_adapters()
        # Build the index immediately so first search is fast.
        self._ensure_index()

    def _register_browser_catalog_adapters(self) -> None:
        """T-X3: register each services/browser SourceAdapter as a live CatalogProvider
        (domain="catalog") — the contracts/parts.md §6.3 seam reserved for "paid catalog
        providers" (TraceParts/Werk24), populated here by the browser-session mechanism
        instead. Best-effort: services/browser is an optional peer package (Playwright is
        a lazy runtime dependency of its own session module) — any import/registration
        failure here must never break hub bootstrap. Failure is retained as
        ``browser_bootstrap_error`` so callers can surface a diagnostic while local
        search keeps working.
        """
        try:
            from services.browser.adapters import all_adapters
            from services.browser.adapters.parts_provider import BrowserCatalogAdapter

            for adapter in all_adapters():
                self.register(BrowserCatalogAdapter(adapter))
            self.browser_bootstrap_error = None
        except Exception as exc:  # noqa: BLE001 — optional integration, never fatal at bootstrap
            self.browser_bootstrap_error = f"{type(exc).__name__}: {exc}"

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
               domain: str | list[str] | None = None,
               filters: dict | None = None,
               limit: int = 20,
               sites: list[str] | None = None,
               include_live: bool = True) -> list[dict]:
        """Full-text search. Returns ranked PartRecord dicts.

        ``domain`` accepts a single domain string or a list of domains
        (taxonomy routing, Parts Library V2) — semantics otherwise unchanged.

        ``sites`` is an optional vendor allow-list for the LIVE fan-out: when
        given, only live providers whose ``site`` is in the list are queried.
        This is how taxonomy routing keeps a query on the vendors that fit its
        category (contracts/parts.md §6.3) — an electronics search never touches
        McMaster or a metals house, a metals search never touches DigiKey — so a
        query only ever spends a fetch against a vendor that could plausibly
        stock it. ``None`` preserves the prior behavior (all matching providers).

        Primary: FTS5 index over all indexable providers.
        Fallback: when the index returns no results AND domain matches a live
        provider (e.g. ecad_vendor for domain=ecad or unfiltered), query each
        live-only provider concurrently under a wall-clock budget
        (``CHUDWARE_PARTS_LIVE_BUDGET_S``, default a few seconds). Live results
        carry source:"live" and are NEVER indexed. Timed-out sites surface as
        notice stubs (no part_id/title) so callers can render honest per-site
        timeout notices. ``include_live=False`` does ZERO network.
        """
        self._ensure_index()
        results = self._index.search(query, domain=domain, limit=limit)
        if results:
            return _dedupe_related(results)
        # Local-first phase: the caller wants only the instant FTS layer and will
        # fan out to live vendors separately (Parts Library streaming search). Skip
        # the synchronous browser fan-out so the local call returns immediately.
        if not include_live:
            return []
        return self._search_live(query, domain=domain, filters=filters,
                                 limit=limit, sites=sites)

    def _search_live(self, query: str, *,
                     domain: str | list[str] | None,
                     filters: dict | None,
                     limit: int,
                     sites: list[str] | None) -> list[dict]:
        """Concurrent, deadline-bounded live-vendor fan-out.

        Returns partial hits plus timeout/login notice stubs. Never blocks past
        the overall wall budget even if a provider stalls. Workers are daemon
        threads so a stall cannot keep the process (or pytest) alive after
        return.
        """
        domains = [domain] if isinstance(domain, str) else list(domain or [])
        site_allow = set(sites) if sites else None
        providers: list[object] = []
        for provider in self._registry.providers():
            if not _is_live_only(provider):
                continue
            pdomain = getattr(provider, "domain", None)
            if domains and pdomain and pdomain not in domains:
                continue
            if site_allow is not None:
                psite = getattr(provider, "site", None)
                if psite is not None and psite not in site_allow:
                    continue
            if callable(getattr(provider, "search", None)):
                providers.append(provider)
        if not providers:
            return []

        budget = _live_budget_s()
        if budget <= 0:
            return []

        out_q: queue.Queue = queue.Queue()

        def _worker(provider: object) -> None:
            site = str(getattr(provider, "site", None)
                       or getattr(provider, "provider_id", "unknown")
                       or "unknown")
            try:
                hits = provider.search(  # type: ignore[attr-defined]
                    query, domain=domain, filters=filters)
                if not isinstance(hits, list):
                    out_q.put((site, [], f"search returned {type(hits).__name__}"))
                else:
                    out_q.put((site, hits, None))
            except Exception as exc:  # noqa: BLE001
                out_q.put((site, [], str(exc) or type(exc).__name__))

        threads: list[threading.Thread] = []
        for p in providers:
            site = str(getattr(p, "site", None)
                       or getattr(p, "provider_id", "x") or "x")
            t = threading.Thread(
                target=_worker, args=(p,),
                name=f"parts-live-{site}",
                daemon=True,
            )
            threads.append(t)
            t.start()

        deadline = time.monotonic() + budget
        live_results: list[dict] = []
        finished_sites: set[str] = set()
        n = len(threads)
        finished = 0
        while finished < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                site, hits, err = out_q.get(timeout=remaining)
            except queue.Empty:
                break
            finished += 1
            finished_sites.add(site)
            if err:
                reason = err if err in (
                    "login_required", "no_profile", "unavailable",
                    "timeout", "challenge_page",
                ) else "error"
                live_results.append({
                    "site": site,
                    "reason": reason,
                    "hint": err if reason == "error" else "",
                })
            else:
                live_results.extend(hits)
            if len([r for r in live_results
                    if r.get("part_id") and r.get("title")]) >= limit:
                break

        # Non-blocking drain: anything that finished in the last moment of the
        # budget still contributes (avoids false timeouts on race-at-deadline).
        while True:
            try:
                site, hits, err = out_q.get_nowait()
            except queue.Empty:
                break
            finished_sites.add(site)
            if err:
                reason = err if err in (
                    "login_required", "no_profile", "unavailable",
                    "timeout", "challenge_page",
                ) else "error"
                live_results.append({
                    "site": site,
                    "reason": reason,
                    "hint": err if reason == "error" else "",
                })
            else:
                live_results.extend(hits)

        # Still-running (or never-drained) providers → honest timeout notices.
        for p in providers:
            site = str(getattr(p, "site", None)
                       or getattr(p, "provider_id", "unknown")
                       or "unknown")
            if site in finished_sites:
                continue
            live_results.append({
                "site": site,
                "reason": "timeout",
                "hint": (f"live vendor '{site}' exceeded "
                         f"{budget:g}s budget (CHUDWARE_PARTS_LIVE_BUDGET_S)"),
            })
        real = [r for r in live_results
                if r.get("part_id") and str(r.get("title") or "").strip()]
        notices = [r for r in live_results if r not in real]
        return real[:limit] + notices

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


# ── Live fan-out budgets ─────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0.05, float(raw))
    except (TypeError, ValueError):
        return default


def _live_budget_s() -> float:
    """Overall wall budget for one live fan-out call."""
    return _env_float("CHUDWARE_PARTS_LIVE_BUDGET_S", _DEFAULT_LIVE_BUDGET_S)


def _live_site_budget_s(overall: float) -> float:
    """Per-site wait slice (capped by the overall budget)."""
    return min(overall, _env_float("CHUDWARE_PARTS_LIVE_SITE_BUDGET_S", overall))

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

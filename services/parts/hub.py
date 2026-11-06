"""PartsHub — the orchestrator for aicad.parts/1.

Ties together the ProviderRegistry, the FTS index, and the provider lookup.
Bootstrap auto-registers all adapters:
  core:   bolts/bearings + core_families
  domain: structural_sections, piping_fittings, hydraulic_components,
          ecad_kicad (indexable)

Live-vendor fallback: weak local coverage, or an explicit request, queries
registered live-only providers. The synchronous fan-out defaults to a 15-second
wall budget (CHUDWARE_PARTS_LIVE_BUDGET_S) and returns partial hits with timeout
notices. Live results carry source:"live" and are never indexed.

Provider-overlap annotation links records only when domain, standard and
designation identify the same part. A shared nominal size alone is not proof
of physical equivalence.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Iterator
from contextvars import ContextVar, copy_context
from services.agent.request_context import owner_key

from .registry import ProviderRegistry
from .index import PartsIndex
from .search_relevance import live_relevant, score_record

log = logging.getLogger(__name__)

_LIVE_THREADS: dict[threading.Thread, tuple[str, str] | None] = {}
_LIVE_LOCK = threading.Lock()

# Share the GUI's browser-fetch cap with synchronous agent searches. Public
# Printables API calls do not occupy a browser slot.
_LIVE_FETCH_CONCURRENCY = 3
_live_fetch_slots = threading.BoundedSemaphore(_LIVE_FETCH_CONCURRENCY)
_live_slot_held: ContextVar[bool] = ContextVar("parts_live_slot_held", default=False)


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    owner = owner_key()
    with registry_lock(_LIVE_LOCK):
        for thread in list(_LIVE_THREADS):
            if thread.ident is not None and not thread.is_alive():
                _LIVE_THREADS.pop(thread)
        count = sum(stored_owner == owner for stored_owner in _LIVE_THREADS.values())
    return {"by_kind": {"parts_hub_search": count}}

# Overall wall-clock budget for the synchronous live-vendor fan-out (seconds).
# Override with CHUDWARE_PARTS_LIVE_BUDGET_S. Per-site budget defaults to the same
# value (CHUDWARE_PARTS_LIVE_SITE_BUDGET_S) so one stall cannot dominate the call.
_DEFAULT_LIVE_BUDGET_S = 15.0

# ── Persistent index cache ───────────────────────────────────────────────────

# Bumped BY HAND whenever adapter code changes the shape/content of emitted
# PartRecords. It is mixed into the fingerprint, so bumping it invalidates every
# user's on-disk snapshot without touching their data files.
_INDEX_CODE_VERSION = 8

# Default seconds between cheap stat-only revalidations during search().
_DEFAULT_REVALIDATE_S = 20.0

# Hard cap on dirents visited while fingerprinting the watched library folder.
# Beyond this the signature is marked "partial" (still stable, just coarser).
_FP_WALK_LIMIT = 20000


class PartsHub:
    """Orchestrates provider registration, index build, search, and get."""

    def __init__(self) -> None:
        self._registry = ProviderRegistry()
        self._index = PartsIndex(_index_cache_path())
        self._lock = threading.Lock()
        self._index_built = False
        # Index provenance/telemetry (feeds index_status() → parts_index_status RPC).
        self._index_source = "none"      # "none" | "cache" | "fresh"
        self._index_building = False
        self._index_fingerprint = ""
        self._last_revalidate = 0.0      # time.monotonic() of the last stat sweep
        # Optional peer package diagnostic (browser adapters). Local FTS still
        # works when this is set; never silent-swallow on the public status API.
        self.browser_bootstrap_error: str | None = None
        self._diagnostics: list[dict] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def register(self, provider: object) -> None:
        """Add a CatalogProvider. Invalidates the current index."""
        with self._lock:
            self._registry.register(provider)
            self._index_built = False

    def bootstrap(self) -> None:
        """Auto-register all built-in adapters (core + domain + local library)."""
        # Core adapters
        from .adapters.bolts_adapter import BoltsAdapter
        from .adapters.core_families_adapter import CoreFamiliesAdapter
        # Domain adapters (indexable)
        from .adapters.structural_sections_adapter import StructuralSectionsAdapter
        from .adapters.piping_fittings_adapter import PipingFittingsAdapter
        from .adapters.hydraulic_components_adapter import HydraulicComponentsAdapter
        from .adapters.ecad_kicad_adapter import EcadKicadAdapter
        # Curated heatsink catalog (thermal metadata for heatsink_select)
        from .adapters.heatsinks_adapter import HeatsinksAdapter
        # T-PL-motion coverage adapters (stepper motors, belts/pulleys, shaft couplings)
        from .adapters.stepper_motors_adapter import StepperMotorsAdapter
        from .adapters.belts_pulleys_adapter import BeltsPulleysAdapter
        from .adapters.shaft_couplings_adapter import ShaftCouplingsAdapter
        # T-PL-stock coverage adapters (o-rings, raw stock, aluminum extrusion)
        from .adapters.orings_adapter import OringsAdapter
        from .adapters.raw_stock_adapter import RawStockAdapter
        from .adapters.extrusion_adapter import ExtrusionAdapter
        # Watched local library (vendor_parts index) — indexable STEP/STEP+ files
        from .adapters.local_library_adapter import LocalLibraryAdapter

        self.register(BoltsAdapter())
        self.register(CoreFamiliesAdapter())
        self.register(StructuralSectionsAdapter())
        self.register(PipingFittingsAdapter())
        self.register(HydraulicComponentsAdapter())
        self.register(EcadKicadAdapter())
        self.register(HeatsinksAdapter())
        self.register(StepperMotorsAdapter())
        self.register(BeltsPulleysAdapter())
        self.register(ShaftCouplingsAdapter())
        self.register(OringsAdapter())
        self.register(RawStockAdapter())
        self.register(ExtrusionAdapter())
        self.register(LocalLibraryAdapter())
        self._register_browser_catalog_adapters()
        # Build the index immediately so first search is fast.
        self._ensure_index()

    def _register_browser_catalog_adapters(self) -> None:
        """Register each services/browser SourceAdapter as a live CatalogProvider
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

            errors = []
            for adapter in all_adapters():
                try:
                    self.register(BrowserCatalogAdapter(adapter))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{getattr(adapter, 'key', type(adapter).__name__)}: {exc}")
            self.browser_bootstrap_error = "; ".join(errors) or None
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

    def _build_index_locked(self, *, force: bool = False) -> int:
        """Must be called with self._lock held. Returns record count.

        Tries the on-disk snapshot first (``force=False``): a fingerprint hit
        restores the whole FTS index with ZERO provider ``iter_records()``
        calls. A miss falls back to the full enumeration and re-persists.
        """
        self._index_building = True
        try:
            fingerprint = self._provider_fingerprint()
            previous_fingerprint = self._index_fingerprint
            self._index_fingerprint = fingerprint
            self._diagnostics = []
            self._last_revalidate = time.monotonic()
            if force or (previous_fingerprint and previous_fingerprint != fingerprint):
                for provider in self._registry.iter_indexable():
                    invalidate = getattr(provider, "invalidate_cache", None)
                    if callable(invalidate):
                        invalidate()
            if not force and fingerprint and self._index.load_if_fresh(fingerprint):
                self._index_built = True
                self._index_source = "cache"
                return self._index.record_count
            count = self._index.rebuild(self._iter_all_records())
            self._index_built = True
            self._index_source = "fresh"
            if not self._diagnostics:
                self._index.persist(fingerprint)
            return count
        finally:
            self._index_building = False

    def rebuild_index(self) -> int:
        """Force a full index rebuild (skips the snapshot). Returns record count."""
        with self._lock:
            self._index_built = False
            return self._build_index_locked(force=True)

    # ── Persistent-cache validity ────────────────────────────────────────

    def _provider_fingerprint(self) -> str:
        """Cheap signature of everything that could change the index.

        Stats only — never enumerates a single record. Inputs:
          * ``_INDEX_CODE_VERSION`` (hand-bumped when record shapes change),
          * the ``provider_id`` (or class path) of every registered indexable
            provider, in registration order,
          * ``(size, st_mtime_ns)`` of every file-backed data source: the
            ``*.blt.yaml`` bolt/bearing tables, ``taxonomy.json``, the
            structural section + lumber tables, the vendor_parts
            ``index.json`` registry, and any ``CHUDWARE_KICAD_SYM_PATH`` libs,
          * ``(count, max_mtime_ns, total_size)`` of the watched parts-library
            folder, walked with ``os.scandir`` and capped at
            ``_FP_WALK_LIMIT`` dirents (beyond which it is marked ``partial``).

        Dropping a new STEP file into the watched folder moves at least the
        count and max mtime, so a new part can never be silently missed.
        """
        h = hashlib.blake2b(digest_size=16)
        h.update(f"code={_INDEX_CODE_VERSION}\n".encode())
        for provider in self._registry.iter_indexable():
            pid = getattr(provider, "provider_id", None)
            if not pid:
                cls = type(provider)
                pid = f"{cls.__module__}.{cls.__qualname__}"
            h.update(f"p={pid}\n".encode("utf-8", "replace"))
            paths = getattr(provider, "fingerprint_paths", None)
            if callable(paths):
                for path in paths():
                    _hash_stat(h, f"{pid}/{path}", path)
        for label, path in _fingerprint_sources():
            _hash_stat(h, label, path)
        count, max_mtime_ns, total_size, partial = _library_walk_signature(
            _watched_library_root())
        h.update(
            f"lib={count}:{max_mtime_ns}:{total_size}:{int(partial)}\n".encode())
        return h.hexdigest()

    def revalidate(self) -> bool:
        """Recompute the (stat-only) fingerprint; invalidate a stale index.

        Returns True when the index was invalidated — the next search rebuilds.
        Safe to call at any time; never raises.
        """
        try:
            fingerprint = self._provider_fingerprint()
        except Exception as exc:  # noqa: BLE001 — revalidation is best-effort
            log.debug("parts index revalidation failed: %s", exc)
            return False
        with self._lock:
            self._last_revalidate = time.monotonic()
            if not self._index_built or not self._index_fingerprint:
                return False
            if fingerprint == self._index_fingerprint:
                return False
            self._index_built = False
            return True

    def _maybe_revalidate(self) -> None:
        """Rate-limited :meth:`revalidate` — at most once per interval."""
        interval = _revalidate_interval_s()
        if interval <= 0 or not self._index_built:
            return
        if (time.monotonic() - self._last_revalidate) < interval:
            return
        self.revalidate()

    def _provider_error(self, provider: object, operation: str, exc: Exception) -> None:
        diagnostic = {
            "provider": str(getattr(provider, "provider_id", type(provider).__name__)),
            "operation": operation, "error": f"{type(exc).__name__}: {exc}",
        }
        if diagnostic not in self._diagnostics:
            self._diagnostics.append(diagnostic)
            log.warning("parts provider %s failed: %s", operation, diagnostic)

    def index_status(self) -> dict:
        """Snapshot of index state for the ``parts_index_status`` RPC.

        Lock-free by design: it must answer instantly while a build is running.
        """
        built_at = self._index.built_at
        return {
            "built": bool(self._index_built),
            "building": bool(self._index_building),
            "count": int(self._index.record_count),
            "source": self._index_source,
            "native_ecad_available": any(
                getattr(provider, "provider_id", None) == "ecad_kicad"
                for provider in self._registry.get_by_domain("ecad")),
            "age_s": round(max(0.0, time.time() - built_at), 3) if built_at else 0.0,
            "diagnostics": ([{"provider": "browser", "operation": "registration",
                              "error": self.browser_bootstrap_error}]
                            if self.browser_bootstrap_error else []) + list(self._diagnostics),
        }

    def _iter_all_records(self) -> Iterator[dict]:
        """Yield all PartRecord dicts from indexable providers."""
        for provider in self._registry.iter_indexable():
            try:
                yield from provider.iter_records()
            except NotImplementedError:
                continue  # Live-only providers intentionally cannot be enumerated.
            except Exception as exc:  # noqa: BLE001 — keep other providers searchable
                self._provider_error(provider, "enumeration", exc)

    # ------------------------------------------------------------------
    # Search + get
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | list[str] | None = None,
               filters: dict | None = None,
               limit: int = 20,
               sites: list[str] | None = None,
               include_live: bool = True,
               force_live: bool = False) -> list[dict]:
        """Full-text search. Returns ranked PartRecord dicts.

        ``domain`` accepts a single domain string or a list of domains
        (taxonomy routing, Parts Library V2) — semantics otherwise unchanged.

        ``sites`` is an optional vendor allow-list for the LIVE fan-out: when
        given, only live providers whose ``site`` is in the list are queried.
        This is how taxonomy routing keeps a query on the vendors that fit its
        category (contracts/parts.md §6.3) — an electronics search never touches
        McMaster or a metals house, a metals search never touches DigiKey — so a
        query only ever spends a fetch against a vendor that could plausibly
        stock it. ``None`` selects all matching providers; ``[]`` selects none.

        Primary: FTS5 index over indexable providers, merged with live local
        KiCad library search (including project libraries).
        Fallback: local hits are checked against ``PartsIndex.term_coverage()``
        (Contract: cheap, never raises). Local is weak when no results exist or
        a query term-group matched nothing in the corpus. If coverage is
        unavailable, a small hit count is used as a fallback signal. A query
        like "stepper nema 17" can otherwise match bearings on the lone "17"
        while missing "stepper" and "nema". When local is weak AND ``include_live`` is
        true, the live vendor fan-out still runs and its results are MERGED
        with local (never gated behind an early return) — see
        ``_merge_local_live``. Strong local results normally skip the live probe.
        ``force_live=True`` explicitly requests it even when local coverage is
        strong. Local and live results are ranked together without a reserved
        live quota. Live results carry source:"live" and are never indexed.
        Timed-out sites surface as notices. ``include_live=False`` does zero
        network, unconditionally.

        A stat-only revalidation runs at most every
        ``CHUDWARE_PARTS_REVALIDATE_S`` seconds (default 20, 0 disables) so a
        long-lived worker notices files dropped into the watched library
        without hammering the disk on every keystroke.
        """
        self._maybe_revalidate()
        self._ensure_index()
        # Two equivalent indexed providers may contribute the same native recipe.
        # Fetch enough candidates to collapse pairs without shortening the page.
        candidate_limit = max(1, limit * 2)
        results = self._index.search(query, domain=domain, limit=candidate_limit)
        domains = ([domain] if domain else []) if isinstance(domain, str) else list(domain or [])
        # An unscoped empty browse uses the prebuilt index. A native empty search
        # can parse the entire installed KiCad corpus synchronously.
        if "ecad" in domains or (not domains and query.strip()):
            for provider in self._registry.get_by_domain("ecad"):
                if getattr(provider, "provider_id", None) != "ecad_kicad":
                    continue
                try:
                    kicad = provider.search(query, domain="ecad", filters=filters,
                                            limit=candidate_limit)
                except Exception as exc:  # noqa: BLE001 — the FTS index is still usable
                    if filters and filters.get("doc_id"):
                        raise
                    self._provider_error(provider, "search", exc)
                    continue
                results = _merge_kicad_results(results, kicad, query, candidate_limit,
                                               ecad_only=bool(domains) and set(domains) == {"ecad"})
        if results:
            results = _dedupe_related(_merge_native_duplicates(results))
            results.sort(key=lambda rec: score_record(rec, query), reverse=True)
        local_page = results[:limit]
        # Local-first phase: the caller wants only the instant FTS layer and will
        # fan out to live vendors separately (Parts Library streaming search).
        if not include_live:
            return local_page
        if not force_live and not self._local_is_weak(query, local_page, limit):
            return local_page
        live = self._search_live(query, domain=domain, filters=filters,
                                 limit=limit, sites=sites)
        if not live:
            return local_page
        return _merge_local_live(results, live, limit, query=query)

    def _local_is_weak(self, query: str, results: list[dict], limit: int) -> bool:
        """Weak-local test gating the live fan-out merge (Contract §Recall).

        Weak when there are no local results at all, or when term coverage is
        partial — some query term-group matched nothing in the corpus, so the
        rows we do have were assembled from a subset of what the user asked
        for and a vendor probably stocks the real thing.

        A FULL-coverage query is never weak, however few rows it returns. An
        exact designation ("608zz", "as568-014 viton") legitimately matches a
        handful of parts. When no usable coverage signal exists, the hit-count
        heuristic is used instead. Cheap by contract — never raises, never
        touches the network.
        """
        if not results:
            return True
        try:
            matched, total = self._index.term_coverage(query)
        except Exception:  # noqa: BLE001 — term_coverage must never gate correctness
            matched, total = (0, 0)
        if total > 0:
            return matched < total
        # No usable coverage signal (empty query, or an index without the
        # probe): fall back to the hit-count heuristic.
        return len(results) < max(1, limit // 2)

    def _search_live(self, query: str, *,
                     domain: str | list[str] | None,
                     filters: dict | None,
                     limit: int,
                     sites: list[str] | None) -> list[dict]:
        """Concurrent, deadline-bounded live-vendor fan-out.

        Provider discovery and worker startup count against the wall budget.
        Return completed hits and per-site notices without waiting for a stalled
        provider; daemon workers cannot keep the process alive after return.
        """
        budget = _live_site_budget_s(_live_budget_s())
        deadline = time.monotonic() + budget
        domains = [domain] if isinstance(domain, str) else list(domain or [])
        site_allow = set(sites) if sites is not None else None
        if site_allow is not None and not site_allow:
            return []

        candidates: list[tuple[object, str, bool]] = []
        for provider in self._registry.providers():
            pdomain = getattr(provider, "domain", None)
            if domains and pdomain and pdomain not in domains:
                continue
            psite = getattr(provider, "site", None)
            if site_allow is not None and psite not in site_allow:
                continue
            if callable(getattr(provider, "search", None)):
                site = str(psite or getattr(provider, "provider_id", "unknown") or "unknown")
                candidates.append((provider, site, bool(psite)))
        if not candidates:
            return []
        if sites is not None:
            order = {site: index for index, site in enumerate(sites)}
            candidates.sort(key=lambda item: order.get(item[1], len(order)))

        out_q: queue.Queue = queue.Queue()
        search_owner = owner_key()

        def _start(thread: threading.Thread) -> None:
            with _LIVE_LOCK:
                for finished_thread in list(_LIVE_THREADS):
                    if finished_thread.ident is not None and not finished_thread.is_alive():
                        _LIVE_THREADS.pop(finished_thread)
                _LIVE_THREADS[thread] = search_owner
            try:
                thread.start()
            except BaseException:
                with _LIVE_LOCK:
                    _LIVE_THREADS.pop(thread, None)
                raise

        def _worker(index: int, provider: object) -> None:
            # A queued request never starts after its search's deadline.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            use_slot = candidates[index][1] != "printables" and not _live_slot_held.get()
            acquired = False
            try:
                if use_slot:
                    acquired = _live_fetch_slots.acquire(timeout=remaining)
                    if not acquired or time.monotonic() >= deadline:
                        return
                hits = provider.search(  # type: ignore[attr-defined]
                    query, domain=domain, filters=filters)
                if not isinstance(hits, list):
                    out_q.put(("done", index, [], f"search returned {type(hits).__name__}"))
                else:
                    out_q.put(("done", index, hits, None))
            except Exception as exc:  # noqa: BLE001
                out_q.put(("done", index, [], str(exc) or type(exc).__name__))
            finally:
                if acquired:
                    _live_fetch_slots.release()

        def _launch() -> None:
            # Classify providers and start the same one-worker-per-live-provider
            # fan-out as before. Keeping this off the caller thread also bounds
            # a slow count()/iter_records() probe or worker startup.
            for index, (provider, site, _) in enumerate(candidates):
                if time.monotonic() >= deadline:
                    return
                try:
                    is_live = _is_live_only(provider)
                except Exception as exc:  # noqa: BLE001
                    out_q.put(("done", index, [], str(exc) or type(exc).__name__))
                    continue
                if not is_live:
                    out_q.put(("skip", index, [], None))
                    continue
                out_q.put(("live", index, [], None))
                if time.monotonic() >= deadline:
                    return
                context = copy_context()
                thread = threading.Thread(
                    target=context.run, args=(_worker, index, provider),
                    name=f"parts-live-{site}", daemon=True,
                )
                try:
                    _start(thread)
                except Exception as exc:  # noqa: BLE001
                    out_q.put(("done", index, [], str(exc) or type(exc).__name__))

        if time.monotonic() < deadline:
            context = copy_context()
            launcher = threading.Thread(target=context.run, args=(_launch,),
                                        name="parts-live-launch", daemon=True)
            _start(launcher)

        live_results: list[dict] = []
        completed: set[int] = set()
        confirmed_live: set[int] = set()

        def _collect(message: tuple) -> None:
            kind, index, hits, err = message
            if kind == "live":
                confirmed_live.add(index)
                return
            completed.add(index)
            if kind == "skip":
                return
            if err:
                reason = err if err in (
                    "login_required", "no_profile", "unavailable", "timeout",
                    "challenge_page", "blocked", "captcha", "transport_failed",
                    "parse_failed", "policy_refused", "unsupported", "terms_required",
                    "rate_limited", "not_found", "credentials_required", "internal_error",
                ) else "internal_error"
                live_results.append({
                    "site": candidates[index][1],
                    "reason": reason,
                    "hint": err if reason == "internal_error" else "",
                })
            else:
                live_results.extend(hits)

        while len(completed) < len(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                _collect(out_q.get(timeout=remaining))
            except queue.Empty:
                break

        # Include outcomes queued at the deadline without waiting for late work.
        while True:
            try:
                _collect(out_q.get_nowait())
            except queue.Empty:
                break

        timed_out_sites: set[str] = set()
        for index, (_, site, has_site) in enumerate(candidates):
            if (index in completed or site in timed_out_sites
                    or (index not in confirmed_live and not has_site)):
                continue
            timed_out_sites.add(site)
            live_results.append({
                "site": site,
                "reason": "timeout",
                "hint": (f"live vendor '{site}' did not complete discovery or search "
                         f"within the {budget:g}s live site budget"),
            })

        real: list[dict] = []
        notices: list[dict] = []
        for rec in live_results:
            if rec.get("part_id") and str(rec.get("title") or "").strip():
                if live_relevant(rec, query):
                    real.append(rec)
            else:
                notices.append(rec)
        real.sort(key=lambda rec: score_record(rec, query), reverse=True)
        return real[:limit] + notices

    def get(self, part_id: str, *, include_live: bool = True) -> dict | None:
        """Retrieve a PartRecord, including local KiCad libraries when offline."""
        self._ensure_index()
        local_kicad = [p for p in self._registry.get_by_domain("ecad")
                       if getattr(p, "provider_id", None) == "ecad_kicad"]
        # Native KiCad must beat an indexed starter entry of the same ID, and
        # local/project libraries must also be reachable with include_live=False.
        for provider in local_kicad:
            try:
                rec = provider.get(part_id)
                if rec is not None:
                    return rec
            except Exception as exc:  # noqa: BLE001
                self._provider_error(provider, "lookup", exc)
        rec = self._index.get(part_id)
        if rec is not None or not include_live:
            return rec
        for provider in self._registry.providers():
            if provider in local_kicad:
                continue
            if callable(getattr(provider, "get", None)):
                try:
                    rec = provider.get(part_id)
                    if rec is not None:
                        return rec
                except Exception as exc:  # noqa: BLE001
                    self._provider_error(provider, "lookup", exc)
        return None


def _merge_kicad_results(indexed: list[dict], native: list[dict], query: str,
                         limit: int, *, ecad_only: bool) -> list[dict]:
    """Prefer current KiCad rows without starving unrelated indexed domains."""
    native_by_id = {r["part_id"]: r for r in native if r.get("part_id")}
    indexed = [native_by_id.get(r.get("part_id"), r) for r in indexed]
    seen = {r.get("part_id") for r in indexed}
    additions = [r for r in native if r.get("part_id") and r["part_id"] not in seen]
    if ecad_only:
        merged = list(native)
        seen = {r.get("part_id") for r in merged}
        merged.extend(r for r in indexed if r.get("part_id") not in seen)
        if not str(query or "").strip():
            merged.sort(key=lambda rec: str(rec.get("part_id") or ""))
        return merged[:limit]
    if not additions:
        return indexed[:limit]
    if not str(query or "").strip():
        additions.sort(key=lambda rec: str(rec.get("part_id") or ""))
        merged = []
        extra = iter(additions)
        for i, rec in enumerate(indexed):
            merged.append(rec)
            if i % 5 == 4:
                addition = next(extra, None)
                if addition is not None:
                    merged.append(addition)
            if len(merged) >= limit:
                return merged[:limit]
        merged.extend(extra)
        return merged[:limit]
    exact = str(query or "").strip().casefold()
    exact_additions = [r for r in additions
                       if exact and exact in (str(r.get("designation") or "").casefold(),
                                              str(r.get("part_id") or "").casefold())]
    other_additions = [r for r in additions if r not in exact_additions]
    # Keep the best existing cross-domain result ahead of non-exact KiCad hits.
    if not indexed:
        return (exact_additions + other_additions)[:limit]
    quota = max(0, max(1, limit // 4) - len(exact_additions))
    return (exact_additions + indexed[:1] + other_additions[:quota]
            + indexed[1:] + other_additions[quota:])[:limit]


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


# ── Persistent index cache: location, revalidation, fingerprint inputs ───────

_CACHE_OFF = frozenset({"0", "off", "false", "no", "none", "disabled"})


def _index_cache_path() -> Path | None:
    """Where to snapshot the FTS index, or None to stay purely in-memory.

    ``CHUDWARE_PARTS_INDEX_CACHE=0`` (or off/false/no/none) disables
    persistence; any other value is used verbatim as the snapshot path.
    Default: ``<app data root>/cache/parts_index.sqlite``.
    """
    raw = os.environ.get("CHUDWARE_PARTS_INDEX_CACHE")
    if raw is not None:
        value = raw.strip()
        if value.lower() in _CACHE_OFF or not value:
            return None
        path = Path(value).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug("parts index cache dir unusable (%s): %s", path.parent, exc)
            return None
        return path
    try:
        from services import paths as _paths
        path = Path(_paths.ROOT) / "cache" / "parts_index.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception as exc:  # noqa: BLE001 — no cache is always survivable
        log.debug("parts index cache unavailable: %s", exc)
        return None


def _revalidate_interval_s() -> float:
    """Seconds between stat-only revalidations in search(). 0 disables."""
    raw = os.environ.get("CHUDWARE_PARTS_REVALIDATE_S")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_REVALIDATE_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_REVALIDATE_S


def _hash_stat(h: "hashlib._Hash", label: str, path: Path | str) -> None:
    """Mix ``(size, st_mtime_ns)`` of ``path`` into ``h``; missing → sentinel."""
    try:
        st = os.stat(path)
        h.update(f"{label}={st.st_size}:{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    except OSError:
        h.update(f"{label}=-\n".encode("utf-8", "replace"))


def _fingerprint_sources() -> list[tuple[str, Path]]:
    """Every file-backed data source behind an indexable provider.

    Import failures are swallowed per-source: a source we cannot locate simply
    stops contributing (the code version + the remaining sources still guard
    correctness), it never breaks the build.
    """
    out: list[tuple[str, Path]] = []

    # Bolts + bearings clean-room YAML tables (BoltsAdapter).
    try:
        from .bolts_loader import DATA_DIR as _BOLTS_DIR
        for name in sorted(os.listdir(_BOLTS_DIR)):
            if name.endswith((".yaml", ".yml")):
                out.append((f"bolts/{name}", Path(_BOLTS_DIR) / name))
    except Exception:  # noqa: BLE001
        pass

    # Taxonomy routing tree (domain/site routing feeds the searchable set).
    try:
        from .taxonomy import TAXONOMY_PATH
        out.append(("taxonomy", Path(TAXONOMY_PATH)))
    except Exception:  # noqa: BLE001
        pass

    # Structural sections + lumber tables (StructuralSectionsAdapter).
    try:
        from .adapters import structural_sections_adapter as _ss
        for attr in ("_AISC_PATH", "_LUMBER_PATH"):
            src = getattr(_ss, attr, None)
            if src is not None:
                out.append((f"structural{attr.lower()}", Path(src)))
    except Exception:  # noqa: BLE001
        pass

    # Vendor-part registry JSON — the LocalLibraryAdapter's whole record set.
    try:
        from .vendor_parts import INDEX as _VENDOR_INDEX
        out.append(("vendor_index", Path(_VENDOR_INDEX)))
    except Exception:  # noqa: BLE001
        pass

    # User-supplied KiCad symbol libraries (EcadKicadAdapter, env-pointed).
    for lib in (p.strip() for p in
                os.environ.get("CHUDWARE_KICAD_SYM_PATH", "").split(os.pathsep)):
        if lib:
            out.append((f"kicad/{lib}", Path(lib).expanduser()))

    return out


def _watched_library_root() -> Path | None:
    """The watched parts-library folder — WITHOUT creating it."""
    try:
        from services import paths as _paths
        return Path(_paths.PARTS_LIBRARY)
    except Exception:  # noqa: BLE001
        return None


def _library_walk_signature(root: Path | None) -> tuple[int, int, int, bool]:
    """``(file_count, max_mtime_ns, total_size, partial)`` for a folder tree.

    Pure ``os.scandir`` traversal: stat metadata only, file contents are NEVER
    read or hashed. Symlinked directories are not followed (no cycles) and the
    sweep stops after ``_FP_WALK_LIMIT`` dirents, flagging ``partial``.
    """
    if root is None:
        return (0, 0, 0, False)
    count = 0
    max_mtime_ns = 0
    total_size = 0
    visited = 0
    partial = False
    stack: list[Path | str] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    visited += 1
                    if visited > _FP_WALK_LIMIT:
                        return (count, max_mtime_ns, total_size, True)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    count += 1
                    total_size += st.st_size
                    if st.st_mtime_ns > max_mtime_ns:
                        max_mtime_ns = st.st_mtime_ns
        except OSError:
            continue
    return (count, max_mtime_ns, total_size, partial)


# ── Provider-overlap deduplication (contract-additive) ──────────────────────

# Domains that may emit the same physical hardware designation.
_OVERLAP_DOMAINS = frozenset({"fastener", "bearing", "mech_family"})

# Priority: lower number = preferred canonical row (shown first when designation collides).
_DOMAIN_PRIORITY: dict[str, int] = {
    "fastener":    0,   # BOLTS catalog — authoritative for standards-designated hardware
    "bearing":     0,   # BOLTS bearing rows share the same priority tier as fastener
    "mech_family": 1,   # Core parametric families — instantiation recipe, not catalog record
}


def _normalise_desig(desig: str | None) -> str:
    """Canonical form for standards and designations (strip spaces, upper-case)."""
    if not desig:
        return ""
    return desig.replace(" ", "").upper()

def _merge_native_duplicates(results: list[dict]) -> list[dict]:
    """Collapse catalog/core rows only when they describe the same native recipe."""
    groups: dict[tuple[str, str], dict[str, list[tuple[int, dict]]]] = {}
    for index, rec in enumerate(results):
        domain = rec.get("domain")
        ref = rec.get("geometry_ref") or {}
        if domain not in _OVERLAP_DOMAINS or ref.get("kind") != "core_family":
            continue
        family, size = str(ref.get("family") or ""), str(ref.get("size") or "")
        if not family or not size:
            continue
        group = groups.setdefault((family.casefold(), size.casefold()), {"catalog": [], "core": []})
        group["core" if domain == "mech_family" else "catalog"].append((index, rec))

    removed: set[int] = set()
    replacements: dict[int, dict] = {}
    for group in groups.values():
        for core_index, core in group["core"]:
            for catalog_index, catalog in group["catalog"]:
                if catalog_index in removed or core_index in removed:
                    continue
                if (catalog.get("insert_params") or {}) != (core.get("insert_params") or {}):
                    continue
                cat_std = "".join(c for c in str(catalog.get("standard") or "").casefold() if c.isalnum())
                core_std = "".join(c for c in str(core.get("standard") or "").casefold() if c.isalnum())
                if cat_std and core_std and cat_std not in core_std:
                    continue
                cat_params, core_params = catalog.get("params") or {}, core.get("params") or {}
                if (cat_params.get("material") and core_params.get("material") and
                        cat_params["material"] != core_params["material"]):
                    continue
                merged = dict(catalog)
                merged["geometry_ref"] = {**catalog["geometry_ref"], **core["geometry_ref"]}
                merged["params"] = {**core_params, **cat_params}
                if core.get("description") and not merged.get("description"):
                    merged["description"] = core["description"]
                merged["aliases"] = list(dict.fromkeys(
                    [*(catalog.get("aliases") or []), *(core.get("aliases") or []),
                     *([core["standard"]] if core.get("standard") else [])]))
                merged["related_ids"] = list(dict.fromkeys(
                    [*(catalog.get("related_ids") or []), *(core.get("related_ids") or []),
                     core["part_id"]]))
                first = min(core_index, catalog_index)
                replacements[first] = merged
                removed.update((core_index, catalog_index))
                break
    if not replacements:
        return results
    return [replacements[index] if index in replacements else rec
            for index, rec in enumerate(results)
            if index not in removed or index in replacements]



def _dedupe_related(results: list[dict]) -> list[dict]:
    """Inject ``related_ids`` on matching physical identities and re-rank groups.

    Only touches records whose domain is in ``_OVERLAP_DOMAINS``.  Non-hardware
    domains (ecad, section, fitting, hydraulic) pass through unchanged.

    Contract-additive: ``related_ids`` is a NEW optional list[str] field on PartRecord —
    contracts/parts.md §0 permits new optional fields without a version bump.
    """
    # A size alone (e.g. M8) does not identify a screw, nut or washer.
    from collections import defaultdict
    desig_groups: dict[tuple[str, ...], list[tuple[int, dict]]] = defaultdict(list)
    for idx, rec in enumerate(results):
        if rec.get("domain") in _OVERLAP_DOMAINS:
            key = _dedupe_key(rec)
            if key and key[0] == "standard":
                desig_groups[key].append((idx, rec))

    overlap_indices: set[int] = set()
    clusters: dict[int, list[dict]] = {}
    for group in desig_groups.values():
        if len(group) < 2:
            continue
        overlap_indices.update(index for index, _ in group)
        ids = [rec["part_id"] for _, rec in group]
        ordered = sorted(group, key=lambda pair: _DOMAIN_PRIORITY.get(pair[1].get("domain", ""), 99))
        clusters[min(index for index, _ in group)] = [
            {**rec, "related_ids": [pid for pid in ids if pid != rec["part_id"]]}
            for _, rec in ordered
        ]
    if not clusters:
        return results
    out = []
    for index, rec in enumerate(results):
        if index in clusters:
            out.extend(clusters[index])
        elif index not in overlap_indices:
            out.append(rec)
    return out


# ── Local/live merge & cross-boundary dedupe (weak-local fan-out) ────────────

def _dedupe_key(rec: dict) -> tuple[str, ...] | None:
    """Use a governing standard and designation, never a generic title or size."""
    domain = str(rec.get("domain") or "").strip().lower()
    standard = _normalise_desig(rec.get("standard"))
    designation = _normalise_desig(rec.get("designation"))
    if domain and standard and designation:
        geometry = rec.get("geometry_ref")
        params = rec.get("params")
        geometry = geometry if isinstance(geometry, dict) else {}
        params = params if isinstance(params, dict) else {}
        length = geometry.get("length_mm", params.get("length_mm"))
        return ("standard", domain, standard, designation,
                "" if length is None else str(length))
    part_id = str(rec.get("part_id") or "").strip()
    return ("id", part_id) if part_id else None


def _record_richness(rec: dict) -> int:
    """Cheap completeness score used to pick the better of two merged records.

    Never raises: every access is defensive against missing/malformed fields
    (live vendor rows are less structured than local PartRecords).
    """
    score = 0
    try:
        if rec.get("geometry_ref"):
            score += 2
        if rec.get("standard"):
            score += 1
        if rec.get("designation"):
            score += 1
        params = rec.get("params")
        if isinstance(params, dict) and params:
            score += 1
        sourcing = rec.get("sourcing")
        if isinstance(sourcing, list):
            for row in sourcing:
                if not isinstance(row, dict):
                    continue
                if row.get("unit_price") is not None:
                    score += 1
                if row.get("cad_format") or row.get("product_url"):
                    score += 1
                if row.get("datasheet_url") or row.get("datasheet"):
                    score += 1
        # Vendor-flavored live rows (part_source-style) carry these directly
        # on the record rather than inside sourcing[].
        if rec.get("price") is not None or rec.get("unit_price") is not None:
            score += 1
        if rec.get("datasheet_url") or rec.get("datasheet"):
            score += 1
        if rec.get("cad_url") or rec.get("model_url") or rec.get("step_url"):
            score += 1
    except Exception:  # noqa: BLE001 — richness is a tiebreaker, never fatal
        return score
    return score


def _merge_local_live(local: list[dict], live: list[dict], limit: int,
                      *, query: str = "") -> list[dict]:
    """Merge and rank local/live records, deduping only physical identities.

    When a part appears on both sides, the richer record wins and sourcing
    from both is retained. Stable relevance ordering keeps local results ahead
    on equal scores. Timeout and other notice stubs always survive the limit.
    """
    real_live = [r for r in live
                 if r.get("part_id") and str(r.get("title") or "").strip()]
    notices = [r for r in live if r not in real_live]

    merged: list[dict] = []
    seen: dict[tuple[str, ...], int] = {}

    def _add(rec: dict) -> None:
        key = _dedupe_key(rec)
        if key is None:
            merged.append(rec)
            return
        existing_idx = seen.get(key)
        if existing_idx is None:
            seen[key] = len(merged)
            merged.append(rec)
            return
        previous = merged[existing_idx]
        winner, other = ((rec, previous) if _record_richness(rec) > _record_richness(previous)
                         else (previous, rec))
        combined = dict(winner)
        sourcing = list(winner.get("sourcing") or [])
        for source in other.get("sourcing") or []:
            if source not in sourcing:
                sourcing.append(source)
        if sourcing:
            combined["sourcing"] = sourcing
        merged[existing_idx] = combined

    for rec in local:
        _add(rec)
    for rec in real_live:
        _add(rec)

    real_merged = [r for r in merged
                   if r.get("part_id") and str(r.get("title") or "").strip()]
    real_merged.sort(key=lambda rec: score_record(rec, query), reverse=True)
    return real_merged[:limit] + notices


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

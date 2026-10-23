"""PartsHub — the orchestrator for aicad.parts/1.

Ties together the ProviderRegistry, the FTS index, and the provider lookup.
Bootstrap auto-registers all adapters:
  core:   bolts/bearings + core_families
  domain: structural_sections, piping_fittings, hydraulic_components,
          ecad_kicad (indexable)

Live-vendor fallback: when the local FTS index returns no results for a
query, any registered live-only provider (iter_records raises NotImplementedError)
is queried directly. Results from live providers carry source:"live" and are
NEVER indexed.

Provider-overlap deduplication (contract-additive):
  For standard threaded hardware (fasteners / bearings / nuts / washers), the
  BoltsAdapter (domain="fastener") and CoreFamiliesAdapter (domain="mech_family")
  both emit records for the same designation (e.g. "M8").  After ranking, the hub:
  1. Groups records with the same normalised designation across these two domains.
  2. Keeps both, but promotes fastener > mech_family (canonical row first).
  3. Injects a ``related_ids`` additive field (list[str]) on each record pointing
     to its sibling(s).  Contract-additive per contracts/parts.md §0.
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
from contextvars import copy_context
from services.agent.request_context import owner_key

from .registry import ProviderRegistry
from .index import PartsIndex

log = logging.getLogger(__name__)

_LIVE_THREADS: dict[threading.Thread, tuple[str, str] | None] = {}
_LIVE_LOCK = threading.Lock()


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
_DEFAULT_LIVE_BUDGET_S = 4.0

# ── Persistent index cache ───────────────────────────────────────────────────

# Bumped BY HAND whenever adapter code changes the shape/content of emitted
# PartRecords. It is mixed into the fingerprint, so bumping it invalidates every
# user's on-disk snapshot without touching their data files.
_INDEX_CODE_VERSION = 2

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

    def _build_index_locked(self, *, force: bool = False) -> int:
        """Must be called with self._lock held. Returns record count.

        Tries the on-disk snapshot first (``force=False``): a fingerprint hit
        restores the whole FTS index with ZERO provider ``iter_records()``
        calls. A miss falls back to the full enumeration and re-persists.
        """
        self._index_building = True
        try:
            fingerprint = self._provider_fingerprint()
            self._index_fingerprint = fingerprint
            self._last_revalidate = time.monotonic()
            if not force and fingerprint and self._index.load_if_fresh(fingerprint):
                self._index_built = True
                self._index_source = "cache"
                return self._index.record_count
            count = self._index.rebuild(self._iter_all_records())
            self._index_built = True
            self._index_source = "fresh"
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
            "age_s": round(max(0.0, time.time() - built_at), 3) if built_at else 0.0,
        }

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
        Fallback: local hits are checked against ``PartsIndex.term_coverage()``
        (Contract: cheap, never raises). Local is WEAK when a meaningful
        fraction of the query's term-groups matched nothing in the corpus, or
        when the local hit count falls well short of ``limit`` — the proven
        failure mode where a query like "stepper nema 17" gets zero real
        matches on "stepper"/"nema" but the lone token "17" coincidentally
        matches bearing designations, and its BM25 rarity makes those junk
        rows outrank everything. When local is weak AND ``include_live`` is
        true, the live vendor fan-out still runs and its results are MERGED
        with local (never gated behind an early return) — see
        ``_merge_local_live``. Strong local (full term coverage, plentiful
        hits) returns immediately with no live probe, matching prior
        behavior and latency. Live results carry source:"live" and are NEVER
        indexed. Timed-out sites surface as notice stubs (no part_id/title)
        so callers can render honest per-site timeout notices.
        ``include_live=False`` does ZERO network, unconditionally.

        A stat-only revalidation runs at most every
        ``CHUDWARE_PARTS_REVALIDATE_S`` seconds (default 20, 0 disables) so a
        long-lived worker notices files dropped into the watched library
        without hammering the disk on every keystroke.
        """
        self._maybe_revalidate()
        self._ensure_index()
        results = self._index.search(query, domain=domain, limit=limit)
        if results:
            results = _dedupe_related(results)
        # Local-first phase: the caller wants only the instant FTS layer and will
        # fan out to live vendors separately (Parts Library streaming search). Skip
        # the synchronous browser fan-out so the local call returns immediately.
        if not include_live:
            return results
        if not self._local_is_weak(query, results, limit):
            return results
        live = self._search_live(query, domain=domain, filters=filters,
                                 limit=limit, sites=sites)
        if not live:
            return results
        return _merge_local_live(results, live, limit)

    def _local_is_weak(self, query: str, results: list[dict], limit: int) -> bool:
        """Weak-local test gating the live fan-out merge (Contract §Recall).

        Weak when there are no local results at all, or when term coverage is
        partial — some query term-group matched nothing in the corpus, so the
        rows we do have were assembled from a subset of what the user asked
        for and a vendor probably stocks the real thing.

        A FULL-coverage query is never weak, however few rows it returns. An
        exact designation ("608zz", "as568-014 viton") legitimately matches a
        handful of parts, and treating a small-but-precise answer as weak used
        to spend a whole vendor fan-out re-deriving a result already sitting at
        rank 1. The shortfall rule therefore only applies while coverage is
        already incomplete. Cheap by contract — never raises, never touches the
        network.
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
            context = copy_context()
            t = threading.Thread(
                target=context.run, args=(_worker, p),
                name=f"parts-live-{site}",
                daemon=True,
            )
            threads.append(t)
            with _LIVE_LOCK:
                for finished in list(_LIVE_THREADS):
                    if finished.ident is not None and not finished.is_alive():
                        _LIVE_THREADS.pop(finished)
                _LIVE_THREADS[t] = owner_key()
            try:
                t.start()
            except BaseException:
                with _LIVE_LOCK:
                    _LIVE_THREADS.pop(t, None)
                raise

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
        # Fall back to individual providers (handles non-indexable live providers).
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


# ── Local/live merge & cross-boundary dedupe (weak-local fan-out) ────────────

def _dedupe_key(rec: dict) -> tuple[str, str] | None:
    """Identity key for merging the same physical part across local + live.

    Designation is the strongest signal (survives vendor-specific part_ids);
    falls back to normalised title. Notice stubs (no title/designation) get
    no key and are therefore never merged away.
    """
    desig = _normalise_desig(rec.get("designation"))
    if desig:
        return ("d", desig)
    title = str(rec.get("title") or "").strip().lower()
    if title:
        return ("t", title)
    return None


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


def _merge_local_live(local: list[dict], live: list[dict], limit: int) -> list[dict]:
    """Merge weak local results with the live fan-out, deduping by physical part.

    Local rows are added first (strong local hits stay on top), live rows
    follow. When the same part appears on both sides, the richer record wins
    in place — position is NOT reshuffled to "prefer live" or "prefer local",
    only completeness decides. Notice stubs (timeouts/login-required) are
    never deduped and always survive to the end of the result, exactly as
    ``_search_live`` already appends them.
    """
    real_live = [r for r in live
                 if r.get("part_id") and str(r.get("title") or "").strip()]
    notices = [r for r in live if r not in real_live]

    merged: list[dict] = []
    seen: dict[tuple[str, str], int] = {}

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
        if _record_richness(rec) > _record_richness(merged[existing_idx]):
            merged[existing_idx] = rec

    for rec in local:
        _add(rec)
    for rec in real_live:
        _add(rec)

    real_merged = [r for r in merged
                   if r.get("part_id") and str(r.get("title") or "").strip()][:limit]
    return real_merged + notices


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

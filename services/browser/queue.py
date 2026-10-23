"""services/browser/queue.py — background fetch/download queue.

A persistent, thread-safe job queue for vendor searches + CAD model downloads
that run in a daemon background thread inside the MCP server process.  Each job
calls the appropriate source adapter's ``search()`` then ``acquire()`` via the
same authenticated browser sessions managed by :mod:`services.browser.session`.

Design contract
---------------
- **State is in-process only** (no external message broker).  The MCP server
  process is long-lived (started once per app session); jobs survive normal RPC
  calls but are lost if the server restarts.  A persistent on-disk queue is a
  future enhancement.
- **Worker pool, serialised per site** — a small pool of daemon
  threads drains the FIFO. Browser sessions stay single-threaded *within* a
  site (per-site lock + at most one RUNNING job per site). Independent sites
  run in parallel so a slow McMaster scrape cannot stall Digi-Key.
- **Honest placeholder on permanent concurrency**: Playwright contexts are still
  process-global per site; multi-user MCP is out of scope (see
  ``contracts/license_debt.md``).

Public API (called by ``parts_tools.py`` register functions)
------------------------------------------------------------
- :func:`enqueue`      — add a (site, query) job, returns job id + status.
- :func:`queue_status` — current snapshot of all jobs.
- :func:`cancel_job`   — mark a pending job as cancelled (best-effort).
- :func:`ensure_worker_started` — idempotent; call once at server startup.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from contextvars import Context, copy_context
from services.agent.request_context import owner_key


class JobStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


@dataclass
class ScrapeJob:
    id:             str
    site:           str
    query:          str
    status:         JobStatus = JobStatus.PENDING
    created_ts:     float     = field(default_factory=time.time)
    started_ts:     Optional[float] = None
    finished_ts:    Optional[float] = None
    artifact_path:  str = ""
    error:          str = ""
    # Intermediate results from search() (lightweight stubs, not yet acquired).
    search_hits:    list = field(default_factory=list)
    # Optional pre-selected search hit (e.g. the GUI already holds the item URL from
    # part_search) — when set, the worker skips the search step and acquires it directly.
    preset:         Optional[dict] = None
    # Spec metadata extracted from the acquire() provenance envelope:
    # title, sku, price, source_url, fetched_ts, and any other vendor extras.
    # Populated when status transitions to DONE; empty dict until then.
    spec_data:      dict = field(default_factory=dict)
    context:        Context | None = field(default=None, repr=False)
    owner:          tuple[str, str] | None = field(default=None, repr=False)


# ---- Global queue ----------------------------------------------------------------

_lock   = threading.Lock()
_jobs: list[ScrapeJob] = []
_cond   = threading.Condition(_lock)
_worker_started = False
# Small worker pool (not a single thread). Per-site locks keep Playwright
# sessions single-threaded within a site while independent sites run in parallel.
_POOL_SIZE = 4
_site_locks: dict[str, threading.Lock] = {}
_site_locks_guard = threading.Lock()
_workers: list[threading.Thread] = []


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    owner = owner_key()
    with registry_lock(_lock):
        jobs = sum(
            job.owner == owner and (
                job.status in (JobStatus.PENDING, JobStatus.RUNNING)
                or (job.started_ts is not None and job.finished_ts is None))
            for job in _jobs)
        # Every worker claims a published job under this same lock before it can
        # write. Empty executor capacity is idle; terminal status alone is not
        # completion until the worker publishes finished_ts after unwinding.
    return {"by_kind": {"scrape_jobs": jobs}}


def _site_lock(site: str) -> threading.Lock:
    with _site_locks_guard:
        lk = _site_locks.get(site)
        if lk is None:
            lk = threading.Lock()
            _site_locks[site] = lk
        return lk


def enqueue(site: str, query: str, preset: Optional[dict] = None) -> dict:
    """Add a (site, query) job to the download queue.  Idempotent within a session:
    if an identical (site, query) is already pending or running, returns the
    existing job's status instead of queuing a duplicate.

    ``preset``: an already-chosen search-hit dict (must carry the fields the site
    adapter's ``acquire()`` expects, typically ``url``/``title``) — the worker then
    acquires it directly instead of re-searching.
    """
    site  = site.strip().lower()
    query = query.strip()
    if not site or not query:
        return {"ok": False, "reason": "site and query must be non-empty"}

    with _lock:
        # Dedup: pending or running same-site+query.
        for j in _jobs:
            if j.site == site and j.query == query and j.status in (
                    JobStatus.PENDING, JobStatus.RUNNING):
                return {"ok": True, "id": j.id, "status": j.status.value, "deduplicated": True}

        job = ScrapeJob(id=str(uuid.uuid4())[:8], site=site, query=query,
                        preset=preset if isinstance(preset, dict) else None,
                        context=copy_context(), owner=owner_key())
        _jobs.append(job)
        _cond.notify_all()

    return {"ok": True, "id": job.id, "status": job.status.value}


def queue_status() -> dict:
    """Return a snapshot of all jobs (newest first, capped at 200)."""
    with _lock:
        snapshot = list(reversed(_jobs[-200:]))
    return {
        "ok": True,
        "jobs": [
            {
                "id":            j.id,
                "site":          j.site,
                "query":         j.query,
                "status":        j.status.value,
                "artifact_path": j.artifact_path,
                "error":         j.error,
                "created_ts":    j.created_ts,
                "started_ts":    j.started_ts,
                "finished_ts":   j.finished_ts,
                # Spec metadata from acquire() provenance envelope (title, sku, price, …).
                # Empty dict for pending/running/error jobs; populated on DONE.
                "spec_data":     j.spec_data,
            }
            for j in snapshot
        ],
    }


def cancel_job(job_id: str) -> dict:
    """Mark a pending job cancelled.  Has no effect on running/finished jobs."""
    with _lock:
        for j in _jobs:
            if j.id == job_id:
                if j.status == JobStatus.PENDING:
                    j.status = JobStatus.CANCELLED
                    return {"ok": True, "id": job_id, "status": j.status.value}
                return {"ok": False, "reason": f"job {job_id!r} is {j.status.value}, not cancellable"}
    return {"ok": False, "reason": f"job {job_id!r} not found"}


# ---- Worker thread ---------------------------------------------------------------

def _run_job(job: ScrapeJob) -> None:
    """Execute a single job synchronously (runs on the worker thread)."""
    try:
        from services.browser.adapters import get_adapter
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.ERROR
        job.error  = f"import adapters failed: {exc}"
        return

    adapter = get_adapter(job.site)
    if adapter is None:
        job.status = JobStatus.ERROR
        job.error  = f"no adapter registered for site '{job.site}'"
        return

    # Step 1: search — skipped when the caller already picked the hit (GUI download
    # button holds the item URL from part_search).
    if job.preset is not None:
        hits = [job.preset]
    else:
        try:
            hits = adapter.search(job.query)
        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.ERROR
            job.error  = f"search failed: {exc}"
            return

    if not hits:
        job.status = JobStatus.ERROR
        job.error  = "search returned no results"
        return

    # Surface login_required gracefully.
    first = hits[0] if isinstance(hits[0], dict) else {}
    if not first.get("ok", True) and first.get("reason") in ("login_required", "no_profile"):
        job.status = JobStatus.ERROR
        job.error  = first.get("hint", "login required — use vendor_login first")
        return

    job.search_hits = hits

    # Step 2: acquire the first real hit.
    for hit in hits:
        if not isinstance(hit, dict) or not hit.get("ok", True):
            continue  # skip error stubs
        try:
            result = adapter.acquire(hit)
        except Exception as exc:  # noqa: BLE001
            job.error = f"acquire failed: {exc}"
            continue
        if result.get("ok"):
            job.artifact_path = result.get("artifact_path", "")
            job.status = JobStatus.DONE
            # Preserve the full acquisition provenance (except the success flag)
            # alongside vendor extras so downstream imports and pricing registration
            # can stamp the exact source, browser profile, licence, and fetch time.
            job.spec_data = {k: v for k, v in result.items()
                             if k != "ok" and v is not None}
            job.spec_data.setdefault("site", job.site)
            return
        else:
            job.error = result.get("hint", result.get("reason", "acquire failed"))
            # Don't break — try next hit.

    # All hits failed.
    job.status = JobStatus.ERROR
    # job.error already set from last attempt


def _claim_next_job() -> Optional[ScrapeJob]:
    """Oldest pending job whose site is not already being worked by another pool thread.

    Sites with an in-flight RUNNING job are skipped so two workers never share one
    Playwright context. Other sites remain eligible in parallel (worker pool).
    """
    with _lock:
        running_sites = {j.site for j in _jobs if j.status == JobStatus.RUNNING}
        pending = [j for j in _jobs if j.status == JobStatus.PENDING]
        for job in pending:
            if job.site in running_sites:
                continue
            job.status = JobStatus.RUNNING
            job.started_ts = time.time()
            return job
    return None


def _worker_loop(worker_id: int) -> None:
    """Long-lived daemon thread: drain the job queue until process exit."""
    while True:
        with _cond:
            while True:
                # Wake when any PENDING exists; claim may still return None if all
                # pending sites are already RUNNING on sibling workers.
                pending = [j for j in _jobs if j.status == JobStatus.PENDING]
                if pending:
                    break
                _cond.wait(timeout=5.0)

        job = _claim_next_job()
        if job is None:
            # All pending jobs are blocked behind same-site RUNNING work — brief yield.
            time.sleep(0.05)
            continue

        try:
            with _site_lock(job.site):
                if job.context is None:
                    _run_job(job)
                else:
                    job.context.run(_run_job, job)
        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.ERROR
            job.error  = f"worker exception: {exc}"
        finally:
            with _lock:
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.ERROR
                    job.error  = "job left in running state (unexpected)"
                job.finished_ts = time.time()
            with _cond:
                _cond.notify_all()  # wake siblings waiting on same-site backlog


def ensure_worker_started() -> None:
    """Start the background worker pool exactly once (idempotent)."""
    global _worker_started
    if _worker_started:
        return
    for i in range(_POOL_SIZE):
        t = threading.Thread(
            target=_worker_loop, args=(i,), name=f"scrape-worker-{i}", daemon=True
        )
        with _lock:
            _workers.append(t)
        try:
            t.start()
        except BaseException:
            with _lock:
                _workers.remove(t)
            raise
    _worker_started = True

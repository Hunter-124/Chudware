"""services/browser/queue.py — background fetch/download queue.

A thread-safe, in-process job queue for vendor searches and artifact downloads
that run in daemon background threads inside the MCP server process. Each job
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
- :func:`select_member` — retry an owned ambiguous job with a chosen option value.
- :func:`cancel_job`   — mark a pending job as cancelled (best-effort).
- :func:`ensure_worker_started` — idempotent; call once at server startup.
"""
from __future__ import annotations

import os
import stat
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
    error:          Optional[dict] = None
    # Search results include structured notices as well as usable hits.
    search_hits:    list = field(default_factory=list)
    # Retain every attempted acquisition failure, including before a later success.
    acquire_errors: list = field(default_factory=list)
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
    """Add a job to the download queue. An identical pending/running job for
    the same owner, query, and selected hit is deduplicated.

    ``preset``: an already-chosen search-hit dict (must carry the fields the site
    adapter's ``acquire()`` expects, typically ``url``/``title``) — the worker then
    acquires it directly instead of re-searching.
    """
    site = site.strip().lower() if isinstance(site, str) else ""
    query = query.strip() if isinstance(query, str) else ""
    if not site or not query:
        return _queue_failure(site, "enqueue", "parse_failed", "site and query must be non-empty")
    owner = owner_key()
    selected = dict(preset) if isinstance(preset, dict) else None

    with _lock:
        # A different owner or selected model must never reuse another job.
        for j in _jobs:
            if (j.owner == owner and j.site == site and j.query == query
                    and j.preset == selected and j.status in (
                        JobStatus.PENDING, JobStatus.RUNNING)):
                return {"ok": True, "id": j.id, "status": j.status.value, "deduplicated": True}

        job = ScrapeJob(id=str(uuid.uuid4())[:8], site=site, query=query,
                        preset=selected, context=copy_context(), owner=owner)
        _jobs.append(job)
        _cond.notify_all()

    return {"ok": True, "id": job.id, "status": job.status.value}


_SELECTION_FIELDS = frozenset({
    "archive_member", "member", "filename", "file_name", "url", "download_url",
    "file_id", "profile_id", "instance_id",
})


def _selection_context(job: ScrapeJob) -> tuple[dict, str, set[str]] | None:
    """Resolve an actionable choice and its original usable hit."""
    error = job.error if job.status == JobStatus.ERROR else None
    if not isinstance(error, dict):
        return None
    question = error.get("ask_user")
    field = error.get("selection_field")
    if (not isinstance(question, dict) or question.get("kind") != "choice" or
            not isinstance(field, str) or field not in _SELECTION_FIELDS):
        return None
    options = question.get("options")
    if not isinstance(options, list):
        return None
    offered = [option["value"] for option in options
               if isinstance(option, dict) and isinstance(option.get("value"), str)
               and option["value"] and option["value"] != "cancel"]
    values = set(offered)
    if not values or len(values) != len(offered):
        return None
    for hit in (error.get("hit"), job.preset):
        if isinstance(hit, dict) and hit.get("ok", True):
            return hit, field, values
    url = error.get("url")
    if url:
        for hit in job.search_hits:
            if (isinstance(hit, dict) and hit.get("ok", True) and
                    hit.get("url") == url):
                return hit, field, values
    return None


def queue_status() -> dict:
    """Return up to 200 jobs owned by the current caller, newest first.

    ``error`` is a C1 failure object or None. ``search_hits`` retains source
    notices alongside hits, and ``acquire_errors`` retains every failed attempt.
    A done job has a nonempty classified artifact and its provenance, including
    ``artifact_kind`` and ``format`` when known, in ``spec_data``.
    ``cad_validated`` is true only for verified CAD; false also covers
    provisional native-format downloads. ``can_select_member`` indicates whether
    an offered choice can be retried through :func:`select_member`.
    """
    owner = owner_key()
    with _lock:
        snapshot = []
        for job in reversed(_jobs):
            if job.owner == owner:
                snapshot.append(job)
                if len(snapshot) == 200:
                    break
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
                "can_select_member": _selection_context(j) is not None,
                "search_hits":   j.search_hits,
                "acquire_errors": j.acquire_errors,
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
    owner = owner_key()
    with _lock:
        for j in _jobs:
            if j.id == job_id and j.owner == owner:
                if j.status == JobStatus.PENDING:
                    j.status = JobStatus.CANCELLED
                    return {"ok": True, "id": job_id, "status": j.status.value}
                return _queue_failure(j.site, "cancel", "policy_refused",
                                      f"job {job_id!r} is {j.status.value}, not cancellable")
    return _queue_failure("", "cancel", "not_found", f"job {job_id!r} not found")


def select_member(job_id: str, member: str, format: str | None = None) -> dict:
    """Retry an owned choice using its option value and declared selection field."""
    if (not isinstance(job_id, str) or not job_id or
            not isinstance(member, str) or not member):
        return _queue_failure("", "select_member", "parse_failed",
                              "job_id and member must be non-empty strings")
    if format is not None and not isinstance(format, str):
        return _queue_failure("", "select_member", "parse_failed",
                              "format must be a string when supplied")
    owner = owner_key()
    with _lock:
        job = next((j for j in _jobs if j.id == job_id and j.owner == owner), None)
        if job is None:
            return _queue_failure("", "select_member", "not_found",
                                  f"job {job_id!r} not found")
        context = _selection_context(job)
        if context is None:
            return _queue_failure(job.site, "select_member", "unsupported",
                                  "job is not awaiting a retryable model selection")
        hit, field, options = context
        if member not in options:
            return _queue_failure(job.site, "select_member", "unsupported",
                                  "option was not offered by this job")
        selected = dict(hit)
        selected[field] = member
        if format and format.strip():
            selected["format"] = format.strip()
        elif field in ("member", "archive_member") and str(selected.get("format") or "").lower() == "zip":
            selected.pop("format", None)
        site, query = job.site, job.query
    return enqueue(site, query, preset=selected)


# ---- Worker thread ---------------------------------------------------------------

def _queue_failure(site: str, op: str, reason: str, hint: str, *,
                   url: str | None = None) -> dict:
    """Create a C1 failure without depending on adapter import succeeding."""
    return {"ok": False, "site": site, "op": op, "reason": reason,
            "hint": hint, "url": url, "status": None, "retry_after_s": None,
            "ask_user": None}


def _failure(job: ScrapeJob, op: str, reason: str, hint: str, *,
             url: str | None = None) -> dict:
    return _queue_failure(job.site, op, reason, hint, url=url)


def _source_failure(job: ScrapeJob, op: str, result: dict, *,
                    url: str | None = None) -> dict:
    """Keep the source failure and fill only missing queue context."""
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "parse_failed"
    hint = result.get("hint") or result.get("detail")
    if not isinstance(hint, str) or not hint.strip():
        hint = f"{op} failed ({reason})"
    failure = dict(result)
    failure.update({
        "ok": False,
        "site": result.get("site") or job.site,
        "op": result.get("op") or op,
        "reason": reason,
        "hint": hint,
        "url": result.get("url") or result.get("source_url") or url,
        "status": result.get("status"),
        "retry_after_s": result.get("retry_after_s"),
        "ask_user": result.get("ask_user"),
    })
    return failure


def _exception_failure(job: ScrapeJob, op: str, exc: Exception, *,
                       url: str | None = None) -> dict:
    reason = "timeout" if isinstance(exc, TimeoutError) else "internal_error"
    return _failure(job, op, reason, f"{op} raised {type(exc).__name__}: {exc}",
                    url=url)


def _set_error(job: ScrapeJob, error: dict) -> None:
    job.error = error
    job.status = JobStatus.ERROR


def _requires_choice(error: dict) -> bool:
    question = error.get("ask_user")
    return (isinstance(question, dict) and question.get("kind") == "choice"
            and question.get("required", True))


def _checked_artifact(job: ScrapeJob, hit: dict, result: dict) -> tuple[str, dict | None]:
    """Only publish nonempty regular files as acquired artifacts."""
    url = result.get("url") or result.get("source_url") or hit.get("url")
    raw_path = result.get("artifact_path")
    if raw_path is None or raw_path == "":
        return "", _failure(job, "acquire", "not_found",
                            "acquire reported success without an artifact path", url=url)
    try:
        path = os.fsdecode(os.fspath(raw_path))
    except (TypeError, ValueError):
        return "", _failure(job, "acquire", "parse_failed",
                            "acquire returned an invalid artifact path", url=url)
    if not path:
        return "", _failure(job, "acquire", "not_found",
                            "acquire reported success without an artifact path", url=url)
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "", _failure(job, "acquire", "not_found",
                            f"acquire reported an artifact that does not exist: {path}", url=url)
    except ValueError as exc:
        return "", _failure(job, "acquire", "parse_failed",
                            f"cannot inspect acquired artifact {path!r}: {exc}", url=url)
    except OSError as exc:
        return "", _failure(job, "acquire", "internal_error",
                            f"cannot inspect acquired artifact {path!r}: {exc}", url=url)
    if not stat.S_ISREG(info.st_mode):
        return "", _failure(job, "acquire", "parse_failed",
                            f"acquired artifact is not a regular file: {path}", url=url)
    if info.st_size <= 0:
        return "", _failure(job, "acquire", "parse_failed",
                            f"acquired artifact is empty: {path}", url=url)
    return path, None


def _classify_artifact(job: ScrapeJob, hit: dict, result: dict,
                       path: str) -> tuple[dict | None, dict | None]:
    """Classify legacy envelopes and verify declared CAD before publishing them."""
    url = result.get("url") or result.get("source_url") or hit.get("url")
    selected_member = hit.get("member") or hit.get("archive_member")
    acquired_member = result.get("member") or result.get("archive_member")
    if (result.get("artifact_kind") not in ("archive", "ecad") and
            selected_member and acquired_member and selected_member != acquired_member):
        return None, _failure(job, "acquire", "parse_failed",
                              "acquired archive member does not match the selected member",
                              url=url)
    try:
        from services.browser.adapters import model_selection_question
        from services.browser.cad_validate import classify_artifact

        kind = result.get("artifact_kind") or None
        requested_format = result.get("format")
        if not isinstance(requested_format, str) or requested_format.lower() == "zip":
            requested_format = None
        requested_member = None
        if os.path.splitext(path)[1].lower() == ".zip" and kind not in ("archive", "ecad"):
            requested_member = acquired_member or selected_member
        classified = classify_artifact(path, kind=kind, requested_format=requested_format,
                                       requested_member=requested_member)
    except Exception as exc:  # noqa: BLE001
        return None, _exception_failure(job, "acquire", exc, url=url)
    if not isinstance(classified, dict):
        return None, _failure(job, "acquire", "parse_failed",
                              "artifact classifier did not return an object", url=url)
    candidates = classified.get("candidates")
    if candidates and kind not in ("archive", "ecad"):
        error = _failure(job, "acquire", "unsupported",
                         classified.get("reason") or "Select a model from the archive",
                         url=url)
        error["candidates"] = candidates
        error["selection_field"] = "archive_member"
        error["ask_user"] = model_selection_question(job.site, url or "", candidates)
        error["artifact_path"] = path
        error["hit"] = dict(hit)
        return None, error
    if not classified.get("ok"):
        detail = classified.get("reason") or "unknown validation failure"
        error = _failure(job, "acquire", "parse_failed",
                         f"acquired artifact failed classification: {detail}", url=url)
        error["validation_reason"] = detail
        for key, value in classified.items():
            if key not in ("ok", "reason", "artifact_kind", "format", "cad_validated"):
                error[key] = value
        return None, error
    claimed_kind = result.get("artifact_kind")
    actual_kind = classified.get("artifact_kind")
    if actual_kind not in ("cad", "document", "ecad", "archive"):
        return None, _failure(job, "acquire", "parse_failed",
                              "artifact classifier returned an unknown kind", url=url)
    if claimed_kind and claimed_kind != actual_kind:
        return None, _failure(job, "acquire", "parse_failed",
                              f"artifact kind {claimed_kind!r} does not match "
                              f"detected {actual_kind!r}", url=url)
    if actual_kind == "cad":
        validated = classified.get("cad_validated")
        if not isinstance(validated, bool):
            return None, _failure(job, "acquire", "parse_failed",
                                  "artifact classifier omitted CAD validation state", url=url)
        if not validated and (claimed_kind != "cad" or not result.get("format")):
            return None, _failure(job, "acquire", "unsupported",
                                  "unvalidated CAD requires an explicitly declared "
                                  "kind and format", url=url)

    claimed = result.get("format")
    actual = classified.get("format")
    if claimed and actual:
        if not isinstance(claimed, str) or not isinstance(actual, str):
            return None, _failure(job, "acquire", "parse_failed",
                                  "artifact format is not a string", url=url)
        aliases = {"stp": "step", "igs": "iges"}
        claimed_format = aliases.get(claimed.lower(), claimed.lower())
        actual_format = aliases.get(actual.lower(), actual.lower())
        if (claimed_format != actual_format and
                not (claimed_format == "zip" and classified.get("member"))):
            return None, _failure(job, "acquire", "parse_failed",
                                  f"artifact format {claimed!r} does not match "
                                  f"detected {actual!r}", url=url)
    return classified, None


def _run_job(job: ScrapeJob) -> None:
    """Execute a single job synchronously (runs on the worker thread)."""
    try:
        from services.browser.adapters import get_adapter
    except Exception as exc:  # noqa: BLE001
        _set_error(job, _exception_failure(job, "queue", exc))
        return

    adapter = get_adapter(job.site)
    if adapter is None:
        _set_error(job, _failure(job, "queue", "unsupported",
                                 f"no adapter registered for site '{job.site}'"))
        return

    # A pre-selected hit from the GUI bypasses search but otherwise follows the
    # same handling as an adapter result.
    if job.preset is not None:
        hits = [job.preset]
    else:
        try:
            hits = adapter.search(job.query)
        except Exception as exc:  # noqa: BLE001
            _set_error(job, _exception_failure(job, "search", exc))
            return

    if not isinstance(hits, list):
        _set_error(job, _failure(job, "search", "parse_failed",
                                 "adapter search did not return a list of results"))
        return
    if not hits:
        _set_error(job, _failure(job, "search", "not_found",
                                 "search returned no results"))
        return

    # Preserve notices even when a search also found usable results.  Normalize
    # legacy failures without discarding their status, retry, question, or extras.
    for hit in hits:
        if not isinstance(hit, dict):
            job.search_hits.append(_failure(job, "search", "parse_failed",
                                             "adapter returned a non-object search result"))
        elif not hit.get("ok", True):
            job.search_hits.append(_source_failure(job, "search", hit))
        else:
            job.search_hits.append(hit)
    for notice in job.search_hits:
        if not notice.get("ok", True) and _requires_choice(notice):
            _set_error(job, notice)
            return


    for hit in job.search_hits:
        if not hit.get("ok", True):
            continue
        try:
            result = adapter.acquire(hit)
        except Exception as exc:  # noqa: BLE001
            job.acquire_errors.append(_exception_failure(job, "acquire", exc,
                                                          url=hit.get("url")))
            continue
        if not isinstance(result, dict):
            job.acquire_errors.append(_failure(job, "acquire", "parse_failed",
                                               "adapter acquire did not return an object",
                                               url=hit.get("url")))
            continue
        if not result.get("ok"):
            error = _source_failure(job, "acquire", result, url=hit.get("url"))
            if _requires_choice(error):
                error["hit"] = dict(hit)
                job.acquire_errors.append(error)
                _set_error(job, error)
                return
            job.acquire_errors.append(error)
            continue
        path, error = _checked_artifact(job, hit, result)
        classified = None
        if error is None:
            classified, error = _classify_artifact(job, hit, result, path)
        if error is not None:
            for key in ("status", "retry_after_s", "ask_user", "artifact_kind", "format"):
                if key in result and (key != "ask_user" or not error.get("ask_user")):
                    error[key] = result[key]
            job.acquire_errors.append(error)
            if _requires_choice(error):
                _set_error(job, error)
                return
            continue
        # Publish provenance and the path before DONE.  In particular, retain
        # artifact_kind/format so a document is not mistaken for CAD downstream.
        job.spec_data = {k: v for k, v in result.items()
                         if k != "ok" and v is not None}
        job.spec_data["artifact_path"] = path
        for key in ("artifact_kind", "format"):
            if not job.spec_data.get(key) and key in classified:
                job.spec_data[key] = classified[key]
        job.spec_data["cad_validated"] = (
            classified["artifact_kind"] == "cad" and classified["cad_validated"])
        for key in ("member", "candidates"):
            if classified.get(key):
                job.spec_data[key] = classified[key]
        if classified.get("reason"):
            job.spec_data["classification_reason"] = classified["reason"]
        job.spec_data.setdefault("site", job.site)
        job.artifact_path = path
        job.error = None
        job.status = JobStatus.DONE
        return

    # A notice-only search still has a concrete error.  If acquisition was
    # attempted, report its last failure and retain every attempt separately.
    error = job.acquire_errors[-1] if job.acquire_errors else job.search_hits[0]
    _set_error(job, error)


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
            _set_error(job, _exception_failure(job, "queue", exc))
        finally:
            with _lock:
                if job.status == JobStatus.RUNNING:
                    _set_error(job, _failure(job, "queue", "internal_error",
                                             "job left in running state unexpectedly"))
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

"""Nonblocking services-worker RPCs for the shared fitting CFD pipeline.

Heavy native work is confined to sim.jobs children (see fitting_cfd). The worker
keeps orchestration threads; status is an in-memory snapshot, never a solver call.
MCP background jobs are polled through the same worker, so they survive agent turns.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from contextvars import copy_context

from services.agent.request_context import owner_key
from services.piping import fitting_cfd

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_THREADS: dict[str, threading.Thread] = {}


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_LOCK):
        for job_id, thread in list(_THREADS.items()):
            if thread.ident is not None and not thread.is_alive():
                _THREADS.pop(job_id)
        owner = owner_key()
        return {"by_kind": {"piping_cfd": sum(
            (entry["state"] == "running" or job_id in _THREADS)
            and entry.get("_owner") == owner for job_id, entry in _JOBS.items())}}


def _snapshot(job_id: str) -> dict:
    with _LOCK:
        entry = _JOBS.get(job_id)
        if entry is None:
            return {"ok": False, "reason": "unknown_job", "hint": "CFD-K job not found in this worker", "job_id": job_id}
        return copy.deepcopy({k: v for k, v in entry.items()
                              if k not in {"cancel_event", "_owner"}})


def _run(job_id: str, params: dict, event: threading.Event) -> None:
    def progress(pct, stage):
        with _LOCK:
            _JOBS[job_id].update(pct=pct, stage=stage)
    try:
        result = fitting_cfd.compute(**{k: params[k] for k in (
            "fitting", "schedule", "fluid", "reynolds", "quality", "geometry") if k in params},
            cancel_event=event, progress=progress)
        context = params.get("apply_context")
        if result.get("ok") and context and not event.is_set():
            # Persistence stays in the live daemon, not in an isolated solver child.
            from services.mcp import piping_tools as tools
            progress(100, "Applying fitting loss curve")
            base = context["piping"]
            if context.get("doc_id"):
                saved = tools._find_study(context["doc_id"], tools._NETWORK_STUDY, context["network"])
                if saved and isinstance((saved.get("params") or {}).get("piping"), dict):
                    base = saved["params"]["piping"]
            edited = fitting_cfd.apply_model(base, context["fitting_id"],
                                            result["k_model"], tools._rpc)
            if edited.get("ok"):
                result.update({"applied": True, "piping": edited["piping"],
                               "created": edited.get("created", []), "removed": edited.get("removed", []),
                               "changed": edited.get("changed", []), "network": context["network"]})
                if context.get("doc_id"):
                    result.update(tools._persist_network(context["doc_id"], context["network"], edited["piping"]))
            else:
                result.update({"ok": False, "applied": False, "reason": edited.get("reason"),
                               "hint": edited.get("hint"), "edit": edited})
        state = "cancelled" if result.get("reason") == "cancelled" or (event.is_set() and not result.get("applied")) else (
            "done" if result.get("ok") else "failed")
        with _LOCK:
            _JOBS[job_id].update(state=state, stage=state, result=result, ok=True,
                                  completed_at=time.time())
            if state == "done":
                _JOBS[job_id]["pct"] = 100
            if not result.get("ok"):
                _JOBS[job_id].update(reason=result.get("reason"), hint=result.get("hint"))
    except Exception as exc:
        with _LOCK:
            _JOBS[job_id].update(state="failed", stage="failed", reason="cfd_job_failed", hint=str(exc))


def start(params: dict, _emit_event=None) -> dict:
    refusal = fitting_cfd.validate_request(params.get("fitting"), params.get("quality", "quick"),
                                           params.get("geometry"), params.get("reynolds"))
    if refusal:
        return refusal
    job_id = "fitting-k-" + uuid.uuid4().hex[:16]
    event = threading.Event()
    with _LOCK:
        _JOBS[job_id] = {"ok": True, "job_id": job_id, "state": "running", "pct": 0,
                         "stage": "Preparing fitting CFD", "started_at": time.time(),
                         "cancel_event": event, "_owner": owner_key()}
    context = copy_context()
    thread = threading.Thread(target=context.run,
                              args=(_run, job_id, copy.deepcopy(params), event),
                              name=job_id, daemon=True)
    with _LOCK:
        for finished_id, finished in list(_THREADS.items()):
            if finished.ident is not None and not finished.is_alive():
                _THREADS.pop(finished_id)
        _THREADS[job_id] = thread
    try:
        thread.start()
    except BaseException:
        with _LOCK:
            _THREADS.pop(job_id, None)
            _JOBS[job_id].update(state="failed", stage="failed", reason="cfd_job_failed")
        raise
    return {"ok": True, "job_id": job_id}


def status(params: dict, _emit_event=None) -> dict:
    return _snapshot(str(params.get("job_id", "")))


def cancel(params: dict, _emit_event=None) -> dict:
    job_id = str(params.get("job_id", ""))
    with _LOCK:
        entry = _JOBS.get(job_id)
        if entry is not None and entry["state"] == "running":
            entry["cancel_event"].set()
            entry["stage"] = "Cancelling CFD solver"
    return _snapshot(job_id)


WORKER_METHODS = {
    "piping_fitting_k_cfd_start": start,
    "piping_fitting_k_cfd_status": status,
    "piping_fitting_k_cfd_cancel": cancel,
}

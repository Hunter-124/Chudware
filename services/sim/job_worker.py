"""Child process that runs ONE simulation job stage. ``python -m services.sim.job_worker <id>``.

Contract with :mod:`services.sim.jobs` (the parent-side registry):

* reads ``<spool>/<job_id>/payload.json`` -> ``{stage, kind, payload}``
* runs the stage on a daemon THREAD while the main thread waits on a signal
* streams ``<spool>/<job_id>/partial.json`` — every snapshot carries ``"partial": true``,
  so a reader can never mistake it for a converged answer
* writes ``<spool>/<job_id>/result.json`` ATOMICALLY (tmp + os.replace) exactly once

The stage runs on a thread, not the main thread, on purpose: an FEA solve is one ctypes
call into ``libchudware`` and a Python signal handler cannot run while the interpreter is
inside it. With the solve on a side thread the main thread is always free to take SIGTERM,
flush the last partial snapshot, and ``os._exit`` — which is what makes ``sim_cancel``
terminate the solver promptly instead of waiting out the solve it is trying to abort.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from services.sim import jobs as J


def _atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: python -m services.sim.job_worker <job_id>", file=sys.stderr)
        return 2
    job_id = argv[1]
    d = J.job_dir(job_id)
    spec = json.loads((d / "payload.json").read_text(encoding="utf-8"))
    stage_name = spec["stage"]
    payload = spec.get("payload") or {}
    fn = J.STAGES.get(stage_name)
    if fn is None:
        _atomic_write(d / "result.json",
                      {"error": f"unknown stage {stage_name!r}", "done_at": time.time()})
        return 2

    partial_path = d / "partial.json"
    t0 = time.time()
    latest: dict = {}
    lock = threading.Lock()

    def emit(snapshot: dict) -> None:
        """Publish a labelled, finite partial snapshot."""
        if not isinstance(snapshot, dict):
            raise ValueError("partial snapshot must be an object")
        with lock:
            latest.update(snapshot)
            body = dict(latest)
            body.update(partial=True, converged=False, job_id=job_id,
                        elapsed_s=round(time.time() - t0, 3))
            try:
                _atomic_write(partial_path, body)
            except (OSError, TypeError, ValueError):
                pass

    # Solvers that publish live progress to a file of their own (the LBM CFD path writes
    # {pct, step, n_steps}) get it folded into the same partial channel.
    prog_file = payload.get("progress_file")
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(0.5):
            try:
                snapshot = json.loads(
                    Path(prog_file).read_text(encoding="utf-8"),
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON constant {token}")))
                if isinstance(snapshot, dict):
                    emit(snapshot)
            except (OSError, TypeError, ValueError):
                continue

    if prog_file:
        threading.Thread(target=_watch, name="progress-watch", daemon=True).start()

    outcome: dict = {}
    finished = threading.Event()

    def _run() -> None:
        try:
            result = fn(payload, emit)
            if not isinstance(result, dict):
                raise TypeError("simulation stage must return a result object")
            json.dumps(result, allow_nan=False)
            outcome["result"] = result
            if result.get("ok") is not True:
                outcome["error"] = str(
                    result.get("error") or result.get("reason") or result.get("hint") or
                    "simulation stage did not confirm ok=true")
        except BaseException as exc:  # noqa: BLE001 — child must report every failure
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["traceback"] = traceback.format_exc()[-2000:]
        finally:
            finished.set()

    def _on_term(signum, _frame):  # noqa: ANN001
        stop.set()
        emit({"terminated_by": signal.Signals(signum).name,
              "note": "worker was cancelled; this snapshot is the last state reached"})
        os._exit(128 + int(signum))  # kill the stuck ctypes solve thread with us

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    threading.Thread(target=_run, name=f"sim-stage-{stage_name}", daemon=True).start()
    while not finished.wait(0.25):
        pass
    stop.set()

    outcome["done_at"] = time.time()
    _atomic_write(d / "result.json", outcome)
    return 1 if outcome.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

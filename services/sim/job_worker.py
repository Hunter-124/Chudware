"""Child process that runs ONE simulation job stage. ``python -m services.sim.job_worker <id>``.

Contract with :mod:`services.sim.jobs` (the parent-side registry):

* reads ``<spool>/<job_id>/payload.json`` -> ``{stage, kind, payload}``
* runs the stage on a daemon THREAD while the main thread waits on a signal
* streams ``<spool>/<job_id>/partial.json`` — every snapshot carries ``"partial": true``,
  so a reader can never mistake it for a converged answer
* writes ``<spool>/<job_id>/result.json`` ATOMICALLY (tmp + os.replace) exactly once
* logs each phase snapshot as one JSON line and heartbeats the last real state every
  30 seconds, even while a native fill/solve has released the GIL

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
from services.agent import request_context as _rc


def _atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


_HEARTBEAT_S = 30.0

#: Process CPU seconds (all threads, native ones included) that count as "the solver is
#: working". A native mesh sub-phase can run for minutes without moving one counter
#: (exterior_edge_projection: 150-166 s on a 58k-element chassis solid), while the main
#: thread's own heartbeats cost milliseconds per 30 s, so one CPU-second separates a busy
#: solver from a blocked one.
_CPU_ACTIVE_S = 1.0

#: How many completed mesh fills keep their element count in the progress record.
_FILL_HISTORY = 8


class _Progress:
    """Serialize phase events and heartbeat snapshots through one atomic publisher."""

    def __init__(self, directory: Path, job_id: str, *, clock=None, cpu_clock=None):
        self.path = directory / "partial.json"
        self.job_id = job_id
        self.clock = clock or J._clock
        self.cpu_clock = cpu_clock or time.process_time
        self.started = self.clock()
        self.last_write = self.started
        self.last_advance = self.started
        self.cpu_mark = self.cpu_clock()
        self.last_cpu_active = self.started
        # The mesher fills one lattice per probe, per recovery rung and per separate
        # solid, and each fill restarts its own sub-phase passes and element count. Count
        # them so the record says WHICH fill it describes instead of appearing to go
        # backwards.
        self.fills_done = 0
        self.fill_elements: list = []
        self.awaiting_fill = False
        self.latest: dict = {"phase": "starting", "elements": None}
        # SIGTERM may interrupt the main thread during its heartbeat publication.
        self.lock = threading.RLock()

    def emit(self, snapshot: dict) -> None:
        if not isinstance(snapshot, dict):
            raise ValueError("partial snapshot must be an object")
        with self.lock:
            previous = self._counters()
            was_complete = str(self.latest.get("phase") or "").endswith("_complete")
            self.latest.update((key, value) for key, value in snapshot.items()
                               if key != "elements" or value is not None)
            if isinstance(snapshot.get("probe"), dict):
                probe = dict(snapshot["probe"])
                self.latest["probe"] = probe
                if snapshot.get("elements") is None and probe.get("elements_so_far") is not None:
                    self.latest["elements"] = probe["elements_so_far"]
            if "stage" in snapshot and "phase" not in snapshot:
                self.latest["phase"] = snapshot["stage"]
            phase = str(self.latest.get("phase") or "")
            if (phase.startswith("mesh") and phase.endswith("_complete")
                    and not was_complete and "phase" in snapshot):
                self.fills_done += 1
                self.fill_elements = (self.fill_elements
                                      + [self.latest.get("elements")])[-_FILL_HISTORY:]
                self.awaiting_fill = True
            elif (isinstance(snapshot.get("probe"), dict)
                    and snapshot["probe"].get("sub_phase") != "complete"):
                # The native event carries the LAST fill's probe until the next fill
                # reports, so only a live sub-phase marks the new fill as started.
                self.awaiting_fill = False
            if self.fills_done or isinstance(self.latest.get("probe"), dict):
                self.latest["fill"] = self._fill_record(phase)
                self.latest["summary"] = self._summary(phase)
            now = self.clock()
            if self._counters() != previous:
                self.last_advance = now
            cpu = self.cpu_clock()
            if cpu - self.cpu_mark >= _CPU_ACTIVE_S:
                self.cpu_mark = cpu
                self.last_cpu_active = now
            body = dict(self.latest)
            body.update(partial=True, converged=False, job_id=self.job_id,
                        elapsed_s=round(now - self.started, 3),
                        _last_advance_monotonic=self.last_advance,
                        last_advance_s=round(now - self.last_advance, 3),
                        cpu_s=round(cpu, 1),
                        _last_cpu_active_monotonic=self.last_cpu_active,
                        last_cpu_active_s=round(now - self.last_cpu_active, 3))
            _atomic_write(self.path, body)
            print(json.dumps(body, allow_nan=False), flush=True)
            self.last_write = now

    def _fill_record(self, phase: str) -> dict:
        complete = phase.endswith("_complete")
        return {"index": self.fills_done if complete else self.fills_done + 1,
                "completed": self.fills_done,
                "completed_elements": list(self.fill_elements)}

    def _summary(self, phase: str) -> str:
        """One line that reads monotonically: which solid, which fill, which pass."""
        parts = []
        solid = self.latest.get("body")
        if isinstance(solid, dict) and solid.get("index") and solid.get("total"):
            parts.append(f"solid {solid['index']}/{solid['total']}")
        fill = self._fill_record(phase)
        if phase.endswith("_complete"):
            parts.append(f"mesh fill {fill['index']} complete")
        else:
            parts.append(f"mesh fill {fill['index']} ({fill['completed']} completed)")
        probe = self.latest.get("probe") or {}
        if phase.endswith("_complete"):
            pass
        elif self.awaiting_fill:
            parts.append(f"{phase}: starting")
        elif probe.get("sub_phase"):
            sub = probe["sub_phase"]
            parts.append(f"{phase}: " + ("fill done, finishing" if sub == "complete" else sub))
        if self.latest.get("elements") is not None and not self.awaiting_fill:
            parts.append(f"{self.latest['elements']} elements in this fill")
        return ("; ".join(parts) + ". Pass counters and element counts restart with each "
                "fill (probe sizes, recovery retries, separate solids); the job is finished "
                "only when its state leaves running.")

    def _counters(self) -> tuple:
        # Publication time, elapsed time and notes are not work. Native fill
        # counters can reset at a new h/sub-phase; those transitions are work too.
        probe = self.latest.get("probe") or {}
        solid = self.latest.get("body") or {}
        return (
            *(self.latest.get(key) for key in (
                "phase", "elements", "step", "n_steps", "pct", "iteration",
                "iterations", "legs_done", "current_leg", "cells_done",
                "cells_total", "work_done", "work_total")),
            *(probe.get(key) for key in (
                "h_mm", "cells_done", "cells_total", "elements_so_far", "sub_phase")),
            solid.get("index") if isinstance(solid, dict) else None,
        )

    def heartbeat(self) -> None:
        with self.lock:
            if self.clock() - self.last_write >= _HEARTBEAT_S:
                self.emit({})


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: python -m services.sim.job_worker <job_id>", file=sys.stderr)
        return 2
    job_id = argv[1]
    if len(argv) > 2:
        if len(argv) != 4 or argv[2] != "--job-dir":
            raise ValueError("cell_refused")
        d = Path(argv[3]).resolve()
        base = Path(os.environ.get("XDG_DATA_HOME", "/actor/data")).resolve()
        if base not in d.parents:
            raise ValueError("cell_refused")
        spec = json.loads((d / "payload.json").read_text(encoding="utf-8"))
        roots = spec.get("roots") or {}
        caller = _rc.owner_key() if _rc.current_context().get("cell") else None
        with _rc.bind_context(spec.get("ctx")), _rc.bind_roots(
                roots.get("project", "/work"), roots.get("save", "/work")):
            if caller is not None and _rc.owner_key() != caller:
                raise KeyError("not_found")
            if not _rc.in_cell() or J.job_dir(job_id).resolve() != d:
                raise KeyError("not_found")
            return _main(job_id, d, spec)
    if _rc.in_cell():
        raise ValueError("cell_refused")
    d = J.job_dir(job_id)
    spec = json.loads((d / "payload.json").read_text(encoding="utf-8"))
    return _main(job_id, d, spec)


def _main(job_id: str, d: Path, spec: dict) -> int:
    ctx = _rc.current_context()
    project = _rc.root_value("CHUDWARE_PROJECT_DIR")
    save = _rc.root_value("CHUDWARE_PROJECT_SAVE_DIR")

    def _bound(fn):
        def invoke():
            with _rc.bind_context(ctx), _rc.bind_roots(project, save):
                fn()
        return invoke

    stage_name = spec["stage"]
    payload = spec.get("payload") or {}
    fn = J.STAGES.get(stage_name)
    if fn is None:
        _atomic_write(d / "result.json",
                      {"error": f"unknown stage {stage_name!r}", "done_at": time.time()})
        return 2

    progress = _Progress(d, job_id)
    emit = progress.emit
    emit({})

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
        threading.Thread(target=_bound(_watch), name="progress-watch", daemon=True).start()

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

    threading.Thread(target=_bound(_run), name=f"sim-stage-{stage_name}", daemon=True).start()
    while not finished.wait(0.25):
        progress.heartbeat()
    stop.set()
    emit({"state": "failed" if outcome.get("error") else "done"})

    outcome["done_at"] = time.time()
    _atomic_write(d / "result.json", outcome)
    return 1 if outcome.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

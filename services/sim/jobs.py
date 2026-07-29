"""Out-of-process, cross-turn job registry for long simulation solves.

WHY THIS EXISTS
---------------
Every ``run_fea_*`` / ``run_cfd_*`` tool was synchronous. ``services/mcp/cad_common.py``
already had a *thread* job table (``_start_job`` / ``get_job``) that keeps a heavy solve
from tripping the MCP transport timeout, but it cannot do the three things an agent
actually needs on a multi-minute solve:

* **Cancel.** A Python thread cannot be killed, and the solve itself sits inside one
  ctypes call into ``libchudware``. A "cancel" that only flips a dict field would leave
  the solver burning a core — worse than no cancel at all.
* **Survive the turn.** Agent turns fork a fresh ``services.worker_main`` per turn, so an
  in-memory table means a job started in turn N is unreachable in turn N+1.
* **Partial results.** A thread job exposes nothing until it returns.

So a job here is a **child process**, and its bookkeeping lives in a small SQLite file
under the data root. Cancel is ``killpg`` + proof-of-death; state is readable from any
later process; the child streams a partial snapshot to disk while it runs.

The child runs with ``CHUDWARE_ENGINE_DAEMON=0`` (its own in-process ``libchudware``,
~0.1 s to load) *deliberately*: if the solve ran inside the shared ``chudware-hostd``
daemon, killing the child would leave the daemon still solving and the cancel would be a
lie. The child opens no documents — a solve payload is self-contained (an exported STEP
path plus a JSON study), so nothing is lost by not sharing the daemon's doc table.

STAGES
------
A job runs one named stage from :data:`STAGES` (an allowlist — a job payload can never
name an arbitrary import). Stages are atomic and JSON-in/JSON-out, which is what lets the
pipeline be split into *setup* (parent, needs the live doc) -> *solve* (child, pure) ->
*collect* (parent, shapes the result and persists the study).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- states

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (DONE, FAILED, CANCELLED)

# How long a finished job's row + spool directory are kept so a LATER turn can still
# collect it. Generous by default: the whole point is surviving turn boundaries.
_TTL_S = float(os.environ.get("CHUDWARE_SIM_JOB_TTL_S", "86400") or 86400)


def jobs_dir() -> Path:
    """Spool root: ``$CHUDWARE_SIM_JOBS_DIR`` or ``<appdata root>/sim_jobs``."""
    override = os.environ.get("CHUDWARE_SIM_JOBS_DIR")
    if override:
        d = Path(override)
    else:
        try:
            from services.paths import appdata_root  # noqa: PLC0415
            d = appdata_root() / "sim_jobs"
        except Exception:  # noqa: BLE001 — never let a path helper break job start
            import tempfile
            d = Path(tempfile.gettempdir()) / "Chudware" / "sim_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_dir(job_id: str) -> Path:
    return jobs_dir() / job_id


def _db_path() -> Path:
    return jobs_dir() / "jobs.sqlite"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs("
        " job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL,"
        " state TEXT NOT NULL, pid INTEGER, pgid INTEGER, proc_key TEXT,"
        " created_at REAL, started_at REAL, done_at REAL,"
        " cancel_requested INTEGER DEFAULT 0, error TEXT, meta TEXT)")
    return conn


def _set(job_id: str, **cols: Any) -> None:
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    with _db() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                     (*cols.values(), job_id))


def _row(job_id: str) -> Optional[sqlite3.Row]:
    with _db() as conn:
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return cur.fetchone()


# ---------------------------------------------------------------- process liveness
# A later turn is a DIFFERENT process, so it can never waitpid() the child: liveness has
# to come from the OS. Two traps are handled explicitly, because an honest cancel report
# depends on getting this right:
#   * ZOMBIE — the starting process is still alive and has not reaped the child, so
#     kill(pid, 0) succeeds on a corpse. /proc state 'Z' means dead for our purposes.
#   * PID REUSE — the pid may belong to something else entirely by now. We record the
#     kernel's per-process start ticks at launch and compare on every check.

def _proc_key(pid: int) -> Optional[str]:
    """Kernel start-time of `pid` (field 22 of /proc/pid/stat), the pid-reuse guard."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        # comm may contain spaces/parens — everything after the last ')' is fixed-width.
        tail = raw[raw.rindex(")") + 2:].split()
        return tail[19]  # field 22 overall = index 19 after state
    except (ValueError, IndexError):
        return None


def _proc_state(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[0]
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: Optional[int], proc_key: Optional[str] = None) -> bool:
    """True only if `pid` is a LIVE (non-zombie) process that is still the one we started."""
    if not pid:
        return False
    st = _proc_state(int(pid))
    if st is not None:
        if st == "Z":
            return False
        if proc_key and _proc_key(int(pid)) != proc_key:
            return False  # pid reuse — a different process now wears this number
        return True
    # No procfs (non-Linux): fall back to the signal probe. Cannot detect zombies.
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- stages

def _stage_core_rpc(payload: dict, emit) -> dict:
    """One C++ core RPC (``sim_run_static``, ``sim_run_modal``, ...) in this child.

    ``CHUDWARE_ENGINE_DAEMON=0`` is forced by the launcher so the solve really runs HERE
    and dies with this process.
    """
    from services.mcp import core_bridge as cb  # noqa: PLC0415
    method = payload.get("method")
    if not method:
        raise ValueError("core_rpc payload needs a 'method'")
    emit({"stage": method, "note": "engine RPC in flight"})
    return cb.call(str(method), payload.get("params") or {})


def _stage_selftest_sleep(payload: dict, emit) -> dict:
    """A metered sleep that publishes a partial snapshot each step.

    Not a toy: this is the only stage whose duration and progress are exactly known, so it
    is what the cancel-termination and partial-labelling contracts are *proved* against
    (a real solve's runtime is machine-dependent and its partial channel is solver-
    dependent). It also doubles as a liveness probe for the whole spool/kill path.
    """
    total = float(payload.get("seconds", 5.0))
    steps = max(1, int(payload.get("steps", 10)))
    dt = total / steps
    t0 = time.time()
    for i in range(steps):
        time.sleep(dt)
        emit({"steps_done": i + 1, "steps_total": steps,
              "pct": round(100.0 * (i + 1) / steps, 1),
              "elapsed_s": round(time.time() - t0, 3)})
    return {"ok": True, "slept_s": round(time.time() - t0, 3), "steps": steps}


STAGES: dict[str, Callable[[dict, Callable[[dict], None]], dict]] = {
    "core_rpc": _stage_core_rpc,
    "selftest_sleep": _stage_selftest_sleep,
}


# ---------------------------------------------------------------- start

def _prune(conn: sqlite3.Connection) -> None:
    """Drop rows + spool dirs of TERMINAL jobs older than the TTL. Running jobs never."""
    cutoff = time.time() - _TTL_S
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE state IN (?,?,?) AND done_at IS NOT NULL "
        "AND done_at < ?", (DONE, FAILED, CANCELLED, cutoff)).fetchall()
    for r in rows:
        shutil.rmtree(job_dir(r["job_id"]), ignore_errors=True)
        conn.execute("DELETE FROM jobs WHERE job_id=?", (r["job_id"],))


def start(kind: str, stage: str, payload: dict, meta: Optional[dict] = None) -> dict:
    """Spawn a child process for `stage` and return ``{job_id, state, pid, ...}``.

    Returns as soon as the child is forked — it does NOT wait for the stage to begin.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; known: {sorted(STAGES)}")
    job_id = "sim-" + uuid.uuid4().hex[:12]
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "payload.json").write_text(
        json.dumps({"stage": stage, "kind": kind, "payload": payload}), encoding="utf-8")

    now = time.time()
    with _db() as conn:
        _prune(conn)
        conn.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,created_at,started_at,meta)"
            " VALUES(?,?,?,?,?,?,?)",
            (job_id, kind, stage, QUEUED, now, now, json.dumps(meta or {})))

    env = dict(os.environ)
    # The whole reason the solve is out-of-process: it must die when we kill it. Routing
    # it through the shared daemon would leave the daemon solving after a "cancel".
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    log = open(d / "worker.log", "wb")  # noqa: SIM115 — handed to the child, closed below
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.sim.job_worker", job_id],
            cwd=str(repo_root), env=env, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True)  # own process GROUP: kill takes its children too
    finally:
        log.close()
    _set(job_id, state=RUNNING, pid=proc.pid, pgid=proc.pid,
         proc_key=_proc_key(proc.pid))
    _LOCAL_PROCS[job_id] = proc
    return {"job_id": job_id, "state": RUNNING, "pid": proc.pid, "kind": kind,
            "stage": stage, "started_at": now}


# Popen handles for jobs THIS process started, so we can reap them and never leave a
# zombie that would make pid_alive() lie about a killed worker.
_LOCAL_PROCS: dict[str, subprocess.Popen] = {}


# ---------------------------------------------------------------- read

def _read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


UNKNOWN_JOB_HELP = (
    "No such simulation job in the registry at {dir}. Jobs are PERSISTED across agent "
    "turns, so an id from an earlier turn normally still resolves; an id disappears only "
    "when it was never issued (typo / invented id), when it came from a different data "
    "root, or when it finished more than {ttl:.0f} h ago and was pruned. "
    "Call sim_jobs() to list what actually exists.")


def unknown(job_id: str) -> dict:
    return {"ok": False, "job_id": job_id, "state": "unknown",
            "error": UNKNOWN_JOB_HELP.format(dir=jobs_dir(), ttl=_TTL_S / 3600.0)}


def _reconcile(row: sqlite3.Row) -> dict:
    """Row + on-disk truth -> the CURRENT state. Never reports a verdict it didn't check."""
    job_id = row["job_id"]
    state = row["state"]
    d = job_dir(job_id)
    out: dict = {"job_id": job_id, "kind": row["kind"], "stage": row["stage"],
                 "state": state, "pid": row["pid"],
                 "started_at": row["started_at"], "done_at": row["done_at"],
                 "meta": _read_json_str(row["meta"])}
    if state in TERMINAL:
        if row["error"]:
            out["error"] = row["error"]
        return out

    fin = _read_json(d / "result.json")
    if fin is not None:
        # The child finished; adopt its verdict and make it durable.
        if fin.get("error") is not None:
            state, err = FAILED, str(fin["error"])
        else:
            state, err = DONE, None
        done_at = float(fin.get("done_at") or time.time())
        _set(job_id, state=state, error=err, done_at=done_at)
        out.update(state=state, done_at=done_at)
        if err:
            out["error"] = err
        _reap(job_id)
        return out

    if not pid_alive(row["pid"], row["proc_key"]):
        # No result file and no process: the worker died. Say exactly that — do NOT
        # report "done", and do NOT report "running" for a corpse.
        if row["cancel_requested"]:
            state, err = CANCELLED, "cancelled by sim_cancel"
        else:
            state = FAILED
            tail = _log_tail(job_id)
            err = ("the solver process exited without writing a result"
                   + (f"; last output: {tail}" if tail else ""))
        _set(job_id, state=state, error=err, done_at=time.time())
        out.update(state=state, error=err, done_at=time.time())
        _reap(job_id)
        return out

    out["state"] = RUNNING
    return out


def _read_json_str(s) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _log_tail(job_id: str, limit: int = 400) -> str:
    try:
        txt = (job_dir(job_id) / "worker.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return txt.strip()[-limit:]


def _reap(job_id: str) -> None:
    proc = _LOCAL_PROCS.pop(job_id, None)
    if proc is not None:
        try:
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass


def status(job_id: str) -> dict:
    """Current state of a job, reconciled against the OS and the spool directory."""
    row = _row(job_id)
    if row is None:
        return unknown(job_id)
    st = _reconcile(row)
    started = st.get("started_at") or time.time()
    end = st.get("done_at") if st["state"] in TERMINAL and st.get("done_at") else time.time()
    st["elapsed_s"] = round(float(end) - float(started), 3)
    prog = _read_json(job_dir(job_id) / "partial.json")
    if prog:
        st["progress"] = {k: v for k, v in prog.items() if k != "partial"}
    st["ok"] = st["state"] != FAILED
    return st


def partial(job_id: str) -> Optional[dict]:
    """The child's latest partial snapshot, or None when the stage published none."""
    return _read_json(job_dir(job_id) / "partial.json")


def raw_result(job_id: str) -> Optional[dict]:
    """The stage's return value for a finished job, else None."""
    fin = _read_json(job_dir(job_id) / "result.json")
    if fin is None or fin.get("error") is not None:
        return None
    return fin.get("result")


def list_jobs(limit: int = 20) -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [status(r["job_id"]) for r in rows]


# ---------------------------------------------------------------- cancel

def cancel(job_id: str, timeout_s: float = 5.0) -> dict:
    """Terminate a job's process GROUP and verify it is gone before saying `cancelled`.

    SIGTERM first (the worker flushes its partial snapshot and exits), SIGKILL if it is
    still there. The reply carries the evidence — `pid`, `signals`, `pid_alive_after` —
    because "cancelled" is worthless without proof the solver actually stopped.
    """
    row = _row(job_id)
    if row is None:
        return unknown(job_id)
    st = _reconcile(row)
    if st["state"] in TERMINAL:
        return {"ok": True, "job_id": job_id, "state": st["state"], "terminated": False,
                "note": f"job already finished as {st['state']} — nothing to cancel"}

    pid, pgid = row["pid"], row["pgid"] or row["pid"]
    key = row["proc_key"]
    _set(job_id, cancel_requested=1)
    signals: list = []

    def _alive() -> bool:
        return pid_alive(pid, key)

    for sig, grace in ((signal.SIGTERM, min(3.0, timeout_s)), (signal.SIGKILL, timeout_s)):
        if not _alive():
            break
        try:
            os.killpg(int(pgid), sig)
        except ProcessLookupError:
            break
        except OSError:
            try:
                os.kill(int(pid), sig)
            except OSError:
                break
        signals.append(sig.name)
        deadline = time.time() + max(0.2, grace)
        while time.time() < deadline:
            _reap_nb(job_id)
            if not _alive():
                break
            time.sleep(0.05)

    _reap_nb(job_id)
    still = _alive()
    now = time.time()
    state = RUNNING if still else CANCELLED
    err = None if still else "cancelled by sim_cancel"
    _set(job_id, state=state, error=err, done_at=None if still else now)
    out = {"ok": not still, "job_id": job_id, "state": state, "pid": pid,
           "signals": signals, "pid_alive_after": still, "terminated": not still,
           "group_alive_after": _group_alive(pgid)}
    if still:
        out["error"] = (f"pid {pid} is STILL ALIVE after {signals or ['no signal']} — the "
                        "solver was not stopped; do not treat this job as cancelled")
    return out


def _reap_nb(job_id: str) -> None:
    proc = _LOCAL_PROCS.get(job_id)
    if proc is not None and proc.poll() is not None:
        _LOCAL_PROCS.pop(job_id, None)


def _group_alive(pgid) -> bool:
    """True if ANY process still sits in the job's process group (catches solver children
    such as an OpenFOAM subprocess that outlived its Python parent)."""
    if not pgid:
        return False
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                if os.getpgid(int(entry)) == int(pgid) and _proc_state(int(entry)) != "Z":
                    return True
            except (ProcessLookupError, PermissionError, OSError):
                continue
    except OSError:
        return False
    return False

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
import math
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


def _json_text(value: Any, label: str) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON data: {exc}") from None


def _validate_core_rpc_payload(payload: dict) -> None:
    method = payload.get("method")
    if method not in ("sim_run_static", "sim_run_modal"):
        raise ValueError("core_rpc method must be sim_run_static or sim_run_modal")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("core_rpc payload needs a params object")
    study = params.get("study")
    if not isinstance(study, dict):
        raise ValueError(f"{method} params need a study object")
    step_path = study.get("step_path")
    if not isinstance(step_path, str) or not step_path.strip():
        raise ValueError(f"{method} study needs a STEP geometry path")
    geometry = Path(step_path)
    try:
        geometry_ready = geometry.is_file() and geometry.stat().st_size > 0
    except OSError:
        geometry_ready = False
    if not geometry_ready:
        raise ValueError(f"{method} STEP geometry is unavailable: {step_path}")
    material = study.get("material")
    if not isinstance(material, dict) or not material:
        raise ValueError(f"{method} study needs a material object")
    required_material = ("E_mpa", "nu", "yield_mpa") if method == "sim_run_static" \
        else ("E_mpa", "nu", "density_g_cm3")
    for key in required_material:
        try:
            value = float(material[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(f"{method} material needs finite {key}") from None
        if not math.isfinite(value):
            raise ValueError(f"{method} material needs finite {key}")
        if key != "nu" and value <= 0.0:
            raise ValueError(f"{method} material {key} must be > 0")
        if key == "nu" and not -1.0 < value < 0.5:
            raise ValueError(f"{method} material nu must be between -1 and 0.5")
    fixtures = study.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError(f"{method} study fixtures must be a list")
    if method == "sim_run_static":
        if not fixtures:
            raise ValueError("sim_run_static needs at least one fixture")
        loads = study.get("loads")
        if not isinstance(loads, list) or not loads:
            raise ValueError("sim_run_static needs at least one non-zero load")
        driven = False
        for load in loads:
            if not isinstance(load, dict) or not load.get("faces"):
                raise ValueError("every static load needs resolved faces")
            if load.get("type") == "pressure":
                magnitude = float(load.get("magnitude", 0.0))
                driven = driven or math.isfinite(magnitude) and magnitude != 0.0
            elif load.get("type") == "force":
                vector = load.get("vector")
                if not isinstance(vector, list) or len(vector) != 3:
                    raise ValueError("static force load needs a three-component vector")
                values = [float(value) for value in vector]
                driven = driven or all(math.isfinite(value) for value in values) \
                    and any(value != 0.0 for value in values)
            else:
                raise ValueError("static load type must be pressure or force")
        if not driven:
            raise ValueError("sim_run_static needs at least one non-zero load")
    else:
        n_modes = params.get("n_modes")
        if isinstance(n_modes, bool):
            raise ValueError("sim_run_modal n_modes must be a positive integer")
        try:
            parsed_modes = int(n_modes)
            exact_modes = float(n_modes)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("sim_run_modal n_modes must be a positive integer") from None
        if parsed_modes <= 0 or not math.isfinite(exact_modes) or exact_modes != parsed_modes:
            raise ValueError("sim_run_modal n_modes must be a positive integer")

    from services.mcp import core_bridge as cb  # noqa: PLC0415
    if not cb.available():
        raise ValueError("core simulation backend is unavailable in the job worker process")
    try:
        probe = cb.call(method, {})
    except Exception as exc:
        raise ValueError(f"core simulation backend cannot dispatch {method}: {exc}") from None
    probe_error = str(probe.get("error") or probe.get("message") or "").lower()
    if (probe.get("ok") is False
            and any(token in probe_error for token in
                    ("unknown method", "solver unavailable", "not compiled"))):
        raise ValueError(f"core simulation backend cannot dispatch {method}: {probe_error}")


def _validate_start(kind: str, stage: str, payload: dict, meta: Optional[dict]) -> tuple[str, str]:
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("job kind must be a non-empty string")
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; known: {sorted(STAGES)}")
    if not isinstance(payload, dict):
        raise ValueError("job payload must be an object")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("job metadata must be an object")
    if stage == "core_rpc":
        _validate_core_rpc_payload(payload)
    elif stage == "selftest_sleep":
        seconds = float(payload.get("seconds", 5.0))
        steps = payload.get("steps", 10)
        if not math.isfinite(seconds) or seconds <= 0.0:
            raise ValueError("selftest_sleep seconds must be finite and > 0")
        if isinstance(steps, bool):
            raise ValueError("selftest_sleep steps must be a positive integer")
        parsed_steps = int(steps)
        if parsed_steps <= 0 or float(steps) != parsed_steps:
            raise ValueError("selftest_sleep steps must be a positive integer")
    return (_json_text({"stage": stage, "kind": kind, "payload": payload}, "job payload"),
            _json_text(meta or {}, "job metadata"))


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
    """Validate setup, spawn one stage process, and return its durable job id."""
    payload_json, meta_json = _validate_start(kind, stage, payload, meta)
    job_id = "sim-" + uuid.uuid4().hex[:12]
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "payload.json").write_text(payload_json, encoding="utf-8")
        now = time.time()
        with _db() as conn:
            _prune(conn)
            conn.execute(
                "INSERT INTO jobs(job_id,kind,stage,state,created_at,started_at,meta)"
                " VALUES(?,?,?,?,?,?,?)",
                (job_id, kind, stage, QUEUED, now, now, meta_json))
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise

    env = dict(os.environ)
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    try:
        log = open(d / "worker.log", "wb")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "services.sim.job_worker", job_id],
                cwd=str(repo_root), env=env, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            log.close()
    except Exception:
        with _db() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        shutil.rmtree(d, ignore_errors=True)
        raise
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
        value = json.loads(
            p.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {token}")))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


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
    """Adopt terminal verdicts from both spool data and live process truth."""
    job_id = row["job_id"]
    state = row["state"]
    d = job_dir(job_id)
    out: dict = {"job_id": job_id, "kind": row["kind"], "stage": row["stage"],
                 "state": state, "pid": row["pid"],
                 "started_at": row["started_at"], "done_at": row["done_at"],
                 "meta": _read_json_str(row["meta"])}
    if state in (FAILED, CANCELLED):
        if row["error"]:
            out["error"] = row["error"]
        return out

    finished = _read_json(d / "result.json")
    if state == DONE:
        if row["cancel_requested"]:
            error = "cancelled by sim_cancel"
            _set(job_id, state=CANCELLED, error=error)
            out.update(state=CANCELLED, error=error)
            return out
        result = finished.get("result") if finished is not None else None
        if (finished is not None and finished.get("error") is None
                and isinstance(result, dict) and result.get("ok") is True):
            return out
        error = "completed job result artifact is missing, malformed, or failed"
        _set(job_id, state=FAILED, error=error)
        out.update(state=FAILED, error=error)
        return out
    if finished is not None:
        result = finished.get("result")
        if row["cancel_requested"]:
            state, error = CANCELLED, "cancelled by sim_cancel"
        elif finished.get("error") is not None:
            state, error = FAILED, str(finished["error"])
        elif not isinstance(result, dict):
            state, error = FAILED, "worker result file contains no result object"
        elif result.get("ok") is not True:
            detail = (result.get("error") or result.get("reason") or result.get("hint")
                      or "stage did not confirm ok=true")
            state, error = FAILED, str(detail)
        else:
            state, error = DONE, None
        done_at = float(finished.get("done_at") or time.time())
        _set(job_id, state=state, error=error, done_at=done_at)
        out.update(state=state, done_at=done_at)
        if error:
            out["error"] = error
        _reap(job_id)
        return out

    if not pid_alive(row["pid"], row["proc_key"]):
        if row["cancel_requested"]:
            state, error = CANCELLED, "cancelled by sim_cancel"
        else:
            state = FAILED
            tail = _log_tail(job_id)
            error = ("the solver process exited without writing a result"
                     + (f"; last output: {tail}" if tail else ""))
        done_at = time.time()
        _set(job_id, state=state, error=error, done_at=done_at)
        out.update(state=state, error=error, done_at=done_at)
        _reap(job_id)
        return out

    out["state"] = RUNNING
    return out


def _read_json_str(s) -> dict:
    try:
        value = json.loads(
            s or "{}",
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {token}")))
        return value if isinstance(value, dict) else {}
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
    st["ok"] = st["state"] not in (FAILED, CANCELLED)
    return st


def partial(job_id: str) -> Optional[dict]:
    """The child's latest partial snapshot, or None when the stage published none."""
    return _read_json(job_dir(job_id) / "partial.json")


def raw_result(job_id: str) -> Optional[dict]:
    """The stage result only when the durable state is successfully done."""
    row = _row(job_id)
    if row is None or _reconcile(row).get("state") != DONE:
        return None
    finished = _read_json(job_dir(job_id) / "result.json")
    if finished is None or finished.get("error") is not None:
        return None
    result = finished.get("result")
    return result if isinstance(result, dict) and result.get("ok") is True else None


def list_jobs(limit: int = 20) -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [status(r["job_id"]) for r in rows]


# ---------------------------------------------------------------- cancel

def cancel(job_id: str, timeout_s: float = 5.0) -> dict:
    """Terminate the entire process group and prove no solver child survived."""
    row = _row(job_id)
    if row is None:
        return unknown(job_id)
    current = _reconcile(row)
    if current["state"] in TERMINAL:
        return {"ok": True, "job_id": job_id, "state": current["state"],
                "terminated": False,
                "note": f"job already finished as {current['state']} — nothing to cancel"}

    pid, pgid = row["pid"], row["pgid"] or row["pid"]
    key = row["proc_key"]
    _set(job_id, cancel_requested=1)
    signals: list = []

    def alive() -> bool:
        return pid_alive(pid, key) or _group_alive(pgid)

    for sig, grace in ((signal.SIGTERM, min(3.0, timeout_s)),
                       (signal.SIGKILL, timeout_s)):
        if not alive():
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
            if not alive():
                break
            time.sleep(0.05)

    _reap_nb(job_id)
    pid_still_alive = pid_alive(pid, key)
    group_still_alive = _group_alive(pgid)
    still_alive = pid_still_alive or group_still_alive
    now = time.time()
    state = RUNNING if still_alive else CANCELLED
    error = None if still_alive else "cancelled by sim_cancel"
    _set(job_id, state=state, error=error, done_at=None if still_alive else now)
    out = {"ok": not still_alive, "job_id": job_id, "state": state, "pid": pid,
           "signals": signals, "pid_alive_after": pid_still_alive,
           "group_alive_after": group_still_alive, "terminated": not still_alive}
    if still_alive:
        out["error"] = (f"solver process group is STILL ALIVE after "
                        f"{signals or ['no signal']} — do not treat this job as cancelled")
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

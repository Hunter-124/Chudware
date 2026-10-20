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

* ``core_rpc`` — one engine solve RPC from :data:`CORE_RPC_METHODS`. A doc-shaped method
  (``sim_run_flow`` tessellates a document) additionally names ``open_step``: the parent
  exported the solid, and the child re-opens that ONE file, because it shares no document
  table with the server that started it. ``sim_run_pipe_flow`` (internal-flow LBM over a
  voxelized fluid volume) takes either ``params.stl_path`` or that same ``open_step``
  prelude. ``sim_run_topology`` carries a self-contained
  topology envelope (exported STEP or inline tet4 mesh + resolved facesets) and is
  validated by :func:`services.sim.topology.validate_run_envelope`, never as a static
  or modal study.
* ``modal_rpc`` — ``sim_run_modal`` plus the free-free rigid-mode subspace widening, so an
  asynchronous modal answer is the same physics as the synchronous one.
* ``openfoam_tunnel`` — a steady-RANS wind tunnel over an exported STL. Every OpenFOAM
  process is a subprocess inside this job's group, so cancel really stops the mesher.
* ``selftest_sleep`` — the metered stage the cancel/partial contracts are proved against.

COLLECT RUNS ONCE
-----------------
The collect stage writes the solved study into the document, so it is not replayable: the
spool arbitrates it with an atomic claim file and caches the resulting envelope
(:func:`collected` / :func:`claim_collect` / :func:`save_collected`). Every later poll —
from this process, a later turn, or a server that restarted in between — reads that one
envelope instead of persisting a duplicate study.
"""
from __future__ import annotations

import ctypes
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

from services.agent import request_context as _rc

# ---------------------------------------------------------------- states

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (DONE, FAILED, CANCELLED)

# Monotonic time is shared across local worker/poller processes. Keep it injectable
# so stall thresholds can be exercised without waiting or changing wall time.
_clock = time.monotonic
_STALL_S = 120.0

# How long a finished job's row + spool directory are kept so a LATER turn can still
# collect it. Generous by default: the whole point is surviving turn boundaries.
_TTL_S = float(os.environ.get("CHUDWARE_SIM_JOB_TTL_S", "86400") or 86400)


def jobs_dir(*, create: bool = True) -> Path:
    """Spool root: ``$CHUDWARE_SIM_JOBS_DIR`` or ``<appdata root>/sim_jobs``."""
    if _rc.in_cell():
        d = _rc.actor_state_root() / "sim_jobs"
    elif os.environ.get("CHUDWARE_SIM_JOBS_DIR"):
        d = Path(os.environ["CHUDWARE_SIM_JOBS_DIR"])
    else:
        try:
            if create:
                from services.paths import appdata_root  # noqa: PLC0415
            else:
                # A census may use the worker's already-loaded path authority,
                # never import it (or trigger its legacy-root migration).
                appdata_root = sys.modules["services.paths"].appdata_root
            d = appdata_root() / "sim_jobs"
        except Exception:  # noqa: BLE001 — never let a path helper break job start
            if not create:
                raise
            import tempfile
            d = Path(tempfile.gettempdir()) / "Chudware" / "sim_jobs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _owner_text() -> Optional[str]:
    owner = _rc.owner_key()
    return json.dumps(owner) if owner is not None else None


def _desktop_directory(job_id: str) -> Path:
    if (not isinstance(job_id, str) or not job_id or job_id in (".", "..")
            or "/" in job_id or "\\" in job_id or "\x00" in job_id):
        raise KeyError("not_found")
    root = jobs_dir()
    path = root / job_id
    if path.resolve().parent != root.resolve():
        raise KeyError("not_found")
    return path


def _directory(row: sqlite3.Row) -> Path:
    if row["owner"] != _owner_text():
        raise KeyError("not_found")
    if not _rc.in_cell():
        return _desktop_directory(row["job_id"])
    path = Path(row["storage_dir"] or "")
    if not path.is_absolute() or path.resolve().parent != jobs_dir().resolve():
        raise KeyError("not_found")
    return path


def job_dir(job_id: str) -> Path:
    if not _rc.in_cell():
        return _desktop_directory(job_id)
    row = _row(job_id)
    if row is None:
        raise KeyError("not_found")
    return _directory(row)


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
        " cancel_requested INTEGER DEFAULT 0, error TEXT, meta TEXT,"
        " owner TEXT, storage_dir TEXT)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if not {"owner", "storage_dir"} <= columns:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name in ("owner", "storage_dir"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
    return conn


def _set(job_id: str, **cols: Any) -> None:
    if not cols:
        return
    if _rc.in_cell() and _row(job_id) is None:
        raise KeyError("not_found")
    sets = ", ".join(f"{k}=?" for k in cols)
    with _db() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=? AND owner IS ?",
                     (*cols.values(), job_id, _owner_text()))


def _row(job_id: str) -> Optional[sqlite3.Row]:
    if _rc.in_cell() and (
            not isinstance(job_id, str) or not job_id or job_id in (".", "..")
            or "/" in job_id or "\\" in job_id or "\x00" in job_id):
        return None
    with _db() as conn:
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=? AND owner IS ?",
                           (job_id, _owner_text()))
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

#: Payload placeholder that :func:`start` rewrites to a path INSIDE the job's own spool
#: directory. A solver that streams live progress to a file of its own (the LBM wind
#: tunnel writes ``{pct, step, n_steps}`` there) cannot be handed that path by the caller:
#: the job id does not exist yet, and a caller-minted temp path is unreachable to a LATER
#: process reconnecting to the job. The token keeps the payload pure JSON and puts the
#: progress file inside the spool ``_prune`` already owns.
PROGRESS_TOKEN = "@job_progress"


def _resolve_tokens(value: Any, progress_path: str) -> Any:
    """Substitute :data:`PROGRESS_TOKEN` throughout a payload.

    EXACT string matches only — the token is never spliced into a longer string, so a
    payload cannot smuggle a path of its own choosing through a partial match.
    """
    if isinstance(value, str):
        return progress_path if value == PROGRESS_TOKEN else value
    if isinstance(value, dict):
        return {k: _resolve_tokens(v, progress_path) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_tokens(v, progress_path) for v in value]
    return value


#: The engine solve entry points a ``core_rpc`` payload may name. A payload names a
#: METHOD from this closed list — never an import path, a module or a callable.
CORE_RPC_METHODS = ("sim_run_static", "sim_run_modal", "sim_run_adaptive",
                    "sim_run_fatigue", "sim_run_flow", "sim_run_topology",
                    "sim_run_pipe_flow")

#: Methods whose params carry a self-contained ``study`` (an exported STEP path + JSON),
#: i.e. everything the child needs is already on disk.
_STUDY_METHODS = ("sim_run_static", "sim_run_modal", "sim_run_adaptive", "sim_run_fatigue")

#: Study methods whose payload is shaped like a static solve (fixtures + driving loads).
_STATIC_SHAPED = ("sim_run_static", "sim_run_adaptive", "sim_run_fatigue")

#: Methods that tessellate a DOCUMENT rather than a study file, so the child has to open
#: the parent's exported solid first.
_DOC_SHAPED_METHODS = ("sim_run_flow",)

#: Methods whose params carry their own self-contained envelope with its own schema.
_TOPOLOGY_METHODS = ("sim_run_topology",)

#: Internal-flow methods: the fluid volume arrives as ``params.stl_path`` OR as a document
#: the child re-opens from ``open_step`` (never both).
_PIPE_FLOW_METHODS = ("sim_run_pipe_flow",)

#: The only params an internal pipe-flow payload may carry (``doc_id`` is filled by the
#: ``open_step`` prelude, never by the payload).
_PIPE_FLOW_PARAMS = frozenset({
    "stl_path", "body", "inlet", "outlet", "fluid", "temp_c", "pressure_kpa",
    "density_kgm3", "viscosity_pas", "mean_velocity_ms", "flow_m3s", "voxel_mm",
    "cells_across", "max_fluid_cells", "max_steps", "tolerance", "inlet_profile",
    "roughness_mm", "progress_path", "piping", "run_id"})

#: The only params key a geometry prelude is allowed to fill.
_DOC_PARAM = "doc_id"


def _finite(value: Any, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a finite number, got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return out


def _require_geometry_file(path: Any, label: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{label} must be a filesystem path")
    try:
        ready = Path(path).is_file() and Path(path).stat().st_size > 0
    except OSError:
        ready = False
    if not ready:
        raise ValueError(f"{label} is unavailable: {path}")
    return path


def _core_call(method: str, params: dict, emit) -> dict:
    """Stream the existing thread-local native event hook into this job's spool."""
    from services.mcp import core_bridge as cb  # noqa: PLC0415

    # Job launch forces the local engine. A daemon call would install the hook in
    # the wrong process and leave the actual solve silent.
    if cb.active_daemon_mode():
        raise RuntimeError("simulation job progress requires the local engine")
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
    sink = cb._load().chudware_set_event_sink
    sink.argtypes = [callback_type, ctypes.c_void_p]
    sink.restype = None
    callback_error: list[Exception] = []

    def on_event(raw, _user_data):
        try:
            event = json.loads(raw)
            if isinstance(event, dict) and event.get("type") == "sim.progress":
                emit({key: value for key, value in event.items() if key != "type"})
        except Exception as exc:
            # ctypes callbacks cannot propagate an exception across the C ABI.
            # Retain the first failure and report it once the call returns.
            if not callback_error:
                callback_error.append(exc)

    callback = callback_type(on_event)
    sink(callback, None)
    try:
        result = cb.call(method, params)
        if callback_error:
            raise RuntimeError(f"simulation progress publication failed: {callback_error[0]}")
        return result
    finally:
        sink(callback_type(), None)


def _open_geometry(payload: dict, emit) -> Optional[str]:
    """Open the payload's ``open_step`` into a fresh document in THIS child.

    A doc-shaped RPC (``sim_run_flow`` tessellates a document) cannot be replayed in a
    child that shares no document table with the server that started it, so the parent
    exports the solid once at setup and the child re-opens that ONE file. Nothing else in
    the payload can name a file to open, and the only params key the fresh id may fill is
    ``doc_id``.
    """
    step = payload.get("open_step")
    if not step:
        return None
    emit({"stage": "open_geometry", "note": "loading the exported study solid"})
    res = _core_call("doc_open", {"path": str(step)}, emit) or {}
    doc_id = res.get("doc_id")
    if not doc_id:
        raise ValueError(
            f"could not open the exported study geometry {step!r}: "
            f"{res.get('error') or res}")
    return str(doc_id)


def _stage_core_rpc(payload: dict, emit) -> dict:
    """One C++ core RPC (``sim_run_static``, ``sim_run_modal``, ...) in this child.

    ``CHUDWARE_ENGINE_DAEMON=0`` is forced by the launcher so the solve really runs HERE
    and dies with this process.
    """
    method = payload.get("method")
    if method not in CORE_RPC_METHODS:
        raise ValueError(f"core_rpc method must be one of {list(CORE_RPC_METHODS)}")
    params = dict(payload.get("params") or {})
    doc_id = _open_geometry(payload, emit)
    if doc_id is not None:
        params[_DOC_PARAM] = doc_id
    emit({"stage": method, "note": "engine RPC in flight"})
    return _core_call(str(method), params, emit)


def _n_rigid(freqs) -> int:
    """How many of `freqs` sit in the ~0 Hz rigid-body cluster (elastic modes are >> 0.5)."""
    n = 0
    for f in freqs or ():
        try:
            if float(f) < 0.5:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def _stage_modal_rpc(payload: dict, emit) -> dict:
    """``sim_run_modal`` plus the free-free rigid-mode safety net.

    A free-free part spends its first six modes on rigid-body motion and that 0 Hz cluster
    is six-fold degenerate, so an eigensolver can hand back fewer than six of them. The
    synchronous tool has always widened the subspace and re-solved (up to twice) in that
    case. Doing it HERE — inside the one killable child — is what keeps the asynchronous
    answer IDENTICAL to the synchronous one instead of making the background path a
    second, weaker physics that silently renumbers modes.
    """
    study = payload.get("study")
    solve_n = int(payload.get("solve_n") or 0)
    emit({"stage": "sim_run_modal", "n_modes": solve_n, "note": "eigensolve in flight"})
    res = _core_call("sim_run_modal", {"study": study, "n_modes": solve_n}, emit)
    if not payload.get("widen_free_free") or not res.get("ok"):
        return res
    for _ in range(2):
        if _n_rigid(res.get("freqs_hz")) >= 6:
            break
        solve_n += 6
        emit({"stage": "sim_run_modal", "n_modes": solve_n,
              "note": "widening the Krylov subspace for the rigid-body cluster"})
        retry = _core_call("sim_run_modal", {"study": study, "n_modes": solve_n}, emit)
        if not retry.get("ok"):
            break
        res = retry
    return res


#: OpenFOAM mesh/solve presets, in the vocabulary services/sim/foam_case.py accepts.
FOAM_PRESETS = ("coarse", "medium", "fine", "xfine")

#: The scalars an OpenFOAM tunnel result must carry to be reportable at all. Every one is
#: checked finite HERE, in the child, so a NaN can never reach the spool (the result file
#: is written with ``allow_nan=False``) and be mistaken for a missing artifact.
_FOAM_SCALARS = ("cd", "cl", "ld", "drag_n", "lift_n", "side_n", "reynolds",
                 "ref_area_mm2", "vmax_mps")


def _stage_openfoam_tunnel(payload: dict, emit) -> dict:
    """One OpenFOAM steady-RANS wind tunnel over an exported STL, in THIS child.

    Every OpenFOAM invocation is a subprocess (GPL isolation — never linked), and each one
    is born into this job's process group. That is what makes ``cancel`` actually stop a
    run with minutes of snappyHexMesh left instead of orphaning a mesher that keeps a core
    busy long after the agent was told the job was cancelled.
    """
    from services.sim.openfoam import run_openfoam_tunnel  # noqa: PLC0415
    stl = _require_geometry_file(payload.get("stl_path"), "openfoam_tunnel STL geometry")
    fluid = payload.get("fluid")
    emit({"stage": "mesh", "pct": 0, "text": "queued"})
    r = run_openfoam_tunnel(
        stl,
        direction=(1.0, 0.0, 0.0),
        speed_mps=_finite(payload.get("speed_mps"), "speed_mps"),
        preset=str(payload.get("preset") or "coarse"),
        aoa_deg=_finite(payload.get("aoa_deg", 0.0), "aoa_deg"),
        fluid=fluid if isinstance(fluid, dict) else None,
        on_progress=lambda ev: emit({"stage": ev.get("stage", ""),
                                     "pct": ev.get("pct", 0),
                                     "text": ev.get("text", "")}))
    out: dict = {"ok": True, "diverged": bool(r.diverged),
                 "steps_run": int(r.steps_run),
                 "solver_notes": list(r.solver_notes or [])}
    for key in _FOAM_SCALARS:
        out[key] = _finite(getattr(r, key), f"OpenFOAM {key}")
    return out


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
    "modal_rpc": _stage_modal_rpc,
    "openfoam_tunnel": _stage_openfoam_tunnel,
    "selftest_sleep": _stage_selftest_sleep,
}


def _json_text(value: Any, label: str) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON data: {exc}") from None


def _validate_material(method: str, study: dict, keys: tuple) -> None:
    material = study.get("material")
    if not isinstance(material, dict) or not material:
        raise ValueError(f"{method} study needs a material object")
    if "per_body" in material:
        if method != "sim_run_static":
            raise ValueError(f"{method} does not support per-body materials")
        entries = material["per_body"]
        if not isinstance(entries, dict) or (not entries and "default" not in material):
            raise ValueError(f"{method} material.per_body needs assignments or a default")
        for ordinal, leaf in entries.items():
            if not isinstance(ordinal, str) or not ordinal.isascii() or not ordinal.isdecimal() or ordinal.startswith("0"):
                raise ValueError(f"{method} invalid material body ordinal {ordinal!r}")
            if not isinstance(leaf, dict) or "per_body" in leaf:
                raise ValueError(f"{method} material body {ordinal} needs a property object")
            _validate_material(method, {"material": {"yield_mpa": 250.0, **leaf}}, keys)
        if "default" in material:
            default = material["default"]
            if not isinstance(default, dict) or "per_body" in default:
                raise ValueError(f"{method} material.default needs a property object")
            _validate_material(method, {"material": {"yield_mpa": 250.0, **default}}, keys)
        return
    for key in keys:
        value = _finite(material.get(key), f"{method} material {key}")
        if key != "nu" and value <= 0.0:
            raise ValueError(f"{method} material {key} must be > 0")
        if key == "nu" and not -1.0 < value < 0.5:
            raise ValueError(f"{method} material nu must be between -1 and 0.5")


def _validate_driving_loads(method: str, study: dict) -> None:
    """A study nothing drives solves to an identically zero field — a well-formed lie."""
    loads = study.get("loads")
    if not isinstance(loads, list) or not loads:
        raise ValueError(f"{method} needs at least one non-zero load")
    driven = False
    for load in loads:
        if not isinstance(load, dict) or not load.get("faces"):
            raise ValueError(f"every {method} load needs resolved faces")
        if load.get("type") == "pressure":
            magnitude = _finite(load.get("magnitude", 0.0), f"{method} load magnitude")
            driven = driven or magnitude != 0.0
        elif load.get("type") == "force":
            vector = load.get("vector")
            if not isinstance(vector, list) or len(vector) != 3:
                raise ValueError(f"{method} force load needs a three-component vector")
            values = [_finite(v, f"{method} load vector") for v in vector]
            driven = driven or any(value != 0.0 for value in values)
        else:
            raise ValueError(f"{method} load type must be pressure or force")
    if not driven:
        raise ValueError(f"{method} needs at least one non-zero load")


def validate_interfaces(interfaces: Any, label: str = "interfaces") -> None:
    """Validate the JSON shape only; the engine resolves STEP/body identities."""
    if not isinstance(interfaces, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(interfaces) - {"bonded", "contact", "default"}
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(sorted(map(str, unknown)))}")
    if "default" in interfaces and interfaces["default"] not in ("contact", "bonded", "free"):
        raise ValueError(f"{label}.default must be contact, bonded, or free")
    for kind in ("bonded", "contact"):
        if kind not in interfaces:
            continue
        pairs = interfaces[kind]
        if not isinstance(pairs, list):
            raise ValueError(f"{label}.{kind} must be a list of [a, b] body pairs")
        for index, pair in enumerate(pairs):
            where = f"{label}.{kind}[{index}]"
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"{where} must be a two-item [a, b] body pair")
            for body in pair:
                if (
                    isinstance(body, int) and not isinstance(body, bool) and body > 0
                ) or (isinstance(body, str) and body.strip()):
                    continue
                raise ValueError(
                    f"{where} body references must be positive 1-based STEP solid "
                    "ordinals or non-empty body names"
                )


def _validate_study_params(method: str, params: dict) -> None:
    study = params.get("study")
    if not isinstance(study, dict):
        raise ValueError(f"{method} params need a study object")
    _require_geometry_file(study.get("step_path"), f"{method} STEP geometry")
    if "interfaces" in study:
        validate_interfaces(study["interfaces"], f"{method} study.interfaces")
    if "bond_gap_mm" in study:
        gap = study["bond_gap_mm"]
        if isinstance(gap, bool) or _finite(gap, f"{method} bond_gap_mm") < 0.0:
            raise ValueError(f"{method} bond_gap_mm must be finite and >= 0")
    if "contact" in study:
        contact = study["contact"]
        if not isinstance(contact, dict):
            raise ValueError(f"{method} study.contact must be an object")
        penalty = contact.get("penalty_scale", 1.0)
        if isinstance(penalty, bool) or _finite(penalty, f"{method} contact.penalty_scale") <= 0.0:
            raise ValueError(f"{method} contact.penalty_scale must be finite and > 0")
        if (
            contact.get("enabled", True)
            and "bond_gap_mm" in study
            and "interfaces" not in study
        ):
            raise ValueError(
                f"{method} bond_gap_mm with contact requires study.interfaces "
                "to distinguish bonded and contact body pairs"
            )
    fixtures = study.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError(f"{method} study fixtures must be a list")
    if method in _STATIC_SHAPED:
        if not fixtures:
            raise ValueError(f"{method} needs at least one fixture")
        _validate_material(method, study, ("E_mpa", "nu", "yield_mpa"))
        _validate_driving_loads(method, study)
    else:
        _validate_material(method, study, ("E_mpa", "nu", "density_g_cm3"))
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
    if method == "sim_run_adaptive":
        adapt = study.get("adapt")
        if not isinstance(adapt, dict):
            raise ValueError("sim_run_adaptive study needs an adapt object")
        passes = adapt.get("max_passes")
        if isinstance(passes, bool) or not isinstance(passes, int) or not 1 <= passes <= 6:
            raise ValueError("sim_run_adaptive max_passes must be an integer from 1 to 6")
        if _finite(adapt.get("eta_target"), "sim_run_adaptive eta_target") <= 0.0:
            raise ValueError("sim_run_adaptive eta_target must be > 0")
    if method == "sim_run_fatigue":
        fatigue = study.get("fatigue")
        if not isinstance(fatigue, dict) or not fatigue:
            raise ValueError("sim_run_fatigue study needs a fatigue object")
        for key in ("ultimate_mpa", "se_corrected_mpa"):
            if _finite(fatigue.get(key), f"sim_run_fatigue fatigue.{key}") <= 0.0:
                raise ValueError(f"sim_run_fatigue fatigue.{key} must be > 0")


def _validate_flow_params(params: dict) -> None:
    if _finite(params.get("velocity"), "sim_run_flow velocity") <= 0.0:
        raise ValueError("sim_run_flow velocity must be > 0 m/s")
    _finite(params.get("aoa", 0.0), "sim_run_flow aoa")
    fluid = params.get("fluid", "air")
    if fluid not in ("air", "water"):
        raise ValueError("sim_run_flow fluid must be 'air' or 'water'")
    for key, floor in (("cells", 16), ("n_steps", 1)):
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < floor:
            raise ValueError(f"sim_run_flow {key} must be an integer >= {floor}")


def _vec3(value: Any, label: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must be a 3-vector [x, y, z]")
    return [_finite(v, label) for v in value]


def _validate_pipe_flow_payload(payload: dict, params: dict) -> None:
    """``sim_run_pipe_flow``: one geometry source, two boundary planes, one flow rate."""
    extra = sorted(set(params) - _PIPE_FLOW_PARAMS)
    if extra:
        raise ValueError(f"sim_run_pipe_flow params do not accept {extra}")
    step = payload.get("open_step")
    stl = params.get("stl_path")
    if (step is None) == (stl is None):
        raise ValueError(
            "sim_run_pipe_flow needs exactly one geometry source: params.stl_path (a fluid-"
            "volume STL) or open_step=<exported STEP path> (a document the child re-opens)")
    if step is not None:
        _require_geometry_file(step, "sim_run_pipe_flow exported document geometry")
    else:
        _require_geometry_file(stl, "sim_run_pipe_flow STL geometry")
    if params.get("body") is not None and not isinstance(params.get("body"), str):
        raise ValueError("sim_run_pipe_flow body must be a body name")
    for side in ("inlet", "outlet"):
        plane = params.get(side)
        if not isinstance(plane, dict):
            raise ValueError(f"sim_run_pipe_flow {side} must be {{point, normal}}")
        _vec3(plane.get("point"), f"sim_run_pipe_flow {side}.point")
        normal = _vec3(plane.get("normal"), f"sim_run_pipe_flow {side}.normal")
        if math.sqrt(sum(c * c for c in normal)) <= 1e-12:
            raise ValueError(f"sim_run_pipe_flow {side}.normal must be non-zero")
    rates = [k for k in ("mean_velocity_ms", "flow_m3s") if params.get(k) is not None]
    if len(rates) != 1:
        raise ValueError("sim_run_pipe_flow needs exactly one of mean_velocity_ms or flow_m3s")
    if _finite(params[rates[0]], f"sim_run_pipe_flow {rates[0]}") <= 0.0:
        raise ValueError(f"sim_run_pipe_flow {rates[0]} must be > 0")
    explicit = [k for k in ("density_kgm3", "viscosity_pas") if params.get(k) is not None]
    if explicit:
        if len(explicit) != 2:
            raise ValueError("sim_run_pipe_flow density_kgm3 and viscosity_pas go together")
        for key in explicit:
            if _finite(params[key], f"sim_run_pipe_flow {key}") <= 0.0:
                raise ValueError(f"sim_run_pipe_flow {key} must be > 0")
    else:
        fluid = params.get("fluid")
        if not isinstance(fluid, str) or not fluid.strip():
            raise ValueError("sim_run_pipe_flow needs a fluid name or density_kgm3 + "
                             "viscosity_pas")
    for key in ("temp_c", "pressure_kpa"):
        if params.get(key) is not None:
            _finite(params[key], f"sim_run_pipe_flow {key}")
    for key in ("voxel_mm", "roughness_mm"):
        if params.get(key) is not None:
            value = _finite(params[key], f"sim_run_pipe_flow {key}")
            if value < 0.0 or (key == "voxel_mm" and value == 0.0):
                raise ValueError(f"sim_run_pipe_flow {key} must be > 0")
    steps = params.get("max_steps")
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int)
                              or steps < 1):
        raise ValueError("sim_run_pipe_flow max_steps must be an integer >= 1")
    cells = params.get("cells_across")
    if cells is not None and (isinstance(cells, bool) or not isinstance(cells, int)
                              or not 4 <= cells <= 128):
        raise ValueError("sim_run_pipe_flow cells_across must be an integer in [4, 128]")
    if params.get("progress_path") is not None and not isinstance(params["progress_path"], str):
        raise ValueError("sim_run_pipe_flow progress_path must be a path")
    piping = params.get("piping")
    if piping is not None and not isinstance(piping, dict):
        raise ValueError("sim_run_pipe_flow piping must be an aicad.piping/1 object")
    run = params.get("run_id")
    if run is not None and (not isinstance(run, str) or not run):
        raise ValueError("sim_run_pipe_flow run_id must be a run id string")
    if run is not None and piping is None:
        raise ValueError("sim_run_pipe_flow run_id needs the piping document it names")


#: Substrings that mean "this build has no such solver", as opposed to "the solver is here
#: and rejected an empty payload" — which is the answer a WORKING solver gives the probe.
_MISSING_SOLVER_TOKENS = ("unknown method", "solver unavailable", "not compiled")


def _probe_backend(method: str) -> None:
    """Refuse a job the engine could never dispatch — without inventing the verdict.

    The probe deliberately sends EMPTY params. A build WITHOUT the solver answers "unknown
    method"; a build WITH it rejects the empty study on its own terms (a missing doc, a
    missing study, a degenerate geometry). Only the first is a reason not to queue, so an
    ordinary rejection — however it is delivered, return value or raise — is read as the
    solver confirming it is there. Queuing a job that can only fail is bad; refusing one
    that would have worked is worse.
    """
    from services.mcp import core_bridge as cb  # noqa: PLC0415
    if not cb.available():
        raise ValueError("core simulation backend is unavailable in the job worker process")
    try:
        probe = cb.call(method, {})
    except Exception as exc:  # noqa: BLE001 — any error text is inspected, none is trusted
        text = str(exc).lower()
        if any(token in text for token in _MISSING_SOLVER_TOKENS):
            raise ValueError(
                f"core simulation backend cannot dispatch {method}: {exc}") from None
        return
    probe_error = str(probe.get("error") or probe.get("message") or "").lower()
    if (probe.get("ok") is False
            and any(token in probe_error for token in _MISSING_SOLVER_TOKENS)):
        raise ValueError(f"core simulation backend cannot dispatch {method}: {probe_error}")


def _validate_core_rpc_payload(payload: dict) -> None:
    method = payload.get("method")
    if method not in CORE_RPC_METHODS:
        raise ValueError(f"core_rpc method must be one of {list(CORE_RPC_METHODS)}")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("core_rpc payload needs a params object")
    if method in _TOPOLOGY_METHODS:
        if payload.get("open_step") is not None:
            raise ValueError(
                f"{method} carries its geometry in study.step_path (or an inline tet4 "
                "mesh); open_step is not accepted")
        extra = sorted(set(params) - {"study"})
        if extra:
            raise ValueError(f"{method} params accept only study, got {extra}")
        from services.sim.topology import validate_run_envelope  # noqa: PLC0415

        validate_run_envelope(params.get("study"))
        _probe_backend(method)
        return
    if method in _PIPE_FLOW_METHODS:
        _validate_pipe_flow_payload(payload, params)
        _probe_backend(method)
        return
    step = payload.get("open_step")
    if step is None:
        if method in _DOC_SHAPED_METHODS:
            raise ValueError(
                f"{method} solves a DOCUMENT, so its job payload needs "
                "open_step=<exported STEP path>: a child process shares no document table "
                "with the server that started it")
    else:
        _require_geometry_file(step, f"{method} exported document geometry")
    if method in _STUDY_METHODS:
        _validate_study_params(method, params)
    else:
        _validate_flow_params(params)
    _probe_backend(method)


def _validate_modal_rpc_payload(payload: dict) -> None:
    solve_n = payload.get("solve_n")
    if isinstance(solve_n, bool) or not isinstance(solve_n, int) or solve_n <= 0:
        raise ValueError("modal_rpc solve_n must be a positive integer")
    _validate_study_params("sim_run_modal", {"study": payload.get("study"),
                                             "n_modes": solve_n})
    _probe_backend("sim_run_modal")


def _validate_openfoam_payload(payload: dict) -> None:
    _require_geometry_file(payload.get("stl_path"), "openfoam_tunnel STL geometry")
    if _finite(payload.get("speed_mps"), "openfoam_tunnel speed_mps") <= 0.0:
        raise ValueError("openfoam_tunnel speed_mps must be > 0")
    _finite(payload.get("aoa_deg", 0.0), "openfoam_tunnel aoa_deg")
    preset = payload.get("preset", "coarse")
    if preset not in FOAM_PRESETS:
        raise ValueError(f"openfoam_tunnel preset must be one of {list(FOAM_PRESETS)}")
    fluid = payload.get("fluid")
    if fluid is not None:
        if not isinstance(fluid, dict):
            raise ValueError("openfoam_tunnel fluid must be an object")
        for key in ("nu", "rho"):
            if _finite(fluid.get(key), f"openfoam_tunnel fluid.{key}") <= 0.0:
                raise ValueError(f"openfoam_tunnel fluid.{key} must be > 0")


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
    elif stage == "modal_rpc":
        _validate_modal_rpc_payload(payload)
    elif stage == "openfoam_tunnel":
        _validate_openfoam_payload(payload)
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
        "SELECT * FROM jobs WHERE state IN (?,?,?) AND done_at IS NOT NULL "
        "AND done_at < ? AND owner IS ?",
        (DONE, FAILED, CANCELLED, cutoff, _owner_text())).fetchall()
    for r in rows:
        shutil.rmtree(_directory(r), ignore_errors=True)
        conn.execute("DELETE FROM jobs WHERE job_id=?", (r["job_id"],))


def _module_command(module: str) -> list[str]:
    """Re-enter a Python module: ``-m`` under an interpreter, the compiled sidecar's own
    ``--module`` dispatcher (services/compiled_entry.py) when frozen. A compiled sidecar
    is not a Python interpreter, so it is never handed ``-m``."""
    flag = "--module" if "__compiled__" in globals() or getattr(sys, "frozen", False) else "-m"
    return [sys.executable, flag, module]


def start(kind: str, stage: str, payload: dict, meta: Optional[dict] = None) -> dict:
    """Validate setup, spawn one stage process, and return its durable job id."""
    token = uuid.uuid4().hex
    job_id = "sim-" + (token if _rc.in_cell() else token[:12])
    d = (jobs_dir() / ("spool-" + uuid.uuid4().hex) if _rc.in_cell()
         else job_dir(job_id))
    # The job id has to exist BEFORE validation so a payload can address the job's own
    # spool (PROGRESS_TOKEN) and be validated against the paths the child will really see.
    payload = _resolve_tokens(payload, str(d / "progress.json"))
    payload_json, meta_json = _validate_start(kind, stage, payload, meta)
    if _rc.in_cell():
        spec = json.loads(payload_json)
        spec["ctx"] = _rc.current_context()
        spec["roots"] = {"project": _rc.root_value("CHUDWARE_PROJECT_DIR", "/work"),
                         "save": _rc.root_value("CHUDWARE_PROJECT_SAVE_DIR", "/work")}
        payload_json = _json_text(spec, "job payload")
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "payload.json").write_text(payload_json, encoding="utf-8")
        now = time.time()
        with _db() as conn:
            _prune(conn)
            conn.execute(
                "INSERT INTO jobs(job_id,kind,stage,state,created_at,started_at,meta,owner,storage_dir)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, kind, stage, QUEUED, now, now, meta_json, _owner_text(), str(d)))
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise

    env = dict(os.environ)
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    if _rc.in_cell():
        env["CHUDWARE_CELL"] = "1"
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    try:
        log = open(d / "worker.log", "wb")
        try:
            proc = subprocess.Popen(
                _rc.sandbox_argv([*_module_command("services.sim.job_worker"), job_id,
                                  *(["--job-dir", str(d)] if _rc.in_cell() else [])]),
                cwd="/work" if _rc.in_cell() else str(repo_root),
                env=env, stdout=log, stderr=subprocess.STDOUT,
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
    _LOCAL_PROCS[_rc.scoped_key(job_id)] = proc
    return {"job_id": job_id, "state": RUNNING, "pid": proc.pid, "kind": kind,
            "stage": stage, "started_at": now}


# Popen handles for jobs THIS process started, so we can reap them and never leave a
# zombie that would make pid_alive() lie about a killed worker.
_LOCAL_PROCS: dict[str, subprocess.Popen] = {}


def worker_activity() -> dict:
    """Read the existing registry without creating, reconciling or cancelling it."""
    children = [proc for key, proc in list(_LOCAL_PROCS.items())
                if _rc.owns_registry_key(key) and proc.poll() is None]
    database = jobs_dir(create=False) / "jobs.sqlite"
    if not database.exists():
        if children:
            raise RuntimeError("simulation_registry_missing")
        return {"by_kind": {"simulation_jobs": 0}, "processes": []}
    if database.is_symlink():
        raise RuntimeError("simulation_registry_unsafe")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT state,pid,pgid,proc_key FROM jobs WHERE owner IS ?",
            (_owner_text(),)).fetchmany(65537)
    if len(rows) > 65536:
        raise RuntimeError("simulation_registry_oversized")
    from services.worker_activity import _process_snapshot

    records = _process_snapshot()
    groups = {record.group for record in records.values()}
    active = 0
    for row in rows:
        record = records.get(row["pid"])
        alive = record is not None and (not row["proc_key"] or record.start == row["proc_key"])
        if ((row["state"] == QUEUED and not row["pid"]) or alive
                or (row["state"] not in TERMINAL and row["pgid"] in groups)):
            active += 1
    return {"by_kind": {"simulation_jobs": active}, "processes": children}


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
    if _rc.in_cell():
        return {"ok": False, "state": "unknown", "error": "not_found",
                "reason": "not_found", "code": "not_found"}
    return {"ok": False, "job_id": job_id, "state": "unknown",
            "error": UNKNOWN_JOB_HELP.format(dir=jobs_dir(), ttl=_TTL_S / 3600.0)}


def _reconcile(row: sqlite3.Row) -> dict:
    """Adopt terminal verdicts from both spool data and live process truth."""
    job_id = row["job_id"]
    state = row["state"]
    d = _directory(row)
    out: dict = {"job_id": job_id, "kind": row["kind"], "stage": row["stage"],
                 "state": state, "pid": row["pid"],
                 "started_at": row["started_at"], "done_at": row["done_at"],
                 "meta": _read_json_str(row["meta"])}
    if state in (FAILED, CANCELLED):
        if row["error"]:
            out["error"] = row["error"]
        return out

    # Observe exit before reading the artifact. The worker publishes its final
    # partial and result before exiting; a missing result read made BEFORE an
    # exit check can otherwise turn a successful completion into a permanent failure.
    alive = state != DONE and pid_alive(row["pid"], row["proc_key"])
    if not alive:
        _reap(job_id)
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

    if not alive:
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
    proc = _LOCAL_PROCS.pop(_rc.scoped_key(job_id), None)
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
        progress = {k: v for k, v in prog.items()
                    if k not in ("partial", "_last_advance_monotonic")}
        last_advance = prog.get("_last_advance_monotonic")
        age = float(progress.get("last_advance_s", 0.0))
        if last_advance is not None:
            age = max(0.0, _clock() - float(last_advance))
        progress["last_advance_s"] = round(age, 3)
        progress["stalled"] = st["state"] == RUNNING and age > _STALL_S
        st["progress"] = progress
    st["ok"] = st["state"] not in (FAILED, CANCELLED)
    return st


def partial(job_id: str) -> Optional[dict]:
    """The child's latest partial snapshot, or None when the stage published none."""
    return _read_json(job_dir(job_id) / "partial.json")


def raw_result(job_id: str) -> Optional[dict]:
    """The stage result only when the durable state is successfully done."""
    row = _row(job_id)
    if row is None and _rc.in_cell():
        raise KeyError("not_found")
    if row is None or _reconcile(row).get("state") != DONE:
        return None
    finished = _read_json(job_dir(job_id) / "result.json")
    if finished is None or finished.get("error") is not None:
        return None
    result = finished.get("result")
    return result if isinstance(result, dict) and result.get("ok") is True else None


def refusal_result(job_id: str) -> Optional[dict]:
    """A failed stage's diagnostic packet, never a converged/collectable result."""
    row = _row(job_id)
    if row is None and _rc.in_cell():
        raise KeyError("not_found")
    if row is None or _reconcile(row).get("state") != FAILED:
        return None
    finished = _read_json(job_dir(job_id) / "result.json")
    result = finished.get("result") if isinstance(finished, dict) else None
    return result if isinstance(result, dict) and result.get("ok") is False else None


def terminal_result(job_id: str) -> Optional[dict]:
    """The stage result a FAILED or CANCELLED job's worker actually wrote, else None.

    None is the honest answer for a worker that failed before its stage returned or was
    killed (``sim_cancel``): nothing was replied, so nothing may be invented. A cancel that
    raced a finished stage keeps that stage's own reply; the job is still CANCELLED.
    """
    row = _row(job_id)
    if row is None and _rc.in_cell():
        raise KeyError("not_found")
    if row is None or _reconcile(row).get("state") not in (FAILED, CANCELLED):
        return None
    finished = _read_json(job_dir(job_id) / "result.json")
    result = finished.get("result") if isinstance(finished, dict) else None
    return result if isinstance(result, dict) else None


# ---------------------------------------------------------------- collect (exactly once)
# The stage result is raw solver output; the COLLECT stage that turns it into the tool's
# reply envelope runs back in the parent, because that is where the live document is and
# therefore the only place the study can be written into the part. Collect is not pure:
# `_persist_study` appends a study record to the document. So it must run ONCE per job, no
# matter how many times (or from how many processes) the job is polled afterwards — the
# alternative is one solve producing five identical studies in the feature tree.
#
# The claim file is the arbiter: O_EXCL creation is atomic across processes, so exactly one
# poller collects and everybody else reads the artifact it wrote.

def collected(job_id: str) -> Optional[dict]:
    """The collected reply envelope for a finished job, or None when nobody collected yet."""
    return _read_json(job_dir(job_id) / "collected.json")


def claim_collect(job_id: str) -> bool:
    """True for the ONE caller that may run the collect stage for `job_id`."""
    try:
        fd = os.open(str(job_dir(job_id) / "collect.claim"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        # No spool to arbitrate with (pruned, read-only): collecting twice is a lesser
        # failure than never reporting the answer at all.
        return True
    try:
        os.write(fd, f"{os.getpid()} {time.time():.3f}".encode())
    finally:
        os.close(fd)
    return True


def release_collect(job_id: str) -> None:
    """Drop an UNFULFILLED claim so a later poll can retry the collect."""
    try:
        (job_dir(job_id) / "collect.claim").unlink()
    except OSError:
        pass


def save_collected(job_id: str, value: dict) -> dict:
    """Persist the collect stage's reply so every later poll returns the SAME answer.

    Best-effort by design: a reply that cannot be spooled (non-finite float, unwritable
    spool) is still returned to the caller who computed it — losing the answer would be a
    worse outcome than losing the ability to replay it.
    """
    d = job_dir(job_id)
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return value
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "collected.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, d / "collected.json")
    except OSError:
        pass
    return value


def collect_evidence(job_id: str) -> Optional[dict]:
    """Evidence a previous, unfinished collect already wrote for `job_id`, or None.

    A collect whose document write failed retryably releases its claim WITHOUT caching a
    reply. The files it had already written stay recorded here, so the retry persists the
    same evidence instead of writing a second copy of every artifact.
    """
    return _read_json(job_dir(job_id) / "collect_evidence.json")


def save_collect_evidence(job_id: str, value: dict) -> bool:
    """Record a collect's written evidence; False when the spool cannot hold it."""
    d = job_dir(job_id)
    try:
        text = json.dumps(value, allow_nan=False)
        tmp = d / "collect_evidence.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, d / "collect_evidence.json")
    except (OSError, TypeError, ValueError):
        return False
    return True


def list_jobs(limit: int = 20) -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE owner IS ? ORDER BY started_at DESC LIMIT ?",
            (_owner_text(), int(limit))).fetchall()
    return [status(r["job_id"]) for r in rows]


# ---------------------------------------------------------------- cancel

def cancel(job_id: str, timeout_s: float = 5.0) -> dict:
    """Terminate the entire process group and prove no solver child survived."""
    delegate = _rc.control_delegate("cancel")
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
    if delegate and not still_alive:
        error = "manager_stop"
        meta = _read_json_str(row["meta"])
        meta["last_control_stop"] = {"reason": "manager_stop", "control_delegate": delegate}
        _set(job_id, meta=json.dumps(meta))
        event = {"kind": "run_stopped", "job_id": job_id, "reason": "manager_stop",
                 "control_delegate": delegate, "ts": now}
        with (job_dir(job_id) / "control-events.jsonl").open("a") as audit:
            audit.write(json.dumps(event) + "\n")
            audit.flush()
    _set(job_id, state=state, error=error, done_at=None if still_alive else now)
    out = {"ok": not still_alive, "job_id": job_id, "state": state, "pid": pid,
           "signals": signals, "pid_alive_after": pid_still_alive,
           "group_alive_after": group_still_alive, "terminated": not still_alive}
    if still_alive:
        out["error"] = (f"solver process group is STILL ALIVE after "
                        f"{signals or ['no signal']} — do not treat this job as cancelled")
    if delegate:
        result = {"ok": out["ok"], "job_id": job_id, "state": state,
                  "terminated": out["terminated"]}
        if still_alive:
            result["error"] = "solver process group is still alive"
        else:
            result.update(reason="manager_stop", control_delegate=delegate)
        return result
    return out


def _reap_nb(job_id: str) -> None:
    key = _rc.scoped_key(job_id)
    proc = _LOCAL_PROCS.get(key)
    if proc is not None and proc.poll() is not None:
        _LOCAL_PROCS.pop(key, None)


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

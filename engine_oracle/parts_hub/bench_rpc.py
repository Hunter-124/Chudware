"""Worker-RPC bridge for GUI-launched gym benchmarks (Settings → AI leaderboard).

Exposes ``bench_run`` / ``bench_status`` as engine-worker methods so the C++ Settings
panel can kick off a gym CLI cycle without blocking the GL thread.

``bench_run`` spawns ``python3 -m tools.flywheel.bench.gym.cli …`` as a DETACHED
subprocess on a daemon thread (same non-blocking pattern as subscription_login) and
returns immediately. On process exit it runs ``leaderboard build`` so the Settings
table can refresh from ``benchmarks/leaderboard/leaderboard.json``.

Status is written to a JSON file under the gym reports dir; ``bench_status`` reads it.
Never blocks the RPC dispatch thread. Never writes API keys into the status file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _repo_root() -> Path:
    env = os.environ.get("AI_CAD_REPO") or os.environ.get("AICAD_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # engine_oracle/parts_hub/bench_rpc.py → repo root
    return Path(__file__).resolve().parents[2]


def _reports_dir() -> Path:
    return _repo_root() / "tools" / "flywheel" / "bench" / "gym" / "reports"


def _status_path() -> Path:
    return _reports_dir() / "bench_status.json"


def _leaderboard_path() -> Path:
    return _repo_root() / "benchmarks" / "leaderboard" / "leaderboard.json"


_state_lock = threading.Lock()
_active_proc: subprocess.Popen | None = None
_active_thread: threading.Thread | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_status() -> dict:
    return {
        "running": False,
        "stage": "idle",
        "started": None,
        "finished": None,
        "last_error": None,
        "last_result_path": None,
        "mode": None,
        "tiers": [],
        "pid": None,
        "exit_code": None,
    }


def _write_status(patch: dict) -> dict:
    """Merge *patch* into the on-disk status file under the lock."""
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        cur = _default_status()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cur.update(loaded)
            except Exception:  # noqa: BLE001
                pass
        cur.update(patch)
        path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        return dict(cur)


def _read_status() -> dict:
    path = _status_path()
    with _state_lock:
        if not path.exists():
            return _default_status()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return _default_status()
            out = _default_status()
            out.update(loaded)
            # Live process check — if the status says running but the process is gone
            # and no thread is finishing it up, mark error.
            global _active_proc, _active_thread
            if out.get("running") and _active_proc is not None:
                rc = _active_proc.poll()
                if rc is not None and (_active_thread is None or not _active_thread.is_alive()):
                    # Thread should have written final status; heal if it crashed.
                    if out.get("stage") not in ("done", "error", "building_leaderboard"):
                        out["running"] = False
                        out["stage"] = "error" if rc != 0 else "done"
                        out["exit_code"] = rc
                        out["finished"] = out.get("finished") or _utc_now()
                        if rc != 0 and not out.get("last_error"):
                            out["last_error"] = f"bench process exited with code {rc}"
            return out
        except Exception as exc:  # noqa: BLE001
            st = _default_status()
            st["last_error"] = f"status read failed: {exc}"
            return st


def _normalize_tiers(params: dict) -> list[str]:
    raw = (params or {}).get("tiers")
    if raw is None:
        return ["local-24k"]
    if isinstance(raw, str):
        parts = [t.strip() for t in raw.split(",") if t.strip()]
        return parts or ["local-24k"]
    if isinstance(raw, (list, tuple)):
        parts = [str(t).strip() for t in raw if str(t).strip()]
        return parts or ["local-24k"]
    return ["local-24k"]


def _build_cmd(mode: str, tiers: list[str], cycle: str) -> list[str]:
    py = sys.executable or "python3"
    tiers_csv = ",".join(tiers)
    if mode == "diag":
        return [py, "-m", "tools.flywheel.bench.gym.cli", "diag", "--tiers", tiers_csv]
    # smoke = C1 only, real execution on the selected tiers
    return [
        py, "-m", "tools.flywheel.bench.gym.cli", "run",
        "--real",
        "--challenges", "C1",
        "--tiers", tiers_csv,
        "--cycle", cycle,
        "--no-adjust",
    ]


def _run_pipeline(mode: str, tiers: list[str], cycle: str) -> None:
    """Daemon-thread body: spawn gym CLI, wait, rebuild leaderboard, write status."""
    global _active_proc, _active_thread
    root = _repo_root()
    cmd = _build_cmd(mode, tiers, cycle)
    env = os.environ.copy()
    env.setdefault("AI_CAD_REPO", str(root))
    # Never inherit interactive TTY assumptions into the detached child.
    env.setdefault("PYTHONUNBUFFERED", "1")

    log_dir = _reports_dir() / "gui_bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cycle}.log"

    _write_status({
        "running": True,
        "stage": "running",
        "last_error": None,
        "exit_code": None,
        "cmd": cmd,
        "log_path": str(log_path),
    })

    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"# bench_run mode={mode} tiers={tiers} cycle={cycle}\n")
            logf.write(f"# cwd={root}\n# cmd={' '.join(cmd)}\n\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from worker process group
            )
            with _state_lock:
                _active_proc = proc
            _write_status({"pid": proc.pid, "stage": "running"})
            rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"spawn failed: {exc}",
            "pid": None,
            "exit_code": None,
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    if rc != 0:
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"gym cli exited with code {rc} (see {log_path})",
            "exit_code": rc,
            "last_result_path": str(log_path),
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    # Rebuild leaderboard so the GUI table picks up the new cells.
    _write_status({"stage": "building_leaderboard", "exit_code": rc})
    lb_cmd = [sys.executable or "python3", "-m", "tools.flywheel.bench.gym.leaderboard", "build"]
    lb_rc = 0
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n# leaderboard build\n# cmd={' '.join(lb_cmd)}\n")
            logf.flush()
            lb = subprocess.run(
                lb_cmd,
                cwd=str(root),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            lb_rc = int(lb.returncode)
    except Exception as exc:  # noqa: BLE001
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"leaderboard build failed: {exc}",
            "exit_code": rc,
            "last_result_path": str(log_path),
        })
        with _state_lock:
            _active_proc = None
            _active_thread = None
        return

    lb_path = _leaderboard_path()
    result_path = str(lb_path) if lb_path.exists() else str(log_path)
    if lb_rc != 0:
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": f"leaderboard build exited with code {lb_rc}",
            "exit_code": lb_rc,
            "last_result_path": result_path,
        })
    else:
        _write_status({
            "running": False,
            "stage": "done",
            "finished": _utc_now(),
            "last_error": None,
            "exit_code": 0,
            "last_result_path": result_path,
        })
    with _state_lock:
        _active_proc = None
        _active_thread = None


def _bench_run(params: dict, _emit: Callable) -> dict:
    """Start a gym CLI run on a daemon thread; return immediately."""
    global _active_thread, _active_proc
    try:
        mode = str((params or {}).get("mode", "diag")).strip().lower()
        if mode not in ("diag", "smoke"):
            return {
                "ok": False,
                "reason": f"mode must be 'diag' or 'smoke', got {mode!r}",
                "hint": "Use mode:'diag' for axis suites or mode:'smoke' for C1.",
            }
        tiers = _normalize_tiers(params or {})
        # Refuse a second concurrent run (do NOT call _read_status under the lock —
        # _read_status itself takes _state_lock → deadlock).
        with _state_lock:
            if _active_thread is not None and _active_thread.is_alive():
                already = True
            else:
                already = False
        if already:
            return {
                "ok": True,
                "already": True,
                "in_progress": True,
                "status": _read_status(),
            }

        cycle = f"gui-{mode}-{int(time.time())}"
        _write_status({
            "running": True,
            "stage": "starting",
            "started": _utc_now(),
            "finished": None,
            "last_error": None,
            "last_result_path": None,
            "mode": mode,
            "tiers": tiers,
            "cycle": cycle,
            "pid": None,
            "exit_code": None,
        })

        t = threading.Thread(
            target=_run_pipeline,
            args=(mode, tiers, cycle),
            name=f"bench-run-{mode}",
            daemon=True,
        )
        with _state_lock:
            _active_thread = t
        t.start()
        return {
            "ok": True,
            "in_progress": True,
            "mode": mode,
            "tiers": tiers,
            "cycle": cycle,
        }
    except Exception as exc:  # noqa: BLE001
        _write_status({
            "running": False,
            "stage": "error",
            "finished": _utc_now(),
            "last_error": str(exc),
        })
        return {"ok": False, "reason": f"bench_run failed: {exc}", "hint": ""}


def _bench_status(params: dict, _emit: Callable) -> dict:
    """Return the latest bench status JSON (no tokens)."""
    try:
        st = _read_status()
        return {"ok": True, **st}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"bench_status failed: {exc}", "hint": ""}


BENCH_METHODS: dict[str, Callable] = {
    "bench_run": _bench_run,
    "bench_status": _bench_status,
}

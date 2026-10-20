"""Durable local Gaussian jobs, with one cross-process GPU training slot.

Only splat_job.json is written here: capture owns session.json. Each run has a
supervisor process group, immutable batch directories, and a verified Linux
process identity. The supervisor watches its services-worker owner, so a worker
crash cannot leave an unattended trainer consuming the GPU.
"""
from __future__ import annotations

import argparse
import atexit
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from services.agent import request_context as _rc

TERMINAL = {"done", "cancelled", "failed", "interrupted"}
_CHILDREN: dict[str, tuple[Path, subprocess.Popen]] = {}
_STOP = False


def worker_activity() -> dict:
    # Snapshot existing Popen ownership only: no session files, locks or imports.
    children = [child for key, (_, child) in list(_CHILDREN.items())
                if _rc.owns_registry_key(key) and child.poll() is None]
    return {"by_kind": {"gaussian_jobs": len(children)}, "processes": children}


def _module_command(module: str) -> list[str]:
    # A compiled sidecar is not a Python interpreter: re-enter its explicit dispatcher.
    flag = "--module" if "__compiled__" in globals() or getattr(sys, "frozen", False) else "-m"
    return [sys.executable, flag, module]


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def _locked(root: Path):
    if fcntl is None:
        raise OSError("Local Gaussian jobs require Linux process supervision")
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".splat.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _identity(pid: int) -> dict:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return {}
        return {"pid": pid, "start": fields[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return {}


def _alive(identity: dict) -> bool:
    return bool(identity) and _identity(int(identity.get("pid", 0))) == identity


def _owned_group(rec: dict) -> int | None:
    pgid = rec.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        return None
    for key in ("process", "pipeline_process"):
        ident = rec.get(key) or {}
        if _alive(ident):
            try:
                if os.getpgid(ident["pid"]) == pgid:
                    return pgid
            except ProcessLookupError:
                pass
    return None


def _signal_group(rec: dict, sig: int) -> int | None:
    group = _owned_group(rec)
    if group:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass
    return group


def _reconcile(root: Path, rec: dict) -> dict:
    from services.mcp.reveng_common import _require_record_owner
    if rec:
        _require_record_owner(rec)
    for run_id, (_, child) in list(_CHILDREN.items()):
        if _CHILDREN[run_id][0] != root:
            continue
        if child.poll() is not None:
            _CHILDREN.pop(run_id, None)
    if rec and rec.get("status") not in TERMINAL and not _alive(rec.get("process") or {}):
        # A verified surviving pipeline is enough to identify the dead run's group.
        _signal_group(rec, signal.SIGKILL)
        rec.update(status="interrupted", stage="interrupted", ok=False,
                   reason="worker_interrupted", error="Gaussian worker exited before completion",
                   hint="Start a new run; the last valid preview is retained. Training restarts.",
                   updated_at=time.time())
        _atomic(root / "splat_job.json", rec)
    return rec


def active_run(root: Path) -> str | None:
    """Used by the existing mission reservation guard (no second mission catalog)."""
    if sys.platform != "linux":
        return None
    with _locked(root):
        rec = _reconcile(root, _read(root / "splat_job.json"))
        return rec.get("run_id") if rec and rec.get("status") not in TERMINAL else None


def _root(session_id: str) -> Path:
    from services.mcp.reveng_common import _session_path, _load_session
    if _load_session(session_id, merge_gaussian=False) is None:
        raise KeyError("not_found")
    root = _session_path(session_id).resolve()
    return root


def _error(reason: str, hint: str = "") -> dict:
    if _rc.in_cell() and reason == "not_found":
        return {"ok": False, "reason": reason, "error": reason, "code": reason}
    return {"ok": False, "reason": reason, "error": reason, "hint": hint}


def _bounds(bounds) -> list[float] | None:
    if bounds is None:
        return None
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 6:
        raise ValueError("crop_bounds must contain six finite numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bounds):
        raise ValueError("crop_bounds must contain six finite numbers")
    values = [float(v) for v in bounds]
    if not all(math.isfinite(v) for v in values) or any(values[i] >= values[i + 3] for i in range(3)):
        raise ValueError("crop_bounds minima must be below maxima")
    return values


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items()
            if k not in {"process", "pipeline_process", "owner", "pgid", "_owner", "_ctx", "_roots"}}


def status(session_id: str) -> dict:
    try:
        root = _root(session_id)
        with _locked(root):
            rec = _reconcile(root, _read(root / "splat_job.json"))
        return _public(rec) if rec else _error("no_splat_job")
    except (OSError, ValueError, KeyError) as exc:
        return _error("not_found" if isinstance(exc, KeyError) else str(exc))


def start(session_id: str, mode: str = "environment", live: bool = False,
          video_path: str = "", quality: str = "normal", iterations: int = 2000,
          crop_bounds=None) -> dict:
    from services.mcp import reveng_common as common
    if sys.platform != "linux":
        return _error("gaussian_jobs_require_linux")
    try:
        root = _root(session_id)
        bounds = _bounds(crop_bounds)
        if mode not in ("environment", "object") or quality not in ("draft", "normal", "high"):
            return _error("invalid_gaussian_options")
        if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
            return _error("iterations_must_be_positive")
        if video_path:
            video_path = str(Path(video_path).expanduser().resolve())
            if not Path(video_path).is_file():
                return _error("video_not_found")
        mission = common._find_session_run(session_id)
        # Same mutex/order as _reserve_mission: neither path can race the other's claim.
        with common._MISSION_LOCK, _locked(root):
            entry = common._MISSION_THREADS.get(_rc.scoped_key(session_id))
            if common._slot_busy(entry) or (mission and mission.get("status") in ("running", "paused")):
                return common._busy_envelope(session_id, entry[0] if entry else mission["run_id"])
            previous = _reconcile(root, _read(root / "splat_job.json"))
            if previous and previous.get("status") not in TERMINAL:
                return {**_public(previous), **_error("splat_already_running")}
            run_id = "splat_" + uuid.uuid4().hex
            if _rc.in_cell():
                work = Path(tempfile.mkdtemp(prefix="training-", dir=root))
            else:
                work = root / "gaussian" / run_id
                work.mkdir(parents=True)
            rec = dict(ok=True, session_id=session_id, run_id=run_id, status="waiting",
                       stage="waiting", msg="Waiting for accepted frames and the local GPU slot",
                       mode=mode, live=bool(live), quality=quality, iterations=iterations,
                       total_iterations=iterations, iteration=0, pct=0, frames=0, registered=0,
                       max_splats=2_000_000,
                       crop_bounds=bounds, bounds=previous.get("bounds"), units="reconstruction",
                       backend="brush", video_path=video_path, finish_requested=not live,
                       splat_path=previous.get("splat_path", ""),
                       source_splat_path=previous.get("source_splat_path", ""),
                       artifact_revision=previous.get("artifact_revision", 0),
                       error="", reason="", hint="", owner=_identity(os.getpid()),
                       started_at=time.time(), updated_at=time.time(), work_dir=str(work))
            rec["_owner"] = _rc.owner_key()
            rec["_ctx"] = _rc.current_context()
            rec["_roots"] = {"project": _rc.root_value("CHUDWARE_PROJECT_DIR", "/work"),
                             "save": _rc.root_value("CHUDWARE_PROJECT_SAVE_DIR", "/work")}
            if "view_camera" in previous:
                rec["view_camera"] = previous["view_camera"]
            _atomic(root / "splat_job.json", rec)
            try:
                env = dict(os.environ)
                if _rc.in_cell():
                    env["CHUDWARE_CELL"] = "1"
                with (work / "supervisor.log").open("ab") as log:
                    child = subprocess.Popen(
                        _rc.sandbox_argv([*_module_command("services.reveng.gaussian_jobs"),
                                         "--session-dir", str(root), "--run-id", run_id]),
                        cwd="/work" if _rc.in_cell() else Path(__file__).resolve().parents[2],
                        stdin=subprocess.DEVNULL,
                        env=env, stdout=log, stderr=log, start_new_session=True)
                rec.update(process=_identity(child.pid), pgid=child.pid)
                _CHILDREN[_rc.scoped_key(run_id)] = (root, child)
                _atomic(root / "splat_job.json", rec)
            except Exception as exc:
                rec.update(ok=False, status="failed", stage="failed", reason="launch_failed", error=str(exc))
                _atomic(root / "splat_job.json", rec)
            return _public(rec)
    except (OSError, ValueError, KeyError) as exc:
        return _error("not_found" if isinstance(exc, KeyError) else str(exc))


def finish(session_id: str) -> dict:
    try:
        root = _root(session_id)
        with _locked(root):
            rec = _reconcile(root, _read(root / "splat_job.json"))
            if not rec:
                return _error("no_splat_job")
            if rec.get("status") not in TERMINAL:
                rec.update(finish_requested=True, status="finalizing", msg="Final pass requested for all accepted frames",
                           updated_at=time.time())
                _atomic(root / "splat_job.json", rec)
            return _public(rec)
    except (OSError, ValueError, KeyError) as exc:
        return _error("not_found" if isinstance(exc, KeyError) else str(exc))


def cancel(session_id: str) -> dict:
    try:
        root = _root(session_id)
        with _locked(root):
            rec = _reconcile(root, _read(root / "splat_job.json"))
            if not rec:
                return _error("no_splat_job")
            if rec.get("status") in TERMINAL:
                return _public(rec)
            # Terminal record first: every progress/final publication checks it under this lock.
            rec.update(status="cancelled", stage="cancelled", ok=True,
                       msg="Cancelled; last valid preview retained", updated_at=time.time())
            _atomic(root / "splat_job.json", rec)
            group = _signal_group(rec, signal.SIGTERM)
        if group:
            # The group number cannot be reused while this owned group still exists.
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.03)
            else:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        child_key = _rc.scoped_key(rec["run_id"])
        owned = _CHILDREN.pop(child_key, None)
        if owned is not None:
            try:
                owned[1].wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                _CHILDREN[child_key] = owned
        return _public(rec)
    except (OSError, ValueError, KeyError) as exc:
        return _error("not_found" if isinstance(exc, KeyError) else str(exc))


def crop(session_id: str, bounds) -> dict:
    try:
        values = _bounds(bounds)
        root = _root(session_id)
        # A crop never touches its source and only publishes after successful validation.
        with _locked(root):
            rec = _reconcile(root, _read(root / "splat_job.json"))
            if rec and rec.get("status") not in TERMINAL:
                return _error("splat_busy", "Finish or cancel training before cropping.")
            source = rec.get("source_splat_path") or rec.get("splat_path")
            if not source or not Path(source).is_file():
                return _error("no_splat_artifact")
            from services.vision.gaussian_pipeline import crop_splat, inspect_splat
            output = root / "gaussian" / ("crop_" + uuid.uuid4().hex + ".ply")
            result = (crop_splat(source, str(output), values) if values is not None
                      else inspect_splat(source))
            if result.get("ok") is False:
                return result
            rec.update(splat_path=str(output.resolve()) if values is not None else source,
                       source_splat_path=source,
                       crop_bounds=values, bounds=result.get("bounds"),
                       artifact_revision=int(rec.get("artifact_revision", 0)) + 1,
                       updated_at=time.time())
            _atomic(root / "splat_job.json", rec)
            return {**_public(rec), "ok": True}
    except (OSError, ValueError, KeyError) as exc:
        return _error("not_found" if isinstance(exc, KeyError) else str(exc))


def _update(root: Path, run_id: str, **changes) -> dict | None:
    with _locked(root):
        rec = _read(root / "splat_job.json")
        from services.mcp.reveng_common import _require_record_owner
        if rec:
            _require_record_owner(rec)
        if rec.get("run_id") != run_id or rec.get("status") in TERMINAL:
            return None
        rec.update(changes, updated_at=time.time())
        _atomic(root / "splat_job.json", rec)
        return rec


def _images(root: Path) -> list[str]:
    extensions = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
    return [str(p.resolve()) for folder in ("images", "frames")
            if (root / folder).is_dir() for p in sorted((root / folder).iterdir())
            if p.is_file() and p.suffix.lower() in extensions]


def _publish(root: Path, run_id: str, payload: dict, finalizing: bool) -> dict | None:
    # Pipeline only exposes complete, immutable artifacts. Never accept a missing file.
    allowed = ("pct", "msg", "frames", "registered", "iteration", "total_iterations", "sparse_dir")
    changes = {key: payload[key] for key in allowed if key in payload}
    stage = payload.get("stage", "sparse")
    changes["stage"] = stage
    changes["status"] = "finalizing" if finalizing else ("training" if stage in ("training", "splat", "export", "done") else "sparse")
    with _locked(root):
        rec = _read(root / "splat_job.json")
        from services.mcp.reveng_common import _require_record_owner
        if rec:
            _require_record_owner(rec)
        if rec.get("run_id") != run_id or rec.get("status") in TERMINAL:
            return None
        artifact = payload.get("splat_path")
        if artifact and Path(artifact).is_file() and Path(artifact).stat().st_size > 0:
            artifact = str(Path(artifact).resolve())
            if artifact != rec.get("splat_path"):
                changes.update(splat_path=artifact, artifact_revision=int(rec.get("artifact_revision", 0)) + 1)
                # A newly solved batch has a new gauge. Never pair its artifact
                # with the retained preview's camera, or update a camera alone.
                if payload.get("view_camera") is not None:
                    changes["view_camera"] = payload["view_camera"]
                else:
                    rec.pop("view_camera", None)
            changes["source_splat_path"] = payload.get("source_splat_path") or artifact
            if payload.get("bounds") is not None:
                changes["bounds"] = payload["bounds"]
        rec.update(changes, updated_at=time.time())
        _atomic(root / "splat_job.json", rec)
        return rec


def _stop_requested(root: Path, run_id: str) -> bool:
    rec = _read(root / "splat_job.json")
    from services.mcp.reveng_common import _require_record_owner
    if rec:
        _require_record_owner(rec)
    return (_STOP or rec.get("run_id") != run_id or rec.get("status") in TERMINAL
            or not _alive(rec.get("owner") or {}))


def _supervise(root: Path, run_id: str) -> None:
    global _STOP
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_STOP", True))
    child = None
    gpu = None
    seen: set[str] = set()
    batch = 0
    video_pending = True
    try:
        while not _stop_requested(root, run_id):
            rec = _read(root / "splat_job.json")
            paths = _images(root)
            finalizing = bool(rec.get("finish_requested"))
            video = rec.get("video_path", "") if video_pending else ""
            ready = bool(video) or len(paths) >= 6
            fresh = len(set(paths) - seen) >= 3
            if not ready or (not finalizing and seen and not fresh):
                if finalizing and not ready:
                    _update(root, run_id, ok=False, status="failed", stage="failed",
                            reason="insufficient_frames", error="At least six accepted images are required", frames=len(paths))
                    break
                _update(root, run_id, status="watching" if seen else "waiting", stage="capture",
                        frames=len(paths), msg="Waiting for three new accepted images" if seen else "Waiting for at least six accepted images")
                time.sleep(0.4)
                continue
            if gpu is None:
                gpu = (root.parent / ".gaussian_gpu.lock").open("a")
            try:
                fcntl.flock(gpu, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _update(root, run_id, status="waiting", stage="waiting", frames=len(paths),
                        msg="Waiting for another local Gaussian job to release the GPU")
                time.sleep(0.4)
                continue
            # Capture may grow while waiting for the slot. Freeze the latest list only now.
            paths = _images(root)
            batch += 1
            work = Path(rec["work_dir"]) / f"batch_{batch:04d}"
            work.mkdir(parents=True)
            steps = rec["iterations"] if finalizing else min(rec["iterations"], 250)
            request = dict(session_id=rec["session_id"], session_dir=str(root), work_dir=str(work),
                           images=paths, video_path=video, iterations=steps, export_every=min(steps, 100),
                           mode=rec["mode"], crop_bounds=rec.get("crop_bounds"))
            _atomic(work / "request.json", request)
            _update(root, run_id, status="finalizing" if finalizing else "sparse", stage="sparse",
                    frames=len(paths), iteration=0, total_iterations=steps, pct=0,
                    msg="Starting final reconstruction" if finalizing else "Reconstructing captured snapshot")
            with (work / "pipeline.log").open("ab") as log:
                child = subprocess.Popen([*_module_command("services.vision.gaussian_pipeline"),
                                          "--request", str(work / "request.json"),
                                          "--supervised-process-group"],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            _update(root, run_id, pipeline_process=_identity(child.pid))
            while child.poll() is None and not _stop_requested(root, run_id):
                progress = _read(work / "progress.json")
                if progress:
                    _publish(root, run_id, progress, finalizing)
                else:
                    _update(root, run_id)
                time.sleep(0.25)
            if _stop_requested(root, run_id):
                break
            code = child.wait()
            child = None
            result = _read(work / "result.json")
            artifact = result.get("splat_path")
            if code != 0 or result.get("ok") is not True or not artifact or not Path(artifact).is_file():
                _update(root, run_id, ok=False, status="failed", stage="failed",
                        reason=result.get("reason") or "pipeline_failed",
                        error=result.get("msg") or f"Gaussian pipeline exited {code} without a valid result",
                        hint=result.get("hint") or f"See {work / 'pipeline.log'}")
                break
            _publish(root, run_id, result, finalizing)
            _update(root, run_id, pipeline_process={})
            video_pending = False
            # Do not consume frames arriving during training: next snapshot must include them.
            seen = set(paths)
            if video:
                extraction = result.get("video_extraction") or {}
                seen.update(str(Path(frame["path"]).resolve()) for frame in extraction.get("frames", [])
                            if isinstance(frame, dict) and frame.get("path"))
            fcntl.flock(gpu, fcntl.LOCK_UN)
            if finalizing:
                _update(root, run_id, ok=True, status="done", stage="done", pct=100,
                        msg="Gaussian reconstruction complete", error="", reason="")
                break
        if _stop_requested(root, run_id):
            _update(root, run_id, ok=False, status="interrupted", stage="interrupted",
                    reason="worker_interrupted", error="Owning worker stopped; last preview retained")
    except Exception as exc:
        _update(root, run_id, ok=False, status="failed", stage="failed", reason="supervisor_failed", error=str(exc))
    finally:
        # This supervisor owns the entire group. Even a pipeline that exited may have
        # left a Brush descendant; TERM then KILL the group before releasing the GPU.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        if child is not None:
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        # Keep the advisory GPU lock until process death, including escalation.
        os.killpg(os.getpgrp(), signal.SIGKILL)


def shutdown() -> None:
    """Reap owned supervisors without session locks, including from signal handlers.

    A signal can interrupt this thread while it holds .splat.lock. Re-opening
    that flock here would deadlock. Leave the durable record for reconciliation;
    killing the verified group prevents any subsequent artifact publication.
    """
    if _rc.in_cell():
        from services.mcp.reveng_common import _require_record_owner
        owned = []
        for key, (root, child) in list(_CHILDREN.items()):
            try:
                _require_record_owner(_read(root / "splat_job.json"))
            except KeyError:
                continue
            owned.append((key, (root, child)))
    else:
        owned = list(_CHILDREN.items())
    _shutdown_children(owned)


def _shutdown_children(owned) -> None:
    for _, (_, child) in owned:
        if child.poll() is None:
            _signal_group({"process": _identity(child.pid), "pgid": child.pid}, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    for run_id, (_, child) in owned:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
            _CHILDREN.pop(run_id, None)
        except subprocess.TimeoutExpired:
            pass


atexit.register(lambda: _shutdown_children(list(_CHILDREN.items())))

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    root = Path(args.session_dir).resolve()
    if _rc.in_cell():
        base = Path(os.environ.get("XDG_DATA_HOME", "/actor/data")).resolve()
        if base not in root.parents:
            raise ValueError("cell_refused")
        rec = _read(root / "splat_job.json")
        roots = rec.get("_roots") or {}
        with _rc.bind_context(rec.get("_ctx")), _rc.bind_roots(
                roots.get("project", "/work"), roots.get("save", "/work")):
            if _root(rec.get("session_id", "")) != root or rec.get("run_id") != args.run_id:
                raise KeyError("not_found")
            _supervise(root, args.run_id)
    else:
        _supervise(root, args.run_id)


if __name__ == "__main__":
    main()

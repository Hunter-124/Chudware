"""Admin-only rebuild / redeploy ops.

Jobs are launched DETACHED (systemd-run --user scope, with a setsid/nohup
fallback) so redeploy can restart billet-gateway without suiciding the worker
that answered the request. A flock on deploy.lock serializes concurrent runs
inside the shell scripts; the Python side also gates on deploy-status.json.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

try:
    from .config import settings
    from .auth import get_current_user
except ImportError:  # uvicorn app:app from package dir
    from config import settings
    from auth import get_current_user

log = logging.getLogger("billet-web.deploy")

router = APIRouter(prefix="/api/admin", tags=["deploy"])

_VALID_TARGETS = frozenset({"wasm", "image", "engine"})


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _logs_dir() -> Path:
    d = settings.logs_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _status_path() -> Path:
    return _logs_dir() / "deploy-status.json"


def _lock_path() -> Path:
    return _logs_dir() / "deploy.lock"


def _log_path(kind: str) -> Path:
    name = "admin-rebuild.log" if kind == "rebuild" else "admin-redeploy.log"
    return _logs_dir() / name


def _repo_dir() -> Path:
    env = (os.environ.get("BILLET_OMP_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _scripts_dir() -> Path:
    return _repo_dir() / "Webservice" / "deploy"


def _default_status() -> dict[str, Any]:
    return {"running": False, "kind": None, "started_at": None, "last": None}


def _read_status() -> dict[str, Any]:
    path = _status_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_status()
    if not isinstance(raw, dict):
        return _default_status()
    out = _default_status()
    out["running"] = bool(raw.get("running"))
    kind = raw.get("kind")
    out["kind"] = kind if isinstance(kind, str) else None
    started = raw.get("started_at")
    out["started_at"] = started if isinstance(started, (int, float)) else None
    last = raw.get("last")
    out["last"] = last if isinstance(last, dict) else None
    return out


def _write_status_starting(kind: str) -> None:
    """Best-effort mark-as-running before the detached script takes the flock.

    The shell script rewrites STATUS under the flock; this just gives the UI an
    immediate busy signal so a double-click 409s instead of double-launching.
    """
    path = _status_path()
    data = {
        "running": True,
        "kind": kind,
        "started_at": int(time.time()),
        "last": _read_status().get("last"),
    }
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data) + "\n")
        tmp.replace(path)
    except OSError as exc:
        log.warning("deploy status pre-write failed: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _launch_detached(argv: list[str], *, delay: float = 0, logfile: Path | None = None) -> None:
    """Fire-and-forget: return immediately; child runs outside this process tree.

    Prefer ``systemd-run --user --scope`` (optionally ``--on-active=Ns``) so the
    job survives the gateway cgroup. Fall back to setsid/nohup + Popen with
    ``start_new_session=True`` when systemd-run is missing or fails.
    """
    if not argv:
        raise ValueError("empty argv")
    delay_s = max(0, int(delay))
    sleep_delay = max(0.0, float(delay))

    sd = shutil.which("systemd-run")
    if sd:
        cmd = [sd, "--user", "--scope", "--quiet", "--collect"]
        if delay_s > 0:
            cmd.append(f"--on-active={delay_s}s")
        cmd.append("--")
        cmd.extend(argv)
        try:
            cp = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=str(_repo_dir()),
            )
            if cp.returncode == 0:
                log.info("deploy launched via systemd-run: %s", " ".join(argv))
                return
            log.warning(
                "systemd-run failed (rc=%s): %s — falling back to setsid/nohup",
                cp.returncode,
                (cp.stderr or cp.stdout or "").strip()[-300:],
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("systemd-run error: %s — falling back to setsid/nohup", exc)

    # Fallback: detach via start_new_session; optional sleep then exec.
    log_file = logfile or (_logs_dir() / "deploy-launch.log")
    try:
        log_fh = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
    except OSError:
        log_fh = subprocess.DEVNULL

    if sleep_delay > 0:
        # bash -c 'sleep N; exec "$@"' _ arg0 arg1 ...
        inner = ["bash", "-c", 'sleep "$1"; shift; exec "$@"', "_", str(sleep_delay), *argv]
    else:
        inner = list(argv)

    setsid = shutil.which("setsid")
    nohup = shutil.which("nohup")
    if setsid and nohup:
        final = [setsid, nohup, *inner]
    elif setsid:
        final = [setsid, *inner]
    elif nohup:
        final = [nohup, *inner]
    else:
        final = inner

    try:
        subprocess.Popen(  # noqa: S603
            final,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(_repo_dir()),
            start_new_session=True,
            close_fds=True,
        )
        log.info("deploy launched via setsid/nohup fallback: %s", " ".join(argv))
    finally:
        if log_fh is not subprocess.DEVNULL:
            try:
                log_fh.close()
            except OSError:
                pass


def _busy_response(status: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "busy": True,
            "kind": status.get("kind"),
        },
    )


@router.post("/deploy/rebuild")
async def deploy_rebuild(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    targets = body.get("targets") or ["wasm"]
    if not isinstance(targets, list) or not targets:
        raise HTTPException(status_code=400, detail="targets must be a non-empty list")
    cleaned: list[str] = []
    for t in targets:
        if not isinstance(t, str) or t not in _VALID_TARGETS:
            raise HTTPException(
                status_code=400,
                detail=f"invalid target {t!r}; expected one of wasm,image,engine",
            )
        if t not in cleaned:
            cleaned.append(t)

    status = _read_status()
    if status.get("running"):
        return _busy_response(status)

    script = _scripts_dir() / "rebuild.sh"
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"missing rebuild script: {script}")

    _write_status_starting("rebuild")
    argv = ["bash", str(script), *cleaned]
    _launch_detached(argv, delay=0, logfile=_log_path("rebuild"))
    return JSONResponse(
        status_code=202,
        content={"ok": True, "started": True, "kind": "rebuild", "log": "rebuild"},
    )


@router.post("/deploy/redeploy")
async def deploy_redeploy(request: Request) -> JSONResponse:
    _require_admin(request)
    status = _read_status()
    if status.get("running"):
        return _busy_response(status)

    script = _scripts_dir() / "redeploy.sh"
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"missing redeploy script: {script}")

    _write_status_starting("redeploy")
    # delay=2 so the 202 response flushes before billet-gateway is restarted.
    argv = ["bash", str(script)]
    _launch_detached(argv, delay=2, logfile=_log_path("redeploy"))
    return JSONResponse(
        status_code=202,
        content={"ok": True, "started": True, "kind": "redeploy"},
    )


@router.get("/deploy/status")
async def deploy_status(request: Request) -> dict[str, Any]:
    _require_admin(request)
    st = _read_status()
    return {
        "ok": True,
        "running": bool(st.get("running")),
        "kind": st.get("kind"),
        "started_at": st.get("started_at"),
        "last": st.get("last"),
    }


@router.get("/deploy/log")
async def deploy_log(
    request: Request,
    kind: str = "rebuild",
    lines: int = 200,
) -> PlainTextResponse:
    _require_admin(request)
    if kind not in ("rebuild", "redeploy"):
        raise HTTPException(status_code=400, detail="kind must be rebuild or redeploy")
    n = max(10, min(int(lines), 2000))
    path = _log_path(kind)
    if not path.is_file():
        return PlainTextResponse("")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"log read failed: {exc}") from exc
    if not text:
        return PlainTextResponse("")
    parts = text.splitlines()
    tail = parts[-n:] if len(parts) > n else parts
    body = "\n".join(tail)
    if body and not body.endswith("\n"):
        body += "\n"
    return PlainTextResponse(body)

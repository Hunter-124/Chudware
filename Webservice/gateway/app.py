"""billet-web gateway — portal, sessions, stream proxy, chat, transfer."""

from __future__ import annotations

from dataclasses import replace
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

try:
    from .auth import AuthMiddleware, User, get_current_user
    from .config import settings
    from .proxy import router as proxy_router
    from .sessions import SessionBusy, PodmanError, manager, mem_available_bytes, mem_available_gib
    from .chat import Presence, presence, router as chat_router  # noqa: F401
except ImportError:  # uvicorn app:app from package dir
    from auth import AuthMiddleware, User, get_current_user
    from config import settings
    from proxy import router as proxy_router
    from sessions import SessionBusy, PodmanError, manager, mem_available_bytes, mem_available_gib
    from chat import Presence, presence, router as chat_router  # noqa: F401

log = logging.getLogger("billet-web")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

try:
    from .auth import UserDirectory
except ImportError:
    from auth import UserDirectory

try:
    from .engine_bridge import router as engine_router, active_bridge_count
    from .workspace import router as workspace_router
    from .appserve import router as appserve_router
    from .perf_client import router as perf_router
    from .providers import router as providers_router
    from .terminal import router as terminal_router
    from .deploy_ops import router as deploy_router
except ImportError:
    from engine_bridge import router as engine_router, active_bridge_count
    from workspace import router as workspace_router
    from appserve import router as appserve_router
    from perf_client import router as perf_router
    from providers import router as providers_router
    from terminal import router as terminal_router
    from deploy_ops import router as deploy_router

settings.ensure_data_dirs()  # users_file must exist before the directory loads
user_directory = UserDirectory(settings.users_file)

app = FastAPI(title="billet-web", version="1.0.0")
app.add_middleware(AuthMiddleware, directory=user_directory)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    # Stream iframe and popout are same-origin; nothing embeds us cross-site.
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


sessions_router = APIRouter(prefix="/api/sessions", tags=["sessions"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(request: Request) -> User:
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


@app.get("/api/me")
async def whoami(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    return {"email": user.email, "display": user.display,
            "role": user.role, "heavy": user.heavy}


async def _sync_access_policy() -> dict[str, Any]:
    """Push the current users file into the Cloudflare Access policy.

    Reuses deploy/cf_provision.py --sync-users (single tested code path).
    Best-effort: a CF failure must not lose the local allowlist edit.
    """
    script = Path(__file__).resolve().parents[1] / "deploy" / "cf_provision.py"
    env = dict(os.environ, BILLET_WEB_USERS_FILE=str(settings.users_file))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), "--sync-users",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"synced": False, "detail": "cloudflare sync timed out"}
    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    ok = proc.returncode == 0
    if not ok:
        log.warning("Access policy sync failed: %s", out)
    return {"synced": ok, "detail": out[-500:]}


def _user_dict(u: User) -> dict[str, Any]:
    return {"email": u.email, "display": u.display, "role": u.role,
            "heavy": u.heavy, "project": u.project, "doc": u.doc}


def _available_projects() -> list[str]:
    try:
        return sorted(p.name[:-4] for p in settings.plm_dir.glob("*.git"))
    except OSError:
        return []


@admin_router.get("/users")
async def admin_list_users(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {
        "users": [_user_dict(u) for u in sorted(user_directory.all(), key=lambda u: u.email)],
        "projects": _available_projects(),
        "default_project": user_directory.default_project,
        "default_doc": user_directory.default_doc,
    }


_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")

# Part files the admin browser lists / the auto-open picker accepts.
_CAD_EXTS = {".step+", ".step", ".stp", ".stl", ".3mf", ".iges", ".igs", ".brep", ".aicm"}


def _valid_project(p: str) -> bool:
    """A PLM project name, or an absolute host directory (bind-mounted into sessions)."""
    if p.startswith("/"):
        return ".." not in p.split("/") and Path(p).is_dir()
    return bool(_PROJECT_RE.match(p))


def _valid_doc(d: str) -> bool:
    return bool(d) and not d.startswith("/") and ".." not in d.split("/")


@admin_router.get("/browse")
async def admin_browse(request: Request, path: str = "") -> dict[str, Any]:
    """Host-machine folder listing for the admin project picker (admin-only)."""
    _require_admin(request)
    base = Path(path).expanduser() if path else Path.home()
    try:
        base = base.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    dirs: list[str] = []
    parts: list[str] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.suffix.lower() in _CAD_EXTS:
                parts.append(entry.name)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"path": str(base),
            "parent": str(base.parent) if base.parent != base else None,
            "dirs": dirs[:500], "parts": parts[:500]}


@admin_router.put("/default-project")
async def admin_set_default_project(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Site-wide default project — every user without an explicit project inherits it."""
    _require_admin(request)
    project = str(body.get("project") or "").strip()
    doc = str(body.get("doc") or "").strip()
    if project and not _valid_project(project):
        raise HTTPException(status_code=400,
                            detail="project must be a PLM project name or an existing host folder path")
    if doc and not _valid_doc(doc):
        raise HTTPException(status_code=400, detail="invalid part path")
    user_directory.set_default_project(project, doc)
    return {"ok": True, "default_project": project, "default_doc": doc}


@admin_router.post("/users")
async def admin_add_user(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    _require_admin(request)
    email = str(body.get("email", "")).strip().lower()
    if "@" not in email or " " in email or len(email) > 254:
        raise HTTPException(status_code=400, detail="invalid email")
    role = str(body.get("role", "user"))
    if role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="role must be admin or user")
    project = str(body.get("project") or "").strip()
    if project and not _valid_project(project):
        raise HTTPException(status_code=400,
                            detail="project must be a PLM project name or an existing host folder path")
    user = User(
        email=email,
        display=str(body.get("display") or email.split("@")[0]),
        role=role,
        heavy=bool(body.get("heavy", False)),
        project=project,
    )
    user_directory.upsert(user)
    sync = await _sync_access_policy()
    return {"ok": True, "user": _user_dict(user), "access_policy": sync}


@admin_router.delete("/users/{email}")
async def admin_remove_user(email: str, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    email = email.strip().lower()
    if email == admin.email:
        raise HTTPException(status_code=400, detail="cannot remove yourself")
    if not user_directory.remove(email):
        raise HTTPException(status_code=404, detail="no such user")
    sync = await _sync_access_policy()
    return {"ok": True, "removed": email, "access_policy": sync}


def _gpu_info() -> dict[str, Any] | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        if not out:
            return None
        used, total, util = (int(x.strip()) for x in out[0].split(","))
        return {"vram_used_mib": used, "vram_total_mib": total, "util_pct": util}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


_OPS_UNITS = ("billet-cloudflared.service", "billet-monitor.timer", "billet-backup.timer")


def _unit_health() -> dict[str, str]:
    """is-active for the sibling units — tunnel down = nobody can connect."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", *_OPS_UNITS],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {u.removeprefix("billet-").split(".")[0]: (s.strip() or "unknown")
            for u, s in zip(_OPS_UNITS, out)}


@admin_router.get("/ops")
async def admin_ops(request: Request) -> dict[str, Any]:
    """Live sessions + host health for the admin dashboard."""
    _require_admin(request)
    stats = await manager.container_stats()
    now = time.time()
    sessions = []
    for s in manager.list_sessions():
        st = stats.get(s.container, {})
        sessions.append({
            "sid": s.sid, "email": s.email, "display": s.display,
            "heavy": s.heavy, "status": s.portal_status(),
            "uptime_sec": int(now - s.created_at),
            "idle_sec": int(now - s.last_activity),
            "cpu": st.get("cpu"), "mem": st.get("mem"),
        })
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return {
        "sessions": sessions,
        "host": {
            "mem_available_gib": mem_available_gib(),
            "load1": load,
            "gpu": _gpu_info(),
            "units": _unit_health(),
        },
        "refuse_new": settings.refuse_new_flag.exists(),
        "limits": {"max_sessions": settings.max_sessions, "max_heavy": settings.max_heavy,
                   "idle_timeout_sec": settings.idle_timeout_sec},
    }


@admin_router.put("/refuse-new")
async def admin_refuse_new(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Pause/resume NEW session starts (running seats unaffected).

    Same REFUSE_NEW flag file the disk monitor uses — the monitor may
    re-create it while disk is critical, so a manual clear can be overridden.
    """
    _require_admin(request)
    flag = settings.refuse_new_flag
    if bool(body.get("on")):
        flag.touch(exist_ok=True)
    else:
        flag.unlink(missing_ok=True)
    return {"ok": True, "refuse_new": flag.exists()}


@admin_router.post("/sessions/{sid}/stop")
async def admin_stop_session(sid: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    sess = await manager.stop(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="no such session")
    return {"ok": True, "sid": sid, "email": sess.email}


@admin_router.patch("/users/{email}")
async def admin_edit_user(email: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Edit display/role/heavy/project/doc in place (no remove+re-add dance).

    Email itself is immutable — it is the Access identity; add a new entry
    instead. Live sessions keep their current seat; changes apply next start.
    """
    admin = _require_admin(request)
    email = email.strip().lower()
    user = user_directory.get(email)
    if user is None:
        raise HTTPException(status_code=404, detail="no such user")
    fields: dict[str, Any] = {}
    if "display" in body:
        fields["display"] = str(body.get("display") or "").strip() or user.display
    if "role" in body:
        role = str(body.get("role") or "").strip()
        if role not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="role must be admin or user")
        if email == admin.email and role != "admin":
            raise HTTPException(status_code=400, detail="cannot demote yourself")
        fields["role"] = role
    if "heavy" in body:
        fields["heavy"] = bool(body.get("heavy"))
    if "project" in body:
        project = str(body.get("project") or "").strip()
        if project and not _valid_project(project):
            raise HTTPException(status_code=400,
                detail="project must be a PLM project name or an existing host folder path")
        fields["project"] = project
    if "doc" in body:
        fields["doc"] = str(body.get("doc") or "").strip()
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to change")
    user_directory.upsert(replace(user, **fields))
    return {"ok": True, "user": _user_dict(user_directory.get(email))}


@admin_router.get("/screenshot/{sid}")
async def admin_screenshot(sid: str, request: Request) -> Response:
    _require_admin(request)
    sess = manager.get(sid)
    if sess is None or sess.status not in {"ready", "starting"}:
        raise HTTPException(status_code=404, detail="no such session")
    png = await manager.screenshot_png(sess)
    if png is None:
        raise HTTPException(status_code=503, detail="screenshot unavailable")
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@admin_router.get("/logs")
async def admin_logs(request: Request, lines: int = 200) -> PlainTextResponse:
    """Tail the gateway journal — remote debugging without SSH."""
    _require_admin(request)
    lines = max(10, min(int(lines), 2000))
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "billet-gateway", "-n", str(lines),
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail=f"journalctl failed: {exc}") from exc
    return PlainTextResponse(out or "(empty)")


@app.get("/admin")
async def admin_page(request: Request) -> HTMLResponse:
    _require_admin(request)
    return HTMLResponse((STATIC_DIR / "admin.html").read_text())


@app.on_event("startup")
async def _startup() -> None:
    settings.ensure_data_dirs()
    await manager.adopt_running()
    await manager.start_reaper()
    log.info(
        "gateway up data_root=%s max_sessions=%s max_heavy=%s dev=%s",
        settings.data_root,
        settings.max_sessions,
        settings.max_heavy,
        settings.dev_mode,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await manager.stop_reaper()


@sessions_router.get("")
async def sessions_status(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    sess = manager.get_for_user(user.email)
    return manager.portal_payload(sess)


@sessions_router.post("")
async def sessions_start(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    # Site-wide default project: users without an explicit project inherit it
    # (and its auto-open part).
    if not user.project and user_directory.default_project:
        user = replace(user, project=user_directory.default_project,
                       doc=user.doc or user_directory.default_doc)
    try:
        sess = await manager.start(user)
    except SessionBusy as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc
    except PodmanError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("start failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return manager.portal_payload(
        sess, message="started" if sess.status == "ready" else sess.status
    )


@sessions_router.delete("")
async def sessions_stop(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    sess = await manager.stop_for_user(user.email)
    if sess is None:
        return manager.portal_payload(None, message="no session")
    return {
        "status": "stopped",
        "sid": sess.sid,
        "stream_path": None,
        "stream_url": None,
        "heavy": sess.heavy,
        "error": None,
        "message": "stopped",
    }


@sessions_router.post("/paste")
async def sessions_paste(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Type clipboard text into the caller's running session (xdotool).

    The stream iframe can't reach the container's X clipboard from the
    browser, so this is the paste path for AI keys, dimensions, chat text.
    """
    user = get_current_user(request)
    sess = manager.get_for_user(user.email)
    if sess is None or sess.status != "ready":
        raise HTTPException(status_code=409, detail="no running session")
    text = str(body.get("text") or "")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="text too large (20k max)")
    ok, msg = await manager.paste_text(sess, text)
    if not ok:
        raise HTTPException(status_code=503, detail=f"paste failed: {msg}")
    return {"ok": True, "chars": len(text)}


@sessions_router.get("/{sid}")
async def sessions_get(sid: str, request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    sess = manager.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    if sess.email != user.email and user.role != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return manager.portal_payload(sess)


async def _podman_cp(src: str, dst: str, *, timeout: float = 120.0) -> tuple[int, str]:
    """`podman cp` into/out of the user's RUNNING session container.

    HOME volumes are :U-chowned into the subuid range and keep host SELinux
    labels, so direct host-side writes produce files the container cannot
    read; `podman cp` handles both the userns mapping and labeling.
    """
    proc = await asyncio.create_subprocess_exec(
        "podman", "cp", src, dst,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="file operation timed out")
    return proc.returncode or 0, (err or out or b"").decode("utf-8", errors="replace")


def _live_session_for(request: Request) -> tuple[User, "object"]:
    user = get_current_user(request)
    sess = manager.get_for_user(user.email)
    if sess is None or sess.status not in {"starting", "ready"}:
        raise HTTPException(
            status_code=409,
            detail="no running session — start your session first, then transfer files",
        )
    return user, sess


def _safe_rel(rel: str) -> str:
    """Validate a container-side relative path under /home/user."""
    rel = (rel or "").lstrip("/")
    if not rel or rel in {".", "./"}:
        raise HTTPException(status_code=400, detail="path required")
    if "\x00" in rel or rel.startswith("~"):
        raise HTTPException(status_code=400, detail="invalid path")
    parts = Path(rel).parts
    if ".." in parts:
        raise HTTPException(status_code=400, detail="path traversal rejected")
    return str(Path(*parts))


@app.post("/transfer")
async def transfer_upload(
    request: Request,
    file: UploadFile = File(...),
    path: str = Query(default="", description="relative path under the session home"),
) -> dict[str, Any]:
    user, sess = _live_session_for(request)
    rel = _safe_rel(path or file.filename or "")

    tmp_dir = settings.data_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    size = 0
    with tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            tmp.write(chunk)
    try:
        parent = str(Path("/home/user") / Path(rel).parent)
        if parent not in {"/home/user", "/home/user/."}:
            # podman cp cannot create intermediate directories
            await manager.run_podman(
                ["exec", sess.container, "mkdir", "-p", parent], timeout=30
            )
        rc, msg = await _podman_cp(str(tmp_path), f"{sess.container}:/home/user/{rel}")
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"upload failed: {msg.strip()}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"ok": True, "path": rel, "bytes": size}


@app.get("/transfer")
async def transfer_download(
    request: Request,
    path: str = Query(..., description="relative path under the session home"),
) -> FileResponse:
    user, sess = _live_session_for(request)
    rel = _safe_rel(path)

    tmp_dir = settings.data_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False)
    tmp.close()
    rc, msg = await _podman_cp(f"{sess.container}:/home/user/{rel}", tmp.name)
    if rc != 0:
        Path(tmp.name).unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        tmp.name,
        filename=Path(rel).name,
        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)),
    )


def _image_present() -> bool:
    try:
        r = subprocess.run(
            ["podman", "image", "exists", settings.session_image],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _nvidia_vram() -> dict[str, Any] | None:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        line = r.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            return None
        return {
            "used_mib": int(float(parts[0])),
            "total_mib": int(float(parts[1])),
            "util_pct": int(float(parts[2])) if len(parts) > 2 else None,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    mem_b = mem_available_bytes()
    mem_g = mem_available_gib()
    try:
        online = await presence.list()
        presence_n = len(online) if online is not None else 0
    except Exception:  # noqa: BLE001
        presence_n = None
    return {
        "ok": True,
        "sessions": manager.active_count(),
        "heavy_sessions": manager.heavy_count(),
        "max_sessions": settings.max_sessions,
        "max_heavy": settings.max_heavy,
        "mem_available_bytes": mem_b,
        "mem_available_gib": round(mem_g, 2) if mem_g is not None else None,
        "refuse_new": settings.refuse_new_flag.exists(),
        "image": settings.session_image,
        "image_present": _image_present(),
        "vram": _nvidia_vram(),
        "presence": presence_n,
        "dev_mode": settings.dev_mode,
        "engine_bridges": active_bridge_count(),
    }


app.include_router(sessions_router)
app.include_router(proxy_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(engine_router)
app.include_router(workspace_router)
app.include_router(appserve_router)
app.include_router(perf_router)
app.include_router(providers_router)
app.include_router(terminal_router)
app.include_router(deploy_router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_model=None)
async def portal_index():
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(
        """<!DOCTYPE html>
<html><head><title>Chudware</title></head>
<body style="font-family:system-ui;background:#0f1419;color:#e7ecf1;padding:2rem">
  <h1>Chudware</h1>
  <p>Portal static files not installed yet (waiting for static/index.html).</p>
  <p>API: <code>/api/sessions</code> · <code>/healthz</code> · <code>/ws/chat</code></p>
</body></html>""",
        status_code=200,
    )


__all__ = ["app"]

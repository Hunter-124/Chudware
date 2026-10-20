"""chudware-web gateway — portal, chat, engine bridge, workspace, admin."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

try:
    from . import db, provider_broker, deployment_drain
    from .auth import AuthMiddleware, User, get_current_user
    from .config import settings
    from .chat import Presence, presence, router as chat_router, _scope_key  # noqa: F401
    from .bucket import router as bucket_router
except ImportError:  # uvicorn app:app from package dir
    import db, provider_broker, deployment_drain  # type: ignore
    from auth import AuthMiddleware, User, get_current_user
    from config import settings
    from chat import Presence, presence, router as chat_router, _scope_key  # noqa: F401
    from bucket import router as bucket_router

log = logging.getLogger("chudware-web")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

try:
    from . import mailer
    from .authn import router as authn_router
    from .oauth import router as oauth_router
except ImportError:
    import mailer  # type: ignore
    from authn import router as authn_router  # type: ignore
    from oauth import router as oauth_router  # type: ignore

try:
    from .engine_bridge import router as engine_router, active_bridge_count
    from .workspace import router as workspace_router
    from .appserve import router as appserve_router
    from .perf_client import router as perf_router
    from .providers import router as providers_router
    from .provider_keys import router as provider_keys_router
    from .provider_accounts import router as provider_accounts_router
    from .terminal import router as terminal_router
    from .deploy_ops import router as deploy_router
    from .releases import router as releases_router
    from .accounts import router as accounts_router
    from .billing import router as billing_router
    from .entitlement import _load_private_key as load_entitlement_private_key, router as entitlement_router
    from .metrics import MetricsMiddleware, router as metrics_router
    from .security import router as security_router
    from .appeals import router as appeals_router
    from .orgs import router as orgs_router
    from .portal_api import router as portal_api_router
    from .portal_serve import router as portal_serve_router
except ImportError:
    from engine_bridge import router as engine_router, active_bridge_count
    from workspace import router as workspace_router
    from appserve import router as appserve_router
    from perf_client import router as perf_router
    from providers import router as providers_router
    from provider_keys import router as provider_keys_router
    from provider_accounts import router as provider_accounts_router  # type: ignore
    from terminal import router as terminal_router
    from deploy_ops import router as deploy_router
    from releases import router as releases_router
    from accounts import router as accounts_router
    from billing import router as billing_router
    from entitlement import _load_private_key as load_entitlement_private_key, router as entitlement_router
    from metrics import MetricsMiddleware, router as metrics_router
    from security import router as security_router
    from appeals import router as appeals_router
    from orgs import router as orgs_router
    from portal_api import router as portal_api_router
    from portal_serve import router as portal_serve_router

# Marketplace. Eight routers across six modules, and the split is deliberate
# rather than incidental: three of them share the /api/studio prefix because the
# seller studio is one surface assembled from three concerns (listings, money,
# third-party import) that must not know about each other's internals. FastAPI
# resolves them as one flat table, so the paths within each are disjoint by
# design and the registration order below does not matter.
#
# Every route refuses with 404 when CHUDWARE_MARKETPLACE_ENABLED is unset, via
# marketplace_authz.marketplace_enabled_or_404 — so registering them on a
# deployment that does not sell anything costs a route-table entry and nothing
# else. That is why they are imported unconditionally rather than behind an
# `if settings.marketplace_enabled`: a conditional import makes the route table
# depend on environment, which turns "the endpoint 404s" into two
# indistinguishable causes.
try:
    from .marketplace import router as marketplace_router, studio_router as marketplace_studio_router
    from .marketplace_commerce import (
        router as commerce_router,
        studio_router as commerce_studio_router,
    )
    from .marketplace_sources import router as marketplace_sources_router
    from .marketplace_render import router as marketplace_render_router
    from .admin_billing import router as admin_billing_router
    from .admin_marketplace import router as admin_marketplace_router
except ImportError:
    from marketplace import router as marketplace_router, studio_router as marketplace_studio_router  # type: ignore
    from marketplace_commerce import (  # type: ignore
        router as commerce_router,
        studio_router as commerce_studio_router,
    )
    from marketplace_sources import router as marketplace_sources_router  # type: ignore
    from marketplace_render import router as marketplace_render_router  # type: ignore
    from admin_billing import router as admin_billing_router  # type: ignore
    from admin_marketplace import router as admin_marketplace_router  # type: ignore

settings.ensure_data_dirs()

app = FastAPI(title="chudware-web", version="1.0.0")
app.add_middleware(AuthMiddleware)
app.add_middleware(MetricsMiddleware)


# Verified against static/*.html: every <script> is an external /static file
# (no inline script blocks anywhere), so script-src needs no 'unsafe-inline';
# inline <style> blocks and style= attributes DO exist, hence style-src keeps
# it. The app is same-origin only; portals speak wss: to their own host.
_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; frame-src 'self'; frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    # Stream iframe and popout are same-origin; nothing embeds us cross-site.
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault(
        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
    )
    h.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    # setdefault: appserve already sets COOP/COEP for /app — never fight it.
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


# Keep admission outside BaseHTTPMiddleware: its in-memory response stream may
# finish before the final body reaches the actual ASGI send boundary.
app.add_middleware(deployment_drain.AdmissionMiddleware)


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


_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")

# Part files the admin browser lists.
_CAD_EXTS = {".step+", ".step", ".stp", ".stl", ".3mf", ".iges", ".igs", ".brep", ".aicm"}


def _in_project_roots(path: Path) -> bool:
    """Is ``path`` (resolved) inside one of the allowlisted project roots?

    resolve() follows symlinks, so a symlink inside a root cannot point the
    check at an outside directory.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in settings.project_roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


def _valid_project(p: str) -> bool:
    """A PLM project name, or an absolute host directory.

    Absolute paths must sit inside settings.project_roots — otherwise an admin
    could point a project at /etc or another user's home.
    """
    if p.startswith("/"):
        if ".." in p.split("/"):
            return False
        try:
            resolved = Path(p).resolve()
        except OSError:
            return False
        return resolved.is_dir() and _in_project_roots(resolved)
    return bool(_PROJECT_RE.match(p))


@admin_router.get("/browse")
async def admin_browse(request: Request, path: str = "") -> dict[str, Any]:
    """Host-machine folder listing (admin-only).

    Confined to settings.project_roots: an absolute path outside every
    allowlisted root is rejected rather than listed.
    """
    _require_admin(request)
    base = Path(path).expanduser() if path else settings.project_roots[0]
    try:
        base = base.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _in_project_roots(base):
        raise HTTPException(status_code=400,
                            detail="path is outside the allowed project roots")
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
    # Don't offer an "up" link that steps outside the allowlisted roots — the
    # follow-up browse would be rejected anyway.
    parent = str(base.parent) if base.parent != base and _in_project_roots(base.parent) else None
    return {"path": str(base),
            "parent": parent,
            "dirs": dirs[:500], "parts": parts[:500]}


def mem_available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def mem_available_gib() -> float | None:
    b = mem_available_bytes()
    if b is None:
        return None
    return b / (1024**3)


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


_OPS_UNITS = ("chudware-cloudflared.service", "chudware-monitor.timer", "chudware-backup.timer")


def _unit_health() -> dict[str, str]:
    """is-active for the sibling units — tunnel down = nobody can connect."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", *_OPS_UNITS],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {u.removeprefix("chudware-").split(".")[0]: (s.strip() or "unknown")
            for u, s in zip(_OPS_UNITS, out)}


@admin_router.get("/ops")
async def admin_ops(request: Request) -> dict[str, Any]:
    """Host health for the admin dashboard."""
    _require_admin(request)
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return {
        "host": {
            "mem_available_gib": mem_available_gib(),
            "load1": load,
            "gpu": _gpu_info(),
            "units": _unit_health(),
        },
        "refuse_new": settings.refuse_new_flag.exists(),
    }


@admin_router.put("/refuse-new")
async def admin_refuse_new(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Set/clear the REFUSE_NEW disk-pressure flag by hand.

    Same flag file the disk monitor uses — the monitor may re-create it while
    disk is critical, so a manual clear can be overridden on its next sample.
    """
    _require_admin(request)
    flag = settings.refuse_new_flag
    if bool(body.get("on")):
        flag.touch(exist_ok=True)
    else:
        flag.unlink(missing_ok=True)
    return {"ok": True, "refuse_new": flag.exists()}


# Obvious secret shapes that may land in the gateway journal (auth headers,
# CF Access tokens, env dumps). admin_logs redacts them before serving.
_REDACT_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\b")
_REDACT_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_REDACT_CFAT = re.compile(r"\bcfat_[A-Za-z0-9_-]+\b")
# ALL-CAPS env-style assignments ending in KEY/SECRET/TOKEN (uppercase-only so
# ordinary words like "monkey=banana" never false-positive).
_REDACT_ASSIGN = re.compile(
    r"\b([A-Z_][A-Z0-9_]*(?:_KEY|_SECRET|_TOKEN|KEY|SECRET|TOKEN))=([^\s]+)"
)
_REDACT_QUERY_SECRET = re.compile(
    r"([?&](?:token|code|state)=)([^&#\s]+)", re.IGNORECASE
)


def _redact_secrets(text: str) -> str:
    """Strip obvious credential shapes from journal output before serving it."""
    text = _REDACT_JWT.sub("[REDACTED-JWT]", text)
    text = _REDACT_BEARER.sub(r"\1[REDACTED]", text)
    text = _REDACT_CFAT.sub("[REDACTED-CFAT]", text)
    text = _REDACT_ASSIGN.sub(r"\1=[REDACTED]", text)
    text = _REDACT_QUERY_SECRET.sub(r"\1[REDACTED]", text)
    return text


@admin_router.get("/logs")
async def admin_logs(request: Request, lines: int = 200) -> PlainTextResponse:
    """Tail the gateway journal — remote debugging without SSH."""
    _require_admin(request)
    lines = max(10, min(int(lines), 2000))
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", settings.gateway_unit, "-n", str(lines),
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail=f"journalctl failed: {exc}") from exc
    return PlainTextResponse(_redact_secrets(out) if out else "(empty)")


@app.get("/admin")
async def admin_page(request: Request) -> HTMLResponse:
    _require_admin(request)
    return HTMLResponse((STATIC_DIR / "admin.html").read_text())


@app.on_event("startup")
async def _startup() -> None:
    settings.ensure_data_dirs()
    deployment_drain.startup()
    # The online signing key is a systemd credential, never generated or read
    # from an EnvironmentFile. Validate it before opening any serving resource.
    load_entitlement_private_key()
    # Store first: open the pool and bring the schema up to date. This raises
    # if PostgreSQL is unreachable or a migration fails, and that is the point
    # — a gateway serving requests against a missing or half-migrated schema
    # looks healthy and answers wrongly.
    db.init()
    await provider_broker.startup()
    # Outbound mail is queued into mail_outbox by mailer.send() and delivered
    # only by this task. Without it every verification and reset link ever
    # generated sits in the table unsent, and registration silently dead-ends.
    mailer.start_drain()
    log.info("gateway up data_root=%s dev=%s", settings.data_root, settings.dev_mode)


@app.on_event("shutdown")
async def _shutdown() -> None:
    deployment_drain.controller().stop_admission()
    try:
        from .marketplace_render import wait_pending
    except ImportError:
        from marketplace_render import wait_pending
    await wait_pending()
    # Mail marks rows sent through db.tx(); retain the pool until delivery has
    # actually finished, not merely until its asyncio wrapper was canceled.
    await mailer.stop_drain()
    await deployment_drain.controller().wait_work()
    await provider_broker.shutdown()
    db.close_pool()


# nvidia-smi on a box with a dead driver (e.g. livingroom 1050 Ti, no module)
# still burns its full timeout before failing. Cache both hit and miss so
# /healthz never serializes the event loop behind a multi-second shell-out.
_VRAM_CACHE_TTL_S = 30.0
_vram_cache: tuple[float, dict[str, Any] | None] | None = None


def _nvidia_vram() -> dict[str, Any] | None:
    global _vram_cache
    now = time.monotonic()
    if _vram_cache is not None and (now - _vram_cache[0]) < _VRAM_CACHE_TTL_S:
        return _vram_cache[1]
    result: dict[str, Any] | None = None
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                result = {
                    "used_mib": int(float(parts[0])),
                    "total_mib": int(float(parts[1])),
                    "util_pct": int(float(parts[2])) if len(parts) > 2 else None,
                }
    except (OSError, ValueError, subprocess.SubprocessError):
        result = None
    _vram_cache = (now, result)
    return result


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    # /healthz is on the public allowlist for uptime probes — anonymous
    # callers get a bare liveness result ONLY. Host memory/GPU telemetry and
    # dev_mode stay behind auth (AuthMiddleware soft-attaches the user when
    # valid credentials ride along on a public path).
    if getattr(request.state, "user", None) is None:
        return {"ok": True}
    mem_b = mem_available_bytes()
    mem_g = mem_available_gib()
    try:
        user = get_current_user(request)
        scope_key = None if user.role == "admin" else _scope_key(request, user.email.lower())
        online = await presence.list(scope_key)
        presence_n = len(online) if online is not None else 0
    except Exception:  # noqa: BLE001
        presence_n = None
    # The nvidia-smi shell-out must not block the asyncio loop — a wedged
    # driver previously made every /healthz take ~5s and stalled concurrent
    # portal traffic on the same worker.
    vram = await asyncio.to_thread(_nvidia_vram)
    result = {
        "ok": True,
        "mem_available_bytes": mem_b,
        "mem_available_gib": round(mem_g, 2) if mem_g is not None else None,
        "refuse_new": settings.refuse_new_flag.exists(),
        "vram": vram,
        "presence": presence_n,
        "dev_mode": settings.dev_mode,
    }
    if user.role == "admin":
        result["engine_bridges"] = active_bridge_count()
        result["provider_streams"] = provider_broker.active_request_count()
    return result


app.include_router(chat_router)
app.include_router(deployment_drain.router)
app.include_router(authn_router)
app.include_router(oauth_router)
app.include_router(admin_router)
app.include_router(engine_router)
app.include_router(workspace_router)
app.include_router(appserve_router)
app.include_router(perf_router)
app.include_router(providers_router)
app.include_router(provider_keys_router)
app.include_router(provider_accounts_router)
app.include_router(terminal_router)
app.include_router(deploy_router)
app.include_router(releases_router)
app.include_router(bucket_router)
app.include_router(orgs_router)
app.include_router(accounts_router)
app.include_router(billing_router)
app.include_router(entitlement_router)
app.include_router(security_router)
app.include_router(appeals_router)
app.include_router(metrics_router)
# Marketplace, before portal_serve. portal_serve owns the catch-all "/" route,
# so anything registered after it is shadowed — that is why it is last in this
# list and why these go above it rather than at the end.
app.include_router(marketplace_router)
app.include_router(commerce_router)
app.include_router(marketplace_studio_router)
app.include_router(commerce_studio_router)
app.include_router(marketplace_sources_router)
app.include_router(marketplace_render_router)
app.include_router(admin_billing_router)
app.include_router(admin_marketplace_router)
app.include_router(portal_api_router)
app.include_router(portal_serve_router)


class _AdminGatedStatic(StaticFiles):
    """admin.* portal assets are the privileged UI — never serve them to
    standard users. The mutations were already gated server-side; this keeps
    the admin UI itself unreadable. /admin reads admin.html from disk and the
    admin's own browser fetches admin.js here — both pass for role='admin'."""

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            name = (scope.get("path") or "").rsplit("/", 1)[-1]
            # admin.html/admin.js plus the admin_-prefixed terminal/ops/wm
            # assets (all referenced only by admin.html).
            if name.startswith("admin"):
                state = scope.get("state") or {}
                user = (
                    state.get("user")
                    if isinstance(state, dict)
                    else getattr(state, "user", None)
                )
                if user is None or getattr(user, "role", "") != "admin":
                    resp = PlainTextResponse("admin required", status_code=403)
                    await resp(scope, receive, send)
                    return
        await super().__call__(scope, receive, send)


if STATIC_DIR.is_dir():
    app.mount("/static", _AdminGatedStatic(directory=str(STATIC_DIR)), name="static")




__all__ = ["app"]

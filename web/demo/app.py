"""Chudware public demo front door (Starlette, loopback listener behind cloudflared).

Own routes:  GET /   GET /demo/status   POST /demo/join   POST /demo/end   GET /demo/health
Proxied, for an admitted visitor only:  /app/*  /engine (WebSocket)  /workspace/*
/files/*  /reveng/*  /healthz

Run:  python -m uvicorn demo.app:create_app --factory --host 127.0.0.1 --port 8300
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import cookies
from .broker import ProviderBroker
from .config import DemoConfig
from .manager import DemoManager, JoinRefused
from .metrics import Metrics
from .podman import PodmanRuntime
from .proxy import Proxy, same_origin

log = logging.getLogger("chudware-demo")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    "waiting.js": "application/javascript; charset=utf-8",
    "waiting.css": "text/css; charset=utf-8",
    "admin.js": "application/javascript; charset=utf-8",
    "admin.css": "text/css; charset=utf-8",
}
# The front door's own pages carry no inline script/style and load nothing off-origin.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
REFUSAL_STATUS_TEXT = {
    "queue_full": "The queue is full.", "ip_limit": "Another session from this network is active or queued.",
    "rate_limited": "Too many join attempts.", "session_ending": "Your previous session is still closing.",
}
PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


class _SecurityHeaders:
    """Defaults only: headers the inner gateway already set on proxied responses win."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Content-Security-Policy", PAGE_CSP)
                headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            await send(message)

        await self.app(scope, receive, with_headers)


class _MetricsMiddleware:
    """Counts every HTTP request once, by route class and status (see metrics.py
    for what counts as a pageview). WebSockets are counted as events by the proxy
    path itself, not here."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            self.metrics.event("websocket_connection")
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 0}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            request = Request(scope)
            self.metrics.record(str(scope.get("method", "GET")), request.url.path,
                                status["code"] or 500, ip_tag(client_address(request)))


def client_address(conn: HTTPConnection) -> str:
    """The visitor's address: CF-Connecting-IP, trusted only from a loopback peer.

    The listener is bound to 127.0.0.1 behind cloudflared, so a loopback peer is the
    tunnel and the header is Cloudflare's. A direct (non-loopback) peer gets no such trust.
    """
    peer = conn.client.host if conn.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        peer_ip = None
    claimed = conn.headers.get("cf-connecting-ip", "").strip()
    if claimed and peer_ip is not None and any(peer_ip in net for net in _LOOPBACK):
        with contextlib.suppress(ValueError):
            return str(ipaddress.ip_address(claimed))
    return peer or "0.0.0.0"


def ip_tag(address: str) -> str:
    """Stable, non-reversible key for per-client limits; IPv6 clients share their /64."""
    key = address
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            else:
                ip = ipaddress.ip_network(f"{ip}/64", strict=False).network_address
        key = str(ip)
    return hashlib.sha256(b"chudware-demo-ip\0" + key.encode("utf-8", "replace")).hexdigest()[:24]


class FrontDoor:
    def __init__(self, cfg: DemoConfig, manager: DemoManager, metrics: Metrics | None = None):
        self.cfg, self.manager = cfg, manager
        self.metrics = metrics or Metrics()
        self.index_html = (STATIC_DIR / "index.html").read_bytes()
        admin_path = STATIC_DIR / "admin.html"
        self.admin_html = admin_path.read_bytes() if admin_path.exists() else b""
        self.static = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_TYPES
                       if (STATIC_DIR / name).exists()}
        self._admin_logins: dict[str, Any] = {}

    def session_of(self, conn: HTTPConnection) -> str | None:
        return cookies.verify(self.cfg.cookie_key, conn.cookies.get(cookies.COOKIE_NAME))

    @staticmethod
    def _json(body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
        out = {"Cache-Control": "no-store", **(headers or {})}
        return JSONResponse(body, status_code=status, headers=out)

    @staticmethod
    def _origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or same_origin(origin, request.headers.get("host", ""))

    # -- pages ---------------------------------------------------------------------------
    async def index(self, request: Request) -> Response:
        if self.manager.admitted(self.session_of(request)) is not None:
            return RedirectResponse("/app/", status_code=307, headers={"Cache-Control": "no-store"})
        return Response(self.index_html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    async def static_file(self, request: Request) -> Response:
        name = request.path_params["name"]
        if name not in self.static:
            return Response("not found", status_code=404)
        return Response(self.static[name], media_type=STATIC_TYPES[name],
                        headers={"Cache-Control": "no-cache"})

    # -- JSON API ------------------------------------------------------------------------
    async def status(self, request: Request) -> Response:
        sid = self.session_of(request)
        try:
            wait = min(max(int(request.query_params.get("wait", "0")), 0), self.cfg.status_wait_max)
        except ValueError:
            wait = 0
        if sid is not None and wait:
            await self.manager.wait_for_change(sid, wait)
        return self._json(self.manager.status(sid))

    async def join(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        sid = self.session_of(request)
        fresh = sid is None
        if fresh:
            sid = cookies.new_sid()
        address = client_address(request)
        try:
            self.manager.join(sid, ip_tag(address), address)
        except JoinRefused as refused:
            self.metrics.event(f"join_refused:{refused.code}")
            headers = {"Retry-After": str(refused.retry_after)} if refused.retry_after else None
            return self._json({"error": {"code": refused.code,
                                         "message": REFUSAL_STATUS_TEXT.get(refused.code, refused.code)}},
                              refused.status, headers)
        self.metrics.event("join")
        response = self._json(self.manager.status(sid))
        if fresh:
            response.set_cookie(cookies.COOKIE_NAME, cookies.sign(self.cfg.cookie_key, sid),
                                max_age=self.cfg.cookie_max_age, path="/", secure=True,
                                httponly=True, samesite="lax")
        return response

    async def end(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        sid = self.session_of(request)
        if sid is not None:
            await self.manager.end(sid)
        return self._json(self.manager.status(sid))

    async def health(self, request: Request) -> Response:
        status = self.manager.status(None)
        return self._json({"ok": True, "active": status["active_sessions"],
                           "queued": status["queue_length"], "max": status["max_sessions"]})

    async def delete_workspace(self, request: Request) -> Response:
        """The visitor erases their own saved workspace (cookie-named, sleeping only)."""
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        sid = self.session_of(request)
        if sid is None or not await self.manager.delete_workspace(sid):
            return self._json({"error": {"code": "no_sleeping_workspace"}}, 409)
        self.metrics.event("workspace_deleted_by_visitor")
        return self._json(self.manager.status(sid))

    # -- admin -------------------------------------------------------------------
    # /admin exists only when CHUDWARE_DEMO_ADMIN_KEY is configured. The key is
    # never a URL or a visitor cookie: a correct POST mints a separate short-lived
    # admin cookie (HMAC under the admin key), logins are rate-limited per client
    # network, and every admin response is no-store under the page CSP.
    def _admin_on(self) -> bool:
        return bool(self.cfg.admin_key and self.admin_html)

    def _admin_ok(self, request: Request) -> bool:
        return self._admin_on() and cookies.verify_admin(
            self.cfg.admin_key, request.cookies.get(cookies.ADMIN_COOKIE_NAME), time.time())

    async def admin_page(self, request: Request) -> Response:
        if not self._admin_on():
            return Response("not found", status_code=404)
        return Response(self.admin_html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    async def admin_login(self, request: Request) -> Response:
        if not self._admin_on():
            return Response("not found", status_code=404)
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        tag = ip_tag(client_address(request))
        now = time.time()
        window = self._admin_logins.setdefault(tag, collections.deque())
        while window and window[0] <= now - self.cfg.admin_login_window:
            window.popleft()
        if len(window) >= self.cfg.admin_login_limit:
            self.metrics.event("admin_login_refused:rate_limited")
            return self._json({"error": {"code": "rate_limited"}}, 429,
                              {"Retry-After": str(self.cfg.admin_login_window)})
        window.append(now)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        supplied = str(body.get("key", "") if isinstance(body, dict) else "").encode("utf-8")
        if not hmac.compare_digest(supplied, self.cfg.admin_key):
            self.metrics.event("admin_login_refused:bad_key")
            return self._json({"error": {"code": "bad_key"}}, 401)
        window.clear()
        self.metrics.event("admin_login")
        response = self._json({"ok": True})
        response.set_cookie(cookies.ADMIN_COOKIE_NAME,
                            cookies.sign_admin(self.cfg.admin_key,
                                               int(now) + self.cfg.admin_cookie_seconds),
                            max_age=self.cfg.admin_cookie_seconds, path="/", secure=True,
                            httponly=True, samesite="strict")
        return response

    async def admin_logout(self, request: Request) -> Response:
        response = self._json({"ok": True})
        response.delete_cookie(cookies.ADMIN_COOKIE_NAME, path="/", secure=True,
                               httponly=True, samesite="strict")
        return response

    def _host_snapshot(self) -> dict[str, Any]:
        from .manager import host_capacity
        mem, _ = host_capacity(self.manager._disk_path)
        disk = shutil.disk_usage(self.cfg.workspace_root if self.cfg.workspace_root.exists()
                                 else self.cfg.state_dir)
        load = os.getloadavg()
        return {"mem_available_bytes": mem, "disk_free_bytes": disk.free,
                "disk_total_bytes": disk.total, "load": [round(v, 2) for v in load],
                "cpu_count": os.cpu_count()}

    async def admin_metrics(self, request: Request) -> Response:
        if not self._admin_ok(request):
            return self._json({"error": {"code": "admin_auth_required"}}, 401)
        snap = self.manager.snapshot()
        broker = getattr(self.manager, "broker", None)
        claude = getattr(broker, "claude", None)
        ai = {"free_available": self.broker_free(), "claude_available": self.broker_claude(),
              "claude_today_usd": round(claude.day_micros / 1_000_000, 4) if claude else None,
              "claude_month_usd": round(claude.month_micros / 1_000_000, 4) if claude else None}
        return self._json({"now": time.time(), "traffic": self.metrics.snapshot(),
                           "host": self._host_snapshot(), "ai": ai, **snap,
                           "limits": {"max_sessions": self.cfg.max_active,
                                      "max_queue": self.cfg.max_queue,
                                      "idle_seconds": self.cfg.idle_seconds,
                                      "workspace_quota_bytes": self.cfg.workspace_quota_bytes,
                                      "workspaces_total_bytes": self.cfg.workspaces_total_bytes}})

    def broker_free(self) -> bool:
        try:
            return bool(self.manager.broker.free_ai_available())
        except Exception:  # noqa: BLE001
            return False

    def broker_claude(self) -> bool:
        try:
            return bool(self.manager.broker.claude_available())
        except Exception:  # noqa: BLE001
            return False

    async def _admin_body_sid(self, request: Request) -> str | None:
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return None
        sid = body.get("sid") if isinstance(body, dict) else None
        return sid if isinstance(sid, str) and sid else None

    async def admin_session_end(self, request: Request) -> Response:
        if not self._admin_ok(request):
            return self._json({"error": {"code": "admin_auth_required"}}, 401)
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        sid = await self._admin_body_sid(request)
        # Accept the truncated id shown in the table as well as a full one.
        full = next((s for s in list(self.manager.entries) + list(self.manager.dormant)
                     if s == sid or s.startswith(str(sid))), None) if sid else None
        if full is None or not await self.manager.end(full, "admin_ended"):
            return self._json({"error": {"code": "no_such_session"}}, 404)
        self.metrics.event("admin_session_ended")
        return self._json({"ok": True})

    async def admin_workspace_delete(self, request: Request) -> Response:
        if not self._admin_ok(request):
            return self._json({"error": {"code": "admin_auth_required"}}, 401)
        if not self._origin_ok(request):
            return self._json({"error": {"code": "cross_origin"}}, 403)
        sid = await self._admin_body_sid(request)
        full = next((s for s in list(self.manager.dormant) if s == sid or s.startswith(str(sid))),
                    None) if sid else None
        if full is None or not await self.manager.delete_workspace(full):
            return self._json({"error": {"code": "no_such_workspace"}}, 404)
        self.metrics.event("admin_workspace_deleted")
        return self._json({"ok": True})


def create_app(config: DemoConfig | None = None, *, runtime: Any = None, broker: Any = None,
               clock: Callable[[], float] = time.time, capacity_probe: Any = None,
               port_free: Callable[[int], bool] | None = None,
               http_client: httpx.AsyncClient | None = None, run_background: bool = True) -> Starlette:
    """Build the app. With no arguments it reads CHUDWARE_DEMO_* from the environment."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if config is None:
        config = DemoConfig.from_env()
        # The keys stay in this process only: scrub them from the environment that child
        # processes (podman, conmon) would otherwise inherit.
        for name in ("OPENROUTER_KEY", "NVIDIA_KEY", "ANTHROPIC_KEY", "COOKIE_KEY", "ADMIN_KEY"):
            os.environ.pop("CHUDWARE_DEMO_" + name, None)
    cfg = config
    if not cfg.ai_enabled:
        log.warning("neither CHUDWARE_DEMO_OPENROUTER_KEY nor CHUDWARE_DEMO_NVIDIA_KEY is set: "
                    "the free-model lane answers 503, the rest works")
    if not cfg.cookie_key_persistent:
        log.warning("CHUDWARE_DEMO_COOKIE_KEY is not set: sessions cannot survive a front-door restart")
    if broker is None:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.state_dir.chmod(0o700)
        broker = ProviderBroker(cfg, budget_path=cfg.state_dir / "ai-budget.json",
                                claude_path=cfg.state_dir / "claude-spend.json", clock=clock)
    runtime = runtime or PodmanRuntime(cfg)
    kwargs: dict[str, Any] = {"clock": clock, "capacity_probe": capacity_probe}
    if port_free is not None:
        kwargs["port_free"] = port_free
    manager = DemoManager(cfg, runtime, broker, **kwargs)
    metrics = Metrics(cfg.state_dir / "metrics.json", clock=clock)
    manager.event_hook = metrics.event
    door = FrontDoor(cfg, manager, metrics)
    proxy = Proxy(cfg, manager, door.session_of, client=http_client)
    manager.add_end_hook(proxy.close_session)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await manager.startup(run_loop=run_background)
        try:
            yield
        finally:
            await manager.shutdown()
            metrics.flush()
            await broker.aclose()
            await proxy.aclose()

    routes = [
        Route("/", door.index, methods=["GET", "HEAD"]),
        Route("/demo/static/{name}", door.static_file, methods=["GET", "HEAD"]),
        Route("/demo/status", door.status, methods=["GET"]),
        Route("/demo/join", door.join, methods=["POST"]),
        Route("/demo/end", door.end, methods=["POST"]),
        Route("/demo/workspace/delete", door.delete_workspace, methods=["POST"]),
        Route("/demo/health", door.health, methods=["GET"]),
        Route("/admin", door.admin_page, methods=["GET", "HEAD"]),
        Route("/admin/login", door.admin_login, methods=["POST"]),
        Route("/admin/logout", door.admin_logout, methods=["POST"]),
        Route("/admin/api/metrics", door.admin_metrics, methods=["GET"]),
        Route("/admin/api/session/end", door.admin_session_end, methods=["POST"]),
        Route("/admin/api/workspace/delete", door.admin_workspace_delete, methods=["POST"]),
        Route("/app", proxy.http, methods=PROXY_METHODS),
        Route("/app/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/workspace/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/files/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/reveng/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/healthz", proxy.http, methods=["GET", "HEAD"]),
        WebSocketRoute("/engine", proxy.websocket),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[
        Middleware(_MetricsMiddleware, metrics=metrics),
        Middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.public_hosts)),
        Middleware(_SecurityHeaders),
    ])
    app.state.demo = SimpleNamespace(cfg=cfg, manager=manager, broker=broker, runtime=runtime,
                                     proxy=proxy, door=door, metrics=metrics)
    return app

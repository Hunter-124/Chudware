"""Chudware public demo front door (Starlette, loopback listener behind cloudflared).

Own routes:  GET /   GET /demo/status   POST /demo/join   POST /demo/end   GET /demo/health
Proxied, for an admitted visitor only:  /app/*  /engine (WebSocket)  /workspace/*
/files/*  /reveng/*  /healthz

Run:  python -m uvicorn demo.app:create_app --factory --host 127.0.0.1 --port 8300
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import os
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
from .podman import PodmanRuntime
from .proxy import Proxy, same_origin

log = logging.getLogger("chudware-demo")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    "waiting.js": "application/javascript; charset=utf-8",
    "waiting.css": "text/css; charset=utf-8",
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
    def __init__(self, cfg: DemoConfig, manager: DemoManager):
        self.cfg, self.manager = cfg, manager
        self.index_html = (STATIC_DIR / "index.html").read_bytes()
        self.static = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_TYPES}

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
            headers = {"Retry-After": str(refused.retry_after)} if refused.retry_after else None
            return self._json({"error": {"code": refused.code,
                                         "message": REFUSAL_STATUS_TEXT.get(refused.code, refused.code)}},
                              refused.status, headers)
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


def create_app(config: DemoConfig | None = None, *, runtime: Any = None, broker: Any = None,
               clock: Callable[[], float] = time.time, capacity_probe: Any = None,
               port_free: Callable[[int], bool] | None = None,
               http_client: httpx.AsyncClient | None = None, run_background: bool = True) -> Starlette:
    """Build the app. With no arguments it reads CHUDWARE_DEMO_* from the environment."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if config is None:
        config = DemoConfig.from_env()
        # The key stays in this process only: scrub it from the environment that child
        # processes (podman, conmon) would otherwise inherit.
        os.environ.pop("CHUDWARE_DEMO_OPENROUTER_KEY", None)
        os.environ.pop("CHUDWARE_DEMO_COOKIE_KEY", None)
    cfg = config
    if not cfg.ai_enabled:
        log.warning("CHUDWARE_DEMO_OPENROUTER_KEY is not set: the AI lane answers 503, the rest works")
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
    door = FrontDoor(cfg, manager)
    proxy = Proxy(cfg, manager, door.session_of, client=http_client)
    manager.add_end_hook(proxy.close_session)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await manager.startup(run_loop=run_background)
        try:
            yield
        finally:
            await manager.shutdown()
            await broker.aclose()
            await proxy.aclose()

    routes = [
        Route("/", door.index, methods=["GET", "HEAD"]),
        Route("/demo/static/{name}", door.static_file, methods=["GET", "HEAD"]),
        Route("/demo/status", door.status, methods=["GET"]),
        Route("/demo/join", door.join, methods=["POST"]),
        Route("/demo/end", door.end, methods=["POST"]),
        Route("/demo/health", door.health, methods=["GET"]),
        Route("/app", proxy.http, methods=PROXY_METHODS),
        Route("/app/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/workspace/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/files/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/reveng/{path:path}", proxy.http, methods=PROXY_METHODS),
        Route("/healthz", proxy.http, methods=["GET", "HEAD"]),
        WebSocketRoute("/engine", proxy.websocket),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.public_hosts)),
        Middleware(_SecurityHeaders),
    ])
    app.state.demo = SimpleNamespace(cfg=cfg, manager=manager, broker=broker, runtime=runtime,
                                     proxy=proxy, door=door)
    return app

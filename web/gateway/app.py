"""Chudware web gateway: serves the WASM client and bridges it to its engine cell.

Run from this directory::

    uvicorn app:app --host 127.0.0.1 --port 8300

There are no accounts. Every request acts as the configured local operator
(identity.py), so keep the listener on loopback or put an authenticating reverse
proxy in front of it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    from .appserve import router as appserve_router
    from .cells import container_runtime, resolve_workspace
    from .config import CellBackend, settings
    from .engine_bridge import router as engine_router
    from .identity import SameOriginMiddleware
    from .limits import RequestBodyLimitMiddleware
    from .workspace import router as workspace_router
except ImportError:  # uvicorn app:app from the package directory
    from appserve import router as appserve_router  # type: ignore
    from cells import container_runtime, resolve_workspace  # type: ignore
    from config import CellBackend, settings  # type: ignore
    from engine_bridge import router as engine_router  # type: ignore
    from identity import SameOriginMiddleware  # type: ignore
    from limits import RequestBodyLimitMiddleware  # type: ignore
    from workspace import router as workspace_router  # type: ignore

log = logging.getLogger("chudware-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings.ensure_dirs()
    if settings.cell_backend == CellBackend.CONTAINER:
        container_runtime(resolve_workspace())
    log.info("gateway up: operator=%s workspace=%s app=%s",
             settings.operator, settings.workspace_root, settings.app_dir)
    yield


app = FastAPI(title="chudware-web", version="1.0.0", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

# Gateway-generated responses carry no inline script; appserve sets its own,
# wasm-capable policy for /app and these defaults never override it.
_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)


class _SecurityHeadersMiddleware:
    """Pure ASGI, so receive exceptions reach the multipart parser's cleanup.

    BaseHTTPMiddleware groups receive failures in an ExceptionGroup, obscuring
    the size exception that Starlette must catch to close its spooled files.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("X-Frame-Options", "SAMEORIGIN")
                h.setdefault("Content-Security-Policy", _CSP)
                h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            await send(message)

        await self.app(scope, receive, with_headers)


# The last middleware added is the outermost. Order, outside in: refuse unknown
# Host headers, refuse cross-site writes and WebSockets, cap request bodies
# before any parser runs, then add response headers.
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SameOriginMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/app/", status_code=307)


app.include_router(engine_router)
app.include_router(workspace_router)
app.include_router(appserve_router)


__all__ = ["app"]

"""The single local operator, and the guard that keeps other sites out.

The gateway has no accounts or sign-in. Every request acts as the one operator
named by CHUDWARE_WEB_OPERATOR, so anyone who can reach the listener has that
operator's workspace and engine. The listener therefore binds to loopback by
default; exposing it to a network means putting it behind a reverse proxy that
authenticates (see web/README.md).

Loopback alone does not stop the operator's own browser from being used against
it. Two checks cover that:

* ``allowed_hosts`` (Starlette's TrustedHostMiddleware, wired in app.py) refuses
  any Host header the operator did not configure, which defeats DNS rebinding.
* :class:`SameOriginMiddleware` refuses WebSocket handshakes and state-changing
  requests whose Origin is not this gateway. Browsers always send Origin on both
  and a page cannot forge it; clients that send no Origin (curl, scripts) are not
  browser-driven and pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

try:
    from .config import settings
except ImportError:  # uvicorn app:app from the package directory
    from config import settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Operator:
    name: str

    @property
    def workspace_key(self) -> str:
        return "user:" + self.name


def current_operator() -> Operator:
    return Operator(settings.operator)


def same_origin(origin: str, host: str) -> bool:
    """True when ``origin`` names the host the request was sent to."""
    parsed = urlsplit(origin)
    return (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
            and not parsed.path and parsed.netloc.lower() == host.lower())


class SameOriginMiddleware:
    """Refuse cross-origin WebSocket handshakes and unsafe HTTP methods."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        guarded = scope["type"] == "websocket" or (
            scope["type"] == "http" and scope["method"] not in SAFE_METHODS)
        if guarded:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin is not None and not same_origin(origin, headers.get("host", "")):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await PlainTextResponse("cross-origin request refused", 403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = ["Operator", "SameOriginMiddleware", "current_operator", "same_origin"]

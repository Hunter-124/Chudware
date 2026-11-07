"""Fakes shared by the front-door tests."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from starlette.testclient import TestClient

from demo.app import create_app
from demo.config import DemoConfig
from demo.cookies import COOKIE_NAME
from demo.podman import ContainerInfo, PodmanError, SessionSpec

COOKIE_KEY = b"k" * 32


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_cfg(tmp_path: Path, **overrides: Any) -> DemoConfig:
    values: dict[str, Any] = dict(
        cookie_key=COOKIE_KEY, cookie_key_persistent=True,
        session_root=tmp_path / "sessions", state_dir=tmp_path / "state",
        openrouter_key="sk-or-owner-key", tick_seconds=0.05, status_wait_max=5)
    values.update(overrides)
    return DemoConfig(**values)


class FakeRuntime:
    """Stands in for PodmanRuntime; records calls, simulates containers."""

    def __init__(self) -> None:
        self.started: list[SessionSpec] = []
        self.removed: list[str] = []
        self.containers: dict[str, ContainerInfo] = {}
        self.ready_results: list[bool] = []     # popped per start; default True
        self.hang_ready: set[str] = set()       # sids whose readiness wait never returns
        self.start_error: Exception | None = None
        self.healthy_result = True

    async def start(self, spec: SessionSpec) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(spec)
        self.containers[spec.name] = ContainerInfo(
            spec.name, "running", spec.sid, spec.port, spec.started, spec.ip_tag)

    async def wait_ready(self, spec: SessionSpec, timeout: float) -> bool:
        if spec.sid in self.hang_ready:
            await asyncio.sleep(3600)
        return self.ready_results.pop(0) if self.ready_results else True

    async def healthy(self, spec: SessionSpec, *, timeout: float = 3.0) -> bool:
        return self.healthy_result

    async def remove(self, name: str) -> None:
        self.removed.append(name)
        self.containers.pop(name, None)

    async def list_labelled(self) -> list[ContainerInfo]:
        return list(self.containers.values())

    async def storage_path(self) -> Path | None:
        return None

    async def is_running(self, name: str) -> bool:
        return name in self.containers

    async def logs_tail(self, name: str, lines: int = 40) -> str:
        return ""


class FakeBroker:
    def __init__(self, remaining: int | None = 45) -> None:
        self.remaining = remaining
        self.opened: dict[str, tuple[Path, Any]] = {}
        self.closed: list[str] = []

    async def open_session(self, sid: str, run_dir: Path, usage: Any, usage_path: Path | None = None) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.opened[sid] = (run_dir, usage)

    async def close_session(self, sid: str) -> None:
        self.closed.append(sid)

    def ai_remaining_today(self) -> int | None:
        return self.remaining

    async def aclose(self) -> None:
        pass


def sid_for(n: int) -> str:
    return f"{n:024x}"


def ip_for(n: int) -> str:
    return f"{n:024x}"[::-1]


class FakeInner:
    """A stand-in for a session container's gateway, on a real loopback port."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.ws_headers: list[dict[str, str]] = []
        self.port = 0
        self.blob = bytes(range(256)) * 4096   # 1 MiB
        routes = [
            Route("/healthz", self.healthz),
            Route("/app", self.app_redirect),
            Route("/app/", self.app_index),
            Route("/app/{path:path}", self.app_index),
            Route("/workspace/{path:path}", self.workspace, methods=["GET", "POST"]),
            Route("/files/{path:path}", self.workspace),
            Route("/reveng/{path:path}", self.workspace),
            WebSocketRoute("/engine", self.engine),
        ]
        self.app = Starlette(routes=routes)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", ws="auto"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "FakeInner":
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                break
            threading.Event().wait(0.01)
        assert self.server.started, "fake inner gateway did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)

    def _record(self, request: Request) -> None:
        self.seen.append({"method": request.method, "path": request.url.path,
                          "query": request.url.query, "headers": dict(request.headers)})

    async def healthz(self, request: Request) -> Response:
        self._record(request)
        return JSONResponse({"ok": True, "demo": True})

    async def app_redirect(self, request: Request) -> Response:
        self._record(request)
        return Response(status_code=307, headers={"location": f"http://127.0.0.1:{self.port}/app/"})

    async def app_index(self, request: Request) -> Response:
        self._record(request)
        if request.url.path.endswith("/blob"):
            async def body():
                for i in range(0, len(self.blob), 65536):
                    yield self.blob[i:i + 65536]
            return StreamingResponse(body(), media_type="application/octet-stream",
                                     headers={"content-length": str(len(self.blob))})
        return Response(
            b"<!doctype html><title>inner app</title>", media_type="text/html",
            headers={"Cross-Origin-Opener-Policy": "same-origin",
                     "Cross-Origin-Embedder-Policy": "require-corp",
                     "Cross-Origin-Resource-Policy": "same-origin",
                     "Set-Cookie": "leak=1; Domain=chudware.com", "Server": "inner-gateway",
                     "X-Inner": "yes"})

    async def workspace(self, request: Request) -> Response:
        self._record(request)
        body = await request.body()
        return JSONResponse({"len": len(body), "echo": body.decode("utf-8", "replace")})

    async def engine(self, ws: WebSocket) -> None:
        self.ws_headers.append(dict(ws.headers))
        await ws.accept()
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await ws.send_text("echo:" + message["text"])
                elif message.get("bytes") is not None:
                    await ws.send_bytes(b"echo:" + message["bytes"])
        except WebSocketDisconnect:
            return


GIB = 1024 ** 3
PLENTY = (64 * GIB, 500 * GIB)
LOW = (1 * GIB, 500 * GIB)
ORIGIN = "https://app.chudware.com"


class Visitor:
    """One browser: its own cookie and client address, sharing the test's TestClient.

    The TestClient's own cookie jar is cleared on every call so visitors never leak into
    each other; the cookie travels as an explicit header, exactly as a browser sends it.
    """

    def __init__(self, client: TestClient, ip: str, cookie: str | None = None):
        self.client, self.ip, self.cookie = client, ip, cookie

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"cf-connecting-ip": self.ip, "host": "app.chudware.com"}
        if self.cookie:
            headers["cookie"] = f"{COOKIE_NAME}={self.cookie}"
        headers.update(extra or {})
        return headers

    def request(self, method: str, url: str, **kwargs: Any):
        headers = self.headers(kwargs.pop("headers", None))
        self.client.cookies.clear()
        response = self.client.request(method, url, headers=headers, **kwargs)
        match = re.match(rf"{re.escape(COOKIE_NAME)}=([^;]+)", response.headers.get("set-cookie", ""))
        if match:
            self.cookie = match.group(1)
        return response

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def join(self):
        return self.post("/demo/join", headers={"origin": ORIGIN})

    def end(self):
        return self.post("/demo/end", headers={"origin": ORIGIN})

    def state(self) -> str:
        return self.get("/demo/status").json()["state"]

    def wait_for(self, state: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        status: dict[str, Any] = {}
        while time.time() < deadline:
            status = self.get("/demo/status").json()
            if status["state"] == state:
                return status
            time.sleep(0.03)
        raise AssertionError(f"never reached {state!r}; last status {status}")

    def ws_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return self.headers({"origin": ORIGIN, **(extra or {})})


@contextmanager
def front_door(tmp_path: Path, *, capacity: tuple[Any, Any] = PLENTY, clock: Any = None,
               peer: str = "127.0.0.1", inner: "FakeInner | None" = None,
               **cfg_overrides: Any) -> Iterator[SimpleNamespace]:
    """The real app (real tick loop) over fake podman/broker; yields handles to poke at."""
    state = {"capacity": capacity}
    runtime, broker = FakeRuntime(), FakeBroker()
    if inner is not None:
        cfg_overrides.update(port_base=inner.port, port_count=8)
    cfg = make_cfg(tmp_path, **cfg_overrides)
    app = create_app(cfg, runtime=runtime, broker=broker, clock=clock or time.time,
                     capacity_probe=lambda: state["capacity"], port_free=lambda port: True)
    with TestClient(app, base_url=ORIGIN, client=(peer, 50000), follow_redirects=False) as client:
        yield SimpleNamespace(client=client, app=app, manager=app.state.demo.manager, runtime=runtime,
                              broker=broker, state=state, cfg=cfg,
                              visitor=lambda ip="198.51.100.1", cookie=None: Visitor(client, ip, cookie))


__all__ = ["COOKIE_KEY", "FakeBroker", "FakeClock", "FakeInner", "FakeRuntime", "LOW", "ORIGIN",
           "PLENTY", "PodmanError", "Visitor", "front_door", "ip_for", "make_cfg", "sid_for"]

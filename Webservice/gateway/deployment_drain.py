"""Protected deployment maintenance and accepted-work admission lifetimes.

This is separate from the disk monitor's auto-cleared REFUSE_NEW flag. Only the
operator API changes the durable lease; no timeout releases it or cancels work.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

try:
    from .config import settings
except ImportError:
    from config import settings

LEASE_ID = re.compile(r"[0-9a-f]{64}\Z")
CELL_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_STATE = 1 << 20
_context = contextvars.ContextVar("gateway_admission", default=None)
router = APIRouter(prefix="/api/_ops/deployment-drain", tags=["deployment"])
_OPS_PATHS = frozenset(f"/api/_ops/deployment-drain/{name}"
                      for name in ("begin", "status", "exit-cells", "release"))


class _OpsAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn normally logs the full request target, including the private
        # ownership token in GET status. Keep the route, never its query.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split("?", 1)[0] in _OPS_PATHS:
                args[2] = args[2].split("?", 1)[0]
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(_OpsAccessFilter())


def _lease(value):
    if not isinstance(value, str) or not LEASE_ID.fullmatch(value):
        raise HTTPException(400, "invalid deployment lease")
    return value


class Admission:
    def __init__(self, owner, kind):
        self.owner, self.kind, self.active = owner, kind, True

    def close(self):
        with self.owner.lock:
            if self.active:
                self.active = False
                self.owner.counts[self.kind] -= 1

    @contextmanager
    def bind(self):
        marker = _context.set(self)
        try:
            yield self
        finally:
            _context.reset(marker)


class AdmissionController:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.lock = threading.RLock()
        self.counts = {"http_requests": 0, "gateway_jobs": 0}
        self.state = None
        self.unsafe = False
        self.stopping = False
        self.operations = asyncio.Lock()
        with self.lock:
            self._reload()

    def _directory(self):
        # The root is gateway state, never an actor/workspace mount. Walk every
        # component without following links and pin the private child directory.
        try:
            from .cells import directory_beneath
        except ImportError:
            from cells import directory_beneath
        tenant_roots = [settings.workspace_root, settings.bucket_root, settings.cells_root]
        if settings.university_workspace_root:
            tenant_roots.append(Path(settings.university_workspace_root))
        destination = self.root / ".deployment-maintenance"
        if any(destination.is_relative_to(Path(base).absolute()) for base in tenant_roots):
            raise ValueError("tenant-mounted maintenance state")
        parent = directory_beneath(self.root)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("unsafe gateway state root")
            try:
                os.mkdir(".deployment-maintenance", 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            fd = os.open(".deployment-maintenance", os.O_RDONLY | os.O_DIRECTORY |
                         os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                os.close(fd)
                raise ValueError("unsafe maintenance directory")
            return fd
        finally:
            os.close(parent)

    @staticmethod
    def _validate(value):
        if (not isinstance(value, dict) or set(value) != {"schema", "lease_id", "admission", "cells", "exit_requested"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or not isinstance(value["lease_id"], str) or not LEASE_ID.fullmatch(value["lease_id"])
                or value["admission"] not in {"open", "closed"} or not isinstance(value["cells"], dict)
                or type(value["exit_requested"]) is not bool):
            raise ValueError("invalid maintenance state")
        for cell, row in value["cells"].items():
            if (not CELL_ID.fullmatch(cell) or not isinstance(row, dict)
                    or set(row) != {"generation", "invocation"}
                    or type(row["generation"]) is not int or row["generation"] < 0
                    or not isinstance(row["invocation"], str) or not CELL_ID.fullmatch(row["invocation"])):
                raise ValueError("invalid maintenance cell fence")
        return value

    def _reload(self):
        if self.unsafe:
            return
        fd = None
        try:
            fd = self._directory()
            try:
                file = os.open("lease.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW |
                               os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                if self.state is not None:
                    raise ValueError("maintenance state disappeared")
                self.state = None
                return
            with os.fdopen(file, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size > _MAX_STATE):
                    raise ValueError("unsafe maintenance lease")
                raw = handle.read(_MAX_STATE + 1)
            if len(raw) > _MAX_STATE:
                raise ValueError("oversized maintenance lease")
            self.state = self._validate(json.loads(raw))
        except (OSError, ValueError, TypeError, RecursionError, HTTPException):
            self.unsafe = True
        finally:
            if fd is not None:
                os.close(fd)

    def _persist(self, value):
        fd = None
        temp = ".lease-" + secrets.token_hex(16)
        try:
            raw = json.dumps(self._validate(value), separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > _MAX_STATE:
                raise ValueError("oversized maintenance state")
            fd = self._directory()
            file = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                           os.O_CLOEXEC, 0o600, dir_fd=fd)
            with os.fdopen(file, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp, "lease.json", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            self.state = value
        except (OSError, ValueError, HTTPException):
            self.unsafe = True
            raise HTTPException(503, "deployment maintenance state unavailable") from None
        finally:
            if fd is not None:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
                os.close(fd)

    @property
    def closed(self):
        with self.lock:
            return self.stopping or self.unsafe or (self.state is not None and self.state["admission"] == "closed")

    def stop_admission(self):
        with self.lock:
            self.stopping = True

    def acquire(self, kind="gateway_jobs", *, inherit=True):
        if kind not in self.counts:
            raise ValueError("unknown admission kind")
        with self.lock:
            parent = _context.get() if inherit else None
            accepted = parent is not None and parent.owner is self and parent.active
            if self.closed and not accepted:
                raise HTTPException(503, "deployment maintenance")
            self.counts[kind] += 1
            return Admission(self, kind)

    def accepted(self):
        with self.lock:
            parent = _context.get()
            return parent is not None and parent.owner is self and parent.active

    def require(self, lease_id):
        _lease(lease_id)
        with self.lock:
            self._reload()
            if self.unsafe:
                raise HTTPException(503, "deployment maintenance state unavailable")
            if self.state is None:
                raise HTTPException(404, "deployment lease not found")
            if self.state["lease_id"] != lease_id:
                raise HTTPException(409, "deployment lease conflict")
            return self.state

    def begin(self, lease_id):
        _lease(lease_id)
        with self.lock:
            self._reload()
            if self.unsafe:
                raise HTTPException(503, "deployment maintenance state unavailable")
            if self.closed:
                self.require(lease_id)
                return
            self._persist({"schema": 1, "lease_id": lease_id, "admission": "closed",
                           "cells": {}, "exit_requested": False})

    def fence(self, lease_id, cell_id, invocation, generation):
        with self.lock:
            state = self.require(lease_id)
            value = {**state, "cells": {**state["cells"], cell_id: {
                "invocation": invocation, "generation": generation}}}
            self._persist(value)

    def request_exit(self, lease_id):
        with self.lock:
            state = self.require(lease_id)
            self._persist({**state, "exit_requested": True})

    def release(self, lease_id):
        with self.lock:
            state = self.require(lease_id)
            self._persist({**state, "admission": "open", "cells": {}, "exit_requested": False})

    def snapshot(self):
        with self.lock:
            return dict(self.counts)

    async def wait_work(self):
        # Executor threads can outlive a canceled HTTP wrapper. Never close DB
        # resources or reset admission state while that accepted work remains.
        while any(self.snapshot().values()):
            await asyncio.sleep(0.05)


_controller = None


def startup():
    global _controller
    _controller = AdmissionController(settings.data_root)


def controller():
    global _controller
    if _controller is None:
        startup()
    return _controller


@contextmanager
def work(*, inherit=True):
    admission = controller().acquire(inherit=inherit)
    try:
        with admission.bind():
            yield admission
    finally:
        admission.close()


async def run_blocking(fn, *args, **kwargs):
    """Keep accepted executor work/context alive through cancellation/disconnect."""
    admission = controller().acquire()
    def accepted():
        try:
            with admission.bind():
                return fn(*args, **kwargs)
        finally:
            admission.close()
    try:
        task = asyncio.create_task(asyncio.to_thread(accepted))
    except BaseException:
        admission.close()
        raise
    # Covers executor submission failure before accepted() starts. A canceled
    # asyncio wrapper is not proof that an executor thread has stopped.
    task.add_done_callback(lambda done: admission.close() if not done.cancelled() else None)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


class AdmissionMiddleware:
    """Pure ASGI lifetime, including streaming bodies and entire WebSockets."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] not in {"http", "websocket"} or scope.get("path") in _OPS_PATHS
                or scope.get("path") == "/healthz"
                or scope.get("path") == "/api/me" and scope.get("method") == "GET"):
            await self.app(scope, receive, send)
            return
        try:
            admission = controller().acquire(
                "http_requests" if scope["type"] == "http" else "gateway_jobs", inherit=False)
        except HTTPException:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013,
                            "reason": "deployment maintenance"})
            else:
                await JSONResponse({"detail": "deployment maintenance"}, status_code=503)(scope, receive, send)
            return
        try:
            with admission.bind():
                await self.app(scope, receive, send)
        finally:
            admission.close()


def _admin(request):
    # Auth imports mail delivery, which uses admission before auth is initialized.
    try:
        from .auth import get_current_user
    except ImportError:
        from auth import get_current_user
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "admin only")


def _body(body):
    if not isinstance(body, dict) or set(body) != {"lease_id"}:
        raise HTTPException(400, "invalid deployment lease request")
    return _lease(body["lease_id"])


async def _counters(lease_id, *, exit_cells=False, reopen=False):
    try:
        from . import cells, engine_bridge, provider_broker
    except ImportError:
        import cells, engine_bridge, provider_broker
    owner = controller()
    state = owner.require(lease_id)
    local = owner.snapshot()
    local["engine_bridges"] = engine_bridge.active_bridge_count()
    local["provider_streams"] = provider_broker.active_request_count()
    producers_idle = all(type(n) is int and n == 0 for n in local.values())
    native = await cells.deployment_census(
        close=state["admission"] == "closed" and producers_idle and not reopen,
        exit_cells=exit_cells, reopen=reopen, fences=state["cells"],
        record=lambda cell, invocation, generation: owner.fence(lease_id, cell, invocation, generation))
    complete = native["complete"] and all(type(n) is int and n >= 0 for n in local.values())
    quiescent = complete and producers_idle and native["quiescent"]
    return {"schema": 1, "lease_id": lease_id,
            "admission": "closed" if owner.closed else "open",
            "complete": complete, "quiescent": quiescent, **local,
            **{key: native[key] for key in ("cell_jobs", "cells_active", "cells_unknown", "cells_exiting")},
            "diagnostics": native["diagnostics"]}


@router.post("/begin")
async def begin(request: Request, body: dict):
    _admin(request)
    lease_id = _body(body)
    async with controller().operations:
        controller().begin(lease_id)
        return await _counters(lease_id)


@router.get("/status")
async def status(request: Request, lease_id: str):
    _admin(request)
    async with controller().operations:
        return await _counters(_lease(lease_id))


@router.post("/exit-cells")
async def exit_cells(request: Request, body: dict):
    _admin(request)
    lease_id = _body(body)
    async with controller().operations:
        result = await _counters(lease_id)
        state = controller().require(lease_id)
        if result["admission"] != "closed":
            raise HTTPException(409, "deployment admission is not closed")
        if not result["complete"] or not result["quiescent"]:
            if state["exit_requested"]:
                return result  # An acknowledged retry cannot certify exiting units idle.
            raise HTTPException(409, "deployment work is not quiescent")
        controller().request_exit(lease_id)
        return await _counters(lease_id, exit_cells=True)


@router.post("/release")
async def release(request: Request, body: dict):
    _admin(request)
    lease_id = _body(body)
    async with controller().operations:
        state = controller().require(lease_id)
        if state["admission"] == "closed":
            result = await _counters(lease_id, reopen=True)
            if result["cells_unknown"] or result["cells_exiting"]:
                raise HTTPException(503, "deployment cells cannot safely reopen")
            controller().release(lease_id)
        return await _counters(lease_id)

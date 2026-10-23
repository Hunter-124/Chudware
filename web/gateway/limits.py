"""Request-size admission and descriptor-pinned file I/O shared by the routes.

Every byte ceiling is enforced before a parser buffers input, and every file
operation runs through no-follow directory descriptors so a symlink planted in
the workspace cannot redirect a read or write outside it.
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from starlette.background import BackgroundTask
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    from .config import settings
except ImportError:  # uvicorn app:app from the package directory
    from config import settings


# Wire-byte ceilings apply before FastAPI parses JSON or multipart data.
SMALL_REQUEST_BYTES = 64 * 1024
WORKSPACE_CHAT_BYTES = 5 * 1024 * 1024


def request_body_cap(path: str) -> int:
    if path == "/workspace/upload":
        return settings.max_upload_bytes
    if path == "/workspace/chat":
        return WORKSPACE_CHAT_BYTES
    return SMALL_REQUEST_BYTES


class _RequestBodyTooLarge(HTTPException, MultiPartException):
    """Also trigger Starlette's multipart temporary-file cleanup."""

    def __init__(self) -> None:
        HTTPException.__init__(self, 413, "request body too large")
        self.message = self.detail


class RequestBodyLimitMiddleware:
    """Outermost pure ASGI guard, including Content-Length-free/chunked input.

    No body is prebuffered here. Oversized chunks never reach a parser.
    Multipart parsing translates MultiPartException to 400, so send also
    preserves the authoritative 413. WebSocket and lifespan scopes pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = request_body_cap(scope["path"])
        lengths = []
        for key, value in scope.get("headers", ()):
            if key.lower() == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = -1
                lengths.append(length)
        if lengths and (min(lengths) < 0 or len(set(lengths)) != 1):
            await JSONResponse({"detail": "invalid content-length"}, 400)(scope, receive, send)
            return
        if lengths and lengths[0] > cap:
            await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            return
        total = 0
        exceeded = False
        replied = False

        async def bounded_receive() -> Message:
            nonlocal total, exceeded
            if exceeded:
                raise _RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > cap:
                    exceeded = True
                    raise _RequestBodyTooLarge()
            return message

        async def bounded_send(message: Message) -> None:
            nonlocal replied
            if exceeded:
                if not replied:
                    replied = True
                    await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
                return
            await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except _RequestBodyTooLarge:
            if not replied:
                await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)


def enforce_upload_size(content_length: int | None) -> int:
    """Reject an oversized upload before a single byte is buffered."""
    cap = settings.max_upload_bytes
    if content_length is not None and content_length > cap:
        raise HTTPException(
            status_code=413,
            detail=f"upload too large — {cap // (1024 * 1024)} MiB max",
        )
    return cap


async def read_bounded_body(request: Request, cap: int, detail: str) -> bytes:
    """Bound incoming bytes before accumulating a JSON/body payload."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content-length")
        if length < 0:
            raise HTTPException(status_code=400, detail="invalid content-length")
        if length > cap:
            raise HTTPException(status_code=413, detail=detail)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > cap:
            raise HTTPException(status_code=413, detail=detail)
        body.extend(chunk)
    return bytes(body)


@asynccontextmanager
async def bounded_form(request: Request, cap: int, *, max_files: int = 1, max_fields: int = 1):
    """Cap multipart bytes before Starlette buffers/spools each incoming chunk.

    MultiPartException triggers the parser's temporary-file cleanup. Translate
    only our size failure to 413; malformed multipart retains its normal 400.
    """
    total = 0
    oversized = False

    async def receive():
        nonlocal total, oversized
        message = await request.receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > cap:
                oversized = True
                raise MultiPartException("upload too large")
        return message

    bounded = Request(request.scope, receive=receive)
    try:
        async with bounded.form(max_files=max_files, max_fields=max_fields) as form:
            yield form
    except Exception:
        if oversized:
            raise HTTPException(status_code=413, detail="upload too large") from None
        raise


# A fixed pool of locks avoids an ever-growing lock table. This serializes this
# gateway process only; the engine cell writes the same tree independently.
_STORAGE_WRITE_LOCKS = tuple(threading.RLock() for _ in range(64))


def storage_write_lock(root: Path):
    """Hold across the validation and mutation of one workspace root."""
    return _STORAGE_WRITE_LOCKS[hash(root) % len(_STORAGE_WRITE_LOCKS)]


def list_open_directory(fd: int, prefix: str, cap: int) -> tuple[list[dict], list[dict]]:
    """List bounded non-symlink entries using an already pinned directory fd."""
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(fd) as scan:
            for entry in scan:
                if entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    continue
                entries.append((entry.name, st))
                if len(entries) >= cap:
                    break
    finally:
        os.close(fd)
    dirs, files = [], []
    for name, st in sorted(entries, key=lambda row: row[0].lower()):
        path = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(st.st_mode):
            dirs.append({"name": name, "path": path})
        else:
            files.append({"name": name, "path": path, "size": st.st_size, "mtime": int(st.st_mtime)})
    return dirs, files


def stream_open_file(fd: int, st: os.stat_result, *, filename: str = "",
                     headers: dict[str, str] | None = None) -> StreamingResponse:
    """Stream an already pinned descriptor, never reopen a raceable path."""
    file = os.fdopen(fd, "rb")
    response_headers = dict(headers or {})
    response_headers["Content-Length"] = str(st.st_size)
    if filename:
        response_headers["Content-Disposition"] = "attachment; filename*=utf-8''" + quote(filename)

    def chunks():
        remaining = st.st_size
        try:
            while remaining:
                chunk = file.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            file.close()

    return StreamingResponse(chunks(), media_type="application/octet-stream",
                             headers=response_headers, background=BackgroundTask(file.close))


def store_upload(root: Path, relative: str, upload, *, cap: int) -> int:
    """Atomically publish an upload through no-follow directory descriptors.

    The caller holds storage_write_lock. The engine may mutate subdirectories
    concurrently, so no path-based reopen is safe here.
    """
    try:
        from . import cells
    except ImportError:
        import cells
    parts = relative.split("/")
    if relative.startswith("/") or "\\" in relative or any(p in {"", ".", ".."} for p in parts):
        raise HTTPException(400, "bad relative path")
    parent = cells.directory_beneath(root, "/".join(parts[:-1]))
    temporary = ""
    try:
        name = parts[-1]
        try:
            previous = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(previous.st_mode):
                raise HTTPException(400, "not a regular file")
        temporary = ".upload-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        size = 0
        with os.fdopen(fd, "wb") as file:
            while chunk := upload.file.read(1 << 20):
                size += len(chunk)
                if size > cap:
                    raise HTTPException(413, "upload too large")
                file.write(chunk)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        return size
    except OSError:
        raise HTTPException(403, "unsafe filesystem path") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


__all__ = [
    "RequestBodyLimitMiddleware",
    "SMALL_REQUEST_BYTES",
    "WORKSPACE_CHAT_BYTES",
    "bounded_form",
    "enforce_upload_size",
    "list_open_directory",
    "read_bounded_body",
    "request_body_cap",
    "storage_write_lock",
    "store_upload",
    "stream_open_file",
]

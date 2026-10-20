"""Shared abuse controls: per-user rate limits, upload caps, and disk quotas.

Every multi-tenant guard in the gateway routes through this module so the
policy lives in one place. The gateway is a single uvicorn process, so plain
in-process dictionaries are the correct storage — no Redis, no shared state
across workers to keep coherent.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import stat
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Deque
from urllib.parse import quote

from fastapi import HTTPException, Request
from starlette.formparsers import MultiPartException
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

try:
    from .config import settings
    from .plans import limits_for
except ImportError:  # uvicorn app:app from the package dir
    from config import settings
    from plans import limits_for


class RateLimiter:
    """Sliding-window request counter keyed by an arbitrary string.

    Buckets are pruned lazily on access and swept wholesale once the table
    grows past ``max_keys``, so an attacker cycling keys cannot grow it without
    bound (the gateway runs under ``MemoryMax=1G``).
    """

    __slots__ = ("limit", "window", "max_keys", "_hits", "_lock")

    def __init__(self, limit: int, window_sec: float, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window = window_sec
        self.max_keys = max_keys
        self._hits: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, bucket: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def check(self, key: str) -> bool:
        """Record a hit for ``key``. False once the window budget is spent."""
        with self._lock:
            now = time.monotonic()
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self.max_keys:
                    self._sweep(now)
                bucket = self._hits.setdefault(key, deque())
            self._prune(bucket, now)
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` regains budget (>= 1 when throttled)."""
        with self._lock:
            bucket = self._hits.get(key)
            if not bucket:
                return 0
            return max(1, int(self.window - (time.monotonic() - bucket[0])) + 1)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, b in self._hits.items() if not b or b[-1] < cutoff]:
            self._hits.pop(key, None)

    def enforce(self, key: str, what: str = "requests") -> None:
        """Raise 429 when ``key`` is over budget."""
        if not self.check(key):
            raise HTTPException(
                status_code=429,
                detail=f"too many {what} — slow down",
                headers={"Retry-After": str(self.retry_after(key))},
            )


# Shared limiters. Keys are always the authenticated email, never a client-
# supplied value, so one account cannot exhaust another's budget.
api_writes = RateLimiter(settings.rate_writes_per_min, 60.0)
uploads = RateLimiter(settings.rate_uploads_per_min, 60.0)
chat_messages = RateLimiter(settings.rate_chat_per_min, 60.0)

# Pre-authentication limiters. Every other limiter above keys on the
# AUTHENTICATED email, which is exactly what an unauthenticated endpoint does
# not have — so these two key on the SUBMITTED email and on the client address
# instead, and are deliberately far tighter as a result.
#
# Both are enforced before any password hashing: argon2 at 64 MiB a go is a
# denial-of-service amplifier if an attacker can spend it for free, and the
# per-email budget is what actually caps a targeted guess at 5 tries per
# 15 minutes. The per-address budget is 4x looser so that a shared NAT, an
# office, or a university does not lock itself out over one forgetful user.
login_by_email = RateLimiter(5, 900.0)
login_by_ip = RateLimiter(20, 900.0)


def user_slug(email: str) -> str:
    """Return a filesystem-safe, human-diagnosable account slug.

    Existing escaped slugs up to 120 characters remain unchanged. Longer
    identities retain a readable prefix plus a full digest of the complete
    normalized address, so truncation cannot merge colliding prefixes.
    """
    normalized = email.strip().lower()
    out = []
    for ch in normalized:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append(f"~{ord(ch):02x}")
    readable = "".join(out)
    if not readable or readable in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid account identity")
    if len(readable) <= 120:
        return readable
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{readable[:54]}~z{digest}"


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
async def bounded_form(request: Request, cap: int, *, max_files: int = 1):
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
        async with bounded.form(max_files=max_files, max_fields=1) as form:
            yield form
    except Exception:
        if oversized:
            raise HTTPException(status_code=413, detail="upload too large") from None
        raise


def dir_size_bytes(root: Path, *, cap: int | None = None) -> int:
    """Recursive on-disk size of ``root``, short-circuiting once past ``cap``.

    Symlinks are never followed, so a user cannot make their own quota appear
    small by pointing at an empty directory, nor make a scan walk the host.
    """
    # Lazy import keeps cells' identity helpers free to import the rate policy.
    try:
        from . import cells
    except ImportError:
        import cells
    try:
        first = cells.directory_beneath(root)
    except HTTPException as exc:
        if exc.status_code == 404:
            return 0
        raise
    total = 0
    stack = [first]
    try:
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                stack.append(os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY |
                                                     os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current))
                            elif stat.S_ISREG(info.st_mode):
                                total += info.st_size
                        except OSError:
                            continue
            finally:
                os.close(current)
            if cap is not None and total > cap:
                return total
        return total
    finally:
        for fd in stack:
            os.close(fd)


# Bounded stripes avoid an ever-growing lock table for arbitrary account roots.
# This serializes this gateway process, not external Box/native filesystem writers.
_STORAGE_WRITE_LOCKS = tuple(threading.RLock() for _ in range(64))


def storage_write_lock(root: Path):
    """Hold across quota preflight, mutation and final accounting of one root."""
    return _STORAGE_WRITE_LOCKS[hash(root) % len(_STORAGE_WRITE_LOCKS)]


def enforce_disk_quota(
    email: str,
    plan: str,
    root: Path,
    incoming_bytes: int = 0,
    replaced_bytes: int = 0,
) -> None:
    """Raise 507 when an account's tree would exceed its plan storage limit."""
    quota = limits_for(plan).storage_quota_bytes
    used = dir_size_bytes(root, cap=quota + max(0, replaced_bytes))
    if used + max(0, incoming_bytes) - max(0, replaced_bytes) > quota:
        raise HTTPException(
            status_code=507,
            detail=(
                f"storage quota exceeded for {email} — "
                f"{quota // (1024 * 1024)} MiB on {plan} "
                f"({used // (1024 * 1024)} MiB used)"
            ),
        )

def list_open_directory(fd: int, prefix: str, cap: int) -> tuple[list[dict], list[dict]]:
    """List bounded non-symlink entries using an authorized directory fd."""
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
    """Stream an already authorized descriptor, never reopen a raceable path."""
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


def store_upload(root: Path, relative: str, upload, *, cap: int, email: str,
                 plan: str, total: int = 0, create_parents: bool = False) -> int:
    """Quota-check and atomically publish an upload through no-follow dir fds.

    Caller holds storage_write_lock across a batch. Tenant code may mutate
    subdirectories concurrently, so no path-based reopen is safe here.
    """
    try:
        from . import cells
    except ImportError:
        import cells
    parts = relative.split("/")
    if relative.startswith("/") or "\\" in relative or any(p in {"", ".", ".."} for p in parts):
        raise HTTPException(400, "bad relative path")
    parent = cells.directory_beneath(root)
    temporary = ""
    try:
        for component in parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=parent)
            os.close(parent)
            parent = child
        name = parts[-1]
        try:
            previous_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            previous = 0
        else:
            if not stat.S_ISREG(previous_info.st_mode):
                raise HTTPException(400, "not a regular file")
            previous = previous_info.st_size
        enforce_disk_quota(email, plan, root, incoming_bytes=upload.size or 0, replaced_bytes=previous)
        temporary = ".upload-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        size = 0
        with os.fdopen(fd, "wb") as file:
            while chunk := upload.file.read(1 << 20):
                size += len(chunk)
                if total + size > cap:
                    raise HTTPException(413, "upload too large")
                file.write(chunk)
        enforce_disk_quota(email, plan, root, replaced_bytes=previous)
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
    "RateLimiter",
    "storage_write_lock",
    "api_writes",
    "uploads",
    "chat_messages",
    "login_by_email",
    "login_by_ip",
    "user_slug",
    "enforce_upload_size",
    "dir_size_bytes",
    "enforce_disk_quota",
    "read_bounded_body",
    "bounded_form",
    "stream_open_file",
    "list_open_directory",
    "store_upload",
]

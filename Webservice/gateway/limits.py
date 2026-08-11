"""Shared abuse controls: per-user rate limits, upload caps, and disk quotas.

Every multi-tenant guard in the gateway routes through this module so the
policy lives in one place. The gateway is a single uvicorn process, so plain
in-process dictionaries are the correct storage — no Redis, no shared state
across workers to keep coherent.
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Deque

from fastapi import HTTPException

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

    __slots__ = ("limit", "window", "max_keys", "_hits")

    def __init__(self, limit: int, window_sec: float, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window = window_sec
        self.max_keys = max_keys
        self._hits: dict[str, Deque[float]] = {}

    def _prune(self, bucket: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def check(self, key: str) -> bool:
        """Record a hit for ``key``. False once the window budget is spent."""
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
    """Filesystem-safe, collision-free per-user directory name.

    Lowercased and restricted to ``[a-z0-9._-]``; every other byte becomes an
    underscore. Emails are already unique and the substitution is injective
    enough in practice only because the full address is preserved verbatim
    apart from separators — ``a+b@x.com`` and ``a_b@x.com`` would otherwise
    collide, so the raw address is percent-style escaped instead of dropped.
    """
    out = []
    for ch in email.strip().lower():
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append(f"~{ord(ch):02x}")
    slug = "".join(out)
    if not slug or slug in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid account identity")
    return slug[:120]


def enforce_upload_size(content_length: int | None) -> int:
    """Reject an oversized upload before a single byte is buffered."""
    cap = settings.max_upload_bytes
    if content_length is not None and content_length > cap:
        raise HTTPException(
            status_code=413,
            detail=f"upload too large — {cap // (1024 * 1024)} MiB max",
        )
    return cap


def dir_size_bytes(root: Path, *, cap: int | None = None) -> int:
    """Recursive on-disk size of ``root``, short-circuiting once past ``cap``.

    Symlinks are never followed, so a user cannot make their own quota appear
    small by pointing at an empty directory, nor make a scan walk the host.
    """
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
        if cap is not None and total > cap:
            return total
    return total


def enforce_disk_quota(
    email: str,
    plan: str,
    root: Path,
    incoming_bytes: int = 0,
) -> None:
    """Raise 507 when an account's tree would exceed its plan storage limit."""
    quota = limits_for(plan).storage_quota_bytes
    used = dir_size_bytes(root, cap=quota)
    if used + max(0, incoming_bytes) > quota:
        raise HTTPException(
            status_code=507,
            detail=(
                f"storage quota exceeded for {email} — "
                f"{quota // (1024 * 1024)} MiB on {plan} "
                f"({used // (1024 * 1024)} MiB used)"
            ),
        )


__all__ = [
    "RateLimiter",
    "api_writes",
    "uploads",
    "chat_messages",
    "login_by_email",
    "login_by_ip",
    "user_slug",
    "enforce_upload_size",
    "dir_size_bytes",
    "enforce_disk_quota",
]

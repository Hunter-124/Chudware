"""Live subscription spend guard for paid bench matrices (T-W11).

Hunter authorizes paid bench runs against *subscription* quotas, not dollar
budgets, so the meter that matters is each provider's published utilization
window — ``anthropic``'s rolling 7-day bucket and ``kimi-code``'s weekly total
quota. Both are readable for free: the OMP bun bridge keeps refreshed OAuth
credentials in its SQLite ``AuthStorage`` (``<appdata>/omp-bridge-auth.db``),
and :mod:`services.agent.providers.usage` already knows how to turn one of
those credentials into ``§4.5`` usage rows.

The guard is deliberately **absolute, not incremental**: a cap of 15 means
"stop once the 7-day bucket reads 15%", not "spend 15 points on top of
whatever was already used". A matrix that trips a cap stops between runs and
reports honest partial results — it never abandons the rows it already paid
for, and it never keeps spending past the ceiling.

Read-only with respect to credentials: nothing here writes tokens, refreshes
the bridge's store, or mutates ``config.json``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# Absolute utilization ceilings, in percent of the provider's own window.
# Sourced from Hunter's 2026-08-04 authorization for the T-W11 live matrix.
DEFAULT_CAPS: dict[str, float] = {
    "anthropic": 15.0,   # rolling 7-day, all models
    "kimi-code": 18.0,   # weekly total quota
}

# Which usage window is load-bearing per provider. The 5h buckets throttle a
# run but are not the authorized budget, so they are reported and not enforced.
_ENFORCED_WINDOW = {
    "anthropic": ("7d",),
    "kimi-code": ("total", "7d"),
}

_ADVISORY_WINDOW = {
    "anthropic": ("5h",),
    "kimi-code": ("5h",),
}


class SpendCapExceeded(RuntimeError):
    """Raised when a provider's utilization has reached its authorized cap."""

    def __init__(self, provider: str, window: str, used_pct: float, cap_pct: float):
        self.provider = provider
        self.window = window
        self.used_pct = used_pct
        self.cap_pct = cap_pct
        super().__init__(
            f"{provider} {window} utilization {used_pct:.1f}% >= cap {cap_pct:.1f}%"
        )


def appdata_dir() -> Path:
    """Chudware application-data root (honours the standard env override)."""
    env = os.environ.get("CHUDWARE_APPDATA")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "Chudware"


def bridge_auth_db() -> Path:
    return appdata_dir() / "omp-bridge-auth.db"


def _open_auth_db(db: Path) -> sqlite3.Connection | None:
    """Read-only handle on the bridge's credential store.

    The store is WAL-mode and the bridge rotates the access token roughly every
    eight hours. ``immutable=1`` tells SQLite the file cannot change, so it
    skips the ``-wal`` file entirely and hands back the last *checkpointed*
    snapshot — i.e. a long-expired access token, which the usage endpoint
    answers with HTTP 401 and the guard reports as UNREADABLE (fail-safe stop).
    Open plain ``mode=ro`` so the WAL is honoured, and only fall back to the
    immutable snapshot when the WAL side files are unreadable (no bridge
    running, no ``-shm``).
    """
    for uri in (f"file:{db}?mode=ro", f"file:{db}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            # Force a read so a WAL-open failure surfaces here, not later.
            conn.execute("SELECT 1 FROM auth_credentials LIMIT 1").fetchone()
            return conn
        except sqlite3.Error:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
    return None


def _load_bridge_credential(provider: str) -> dict | None:
    """Newest non-disabled OAuth credential for *provider* from the bridge store.

    The bridge writes ``{access, refresh, expires, accountId, email, ...}``;
    :mod:`services.agent.providers.usage` wants ``access_token`` /
    ``refresh_token`` / ``expires_at_ms``. Translate, never persist.
    """
    db = bridge_auth_db()
    if not db.exists():
        return None
    conn = _open_auth_db(db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM auth_credentials "
            " WHERE provider = ? AND credential_type = 'oauth' "
            "   AND disabled_cause IS NULL "
            " ORDER BY updated_at DESC LIMIT 1",
            (provider,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access") or data.get("access_token")
    if not access:
        return None
    return {
        "access_token": access,
        "refresh_token": data.get("refresh") or data.get("refresh_token"),
        "expires_at_ms": data.get("expires") or data.get("expires_at_ms"),
        "account_id": data.get("accountId") or data.get("account_id"),
        "email": data.get("email"),
    }


@dataclass
class Reading:
    """One provider's utilization snapshot."""

    provider: str
    ok: bool
    cap_pct: float
    windows: dict[str, float] = field(default_factory=dict)   # window -> pct
    labels: dict[str, str] = field(default_factory=dict)
    resets: dict[str, str | None] = field(default_factory=dict)
    error: str | None = None
    at: float = field(default_factory=time.time)

    @property
    def enforced_pct(self) -> float | None:
        """Utilization of the load-bearing window, or None when unreadable."""
        for w in _ENFORCED_WINDOW.get(self.provider, ()):  # first match wins
            if w in self.windows:
                return self.windows[w]
        return None

    @property
    def enforced_window(self) -> str | None:
        for w in _ENFORCED_WINDOW.get(self.provider, ()):
            if w in self.windows:
                return w
        return None

    @property
    def headroom_pct(self) -> float | None:
        used = self.enforced_pct
        return None if used is None else self.cap_pct - used

    def exceeded(self) -> bool:
        used = self.enforced_pct
        return used is not None and used >= self.cap_pct

    def summary(self) -> str:
        if not self.ok:
            return f"{self.provider}: UNREADABLE ({self.error})"
        bits = []
        for w, pct in sorted(self.windows.items()):
            mark = "*" if w == self.enforced_window else " "
            bits.append(f"{mark}{w}={pct:.1f}%")
        head = self.headroom_pct
        tail = "" if head is None else f" headroom={head:+.1f}pts"
        return f"{self.provider}: " + " ".join(bits) + f" cap={self.cap_pct:.1f}%{tail}"


def _window_key(row: dict) -> str:
    """Normalize a §4.5 usage row's window id to our short keys."""
    w = str(row.get("window") or "").strip().lower()
    if w in ("7d", "total", "5h"):
        return w
    if w.startswith("7d"):
        return w  # 7d_opus / 7d_fable — reported, not enforced
    return w or "unknown"


def read_provider(provider: str, cap_pct: float | None = None) -> Reading:
    """Fetch one provider's live utilization. Never raises; reports errors."""
    cap = DEFAULT_CAPS.get(provider, 100.0) if cap_pct is None else cap_pct
    reading = Reading(provider=provider, ok=False, cap_pct=cap)

    cred = _load_bridge_credential(provider)
    if not cred:
        reading.error = f"no oauth credential for {provider!r} in {bridge_auth_db()}"
        return reading

    try:
        from services.agent.providers import usage as usage_mod
    except Exception as exc:  # noqa: BLE001
        reading.error = f"usage module import failed: {exc}"
        return reading

    fetch = {
        "anthropic": getattr(usage_mod, "fetch_anthropic_usage", None),
        "kimi-code": getattr(usage_mod, "fetch_kimi_usage", None),
    }.get(provider)
    if fetch is None:
        reading.error = f"no usage fetcher for {provider!r}"
        return reading

    try:
        row = fetch(cred)
    except Exception as exc:  # noqa: BLE001
        reading.error = f"{type(exc).__name__}: {exc}"
        return reading

    for entry in row.get("usage") or []:
        frac = entry.get("used_fraction")
        if frac is None:
            continue
        key = _window_key(entry)
        reading.windows[key] = float(frac) * 100.0
        reading.labels[key] = str(entry.get("label") or key)
        reading.resets[key] = entry.get("resets_at")

    if not reading.windows:
        reading.error = "usage payload carried no readable windows"
        return reading

    reading.ok = True
    return reading


def read_all(caps: dict[str, float] | None = None) -> dict[str, Reading]:
    caps = DEFAULT_CAPS if caps is None else caps
    return {p: read_provider(p, cap) for p, cap in caps.items()}


class SpendGuard:
    """Gate live bench runs on subscription utilization.

    Usage::

        guard = SpendGuard()
        for cell in matrix:
            guard.check(cell.provider)     # raises SpendCapExceeded at the cap
            run(cell)
    """

    def __init__(self, caps: dict[str, float] | None = None, *, min_interval_s: float = 20.0):
        self.caps = dict(DEFAULT_CAPS if caps is None else caps)
        self.min_interval_s = min_interval_s
        self._last: dict[str, Reading] = {}
        self.baseline: dict[str, Reading] = {}
        self.trail: list[Reading] = []

    def snapshot(self, provider: str, *, force: bool = False) -> Reading:
        prev = self._last.get(provider)
        if prev and not force and (time.time() - prev.at) < self.min_interval_s:
            return prev
        reading = read_provider(provider, self.caps.get(provider))
        self._last[provider] = reading
        self.trail.append(reading)
        self.baseline.setdefault(provider, reading)
        return reading

    def check(self, provider: str, *, force: bool = False) -> Reading:
        """Raise :class:`SpendCapExceeded` when *provider* is at its ceiling.

        An UNREADABLE meter is fail-safe: it stops the matrix rather than
        spending blind. A run that cannot prove it is under budget is a run
        that does not happen.
        """
        reading = self.snapshot(provider, force=force)
        if not reading.ok:
            raise SpendCapExceeded(provider, "unreadable", float("nan"), reading.cap_pct)
        if reading.exceeded():
            raise SpendCapExceeded(
                provider,
                reading.enforced_window or "?",
                reading.enforced_pct or 0.0,
                reading.cap_pct,
            )
        return reading

    def spent_pts(self, provider: str) -> float | None:
        """Percentage points consumed since this guard's first reading."""
        base = self.baseline.get(provider)
        last = self._last.get(provider)
        if not base or not last or not base.ok or not last.ok:
            return None
        b, l = base.enforced_pct, last.enforced_pct
        return None if b is None or l is None else l - b

    def report(self) -> str:
        lines = []
        for provider in self.caps:
            last = self._last.get(provider)
            if last is None:
                lines.append(f"{provider}: not polled")
                continue
            spent = self.spent_pts(provider)
            tail = "" if spent is None else f"  spent={spent:+.1f}pts this session"
            lines.append(last.summary() + tail)
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    ap = argparse.ArgumentParser(description="Report live subscription utilization.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    readings = read_all()
    if args.json:
        print(json.dumps(
            {
                p: {
                    "ok": r.ok,
                    "cap_pct": r.cap_pct,
                    "windows": r.windows,
                    "enforced_window": r.enforced_window,
                    "enforced_pct": r.enforced_pct,
                    "headroom_pct": r.headroom_pct,
                    "resets": r.resets,
                    "error": r.error,
                }
                for p, r in readings.items()
            },
            indent=2,
        ))
    else:
        for r in readings.values():
            print(r.summary())
            for w in _ADVISORY_WINDOW.get(r.provider, ()):
                if w in r.windows:
                    print(f"    (advisory) {r.labels.get(w, w)}: {r.windows[w]:.1f}%"
                          f" resets {r.resets.get(w)}")
    return 0 if all(r.ok for r in readings.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

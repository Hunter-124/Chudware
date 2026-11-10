"""Front-door traffic metrics: honest pageviews, abuse signals, daily history.

A "pageview" here means a *document* load only -- ``GET /`` (landing) or
``GET /app/`` (the app shell). Asset requests (the multi-MB WASM bundle is a
dozen requests per load), ``/demo/status`` polls (every 15 s per open tab),
health probes and 404 scans are counted separately, so the pageview number
can be compared against an edge dashboard that counts every hit. Client
identity is the same one-way ``ip_tag`` hash the join limits use; raw
addresses are never stored.

Counters live in memory and are persisted (throttled, atomically) to
``state_dir/metrics.json`` so restarts do not zero the history. Only daily
buckets are kept, for the last 90 days.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEEP_DAYS = 90
MAX_UNIQUES_PER_DAY = 10_000
_TOP = 25


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def route_class(path: str) -> str:
    if path == "/":
        return "landing"
    if path in ("/app", "/app/"):
        return "app_document"
    if path.startswith("/app/"):
        return "app_asset"
    if path == "/demo/status":
        return "status_poll"
    if path.startswith("/demo/"):
        return "demo_api"
    if path == "/admin" or path.startswith("/admin/"):
        return "admin"
    if path == "/engine":
        return "engine_ws"
    if path.startswith(("/workspace/", "/files/", "/reveng/")):
        return "workspace_api"
    if path == "/healthz":
        return "healthz"
    return "other"


class Metrics:
    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.path, self.clock = path, clock
        self.started_at = clock()
        self.requests = 0
        self.pageviews = 0
        self.routes: collections.Counter[str] = collections.Counter()
        self.statuses: collections.Counter[str] = collections.Counter()
        self.events: collections.Counter[str] = collections.Counter()
        self.top_ips: collections.Counter[str] = collections.Counter()
        self.daily: dict[str, dict[str, Any]] = {}
        self._uniques: dict[str, set[str]] = collections.defaultdict(set)
        self._dirty_at = 0.0
        self._load()

    # -- recording ------------------------------------------------------------
    def record(self, method: str, path: str, status: int, ip_tag: str) -> None:
        now = self.clock()
        cls = route_class(path)
        self.requests += 1
        self.routes[cls] += 1
        self.statuses[str(status)] += 1
        if ip_tag:
            self.top_ips[ip_tag[:16]] += 1
        day = self.daily.setdefault(_day(now), {"requests": 0, "pageviews": 0, "joins": 0,
                                                "sessions_started": 0, "asset_requests": 0,
                                                "not_found": 0, "refused": 0})
        day["requests"] += 1
        if status == 404:
            day["not_found"] += 1
        if cls == "app_asset":
            day["asset_requests"] += 1
        # The honest pageview: a document load by a browser, not an asset/poll/probe.
        if method == "GET" and status == 200 and cls in ("landing", "app_document"):
            self.pageviews += 1
            day["pageviews"] += 1
            uniques = self._uniques[_day(now)]
            if ip_tag and len(uniques) < MAX_UNIQUES_PER_DAY:
                uniques.add(ip_tag)
        self._maybe_flush(now)

    def event(self, name: str) -> None:
        self.events[name] += 1
        day = self.daily.setdefault(_day(self.clock()),
                                    {"requests": 0, "pageviews": 0, "joins": 0,
                                     "sessions_started": 0, "asset_requests": 0,
                                     "not_found": 0, "refused": 0})
        if name == "join":
            day["joins"] += 1
        elif name in ("session_started", "session_resumed"):
            day["sessions_started"] += 1
        elif name.startswith("join_refused") or name.startswith("admin_login_refused"):
            day["refused"] += 1

    # -- persistence ------------------------------------------------------------
    def _load(self) -> None:
        if self.path is None:
            return
        with contextlib.suppress(OSError, ValueError):
            raw = json.loads(self.path.read_text("utf-8"))
            self.requests = int(raw.get("requests", 0))
            self.pageviews = int(raw.get("pageviews", 0))
            self.daily = {k: v for k, v in raw.get("daily", {}).items()}
            self._uniques = {k: set(v) for k, v in raw.get("uniques", {}).items()}

    def flush(self) -> None:
        if self.path is None:
            return
        days = sorted(self.daily)[-KEEP_DAYS:]
        payload = {"requests": self.requests, "pageviews": self.pageviews,
                   "daily": {d: self.daily[d] for d in days},
                   "uniques": {d: sorted(self._uniques.get(d, ()))[:MAX_UNIQUES_PER_DAY]
                               for d in days}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(tmp, self.path)
        self._dirty_at = self.clock()

    def _maybe_flush(self, now: float) -> None:
        if now - self._dirty_at >= 30:
            self.flush()

    # -- reporting ----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        today = _day(self.clock())
        days = sorted(self.daily)
        return {
            "started_at": self.started_at, "uptime_seconds": int(self.clock() - self.started_at),
            "requests_total": self.requests, "pageviews_total": self.pageviews,
            "routes": dict(self.routes.most_common(_TOP)),
            "statuses": dict(self.statuses.most_common(_TOP)),
            "events": dict(self.events.most_common(_TOP)),
            "top_client_tags": [{"tag": tag, "requests": n}
                                for tag, n in self.top_ips.most_common(10)],
            "today": {**self.daily.get(today, {}), "unique_clients": len(self._uniques.get(today, ()))},
            "daily": [{"day": d, **self.daily[d],
                       "unique_clients": len(self._uniques.get(d, ()))} for d in days[-30:]],
        }

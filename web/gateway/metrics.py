"""Bounded, dependency-free Prometheus metrics for the gateway.

The request registry is deliberately process-local. PostgreSQL-backed gauges are
sampled at scrape time so a gateway restart does not erase business state. Route
labels always come from FastAPI's matched route template; raw request paths are
never retained.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

try:
    from . import db, mailer
    from .auth import get_current_user
    from .engine_bridge import active_bridge_count
except ImportError:  # uvicorn app:app from the package directory
    import db  # type: ignore
    import mailer  # type: ignore
    from auth import get_current_user  # type: ignore
    from engine_bridge import active_bridge_count  # type: ignore

log = logging.getLogger("chudware-web.metrics")
router = APIRouter(tags=["operations"])

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_UNMATCHED_ROUTE = "__unmatched__"

_SECURITY_BATCH_OUTCOMES = ("accepted", "duplicate", "reauthorize", "rejected", "other")
_SECURITY_ACTION_OUTCOMES = ("revoked", "reversed", "other")
_SECURITY_APPEAL_OUTCOMES = ("submitted", "restored", "upheld", "other")

def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(items: Iterable[tuple[str, str]]) -> str:
    return "{" + ",".join(f'{key}="{_escape_label(value)}"' for key, value in items) + "}"


def _number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return "0"
    return format(value, ".12g")


class Registry:
    """Small fixed-shape request registry safe across ASGI tasks and threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._latency_count: dict[tuple[str, str], int] = defaultdict(int)
        self._latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._latency_buckets: dict[tuple[str, str], list[int]] = {}
        self._webhooks: dict[str, int] = defaultdict(int)
        self._entitlements: dict[str, int] = defaultdict(int)
        self._manifests: dict[str, int] = defaultdict(int)
        self._collection_errors = 0
        self._security_batches: dict[str, int] = defaultdict(int)
        self._security_actions: dict[str, int] = defaultdict(int)
        self._security_appeals: dict[str, int] = defaultdict(int)

    def observe(self, method: str, route: str, status_code: int, elapsed: float) -> None:
        status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "unknown"
        key = (method, route)
        with self._lock:
            self._requests[(method, route, status_class)] += 1
            self._latency_count[key] += 1
            self._latency_sum[key] += max(0.0, elapsed)
            buckets = self._latency_buckets.setdefault(key, [0] * len(_BUCKETS))
            for index, upper in enumerate(_BUCKETS):
                if elapsed <= upper:
                    buckets[index] += 1
            if route == "/api/billing/webhook":
                self._webhooks[_outcome(status_code)] += 1
            elif route == "/api/entitlement/refresh":
                self._entitlements[_outcome(status_code)] += 1
            elif route == "/api/releases/latest":
                self._manifests[_outcome(status_code)] += 1

    def collection_error(self) -> None:
        with self._lock:
            self._collection_errors += 1

    def security_batch(self, outcome: str) -> None:
        self._increment_bounded(self._security_batches, outcome, _SECURITY_BATCH_OUTCOMES)

    def security_action(self, outcome: str) -> None:
        self._increment_bounded(self._security_actions, outcome, _SECURITY_ACTION_OUTCOMES)

    def security_appeal(self, outcome: str) -> None:
        self._increment_bounded(self._security_appeals, outcome, _SECURITY_APPEAL_OUTCOMES)

    def _increment_bounded(
        self,
        family: dict[str, int],
        outcome: str,
        allowed: tuple[str, ...],
    ) -> None:
        label = outcome if outcome in allowed else "other"
        with self._lock:
            family[label] += 1
    def render_process_metrics(self) -> list[str]:
        with self._lock:
            requests = dict(self._requests)
            latency_count = dict(self._latency_count)
            latency_sum = dict(self._latency_sum)
            latency_buckets = {key: list(value) for key, value in self._latency_buckets.items()}
            webhooks = dict(self._webhooks)
            entitlements = dict(self._entitlements)
            manifests = dict(self._manifests)
            collection_errors = self._collection_errors
            security_batches = dict(self._security_batches)
            security_actions = dict(self._security_actions)
            security_appeals = dict(self._security_appeals)

        lines = [
            "# HELP chudware_http_requests_total Gateway HTTP responses by matched route template.",
            "# TYPE chudware_http_requests_total counter",
        ]
        for (method, route, status_class), value in sorted(requests.items()):
            lines.append(
                "chudware_http_requests_total"
                + _labels((("method", method), ("route", route), ("status", status_class)))
                + f" {value}"
            )
        lines.extend(
            [
                "# HELP chudware_http_request_duration_seconds Gateway HTTP response latency by matched route template.",
                "# TYPE chudware_http_request_duration_seconds histogram",
            ]
        )
        for key in sorted(latency_count):
            method, route = key
            common = (("method", method), ("route", route))
            for upper, count in zip(_BUCKETS, latency_buckets[key]):
                lines.append(
                    "chudware_http_request_duration_seconds_bucket"
                    + _labels((*common, ("le", str(upper))))
                    + f" {count}"
                )
            lines.append(
                "chudware_http_request_duration_seconds_bucket"
                + _labels((*common, ("le", "+Inf")))
                + f" {latency_count[key]}"
            )
            lines.append(
                "chudware_http_request_duration_seconds_sum"
                + _labels(common)
                + f" {_number(latency_sum[key])}"
            )
            lines.append(
                "chudware_http_request_duration_seconds_count"
                + _labels(common)
                + f" {latency_count[key]}"
            )
        lines.extend(_counter_family(
            "chudware_webhook_requests_total",
            "Stripe webhook HTTP processing outcomes.",
            webhooks,
        ))
        lines.extend(_counter_family(
            "chudware_entitlement_checkins_total",
            "Desktop entitlement refresh check-in outcomes.",
            entitlements,
        ))
        lines.extend(_counter_family(
            "chudware_update_manifest_hits_total",
            "Desktop update-manifest request outcomes.",
            manifests,
        ))
        lines.extend(_counter_family(
            "chudware_security_batches_total",
            "Signed security batch ingestion outcomes.",
            security_batches,
            _SECURITY_BATCH_OUTCOMES,
        ))
        lines.extend(_counter_family(
            "chudware_security_actions_total",
            "Reversible device security action outcomes.",
            security_actions,
            _SECURITY_ACTION_OUTCOMES,
        ))
        lines.extend(_counter_family(
            "chudware_security_appeals_total",
            "Security appeal lifecycle outcomes.",
            security_appeals,
            _SECURITY_APPEAL_OUTCOMES,
        ))
        lines.extend(
            [
                "# HELP chudware_metrics_collection_errors_total Failed PostgreSQL metric collections.",
                "# TYPE chudware_metrics_collection_errors_total counter",
                f"chudware_metrics_collection_errors_total {collection_errors}",
            ]
        )
        return lines


registry = Registry()


def _counter_family(
    name: str,
    help_text: str,
    values: dict[str, int],
    outcomes: tuple[str, ...] = ("success", "client_error", "server_error", "other"),
) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
    for outcome in outcomes:
        lines.append(f"{name}" + _labels((("outcome", outcome),)) + f" {values.get(outcome, 0)}")
    return lines


def _outcome(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "success"
    if 400 <= status_code < 500:
        return "client_error"
    if 500 <= status_code < 600:
        return "server_error"
    return "other"


def _matched_route(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) and template else _UNMATCHED_ROUTE


def _bearer_token(scope: dict[str, Any]) -> str:
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name.lower() == b"authorization":
            value = raw_value.decode("latin-1")
            scheme, separator, credential = value.partition(" ")
            if separator and scheme.lower() == "bearer":
                return credential.strip()
            return ""
    return ""


class MetricsMiddleware:
    """Observe HTTP responses and serve the localhost Prometheus token path.

    WebSockets and other ASGI scope types pass through untouched. The `/engine`
    websocket is represented by the authoritative active-bridge gauge instead
    of request-duration instrumentation.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Prometheus cannot hold a browser session cookie. A dedicated random
        # token is accepted only on the exact metrics path; normal admin sessions
        # continue through AuthMiddleware and the router's role check below.
        if scope.get("path") == "/metrics":
            configured = os.environ.get("CHUDWARE_METRICS_ADMIN_TOKEN", "")
            supplied = _bearer_token(scope)
            if configured and supplied and secrets.compare_digest(configured, supplied):
                response = await _metrics_response()
                await response(scope, receive, send)
                return

        started = time.perf_counter()
        status_code = 500

        async def measured_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            # Instrumentation is intentionally incapable of failing a customer
            # request. The application exception, if any, still propagates.
            try:
                registry.observe(
                    str(scope.get("method") or "UNKNOWN"),
                    _matched_route(scope),
                    status_code,
                    time.perf_counter() - started,
                )
            except Exception:  # noqa: BLE001
                log.exception("request metric observation failed")


def _pool_samples() -> list[str]:
    pool = db.pool()
    stats = pool.get_stats()
    size = int(stats.get("pool_size", 0))
    available = int(stats.get("pool_available", 0))
    used = max(0, size - available)
    maximum = int(getattr(pool, "max_size", 0) or stats.get("pool_max", 0) or size)
    waiting = int(stats.get("requests_waiting", 0))
    saturation = (used / maximum) if maximum else 0.0
    return [
        "# HELP chudware_postgres_pool_connections PostgreSQL pool connections by state.",
        "# TYPE chudware_postgres_pool_connections gauge",
        f'chudware_postgres_pool_connections{{state="used"}} {used}',
        f'chudware_postgres_pool_connections{{state="idle"}} {available}',
        f'chudware_postgres_pool_connections{{state="max"}} {maximum}',
        "# HELP chudware_postgres_pool_saturation_ratio Fraction of maximum PostgreSQL connections currently checked out.",
        "# TYPE chudware_postgres_pool_saturation_ratio gauge",
        f"chudware_postgres_pool_saturation_ratio {_number(saturation)}",
        "# HELP chudware_postgres_pool_waiting_requests Requests waiting for a PostgreSQL connection.",
        "# TYPE chudware_postgres_pool_waiting_requests gauge",
        f"chudware_postgres_pool_waiting_requests {waiting}",
    ]


def _database_samples() -> list[str]:
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS active FROM sessions "
            "WHERE revoked_at IS NULL AND expires_at > now()"
        )
        active_sessions = int((cur.fetchone() or {}).get("active") or 0)
        cur.execute(
            "SELECT COALESCE(-sum(credits_delta), 0) AS spent "
            "FROM credit_ledger WHERE kind = 'spend' "
            "AND ts >= now() - interval '5 minutes'"
        )
        spend_five_minutes = int((cur.fetchone() or {}).get("spent") or 0)
        cur.execute(
            "SELECT "
            "count(*) FILTER (WHERE attempts < %s) AS pending, "
            "count(*) FILTER (WHERE attempts >= %s) AS abandoned, "
            "count(*) FILTER (WHERE last_error IS NOT NULL) AS failed, "
            "COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS oldest "
            "FROM mail_outbox WHERE sent_at IS NULL",
            (mailer.MAX_ATTEMPTS, mailer.MAX_ATTEMPTS),
        )
        outbox = cur.fetchone() or {}
        cur.execute("SELECT count(*) AS processed FROM processed_events")
        processed_events = int((cur.fetchone() or {}).get("processed") or 0)

    samples = [
        "# HELP chudware_active_sessions Current unexpired, unrevoked browser sessions.",
        "# TYPE chudware_active_sessions gauge",
        f"chudware_active_sessions {active_sessions}",
        "# HELP chudware_credit_spend_credits_per_second Gross pooled-AI credits spent per second over the last five minutes.",
        "# TYPE chudware_credit_spend_credits_per_second gauge",
        f"chudware_credit_spend_credits_per_second {_number(spend_five_minutes / 300.0)}",
        "# HELP chudware_mail_outbox_depth Undelivered mail rows; pending growth catches a dead delivery backend.",
        "# TYPE chudware_mail_outbox_depth gauge",
        f'chudware_mail_outbox_depth{{state="pending"}} {int(outbox.get("pending") or 0)}',
        f'chudware_mail_outbox_depth{{state="abandoned"}} {int(outbox.get("abandoned") or 0)}',
        "# HELP chudware_mail_outbox_failures Undelivered rows whose latest delivery attempt failed.",
        "# TYPE chudware_mail_outbox_failures gauge",
        f"chudware_mail_outbox_failures {int(outbox.get('failed') or 0)}",
        "# HELP chudware_mail_outbox_oldest_seconds Age of the oldest undelivered mail row.",
        "# TYPE chudware_mail_outbox_oldest_seconds gauge",
        f"chudware_mail_outbox_oldest_seconds {_number(float(outbox.get('oldest') or 0.0))}",
        "# HELP chudware_webhook_events_processed_total Stripe event ids committed by the idempotent webhook processor.",
        "# TYPE chudware_webhook_events_processed_total counter",
        f"chudware_webhook_events_processed_total {processed_events}",
    ]
    samples.extend(_pool_samples())
    return samples


async def _metrics_response() -> Response:
    lines = registry.render_process_metrics()
    try:
        lines.extend(await asyncio.to_thread(_database_samples))
    except Exception:  # noqa: BLE001
        registry.collection_error()
        log.exception("PostgreSQL metric collection failed")
        lines.append("# PostgreSQL metric collection failed; see gateway log")
    lines.extend(
        [
            "# HELP chudware_engine_bridges_active Current authenticated /engine WebSocket bridges.",
            "# TYPE chudware_engine_bridges_active gauge",
            f"chudware_engine_bridges_active {active_bridge_count()}",
        ]
    )
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    user = get_current_user(request)
    if user.role != "admin":
        # Do not disclose operational/business telemetry to an authenticated
        # customer. Use 401 rather than 403 so callers cannot distinguish this
        # endpoint from a missing/expired operator credential.
        raise HTTPException(status_code=401, detail="administrator authentication required")
    return await _metrics_response()


__all__ = ["MetricsMiddleware", "registry", "router"]

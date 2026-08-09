from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import AttentionItem
from .store import AttentionStore


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|token|password|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_QUERY_KEYS = {"authorization", "api_key", "apikey", "access_token", "token", "password", "secret"}


def redact_text(value: str, *, limit: int = 2_000) -> str:
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    return value[:limit]


def safe_links(values: Sequence[str]) -> list[str]:
    links: list[str] = []
    for value in values[:20]:
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        hostname = parsed.hostname
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        query = urlencode([
            (key, "[REDACTED]" if key.lower() in _SECRET_QUERY_KEYS else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        clean = urlunsplit((parsed.scheme, hostname, parsed.path, query, ""))
        links.append(clean[:2_048])
    return links


def webhook_payload(item: AttentionItem, links: Sequence[str] = ()) -> dict[str, Any]:
    """Build an allowlisted payload. Raw event bodies and media can never enter it."""
    return {
        "schema": "chudware.mfg.attention/1",
        "id": item.id,
        "severity": item.severity,
        "device": redact_text(item.device, limit=256),
        "run": redact_text(item.run, limit=256),
        "reason": redact_text(item.reason, limit=256),
        "why": redact_text(item.why),
        "raised_seq": item.raised_seq,
        "occurrence_count": item.occurrence_count,
        "links": safe_links(links),
    }


class Channel(Protocol):
    name: str

    def deliver(self, payload: Mapping[str, Any]) -> None: ...


class InAppChannel:
    name = "in_app"

    def __init__(self, *, capacity: int = 256, on_delivery: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._callback = on_delivery
        self._lock = threading.Lock()

    def deliver(self, payload: Mapping[str, Any]) -> None:
        snapshot = dict(payload)
        with self._lock:
            self._items.append(snapshot)
        if self._callback:
            self._callback(snapshot)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._items)
            self._items.clear()
            return rows


class DesktopChannel:
    name = "desktop"

    def __init__(self, *, command: Sequence[str] | None = None, timeout_s: float = 3.0) -> None:
        self._command = tuple(command) if command is not None else None
        self._timeout_s = timeout_s

    def deliver(self, payload: Mapping[str, Any]) -> None:
        title = f"Chudware · {payload['severity']} · machine needs you"
        body = f"{payload['device']} · {payload['reason']}\n{payload['why']}"
        command = self._command or self._platform_command(title, body)
        completed = subprocess.run(
            [*command, title, body] if self._command else command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self._timeout_s,
        )
        if completed.returncode != 0:
            message = redact_text(completed.stderr.strip() or f"exit {completed.returncode}", limit=300)
            raise RuntimeError(message)

    @staticmethod
    def _platform_command(title: str, body: str) -> list[str]:
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            return ["notify-send", "--app-name=Chudware", title, body]
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = 'display notification "' + body.replace('"', '\\"') + '" with title "' + title.replace('"', '\\"') + '"'
            return ["osascript", "-e", script]
        raise RuntimeError("desktop notifications are unavailable on this host")


class WebhookChannel:
    name = "webhook"

    def __init__(self, url: str, *, timeout_s: float = 5.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("webhook URL must be http(s)")
        self._url = url
        self._timeout_s = timeout_s

    def deliver(self, payload: Mapping[str, Any]) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Chudware-Manufacturing/1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")
            response.read(1)


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    max_attempts: int = 3
    base_retry_s: float = 0.25
    rate_limit: int = 10
    rate_window_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts <= 0 or self.rate_limit <= 0:
            raise ValueError("attempt and rate limits must be positive")
        if self.base_retry_s < 0 or self.rate_window_s <= 0:
            raise ValueError("retry/window durations are invalid")


class _WindowLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        now = self.clock()
        with self._lock:
            while self._sent and self._sent[0] <= now - self.window_s:
                self._sent.popleft()
            if len(self._sent) >= self.limit:
                return False
            self._sent.append(now)
            return True


class DeliveryManager:
    def __init__(
        self,
        store: AttentionStore,
        channels: Sequence[Channel],
        *,
        policies: Mapping[str, ChannelPolicy] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        names = [channel.name for channel in channels]
        if len(names) != len(set(names)):
            raise ValueError("delivery channel names must be unique")
        self.store = store
        self.channels = tuple(channels)
        self.policies = dict(policies or {})
        self.clock = clock
        self.sleeper = sleeper
        self._limiters = {
            channel.name: _WindowLimiter(
                self._policy(channel.name).rate_limit,
                self._policy(channel.name).rate_window_s,
                clock,
            )
            for channel in channels
        }

    def deliver(self, item: AttentionItem, *, links: Sequence[str] = ()) -> None:
        payload = webhook_payload(item, links)
        for channel in self.channels:
            policy = self._policy(channel.name)
            for attempt in range(1, policy.max_attempts + 1):
                if not self._limiters[channel.name].acquire():
                    self.store.record_delivery(
                        item.id,
                        channel=channel.name,
                        attempt=attempt,
                        status="rate_limited",
                        error="per-channel delivery rate limit reached",
                    )
                    break
                try:
                    channel.deliver(payload)
                except Exception as exc:
                    final = attempt == policy.max_attempts
                    self.store.record_delivery(
                        item.id,
                        channel=channel.name,
                        attempt=attempt,
                        status="failed" if final else "retrying",
                        error=redact_text(str(exc), limit=300),
                    )
                    if final:
                        break
                    self.sleeper(policy.base_retry_s * (2 ** (attempt - 1)))
                else:
                    self.store.record_delivery(
                        item.id,
                        channel=channel.name,
                        attempt=attempt,
                        status="delivered",
                    )
                    break

    def _policy(self, channel: str) -> ChannelPolicy:
        return self.policies.get(channel, ChannelPolicy())

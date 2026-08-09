from __future__ import annotations

import errno
import http.client
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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
            return ["notify-send", "--app-name=Chudware", "--", title, body]
        if sys.platform == "darwin" and shutil.which("osascript"):
            return [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                "--",
                body,
                title,
            ]
        raise RuntimeError("desktop notifications are unavailable on this host")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_ResolvedTarget = tuple[int, int, int, tuple[Any, ...]]


def _connect_to_resolved(
    connection: http.client.HTTPConnection, target: _ResolvedTarget
) -> None:
    family, socktype, proto, sockaddr = target
    sys.audit("http.client.connect", connection, connection.host, connection.port)
    sock = socket.socket(family, socktype, proto)
    try:
        sock.settimeout(connection.timeout)
        if connection.source_address:
            sock.bind(connection.source_address)
        sock.connect(sockaddr)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        connection.sock = sock
        if connection._tunnel_host:
            connection._tunnel()
    except BaseException:
        sock.close()
        raise


class _ResolvedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, *, resolved_target: _ResolvedTarget, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._resolved_target = resolved_target

    def connect(self) -> None:
        _connect_to_resolved(self, self._resolved_target)


class _ResolvedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, resolved_target: _ResolvedTarget, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._resolved_target = resolved_target

    def connect(self) -> None:
        _connect_to_resolved(self, self._resolved_target)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _ResolvedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, target: _ResolvedTarget) -> None:
        super().__init__()
        self._target = target

    def http_open(self, request: urllib.request.Request) -> Any:
        def connection(host: str, **kwargs: Any) -> _ResolvedHTTPConnection:
            return _ResolvedHTTPConnection(host, resolved_target=self._target, **kwargs)

        return self.do_open(connection, request)


class _ResolvedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, target: _ResolvedTarget) -> None:
        super().__init__()
        self._target = target

    def https_open(self, request: urllib.request.Request) -> Any:
        def connection(host: str, **kwargs: Any) -> _ResolvedHTTPSConnection:
            return _ResolvedHTTPSConnection(host, resolved_target=self._target, **kwargs)

        return self.do_open(connection, request, context=self._context)


class WebhookChannel:
    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 5.0,
        allow_private_networks: bool = False,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("webhook URL must be http(s)")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("webhook URL port is invalid") from exc
        self._url = url
        self._hostname = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._timeout_s = timeout_s
        self._allow_private_networks = allow_private_networks

    def _resolve_target(self) -> _ResolvedTarget:
        try:
            answers = socket.getaddrinfo(
                self._hostname,
                self._port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise RuntimeError(
                f"webhook target resolution failed for {self._hostname}: {exc}"
            ) from exc
        targets: list[_ResolvedTarget] = []
        for family, socktype, proto, _canonical_name, sockaddr in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            except ValueError as exc:
                raise RuntimeError(
                    f"webhook target resolution returned an invalid address for {self._hostname}"
                ) from exc
            if address.is_unspecified:
                raise RuntimeError(f"webhook target resolves to unspecified address {address}")
            if address.is_multicast:
                raise RuntimeError(f"webhook target resolves to multicast address {address}")
            if not self._allow_private_networks:
                refusal = next(
                    (
                        name
                        for name, blocked in (
                            ("loopback", address.is_loopback),
                            ("link-local", address.is_link_local),
                            ("reserved", address.is_reserved),
                            ("private", address.is_private),
                        )
                        if blocked
                    ),
                    None,
                )
                if refusal:
                    raise RuntimeError(
                        f"webhook target resolves to {refusal} address {address}"
                    )
            targets.append((family, socktype, proto, sockaddr))
        if not targets:
            raise RuntimeError(
                f"webhook target resolution returned no usable addresses for {self._hostname}"
            )
        return targets[0]

    def deliver(self, payload: Mapping[str, Any]) -> None:
        target = self._resolve_target()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _ResolvedHTTPHandler(target),
            _ResolvedHTTPSHandler(target),
            _NoRedirect(),
        )
        request = urllib.request.Request(
            self._url,
            data=json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Chudware-Manufacturing/1"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=self._timeout_s) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"webhook returned HTTP {response.status}")
                response.read(1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RuntimeError(f"webhook redirect refused with HTTP {exc.code}") from exc
            raise RuntimeError(f"webhook returned HTTP {exc.code}") from exc


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

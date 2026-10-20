"""Common adapter interface and polling event stream."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator, Mapping
import hashlib
import json
import threading
import time
from typing import Any

from .http_client import HttpClient
from .model import (
    Artifact,
    AuthorizationRequired,
    EndpointConfig,
    Redactor,
    UnsupportedOperation,
    fresh_row,
)

_MISSING = object()
SNAPSHOT_FIELDS = (
    "device_state",
    "job_state",
    "job_name",
    "progress",
    "nozzle_temperature_c",
    "nozzle_target_c",
    "bed_temperature_c",
    "bed_target_c",
)
_MAX_JOURNALED_RAW_BYTES = 64 * 1024


class PrinterAdapter(ABC):
    adapter_kind: str

    def __init__(self, config: EndpointConfig, *, api_key_header: str = "X-Api-Key") -> None:
        self.config = config
        self.http = HttpClient(config.base_url, config.auth, config.timeout_s, api_key_header,
                               address_class=config.address_class)
        self.redactor = Redactor(config.auth.secret_values())
        self._last_ok: dict[str, dict[str, Any]] = {}
        self._operations: dict[str, Mapping[str, Any]] = {}

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def upload(self, artifact: Artifact) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def start(self, remote_artifact: Mapping[str, Any], authorization_token: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def pause(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def resume(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def cancel(self) -> dict[str, Any]:
        raise NotImplementedError

    def events(
        self,
        *,
        stop_event: threading.Event | None = None,
        poll_interval_s: float = 1.0,
    ) -> Generator[dict[str, Any], None, None]:
        """Authoritative polling stream used by adapters without documented push."""
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        stopper = stop_event or threading.Event()
        while not stopper.is_set():
            yield {"event": "snapshot", "transport": "polling", "rows": self.snapshot()}
            stopper.wait(poll_interval_s)

    def _require_authorization(self, token: str | None) -> None:
        # This is deliberately only a presence check. The adapter cannot validate approvals.
        if not isinstance(token, str) or not token.strip():
            raise AuthorizationRequired("start requires an arbiter-issued authorization token")

    def _require_operation(self, name: str) -> None:
        capability = self._operations.get(name)
        if not capability or capability.get("truth") == "unsupported":
            why = capability.get("why") if capability else "adapter has not been successfully probed"
            raise UnsupportedOperation(f"{name} unsupported: {why}")

    def _row(
        self,
        field: str,
        value: Any = _MISSING,
        *,
        raw: Any,
        why: str | None = None,
    ) -> dict[str, Any]:
        del raw  # Vendor payload is attached once by _finish_snapshot, never once per field.
        available = value is not _MISSING and value is not None
        row = fresh_row(
            field=field,
            value=None if value is _MISSING else value,
            available=available,
            why=why,
            src=self.config.src,
            ttl_ms=self.config.telemetry_ttl_ms,
            redactor=self.redactor,
            last_ok=self._last_ok.get(field),
        )
        if available:
            self._last_ok[field] = {"v": row["v"], "t_mono_ns": row["t_mono_ns"], "seq": row["seq"]}
        return row

    def _finish_snapshot(self, rows: list[dict[str, Any]], raw: Any) -> list[dict[str, Any]]:
        clean = self.redactor.value(raw)
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_JOURNALED_RAW_BYTES:
            clean = {
                "truncated": True,
                "original_bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "preview": encoded[:4096].decode("utf-8", errors="replace"),
            }
        if rows:
            rows[0]["raw"] = clean
        return rows

    def _unknown_snapshot(self, error: Exception) -> list[dict[str, Any]]:
        detail = self.redactor.text(error)
        reason_code = str(getattr(error, "reason_code", "transport_error"))
        why = f"{reason_code}: {self.adapter_kind} telemetry unavailable: {detail}"
        raw = {"transport_error": {"code": reason_code, "detail": detail}}
        rows = [self._row(field, raw=raw, why=why) for field in SNAPSHOT_FIELDS]
        return self._finish_snapshot(rows, raw)

    def _command_result(self, command: str, raw: Any = None) -> dict[str, Any]:
        return {
            "accepted": True,
            "command": command,
            "confirmed": False,
            "why": "command issued once; confirm through snapshot",
            "raw": self.redactor.value(raw),
            "t_mono_ns": time.monotonic_ns(),
        }


def nested(payload: Mapping[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def number_or_missing(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING
    return value


def string_or_missing(value: Any) -> Any:
    return value if isinstance(value, str) and value else _MISSING


def percent_fraction(value: Any) -> Any:
    numeric = number_or_missing(value)
    if numeric is _MISSING:
        return _MISSING
    return min(1.0, max(0.0, float(numeric) / 100.0))

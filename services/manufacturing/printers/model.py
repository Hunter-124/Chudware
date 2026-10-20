"""Shared, JSON-serializable printer-adapter contract and safety vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import time
from typing import Any, Iterable, Literal, Mapping
from urllib.parse import urlsplit

CAPABILITY_LEVELS = (
    "plan",
    "generate",
    "post",
    "parse",
    "simulate",
    "monitor",
    "transport",
    "execute",
    "safety_integration",
)
DEVICE_STATES = frozenset(
    {
        "disconnected",
        "connecting",
        "identifying",
        "idle",
        "running",
        "hold",
        "door",
        "probing",
        "homing",
        "alarm",
        "fault",
        "unknown",
    }
)
RUN_STATES = frozenset(
    {
        "draft",
        "awaiting_approval",
        "approved",
        "arming",
        "queued",
        "running",
        "holding",
        "held",
        "resuming",
        "completing",
        "completed",
        "aborting",
        "aborted",
        "faulted",
        "abandoned",
    }
)
AuthMode = Literal["api_key", "trusted_host", "digest"]
SourceTruth = Literal["probed", "simulated"]


class AdapterError(RuntimeError):
    """Base class for safe adapter refusals."""


class ConfigurationError(AdapterError):
    pass


class ProtocolError(AdapterError):
    def __init__(self, message: str, *, reason_code: str = "protocol_error") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class UnsupportedOperation(AdapterError):
    pass


class AuthorizationRequired(AdapterError):
    pass


def _address_class(host: str) -> str:
    """Mirror transport_tcp.cpp's commissioned address classes, including mapped IPv4."""
    address = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _address_class(str(address.ipv4_mapped))
        if address.is_unspecified or address.is_multicast:
            return "invalid"
        if address.is_loopback:
            return "loopback"
        if address in ipaddress.ip_network("fc00::/7") or address in ipaddress.ip_network("fe80::/10"):
            return "local"
        return "public"
    first, second, *_ = address.packed
    if first == 0 or first >= 224:
        return "invalid"
    if first == 127:
        return "loopback"
    if (first == 10 or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168) or (first == 169 and second == 254)):
        return "local"
    return "public"


def _commissioned_address_class(host: str, value: str | None) -> str:
    if value is not None and value not in {"loopback", "local", "public"}:
        raise ConfigurationError("printer address_class must be loopback, local, or public")
    try:
        literal_class = _address_class(host)
    except ValueError:
        if value is None:
            raise ConfigurationError("commissioned DNS printer host requires address_class")
        return value
    if literal_class == "invalid" or (value is not None and value != literal_class):
        raise ConfigurationError("printer IP does not match commissioned address_class")
    return literal_class


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Authentication is always selected explicitly; there is no auto-detection."""

    mode: AuthMode
    api_key: str | None = None
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"api_key", "trusted_host", "digest"}:
            raise ConfigurationError("auth mode must be api_key, trusted_host, or digest")
        if self.mode == "api_key" and not self.api_key:
            raise ConfigurationError("api_key authentication requires an API key")
        if self.mode == "digest" and (not self.username or not self.password):
            raise ConfigurationError("digest authentication requires username and password")
        if self.mode == "trusted_host" and any((self.api_key, self.username, self.password)):
            raise ConfigurationError("trusted_host authentication must not carry credentials")
        if self.mode == "api_key" and any((self.username, self.password)):
            raise ConfigurationError("api_key authentication must not carry digest credentials")

    def secret_values(self) -> tuple[str, ...]:
        return tuple(v for v in (self.api_key, self.password) if v)


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    base_url: str
    auth: AuthConfig
    endpoint_is_physical: bool
    origin: Literal["live", "virtual", "replay"] = "live"
    timeout_s: float = 2.0
    telemetry_ttl_ms: int = 5_000
    address_class: Literal["loopback", "local", "public"] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise ConfigurationError("printer base_url must be a string")
        if not isinstance(self.auth, AuthConfig):
            raise ConfigurationError("printer auth must be an AuthConfig")
        if not isinstance(self.endpoint_is_physical, bool):
            raise ConfigurationError("endpoint_is_physical must be explicitly true or false")
        if self.origin not in {"live", "virtual", "replay"}:
            raise ConfigurationError("origin must be live, virtual, or replay")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("printer base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ConfigurationError("credentials are forbidden in printer base_url")
        _commissioned_address_class(parsed.hostname, self.address_class)
        if self.timeout_s <= 0:
            raise ConfigurationError("timeout_s must be positive")
        if self.telemetry_ttl_ms <= 0:
            raise ConfigurationError("telemetry_ttl_ms must be positive")

    @property
    def src(self) -> SourceTruth:
        parsed = urlsplit(self.base_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        loopback = host == "localhost" or host.endswith(".localhost")
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if self.origin != "live" or loopback or not self.endpoint_is_physical:
            return "simulated"
        return "probed"


@dataclass(frozen=True, slots=True)
class Artifact:
    data: bytes
    original_name: str = "print.gcode"
    sha256: str | None = None

    def __post_init__(self) -> None:
        digest = hashlib.sha256(self.data).hexdigest()
        if self.sha256 is not None and self.sha256.lower() != digest:
            raise ConfigurationError("artifact sha256 does not match its bytes")
        object.__setattr__(self, "sha256", digest)

    @property
    def remote_name(self) -> str:
        return f"chudware-{self.sha256[:16]}.gcode"


class Redactor:
    _SENSITIVE_KEYS = (
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "x-api-key",
    )

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def text(self, value: Any) -> str:
        result = str(value)
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
        return result

    def value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            clean: dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key)
                folded = key_text.lower().replace("-", "_")
                if any(marker.replace("-", "_") in folded for marker in self._SENSITIVE_KEYS):
                    clean[key_text] = "[REDACTED]"
                else:
                    clean[key_text] = self.value(child)
            return clean
        if isinstance(value, (list, tuple)):
            return [self.value(child) for child in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def capability_report(
    *,
    subject_id: str,
    adapter_kind: str,
    monitor_verified: bool,
    transport_verified: bool,
    operations: Mapping[str, Mapping[str, Any]],
    raw: Mapping[str, Any],
    redactor: Redactor,
) -> dict[str, Any]:
    levels: list[dict[str, Any]] = []
    for level in CAPABILITY_LEVELS:
        if level == "monitor" and monitor_verified:
            item = {"level": level, "truth": "verified", "evidence_ref": [], "emulator_stage": "E3", "why": None}
        elif level == "transport" and transport_verified:
            item = {"level": level, "truth": "verified", "evidence_ref": [], "emulator_stage": "E3", "why": None}
        elif level == "execute":
            item = {
                "level": level,
                "truth": "assumed",
                "evidence_ref": [],
                "emulator_stage": "E3",
                "missing_level": "execute",
                "why": "physical execute requires an attended hardware pilot",
            }
        else:
            item = {
                "level": level,
                "truth": "unsupported",
                "evidence_ref": [],
                "emulator_stage": "E3",
                "why": (
                    "printer adapters do not provide safety integration"
                    if level == "safety_integration"
                    else f"{adapter_kind} adapter does not provide {level}"
                ),
            }
        levels.append(item)
    max_advertised = "transport" if transport_verified else ("monitor" if monitor_verified else None)
    return {
        "subject_id": subject_id,
        "adapter_kind": adapter_kind,
        "levels": levels,
        "max_advertised": max_advertised,
        "emulator_stage_passed": "E3",
        "operations": redactor.value(operations),
        "raw": redactor.value(raw),
    }


def operation_truth(kind: Literal["verified", "unsupported", "execute_assumed"], why: str | None = None) -> dict[str, Any]:
    if kind == "verified":
        return {"truth": "verified", "why": None}
    if kind == "execute_assumed":
        return {
            "truth": "assumed",
            "missing_level": "execute",
            "why": why or "physical execute requires an attended hardware pilot",
        }
    return {"truth": "unsupported", "why": why or "endpoint was not exposed by the probed firmware"}


def fresh_row(
    *,
    field: str,
    value: Any,
    available: bool,
    why: str | None,
    src: SourceTruth,
    ttl_ms: int,
    redactor: Redactor,
    last_ok: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.monotonic_ns()
    if available:
        return {
            "field": field,
            "v": redactor.value(value),
            "state": "ok",
            "t_mono_ns": now,
            "seq": None,
            "age_ms": 0,
            "ttl_ms": ttl_ms,
            "src": src,
            "why": None,
            "last_ok": None,
        }
    return {
        "field": field,
        "v": None,
        "state": "unknown",
        "t_mono_ns": now,
        "seq": None,
        "age_ms": 0,
        "ttl_ms": ttl_ms,
        "src": src,
        "why": why or f"{field} absent from vendor telemetry",
        "last_ok": redactor.value(last_ok),
    }

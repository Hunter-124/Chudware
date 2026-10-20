"""Stateful hostd worker-bridge handlers for printer adapters.

Registration is intentionally external: ``services.worker_main`` imports this module's
``WORKER_METHODS`` table.  The manufacturing lane remains the owner of leases,
approvals, durable sequence, and command arbitration.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
import threading
from typing import Any
from urllib.parse import urlsplit

from .base import PrinterAdapter
from .model import AdapterError, Artifact, AuthConfig, EndpointConfig
from .moonraker import MoonrakerAdapter
from .octoprint import OctoPrintAdapter
from .prusalink import PrusaLinkAdapter

_ADAPTER_TYPES: dict[str, type[PrinterAdapter]] = {
    "moonraker": MoonrakerAdapter,
    "octoprint": OctoPrintAdapter,
    "prusalink": PrusaLinkAdapter,
}
_ADAPTERS: dict[str, PrinterAdapter] = {}
_PROBED: set[str] = set()
_LOCK = threading.RLock()


def _adapter_id(params: Mapping[str, Any]) -> str:
    value = params.get("adapter_id")
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("adapter_id must be a non-empty string of at most 128 characters")
    return value


def _get(params: Mapping[str, Any]) -> PrinterAdapter:
    adapter_id = _adapter_id(params)
    with _LOCK:
        adapter = _ADAPTERS.get(adapter_id)
    if adapter is None:
        raise ValueError(f"printer adapter {adapter_id!r} is not open")
    return adapter


def _get_probed(params: Mapping[str, Any]) -> PrinterAdapter:
    adapter_id = _adapter_id(params)
    adapter = _get(params)
    with _LOCK:
        connected = adapter_id in _PROBED
    if not connected:
        raise ValueError(f"printer adapter {adapter_id!r} is not connected; probe it first")
    return adapter


def _endpoint_origin(value: Any) -> tuple[str, str, int]:
    if not isinstance(value, str):
        raise ValueError("base_url and expected_base_url must be strings")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url and expected_base_url must be absolute http(s) URLs")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower().rstrip("."), port


def _open(params: Mapping[str, Any]) -> dict[str, Any]:
    adapter_id = _adapter_id(params)
    kind = params.get("kind")
    adapter_type = _ADAPTER_TYPES.get(kind) if isinstance(kind, str) else None
    if adapter_type is None:
        raise ValueError("kind must be moonraker, octoprint, or prusalink")
    endpoint = params.get("endpoint")
    auth = params.get("auth")
    if not isinstance(endpoint, Mapping) or not isinstance(auth, Mapping):
        raise ValueError("endpoint and auth must be objects")
    base_url = endpoint.get("base_url")
    expected_base_url = endpoint.get("expected_base_url")
    if _endpoint_origin(base_url) != _endpoint_origin(expected_base_url):
        raise ValueError("base_url does not match the lane-supplied expected endpoint")
    auth_config = AuthConfig(
        mode=auth.get("mode"),
        api_key=auth.get("api_key"),
        username=auth.get("username"),
        password=auth.get("password"),
    )
    config = EndpointConfig(
        base_url=base_url,
        auth=auth_config,
        endpoint_is_physical=endpoint.get("endpoint_is_physical"),
        origin=endpoint.get("origin", "live"),
        timeout_s=endpoint.get("timeout_s", 2.0),
        telemetry_ttl_ms=endpoint.get("telemetry_ttl_ms", 5_000),
        address_class=endpoint.get("address_class"),
    )
    adapter = adapter_type(config)
    with _LOCK:
        if adapter_id in _ADAPTERS:
            raise ValueError(f"printer adapter {adapter_id!r} is already open")
        _ADAPTERS[adapter_id] = adapter
    return {"ok": True, "adapter_id": adapter_id, "kind": kind}


def _close(params: Mapping[str, Any]) -> dict[str, Any]:
    adapter_id = _adapter_id(params)
    with _LOCK:
        removed = _ADAPTERS.pop(adapter_id, None)
        _PROBED.discard(adapter_id)
    return {"ok": True, "adapter_id": adapter_id, "closed": removed is not None}


def _probe(params: Mapping[str, Any]) -> dict[str, Any]:
    adapter_id = _adapter_id(params)
    report = _get(params).probe()
    with _LOCK:
        _PROBED.add(adapter_id)
    return {"ok": True, "report": report}


def _snapshot(params: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "rows": _get_probed(params).snapshot()}


def _upload(params: Mapping[str, Any]) -> dict[str, Any]:
    encoded = params.get("artifact_b64")
    if not isinstance(encoded, str):
        raise ValueError("artifact_b64 must be a base64 string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("artifact_b64 is not valid base64") from exc
    artifact = Artifact(
        data=data,
        original_name=params.get("original_name", "print.gcode"),
        sha256=params.get("sha256"),
    )
    return {"ok": True, "remote_artifact": _get_probed(params).upload(artifact)}


def _start(params: Mapping[str, Any]) -> dict[str, Any]:
    remote = params.get("remote_artifact")
    if not isinstance(remote, Mapping):
        raise ValueError("remote_artifact must be an object")
    return {
        "ok": True,
        "command": _get_probed(params).start(remote, authorization_token=params.get("authorization_token")),
    }


def _pause(params: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "command": _get_probed(params).pause()}


def _resume(params: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "command": _get_probed(params).resume()}


def _cancel(params: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "command": _get_probed(params).cancel()}


def _handler(fn: Callable[[Mapping[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    def wrapped(params: dict[str, Any], emit: Any) -> dict[str, Any]:
        del emit
        try:
            return fn(params or {})
        except (AdapterError, TypeError, ValueError, KeyError) as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    return wrapped


WORKER_METHODS = {
    "mfg_printer_adapter_open": _handler(_open),
    "mfg_printer_adapter_close": _handler(_close),
    "mfg_printer_adapter_probe": _handler(_probe),
    "mfg_printer_adapter_snapshot": _handler(_snapshot),
    "mfg_printer_adapter_upload": _handler(_upload),
    "mfg_printer_adapter_start": _handler(_start),
    "mfg_printer_adapter_pause": _handler(_pause),
    "mfg_printer_adapter_resume": _handler(_resume),
    "mfg_printer_adapter_cancel": _handler(_cancel),
}

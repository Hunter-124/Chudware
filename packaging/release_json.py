"""Canonical JSON bytes shared by release evidence, signing, and publishing."""
from __future__ import annotations

import json
from typing import Any, TypeVar

_Error = TypeVar("_Error", bound=Exception)


def canonical_json_bytes(value: Any) -> bytes:
    """Return compact, key-sorted UTF-8 JSON terminated by exactly one LF."""
    return (json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def parse_canonical_json_bytes(
    raw: bytes,
    label: str,
    error_type: type[_Error] = ValueError,
) -> dict[str, Any]:
    """Parse a canonical JSON object, rejecting alternate encodings/newlines."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise error_type(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label} must be a JSON object")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise error_type(f"{label} contains a non-canonical JSON value") from exc
    if raw != canonical:
        raise error_type(f"{label} bytes are not canonical")
    return value

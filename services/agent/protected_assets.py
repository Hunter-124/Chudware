"""Production seam for build-generated prompt and role constants.

Developer source runs intentionally return no compiled assets and keep reading the
authored files. Nuitka defines ``__compiled__`` in compiled modules; those builds
must contain the generated top-level ``chudware_protected_assets`` module.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Final


_PACKAGED: Final = "__compiled__" in globals()

if _PACKAGED:
    try:
        import chudware_protected_assets as _generated
    except ModuleNotFoundError as exc:
        if exc.name != "chudware_protected_assets":
            raise
        raise RuntimeError("compiled protected assets are unavailable") from exc
    if getattr(_generated, "SCHEMA", None) != 1:
        raise RuntimeError("compiled protected assets have an unsupported schema")
    _PROMPTS = getattr(_generated, "PROMPTS", None)
    _CHEATSHEETS = getattr(_generated, "CHEATSHEETS", None)
    _ROLES = getattr(_generated, "ROLES", None)
    if not all(isinstance(table, dict) and table for table in (_PROMPTS, _CHEATSHEETS, _ROLES)):
        raise RuntimeError("compiled protected asset tables are missing or empty")
else:
    _PROMPTS: Mapping[str, str] = {}
    _CHEATSHEETS: Mapping[str, str] = {}
    _ROLES: Mapping[str, str] = {}


def packaged() -> bool:
    return _PACKAGED


def prompt(path: str) -> str | None:
    value = _PROMPTS.get(path)
    return value if isinstance(value, str) else None


def cheatsheet(path: str) -> str | None:
    value = _CHEATSHEETS.get(path)
    return value if isinstance(value, str) else None


def role_yaml(path: str) -> str | None:
    value = _ROLES.get(path)
    return value if isinstance(value, str) else None


def prompt_names() -> tuple[str, ...]:
    return tuple(sorted(_PROMPTS))


def role_names() -> tuple[str, ...]:
    return tuple(sorted(_ROLES))

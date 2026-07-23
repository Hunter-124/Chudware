"""``unit_convert(value, unit, family?)`` — deterministic SI unit normalisation, contract
``contracts/research_tools.md`` §1 (the answer envelope). Pillar 8 principle: **code
converts units, never the model** (ksi/MPa is the classic silent 6.9x error).

SUPERVISOR RULING (T-F3 Q2): the canonical unit normaliser is
``services.agent.interaction.normalize_dimension``. This helper does **not**
parallel-implement it — it *imports and delegates* to it when the ``agent`` package is
importable, and falls back to a minimal local table (same reject-never-coerce semantics,
same ``{value, unit}`` return shape) only until ``interaction.py`` lands in-tree. Both
paths share this module's envelope wrapper, so behaviour is identical once merged.
"""
from __future__ import annotations

from typing import Any

from .common import BUNDLED, envelope, fail

# --- normaliser seam -------------------------------------------------------------------
# Prefer the canonical agent normaliser; fall back to a local mirror until T-F3 merges.
try:  # pragma: no cover - exercised via the skipif test on the F3 branch
    from agent.interaction import (  # type: ignore
        InteractionError as _InteractionError,
    )
    from agent.interaction import normalize_dimension as _canonical_normalize

    _HAVE_CANONICAL = True
except Exception:  # noqa: BLE001 - agent package not in this tree until T-F3 merges
    _canonical_normalize = None  # type: ignore
    _HAVE_CANONICAL = False

    class _InteractionError(Exception):  # type: ignore
        """Local stand-in mirroring ``agent.interaction.InteractionError``."""


# TODO(T-F3 merge): fallback until interaction.py lands. Keep these tables byte-for-byte
# aligned with ``services/agent/interaction.py`` (_LENGTH_TO_MM / _ANGLE_TO_DEG); when F3
# merges, delete this block — the import above takes over with zero behaviour change.
_LENGTH_TO_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimetre": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimetre": 10.0,
    "m": 1000.0, "meter": 1000.0, "metre": 1000.0,
    "um": 0.001, "µm": 0.001, "micron": 0.001, "micrometer": 0.001,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8, "'": 304.8,
    "thou": 0.0254, "mil": 0.0254,
    "yd": 914.4, "yard": 914.4,
}
_ANGLE_TO_DEG = {
    "deg": 1.0, "degree": 1.0, "degrees": 1.0, "°": 1.0,
    "rad": 57.29577951308232, "radian": 57.29577951308232, "radians": 57.29577951308232,
    "grad": 0.9, "gradian": 0.9,
    "arcmin": 1.0 / 60.0, "arcsec": 1.0 / 3600.0,
}
_CANONICAL = {"length": "mm", "angle": "deg", "count": "count"}


def _fallback_normalize(value: float, unit: str, family: str = "length") -> dict:
    """Local mirror of ``normalize_dimension`` — reject-never-coerce; raises
    :class:`_InteractionError` on a family/unit mismatch (never silently coerces)."""
    if family not in _CANONICAL:
        raise _InteractionError(f"unknown dimension family: {family!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise _InteractionError(f"dimension value not numeric: {value!r}")
    u = (unit or "").strip()
    if family == "count":
        if u not in ("", "count"):
            raise _InteractionError(f"count takes no unit, got {unit!r}")
        return {"value": v, "unit": "count"}
    table = _LENGTH_TO_MM if family == "length" else _ANGLE_TO_DEG
    key = u if u in ('"', "'", "°", "µm") else u.lower()
    if key not in table:
        raise _InteractionError(f"unit {unit!r} is not a {family} unit")
    return {"value": v * table[key], "unit": _CANONICAL[family]}


def normalizer_source() -> str:
    """Which normaliser is live — ``"agent.interaction"`` once F3 merges, else
    ``"fallback"``. Surfaced on the envelope so a caller can see the seam."""
    return "agent.interaction" if _HAVE_CANONICAL else "fallback"


def unit_convert(value: Any, unit: str, family: str = "length") -> dict:
    """Convert ``value`` in ``unit`` to its canonical SI unit for ``family``
    (``length``->mm, ``angle``->deg, ``count``->count), returning the §1 answer
    envelope. A family/unit mismatch is a §6 ``bad_unit`` degradation — **never** a
    silent coercion (that is the whole point of a deterministic converter)."""
    fn = _canonical_normalize if _HAVE_CANONICAL else _fallback_normalize
    try:
        out = fn(value, unit, family)
    except _InteractionError as exc:
        return fail("bad_unit", f"{exc}. Pass a value + a unit that belongs to family {family!r}.")
    except Exception as exc:  # noqa: BLE001 - never raise across the tool boundary
        return fail("bad_unit", f"Could not normalise {value!r} {unit!r} ({family}): {exc}.")

    canon_v, canon_u = out["value"], out["unit"]
    return envelope(
        f"{value} {unit or ''}".strip() + f" = {canon_v:g} {canon_u}",
        units=canon_u,
        source_url=None,
        quote=None,
        confidence=BUNDLED,
        value=canon_v,
        unit=canon_u,
        family=family,
        normalizer=normalizer_source(),
    )

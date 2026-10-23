"""``unit_convert(value, unit, family?)`` — deterministic SI unit normalisation, contract
``contracts/research_tools.md`` §1 (the answer envelope). Pillar 8 principle: **code
converts units, never the model** (ksi/MPa is the classic silent 6.9x error).

Design decision: the canonical unit normaliser is
``services.agent.interaction.normalize_dimension``. This helper does **not**
parallel-implement it — it *imports and delegates* to it when the ``agent`` package is
importable, and falls back to a minimal local table (same reject-never-coerce semantics,
same ``{value, unit}`` return shape) when that package is not importable. Both
paths share this module's envelope wrapper, so behaviour is identical.
"""
from __future__ import annotations

import math
from typing import Any

from .common import BUNDLED, envelope, fail

# --- normaliser seam -------------------------------------------------------------------
# Prefer the canonical agent normaliser; fall back to a local mirror when unavailable.
try:  # pragma: no cover - exercised via the skipif test
    from services.agent.interaction import (  # type: ignore
        InteractionError as _InteractionError,
    )
    from services.agent.interaction import normalize_dimension as _canonical_normalize

    _HAVE_CANONICAL = True
except Exception:  # noqa: BLE001 - agent package not importable
    _canonical_normalize = None  # type: ignore
    _HAVE_CANONICAL = False

    class _InteractionError(Exception):  # type: ignore
        """Local stand-in mirroring ``services.agent.interaction.InteractionError``."""


# Fallback used only when the agent package is not importable. Keep these tables
# byte-for-byte aligned with ``services/agent/interaction.py`` (_LENGTH_TO_MM /
# _ANGLE_TO_DEG) so both paths behave identically.
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
    """Which normaliser is live — ``"services.agent.interaction"`` when importable, else
    ``"fallback"``. Surfaced on the envelope so a caller can see the seam."""
    return "services.agent.interaction" if _HAVE_CANONICAL else "fallback"


def _canonical_direction(value: Any, unit: str, family: str = "length") -> dict:
    """Legacy canonical-direction normalisation (``length``->mm, ``angle``->deg,
    ``count``->count). Behaviour is frozen: every pre-existing caller of
    :func:`unit_convert` routes here byte-identically."""
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


# =========================================================================================
# Arbitrary ``from_unit`` -> ``to_unit`` registry (additive; the canonical path above is
# untouched). Every factor below is an EXACT definition, cited inline. A unit is stored as
# ``(family, scale, offset)`` such that ``base = v * scale + offset`` — the offset exists
# solely for temperature, which is AFFINE: a pure scale factor is silently wrong for degF
# at every value except -40.
# =========================================================================================

#: canonical base unit per family (what ``scale``/``offset`` convert *to*)
_BASE_UNIT = {
    "length": "mm", "area": "mm^2", "volume": "mm^3", "angle": "deg",
    "mass": "kg", "force": "N", "pressure": "Pa", "torque": "N*m",
    "temperature": "K", "count": "count",
}

# --- exact defining constants (NIST SP 811 / ISO 80000 / NIST HB 44) ---------------------
_IN_MM = 25.4                    # international inch, 1959 definition: 1 in == 25.4 mm exactly
_FT_MM = 304.8                   # 12 in
_LB_KG = 0.45359237              # international avoirdupois pound, 1959: exact
_GN = 9.80665                    # standard gravity, exact (CGPM 1901)
_LBF_N = _LB_KG * _GN            # 4.4482216152605 N exactly
_PSI_PA = _LBF_N / 0.00064516    # 1 in^2 == 0.00064516 m^2 exactly -> 6894.757293168... Pa

#: unit symbol -> (family, scale-to-base, offset-to-base)
_UNITS: dict[str, tuple[str, float, float]] = {
    # length -> mm
    "mm": ("length", 1.0, 0.0),
    "cm": ("length", 10.0, 0.0),
    "m": ("length", 1000.0, 0.0),
    "km": ("length", 1.0e6, 0.0),
    "um": ("length", 1.0e-3, 0.0),
    "nm": ("length", 1.0e-6, 0.0),
    "in": ("length", _IN_MM, 0.0),
    "ft": ("length", _FT_MM, 0.0),
    "yd": ("length", 3.0 * _FT_MM, 0.0),
    "mil": ("length", _IN_MM / 1000.0, 0.0),
    # angle -> deg
    "deg": ("angle", 1.0, 0.0),
    "rad": ("angle", 180.0 / math.pi, 0.0),
    "grad": ("angle", 0.9, 0.0),
    "arcmin": ("angle", 1.0 / 60.0, 0.0),
    "arcsec": ("angle", 1.0 / 3600.0, 0.0),
    "rev": ("angle", 360.0, 0.0),
    # area -> mm^2
    "mm^2": ("area", 1.0, 0.0),
    "cm^2": ("area", 100.0, 0.0),
    "m^2": ("area", 1.0e6, 0.0),
    "in^2": ("area", _IN_MM ** 2, 0.0),      # 645.16 mm^2 exactly
    "ft^2": ("area", _FT_MM ** 2, 0.0),
    # volume -> mm^3
    "mm^3": ("volume", 1.0, 0.0),
    "cm^3": ("volume", 1000.0, 0.0),
    "mL": ("volume", 1000.0, 0.0),           # 1 mL == 1 cm^3 exactly
    "L": ("volume", 1.0e6, 0.0),
    "m^3": ("volume", 1.0e9, 0.0),
    "in^3": ("volume", _IN_MM ** 3, 0.0),    # 16387.064 mm^3 exactly
    "ft^3": ("volume", _FT_MM ** 3, 0.0),
    "gal": ("volume", 231.0 * _IN_MM ** 3, 0.0),  # US liquid gallon == 231 in^3 exactly
    # mass -> kg
    "kg": ("mass", 1.0, 0.0),
    "g": ("mass", 1.0e-3, 0.0),
    "mg": ("mass", 1.0e-6, 0.0),
    "t": ("mass", 1000.0, 0.0),
    "lb": ("mass", _LB_KG, 0.0),
    "oz": ("mass", _LB_KG / 16.0, 0.0),      # 1 lb == 16 oz exactly
    # force -> N
    "N": ("force", 1.0, 0.0),
    "kN": ("force", 1000.0, 0.0),
    "lbf": ("force", _LBF_N, 0.0),
    "kgf": ("force", _GN, 0.0),
    "kip": ("force", 1000.0 * _LBF_N, 0.0),
    # pressure / stress -> Pa
    "Pa": ("pressure", 1.0, 0.0),
    "kPa": ("pressure", 1.0e3, 0.0),
    "MPa": ("pressure", 1.0e6, 0.0),         # == N/mm^2
    "GPa": ("pressure", 1.0e9, 0.0),
    "psi": ("pressure", _PSI_PA, 0.0),
    "ksi": ("pressure", 1000.0 * _PSI_PA, 0.0),
    "bar": ("pressure", 1.0e5, 0.0),         # exact by definition
    "mbar": ("pressure", 100.0, 0.0),
    "atm": ("pressure", 101325.0, 0.0),      # standard atmosphere, exact
    "torr": ("pressure", 101325.0 / 760.0, 0.0),
    # torque -> N*m
    "N*m": ("torque", 1.0, 0.0),
    "N*mm": ("torque", 1.0e-3, 0.0),
    "kN*m": ("torque", 1000.0, 0.0),
    "lbf*in": ("torque", _LBF_N * (_IN_MM / 1000.0), 0.0),
    "lbf*ft": ("torque", _LBF_N * (_FT_MM / 1000.0), 0.0),
    "kgf*m": ("torque", _GN, 0.0),
    # temperature -> K (AFFINE)
    "K": ("temperature", 1.0, 0.0),
    "degC": ("temperature", 1.0, 273.15),                    # K = C + 273.15 exactly
    "degF": ("temperature", 5.0 / 9.0, 459.67 * 5.0 / 9.0),  # K = (F + 459.67) * 5/9
    # dimensionless
    "count": ("count", 1.0, 0.0),
}

#: canonical symbol -> extra spellings models actually write. Folded (case-insensitive,
#: whitespace-stripped) unless the spelling is listed in :data:`_CASE_SENSITIVE`.
_ALIASES: dict[str, tuple[str, ...]] = {
    "mm": ("millimeter", "millimeters", "millimetre", "millimetres"),
    "cm": ("centimeter", "centimeters", "centimetre", "centimetres"),
    "m": ("meter", "meters", "metre", "metres"),
    "km": ("kilometer", "kilometers", "kilometre", "kilometres"),
    "um": ("\u00b5m", "\u03bcm", "micron", "microns", "micrometer", "micrometre"),
    "nm": ("nanometer", "nanometre"),
    "in": ("inch", "inches", '"', "\u2033"),
    "ft": ("foot", "feet", "'", "\u2032"),
    "yd": ("yard", "yards"),
    "mil": ("mils", "thou", "thousandth"),
    "deg": ("degree", "degrees", "\u00b0", "degs"),
    "rad": ("radian", "radians"),
    "grad": ("gradian", "gradians", "gon"),
    "arcmin": ("arcminute", "arcminutes"),
    "arcsec": ("arcsecond", "arcseconds"),
    "rev": ("revolution", "revolutions", "turn", "turns"),
    "mm^2": ("mm2", "mm**2", "sqmm", "sq mm", "square mm"),
    "cm^2": ("cm2", "cm**2", "sqcm"),
    "m^2": ("m2", "m**2", "sqm"),
    "in^2": ("in2", "in**2", "sqin", "sq in", "inch^2", "square inch"),
    "ft^2": ("ft2", "ft**2", "sqft"),
    "mm^3": ("mm3", "mm**3", "cumm", "cu mm"),
    "cm^3": ("cm3", "cm**3", "cc", "ccm"),
    "m^3": ("m3", "m**3"),
    "mL": ("milliliter", "millilitre", "milliliters"),
    "L": ("liter", "litre", "liters", "litres"),
    "in^3": ("in3", "in**3", "cuin", "cu in", "cubic inch"),
    "ft^3": ("ft3", "ft**3", "cuft"),
    "gal": ("gallon", "gallons"),
    "kg": ("kilogram", "kilograms", "kilo", "kilos"),
    "g": ("gram", "grams", "gramme", "grammes"),
    "mg": ("milligram", "milligrams"),
    "t": ("tonne", "tonnes", "metric ton"),
    "lb": ("lbs", "lbm", "pound", "pounds", "pound-mass"),
    "oz": ("ounce", "ounces"),
    "N": ("newton", "newtons"),
    "kN": ("kilonewton", "kilonewtons"),
    "lbf": ("pound-force", "poundforce", "pound force"),
    "kgf": ("kilogram-force", "kp"),
    "kip": ("kips",),
    "Pa": ("pascal", "pascals"),
    "kPa": ("kilopascal", "kilopascals"),
    "MPa": ("megapascal", "megapascals", "N/mm^2", "N/mm2", "n/mm2"),
    "GPa": ("gigapascal", "gigapascals"),
    "psi": ("lbf/in^2", "lbf/in2", "lb/in^2", "psia"),
    "ksi": ("kpsi",),
    "bar": ("bars",),
    "mbar": ("millibar", "millibars"),
    "atm": ("atmosphere", "atmospheres"),
    "N*m": ("Nm", "N-m", "N m", "newton meter", "newton-meter", "newtonmetre",
            "newton metre", "joule/rad"),
    "N*mm": ("Nmm", "N-mm", "N mm", "newton millimeter", "newton-millimeter"),
    "kN*m": ("kNm", "kN-m", "kN m"),
    "lbf*in": ("lbf-in", "lbfin", "lbf in", "in*lbf", "in-lbf", "inlbf",
               "in-lb", "inlb", "in lb", "lb-in", "lbin", "lb in"),
    "lbf*ft": ("lbf-ft", "lbfft", "lbf ft", "ft*lbf", "ft-lbf", "ftlbf",
               "ft-lb", "ftlb", "ft lb", "lb-ft", "lbft", "lb ft"),
    "kgf*m": ("kgfm", "kgf-m"),
    "K": ("kelvin", "kelvins"),
    "degC": ("degc", "deg c", "\u00b0C", "\u00b0c", "celsius", "centigrade", "c"),
    "degF": ("degf", "deg f", "\u00b0F", "\u00b0f", "fahrenheit", "f"),
    "count": ("counts", "ea", "each", "pcs", "pc", "piece", "pieces", "qty", "unit",
              "units", "dimensionless", ""),
}

#: spellings whose CASE carries meaning — matched verbatim before any folding.
_CASE_SENSITIVE = frozenset({'"', "'", "\u2033", "\u2032", "\u00b0",
                             "\u00b5m", "\u03bcm", "\u00b0C", "\u00b0F"})

#: folded keys where guessing would be dangerous. Reached ONLY when the caller's spelling
#: did not match a symbol verbatim — i.e. they wrote a case variant we refuse to guess at.
_AMBIGUOUS: dict[str, str] = {
    "mm": "'mm' is millimetre but 'Mm' would be megametre",
    "nm": "'nm' is nanometre but 'Nm' is newton-metre (a torque)",
    "m": "'m' is metre but 'M' is the mega prefix",
    "g": "'g' is gram but 'G' is the giga prefix",
    "k": "'K' is kelvin but 'k' is the kilo prefix, not a unit",
    # NOTE: 'mpa' is deliberately NOT ambiguous — millipascal is not in this registry and
    # 'mpa' is the spelling models overwhelmingly write for MPa.
}


def _fold(sym: str) -> str:
    """Case/whitespace/separator-insensitive lookup key."""
    s = sym.strip().lower()
    for src, dst in (("\u00b7", "*"), ("\u22c5", "*"), ("\u00d7", "*"), ("**", "^")):
        s = s.replace(src, dst)
    return s.replace(" ", "")


def _build_folded() -> dict[str, str]:
    table: dict[str, str] = {}
    for canon in _UNITS:
        for spelling in (canon,) + _ALIASES.get(canon, ()):
            if spelling in _CASE_SENSITIVE:
                continue
            table.setdefault(_fold(spelling), canon)
    return table


def _build_exact() -> dict[str, str]:
    table: dict[str, str] = {}
    for canon in _UNITS:
        for spelling in (canon,) + _ALIASES.get(canon, ()):
            table.setdefault(spelling, canon)
    return table


_EXACT_SYMBOLS = _build_exact()
_FOLDED_SYMBOLS = _build_folded()


class _UnitError(Exception):
    """Unresolvable / ambiguous unit spelling (never escapes the tool boundary)."""


def _resolve_unit(sym: Any) -> tuple[str, str, float, float]:
    """``sym`` -> ``(canonical_symbol, family, scale, offset)``.

    Case-sensitive match first (``mm`` vs ``Mm``, ``nm`` vs ``Nm``, ``K`` vs ``k`` are all
    load-bearing); only then a folded match, and a folded key listed in
    :data:`_AMBIGUOUS` is REFUSED rather than guessed."""
    if sym is None:
        sym = ""
    if not isinstance(sym, str):
        raise _UnitError(f"unit must be a string, got {sym!r}")
    raw = sym.strip()
    canon = _EXACT_SYMBOLS.get(raw)
    if canon is None:
        key = _fold(raw)
        if key in _AMBIGUOUS:
            raise _UnitError(
                f"unit {sym!r} is ambiguous: {_AMBIGUOUS[key]}. Write the exact symbol "
                f"(case matters here) instead of a case variant."
            )
        canon = _FOLDED_SYMBOLS.get(key)
    if canon is None:
        raise _UnitError(f"unknown unit {sym!r}")
    fam, scale, offset = _UNITS[canon]
    return canon, fam, scale, offset


def supported_units() -> dict[str, list[str]]:
    """``family -> [canonical symbols]`` for the from->to registry (introspection/tests)."""
    out: dict[str, list[str]] = {}
    for canon, (fam, _s, _o) in _UNITS.items():
        out.setdefault(fam, []).append(canon)
    return out


def _fmt(x: float) -> str:
    return f"{x:.12g}"


def _convert_between(value: Any, from_unit: Any, to_unit: Any,
                     family: str | None = None) -> dict:
    """Arbitrary ``from_unit`` -> ``to_unit`` conversion. Refuses across families and
    never changes the direction or the family silently — both are echoed back."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fail("bad_unit", f"Value must be numeric, got {value!r}.")
    try:
        src, fam_a, scale_a, off_a = _resolve_unit(from_unit)
    except _UnitError as exc:
        return fail("bad_unit", f"{exc} (from_unit). Call research_unit_convert with a "
                                f"unit from the supported registry.")
    try:
        dst, fam_b, scale_b, off_b = _resolve_unit(to_unit)
    except _UnitError as exc:
        return fail("bad_unit", f"{exc} (to_unit). Call research_unit_convert with a "
                                f"unit from the supported registry.")

    if fam_a != fam_b:
        return fail(
            "bad_unit",
            f"Cannot convert {from_unit!r} (family {fam_a!r}) to {to_unit!r} "
            f"(family {fam_b!r}): {fam_a} and {fam_b} are different physical families. "
            f"No conversion exists; pass two units of the same family.",
        )
    if family and family != fam_a:
        return fail(
            "bad_unit",
            f"family={family!r} contradicts the units: {from_unit!r} -> {to_unit!r} is a "
            f"{fam_a!r} conversion. Drop `family` (it is resolved from the units) or fix it.",
        )

    base = v * scale_a + off_a
    out = (base - off_b) / scale_b
    return envelope(
        f"{_fmt(v)} {src} = {_fmt(out)} {dst}",
        units=dst,
        source_url=None,
        quote=None,
        confidence=BUNDLED,
        value=out,
        unit=dst,
        family=fam_a,
        from_unit=src,
        to_unit=dst,
        from_value=v,
        interpreted=(f"read {from_unit!r} as {src} and {to_unit!r} as {dst} "
                     f"(family {fam_a}); converted {src} -> {dst}"),
        normalizer="research.unit_convert.registry",
    )


def _batch_item(item: Any, index: int) -> dict:
    if not isinstance(item, dict):
        return fail("bad_unit", f"batch entry {index} must be an object with "
                                f"value/from_unit/to_unit, got {item!r}.")
    value = item.get("value", item.get("v"))
    frm = item.get("from_unit", item.get("from", item.get("unit")))
    to = item.get("to_unit", item.get("to"))
    fam = item.get("family")
    if to is None:
        if fam is None and frm is None:
            return fail("bad_unit", f"batch entry {index} needs from_unit and to_unit.")
        return _canonical_direction(value, frm or "", fam or "length")
    return _convert_between(value, frm, to, family=fam)


def unit_convert(value: Any = None, unit: str | None = None, family: str | None = None,
                 from_unit: str | None = None, to_unit: str | None = None,
                 batch: Any = None) -> dict:
    """Deterministic unit conversion — **code converts, never the model** (the ksi/MPa
    6.9x error is the motivating case, and it is now actually supported).

    Three call shapes, all returning the §1 answer envelope:

    * ``unit_convert(value, unit, family)`` — legacy canonical direction
      (``length``->mm, ``angle``->deg, ``count``->count). Unchanged.
    * ``unit_convert(value, from_unit=..., to_unit=...)`` — arbitrary conversion; the
      family is resolved from the units, and a cross-family pair is REFUSED naming both
      families (never coerced).
    * ``unit_convert(batch=[{value, from_unit, to_unit}, ...])`` — per-entry results; a
      bad entry carries an error and **no** value, the good ones still convert.
    """
    if batch is not None:
        if not isinstance(batch, (list, tuple)):
            return fail("bad_unit", f"batch must be a list of conversion objects, "
                                    f"got {type(batch).__name__}.")
        results = [_batch_item(it, i) for i, it in enumerate(batch)]
        n_ok = sum(1 for r in results if r.get("ok"))
        return envelope(
            f"{n_ok}/{len(results)} conversions succeeded",
            units=None,
            source_url=None,
            quote=None,
            confidence=BUNDLED,
            results=results,
            count=len(results),
            succeeded=n_ok,
            failed=len(results) - n_ok,
            normalizer="research.unit_convert.registry",
        )

    if from_unit is not None or to_unit is not None:
        src = from_unit if from_unit is not None else unit
        if src is None or to_unit is None:
            return fail("bad_unit",
                        "An arbitrary conversion needs BOTH from_unit and to_unit "
                        "(e.g. from_unit='ksi', to_unit='MPa'). For the canonical "
                        "direction call unit_convert(value, unit, family).")
        return _convert_between(value, src, to_unit, family=family)

    if unit is None:
        return fail("bad_unit",
                    "Pass either unit= (canonical direction) or from_unit=/to_unit= "
                    "(arbitrary conversion).")
    return _canonical_direction(value, unit, family or "length")

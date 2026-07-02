"""``eng_calc(name, **params)`` — deterministic engineering formulas + a stress-unit
sanity guard. Contract ``contracts/research_tools.md`` §1 (answer envelope). Pillar 8:
the model must not do arithmetic from weights — it names a formula, **code computes it**
in SI and range-checks the result.

The stress guard implements the master-plan **ksi/MPa 6.9x guard**: a metal yield quoted
as "40" is 40 ksi (276 MPa), not 40 MPa — the single most common silent unit error in
mechanical work. ``check_stress`` flags a value whose only plausible reading is the other
unit, so a quote is never trusted at the wrong scale.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

from .common import BUNDLED, MEDIUM, envelope, fail

# Stress unit -> MPa (the canonical stress unit). Deterministic; code owns this table.
_STRESS_TO_MPA = {
    "mpa": 1.0, "n/mm2": 1.0, "n/mm^2": 1.0, "nmm2": 1.0,
    "kpa": 1.0e-3, "pa": 1.0e-6, "gpa": 1.0e3,
    "ksi": 6.894757293168361, "psi": 6.894757293168361e-3,
    "bar": 0.1, "atm": 0.101325,
}
# Plausible band for a structural/engineering stress (yield/ultimate/applied), MPa.
_STRESS_MIN_MPA = 0.05
_STRESS_MAX_MPA = 6000.0
# ksi<->MPa ratio — the classic 6.9x silent error.
_KSI_MPA = _STRESS_TO_MPA["ksi"]


def stress_to_mpa(value: float, unit: str) -> Optional[float]:
    """Convert a stress ``value``+``unit`` to MPa, or ``None`` for an unknown unit."""
    factor = _STRESS_TO_MPA.get((unit or "").strip().lower())
    if factor is None:
        return None
    return float(value) * factor


def check_stress(value: Any, unit: str, *, raw_quote: Optional[str] = None,
                 kind: str = "stress") -> dict:
    """Normalise a stress to MPa with a range check + the ksi/MPa 6.9x guard.

    Returns the §1 envelope with ``value`` (MPa), ``units:"MPa"`` and, when the labelled
    unit is suspicious, a ``warning`` describing the likely swap. A truly implausible
    value (out of range under *both* readings) is a §6 ``bad_value`` degradation."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fail("bad_value", f"stress value not numeric: {value!r}.")
    mpa = stress_to_mpa(v, unit)
    if mpa is None:
        return fail("bad_unit", f"unknown stress unit {unit!r}; use one of {sorted(_STRESS_TO_MPA)}.")

    in_range = _STRESS_MIN_MPA <= abs(mpa) <= _STRESS_MAX_MPA
    warning = None
    confidence = BUNDLED
    u = (unit or "").strip().lower()
    # 6.9x guard: if labelled MPa but the value only makes engineering sense as ksi
    # (i.e. reinterpreting as ksi lands in range while the MPa reading is implausibly low),
    # surface it rather than trusting the label.
    if u in ("mpa", "n/mm2", "n/mm^2", "nmm2") and abs(mpa) < 20.0:
        as_ksi = abs(v) * _KSI_MPA
        if _STRESS_MIN_MPA <= as_ksi <= _STRESS_MAX_MPA and as_ksi > 20.0:
            warning = (f"{v} MPa is unusually low for a {kind}; if the source unit was ksi "
                       f"this is {as_ksi:.1f} MPa (6.9x). Verify the source unit.")
            confidence = MEDIUM
    elif not in_range:
        return fail("bad_value",
                    f"{v} {unit} = {mpa:g} MPa is outside the plausible {kind} band "
                    f"[{_STRESS_MIN_MPA}, {_STRESS_MAX_MPA}] MPa; check value and unit.")

    return envelope(
        f"{v} {unit} = {mpa:g} MPa ({kind})",
        units="MPa",
        source_url=None,
        quote=raw_quote,
        confidence=confidence,
        value=mpa,
        unit="MPa",
        raw_quote=raw_quote,
        warning=warning,
    )


# --------------------------------------------------------------------------- formulas
# Each formula: (fn(params)->(value, unit), required_param_names, doc). All inputs SI-ish
# as documented; code does the arithmetic. Stress outputs in MPa (N, mm, mm^2, mm^4).

def _axial_stress(p):  # sigma = F/A ; F [N], A [mm^2] -> MPa
    return p["force_n"] / p["area_mm2"], "MPa"


def _bending_stress(p):  # sigma = M c / I ; M [N.mm], c [mm], I [mm^4] -> MPa
    return p["moment_nmm"] * p["c_mm"] / p["I_mm4"], "MPa"


def _hoop_stress(p):  # sigma = p r / t ; p [MPa], r [mm], t [mm] -> MPa
    return p["pressure_mpa"] * p["radius_mm"] / p["thickness_mm"], "MPa"


def _safety_factor(p):  # n = strength/stress (dimensionless)
    return p["strength_mpa"] / p["stress_mpa"], None


def _cantilever_end_deflection(p):  # d = F L^3 / (3 E I) ; F [N], L [mm], E [MPa], I [mm^4] -> mm
    return p["force_n"] * p["length_mm"] ** 3 / (3.0 * p["E_mpa"] * p["I_mm4"]), "mm"


def _circle_area(p):  # A = pi d^2 / 4 ; d [mm] -> mm^2
    return math.pi * p["diameter_mm"] ** 2 / 4.0, "mm^2"


_FORMULAS: dict[str, tuple[Callable, tuple, str]] = {
    "axial_stress": (_axial_stress, ("force_n", "area_mm2"),
                     "Axial stress sigma = F/A. F[N], A[mm^2] -> MPa."),
    "bending_stress": (_bending_stress, ("moment_nmm", "c_mm", "I_mm4"),
                       "Bending stress sigma = M*c/I. M[N.mm], c[mm], I[mm^4] -> MPa."),
    "hoop_stress": (_hoop_stress, ("pressure_mpa", "radius_mm", "thickness_mm"),
                    "Thin-wall hoop stress sigma = p*r/t. p[MPa], r,t[mm] -> MPa."),
    "safety_factor": (_safety_factor, ("strength_mpa", "stress_mpa"),
                      "Safety factor n = strength/stress. [MPa]/[MPa] -> dimensionless."),
    "cantilever_end_deflection": (_cantilever_end_deflection,
                                  ("force_n", "length_mm", "E_mpa", "I_mm4"),
                                  "End-load cantilever tip deflection d = F*L^3/(3EI). -> mm."),
    "circle_area": (_circle_area, ("diameter_mm",), "Circle area A = pi*d^2/4. d[mm] -> mm^2."),
}


def formulas() -> dict:
    """Available formula names -> their docstring (for discovery)."""
    return {k: doc for k, (_, _, doc) in _FORMULAS.items()}


def eng_calc(name: str, raw_quote: Optional[str] = None, **params: Any) -> dict:
    """Evaluate a named deterministic engineering formula, returning the §1 envelope.

    ``name`` selects a formula (see :func:`formulas`); ``params`` are its SI-ish inputs.
    A missing/unknown formula or a non-finite result degrades (§6) rather than raising.
    ``raw_quote`` (optional) is echoed for provenance."""
    spec = _FORMULAS.get((name or "").strip().lower())
    if spec is None:
        return fail("not_found", f"unknown formula {name!r}; known: {sorted(_FORMULAS)}.")
    fn, required, doc = spec
    missing = [k for k in required if k not in params]
    if missing:
        return fail("bad_query", f"formula {name!r} needs params {list(required)}; missing {missing}.")
    try:
        vals = {k: float(params[k]) for k in required}
        value, unit = fn(vals)
    except ZeroDivisionError:
        return fail("bad_value", f"formula {name!r}: division by zero (a denominator param is 0).")
    except (TypeError, ValueError) as exc:
        return fail("bad_value", f"formula {name!r}: non-numeric param ({exc}).")
    if not math.isfinite(value):
        return fail("bad_value", f"formula {name!r} produced a non-finite result.")
    return envelope(
        f"{name} = {value:g}" + (f" {unit}" if unit else ""),
        units=unit,
        source_url=None,
        quote=raw_quote,
        confidence=BUNDLED,
        value=value,
        unit=unit,
        formula=name,
        inputs=vals,
        raw_quote=raw_quote,
    )

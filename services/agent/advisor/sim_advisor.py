"""Pure simulation setup and result advisor.

``precheck`` lints normalized current-tool arguments without imposing structural
fixtures/loads on families that do not use them. In particular, free-free modal and
flux-only transient thermal are valid, while static/fatigue/buckling need a reacted
non-zero load. Optional capability evidence can block a requested unavailable backend.

Analytic helpers return ``None`` rather than non-finite or unjustified numbers. The beam
anchor is limited to a finite, slender rectangular-bbox mapping with a force load;
pressure is not converted without area. Thermal and fatigue anchors state their units
and handbook equations.

``postcheck`` separates the nominal yield policy from physical interpretation:
``margin_mpa = yield_mpa / SF - stress``. Exceeding that allowable is not called
yielding; nominal stress must reach yield, and fracture language requires ultimate.
An overall PASS additionally needs a reacted non-zero setup with valid explicit material
properties, a reported mesh, and either numeric two-mesh convergence or a same-order
independent anchor. Missing/failed/skipped/non-converged evidence is WARN, never PASS.

Blocker emission is gated by ``CHUDWARE_SIM_PRECHECK_BLOCK`` (default enabled).
Returned items follow the existing advisor item shape; the optional ``_anchor`` key is
private round-trip data for postcheck.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
from typing import Optional

# ---------------------------------------------------------------------------
# Margin policy loader
# ---------------------------------------------------------------------------

_POLICY_PATH = pathlib.Path(__file__).parent / "margin_policy.json"
_POLICY: Optional[dict] = None


def _load_policy() -> dict:
    global _POLICY  # noqa: PLW0603
    if _POLICY is None:
        with open(_POLICY_PATH) as fh:
            _POLICY = json.load(fh)
    return _POLICY


def resolve_material_class(mat_name: str) -> dict:
    """Return the margin-policy record for *mat_name* (matched by keyword heuristic).

    Falls back to the ``"general"`` record when no keyword matches.
    """
    policy = _load_policy()
    low = mat_name.lower()
    for key, rec in policy.items():
        if key.startswith("_"):
            continue
        if key == "general":
            continue
        for kw in rec.get("keywords", []):
            if kw in low:
                return rec
    return policy["general"]


def _finite_float(value, *, positive: bool = False, nonnegative: bool = False) -> Optional[float]:
    """Return a finite float satisfying the requested sign constraint, else ``None``."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if positive and number <= 0.0:
        return None
    if nonnegative and number < 0.0:
        return None
    return number


def _mesh_fineness_value(value) -> Optional[float]:
    """Normalize the public numeric/preset mesh argument without inventing a length."""
    if isinstance(value, str):
        preset = {
            "coarse": 0.25,
            "medium": 0.5,
            "fine": 0.75,
            "finest": 1.0,
        }.get(value.strip().lower())
        if preset is not None:
            return preset
    return _finite_float(value)


# ---------------------------------------------------------------------------
# Beam anchor helper
# ---------------------------------------------------------------------------

def _beam_aspect_ok(bbox: dict) -> tuple[bool, float, str]:
    """Return (confident, aspect_ratio, axis) for a bounding-box dict.

    Confident when the longest axis is ≥ 3× the larger cross-section side, so a
    thin plate is not mistaken for a beam.
    Returns (False, 0.0, "") when the bbox is missing or degenerate.
    """
    dims = {}
    for ax in ("x", "y", "z"):
        raw = bbox.get(f"{ax}_mm")
        if raw is None:
            raw = bbox.get(ax)
        value = _finite_float(raw, positive=True)
        if value is not None:
            dims[ax] = value
    if len(dims) < 3:
        return False, 0.0, ""
    sorted_axes = sorted(dims, key=lambda k: -dims[k])
    longest = dims[sorted_axes[0]]
    cross_size = dims[sorted_axes[1]]
    if cross_size <= 0.0 or not math.isfinite(cross_size):
        return False, 0.0, ""
    aspect = longest / cross_size
    if not math.isfinite(aspect):
        return False, 0.0, ""
    return aspect >= 3.0, aspect, sorted_axes[0]


def compute_beam_anchor(
    bbox: dict,
    E_mpa: float,
    load_n: float,
    support: str = "cantilever",
    load: str = "point",
    section: str = "rect",
    load_vector_n: Optional[list[float]] = None,
) -> Optional[dict]:
    """Compute a slender rectangular-beam stress anchor.

    ``load_vector_n`` is optional for compatibility with explicit hand-calc callers.
    When supplied (the precheck path), it must be a single transverse, axis-aligned
    force. Its direction selects section depth ``h``; axial or diagonal loading returns
    ``None`` rather than attaching false confidence to the wrong section modulus.
    """
    confident, aspect, long_ax = _beam_aspect_ok(bbox)
    modulus = _finite_float(E_mpa, positive=True)
    force = _finite_float(load_n, positive=True)
    if not confident or modulus is None or force is None:
        return None

    try:
        from services.research.calcs.beam import beam_calc_raw  # noqa: PLC0415
    except ImportError:
        return None

    dims = {}
    for ax in ("x", "y", "z"):
        raw = bbox.get(f"{ax}_mm")
        if raw is None:
            raw = bbox.get(ax)
        dims[ax] = float(raw)
    sorted_axes = sorted(dims, key=lambda k: -dims[k])
    L = dims[long_ax]
    cross_axes = [axis for axis in ("x", "y", "z") if axis != long_ax]
    load_axis = None
    if load_vector_n is not None:
        if not isinstance(load_vector_n, (list, tuple)) or len(load_vector_n) != 3:
            return None
        vector = [_finite_float(component) for component in load_vector_n]
        if any(component is None for component in vector):
            return None
        vector_mag = math.sqrt(sum(component * component for component in vector))
        if vector_mag <= 0.0 or abs(vector_mag - force) > max(1e-9, 0.01 * force):
            return None
        components = dict(zip(("x", "y", "z"), (abs(component) for component in vector)))
        if components[long_ax] > 0.1 * vector_mag:
            return None
        load_axis = max(cross_axes, key=lambda axis: components[axis])
        other_axis = next(axis for axis in cross_axes if axis != load_axis)
        if components[other_axis] > 0.1 * vector_mag:
            return None
        h = dims[load_axis]
        b = dims[other_axis]
    else:
        b = dims[sorted_axes[1]]
        h = dims[sorted_axes[2]]

    try:
        raw = beam_calc_raw(
            support=support,
            load=load,
            section=section,
            L_mm=L,
            E_mpa=modulus,
            F_n=force,
            b_mm=b,
            h_mm=h,
        )
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None

    if not math.isfinite(raw.get("max_stress_mpa", float("nan"))):
        return None

    return {
        "max_stress_mpa": raw["max_stress_mpa"],
        "support": support,
        "load": load,
        "section": section,
        "L_mm": L,
        "b_mm": b,
        "h_mm": h,
        "aspect_ratio": round(aspect, 2),
        "load_axis": load_axis,
        "source": "beam_calc_raw (Shigley §3-4)",
    }


# ---------------------------------------------------------------------------
# Thermal anchor helper (steady-state 1D plane-wall Fourier reference)
# ---------------------------------------------------------------------------

def thermal_anchor(
    bbox: dict,
    k_w_mk: float,
    t_hot_c: float,
    t_cold_c: Optional[float] = None,
    h_w_m2k: Optional[float] = None,
    t_amb_c: Optional[float] = None,
) -> Optional[dict]:
    """Compute a 1D plane-wall steady-conduction anchor (Fourier reference).

    Same shape/contract as :func:`compute_beam_anchor`: returns a dict with the analytic
    reference numbers + provenance, or ``None`` when the mapping is not confident (missing
    bbox, non-positive conductivity, or neither a cold-face temperature nor a convection film
    to close the problem).

    The wall thickness ``L`` is taken as the SHORTEST bbox side (the through-thickness
    direction heat crosses); the face area is the product of the other two sides. Two closed
    forms, matching the FEA validation cases:

      * Dirichlet–Dirichlet (``t_cold_c`` given): pure conduction,
            q'' = k (t_hot - t_cold) / L        [W/m²],   T profile linear.
      * Dirichlet–Robin (``h_w_m2k`` + ``t_amb_c`` given): series resistance,
            q'' = (t_hot - t_amb) / (L/k + 1/h),  T_cold = t_amb + q''/h.

    Units: ``k`` in W/(m·K), ``L`` in mm (converted to m internally), ``h`` in W/(m²·K).

    Parameters
    ----------
    bbox : dict
        Keys ``x_mm``/``y_mm``/``z_mm`` (bounding-box side lengths, mm).
    k_w_mk : float
        Thermal conductivity, W/(m·K) (from the material thermal table).
    t_hot_c : float
        Fixed hot-face temperature, °C.
    t_cold_c : float | None
        Fixed cold-face temperature, °C (Dirichlet–Dirichlet case).
    h_w_m2k, t_amb_c : float | None
        Convection film coefficient W/(m²·K) + ambient °C (Dirichlet–Robin case).
    """
    dims = {}
    for ax in ("x", "y", "z"):
        raw = bbox.get(f"{ax}_mm")
        if raw is None:
            raw = bbox.get(ax)
        value = _finite_float(raw, positive=True)
        if value is not None:
            dims[ax] = value
    k = _finite_float(k_w_mk, positive=True)
    hot = _finite_float(t_hot_c)
    if len(dims) < 3 or k is None or hot is None:
        return None

    sorted_axes = sorted(dims, key=lambda a: dims[a])  # ascending
    L_mm = dims[sorted_axes[0]]                        # thinnest side = through-thickness
    L_m = L_mm / 1000.0
    if L_m <= 0.0 or not math.isfinite(L_m):
        return None

    cold = _finite_float(t_cold_c) if t_cold_c is not None else None
    if cold is not None:
        # Dirichlet–Dirichlet plane wall.
        q = k * (hot - cold) / L_m
        if not math.isfinite(q):
            return None
        return {
            "mode": "conduction",
            "q_w_m2": q,
            "t_hot_c": hot,
            "t_cold_c": cold,
            "L_mm": L_mm,
            "k_w_mk": k,
            "source": "1D plane wall q=kΔT/L (Incropera, Fundamentals of Heat & Mass Transfer §3.1)",
        }
    h = _finite_float(h_w_m2k, positive=True) if h_w_m2k is not None else None
    ambient = _finite_float(t_amb_c) if t_amb_c is not None else None
    if h is not None and ambient is not None:
        # Dirichlet–Robin (series resistance) plane wall.
        resistance = L_m / k + 1.0 / h
        if resistance <= 0.0 or not math.isfinite(resistance):
            return None
        q = (hot - ambient) / resistance
        t_cold = ambient + q / h
        if not (math.isfinite(q) and math.isfinite(t_cold)):
            return None
        return {
            "mode": "convection",
            "q_w_m2": q,
            "t_hot_c": hot,
            "t_cold_c": t_cold,
            "t_amb_c": ambient,
            "h_w_m2k": h,
            "L_mm": L_mm,
            "k_w_mk": k,
            "source": "1D wall series resistance q=(T_hot-T_amb)/(L/k+1/h) "
                      "(Incropera §3.1 thermal circuits)",
        }
    # No cold-face datum (neither fixed cold temp nor convection) -> not confident.
    return None


# ---------------------------------------------------------------------------
# Fatigue anchor helper (Goodman / Soderberg S-N hand-calc reference)
# ---------------------------------------------------------------------------

#: Citation for every number fatigue_anchor produces. The criteria equations and the
#: S-N line constants are hand-typed from the named handbook sections; no text is
#: reproduced. The same Eq 6-14..6-16 / Eq 6-8 references are already validated
#: against literal hand-calc values in scripts/vv_benchmark.py (FAT1..FAT3).
_FATIGUE_SOURCE = (
    "Shigley's Mechanical Engineering Design, 10th ed.: mean-stress criteria §6-12 "
    "(Soderberg Eq. 6-45, mod-Goodman Eq. 6-46); high-cycle S-N line Eq. 6-14..6-16 "
    "(f=0.9); unmodified endurance estimate Se'=0.5·Sut (Sut≤1400 MPa) else 700 MPa, "
    "Eq. 6-8"
)


def fatigue_anchor(
    sigma_max_mpa: float,
    sigma_min_mpa: float,
    ultimate_mpa: float,
    yield_mpa: Optional[float] = None,
    endurance_mpa: Optional[float] = None,
    method: str = "goodman",
) -> Optional[dict]:
    """Hand-calc high-cycle-fatigue reference (S-N anchor) for a cyclic stress state.

    Same contract as :func:`compute_beam_anchor` / :func:`thermal_anchor`: returns a
    dict with the analytic reference numbers + provenance (``source`` cites the
    handbook), or ``None`` when the mapping is not confident (missing/invalid strength
    data, reversed max/min, Soderberg without a yield strength, unknown method).

    The math is INDEPENDENT of the engine solver (core/sim/fea_fatigue.cpp) and of the
    host mirror (services/mcp/fea_advanced.py) — it exists to cross-check them:

      sa = (σmax − σmin)/2,  sm = (σmax + σmin)/2
      sa_eq  (Goodman)   = sa / (1 − sm⁺/Sut)          [Shigley Eq. 6-46 rearranged]
      sa_eq  (Soderberg) = sa / (1 − sm⁺/Sy)           [Shigley Eq. 6-45 rearranged]
      N = (sa_eq/a)^(1/b),  a=(0.9·Sut)²/Se,  b=−⅓·log10(0.9·Sut/Se)   [Eq. 6-14..6-16]

    where sm⁺ = max(sm, 0): a compressive mean stress is NOT taken as a fatigue
    benefit here, deliberately matching the engine's convention (fea_fatigue.cpp
    clamps the mean the same way) so anchor-vs-engine comparisons are apples-to-apples.

    Parameters
    ----------
    sigma_max_mpa, sigma_min_mpa : float
        Peak / valley of the cyclic stress at the critical point, MPa.
    ultimate_mpa : float
        Sut (required).
    yield_mpa : float | None
        Sy — required for ``method="soderberg"``.
    endurance_mpa : float | None
        Corrected endurance limit Se. When absent, the unmodified estimate
        Se' = 0.5·Sut (capped at 700 MPa for Sut > 1400 MPa, Shigley Eq. 6-8) is
        used and flagged in ``se_source``.
    method : str
        ``"goodman"`` (default) or ``"soderberg"``.
    """
    smax = _finite_float(sigma_max_mpa)
    smin = _finite_float(sigma_min_mpa)
    sut = _finite_float(ultimate_mpa, positive=True)
    if smax is None or smin is None or sut is None or smax < smin:
        return None
    method = (method or "").lower()
    if method not in ("goodman", "soderberg"):
        return None
    sy = _finite_float(yield_mpa, positive=True) if yield_mpa is not None else None
    if method == "soderberg" and sy is None:
        return None

    # Endurance limit: given (already Marin-corrected by the caller) or the
    # unmodified Shigley Eq. 6-8 estimate. An explicitly invalid value is not
    # silently replaced by an estimate.
    if endurance_mpa is not None:
        se = _finite_float(endurance_mpa, positive=True)
        if se is None:
            return None
        se_source = "given (caller-corrected)"
    else:
        se = 0.5 * sut if sut <= 1400.0 else 700.0
        se_source = "estimated Se'=0.5*Sut capped 700 MPa (Shigley Eq. 6-8, unmodified)"

    sa = (smax - smin) / 2.0
    sm = (smax + smin) / 2.0
    if not (math.isfinite(sa) and math.isfinite(sm)):
        return None
    sm_eff = max(sm, 0.0)  # compressive mean -> no benefit (matches engine)

    out = {
        "method": method,
        "sa_mpa": sa,
        "sm_mpa": sm,
        "se_mpa": se,
        "se_source": se_source,
        "sut_mpa": sut,
        "sy_mpa": sy,
        "source": _FATIGUE_SOURCE,
    }

    if sa <= 0.0:
        # No stress amplitude — not a fatigue problem (static load).
        out.update({"sa_eq_mpa": 0.0, "regime": "static_no_amplitude",
                    "cycles": None, "fos_endurance": None})
        return out

    denom_ref = sut if method == "goodman" else sy
    denom = 1.0 - sm_eff / denom_ref
    if denom <= 1e-9:
        # Mean stress at/above the static strength — fails statically, not in fatigue.
        out.update({"sa_eq_mpa": None, "regime": "static_overload",
                    "cycles": 1, "fos_endurance": 0.0})
        return out

    sa_eq = sa / denom
    if not math.isfinite(sa_eq):
        return None
    fos_endurance = se / sa_eq
    if not math.isfinite(fos_endurance):
        return None
    out["sa_eq_mpa"] = sa_eq
    out["fos_endurance"] = fos_endurance

    # S-N life (Shigley Eq. 6-14..6-16, f=0.9). Same clamps as the host mirror
    # (services/mcp/fea_advanced.py sn_life) so cross-checks agree bit-for-bit.
    if sa_eq <= se:
        out.update({"regime": "infinite", "cycles": None})
        return out
    if sa_eq >= sut:
        out.update({"regime": "static_overload", "cycles": 1})
        return out
    f = 0.9
    ratio = (f * sut) / se
    if not math.isfinite(ratio) or ratio <= 1.0:
        # The two S-N anchor points coincide or reverse; the log-line has no
        # physically meaningful negative slope.
        return None
    try:
        a = (f * sut) ** 2 / se
    except OverflowError:
        return None
    if not math.isfinite(a) or a <= 0.0:
        return None
    b = -(1.0 / 3.0) * math.log10(ratio)
    try:
        n = (sa_eq / a) ** (1.0 / b)
    except (OverflowError, ZeroDivisionError):
        return None
    if not math.isfinite(n) or n < 1.0:
        n = 1.0
    if n > 1e6:
        n = 1e6
    out["cycles"] = n
    out["regime"] = "finite"
    return out


# ---------------------------------------------------------------------------
# BC lint helpers
# ---------------------------------------------------------------------------

def _study_kind(study: dict) -> str:
    return str(study.get("type") or study.get("study_kind") or "").strip().lower()


def _zero_load_diagnostic(study: dict) -> bool:
    purpose = str(study.get("purpose") or "").strip().lower()
    return bool(study.get("zero_load_diagnostic")) or purpose in {
        "zero_load_diagnostic", "zero-load diagnostic", "null_load_check",
    }


def _load_is_nonzero(load: object) -> bool:
    """Recognise public and normalized structural-load shapes without mixing units."""
    if not isinstance(load, dict):
        return False
    for key in ("pressure", "magnitude"):
        value = _finite_float(load.get(key))
        if value is not None and value != 0.0:
            return True
    vector = load.get("force")
    if vector is None:
        vector = load.get("vector")
    if isinstance(vector, (list, tuple)):
        return any((_finite_float(component) or 0.0) != 0.0 for component in vector)
    return False


def _bc_issues(study: dict) -> list[dict]:
    """Deterministic structural BC checks matched to each public solver surface."""
    issues = []
    kind = _study_kind(study)
    fixtures = study.get("fixtures") or []
    raw_loads = study.get("loads")
    if raw_loads is None:
        raw_loads = study.get("load")
    loads = raw_loads if isinstance(raw_loads, list) else ([raw_loads] if raw_loads else [])
    dangling = study.get("dangling_refs") or []

    fixture_required = (
        "static" in kind or "fatigue" in kind or "buckling" in kind
        or "frequency_response" in kind or "vibration" in kind
    ) and "modal" not in kind
    load_required = "static" in kind or "fatigue" in kind or "buckling" in kind
    diagnostic = _zero_load_diagnostic(study)

    if fixture_required and not fixtures and not diagnostic:
        issues.append({
            "kind": "no_fixture",
            "detail": "No fixtures were supplied. This study needs a mounted face; without "
                      "a fixture its structural solve has rigid-body motion.",
        })
    if load_required and not any(_load_is_nonzero(load) for load in loads):
        if diagnostic:
            issues.append({
                "kind": "zero_load_diagnostic",
                "detail": (
                    "Intentional zero-load diagnostic recorded. Do not interpret zero stress "
                    "or saturated FoS as capacity; structural solver tools refuse zero-load runs."
                ),
            })
        else:
            issues.append({
                "kind": "no_load",
                "detail": (
                    "Study has no loads with a supported non-zero excitation. Static/fatigue "
                    "and buckling tools refuse this setup rather than returning capacity "
                    "evidence."
                ),
            })

    # Empty fixtures are intentional for run_fea_modal: a free-free solve should expose six
    # near-zero rigid-body modes before the elastic modes.
    if fixture_required and fixtures and not diagnostic \
            and all(f in ("all", "all_faces") for f in fixtures):
        issues.append({
            "kind": "all_fixed",
            "detail": (
                "All faces are fixed — displacement is suppressed and the stress field does "
                "not represent a physical mount."
            ),
        })

    for ref in ([] if diagnostic else dangling):
        issues.append({
            "kind": "dangling_ref",
            "detail": (
                f"Fixture/load reference {ref!r} does not resolve to a current face. "
                "Use query_geometry before launching the solver."
            ),
        })
    return issues


def _is_thermal(study: dict) -> bool:
    """True for either steady or transient thermal studies."""
    return "thermal" in _study_kind(study)


def _thermal_bc_issues(study: dict) -> list[dict]:
    """Lint the actual run_fea_thermal steady/transient boundary shapes."""
    issues = []
    transient = bool(study.get("transient")) or "transient" in _study_kind(study)
    generic_fixtures = list(study.get("fixtures") or [])
    hot_faces = list(study.get("hot_faces") or [])
    cold_faces = list(study.get("cold_faces") or [])
    fixtures = generic_fixtures + hot_faces + cold_faces
    loads = study.get("loads") or []

    fixed_temps = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        raw = fixture.get("temp_c")
        if raw is None:
            raw = fixture.get("temperature")
        value = _finite_float(raw)
        if value is not None:
            fixed_temps.append(value)
    transient_spec = study.get("transient")
    t_init = _finite_float(
        transient_spec.get("t_init_c", 20.0)
        if isinstance(transient_spec, dict) else 20.0
    )
    has_fixed_drive = bool(
        transient and t_init is not None
        and any(temp != t_init for temp in fixed_temps)
    )
    has_convection = any(
        isinstance(load, dict)
        and _finite_float(
            load.get("h") if load.get("h") is not None else load.get("h_w_m2k"),
            positive=True,
        ) is not None
        and t_init is not None
        and _finite_float(load.get("t_amb_c", 20.0)) not in (None, t_init)
        for load in loads
    )
    has_flux = any(
        isinstance(load, dict)
        and (_finite_float(
            load.get("magnitude")
            if load.get("magnitude") is not None else load.get("flux_w_m2")
        ) or 0.0) != 0.0
        for load in loads
    )

    if transient:
        if not has_fixed_drive and not has_flux and not has_convection:
            issues.append({
                "kind": "thermal_no_load",
                "detail": (
                    "Transient thermal study has no non-zero thermal input relative to "
                    "t_init_c, so it would only echo its initial field."
                ),
            })
    else:
        ambients = [
            _finite_float(load.get("t_amb_c", 20.0))
            for load in loads if isinstance(load, dict) and _finite_float(
                load.get("h") if load.get("h") is not None else load.get("h_w_m2k"),
                positive=True) is not None
        ]
        temperatures = set(fixed_temps + [t for t in ambients if t is not None])
        if (not fixed_temps and not ambients) or (not has_flux and len(temperatures) < 2):
            issues.append({
                "kind": "thermal_steady_bcs",
                "detail": (
                    "Steady thermal needs a fixed-temperature or convection datum and "
                    "non-zero excitation: distinct boundary temperatures or a heat flux."
                ),
            })
    return issues


def _thermal_bc_fix(kind: str) -> dict:
    """Candidate fix using the public run_fea_thermal argument names."""
    if kind == "thermal_steady_bcs":
        return {
            "action": "add_thermal_dirichlet_pair",
            "params": {
                "hint": (
                    "Pass fixed-temperature faces as {'face': <query>, 'temperature': <°C>}, "
                    "or convection h_w_m2k/t_amb_c loads to anchor the datum. "
                    "Supply distinct boundary temperatures or a non-zero flux_w_m2."
                )
            },
            "basis": "Steady conduction requires a temperature datum and thermal excitation.",
            "predicted_effect": "A finite steady temperature gradient is defined.",
            "confidence": "heuristic",
        }
    return {
        "action": "add_transient_heat_input",
        "params": {
            "hint": (
                "Add a fixed-temperature face, flux_w_m2, or h_w_m2k/t_amb_c load "
                "to the transient study."
            )
        },
        "basis": "Without thermal input, transient temperature stays at t_init_c.",
        "predicted_effect": "A driven temperature history is computed.",
        "confidence": "heuristic",
    }


def _mesh_convergence_evidence(study: dict) -> Optional[dict]:
    """Return numeric two-mesh evidence using a strict change gate capped at 10%."""
    evidence = study.get("mesh_convergence")
    if not isinstance(evidence, dict):
        return None
    requested_tolerance = _finite_float(evidence.get("tolerance_pct", 10.0), positive=True)
    if requested_tolerance is None:
        return None
    tolerance = min(requested_tolerance, 10.0)
    values = evidence.get("values")
    if isinstance(values, list) and len(values) >= 2:
        coarse = _finite_float(values[-2])
        fine = _finite_float(values[-1])
    else:
        coarse = _finite_float(evidence.get("coarse_value"))
        fine = _finite_float(evidence.get("fine_value"))
    if coarse is None or fine is None:
        return None
    denom = max(abs(coarse), abs(fine), 1e-12)
    change_pct = abs(fine - coarse) / denom * 100.0
    if not math.isfinite(change_pct):
        return None
    return {
        "coarse_value": coarse,
        "fine_value": fine,
        "relative_change_pct": change_pct,
        "tolerance_pct": tolerance,
        "stable": change_pct < tolerance,
    }


def _mesh_suggestion(study: dict, doc_geom: Optional[dict] = None) -> Optional[str]:
    """Advise convergence without pretending dimensionless fineness is a length."""
    current = _mesh_fineness_value(study.get("mesh_fineness", 0.5))
    if current is None or not 0.0 <= current <= 1.0:
        return "mesh_fineness must be 0..1 or coarse|medium|fine|finest."
    if _mesh_convergence_evidence(study) is not None:
        return None
    feature_size = None
    for key in ("min_fillet_radius_mm", "min_wall_mm"):
        if doc_geom:
            feature_size = _finite_float(doc_geom.get(key), positive=True)
        if feature_size is not None:
            break
    if feature_size is not None:
        return (
            f"Smallest reported feature is {feature_size:.3g} mm, but mesh_fineness is "
            "dimensionless and cannot be converted to a unit-correct element size. Run at "
            f"{current:.2f} and a finer preset (typically 0.75), report n_elems, and require "
            "<10% change in the governing result."
        )
    if current <= 0.25:
        return (
            f"mesh_fineness {current:.2f} is a coarse screening mesh. Repeat at a finer preset "
            "and require <10% change before claiming a converged value."
        )
    return None


def _surface_issues(study: dict) -> list[dict]:
    """Lint excitation/range fields that replace structural loads in other families."""
    kind = _study_kind(study)
    issues = []
    diagnostic = _zero_load_diagnostic(study)
    if "cfd" in kind or kind in ("flow", "run_cfd_flow", "run_cfd_accurate"):
        velocity = _finite_float(study.get("velocity", study.get("velocity_ms", 10.0)))
        if (velocity is None or velocity <= 0.0) and not diagnostic:
            issues.append({
                "kind": "invalid_excitation",
                "detail": "CFD freestream velocity must be finite and >0 m/s for a driven flow study.",
                "tool": "run_cfd_flow",
            })
        fluid = str(study.get("fluid") or "air").lower()
        if fluid not in ("air", "water"):
            issues.append({
                "kind": "invalid_fluid",
                "detail": "run_cfd_flow/run_cfd_accurate accept fluid='air' or fluid='water' only.",
                "tool": "run_cfd_flow",
            })
    if "motion" in kind:
        stop = _finite_float(study.get("stop"))
        steps = _finite_float(study.get("steps", 24), positive=True)
        if stop is None:
            issues.append({
                "kind": "invalid_motion_range",
                "detail": "simulate_motion requires stop in the driven mate's unit (degrees or mm).",
                "tool": "simulate_motion",
            })
        if steps is None or steps < 2:
            issues.append({
                "kind": "invalid_motion_range",
                "detail": "simulate_motion needs at least two samples across the sweep.",
                "tool": "simulate_motion",
            })
    if "frequency_response" in kind or "vibration" in kind:
        direction = study.get("excitation_dir", [0.0, 0.0, 1.0])
        direction_drives = (
            isinstance(direction, (list, tuple))
            and len(direction) == 3
            and any((_finite_float(value) or 0.0) != 0.0 for value in direction)
        )
        harmonic = (_finite_float(study.get("base_accel_g", 1.0)) or 0.0) != 0.0
        random_input = bool(study.get("psd_breakpoints"))
        if (not direction_drives or not (harmonic or random_input)) and not diagnostic:
            issues.append({
                "kind": "invalid_excitation",
                "detail": (
                    "Frequency response needs a non-zero excitation_dir and either non-zero "
                    "base_accel_g or psd_breakpoints in g²/Hz."
                ),
                "tool": "run_fea_frequency_response",
            })
    if kind.startswith("em_") and kind != "em_status":
        has_cross_section = bool(study.get("standard") or study.get("waveguide"))
        if kind == "em_driven":
            has_cross_section = has_cross_section or bool(study.get("geometry"))
        if not has_cross_section:
            issues.append({
                "kind": "missing_em_geometry",
                "detail": (
                    f"{kind} requires standard='WR-90', a waveguide JSON cross-section"
                    + (", or geometry for em_driven." if kind == "em_driven" else ".")
                ),
                "tool": kind,
            })
    thermal = "thermal" in kind
    if "contact" in kind and _finite_float(
        study.get("max_gap_mm", 0.5), positive=True
    ) is None:
        issues.append({
            "kind": "invalid_contact_gap",
            "detail": (
                "run_fea_contact max_gap_mm must be finite and >0 mm: it is the widest "
                "mesh gap still treated as a contact candidate. Measure the real clearance "
                "and pass just above it."
            ),
            "tool": "run_fea_contact",
        })
    transient_thermal = thermal and (
        bool(study.get("transient")) or "transient" in kind
    )
    if transient_thermal:
        transient_spec = study.get("transient")
        t_end = (
            transient_spec.get("t_end_s")
            if isinstance(transient_spec, dict)
            else study.get("t_end_s")
        )
        if _finite_float(t_end, positive=True) is None:
            issues.append({
                "kind": "invalid_thermal_time",
                "detail": "Transient run_fea_thermal requires finite transient.t_end_s > 0 seconds.",
                "tool": "run_fea_thermal",
            })
    material = study.get("material")
    if thermal and not isinstance(material, dict):
        invalid_overrides = []
        conductivity_override = study.get("conductivity_w_mk")
        if (
            conductivity_override is not None
            and _finite_float(conductivity_override, positive=True) is None
        ):
            invalid_overrides.append("conductivity_w_mk")
        specific_heat_override = study.get("specific_heat_j_kgk")
        if (
            transient_thermal
            and specific_heat_override is not None
            and _finite_float(specific_heat_override, positive=True) is None
        ):
            invalid_overrides.append("specific_heat_j_kgk")
        if invalid_overrides:
            issues.append({
                "kind": "invalid_thermal_properties",
                "detail": (
                    "Thermal property overrides must be finite and >0: "
                    + ", ".join(invalid_overrides)
                    + "."
                ),
                "tool": "run_fea_thermal",
                "blocker": True,
            })
    if isinstance(material, dict):
        # Every cad_sim_tools FEA path resolves E and nu even when the primary physics is
        # thermal. Named materials are resolved by the tool; only explicit dicts can be
        # checked here without guessing registry contents.
        fea = any(token in kind for token in (
            "static", "modal", "fatigue", "frequency_response", "vibration",
            "thermal", "buckling", "contact",
        ))
        required = ["E_mpa", "nu"] if fea else []
        if "static" in kind or "fatigue" in kind:
            required.append("yield_mpa")
        if any(token in kind for token in ("modal", "frequency_response", "vibration")):
            required.append("density_g_cm3")
        if "fatigue" in kind:
            required.extend(("ultimate_mpa", "endurance_mpa"))
        if transient_thermal:
            required.append("density_g_cm3")
        missing = []
        for key in dict.fromkeys(required):
            value = _finite_float(material.get(key))
            if value is None or (key != "nu" and value <= 0.0):
                missing.append(key)
        nu = _finite_float(material.get("nu"))
        if "nu" in required and nu is not None and not -1.0 < nu < 0.5:
            if "nu" not in missing:
                missing.append("nu")
        if thermal:
            conductivity = study.get("conductivity_w_mk")
            if conductivity is None:
                conductivity = material.get(
                    "conductivity_w_mk", material.get("k_w_mk")
                )
            if _finite_float(conductivity, positive=True) is None:
                missing.append("conductivity_w_mk")
            if transient_thermal:
                specific_heat = study.get("specific_heat_j_kgk")
                if specific_heat is None:
                    specific_heat = material.get("cp_j_kgk")
                if _finite_float(specific_heat, positive=True) is None:
                    missing.append("cp_j_kgk/specific_heat_j_kgk")
        if missing:
            issues.append({
                "kind": "missing_material_properties",
                "detail": (
                    "Explicit material dict has missing/invalid properties: "
                    + ", ".join(missing)
                    + ". Use a registry material or sourced values in the documented units."
                ),
                "tool": "research_material_lookup",
                "blocker": True,
            })
    return issues


def _surface_fix(issue: dict) -> dict:
    return {
        "action": "correct_study_arguments",
        "params": {"hint": issue["detail"]},
        "basis": "The requested tool cannot produce a meaningful driven study from this setup.",
        "predicted_effect": "The solver receives a finite excitation/range in its documented units.",
        "confidence": "heuristic",
    }


def _backend_issue(study: dict) -> Optional[dict]:
    """Return an unavailable-backend issue when a capability probe supplied evidence."""
    requested = str(study.get("solver") or study.get("backend") or "").strip().lower()
    unavailable = study.get("backend_available") is False
    capabilities = study.get("capabilities")
    if requested and isinstance(capabilities, dict):
        available = capabilities.get(requested)
        if available is None and isinstance(capabilities.get("solvers"), list):
            match = next((
                entry for entry in capabilities["solvers"]
                if isinstance(entry, dict)
                and str(entry.get("name") or "").strip().lower() == requested
            ), None)
            available = match
        if isinstance(available, dict):
            available = available.get("installed", available.get("available"))
        unavailable = unavailable or available is False
    if not unavailable:
        return None
    kind = _study_kind(study)
    probe = "em_status" if kind.startswith("em_") or "electromag" in kind else None
    return {
        "kind": "backend_unavailable",
        "detail": (
            f"Requested backend {requested or '<unspecified>'!r} is unavailable. "
            "Do not launch it or reinterpret its SKIP/error as a solved study."
        ),
        "probe": probe,
    }


def _axis_side(ref: object) -> Optional[tuple[str, int]]:
    """Map common query_geometry side vocabulary to (axis, sign)."""
    if isinstance(ref, dict):
        if "face" in ref:
            return _axis_side(ref["face"])
        ref = ref.get("side")
    if not isinstance(ref, str):
        return None
    token = ref.strip().lower().replace(" ", "")
    mapping = {
        "left": ("x", -1), "-x": ("x", -1),
        "right": ("x", 1), "+x": ("x", 1),
        "back": ("y", -1), "-y": ("y", -1),
        "front": ("y", 1), "+y": ("y", 1),
        "bottom": ("z", -1), "-z": ("z", -1),
        "top": ("z", 1), "+z": ("z", 1),
    }
    return mapping.get(token)




def _thermal_face_mapping_confident(bbox: dict, first_ref: object, second_ref: object) -> bool:
    """Require opposed named faces across the bbox thickness for an automatic 1-D anchor."""
    dims = {}
    for axis in ("x", "y", "z"):
        raw = bbox.get(f"{axis}_mm")
        if raw is None:
            raw = bbox.get(axis)
        value = _finite_float(raw, positive=True)
        if value is None:
            return False
        dims[axis] = value
    first = _axis_side(first_ref)
    second = _axis_side(second_ref)
    if not first or not second or first[0] != second[0] or first[1] == second[1]:
        return False
    return math.isclose(dims[first[0]], min(dims.values()), rel_tol=1e-12, abs_tol=0.0)


def _cantilever_mapping_confident(study: dict, bbox: dict) -> bool:
    """Require opposite end-face queries before auto-claiming a cantilever anchor."""
    fixtures = study.get("fixtures") or []
    loads = study.get("loads") or []
    if len(fixtures) != 1 or len(loads) != 1 or not isinstance(loads[0], dict):
        return False
    confident, _, long_axis = _beam_aspect_ok(bbox)
    if not confident:
        return False
    fixed_side = _axis_side(fixtures[0])
    loaded_side = _axis_side(loads[0].get("face"))
    return bool(
        fixed_side and loaded_side
        and fixed_side[0] == long_axis == loaded_side[0]
        and fixed_side[1] == -loaded_side[1]
    )


def _single_force_vector_n(loads: list) -> Optional[list[float]]:
    """Return one finite public/normalized force vector in N, else ``None``."""
    if not isinstance(loads, list) or len(loads) != 1 or not isinstance(loads[0], dict):
        return None
    load = loads[0]
    if "pressure" in load or load.get("type") == "pressure":
        return None
    vector = load.get("force")
    if vector is None:
        vector = load.get("vector")
    if isinstance(vector, (list, tuple)) and len(vector) == 3:
        values = [_finite_float(component) for component in vector]
        return None if any(value is None for value in values) else values
    magnitude = _finite_float(load.get("magnitude"), positive=True)
    direction = load.get("direction")
    if magnitude is None or not isinstance(direction, (list, tuple)) or len(direction) != 3:
        return None
    components = [_finite_float(component) for component in direction]
    if any(component is None for component in components):
        return None
    norm = math.sqrt(sum(component * component for component in components))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return [magnitude * component / norm for component in components]


def _total_force_n(loads: list) -> Optional[float]:
    """Magnitude of one force load in N; multi-load/pressure cases have no beam anchor."""
    vector = _single_force_vector_n(loads)
    if vector is None:
        return None
    magnitude = math.sqrt(sum(component * component for component in vector))
    return magnitude if math.isfinite(magnitude) else None


# ---------------------------------------------------------------------------
# Public API — precheck
# ---------------------------------------------------------------------------

#: When True, BC-lint ``blocker`` items STOP dispatch before the solve.
#: This is a deliberate deviation from the §9.3/§10 freedom rule — see module docstring.
PRECHECK_BLOCK_ENABLED: bool = (
    os.environ.get("CHUDWARE_SIM_PRECHECK_BLOCK", "1").strip().lower()
    not in ("0", "false", "off", "no")
)


def precheck(study: dict, doc_geom: Optional[dict] = None) -> list[dict]:
    """Pre-dispatch setup checks over the normalized arguments of current sim tools.

    Static/fatigue/buckling require mounted faces and non-zero force/pressure.
    Modal deliberately permits ``fixtures=[]`` for a free-free solve.
    Steady and transient thermal paths are linted separately because
    their boundary requirements differ. ``backend_available``/``capabilities`` are
    optional inputs from a real capability probe; absence is unknown, never available.
    """
    from services.agent.preferences import resolve_turn_preferences

    # Auto's setup floor comes from the same resolver as model/tool selection.
    # An explicit environment setting retains the diagnostic escape hatch.
    block_enabled = (
        PRECHECK_BLOCK_ENABLED if "CHUDWARE_SIM_PRECHECK_BLOCK" in os.environ
        else resolve_turn_preferences("", task_kind="analysis").sim_precheck_block
    )
    items: list[dict] = []
    kind = _study_kind(study)
    thermal = _is_thermal(study)

    # --- BC lint ---
    if thermal:
        for issue in _thermal_bc_issues(study):
            issue_kind = issue["kind"]
            is_blocker = block_enabled
            items.append({
                "schema": "aicad.advisory/1",
                "check": "sim_bc",
                "severity": "blocker" if is_blocker else "action",
                "finding": issue["detail"],
                "refs": {},
                "numbers": {},
                "candidate_fixes": [_thermal_bc_fix(issue_kind)],
                "helper_tools": ["run_fea_thermal", "query_geometry"],
                "source": {"kind": "heuristic", "detail": "sim_advisor.precheck (thermal)"},
            })
    else:
        for issue in _bc_issues(study):
            issue_kind = issue["kind"]
            diagnostic = issue_kind == "zero_load_diagnostic"
            is_blocker = (
                issue_kind in ("no_fixture", "no_load", "dangling_ref")
                and block_enabled
            )
            items.append({
                "schema": "aicad.advisory/1",
                "check": "sim_bc",
                "severity": "info" if diagnostic else ("blocker" if is_blocker else "action"),
                "finding": issue["detail"],
                "refs": {},
                "numbers": {},
                "candidate_fixes": [] if diagnostic else [_bc_fix(issue_kind)],
                "helper_tools": ["query_geometry"],
                "source": {"kind": "heuristic", "detail": "sim_advisor.precheck"},
            })

    for issue in _surface_issues(study):
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_bc",
            "severity": (
                "blocker" if issue.get("blocker", True) and block_enabled else "action"
            ),
            "finding": issue["detail"],
            "refs": {},
            "numbers": {},
            "candidate_fixes": [_surface_fix(issue)],
            "helper_tools": [issue["tool"]],
            "source": {"kind": "schema", "detail": "current public tool arguments"},
        })

    backend_issue = _backend_issue(study)
    if backend_issue:
        probe = backend_issue.get("probe")
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_bc",
            "severity": "blocker" if block_enabled else "action",
            "finding": backend_issue["detail"],
            "refs": {},
            "numbers": {},
            "candidate_fixes": [{
                "action": "select_available_backend",
                "params": {"hint": "Run the capability/status probe, then select an installed backend."},
                "basis": "The requested solver was reported unavailable.",
                "predicted_effect": "The next run uses a backend that can execute.",
                "confidence": "measured",
            }],
            "helper_tools": [probe] if probe else [],
            "source": {"kind": "runtime", "detail": "caller-supplied capability result"},
        })

    # --- Mesh/convergence suggestion (only tools whose public path uses a real FEA mesh) ---
    mesh_path = (
        any(token in kind for token in ("static", "modal", "fatigue", "frequency_response"))
        or ("thermal" in kind and (bool(study.get("transient")) or "transient" in kind))
    )
    mesh_hint = _mesh_suggestion(study, doc_geom) if mesh_path else None
    if mesh_hint:
        if "modal" in kind:
            mesh_tool = "run_fea_modal"
        elif "fatigue" in kind:
            mesh_tool = "run_fea_fatigue"
        elif "frequency_response" in kind or "vibration" in kind:
            mesh_tool = "run_fea_frequency_response"
        elif "thermal" in kind:
            mesh_tool = "run_fea_thermal"
        else:
            mesh_tool = "run_fea_static"
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_mesh",
            "severity": "info",
            "finding": mesh_hint,
            "refs": {},
            "numbers": {"mesh_fineness": study.get("mesh_fineness")},
            "candidate_fixes": [],
            "helper_tools": [mesh_tool],
            "source": {"kind": "heuristic", "detail": "sim_advisor.precheck"},
        })

    # --- Hand-calc anchor (order-of-magnitude cross-check) ---
    material_value = study.get("material")
    mat = material_value if isinstance(material_value, dict) else {}
    bbox = study.get("bbox")
    loads = study.get("loads") or []

    if thermal:
        conductivity = (
            mat.get("k_w_mk") or mat.get("conductivity_w_mk")
            or study.get("k_w_mk") or study.get("conductivity_w_mk")
        )
        fixtures = list(study.get("fixtures") or [])
        fixtures += list(study.get("hot_faces") or [])
        fixtures += list(study.get("cold_faces") or [])
        temperature_bcs = []
        for fixture in fixtures:
            if not isinstance(fixture, dict):
                continue
            raw = fixture.get("temp_c")
            if raw is None:
                raw = fixture.get("temperature")
            value = _finite_float(raw)
            if value is not None:
                temperature_bcs.append((value, fixture))
        convection = next((
            load for load in loads
            if isinstance(load, dict)
            and (load.get("type") == "convection" or load.get("h_w_m2k") is not None)
        ), None)
        anchor = None
        if conductivity is not None and temperature_bcs:
            hot, hot_bc = max(temperature_bcs, key=lambda entry: entry[0])
            cold, cold_bc = min(temperature_bcs, key=lambda entry: entry[0])
            if hot != cold and _thermal_face_mapping_confident(
                bbox or {}, hot_bc, cold_bc
            ):
                anchor = thermal_anchor(bbox or {}, conductivity, hot, t_cold_c=cold)
            elif convection is not None and _thermal_face_mapping_confident(
                bbox or {}, hot_bc, convection
            ):
                h = convection.get("h")
                if h is None:
                    h = convection.get("h_w_m2k")
                anchor = thermal_anchor(
                    bbox or {}, conductivity, hot, h_w_m2k=h,
                    t_amb_c=convection.get("t_amb_c", 20.0),
                )
        detail = (
            f"Thermal anchor ({anchor['mode']}): wall flux {anchor['q_w_m2']:.1f} W/m² "
            f"(L={anchor['L_mm']:.1f} mm, k={anchor['k_w_mk']:.1f} W/m·K, "
            f"cold face ≈ {anchor['t_cold_c']:.1f} °C); source: {anchor['source']}."
            if anchor else
            "No 1-D thermal anchor: need bbox/conductivity and opposed named faces across "
            "the shortest bbox dimension, with two fixed temperatures or fixed-plus-convection."
        )
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_anchor",
            "severity": "info",
            "finding": detail,
            "refs": {},
            "numbers": {
                "anchor_flux_w_m2": anchor["q_w_m2"] if anchor else None,
                "anchor_cold_face_c": anchor["t_cold_c"] if anchor else None,
            },
            "candidate_fixes": [],
            "helper_tools": [],
            "source": {"kind": "calc", "detail": "thermal_anchor (1D plane wall)"},
            "_anchor": anchor,
        })
        return items

    # Beam stress is not an anchor for modal, CFD, motion, contact, buckling, or EM.
    if not ("static" in kind or "fatigue" in kind):
        return items

    E_mpa = mat.get("E_mpa")
    load_vector_n = _single_force_vector_n(loads)
    total_load_n = _total_force_n(loads)
    anchor = None
    if (
        E_mpa is not None and bbox and total_load_n is not None and total_load_n > 0.0
        and _cantilever_mapping_confident(study, bbox)
    ):
        anchor = compute_beam_anchor(
            bbox, E_mpa, total_load_n, load_vector_n=load_vector_n
        )

    anchor_detail = (
        f"Beam anchor: {anchor['max_stress_mpa']:.2f} MPa (cantilever, rect section, "
        f"L={anchor.get('L_mm', '?'):.1f} mm, aspect={anchor.get('aspect_ratio', '?')}, "
        f"load axis={anchor.get('load_axis')}); source: {anchor.get('source', 'beam_calc_raw')}."
        if anchor else
        "No beam anchor — need a slender bbox, one transverse force, and opposite named "
        "end-face fixture/load queries to justify a cantilever mapping."
    )
    items.append({
        "schema": "aicad.advisory/1",
        "check": "sim_anchor",
        "severity": "info",
        "finding": anchor_detail,
        "refs": {},
        "numbers": {"anchor_stress_mpa": anchor["max_stress_mpa"] if anchor else None},
        "candidate_fixes": [],
        "helper_tools": [],
        "source": {"kind": "calc", "detail": "beam_calc_raw"},
        "_anchor": anchor,   # passed through for postcheck; stripped before injection
    })

    return items


def _bc_fix(kind: str) -> dict:
    """Return a candidate fix dict for a BC-lint issue kind."""
    if kind == "no_fixture":
        return {
            "action": "add_fixture",
            "params": {"hint": "Fix at least one face (e.g. the mounting face) before solving."},
            "basis": "FEA requires at least one fixed boundary condition.",
            "predicted_effect": "Solver can proceed; rigid-body motion is constrained.",
            "confidence": "heuristic",
        }
    if kind == "no_load":
        return {
            "action": "add_load",
            "params": {"hint": "Add a supported non-zero load on the loaded face."},
            "basis": "The structural tools refuse a zero-load study; zero stress is not capacity.",
            "predicted_effect": "A driven, physically interpretable stress field can be computed.",
            "confidence": "heuristic",
        }
    if kind == "all_fixed":
        return {
            "action": "relax_fixtures",
            "params": {"hint": "Remove 'all_faces' fixture; fix only the mounting surfaces."},
            "basis": "Over-constrained body suppresses displacement and may misrepresent stress.",
            "predicted_effect": "Realistic displacement field; stress concentrated at load sites.",
            "confidence": "heuristic",
        }
    # dangling_ref
    return {
        "action": "resolve_face_ref",
        "params": {"hint": "Verify the fixture/load face id against the current doc geometry."},
        "basis": "Current FEA tools reject face queries that resolve to no surface points.",
        "predicted_effect": "The requested boundary condition resolves and is applied.",
        "confidence": "heuristic",
    }


# ---------------------------------------------------------------------------
# Public API — postcheck
# ---------------------------------------------------------------------------

#: Maximum ratio of FEA stress to beam-anchor stress before emitting a divergence WARN.
_DIVERGENCE_CONFIG = _finite_float(
    os.environ.get(
        "CHUDWARE_SIM_DIVERGENCE_RATIO",
        str(_load_policy().get("_doc", {}).get("divergence_warn_ratio", 5.0)),
    ),
    positive=True,
)
DIVERGENCE_WARN_RATIO: float = (
    _DIVERGENCE_CONFIG if _DIVERGENCE_CONFIG is not None and _DIVERGENCE_CONFIG > 1.0 else 5.0
)


def postcheck(
    study: dict,
    precheck_items: Optional[list[dict]] = None,
    doc_geom: Optional[dict] = None,
) -> dict:
    """Post-solve static margin and confidence analysis.

    ``study.results.max_von_mises_mpa`` and ``study.material.yield_mpa`` are MPa.
    ``margin_mpa`` is measured against ``yield/SF``, not raw yield. A favorable margin
    becomes overall PASS only when the solver completed, the reacted setup and explicit
    material properties are valid, a mesh is reported, and either ``mesh_convergence``
    contains two finite governing values within tolerance or the independent beam anchor
    agrees within ``DIVERGENCE_WARN_RATIO``.

    ``precheck_items`` may carry the exact pre-solve ``_anchor``. Without it, a beam
    anchor is recomputed only for finite force loads and a justified slender bbox.
    ``doc_geom`` remains accepted for caller compatibility; postcheck does not infer
    convergence from geometry hints.
    """
    results = study.get("results") or {}
    kind = _study_kind(study)
    material_value = study.get("material")
    mat = material_value if isinstance(material_value, dict) else {}
    vm = _finite_float(results.get("max_von_mises_mpa"), nonnegative=True)
    yield_mpa = _finite_float(mat.get("yield_mpa"), positive=True)
    ultimate_mpa = _finite_float(mat.get("ultimate_mpa"), positive=True)
    mat_name = mat.get("name") or "unknown"

    policy_rec = resolve_material_class(mat_name)
    sf = _finite_float(policy_rec.get("sf_yield"), positive=True) or 2.0
    mat_class = policy_rec["class"]

    items: list[dict] = []
    verdict = "WARN"
    margin_mpa = None
    divergence_ratio = None

    # --- Independent analytic anchor ---
    anchor = _extract_anchor(precheck_items)
    if anchor is None:
        E_mpa = mat.get("E_mpa")
        bbox = study.get("bbox")
        loads = study.get("loads") or []
        load_vector_n = _single_force_vector_n(loads)
        total_load_n = _total_force_n(loads)
        if (
            E_mpa is not None and bbox and total_load_n is not None and total_load_n > 0.0
            and _cantilever_mapping_confident(study, bbox)
        ):
            anchor = compute_beam_anchor(
                bbox, E_mpa, total_load_n, load_vector_n=load_vector_n
            )
    anchor_stress = (
        _finite_float(anchor.get("max_stress_mpa"), positive=True)
        if isinstance(anchor, dict) else None
    )
    if anchor_stress is not None and vm is not None:
        divergence_ratio = vm / anchor_stress
        if not math.isfinite(divergence_ratio):
            divergence_ratio = None

    mesh_fineness = _mesh_fineness_value(study.get("mesh_fineness"))
    n_elems = _finite_float(
        study.get("n_elems", results.get("n_elems")), positive=True
    )
    mesh_reported = (
        (mesh_fineness is not None and 0.0 <= mesh_fineness <= 1.0)
        or n_elems is not None
    )
    mesh_convergence = _mesh_convergence_evidence(study)
    mesh_stable = bool(mesh_convergence and mesh_convergence["stable"])
    anchor_consistent = bool(
        divergence_ratio is not None
        and 1.0 / DIVERGENCE_WARN_RATIO <= divergence_ratio <= DIVERGENCE_WARN_RATIO
    )
    state = str(study.get("state") or results.get("state") or "").lower()
    solver_completed = (
        study.get("ok") is not False
        and results.get("ok") is not False
        and study.get("backend_available") is not False
        and not study.get("skipped")
        and not results.get("skipped")
        and not study.get("partial")
        and not results.get("partial")
        and state not in ("queued", "running", "failed", "cancelled")
        and results.get("converged") is not False
    )
    setup_issues = _bc_issues(study) if (
        "static" in kind or "fatigue" in kind
    ) else []
    surface_blockers = [
        issue for issue in _surface_issues(study) if issue.get("blocker", True)
    ]
    backend_valid = _backend_issue(study) is None
    setup_valid = not setup_issues and not surface_blockers
    confidence_supported = bool(
        solver_completed and setup_valid and backend_valid and mesh_reported
        and (mesh_stable or anchor_consistent)
    )


    # --- Unit-correct static margin verdict ---
    if vm is not None and yield_mpa is not None:
        allowable_mpa = yield_mpa / sf
        margin_mpa = allowable_mpa - vm
        design_stress = vm * sf
        finite_design_stress = design_stress if math.isfinite(design_stress) else None
        raw_utilization = (
            finite_design_stress / yield_mpa if finite_design_stress is not None else None
        )
        utilization = (
            raw_utilization
            if raw_utilization is not None and math.isfinite(raw_utilization)
            else None
        )
        if vm >= yield_mpa:
            verdict = "FAIL"
            failure = (
                f" and exceeds ultimate {ultimate_mpa:.1f} MPa (fracture risk)"
                if ultimate_mpa is not None and vm >= ultimate_mpa else
                " (nominal yielding predicted)"
            )
            verdict_detail = (
                f"Max stress {vm:.2f} MPa exceeds yield {yield_mpa:.2f} MPa{failure} — FAIL."
            )
            sev = "blocker"
            fix = _margin_fix(verdict, mat_class, sf)
        elif design_stress > yield_mpa:
            verdict = "WARN"
            verdict_detail = (
                f"Max stress {vm:.2f} MPa is below yield, but exceeds the policy allowable "
                f"{allowable_mpa:.2f} MPa (= yield {yield_mpa:.2f}/SF {sf:.2f}) by "
                f"{-margin_mpa:.2f} MPa — required design margin is not met."
            )
            sev = "action"
            fix = _margin_fix(verdict, mat_class, sf)
        else:
            verdict = "PASS" if confidence_supported else "WARN"
            verdict_detail = (
                f"Max stress {vm:.2f} MPa is {margin_mpa:.2f} MPa below the policy allowable "
                f"{allowable_mpa:.2f} MPa (= yield {yield_mpa:.2f}/SF {sf:.2f})."
            )
            sev = "info"
            fix = None

        margin_item: dict = {
            "schema": "aicad.advisory/1",
            "check": "sim_margin",
            "severity": sev,
            "finding": verdict_detail,
            "refs": {},
            "numbers": {
                "max_stress_mpa": vm,
                "yield_mpa": yield_mpa,
                "allowable_mpa": round(allowable_mpa, 3),
                "sf_yield": sf,
                "design_stress_mpa": (
                    round(finite_design_stress, 3) if finite_design_stress is not None else None
                ),
                "utilization": round(utilization, 6) if utilization is not None else None,
                "margin_mpa": round(margin_mpa, 3),
                "mat_class": mat_class,
            },
            "candidate_fixes": [fix] if fix else [],
            "helper_tools": ["describe_results", "run_fea_static"],
            "source": {"kind": "table", "detail": f"margin_policy.json [{mat_class}]"},
        }
        items.append(margin_item)
    else:
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_margin",
            "severity": "action",
            "finding": (
                "No finite static margin can be computed: max_von_mises_mpa must be finite "
                "and non-negative, and yield_mpa must be finite and >0."
            ),
            "refs": {},
            "numbers": {},
            "candidate_fixes": [{
                "action": "supply_finite_strength_results",
                "params": {"hint": "Use the solver summary MPa values and a material with yield_mpa."},
                "basis": "A missing/NaN/negative stress or strength cannot define a safety margin.",
                "predicted_effect": "The advisor can compute a finite allowable and utilization.",
                "confidence": "measured",
            }],
            "helper_tools": ["run_fea_static", "describe_results"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.postcheck"},
        })

    # --- Beam-anchor divergence check ---
    if divergence_ratio is not None and (
        divergence_ratio > DIVERGENCE_WARN_RATIO
        or divergence_ratio < 1.0 / DIVERGENCE_WARN_RATIO
    ):
        if divergence_ratio == 0.0:
            ratio_str = "zero while"
        elif divergence_ratio > 1.0:
            ratio_str = f"{divergence_ratio:.1f}×"
        else:
            ratio_str = f"{1.0 / divergence_ratio:.1f}× lower than"
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_divergence",
            "severity": "action",
            "finding": (
                f"FEA result ({vm:.2f} MPa) is {ratio_str} the beam anchor "
                f"({anchor_stress:.2f} MPa). Check units, BCs, load path, and mesh."
            ),
            "refs": {},
            "numbers": {
                "fea_stress_mpa": vm,
                "anchor_stress_mpa": anchor_stress,
                "divergence_ratio": round(divergence_ratio, 6),
                "warn_threshold": DIVERGENCE_WARN_RATIO,
            },
            "candidate_fixes": [{
                "action": "review_bc_load_units_and_mesh",
                "params": {"hint": "Confirm N/MPa/mm inputs, face queries, and repeat on a finer mesh."},
                "basis": "FEA and the independent beam anchor disagree by more than the threshold.",
                "predicted_effect": "The discrepancy is explained or reduced with corrected setup.",
                "confidence": "heuristic",
            }],
            "helper_tools": ["run_fea_static", "describe_results"],
            "source": {"kind": "calc", "detail": "beam_calc_raw cross-check"},
        })
        if verdict == "PASS":
            verdict = "WARN"

    if not confidence_supported:
        missing = []
        if not solver_completed:
            missing.append("completed/converged solver result")
        if not setup_valid:
            missing.append("reacted non-zero structural setup")
        if not backend_valid:
            missing.append("available requested backend")
        if not mesh_reported:
            missing.append("reported mesh_fineness or n_elems")
        if not mesh_stable and not anchor_consistent:
            missing.append("stable two-mesh comparison or consistent analytic anchor")
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_mesh",
            "severity": "action",
            "finding": (
                "Overall PASS withheld: missing " + ", ".join(missing)
                + ". A favorable one-mesh margin is not convergence evidence."
            ),
            "refs": {},
            "numbers": {
                "mesh_reported": mesh_reported,
                "mesh_relative_change_pct": (
                    round(mesh_convergence["relative_change_pct"], 6)
                    if mesh_convergence else None
                ),
                "anchor_consistent": anchor_consistent,
            },
            "candidate_fixes": [{
                "action": "run_mesh_convergence_or_anchor",
                "params": {
                    "hint": (
                        "Repeat with a finer mesh and pass mesh_convergence values; require "
                        "<10% change, or supply an independent analytic anchor."
                    )
                },
                "basis": "A single discretization can look safe while its peak stress is unresolved.",
                "predicted_effect": "A PASS, if retained, has numeric verification evidence.",
                "confidence": "heuristic",
            }],
            "helper_tools": ["run_fea_static", "describe_results"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.postcheck evidence gate"},
        })
        if verdict == "PASS":
            verdict = "WARN"

    # --- Geometry-linked suggestion (fillet/sharp-edge Kt pattern) ---
    max_stress_face = (study.get("results") or {}).get("max_stress_face") or {}
    face_type = max_stress_face.get("type") or ""
    feature_name = max_stress_face.get("feature") or ""
    if face_type in ("fillet", "sharp_edge") and feature_name:
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_margin",
            "severity": "hint",
            "finding": (
                f"Max stress is at a {face_type} on feature '{feature_name}'. This may be a "
                "real stress concentration or a singular idealization; refine locally and "
                "check whether the peak converges before changing geometry."
            ),
            "refs": {"feature": feature_name, "face_type": face_type},
            "numbers": {},
            "candidate_fixes": [{
                "action": "edit_feature_param",
                "params": {"feature": feature_name, "param": "fillet_radius",
                           "hint": "Only increase the radius after a local mesh-convergence check."},
                "basis": "A larger radius often reduces a geometric concentration, but no Kt is inferred here.",
                "predicted_effect": "Potentially lower converged peak stress; re-run to measure it.",
                "confidence": "heuristic",
            }],
            "helper_tools": ["run_fea_static", "describe_results"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.postcheck (Kt pattern)"},
        })

    return {
        "verdict": verdict,
        "confidence": "supported" if confidence_supported else "insufficient",
        "sf_used": sf,
        "mat_class": mat_class,
        "margin_mpa": margin_mpa,
        "anchor_stress_mpa": anchor_stress,
        "divergence_ratio": divergence_ratio,
        "mesh_convergence": mesh_convergence,
        "items": items,
    }


def _extract_anchor(precheck_items: Optional[list[dict]]) -> Optional[dict]:
    """Pull the beam anchor dict out of a precheck item list (via the ``_anchor`` key)."""
    if not precheck_items:
        return None
    for it in precheck_items:
        if it.get("check") == "sim_anchor" and it.get("_anchor"):
            return it["_anchor"]
    return None


def _margin_fix(verdict: str, mat_class: str, sf: float) -> Optional[dict]:
    """Return a candidate fix for a margin verdict, or None for PASS."""
    if verdict == "PASS":
        return None
    if verdict == "WARN":
        return {
            "action": "redesign_cross_section",
            "params": {"hint": f"Increase section modulus to meet "
                                f"max_stress × {sf:.1f} ≤ yield."},
            "basis": f"margin_policy [{mat_class}] SF={sf:.1f}; the required allowable is exceeded.",
            "predicted_effect": "The design meets the required nominal yield margin.",
            "confidence": "heuristic",
        }
    # FAIL
    return {
        "action": "redesign_or_change_material",
        "params": {"hint": f"Reduce nominal stress below yield and then meet "
                           f"max_stress × {sf:.1f} ≤ yield."},
        "basis": f"Nominal stress reached yield for margin_policy [{mat_class}].",
        "predicted_effect": "Nominal loading returns to the elastic range with design margin.",
        "confidence": "heuristic",
    }


# ---------------------------------------------------------------------------
# describe_results extension helper
# ---------------------------------------------------------------------------

def narrate_postcheck(postcheck_result: dict) -> str:
    """Return multi-line text to append to describe_results output.

    The text is *additive* — the existing margin line from gsl_tools.py is not
    replaced; this adds the policy-based verdict and suggestions below it.

    Caller (gsl_tools._describe_results_text) appends this before the source line.
    """
    lines = []
    verdict = postcheck_result.get("verdict", "WARN")
    sf = postcheck_result.get("sf_used")
    mat_class = postcheck_result.get("mat_class", "general")
    confidence = postcheck_result.get("confidence", "insufficient")
    anchor = postcheck_result.get("anchor_stress_mpa")
    div_ratio = postcheck_result.get("divergence_ratio")

    sf_text = f"{sf:.1f}" if isinstance(sf, (int, float)) and math.isfinite(sf) else "unknown"
    lines.append(
        f"\nAdvisor verdict ({mat_class}, SF={sf_text}, confidence={confidence}): {verdict}"
    )

    if anchor is not None:
        if div_ratio is not None and (
            div_ratio > DIVERGENCE_WARN_RATIO or div_ratio < 1.0 / DIVERGENCE_WARN_RATIO
        ):
            lines.append(
                f"  Beam anchor: {anchor:.2f} MPa — ratio {div_ratio:.3g}; verify BCs/units/mesh."
            )
        else:
            lines.append(f"  Beam anchor: {anchor:.2f} MPa — same order of magnitude as FEA.")
    else:
        lines.append("  Beam anchor: not computed (geometry/load mapping not justified).")

    for it in postcheck_result.get("items", []):
        if it.get("check") in ("sim_margin", "sim_divergence", "sim_mesh") \
                and it.get("severity") != "info":
            lines.append(f"  [{it['severity'].upper()}] {it['finding']}")

    return "\n".join(lines)

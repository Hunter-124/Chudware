"""Simulation verification advisor — precheck (BC lint + mesh hint + hand-calc anchor)
and postcheck (margin-policy verdict + implausibility WARN + geometry suggestion) (T-W9).

Design (per advisor review 2026-07-04):

  * **Pure functions over a normalized input** — mirrors engine.py's pattern.
    No I/O in the hot path; the caller supplies a ``StudySpec`` (pre-dispatch) or a
    ``StudyResult`` (post-dispatch) dict, plus an optional ``DocGeom`` hint.
  * **Wired through T-W5** (stages.py + engine.py): findings become ``engine.Finding``
    objects with kind ``"sim_bc"`` / ``"sim_margin"`` / ``"sim_divergence"`` /
    ``"sim_mesh"``; fix-providers registered in ``engine._FIX_PROVIDERS``; check-ids
    added to the ``sim`` battery in ``stages.py``.
  * **Beam anchor**: ``services.research.calcs.beam.beam_calc_raw`` is the
    hand-calc primitive.  The doc→beam mapping is *confident* only when:
      - aspect ratio L/√(A) > 3 (long-axis dominant geometry)
      - load pattern is a plausible cantilever or simply-supported point/UDL
    When the mapping is ambiguous the anchor is ``None`` — "no anchor" is the common
    path; the postcheck never skips the margin verdict just because anchor is absent.
  * **Precheck-blocks-dispatch** is a deliberate deviation from the freedom rule
    (§9.3/§10): BC-lint blockers stop dispatch when the guard is config-enabled.
    This is documented here rather than in the contract so the contract stays frozen.
    Gate: ``CHUDWARE_SIM_PRECHECK_BLOCK`` env var, default ``"1"`` (enabled).

Anchor mock-prove note (citation honesty):
    The FEA solver does not run headlessly in CI.  The FEA-vs-beam-anchor divergence
    test seeds a cantilever study with a known ``max_von_mises_mpa`` via
    ``sim_study_add`` (same path as a real solve) and calls postcheck against it.
    The anchor is computed by beam_calc_raw with the same geometry inputs.
    This proves the comparison logic; it does NOT verify the FEA solver itself.

Contract reference: contracts/advisor.md §3 (advisory item shape), §5 (failure card),
§9 (injection rules).  BC-blocker deviation: this file, above.
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


# ---------------------------------------------------------------------------
# Beam anchor helper
# ---------------------------------------------------------------------------

def _beam_aspect_ok(bbox: dict) -> tuple[bool, float, str]:
    """Return (confident, aspect_ratio, axis) for a bounding-box dict.

    Confident when the longest axis is ≥ 3× the cross-section size.
    The bbox dict has keys like ``x_mm``, ``y_mm``, ``z_mm`` (lengths, not coords).
    Returns (False, 0.0, "") when the bbox is missing or degenerate.
    """
    dims = {}
    for ax in ("x", "y", "z"):
        v = bbox.get(f"{ax}_mm") or bbox.get(ax)
        if v and float(v) > 0:
            dims[ax] = float(v)
    if len(dims) < 2:
        return False, 0.0, ""
    sorted_axes = sorted(dims, key=lambda k: -dims[k])
    longest = dims[sorted_axes[0]]
    second = dims[sorted_axes[1]]
    if second <= 0:
        return False, 0.0, ""
    cross_size = math.sqrt(second * dims.get(sorted_axes[2], second))
    aspect = longest / cross_size if cross_size > 0 else 0.0
    return aspect >= 3.0, aspect, sorted_axes[0]


def compute_beam_anchor(
    bbox: dict,
    E_mpa: float,
    load_n: float,
    support: str = "cantilever",
    load: str = "point",
    section: str = "rect",
) -> Optional[dict]:
    """Compute a beam anchor stress using beam_calc_raw.

    Returns a dict with ``max_stress_mpa`` and provenance fields, or ``None`` when the
    geometry mapping is not confident (aspect ratio too low, missing bbox, bad inputs).

    Parameters
    ----------
    bbox : dict
        Keys ``x_mm``, ``y_mm``, ``z_mm`` (bounding box side lengths in mm).
    E_mpa : float
        Young's modulus from the material record.
    load_n : float
        Total applied load in N (sum of all load magnitudes).
    support, load, section : str
        Beam model assumptions.  Caller chooses based on load pattern.
    """
    confident, aspect, long_ax = _beam_aspect_ok(bbox)
    if not confident:
        return None

    try:
        from services.research.calcs.beam import beam_calc_raw  # noqa: PLC0415
    except ImportError:
        return None

    dims = {ax: float(bbox.get(f"{ax}_mm") or bbox.get(ax, 0)) for ax in ("x", "y", "z")}
    sorted_axes = sorted(dims, key=lambda k: -dims[k])
    L = dims[sorted_axes[0]]
    b = dims.get(sorted_axes[1], L / 4)
    h = dims.get(sorted_axes[2], b)

    try:
        raw = beam_calc_raw(
            support=support,
            load=load,
            section=section,
            L_mm=L,
            E_mpa=E_mpa,
            F_n=load_n,
            b_mm=b,
            h_mm=h,
        )
    except (ValueError, ZeroDivisionError):
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
        v = bbox.get(f"{ax}_mm") or bbox.get(ax)
        if v and float(v) > 0:
            dims[ax] = float(v)
    if len(dims) < 3 or not (k_w_mk and float(k_w_mk) > 0):
        return None
    if not math.isfinite(float(t_hot_c)):
        return None

    sorted_axes = sorted(dims, key=lambda a: dims[a])  # ascending
    L_mm = dims[sorted_axes[0]]                          # thinnest side = through-thickness
    L_m = L_mm / 1000.0
    if L_m <= 0:
        return None
    k = float(k_w_mk)

    if t_cold_c is not None and math.isfinite(float(t_cold_c)):
        # Dirichlet–Dirichlet plane wall.
        q = k * (float(t_hot_c) - float(t_cold_c)) / L_m
        return {
            "mode": "conduction",
            "q_w_m2": q,
            "t_hot_c": float(t_hot_c),
            "t_cold_c": float(t_cold_c),
            "L_mm": L_mm,
            "k_w_mk": k,
            "source": "1D plane wall q=kΔT/L (Incropera, Fundamentals of Heat & Mass Transfer §3.1)",
        }
    if (h_w_m2k is not None and float(h_w_m2k) > 0
            and t_amb_c is not None and math.isfinite(float(t_amb_c))):
        # Dirichlet–Robin (series resistance) plane wall.
        h = float(h_w_m2k)
        q = (float(t_hot_c) - float(t_amb_c)) / (L_m / k + 1.0 / h)
        t_cold = float(t_amb_c) + q / h
        return {
            "mode": "convection",
            "q_w_m2": q,
            "t_hot_c": float(t_hot_c),
            "t_cold_c": t_cold,
            "t_amb_c": float(t_amb_c),
            "h_w_m2k": h,
            "L_mm": L_mm,
            "k_w_mk": k,
            "source": "1D wall series resistance q=(T_hot-T_amb)/(L/k+1/h) "
                      "(Incropera §3.1 thermal circuits)",
        }
    # No cold-face datum (neither fixed cold temp nor convection) -> not confident.
    return None


# ---------------------------------------------------------------------------
# Fatigue anchor helper (Goodman / Soderberg S-N hand-calc reference) — T-SIM4
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
    try:
        smax = float(sigma_max_mpa)
        smin = float(sigma_min_mpa)
        sut = float(ultimate_mpa)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(smax) and math.isfinite(smin) and math.isfinite(sut)):
        return None
    if sut <= 0 or smax < smin:
        return None
    method = (method or "").lower()
    if method not in ("goodman", "soderberg"):
        return None
    sy = None
    if yield_mpa is not None:
        sy = float(yield_mpa)
        if not (math.isfinite(sy) and sy > 0):
            sy = None
    if method == "soderberg" and sy is None:
        return None

    # Endurance limit: given (already Marin-corrected by the caller) or the
    # unmodified Shigley Eq. 6-8 estimate.
    if endurance_mpa is not None and float(endurance_mpa) > 0:
        se = float(endurance_mpa)
        se_source = "given (caller-corrected)"
    else:
        se = 0.5 * sut if sut <= 1400.0 else 700.0
        se_source = "estimated Se'=0.5*Sut capped 700 MPa (Shigley Eq. 6-8, unmodified)"

    sa = (smax - smin) / 2.0
    sm = (smax + smin) / 2.0
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
    out["sa_eq_mpa"] = sa_eq
    out["fos_endurance"] = se / sa_eq

    # S-N life (Shigley Eq. 6-14..6-16, f=0.9). Same clamps as the host mirror
    # (services/mcp/fea_advanced.py sn_life) so cross-checks agree bit-for-bit.
    if sa_eq <= se:
        out.update({"regime": "infinite", "cycles": None})
        return out
    f = 0.9
    a = (f * sut) ** 2 / se
    b = -(1.0 / 3.0) * math.log10((f * sut) / se)
    n = (sa_eq / a) ** (1.0 / b)
    if not math.isfinite(n) or n < 1.0:
        n = 1.0
    if n > 1e6:
        n = 1e6
    out["cycles"] = n
    out["regime"] = "static_overload" if sa_eq >= sut else "finite"
    return out


# ---------------------------------------------------------------------------
# BC lint helpers
# ---------------------------------------------------------------------------

def _bc_issues(study: dict) -> list[dict]:
    """Deterministic BC checks on a study spec dict.

    Returns a list of issue dicts ``{kind, detail}``.  Empty = clean.

    Rules
    -----
    - At least one fixture required.
    - At least one load required.
    - No fixture may reference a face that appears in the ``dangling_refs`` list
      (caller populates from a doc-query; pass ``[]`` when unavailable).
    - Body must not be fully constrained on all faces (all-fixed check: when the
      fixture list contains only the string ``"all"`` or a single entry equal to
      ``"all_faces"``).
    """
    issues = []
    fixtures = study.get("fixtures") or []
    loads = study.get("loads") or []
    dangling = study.get("dangling_refs") or []

    if not fixtures:
        issues.append({"kind": "no_fixture",
                       "detail": "Study has no fixtures — the solver will treat the body as free-floating."})
    if not loads:
        issues.append({"kind": "no_load",
                       "detail": "Study has no loads — solve would produce zero stress everywhere."})

    # All-fixed check: body fully rigid-constrained
    if fixtures and all(f in ("all", "all_faces") for f in fixtures):
        issues.append({"kind": "all_fixed",
                       "detail": "All faces are fixed — the body is over-constrained; FEA will return "
                                 "trivially zero displacement but stress may be unreliable."})

    # Dangling reference check
    for ref in dangling:
        issues.append({"kind": "dangling_ref",
                       "detail": f"Fixture/load reference {ref!r} cannot be resolved to a face id in "
                                 "the current doc — the study may silently ignore this constraint."})

    return issues


def _is_thermal(study: dict) -> bool:
    """True when the study is a steady-state thermal (conduction) study."""
    t = (study.get("type") or "").lower()
    return "thermal" in t or study.get("study_kind") == "thermal"


def _thermal_bc_issues(study: dict) -> list[dict]:
    """Deterministic BC checks for a thermal (conduction) study.

    Returns a list of ``{kind, detail}`` issue dicts. Empty = clean.

    The load-bearing lint is the SINGULARITY check: steady conduction has a floating
    temperature datum unless SOMETHING anchors it. That anchor is a Dirichlet fixed-temperature
    BC OR a convection (Robin) film — NOT a heat flux. A pure-Neumann problem (only fluxes, or
    nothing) is singular and the solver rejects it, so we flag it before dispatch.

    (Task DoD phrased this as "no fixed-T and no flux = singular"; the physically correct
    condition is "no fixed-T and no convection" — a flux does not fix the datum, and convection
    does. The lint matches what actually makes the conductivity matrix singular. Deviation noted
    in the T-SIM1 work note.)
    """
    issues = []
    fixtures = study.get("fixtures") or []
    loads = study.get("loads") or []

    # A thermal fixture is Dirichlet only when it carries a temperature.
    has_fixed_temp = any(
        isinstance(fx, dict) and fx.get("temp_c") is not None for fx in fixtures
    )
    has_convection = any(
        isinstance(ld, dict) and (ld.get("type") == "convection")
        and (ld.get("h") or 0) > 0
        for ld in loads
    )
    has_flux = any(
        isinstance(ld, dict) and ld.get("type") == "flux" and (ld.get("magnitude") or 0) != 0
        for ld in loads
    )

    if not has_fixed_temp and not has_convection:
        issues.append({
            "kind": "thermal_singular",
            "detail": (
                "Thermal study has no fixed-temperature BC and no convection film — the "
                "temperature datum is undefined (pure-Neumann conduction is singular). Add a "
                "fixed temperature or a convection boundary. A heat flux alone does not anchor "
                "the datum."
            ),
        })
    if not has_fixed_temp and not has_flux and not has_convection:
        issues.append({
            "kind": "thermal_no_load",
            "detail": (
                "Thermal study has no heat input (no flux, no convection) and no imposed "
                "temperature difference — the solve is trivial (uniform temperature)."
            ),
        })
    return issues


def _thermal_bc_fix(kind: str) -> dict:
    """Candidate fix for a thermal BC-lint issue kind."""
    if kind == "thermal_singular":
        return {
            "action": "add_thermal_datum",
            "params": {"hint": "Fix a face temperature (temp_c) or add a convection film "
                               "(type='convection', h, t_amb_c) to anchor the temperature."},
            "basis": "Steady conduction needs a Dirichlet or Robin BC to fix the datum.",
            "predicted_effect": "Conductivity matrix becomes non-singular; solve proceeds.",
            "confidence": "heuristic",
        }
    return {
        "action": "add_heat_input",
        "params": {"hint": "Add a heat flux, a convection film, or a temperature difference."},
        "basis": "No heat input + no ΔT ⇒ a uniform-temperature trivial result.",
        "predicted_effect": "A meaningful temperature field is computed.",
        "confidence": "heuristic",
    }


def _mesh_suggestion(study: dict, doc_geom: Optional[dict] = None) -> Optional[str]:
    """Return a mesh-size suggestion string, or None when no suggestion is warranted.

    Uses min_fillet_radius or min_wall_mm from doc_geom if provided, else the
    study's mesh_fineness (a coarse signal).  The suggestion is purely advisory
    (never a blocker) and is emitted as an ``info`` Finding.

    The returned string is the suggestion text, not an advisory item.
    """
    current_fine = float(study.get("mesh_fineness") or 0.5)
    if doc_geom:
        min_fillet = doc_geom.get("min_fillet_radius_mm")
        min_wall = doc_geom.get("min_wall_mm")
        feature_size = None
        if min_fillet and float(min_fillet) > 0:
            feature_size = float(min_fillet)
        elif min_wall and float(min_wall) > 0:
            feature_size = float(min_wall)
        if feature_size is not None:
            # Rule of thumb: at least 2 elements across the smallest feature.
            target_fine = min(0.9, max(0.1, round(feature_size / 5.0, 2)))
            if current_fine < target_fine * 0.8 or current_fine > target_fine * 2.0:
                return (f"Min feature size ≈ {feature_size:.2f} mm — "
                        f"suggested mesh_fineness ≈ {target_fine:.2f} "
                        f"(current {current_fine:.2f}).")
    # Fallback: coarse mesh warning
    if current_fine < 0.2:
        return (f"mesh_fineness {current_fine:.2f} is very coarse — "
                "consider ≥ 0.3 for preliminary accuracy.")
    return None


# ---------------------------------------------------------------------------
# Public API — precheck
# ---------------------------------------------------------------------------

#: When True, BC-lint ``blocker`` items STOP dispatch before the solve.
#: This is a deliberate deviation from the §9.3/§10 freedom rule — see module docstring.
PRECHECK_BLOCK_ENABLED: bool = os.environ.get("CHUDWARE_SIM_PRECHECK_BLOCK", "1") not in ("0", "false")


def precheck(study: dict, doc_geom: Optional[dict] = None) -> list[dict]:
    """Pre-dispatch sanity checks on *study*.

    Returns a list of ``aicad.advisory/1``-shaped items (same shape as
    ``engine._finding_to_item`` output).  Callers inject these directly or pass them
    through ``build_advisory``.

    Parameters
    ----------
    study : dict
        A study spec — must include ``type``, ``fixtures`` (list of str),
        ``loads`` (list of dicts with ``force`` list), and optionally
        ``dangling_refs`` (list of unresolvable face refs from a prior doc-query),
        ``mesh_fineness`` (float), ``material`` (dict with ``E_mpa``),
        ``bbox`` (dict with ``x_mm/y_mm/z_mm``).
    doc_geom : dict | None
        Optional geometry hints from a prior doc-query:
        ``min_fillet_radius_mm``, ``min_wall_mm``.
    """
    items: list[dict] = []
    thermal = _is_thermal(study)

    # --- BC lint ---
    if thermal:
        # Thermal studies lint for the singular temperature datum (no fixed-T + no convection).
        for issue in _thermal_bc_issues(study):
            kind = issue["kind"]
            is_blocker = kind == "thermal_singular" and PRECHECK_BLOCK_ENABLED
            sev = "blocker" if is_blocker else "action"
            items.append({
                "schema": "aicad.advisory/1",
                "check": "sim_bc",
                "severity": sev,
                "finding": issue["detail"],
                "refs": {},
                "numbers": {},
                "candidate_fixes": [_thermal_bc_fix(kind)],
                "helper_tools": ["run_fea_static", "describe_part"],
                "source": {"kind": "heuristic", "detail": "sim_advisor.precheck (thermal)"},
            })
    else:
        for issue in _bc_issues(study):
            kind = issue["kind"]
            # blocker kinds that halt dispatch (config-gated)
            is_blocker = kind in ("no_fixture", "no_load", "dangling_ref") and PRECHECK_BLOCK_ENABLED
            sev = "blocker" if is_blocker else "action"
            item = {
                "schema": "aicad.advisory/1",
                "check": "sim_bc",
                "severity": sev,
                "finding": issue["detail"],
                "refs": {},
                "numbers": {},
                "candidate_fixes": [_bc_fix(kind)],
                "helper_tools": ["run_fea_static", "describe_part"],
                "source": {"kind": "heuristic", "detail": "sim_advisor.precheck"},
            }
            items.append(item)

    # --- Mesh size suggestion ---
    mesh_hint = _mesh_suggestion(study, doc_geom)
    if mesh_hint:
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_mesh",
            "severity": "info",
            "finding": mesh_hint,
            "refs": {},
            "numbers": {"mesh_fineness": study.get("mesh_fineness")},
            "candidate_fixes": [],
            "helper_tools": ["run_fea_static"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.precheck"},
        })

    # --- Hand-calc anchor (order-of-magnitude cross-check) ---
    mat = study.get("material") or {}
    bbox = study.get("bbox")
    loads = study.get("loads") or []

    if thermal:
        # Thermal anchor: 1D plane-wall Fourier reference from the fixed-T + BC set.
        k_w_mk = mat.get("k_w_mk") or study.get("k_w_mk")
        fixtures = study.get("fixtures") or []
        fixed_temps = [float(fx["temp_c"]) for fx in fixtures
                       if isinstance(fx, dict) and fx.get("temp_c") is not None]
        conv = next((ld for ld in loads
                     if isinstance(ld, dict) and ld.get("type") == "convection"), None)
        t_anchor = None
        if k_w_mk and fixed_temps:
            t_hot = max(fixed_temps)
            if len(fixed_temps) >= 2:
                t_anchor = thermal_anchor(bbox or {}, float(k_w_mk), t_hot,
                                          t_cold_c=min(fixed_temps))
            elif conv is not None and (conv.get("h") or 0) > 0:
                t_anchor = thermal_anchor(bbox or {}, float(k_w_mk), t_hot,
                                          h_w_m2k=float(conv.get("h")),
                                          t_amb_c=float(conv.get("t_amb_c", 20.0)))
        anchor_detail = (
            f"Thermal anchor ({t_anchor['mode']}): wall flux {t_anchor['q_w_m2']:.1f} W/m² "
            f"(L={t_anchor['L_mm']:.1f} mm, k={t_anchor['k_w_mk']:.1f} W/m·K, "
            f"cold face ≈ {t_anchor['t_cold_c']:.1f} °C); source: {t_anchor['source']}."
            if t_anchor else
            "No thermal anchor — need a bbox, conductivity, and either two fixed-temperature "
            "faces or a fixed face + convection to map to a 1D plane wall. FEA result will not "
            "be cross-checked."
        )
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_anchor",
            "severity": "info",
            "finding": anchor_detail,
            "refs": {},
            "numbers": {"anchor_flux_w_m2": t_anchor["q_w_m2"] if t_anchor else None,
                        "anchor_cold_face_c": t_anchor["t_cold_c"] if t_anchor else None},
            "candidate_fixes": [],
            "helper_tools": [],
            "source": {"kind": "calc", "detail": "thermal_anchor (1D plane wall)"},
            "_anchor": t_anchor,
        })
        return items

    E_mpa = mat.get("E_mpa")
    total_load_n = 0.0
    for ld in loads:
        force = ld.get("force") or [0, 0, 0]
        total_load_n += math.sqrt(sum(float(v) ** 2 for v in force))

    anchor = None
    if E_mpa and bbox and total_load_n > 0:
        anchor = compute_beam_anchor(bbox, float(E_mpa), total_load_n)

    anchor_detail = (
        f"Beam anchor: {anchor['max_stress_mpa']:.2f} MPa (cantilever, rect section, "
        f"L={anchor.get('L_mm', '?'):.1f} mm, aspect={anchor.get('aspect_ratio', '?')}); "
        f"source: {anchor.get('source', 'beam_calc_raw')}."
        if anchor else
        "No beam anchor — geometry does not map confidently to a beam/plate approximation "
        "(aspect ratio < 3 or missing bbox/load). FEA result will not be cross-checked."
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
            "params": {"hint": "Add a force or pressure on the loaded face."},
            "basis": "No load ⇒ trivially zero stress result.",
            "predicted_effect": "Meaningful stress field computed.",
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
        "basis": "Unresolvable refs are silently dropped by the solver, giving wrong BCs.",
        "predicted_effect": "Correct boundary condition applied.",
        "confidence": "heuristic",
    }


# ---------------------------------------------------------------------------
# Public API — postcheck
# ---------------------------------------------------------------------------

#: Maximum ratio of FEA stress to beam-anchor stress before emitting a divergence WARN.
DIVERGENCE_WARN_RATIO: float = float(
    os.environ.get("CHUDWARE_SIM_DIVERGENCE_RATIO",
                   str(_load_policy().get("_doc", {}).get("divergence_warn_ratio", 5.0)))
)


def postcheck(
    study: dict,
    precheck_items: Optional[list[dict]] = None,
    doc_geom: Optional[dict] = None,
) -> dict:
    """Post-solve analysis of *study* results.

    Returns a dict::

        {
            "verdict":   "PASS" | "WARN" | "FAIL",
            "sf_used":   float,
            "mat_class": str,
            "margin_mpa": float | None,
            "anchor_stress_mpa": float | None,
            "divergence_ratio": float | None,
            "items": [ aicad.advisory/1-shaped dicts ],
        }

    Parameters
    ----------
    study : dict
        A completed study record (same shape as the ``_seed_static_study`` fixture
        in ``test_gsl_tools.py``).  Must include ``results.max_von_mises_mpa``,
        ``material.yield_mpa`` / ``material.E_mpa``, and optionally ``material.name``.
    precheck_items : list | None
        Items returned by :func:`precheck` for this study.  When present, the anchor
        is extracted from the ``sim_anchor`` item so precheck + postcheck share one
        anchor computation.  When absent, postcheck recomputes from ``study.bbox``.
    doc_geom : dict | None
        Optional geometry hints; used for anchor recomputation if precheck_items absent.
    """
    results = study.get("results") or {}
    mat = study.get("material") or {}
    vm = results.get("max_von_mises_mpa")
    yield_mpa = mat.get("yield_mpa")
    ultimate_mpa = mat.get("ultimate_mpa")
    mat_name = mat.get("name") or "unknown"

    policy_rec = resolve_material_class(mat_name)
    sf = policy_rec["sf_yield"]
    mat_class = policy_rec["class"]

    items: list[dict] = []
    verdict = "PASS"
    margin_mpa = None
    divergence_ratio = None

    # --- Margin verdict ---
    if vm is not None and yield_mpa is not None:
        margin_mpa = yield_mpa - vm
        design_stress = vm * sf
        if design_stress <= yield_mpa:
            verdict = "PASS"
            verdict_detail = (
                f"Max stress {vm:.2f} MPa × SF {sf:.1f} = {design_stress:.2f} MPa "
                f"≤ yield {yield_mpa:.1f} MPa — PASS."
            )
            sev = "info"
        elif ultimate_mpa is not None and design_stress <= ultimate_mpa:
            verdict = "WARN"
            verdict_detail = (
                f"Max stress {vm:.2f} MPa × SF {sf:.1f} = {design_stress:.2f} MPa "
                f"exceeds yield {yield_mpa:.1f} MPa but ≤ ultimate {ultimate_mpa:.1f} MPa — "
                f"WARN: permanent deformation predicted under design load."
            )
            sev = "action"
        else:
            verdict = "FAIL"
            ult_str = f"(ultimate {ultimate_mpa:.1f} MPa)" if ultimate_mpa else ""
            verdict_detail = (
                f"Max stress {vm:.2f} MPa × SF {sf:.1f} = {design_stress:.2f} MPa "
                f"exceeds yield {yield_mpa:.1f} MPa {ult_str} — FAIL: fracture risk."
            )
            sev = "blocker"

        fix = _margin_fix(verdict, mat_class, sf)
        margin_item: dict = {
            "schema": "aicad.advisory/1",
            "check": "sim_margin",
            "severity": sev,
            "finding": verdict_detail,
            "refs": {},
            "numbers": {
                "max_stress_mpa": vm,
                "yield_mpa": yield_mpa,
                "sf_yield": sf,
                "design_stress_mpa": round(design_stress, 3),
                "margin_mpa": round(margin_mpa, 3),
                "mat_class": mat_class,
            },
            "candidate_fixes": [fix] if fix else [],
            "helper_tools": ["describe_results", "run_fea_static"],
            "source": {"kind": "table", "detail": f"margin_policy.json [{mat_class}]"},
        }
        # §3 invariant: non-info must carry ≥1 fix
        if sev != "info" and not margin_item["candidate_fixes"]:
            margin_item["severity"] = "info"
        items.append(margin_item)
    else:
        items.append({
            "schema": "aicad.advisory/1",
            "check": "sim_margin",
            "severity": "info",
            "finding": "Margin verdict skipped — max_von_mises_mpa or yield_mpa not in study record.",
            "refs": {},
            "numbers": {},
            "candidate_fixes": [],
            "helper_tools": ["run_fea_static"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.postcheck"},
        })

    # --- Beam-anchor divergence check ---
    anchor = _extract_anchor(precheck_items)
    if anchor is None:
        # Recompute anchor when precheck_items not supplied
        E_mpa = mat.get("E_mpa")
        bbox = study.get("bbox")
        loads = study.get("loads") or []
        total_load_n = 0.0
        for ld in loads:
            force = ld.get("force") or [0, 0, 0]
            total_load_n += math.sqrt(sum(float(v) ** 2 for v in force))
        if E_mpa and bbox and total_load_n > 0:
            anchor = compute_beam_anchor(bbox, float(E_mpa), total_load_n)

    anchor_stress = anchor["max_stress_mpa"] if anchor else None
    if anchor_stress is not None and vm is not None and anchor_stress > 0:
        divergence_ratio = vm / anchor_stress
        if divergence_ratio > DIVERGENCE_WARN_RATIO or divergence_ratio < 1.0 / DIVERGENCE_WARN_RATIO:
            direction = "higher" if divergence_ratio > 1 else "lower"
            ratio_str = f"{divergence_ratio:.1f}×" if divergence_ratio > 1 else f"{1/divergence_ratio:.1f}× lower"
            items.append({
                "schema": "aicad.advisory/1",
                "check": "sim_divergence",
                "severity": "action",
                "finding": (
                    f"FEA result ({vm:.2f} MPa) is {ratio_str} the beam-anchor estimate "
                    f"({anchor_stress:.2f} MPa) — result may be implausible; check BCs and loads."
                ),
                "refs": {},
                "numbers": {
                    "fea_stress_mpa": vm,
                    "anchor_stress_mpa": anchor_stress,
                    "divergence_ratio": round(divergence_ratio, 3),
                    "warn_threshold": DIVERGENCE_WARN_RATIO,
                },
                "candidate_fixes": [{
                    "action": "review_bc_and_loads",
                    "params": {"hint": "Verify fixture faces and load magnitudes match the physical setup."},
                    "basis": (f"FEA/beam ratio {divergence_ratio:.1f}× exceeds the "
                              f"{DIVERGENCE_WARN_RATIO:.0f}× plausibility threshold."),
                    "predicted_effect": "Result converges with the hand-calc order of magnitude.",
                    "confidence": "heuristic",
                }],
                "helper_tools": ["run_fea_static", "describe_results"],
                "source": {"kind": "calc", "detail": "beam_calc_raw cross-check"},
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
                f"Max stress located at {face_type} on feature '{feature_name}' — "
                "stress concentration (Kt) likely elevated. "
                "Consider increasing the fillet radius to reduce Kt "
                "(Shigley §6-8: Kt ≈ 1 + 2√(d/r) for sharp notches)."
            ),
            "refs": {"feature": feature_name, "face_type": face_type},
            "numbers": {},
            "candidate_fixes": [{
                "action": "edit_feature_param",
                "params": {"feature": feature_name, "param": "fillet_radius",
                           "hint": "Increase fillet radius to lower stress concentration."},
                "basis": "Shigley §6-8 Kt analysis: larger radius → lower Kt → lower local stress.",
                "predicted_effect": "Reduced peak stress at the fillet/notch; re-run FEA to verify.",
                "confidence": "heuristic",
            }],
            "helper_tools": ["run_fea_static", "describe_results"],
            "source": {"kind": "heuristic", "detail": "sim_advisor.postcheck (Kt pattern)"},
        })

    return {
        "verdict": verdict,
        "sf_used": sf,
        "mat_class": mat_class,
        "margin_mpa": margin_mpa,
        "anchor_stress_mpa": anchor_stress,
        "divergence_ratio": divergence_ratio,
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
            "params": {"hint": f"Increase section modulus to bring design stress below yield "
                                f"(target: max_stress × {sf:.1f} ≤ yield)."},
            "basis": f"margin_policy [{mat_class}] SF={sf:.1f}; design stress exceeds yield.",
            "predicted_effect": "Stress in elastic range under design load.",
            "confidence": "heuristic",
        }
    # FAIL
    return {
        "action": "redesign_or_change_material",
        "params": {"hint": f"Significantly reduce stress or upgrade material "
                           f"(target: max_stress × {sf:.1f} ≤ yield)."},
        "basis": f"margin_policy [{mat_class}] SF={sf:.1f}; design stress exceeds ultimate — fracture risk.",
        "predicted_effect": "Part survives design load.",
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
    verdict = postcheck_result.get("verdict", "PASS")
    sf = postcheck_result.get("sf_used")
    mat_class = postcheck_result.get("mat_class", "general")
    anchor = postcheck_result.get("anchor_stress_mpa")
    div_ratio = postcheck_result.get("divergence_ratio")

    lines.append(f"\nAdvisor verdict ({mat_class}, SF={sf:.1f}): {verdict}")

    if anchor is not None:
        if div_ratio is not None and (
            div_ratio > DIVERGENCE_WARN_RATIO or div_ratio < 1.0 / DIVERGENCE_WARN_RATIO
        ):
            lines.append(
                f"  Beam anchor: {anchor:.2f} MPa — FEA diverges {div_ratio:.1f}×; verify BCs."
            )
        else:
            lines.append(f"  Beam anchor: {anchor:.2f} MPa — consistent with FEA.")
    else:
        lines.append("  Beam anchor: not computed (geometry not beam-like or data missing).")

    for it in postcheck_result.get("items", []):
        if it.get("check") in ("sim_margin", "sim_divergence") and it.get("severity") != "info":
            lines.append(f"  [{it['severity'].upper()}] {it['finding']}")

    return "\n".join(lines)

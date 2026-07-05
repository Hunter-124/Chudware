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
  * **Beam anchor**: ``engine_oracle.research.calcs.beam.beam_calc_raw`` is the
    hand-calc primitive.  The doc→beam mapping is *confident* only when:
      - aspect ratio L/√(A) > 3 (long-axis dominant geometry)
      - load pattern is a plausible cantilever or simply-supported point/UDL
    When the mapping is ambiguous the anchor is ``None`` — "no anchor" is the common
    path; the postcheck never skips the margin verdict just because anchor is absent.
  * **Precheck-blocks-dispatch** is a deliberate deviation from the freedom rule
    (§9.3/§10): BC-lint blockers stop dispatch when the guard is config-enabled.
    This is documented here rather than in the contract so the contract stays frozen.
    Gate: ``AI_CAD_SIM_PRECHECK_BLOCK`` env var, default ``"1"`` (enabled).

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

_POLICY_PATH = pathlib.Path(__file__).parent.parent.parent.parent / "engine_oracle" / "sim" / "margin_policy.json"
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
        from engine_oracle.research.calcs.beam import beam_calc_raw  # noqa: PLC0415
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
PRECHECK_BLOCK_ENABLED: bool = os.environ.get("AI_CAD_SIM_PRECHECK_BLOCK", "1") not in ("0", "false")


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

    # --- BC lint ---
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
    E_mpa = mat.get("E_mpa")
    bbox = study.get("bbox")
    loads = study.get("loads") or []
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
    os.environ.get("AI_CAD_SIM_DIVERGENCE_RATIO",
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

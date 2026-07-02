"""Demand/Capacity ratio post-processor for the structural workstream (T-D3).

Implements simplified AISC LRFD (steel) and NDS ASD (wood) checks, consuming
:class:`~engine.structural.analysis.AnalysisResult` member-force output from T-D2.

Design-basis notes
------------------
**Steel — AISC LRFD, elastic checks only (conservative).**

  * Axial yield (squash): φPy = φ · A · Fy.  No slenderness / Euler buckling — conservative for
    short compact members; label = ``axial_yield_LRFD``.
  * Bending at first yield: φMy = φ · Sx · Fy.  This is *not* the full plastic φMp = φFyZx
    that AISC tables print — it is intentionally conservative (elastic fibre at extreme, not
    full plastification).  Label = ``bending_yield_LRFD``.
  * φ = 0.90 (resistance factor for yielding) per AISC 360 §D2 / §F2.

**Wood — NDS ASD (allowable stress).**

  * Bending: fb/Fb where fb = M/Sx, Fb = allowable bending stress (no load-duration / wet-service
    factors in v1).  Label = ``bending_ASD_NDS``.
  * Compression parallel: fc/Fc where fc = |P|/A.  No column stability (Cp) in v1.
    Label = ``axial_compression_ASD_NDS``.
  * Values: Douglas Fir-Larch No.2 Fb = 900 psi = 6.20 MPa, Fc = 1350 psi = 9.31 MPa (public-
    domain reference values from NDS Supplement Table 4A as reproduced in open textbooks).

**Governing check** — the maximum of the axial and bending D/C ratios; that limit state is reported
as ``governing_limit``.

Unit conversions (ALL explicit — see work/questions/T-D3.md)
-------------------------------------------------------------
``check_member`` receives SI AnalysisResult values (N, N·mm) and the section dict (mm², mm³).
The ``dc_ratio_axial`` and ``dc_ratio_bending`` helpers accept *caller-consistent* SI units
(N, mm², MPa → N/mm²; and N·mm, mm³, MPa respectively) to avoid silent scaling bugs.

Disclaimer
----------
Every :class:`CheckResult` carries the mandatory disclaimer string (byte-exact):

    "Preliminary check — not sealed engineering. Verify with a licensed structural engineer
    before construction."

This is a non-negotiable field; ``struct_check`` always includes it in its JSON response.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

# --------------------------------------------------------------------------- constants

# LRFD resistance factor for yielding (AISC 360 §D2 / §F2)
PHI_LRFD = 0.90

# Steel Fy values (MPa) — public-domain reference values
_STEEL_FY: dict[str, float] = {
    "steel":     248.0,   # A36 (Fy = 36 ksi = 248 MPa)
    "steel_a36": 248.0,
    "a36":       248.0,
    "steel_a992": 344.7,  # A992 (Fy = 50 ksi = 344.7 MPa) — common for W-shapes
    "a992":       344.7,
}
_DEFAULT_STEEL_FY = 248.0  # A36 fallback

# NDS ASD allowable stresses for Douglas Fir-Larch No.2 (public-domain, NDS Supplement Table 4A)
# Values in MPa (1 psi = 0.006895 MPa)
_WOOD_FB: dict[str, float] = {  # allowable bending stress
    "df":     6.205,   # Douglas Fir-Larch No.2: Fb = 900 psi
    "df_no2": 6.205,
    "douglas": 6.205,
    "fir":    6.205,
    "spf":    5.516,   # Spruce-Pine-Fir No.2: Fb = 800 psi
    "cedar":  6.205,   # Western Red Cedar: Fb ~ 900 psi (select structural; conservative)
    "lumber": 6.205,
    "wood":   6.205,
}
_WOOD_FC: dict[str, float] = {  # allowable compression parallel to grain
    "df":     9.307,   # Douglas Fir-Larch No.2: Fc = 1350 psi
    "df_no2": 9.307,
    "douglas": 9.307,
    "fir":    9.307,
    "spf":    7.929,   # Spruce-Pine-Fir No.2: Fc = 1150 psi
    "cedar":  8.618,   # Western Red Cedar: Fc ~ 1250 psi
    "lumber": 9.307,
    "wood":   9.307,
}

# Byte-exact disclaimer string — enforced by tests
DISCLAIMER = (
    "Preliminary check — not sealed engineering. "
    "Verify with a licensed structural engineer before construction."
)

# --------------------------------------------------------------------------- result dataclass


@dataclass
class CheckResult:
    """D/C ratio result for a single member under a single load combination.

    ``dc_ratio`` is the governing demand/capacity ratio (≤1.0 passes).
    ``governing_limit`` names the controlling limit state.
    ``disclaimer`` is always :data:`DISCLAIMER` — byte-exact, non-negotiable.
    """
    element_id: str
    combo: str
    dc_ratio: float
    passed: bool
    governing_limit: str
    dc_axial: float
    dc_bending: float
    disclaimer: str

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- material helpers


def _is_steel(material: str) -> bool:
    key = (material or "").strip().lower()
    return key.startswith(("steel", "a36", "a992"))


def _is_wood(material: str) -> bool:
    key = (material or "").strip().lower()
    return any(key.startswith(w) for w in ("cedar", "df", "douglas", "fir", "spf", "lumber", "wood"))


def _steel_fy(material: str) -> float:
    """Yield strength (MPa) for a steel material key.

    Longer keys are checked first so ``steel_a992`` is found before the ``steel`` prefix.
    """
    key = (material or "").strip().lower()
    for k in sorted(_STEEL_FY, key=len, reverse=True):
        if key == k or key.startswith(k):
            return _STEEL_FY[k]
    return _DEFAULT_STEEL_FY


def _wood_fb(material: str) -> float:
    """Allowable bending stress (MPa) for a wood material key (longer keys checked first)."""
    key = (material or "").strip().lower()
    for k in sorted(_WOOD_FB, key=len, reverse=True):
        if key == k or key.startswith(k):
            return _WOOD_FB[k]
    return _WOOD_FB["df"]


def _wood_fc(material: str) -> float:
    """Allowable compression stress (MPa) for a wood material key (longer keys checked first)."""
    key = (material or "").strip().lower()
    for k in sorted(_WOOD_FC, key=len, reverse=True):
        if key == k or key.startswith(k):
            return _WOOD_FC[k]
    return _WOOD_FC["df"]


# --------------------------------------------------------------------------- ratio helpers


def dc_ratio_axial(force_n: float, area_mm2: float, Fy_MPa: float,
                   phi: float = PHI_LRFD) -> float:
    """LRFD axial yield D/C: |P| / (φ · A · Fy).

    Parameters are in **consistent SI** units (N, mm², MPa = N/mm²) — no hidden conversion.
    ``Fy_MPa`` is interpreted directly as N/mm²; capacity = φ·A·Fy_MPa [N].

    Returns 0.0 for zero-area sections (degenerate guard).
    """
    capacity = phi * area_mm2 * Fy_MPa  # N
    if capacity == 0.0:
        return 0.0
    return abs(force_n) / capacity


def dc_ratio_bending(moment_nmm: float, Sx_mm3: float, Fy_MPa: float,
                     phi: float = PHI_LRFD) -> float:
    """LRFD bending at first yield D/C: M / (φ · Sx · Fy).

    Parameters are in **consistent SI** units (N·mm, mm³, MPa) — no hidden conversion.
    Capacity = φ · Sx · Fy_MPa [N·mm].

    Returns 0.0 for zero-section modulus (degenerate guard).
    """
    capacity = phi * Sx_mm3 * Fy_MPa  # N·mm
    if capacity == 0.0:
        return 0.0
    return abs(moment_nmm) / capacity


def dc_ratio_axial_wood(force_n: float, area_mm2: float, Fc_MPa: float) -> float:
    """NDS ASD compression D/C: |P| / (Fc · A).  Returns 0 for zero area."""
    capacity = Fc_MPa * area_mm2  # N
    if capacity == 0.0:
        return 0.0
    return abs(force_n) / capacity


def dc_ratio_bending_wood(moment_nmm: float, Sx_mm3: float, Fb_MPa: float) -> float:
    """NDS ASD bending D/C: M / (Fb · Sx).  Returns 0 for zero section modulus."""
    capacity = Fb_MPa * Sx_mm3  # N·mm
    if capacity == 0.0:
        return 0.0
    return abs(moment_nmm) / capacity


# --------------------------------------------------------------------------- member check


def check_member(member_result: dict, section: dict, material: str) -> CheckResult:
    """Run axial + bending D/C checks for a member and return a :class:`CheckResult`.

    Parameters
    ----------
    member_result:
        One row from ``AnalysisResult.to_dict()``: must contain ``element_id``, ``combo``,
        ``axial_n`` (N, +tension), ``moment_nmm`` (N·mm).
    section:
        Section properties dict from :func:`~engine.structural.sections.get_aisc_section` or
        :func:`~engine.structural.sections.get_lumber_section`.  Must contain ``A`` (mm²) and
        ``Sx`` (mm³).
    material:
        Material key (e.g. ``"steel_a36"``, ``"steel_a992"``, ``"df_no2"``, ``"cedar"``).

    Returns
    -------
    :class:`CheckResult` with ``dc_ratio``, ``passed``, ``governing_limit``, and ``disclaimer``.

    Notes
    -----
    - Steel → LRFD elastic (φFy for both axial squash and bending first-yield); φ = 0.90.
    - Wood → NDS ASD (Fb, Fc from the NDS Supplement public-domain values); no reduction factors.
    - Forces/moments from ``member_result`` are in N / N·mm — section in mm² / mm³.  All
      conversions are explicit (no kN/kN·m intermediate step) to avoid silent scaling bugs.
    """
    element_id = member_result.get("element_id", "")
    combo = member_result.get("combo", "")
    axial_n = float(member_result.get("axial_n", 0.0))
    moment_nmm = float(member_result.get("moment_nmm", 0.0))

    area_mm2 = float(section.get("A", 0.0))
    Sx_mm3 = float(section.get("Sx", 0.0))

    if _is_steel(material):
        Fy = _steel_fy(material)
        dc_ax = dc_ratio_axial(axial_n, area_mm2, Fy)
        dc_bm = dc_ratio_bending(moment_nmm, Sx_mm3, Fy)
        lim_ax = "axial_yield_LRFD"
        lim_bm = "bending_yield_LRFD"
    else:
        # Default to NDS ASD wood model for all non-steel materials
        Fb = _wood_fb(material)
        Fc = _wood_fc(material)
        dc_ax = dc_ratio_axial_wood(axial_n, area_mm2, Fc)
        dc_bm = dc_ratio_bending_wood(moment_nmm, Sx_mm3, Fb)
        lim_ax = "axial_compression_ASD_NDS"
        lim_bm = "bending_ASD_NDS"

    if dc_bm >= dc_ax:
        governing = lim_bm
        dc = dc_bm
    else:
        governing = lim_ax
        dc = dc_ax

    return CheckResult(
        element_id=element_id,
        combo=combo,
        dc_ratio=dc,
        passed=(dc <= 1.0),
        governing_limit=governing,
        dc_axial=dc_ax,
        dc_bending=dc_bm,
        disclaimer=DISCLAIMER,
    )


# --------------------------------------------------------------------------- batch


def check_all_members(
    analysis_results: list[dict],
    section_lookup,
    material_lookup,
) -> list[CheckResult]:
    """Run :func:`check_member` for every row in ``analysis_results``.

    Parameters
    ----------
    analysis_results:
        List of ``AnalysisResult.to_dict()`` rows (from ``analyze_doc``).
    section_lookup:
        Callable ``(element_id: str) -> dict`` returning the section property dict for a member.
    material_lookup:
        Callable ``(element_id: str) -> str`` returning the material key for a member.

    Returns
    -------
    List of :class:`CheckResult`, one per ``analysis_results`` row.
    """
    out: list[CheckResult] = []
    for row in analysis_results:
        eid = row.get("element_id", "")
        section = section_lookup(eid)
        material = material_lookup(eid)
        out.append(check_member(row, section, material))
    return out

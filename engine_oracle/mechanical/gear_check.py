"""AGMA spur-gear tooth bending + contact (pitting) stress check (T-BL18).

Implements AGMA 2001 bending stress (Shigley's Eq. 14-15) and contact stress
(Shigley's Eq. 14-16) as described in:

    Budynas & Nisbett, *Shigley's Mechanical Engineering Design*, 10th ed.,
    Chapter 14 (McGraw-Hill Education).

The formulas are published in open engineering textbooks (Shigley's, Norton,
Juvinall & Marshek) and are a clean-room re-implementation from those published
equations — not a copy of AGMA 2001-D04.

Geometry factors J (bending) and I (contact) must be supplied by the caller;
they are taken from AGMA 908-B89 charts or equivalent textbook tables and are
NOT bundled here (chart data is copyrighted to AGMA; cite your source).

Units: SI throughout.  Lengths in mm, forces in N, stresses in MPa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Disclaimer (byte-exact — tests assert this constant)
# ---------------------------------------------------------------------------

DISCLAIMER: str = (
    "Preliminary engineering estimate — not a certified analysis. "
    "Verify with a qualified mechanical engineer before use in safety-critical applications."
)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class GearCheckResult:
    """Output of :func:`gear_check`.

    Attributes
    ----------
    sigma_bending_MPa:
        AGMA bending stress at the tooth root, MPa.
    sigma_contact_MPa:
        AGMA contact (Hertz / pitting) stress at the pitch point, MPa.
    SF_bending:
        Bending safety factor (allowable / sigma_b).  ``None`` when
        *allowable_bending_stress* was not supplied.
    SF_contact:
        Contact safety factor (allowable / sigma_c).  ``None`` when
        *allowable_contact_stress* was not supplied.
    disclaimer:
        Standard preliminary-analysis disclaimer.
    """

    sigma_bending_MPa: float
    sigma_contact_MPa: float
    SF_bending: Optional[float]
    SF_contact: Optional[float]
    disclaimer: str = field(default=DISCLAIMER, init=False)

    def to_dict(self) -> dict:
        return {
            "sigma_bending_MPa": self.sigma_bending_MPa,
            "sigma_contact_MPa": self.sigma_contact_MPa,
            "SF_bending": self.SF_bending,
            "SF_contact": self.SF_contact,
            "disclaimer": self.disclaimer,
        }


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------


def gear_check(
    Wt: float,
    module: float,
    face_width: float,
    pitch_diameter: float,
    J: float,
    I: float,
    Ko: float = 1.0,
    Kv: float = 1.0,
    Ks: float = 1.0,
    Km: float = 1.0,
    KB: float = 1.0,
    Cf: float = 1.0,
    Cp: float = 191.0,
    allowable_bending_stress: Optional[float] = None,
    allowable_contact_stress: Optional[float] = None,
) -> GearCheckResult:
    """AGMA spur-gear bending + contact stress check.

    Parameters
    ----------
    Wt:
        Tangential (transmitted) load at the pitch cylinder, N.
    module:
        Transverse module (= pitch_diameter / number_of_teeth), mm.
        For spur gears the transverse module equals the normal module.
    face_width:
        Gear face width *b*, mm.
    pitch_diameter:
        Pitch diameter of the pinion *d_p*, mm.
    J:
        Bending-strength geometry factor (AGMA 908-B89 or textbook table).
        Caller must supply; do not fabricate.
    I:
        Contact-strength geometry factor (AGMA 908-B89 or textbook table).
        Caller must supply; do not fabricate.
    Ko:
        Overload factor (≥ 1.0).  Default 1.0 = uniform power source.
    Kv:
        Dynamic factor (≥ 1.0).  Accounts for pitch-line velocity error.
    Ks:
        Size factor.  Default 1.0; enter AGMA value for large gears.
    Km:
        Load-distribution factor (≥ 1.0).  Default 1.0 for narrow, well-aligned.
    KB:
        Rim-thickness factor (bending only; Shigley's Eq. 14-15).  Default 1.0
        = solid-body gear.
    Cf:
        Surface condition factor (contact only; Shigley's Eq. 14-16).
        Default 1.0 for normal finish.
    Cp:
        Elastic coefficient, MPa^0.5.  Default 191.0 = steel/steel
        (Shigley's 10th ed., Table 14-8).
    allowable_bending_stress:
        Allowable bending stress *S_t*, MPa.  When supplied, SF_bending is
        returned.  Omit (or pass ``None``) for stress-only output.
    allowable_contact_stress:
        Allowable contact stress *S_c*, MPa.  When supplied, SF_contact is
        returned.  Omit (or pass ``None``) for stress-only output.

    Returns
    -------
    GearCheckResult

    References
    ----------
    Budynas & Nisbett, *Shigley's Mechanical Engineering Design*, 10th ed.,
    Eqs. 14-15 (bending) and 14-16 (contact).

    Examples
    --------
    Worked example (adapted from Shigley's Ch. 14):
    >>> r = gear_check(Wt=1000, module=4, face_width=50, pitch_diameter=100,
    ...                J=0.36, I=0.107, Ko=1.25, Kv=1.52, Ks=1.0, Km=1.8, Cf=1.0)
    >>> round(r.sigma_bending_MPa, 1)
    47.5
    >>> round(r.sigma_contact_MPa, 0)
    483.0
    """
    if face_width <= 0 or module <= 0 or pitch_diameter <= 0:
        raise ValueError("face_width, module, and pitch_diameter must be positive.")
    if J <= 0 or I <= 0:
        raise ValueError("Geometry factors J and I must be positive.")
    if Wt <= 0:
        raise ValueError("Tangential load Wt must be positive.")

    # --- AGMA bending stress (Shigley's Eq. 14-15, SI form) ---
    # sigma_b = Wt * Ko * Kv * Ks * (1 / (b * m_t)) * (Km * KB / J)
    # (KB = rim-thickness factor; Cf belongs to the CONTACT equation only.)
    sigma_b = Wt * Ko * Kv * Ks * (Km * KB / J) / (face_width * module)

    # --- AGMA contact stress (Shigley's Eq. 14-16, SI form) ---
    # sigma_c = Cp * sqrt(Wt * Ko * Kv * Ks * Km * Cf / (d_p * b * I))
    under_root = Wt * Ko * Kv * Ks * Km * Cf / (pitch_diameter * face_width * I)
    sigma_c = Cp * math.sqrt(under_root)

    # --- Safety factors (optional) ---
    sf_b: Optional[float] = None
    if allowable_bending_stress is not None:
        sf_b = allowable_bending_stress / sigma_b

    sf_c: Optional[float] = None
    if allowable_contact_stress is not None:
        sf_c = allowable_contact_stress / sigma_c

    return GearCheckResult(
        sigma_bending_MPa=sigma_b,
        sigma_contact_MPa=sigma_c,
        SF_bending=sf_b,
        SF_contact=sf_c,
    )

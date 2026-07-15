"""ISO 281 rolling-element bearing L10 fatigue life (T-BL18).

Implements the basic rating life equation from ISO 281:2007 as described in:

    Budynas & Nisbett, *Shigley's Mechanical Engineering Design*, 10th ed.,
    Chapter 11 (McGraw-Hill Education).

The formula is published in open engineering textbooks (Shigley's, Norton,
Juvinall & Marshek) and is a clean-room re-implementation from those published
equations — not a copy of ISO 281:2007.

Dynamic load rating C is a **required explicit input**.  The AI-CAD parts
catalog (bearing:ISO 15:* entries) carries geometry only (bore, OD, width) —
it does NOT carry C.  Always supply C from the bearing manufacturer's datasheet
or catalog specification.  Never fabricate a rating.

Units: SI throughout.  Forces in N, speed in rpm, life in millions of revs and
hours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..mechanical.gear_check import DISCLAIMER  # single source of truth

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class BearingLifeResult:
    """Output of :func:`bearing_life`.

    Attributes
    ----------
    L10_million_rev:
        Basic rating life, millions of revolutions  ``(C/P)^p``.
    L10h_hours:
        Basic rating life, hours  ``10^6 / (60*n) * (C/P)^p``.
    P_equiv_N:
        Equivalent dynamic bearing load, N  ``X*Fr + Y*Fa``.
    p_exponent:
        Life exponent used: 3 for ball bearings, 10/3 for roller bearings.
    disclaimer:
        Standard preliminary-analysis disclaimer.
    bearing_designation:
        Bearing designation string (pass-through from caller; reference only).
    """

    L10_million_rev: float
    L10h_hours: float
    P_equiv_N: float
    p_exponent: float
    bearing_designation: str
    disclaimer: str = field(default=DISCLAIMER, init=False)

    def to_dict(self) -> dict:
        return {
            "L10_million_rev": self.L10_million_rev,
            "L10h_hours": self.L10h_hours,
            "P_equiv_N": self.P_equiv_N,
            "p_exponent": self.p_exponent,
            "bearing_designation": self.bearing_designation,
            "disclaimer": self.disclaimer,
        }


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

_BEARING_EXPONENTS: dict[str, float] = {
    "ball":   3.0,
    "roller": 10.0 / 3.0,
}


def bearing_life(
    C: float,
    Fr: float,
    Fa: float = 0.0,
    n_rpm: float = 1000.0,
    bearing_type: str = "ball",
    X: float = 1.0,
    Y: float = 0.0,
    bearing_designation: str = "",
) -> BearingLifeResult:
    """ISO 281 L10 bearing fatigue life.

    Parameters
    ----------
    C:
        Dynamic load rating, N.  **Required.**  Supply from the bearing
        manufacturer's datasheet or catalog.  The AI-CAD parts catalog
        carries bore/OD/width only — it does NOT carry C.  Do not fabricate.
    Fr:
        Radial load, N.
    Fa:
        Axial load, N.  Default 0 (pure radial).
    n_rpm:
        Shaft speed, rpm.
    bearing_type:
        ``"ball"`` (p = 3, default) or ``"roller"`` (p = 10/3).
    X:
        Radial load factor.  Default 1.0 for pure radial.
    Y:
        Axial load factor.  Default 0.0 for pure radial / no axial thrust.
    bearing_designation:
        Optional bearing designation string (e.g. ``"6205"``).  Passed through
        for reference only; geometry is not looked up and C is NOT derived
        from it.

    Returns
    -------
    BearingLifeResult

    Raises
    ------
    ValueError
        If ``C <= 0`` or both ``Fr`` and ``Fa`` are ≤ 0.

    References
    ----------
    ISO 281:2007, Eq. (1); Budynas & Nisbett, *Shigley's Mechanical
    Engineering Design*, 10th ed., Ch. 11, Eq. 11-1.

    Examples
    --------
    Worked example (Shigley's Ch. 11 style, ball bearing, pure radial):
    >>> r = bearing_life(C=30000, Fr=8000, Fa=0, n_rpm=1200, bearing_type="ball")
    >>> round(r.L10_million_rev, 3)
    52.734
    >>> round(r.L10h_hours, 1)
    732.4
    """
    if C <= 0:
        raise ValueError(
            "Dynamic load rating C must be positive. "
            "Supply C from the bearing manufacturer's datasheet."
        )
    if Fr <= 0 and Fa <= 0:
        raise ValueError("At least one of Fr (radial) or Fa (axial) must be positive.")

    btype = bearing_type.lower().strip()
    if btype not in _BEARING_EXPONENTS:
        raise ValueError(
            f"bearing_type must be 'ball' or 'roller', got {bearing_type!r}."
        )
    p = _BEARING_EXPONENTS[btype]

    # Equivalent dynamic load: P = X*Fr + Y*Fa
    P = X * Fr + Y * Fa
    if P <= 0:
        raise ValueError("Equivalent bearing load P = X*Fr + Y*Fa must be positive.")

    # L10 in millions of revolutions: (C/P)^p
    L10_mrev = (C / P) ** p

    # L10 in hours: 10^6 / (60 * n) * L10_mrev
    if n_rpm <= 0:
        raise ValueError("Shaft speed n_rpm must be positive.")
    L10h = 1_000_000.0 / (60.0 * n_rpm) * L10_mrev

    return BearingLifeResult(
        L10_million_rev=L10_mrev,
        L10h_hours=L10h,
        P_equiv_N=P,
        p_exponent=p,
        bearing_designation=bearing_designation,
    )

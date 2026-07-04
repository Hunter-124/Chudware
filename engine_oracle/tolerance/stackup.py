"""1D tolerance stack-up analysis (T-BL08).

Computes worst-case and RSS (root-sum-of-squares) stack-up over an ordered
chain of feature dimensions.  This is a *preliminary analysis tool* — results
are not a substitute for certified metrology or GD&T engineering review.

Reference
---------
The standard formulas implemented here are from:

    James D. Meadows, "Geometric Dimensioning and Tolerancing: Applications,
    Analysis & Measurement [per ASME Y14.5-2009]," ASME Press, 2009.
    Chapter 9, "Tolerance Stackup Analysis."

    Fischer, Bruce R., "Mechanical Tolerance Stackup and Analysis,"
    2nd ed., CRC Press, 2011, Chapter 3 ("The Tolerance Stackup").

Equal-bilateral formulation
---------------------------
Each contributor i has:
  - nominal_i  : nominal dimension (mm, or any consistent unit)
  - tol_i      : half-tolerance (always ≥ 0): the ± value, e.g. ±0.05 → tol=0.05
  - direction_i : +1 or -1 — sign in the 1D chain (positive adds to the gap,
                  negative subtracts).  The assembly gap equals
                  Σ direction_i * nominal_i.

Worst-case (WC)
---------------
  total_tol_wc = Σ |a_i| * tol_i     (sensitivity coeff |a_i| = 1 for 1D)
  gap_max = nominal_gap + total_tol_wc
  gap_min = nominal_gap - total_tol_wc

RSS (root-sum-of-squares, assumes normal distribution, ±3σ = tol)
------------------------------------------------------------------
  total_tol_rss = √(Σ (a_i * tol_i)²)
  gap_max_rss = nominal_gap + total_tol_rss
  gap_min_rss = nominal_gap - total_tol_rss

Sensitivity / contribution
--------------------------
Per-contributor contribution to total stack variation:
  contrib_wc_i   = |a_i| * tol_i          (absolute, worst-case)
  contrib_rss_i  = (a_i * tol_i)²         (squared, RSS)
  pct_wc_i       = contrib_wc_i / total_tol_wc * 100
  pct_rss_i      = contrib_rss_i / (total_tol_rss²) * 100

Results are sorted descending by worst-case contribution (highest sensitivity first).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


DISCLAIMER = (
    "Preliminary stack-up — not a certified tolerance analysis. "
    "Verify with a qualified dimensional-metrology engineer before release."
)


@dataclass
class DimContrib:
    """Per-dimension contribution row in a stackup report."""

    name: str
    """Human-readable label for this dimension."""

    nominal: float
    """Nominal dimension value (same units as the stack)."""

    tol: float
    """Half-tolerance (± value, ≥ 0)."""

    direction: float
    """Sensitivity coefficient: +1 or -1 for a 1D chain."""

    ref: Optional[str]
    """Source reference or doc feature ID (None if passed explicitly)."""

    assumed: bool
    """True if tolerance was defaulted/assumed because the doc model lacked it."""

    # --- computed fields (set by stackup_analyze) ---
    contrib_wc: float = field(default=0.0)
    """Absolute worst-case contribution = |direction| * tol."""

    contrib_rss_sq: float = field(default=0.0)
    """Squared RSS contribution = (direction * tol)²."""

    pct_wc: float = field(default=0.0)
    """Percent of total WC tolerance budget."""

    pct_rss: float = field(default=0.0)
    """Percent of total RSS variance budget."""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "nominal": self.nominal,
            "tol_half": self.tol,
            "direction": self.direction,
            "ref": self.ref,
            "assumed": self.assumed,
            "contrib_wc": round(self.contrib_wc, 8),
            "contrib_rss_sq": round(self.contrib_rss_sq, 8),
            "pct_wc": round(self.pct_wc, 4),
            "pct_rss": round(self.pct_rss, 4),
        }


@dataclass
class StackupResult:
    """Full stackup analysis result."""

    nominal_gap: float
    """Nominal value of the assembly gap (Σ direction_i * nominal_i)."""

    total_tol_wc: float
    """Total worst-case tolerance (Σ |a_i| * tol_i)."""

    total_tol_rss: float
    """Total RSS tolerance (√Σ (a_i * tol_i)²)."""

    gap_max_wc: float
    gap_min_wc: float
    gap_max_rss: float
    gap_min_rss: float

    contributors: list[DimContrib]
    """Sorted descending by worst-case contribution (sensitivity-first)."""

    assumed_dims: list[str]
    """Names of dimensions whose tolerances were defaulted (honest-degrade list)."""

    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "nominal_gap": round(self.nominal_gap, 8),
            "worst_case": {
                "total_tol": round(self.total_tol_wc, 8),
                "gap_max": round(self.gap_max_wc, 8),
                "gap_min": round(self.gap_min_wc, 8),
            },
            "rss": {
                "total_tol": round(self.total_tol_rss, 8),
                "gap_max": round(self.gap_max_rss, 8),
                "gap_min": round(self.gap_min_rss, 8),
            },
            "contributors": [c.to_dict() for c in self.contributors],
            "assumed_dims": self.assumed_dims,
            "disclaimer": self.disclaimer,
        }


def stackup_analyze(dims: Sequence[dict]) -> StackupResult:
    """Run 1D worst-case + RSS tolerance stackup over a chain of dimensions.

    Parameters
    ----------
    dims:
        Ordered list of dimension dicts.  Each dict must have:
          - ``name``    (str)   : label
          - ``nominal`` (float) : nominal value
          - ``tol``     (float) : half-tolerance (± value, ≥ 0)
          - ``direction`` (float, default +1) : sign in the 1D chain
        Optional keys:
          - ``ref``     (str|None) : source reference / feature ID
          - ``assumed`` (bool)     : True if tol was defaulted

    Returns
    -------
    StackupResult

    Raises
    ------
    ValueError
        If ``dims`` is empty, or any ``tol`` is negative.
    """
    if not dims:
        raise ValueError("stackup_analyze requires at least one dimension")

    contribs: list[DimContrib] = []
    assumed_dims: list[str] = []

    for i, d in enumerate(dims):
        name = str(d.get("name", f"dim_{i}"))
        try:
            nominal = float(d["nominal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Dimension {name!r}: missing or invalid 'nominal'") from exc
        try:
            tol = float(d["tol"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Dimension {name!r}: missing or invalid 'tol'") from exc
        if tol < 0:
            raise ValueError(f"Dimension {name!r}: 'tol' must be ≥ 0, got {tol}")

        direction = float(d.get("direction", 1.0))
        if direction not in (1.0, -1.0):
            # Clamp to sign — allow any nonzero, for future weighted use
            direction = math.copysign(1.0, direction) if direction != 0 else 1.0

        ref = d.get("ref", None)
        assumed = bool(d.get("assumed", False))
        if assumed:
            assumed_dims.append(name)

        c = DimContrib(
            name=name,
            nominal=nominal,
            tol=tol,
            direction=direction,
            ref=ref,
            assumed=assumed,
        )
        contribs.append(c)

    # --- compute nominal gap ---
    nominal_gap = sum(c.direction * c.nominal for c in contribs)

    # --- worst-case ---
    # total_tol_wc = Σ |a_i| * tol_i  (|a_i| = 1 for 1D)
    total_tol_wc = sum(abs(c.direction) * c.tol for c in contribs)

    # --- RSS ---
    # total_tol_rss = √(Σ (a_i * tol_i)²)
    rss_sq_sum = sum((c.direction * c.tol) ** 2 for c in contribs)
    total_tol_rss = math.sqrt(rss_sq_sum)

    # --- per-dim contributions ---
    for c in contribs:
        c.contrib_wc = abs(c.direction) * c.tol
        c.contrib_rss_sq = (c.direction * c.tol) ** 2
        c.pct_wc = (c.contrib_wc / total_tol_wc * 100.0) if total_tol_wc > 0 else 0.0
        c.pct_rss = (c.contrib_rss_sq / rss_sq_sum * 100.0) if rss_sq_sum > 0 else 0.0

    # Sort descending by worst-case contribution (sensitivity-first)
    contribs.sort(key=lambda c: c.contrib_wc, reverse=True)

    return StackupResult(
        nominal_gap=nominal_gap,
        total_tol_wc=total_tol_wc,
        total_tol_rss=total_tol_rss,
        gap_max_wc=nominal_gap + total_tol_wc,
        gap_min_wc=nominal_gap - total_tol_wc,
        gap_max_rss=nominal_gap + total_tol_rss,
        gap_min_rss=nominal_gap - total_tol_rss,
        contributors=contribs,
        assumed_dims=assumed_dims,
        disclaimer=DISCLAIMER,
    )

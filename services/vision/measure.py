"""``measure_from_photo`` — the one public metrology entry point.

Given a photo, a fitted :class:`~services.vision.fiducial.Detection`, and the two pixel endpoints
a VLM picked, compute the **in-plane** distance in millimetres and score it. The score is
*intrinsic to the optics*, by design:

* marker pixel resolution (``px_per_mm``) sets the achievable tier — you cannot measure finer
  than the fiducial resolves;
* out-of-plane tilt (marker-area CoV) caps the tier at ``low`` and flips ``in_plane`` false,
  because a foreshortened dimension is a guess no matter how many pixels it spans.

The result is handed to the intake ledger as a *claim* (:meth:`MeasureResult.to_claim`) carrying
``provenance.method = "photo_fiducial"`` and this tier. It never writes geometry: the intake layer
sees the ``photo_fiducial`` provenance and applies its own §5 policy — a photo dim feeding a
tolerance-sensitive fit is downgraded one step and flagged (never promoted to a typed
``measured``), and an out-of-plane / low-confidence read stays demanded until a caliper closes it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from .calib_sheet import DEFAULT_BOARD, BoardSpec
from .fiducial import detect_board, load_image, require_cv2

#: UI honesty string. Shown wherever a photo dimension is.
#: The intake layer then applies its own policy on top: a photo dim feeding a tolerance-sensitive
#: fit is downgraded one step and flagged (never treated as a typed ``measured`` value), and any
#: out-of-plane / low-confidence read stays demanded.
HONESTY_STRING: str = (
    "±2–5% for in-plane dimensions with the marker flat on the measurement plane "
    "and large in frame; out-of-plane dimensions are flagged low-confidence."
)

# Confidence thresholds (px per mm). Below ~4 px/mm a millimetre is a handful of pixels; above
# ~8 px/mm (≈200 dpi at the part) sub-mm reads are honest.
_PX_PER_MM_HIGH = 8.0
_PX_PER_MM_MEDIUM = 4.0
#: Above this marker-area coefficient of variation the board is judged out of plane.
_PLANARITY_COV_LIMIT = 0.25

# In-plane relative uncertainty by tier (the ±2–5% honesty band; low/out-of-plane widened).
_TIER_REL_UNCERT = {"high": 0.02, "medium": 0.05, "low": 0.10}


@dataclass
class MeasureResult:
    """Outcome of one photo measurement. ``ok`` false ⇒ ``reason``/``hint`` are set."""

    ok: bool
    value_mm: Optional[float] = None
    tier: str = "low"
    uncert_mm: Optional[float] = None
    in_plane: bool = True
    px_per_mm: float = 0.0
    n_markers: int = 0
    n_corners: int = 0
    planarity_cov: float = 0.0
    honesty: str = HONESTY_STRING
    provenance: dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    hint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "value_mm": self.value_mm,
            "tier": self.tier,
            "uncert_mm": self.uncert_mm,
            "in_plane": self.in_plane,
            "px_per_mm": round(self.px_per_mm, 3),
            "n_markers": self.n_markers,
            "n_corners": self.n_corners,
            "planarity_cov": round(self.planarity_cov, 4),
            "honesty": self.honesty,
            "provenance": copy.deepcopy(self.provenance),
            "reason": self.reason,
            "hint": self.hint,
        }

    def to_claim(self, feature: str, dim: str, *, fit: Optional[str] = None) -> dict[str, Any]:
        """Shape this result as an intake claim carrying the fiducial tier + provenance.

        The claim enters the ledger at the tier the optics earned (never ``measured`` — only a
        typed caliper reaches that). ``provenance.method="photo_fiducial"`` lets ``compute_demands``
        treat it as a photo dim: tolerance-sensitive fits are downgraded-and-flagged, and any dim
        still below its (possibly downgraded) required tier keeps demanding a typed value.
        """
        claim: dict[str, Any] = {
            "feature": feature,
            "dim": dim,
            "value_mm": self.value_mm,
            "scale_source": "card",          # a printed fiducial IS a calibrated card reference
            "in_plane": self.in_plane,
            "agrees_across_views": False,
            "edge_quality": "sharp",
            "uncert_mm": self.uncert_mm,
            "tier": self.tier,               # authoritative: set by marker pixel-area, not re-scored
            "confidence": self.tier,
            "provenance": copy.deepcopy(self.provenance),
        }
        if fit:
            claim["fit"] = fit
        return claim


def _score(px_per_mm: float, planarity_cov: float) -> tuple[str, bool]:
    """Map optics → (tier, in_plane). Out-of-plane caps the tier at ``low``."""
    in_plane = planarity_cov <= _PLANARITY_COV_LIMIT
    if not in_plane:
        return "low", False
    if px_per_mm >= _PX_PER_MM_HIGH:
        return "high", True
    if px_per_mm >= _PX_PER_MM_MEDIUM:
        return "medium", True
    return "low", True


def measure_from_photo(image_path: str, px_a, px_b,
                       spec: BoardSpec = DEFAULT_BOARD) -> MeasureResult:
    """Measure the in-plane distance between two pixel points against a ChArUco fiducial.

    Raises :class:`~services.vision.fiducial.VisionUnavailable` if OpenCV is absent (the MCP
    adapter turns that into ``{ok: False, reason, hint}``). A missing/unreadable file or an
    undetected board returns ``MeasureResult(ok=False, ...)`` rather than raising.
    """
    require_cv2()  # VisionUnavailable propagates to the adapter's contract-shaped degradation
    try:
        image = load_image(image_path)
    except OSError as e:  # noqa: BLE001
        return MeasureResult(ok=False, reason=str(e),
                             hint="pass a readable image path (png/jpg)")

    det = detect_board(image, spec)
    if det is None:
        return MeasureResult(
            ok=False,
            reason="no ChArUco fiducial detected in the photo",
            hint="lay the printed calibration sheet flat in the same plane as the feature, "
                 "fully and squarely in frame, then retake the photo",
        )

    a_mm, b_mm = det.px_to_mm(px_a, px_b)
    value_mm = ((a_mm[0] - b_mm[0]) ** 2 + (a_mm[1] - b_mm[1]) ** 2) ** 0.5
    tier, in_plane = _score(det.px_per_mm, det.planarity_cov)

    rel = _TIER_REL_UNCERT[tier]
    px_floor = (2.0 / det.px_per_mm) if det.px_per_mm > 0 else value_mm * rel
    uncert_mm = max(rel * value_mm, px_floor)

    provenance = {
        "method": "photo_fiducial",
        "px_a": [float(px_a[0]), float(px_a[1])],
        "px_b": [float(px_b[0]), float(px_b[1])],
        "px_per_mm": round(det.px_per_mm, 3),
        "planarity_cov": round(det.planarity_cov, 4),
        "n_markers": det.n_markers,
        "n_corners": det.n_corners,
        "in_plane": in_plane,
        "board": spec.to_dict(),
    }
    return MeasureResult(
        ok=True,
        value_mm=round(value_mm, 3),
        tier=tier,
        uncert_mm=round(uncert_mm, 3),
        in_plane=in_plane,
        px_per_mm=det.px_per_mm,
        n_markers=det.n_markers,
        n_corners=det.n_corners,
        planarity_cov=det.planarity_cov,
        provenance=provenance,
    )

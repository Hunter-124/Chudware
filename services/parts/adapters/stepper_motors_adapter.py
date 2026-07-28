"""CatalogProvider adapter for NEMA-frame stepper motors (motion/power-transmission
coverage gap: corpus token counts for "stepper"/"nema" were 0 before this adapter).

Generated programmatically from the published NEMA stepper mounting-frame table
(faceplate size in tenths of an inch — "NEMA 17" = 1.7 in = 42.3 mm square) crossed
with the standard commercial body-length options and the two universal step angles
(1.8 deg / 200 steps-per-rev "full step", 0.9 deg / 400 steps-per-rev "half step").
Domain: "motor".

PROVENANCE / HONESTY: frame square, bolt-circle/hole-spacing and shaft diameter are
published NEMA/RepRap-style mounting dimensions used industry-wide (Oriental Motor,
StepperOnline, etc. all mount to the same faceplate). Holding torque is emitted as a
RANGE, not a single fabricated-precision figure — a per-length interpolation across
each frame's honest low/high torque band, so no record claims a specific vendor SKU's
exact spec. ALWAYS verify against the target motor's real datasheet before release.
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "motor"

# (nema_no, body_square_mm, bolt_hole_spacing_mm, shaft_dia_mm, mount_screw,
#  [body_lengths_mm...], torque_low_nm, torque_high_nm)
#
# torque_low_nm / torque_high_nm bound the honest family range: shortest common body
# length sits near the low end, the longest common body length sits near the high end.
_NEMA_FRAMES: list[tuple] = [
    (8,  20.0, 15.4, 4.00, "M2", [20, 25, 30],            0.004, 0.011),
    (11, 28.0, 23.0, 5.00, "M3", [19, 32, 51],             0.06,  0.30),
    (14, 35.0, 26.0, 5.00, "M3", [26, 34, 36, 40],         0.05,  0.30),
    (17, 42.3, 31.0, 5.00, "M3", [20, 26, 34, 39, 40, 48, 60], 0.13, 0.65),
    (23, 57.0, 47.14, 6.35, "M5", [41, 56, 76, 113],       0.50,  3.00),
    (34, 86.0, 69.6, 12.70, "M6", [65, 98, 118, 156],      3.00, 12.00),
]

# Universal stepper step angles (present on virtually every NEMA-frame motor family).
_STEP_ANGLES = [
    (1.8, 200, "standard"),
    (0.9, 400, "high-res half-step"),
]


def _torque_band(length_mm: float, lengths: list, low: float, high: float) -> tuple[float, float]:
    """Interpolate an honest +/-15% torque range for one body length within a frame."""
    lo_len, hi_len = min(lengths), max(lengths)
    frac = 0.5 if hi_len == lo_len else (length_mm - lo_len) / (hi_len - lo_len)
    frac = max(0.0, min(1.0, frac))
    point = low + frac * (high - low)
    lo = round(point * 0.85, 3)
    hi = round(point * 1.15, 3)
    return (lo, hi)


def _round_sig(x: float, digits: int = 3) -> float:
    return round(x, digits)


def _make_record(nema_no: int, body_mm: float, bolt_spacing_mm: float,
                  shaft_mm: float, mount_screw: str, length_mm: int,
                  lengths: list, torque_low: float, torque_high: float,
                  step_deg: float, steps_per_rev: int, step_label: str) -> dict:
    t_lo, t_hi = _torque_band(length_mm, lengths, torque_low, torque_high)
    angle_tag = "1p8deg" if step_deg == 1.8 else "0p9deg"
    designation = f"NEMA{nema_no}-{length_mm}mm-{angle_tag}"
    part_id = f"{_DOMAIN}:stepper:{designation}"

    hs_form = f"{nema_no}HS{length_mm:02d}"  # e.g. 17HS40-style form-factor hint
    title = (
        f"NEMA {nema_no} stepper motor {body_mm:g}x{body_mm:g}x{length_mm}mm "
        f"{step_deg:g} deg step bipolar"
    )
    text = (
        f"nema{nema_no} nema {nema_no} stepper motor stepper {hs_form} "
        f"bipolar 2-phase step motor {length_mm}mm body {step_deg:g} degree "
        f"{steps_per_rev} steps/rev {step_label}"
    )

    params = {
        "nema_size": nema_no,
        "body_square_mm": body_mm,
        "bolt_hole_spacing_mm": bolt_spacing_mm,
        "mount_screw": mount_screw,
        "shaft_dia_mm": shaft_mm,
        "body_length_mm": length_mm,
        "step_angle_deg": step_deg,
        "steps_per_rev": steps_per_rev,
        "holding_torque_nm_min": t_lo,
        "holding_torque_nm_max": t_hi,
        "phases": 2,
    }

    return {
        "part_id":      part_id,
        "domain":       _DOMAIN,
        "title":        f"{title} {text}",
        "standard":     f"NEMA{nema_no}",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind": "parametric_stepper_motor",
            "spec": {
                "nema_size": nema_no,
                "body_square_mm": body_mm,
                "bolt_hole_spacing_mm": bolt_spacing_mm,
                "shaft_dia_mm": shaft_mm,
                "body_length_mm": length_mm,
            },
        },
        "sourcing":     [],
        "provenance":   (
            f"NEMA {nema_no} mounting faceplate/shaft dimensions follow the "
            "industry-standard stepper mounting pattern shared across vendor "
            "datasheets (Oriental Motor, StepperOnline, etc.). Holding torque is a "
            "representative +/-15% band interpolated across the frame's honest "
            "low/high family range for this body length — not a specific vendor "
            "SKU spec. Verify against the target motor's datasheet before release."
        ),
        "license":      "curated-nominal",
    }


class StepperMotorsAdapter:
    """CatalogProvider for NEMA-frame stepper motors (domain "motor")."""

    domain: str = _DOMAIN
    provider_id: str = "stepper_motors"

    def search(self, query: str, *, domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != _DOMAIN:
            return []
        norm = (query or "").strip().lower()
        recs = list(self.iter_records())
        if not norm:
            return recs
        return [r for r in recs
                if norm in r["title"].lower() or norm in r["designation"].lower()]

    def get(self, part_id: str) -> dict | None:
        for rec in self.iter_records():
            if rec["part_id"] == part_id:
                return rec
        return None

    def count(self) -> int:
        return sum(len(lengths) * len(_STEP_ANGLES) for (_, _, _, _, _, lengths, _, _) in _NEMA_FRAMES)

    def iter_records(self) -> Iterator[dict]:
        for (nema_no, body_mm, bolt_spacing_mm, shaft_mm, mount_screw,
             lengths, torque_low, torque_high) in _NEMA_FRAMES:
            for length_mm in lengths:
                for step_deg, steps_per_rev, step_label in _STEP_ANGLES:
                    yield _make_record(
                        nema_no, body_mm, bolt_spacing_mm, shaft_mm, mount_screw,
                        length_mm, lengths, torque_low, torque_high,
                        step_deg, steps_per_rev, step_label,
                    )

"""Molded-part draft-angle helper.

Contract §8 of ``contracts/design_pipeline.md`` names molded draft as a geometry
kind with **no existing trigger** — a "helper to add" for T-DP3. This is that
helper: a real, cited calculator that (a) returns the recommended draft angle for a
molded wall and (b) computes the resulting dimensional taper over a draw depth, so
the geometry_designer sizes a draft rather than recalling "1 degree".

Draft is mandatory for injection-molded parts so the solidified part releases from
the tool. The per-side wall offset a draft angle produces over a draw depth is:

    offset_per_side = depth · tan(α)

The recommended angle rises with texture (a textured surface needs ~1° extra per
0.025 mm of texture depth) and with draw depth.  The baseline angles come from the
process DFM table (``engine_oracle/dfm/process_profiles.py`` → ``injection`` profile,
``draft.min_deg``) and the standard molding references.

Source kind: heuristic (draft angles are DFM guidelines); the taper geometry is exact.
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register

# Baseline recommended draft by surface finish.
# Ref: Bryce, "Plastic Injection Molding"; the process_profiles ``injection`` profile
# fixes the mandatory minimum at 1.0° for a smooth wall.
_DRAFT_BY_FINISH_DEG: dict[str, float] = {
    "smooth":   1.0,   # SPI A/B polished — matches process_profiles injection min_deg
    "matte":    1.5,   # SPI C
    "light_texture": 3.0,   # light MoldTech texture
    "heavy_texture": 5.0,   # deep texture / leather grain
    "default":  1.0,
}


def draft_check(
    draw_depth_mm: float,
    finish: str = "smooth",
    draft_deg: Optional[float] = None,
    prefilled: Optional[dict] = None,
) -> dict:
    """Recommended draft angle + the dimensional taper it produces.

    Parameters
    ----------
    draw_depth_mm : depth of the molded wall in the draw direction [mm].
    finish :        ``smooth`` | ``matte`` | ``light_texture`` | ``heavy_texture``.
    draft_deg :     override the recommended angle (to size an as-drawn taper).
    prefilled :     §7 provenance dict.

    Returns
    -------
    ``aicad.calc/1`` envelope with ``answer.draft_deg`` and
    ``answer.offset_per_side_mm`` (the wall taper over the draw depth).
    """
    depth = float(draw_depth_mm)
    if depth <= 0:
        return calc_fail("bad_query", "draw_depth_mm must be > 0.")

    fin = (finish or "default").strip().lower()
    if draft_deg is None:
        alpha = _DRAFT_BY_FINISH_DEG.get(fin, _DRAFT_BY_FINISH_DEG["default"])
        angle_note = f"{fin} finish → {alpha}° recommended draft"
    else:
        alpha = float(draft_deg)
        if not (0.0 < alpha < 45.0):
            return calc_fail("bad_query", "draft_deg must be in (0, 45).")
        angle_note = f"{alpha}° (user-supplied)"

    offset = depth * math.tan(math.radians(alpha))

    answer = {
        "draft_deg": round(alpha, 2),
        "offset_per_side_mm": round(offset, 3),
        "draw_depth_mm": depth,
        "finish": fin,
        "note": (
            f"{angle_note}; taper per side = depth·tan(α) over {depth:g} mm draw. "
            f"Draft is mandatory for injection release."
        ),
    }
    return calc_envelope(
        calc="draft_check",
        answer=answer,
        units="mm",
        equation="offset_per_side = draw_depth · tan(draft_angle)",
        inputs={
            "draw_depth_mm": depth,
            "finish": fin,
            **({"draft_deg": draft_deg} if draft_deg is not None else {}),
        },
        source_kind="heuristic",
        citation=(
            "Bryce, Plastic Injection Molding Vol. 2 (draft guidelines); "
            "engine_oracle/dfm/process_profiles.py injection profile (min_deg=1.0)."
        ),
        prefilled=prefilled,
    )


register(
    "draft_check",
    draft_check,
    input_schema={
        "draw_depth_mm": {"type": "float", "unit": "mm",
                          "description": "Molded wall depth in the draw direction [mm]",
                          "family": "length"},
        "finish":        {"type": "string", "unit": None,
                          "description": "smooth | matte | light_texture | heavy_texture",
                          "family": "string"},
        "draft_deg":     {"type": "float", "unit": "deg",
                          "description": "Override draft angle (auto by finish if omitted)",
                          "family": "dimensionless"},
    },
    prefill_hints={
        "draw_depth_mm": "bbox_z",
    },
)

"""``thread_spec(designation)`` — screw-thread dimensions **computed** from ISO 724
(metric M profile) and ASME B1.1 (Unified inch), never fetched. Contract
``contracts/research_tools.md`` §1.

Pillar 8: standard thread geometry is a closed-form function of nominal diameter and
pitch (both 60-degree profiles), so code computes it deterministically —
``confidence:"bundled"``, ``source_url:null``, the standard cited in ``quote``. No table
is scraped; only a small **coarse-pitch lookup** (so a bare ``M8`` resolves) is bundled,
itself first-party from the ISO 261 / ASME coarse-series tables.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from .common import BUNDLED, envelope, fail

_SQRT3 = math.sqrt(3.0)

# ISO 261 coarse-pitch series (mm) — bundled first-party so a bare "M8" resolves.
_METRIC_COARSE = {
    1.6: 0.35, 2.0: 0.40, 2.5: 0.45, 3.0: 0.50, 4.0: 0.70, 5.0: 0.80, 6.0: 1.00,
    8.0: 1.25, 10.0: 1.50, 12.0: 1.75, 14.0: 2.00, 16.0: 2.00, 20.0: 2.50, 24.0: 3.00,
    30.0: 3.50, 36.0: 4.00,
}
# Unified coarse (UNC) TPI by nominal major diameter (in). #n sizes -> 0.060 + n*0.013.
_UNC_TPI = {
    0.0730: 64, 0.0860: 56, 0.0990: 48, 0.1120: 40, 0.1250: 40, 0.1380: 32,
    0.1640: 32, 0.1900: 24, 0.2500: 20, 0.3125: 18, 0.3750: 16, 0.4375: 14,
    0.5000: 13, 0.6250: 11, 0.7500: 10, 0.8750: 9, 1.0000: 8,
}
_UNF_TPI = {
    0.1250: 44, 0.1380: 40, 0.1640: 36, 0.1900: 32, 0.2500: 28, 0.3125: 24,
    0.3750: 24, 0.4375: 20, 0.5000: 20, 0.6250: 18, 0.7500: 16, 1.0000: 12,
}
# Numbered machine-screw sizes -> major diameter (in): D = 0.060 + 0.013*number.
_NUM_SIZE_IN = {n: round(0.060 + 0.013 * n, 4) for n in range(0, 13)}


def _metric_spec(major_mm: float, pitch_mm: float) -> dict:
    """ISO 724 basic dimensions for an M-profile thread (all mm / mm^2)."""
    H = _SQRT3 / 2.0 * pitch_mm                       # fundamental triangle height
    d2 = major_mm - 0.6495 * pitch_mm                 # pitch diameter (= D - 0.75H)
    d3 = major_mm - 1.22687 * pitch_mm                # external minor diameter (rounded root)
    d1 = major_mm - 1.08253 * pitch_mm                # internal basic minor diameter
    As = math.pi / 4.0 * ((d2 + d3) / 2.0) ** 2       # ISO 898 tensile stress area
    return {"major_mm": round(major_mm, 4), "pitch_mm": round(pitch_mm, 4),
            "pitch_diameter_mm": round(d2, 4), "minor_diameter_mm": round(d3, 4),
            "minor_diameter_internal_mm": round(d1, 4),
            "tensile_stress_area_mm2": round(As, 4), "thread_height_mm": round(H, 4),
            "thread_angle_deg": 60}


def _inch_spec(major_in: float, tpi: float) -> dict:
    """ASME B1.1 Unified-thread dimensions (inch / inch^2, plus SI mirror)."""
    P = 1.0 / tpi
    d2 = major_in - 0.6495 * P                        # pitch diameter
    d3 = major_in - 1.22687 * P                       # external minor diameter
    At = math.pi / 4.0 * (major_in - 0.9743 / tpi) ** 2   # UN tensile stress area (in^2)
    return {"major_in": round(major_in, 4), "tpi": tpi,
            "pitch_diameter_in": round(d2, 4), "minor_diameter_in": round(d3, 4),
            "tensile_stress_area_in2": round(At, 5),
            "major_mm": round(major_in * 25.4, 4),
            "tensile_stress_area_mm2": round(At * 645.16, 3),
            "thread_angle_deg": 60}


_METRIC_RE = re.compile(r"^m\s*([0-9]+(?:\.[0-9]+)?)\s*(?:[x×]\s*([0-9]+(?:\.[0-9]+)?))?$")
_INCH_RE = re.compile(r"^(#?\d+(?:\.\d+)?|\d+/\d+)\s*-\s*(\d+)\s*(unc|unf|un)?$")
_INCH_BARE_RE = re.compile(r"^(#\d+|\d+/\d+)\s*(unc|unf)?$")


def _parse_inch_size(tok: str) -> Optional[float]:
    tok = tok.strip().lower()
    if tok.startswith("#"):
        try:
            return _NUM_SIZE_IN.get(int(tok[1:]))
        except ValueError:
            return None
    if "/" in tok:
        a, b = tok.split("/")
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(tok)
    except ValueError:
        return None


def _nearest(d: dict, key: float, tol: float = 0.02):
    for k in d:
        if abs(k - key) <= tol:
            return d[k]
    return None


def thread_spec(designation: str) -> dict:
    """Compute thread dimensions for a metric (``"M8"``, ``"M8x1.25"``) or Unified-inch
    (``"1/4-20"``, ``"#10-24 UNC"``, ``"1/4 UNF"``) designation. Returns the §1 envelope
    with a ``spec`` dict (pitch/minor diameters, tensile stress area, ...)."""
    if not designation or not str(designation).strip():
        return fail("bad_query", "Provide a thread designation, e.g. 'M8x1.25' or '1/4-20 UNC'.")
    raw = str(designation).strip()
    s = raw.lower().replace(" ", "")

    m = _METRIC_RE.match(s)
    if m:
        major = float(m.group(1))
        pitch = float(m.group(2)) if m.group(2) else _nearest(_METRIC_COARSE, major)
        if pitch is None:
            return fail("not_found",
                        f"No coarse pitch bundled for M{major:g}; give an explicit pitch, e.g. M{major:g}x1.5.")
        spec = _metric_spec(major, pitch)
        ans = (f"{raw}: pitch dia {spec['pitch_diameter_mm']} mm, minor dia "
               f"{spec['minor_diameter_mm']} mm, tensile stress area "
               f"{spec['tensile_stress_area_mm2']} mm^2 (ISO 724)")
        return envelope(ans, units="mm", source_url=None, quote="ISO 724 basic M-profile dimensions",
                        confidence=BUNDLED, designation=raw, series="metric", spec=spec)

    mi = _INCH_RE.match(s)
    tpi = None
    major_in = None
    series = "unified"
    if mi:
        major_in = _parse_inch_size(mi.group(1))
        tpi = float(mi.group(2))
    else:
        mb = _INCH_BARE_RE.match(s)
        if mb:
            major_in = _parse_inch_size(mb.group(1))
            table = _UNF_TPI if mb.group(2) == "unf" else _UNC_TPI
            if major_in is not None:
                tpi = _nearest(table, major_in, tol=0.0015)
    if major_in is not None and tpi:
        spec = _inch_spec(major_in, tpi)
        ans = (f"{raw}: pitch dia {spec['pitch_diameter_in']} in, minor dia "
               f"{spec['minor_diameter_in']} in, tensile stress area "
               f"{spec['tensile_stress_area_in2']} in^2 (ASME B1.1)")
        return envelope(ans, units="in", source_url=None, quote="ASME B1.1 Unified-thread dimensions",
                        confidence=BUNDLED, designation=raw, series=series, spec=spec)

    return fail("bad_query",
                f"Could not parse thread designation {raw!r}. Use 'M8x1.25', '1/4-20 UNC', or '#10-24'.")

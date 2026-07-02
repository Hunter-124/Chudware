"""``standard_section(designation)`` — structural section properties for AISC W-shapes and
ASME B36.10M pipe schedules. Contract ``contracts/research_tools.md`` §1.

Pillar 8: AISC shape properties are transcribed from the **free official AISC Shapes
Database** and pipe geometry from **ASME B36.10M** as a first-party fact table (public
dimensional standards) — bundled, ``confidence:"bundled"``, no scraping. Pipe section
properties (area, moment of inertia) are *computed* from the standardized OD/wall so the
math is auditable rather than copied.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from .common import BUNDLED, envelope, fail

# AISC W-shapes (imperial), nominal from the AISC Shapes Database v15:
# A[in^2], d[in], Ix[in^4], Sx[in^3], Iy[in^4], Sy[in^3].
_W_SHAPES = {
    "W8X31": {"A_in2": 9.13, "d_in": 8.00, "Ix_in4": 110.0, "Sx_in3": 27.5, "Iy_in4": 37.1, "Sy_in3": 9.27},
    "W8X18": {"A_in2": 5.26, "d_in": 8.14, "Ix_in4": 61.9, "Sx_in3": 15.2, "Iy_in4": 7.97, "Sy_in3": 3.04},
    "W10X22": {"A_in2": 6.49, "d_in": 10.2, "Ix_in4": 118.0, "Sx_in3": 23.2, "Iy_in4": 11.4, "Sy_in3": 3.35},
    "W10X33": {"A_in2": 9.71, "d_in": 9.73, "Ix_in4": 171.0, "Sx_in3": 35.0, "Iy_in4": 36.6, "Sy_in3": 9.20},
    "W12X26": {"A_in2": 7.65, "d_in": 12.2, "Ix_in4": 204.0, "Sx_in3": 33.4, "Iy_in4": 17.3, "Sy_in3": 5.34},
    "W12X40": {"A_in2": 11.7, "d_in": 11.9, "Ix_in4": 307.0, "Sx_in3": 51.5, "Iy_in4": 44.1, "Sy_in3": 11.0},
    "W14X30": {"A_in2": 8.85, "d_in": 13.8, "Ix_in4": 291.0, "Sx_in3": 42.0, "Iy_in4": 19.6, "Sy_in3": 5.82},
    "W16X40": {"A_in2": 11.8, "d_in": 16.0, "Ix_in4": 518.0, "Sx_in3": 64.7, "Iy_in4": 28.9, "Sy_in3": 8.25},
}
# ASME B36.10M pipe: NPS -> {OD_in, schedules{sched: wall_in}}.
_PIPE = {
    "1": {"OD_in": 1.315, "sch": {"40": 0.133, "80": 0.179}},
    "1.5": {"OD_in": 1.900, "sch": {"40": 0.145, "80": 0.200}},
    "2": {"OD_in": 2.375, "sch": {"40": 0.154, "80": 0.218}},
    "3": {"OD_in": 3.500, "sch": {"40": 0.216, "80": 0.300}},
    "4": {"OD_in": 4.500, "sch": {"40": 0.237, "80": 0.337}},
    "6": {"OD_in": 6.625, "sch": {"40": 0.280, "80": 0.432}},
}

_PIPE_RE = re.compile(r"^(?:pipe|nps)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:in)?\s*(?:sch(?:edule)?)?\s*(\d+)?$")


def _pipe_props(od: float, wall: float) -> dict:
    idia = od - 2.0 * wall
    area = math.pi / 4.0 * (od ** 2 - idia ** 2)
    I = math.pi / 64.0 * (od ** 4 - idia ** 4)
    S = I / (od / 2.0)
    return {"OD_in": round(od, 4), "wall_in": round(wall, 4), "ID_in": round(idia, 4),
            "area_in2": round(area, 4), "I_in4": round(I, 5), "S_in3": round(S, 5),
            "OD_mm": round(od * 25.4, 3)}


def standard_section(designation: str) -> dict:
    """Look up an AISC W-shape (``"W8x31"``) or an ASME pipe schedule
    (``"pipe 2 sch 40"``, ``"NPS 2 SCH40"``). Returns the §1 envelope with a ``section``
    dict of the property set."""
    if not designation or not str(designation).strip():
        return fail("bad_query", "Provide a section, e.g. 'W8x31' or 'pipe 2 sch 40'.")
    raw = str(designation).strip()
    key = raw.upper().replace(" ", "").replace("×", "X")

    if key in _W_SHAPES:
        sec = dict(_W_SHAPES[key])
        ans = (f"{key}: area {sec['A_in2']} in^2, depth {sec['d_in']} in, "
               f"Ix {sec['Ix_in4']} in^4, Sx {sec['Sx_in3']} in^3 (AISC)")
        return envelope(ans, units="in", source_url=None, quote="AISC Shapes Database (official, free)",
                        confidence=BUNDLED, designation=key, kind="aisc_w_shape", section=sec)

    m = _PIPE_RE.match(raw.lower())
    if m and m.group(1) in _PIPE:
        nps = m.group(1)
        sched = m.group(2) or "40"
        rec = _PIPE[nps]
        wall = rec["sch"].get(sched)
        if wall is None:
            return fail("not_found",
                        f"NPS {nps} has no bundled schedule {sched}; available: {sorted(rec['sch'])}.")
        sec = _pipe_props(rec["OD_in"], wall)
        sec.update({"nps": nps, "schedule": sched})
        ans = (f"NPS {nps} Sch {sched}: OD {sec['OD_in']} in, wall {sec['wall_in']} in, "
               f"area {sec['area_in2']} in^2, I {sec['I_in4']} in^4 (ASME B36.10M)")
        return envelope(ans, units="in", source_url=None, quote="ASME B36.10M welded/seamless wrought steel pipe",
                        confidence=BUNDLED, designation=f"NPS{nps}-Sch{sched}", kind="pipe", section=sec)

    return fail("not_found",
                f"Section {raw!r} not in the bundled set. Bundled W-shapes: {sorted(_W_SHAPES)}; "
                f"pipe NPS: {sorted(_PIPE)}.")

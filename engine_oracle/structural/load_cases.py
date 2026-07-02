"""ASCE 7 load *combinations* for structural analysis (T-D2).

The frozen contract (``structural_schema.md`` §3) stores per-doc **load cases** tagged by
*kind* (``dead`` / ``live`` / ``wind`` / ``snow`` / ``combo``) each carrying its nominal,
unfactored magnitude. A :class:`LoadCombo` here layers ASCE 7 strength-design *factors* over
those kinds — e.g. ``1.2D + 1.6L``. :mod:`engine.structural.analysis` adds every doc load under
a PyNite load *case* named by its kind, then registers one PyNite load *combination* per combo
via :meth:`LoadCombo.factors`.

Kept deliberately separate from the doc-level ``LoadCase`` (a magnitude) so the two concepts —
"what load" vs. "how it is factored" — never collide. This module is a new, additive surface
(no contract change): the presets below are the ASCE 7-16 §2.3 basic strength combinations we
support today.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoadCombo:
    """A named factored combination of load *kinds*. Factors default to 0 (kind absent)."""

    name: str
    dead_factor: float = 0.0
    live_factor: float = 0.0
    wind_factor: float = 0.0
    snow_factor: float = 0.0

    def factors(self) -> dict[str, float]:
        """``kind -> factor`` for PyNite ``add_load_combo`` — only the nonzero kinds."""
        raw = {
            "dead": self.dead_factor,
            "live": self.live_factor,
            "wind": self.wind_factor,
            "snow": self.snow_factor,
        }
        return {k: float(v) for k, v in raw.items() if v}


# ASCE 7-16 §2.3.1 basic strength-design combinations (the subset the day-2 scaffold ships).
DEAD_ONLY = LoadCombo("DEAD_ONLY", dead_factor=1.4)
DEAD_LIVE = LoadCombo("DEAD_LIVE", dead_factor=1.2, live_factor=1.6)
DEAD_LIVE_WIND = LoadCombo("DEAD_LIVE_WIND", dead_factor=1.2, live_factor=1.6, wind_factor=0.5)

PRESETS: dict[str, LoadCombo] = {c.name: c for c in (DEAD_ONLY, DEAD_LIVE, DEAD_LIVE_WIND)}


def get_combo(name: str) -> LoadCombo:
    """Look up a preset combo by name (case-insensitive). Raises ``KeyError`` if unknown."""
    key = (name or "").strip().upper()
    if key not in PRESETS:
        raise KeyError(f"unknown load combo {name!r}; known: {sorted(PRESETS)}")
    return PRESETS[key]

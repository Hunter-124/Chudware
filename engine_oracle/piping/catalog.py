"""Pipe + fitting dimensional catalog for :mod:`engine.piping`.

Two data sources:

* **Pipe wall/OD** — :func:`nearest_pipe` wraps ``fluids.piping.nearest_pipe`` (MIT), which
  carries ASME B36.10M/B36.19M schedule tables. `fluids` is an optional runtime dependency:
  when it is not importable every lookup degrades **contract-shaped**
  (``{"ok": False, "reason": ..., "hint": ...}``) rather than raising — so a box without
  `fluids` provisioned still imports the module and fails softly.
* **Fitting geometry** — B16.9 buttweld + B16.5 flange dimensions from a bundled CSV
  (``data/fittings.csv``). Dimensions are facts (not copyrightable); ASME text is not bundled.

All dimensions returned by this module are in **mm** (fluids' native metres are converted
here so the rest of engine.piping never touches metres).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_FITTINGS_CSV = os.path.join(_DATA_DIR, "fittings.csv")

# Schedule aliases fluids does not key directly -> a concrete schedule it does.
_SCHEDULE_ALIAS = {"STD": "40", "XS": "80"}


def _err(reason: str, hint: str | None = None) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    return out


def have_fluids() -> bool:
    try:
        import fluids.piping  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class PipeSpec:
    """Resolved pipe cross-section, all in mm."""
    nps: float          # nominal size actually matched (inches)
    outer_dia: float    # OD, mm
    inner_dia: float    # ID / bore, mm
    wall: float         # wall thickness, mm
    schedule: str

    def as_dict(self) -> dict:
        return asdict(self)


def nearest_pipe(nps: float | None = None, schedule: str = "40",
                 outer_dia: float | None = None) -> PipeSpec | dict:
    """Resolve ``(nps, schedule)`` (or an ``outer_dia`` in mm) to a :class:`PipeSpec`.

    Returns a :class:`PipeSpec` on success, or a contract-shaped ``{"ok": False, ...}`` when
    ``fluids`` is unavailable or the size/schedule cannot be resolved. Never raises for a
    bad size — that is a soft, reportable condition (see contract §1).
    """
    try:
        from fluids.piping import nearest_pipe as _np
    except Exception:  # noqa: BLE001
        return _err("fluids library not installed",
                    hint="pip install fluids (MIT) to enable pipe schedule lookup")

    sched = _SCHEDULE_ALIAS.get(str(schedule).upper(), str(schedule))
    kwargs: dict[str, Any] = {"schedule": sched}
    if outer_dia is not None:
        kwargs["Do"] = float(outer_dia) / 1000.0  # mm -> m
    elif nps is not None:
        kwargs["NPS"] = nps
    else:
        return _err("nearest_pipe requires nps or outer_dia")

    try:
        matched_nps, di_m, do_m, wall_m = _np(**kwargs)
    except Exception as exc:  # noqa: BLE001  (fluids raises ValueError for unknown size/sched)
        return _err(f"no catalog pipe for nps={nps!r} schedule={schedule!r}: {exc}",
                    hint="try a standard NPS (0.5,0.75,1,1.5,2,3,4,6,8,10,12) + schedule")

    return PipeSpec(
        nps=float(matched_nps),
        outer_dia=do_m * 1000.0,
        inner_dia=di_m * 1000.0,
        wall=wall_m * 1000.0,
        schedule=str(schedule),
    )


# ── fitting catalog (CSV) ───────────────────────────────────────────────────────

_NUM_COLS = {"nps", "nps2", "center_to_end", "length", "flange_class",
             "od", "thickness", "bolt_circle", "bolt_holes", "bolt_dia"}


@lru_cache(maxsize=1)
def _load_rows() -> tuple[dict, ...]:
    if not os.path.isfile(_FITTINGS_CSV):
        return ()
    rows: list[dict] = []
    with open(_FITTINGS_CSV, newline="") as fh:
        for raw in csv.DictReader(fh):
            row: dict[str, Any] = {}
            for k, v in raw.items():
                if k is None:
                    continue
                key = k.strip()
                val = (v or "").strip()
                if key in _NUM_COLS:
                    row[key] = float(val) if val != "" else None
                else:
                    row[key] = val or None
            rows.append(row)
    return tuple(rows)


def fitting_dims(ftype: str, nps: float, nps2: float | None = None,
                 flange_class: int | None = None) -> dict:
    """Look up bundled B16 fitting dimensions (mm) for ``(ftype, nps[, nps2/flange_class])``.

    Returns the matching CSV row as a dict on success, else a contract-shaped
    ``{"ok": False, "reason": ..., "hint": <nearest available nps>}``.
    """
    rows = [r for r in _load_rows() if r.get("type") == ftype]
    if not rows:
        return _err(f"no catalog rows for fitting type {ftype!r}",
                    hint="known types: elbow, tee, reducer, flange")

    # exact-nps candidates, filtered by the type's secondary key
    def _match(r) -> bool:
        if r.get("nps") != float(nps):
            return False
        if ftype == "reducer" and nps2 is not None:
            return r.get("nps2") == float(nps2)
        if ftype == "flange" and flange_class is not None:
            return int(r.get("flange_class") or 0) == int(flange_class)
        return True

    hit = next((r for r in rows if _match(r)), None)
    if hit is not None:
        return dict(hit)

    available = sorted({r.get("nps") for r in rows if r.get("nps") is not None})
    nearest = min(available, key=lambda a: abs(a - float(nps))) if available else None
    return _err(f"no {ftype} in catalog for nps={nps!r}"
                + (f" nps2={nps2!r}" if ftype == "reducer" else "")
                + (f" class={flange_class!r}" if ftype == "flange" else ""),
                hint=f"nearest available nps={nearest}" if nearest is not None else None)


def available_sizes(ftype: str) -> list[float]:
    """Sorted NPS list catalogued for ``ftype`` (empty if none / CSV missing)."""
    return sorted({r["nps"] for r in _load_rows()
                   if r.get("type") == ftype and r.get("nps") is not None})

"""``feeds_speeds(material, tool_type, tool_dia, operation, machine_limits?)`` and
``tap_drill(thread)`` — deterministic machining-data calculators.

Contract: ``contracts/advisor.md`` §7 (``aicad.calc/1`` envelope) and §3 (advisory item
shape for out-of-envelope warnings).

**Design**
----------
The data table (``services/research/data/feeds_speeds.json``) stores per-material ×
tool-type × operation ranges (Vc surface speed in SFM/m·min, chipload-per-tooth by
diameter bracket, stepdown/stepover fractions). The Python layer:

1. Resolves the material via ``engine_oracle/materials/registry.cam_material_id()`` so
   callers can pass any registry alias (``alu_6061``, ``aluminum``, ``6061-t6``, …).
2. Interpolates a conservative midpoint default (range mid) clamped to
   ``machine_limits``.
3. Returns an ``aicad.calc/1`` envelope that includes the full min/max envelope so the
   advisory engine can range-check caller-supplied values.
4. When explicit params fall outside the envelope, callers can use
   ``check_envelope(material, tool_type, tool_dia, operation, machine_limits, **params)``
   which returns an ``aicad.advisory/1`` item (``severity=hint``, candidate_fixes =
   clamped values) — contract §3.

**Relationship to C++ CuttingDB** (``core/cam/feeds.cpp``)
The C++ CuttingDB computes feeds for actual toolpath generation inside the kernel kernel.
This Python layer is the *advisory* calculator that range-checks and surfaces candidates.
The SFM/chipload values in ``feeds_speeds.json`` are anchored to the same Machinery's
Handbook ranges used to seed ``core/cam/feeds.cpp``; they are NOT meant to replace it.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_DATA_PATH = pathlib.Path(__file__).with_name("data") / "feeds_speeds.json"
_MACHINES_PATH = pathlib.Path(__file__).with_name("data") / "machines.json"

_SCHEMA_CALC = "aicad.calc/1"
_SCHEMA_ADV  = "aicad.advisory/1"
_SCHEMA_ADV_ITEM_CONFIDENCE = "table"  # source kind for advisory candidate_fixes

# Machine limits used when the caller provides none (a typical benchtop VMC).
_DEFAULT_MACHINE_LIMITS = {
    "max_rpm":   15000,
    "min_rpm":   100,
    "max_feed":  5000,   # mm/min
    "min_feed":  10,
}

_db: Optional[dict] = None

_machines_db: Optional[dict] = None


def _load_machines_db() -> dict:
    """Lazy-load services/research/data/machines.json (aicad.machines/1)."""
    global _machines_db
    if _machines_db is None:
        _machines_db = json.loads(_MACHINES_PATH.read_text(encoding="utf-8"))
    return _machines_db


def machine_profile(profile_id: str) -> dict:
    """Return one machine profile record from machines.json.

    Raises ``ValueError`` listing valid ids when ``profile_id`` is unknown.
    """
    pid = str(profile_id or "").strip()
    db = _load_machines_db()
    machines = db.get("machines") or []
    by_id = {str(m.get("id", "")): m for m in machines if isinstance(m, dict)}
    if pid not in by_id:
        valid = sorted(k for k in by_id if k)
        raise ValueError(
            f"unknown machine profile '{profile_id}'; valid ids: {valid}"
        )
    return dict(by_id[pid])


def machine_limits_from_profile(profile: dict) -> dict:
    """Map an aicad.machines/1 record onto feeds_speeds machine_limits keys."""
    out: dict[str, float] = {}
    if "max_rpm" in profile:
        out["max_rpm"] = float(profile["max_rpm"])
    if "min_rpm" in profile:
        out["min_rpm"] = float(profile["min_rpm"])
    if "max_feed_mm_min" in profile:
        out["max_feed"] = float(profile["max_feed_mm_min"])
    elif "max_feed" in profile:
        out["max_feed"] = float(profile["max_feed"])
    if "min_feed" in profile:
        out["min_feed"] = float(profile["min_feed"])
    return out



def _load_db() -> dict:
    global _db
    if _db is None:
        _db = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _db


def _material_row(mat_id: str) -> Optional[dict]:
    """Return the feeds_speeds.json material entry, trying registry alias resolution."""
    db = _load_db()
    mats = db.get("materials", {})

    # Try direct key first (may already be a sim/feeds ID).
    if mat_id in mats:
        return mats[mat_id]

    # Try registry alias -> cam_id mapping, then look up by cam_id.
    # engine_oracle/materials was ported to core (T-RF19); resolve via the C++ RPC.
    try:
        from services.mcp import core_bridge as _cb  # noqa: PLC0415
        resolved = _cb.call("material_resolve_key", {"name": mat_id}).get("result")
        if resolved and resolved in mats:
            return mats[resolved]
        # Fallback: look by cam_id field inside each material row.
        cam_id = _reg.cam_material_id(mat_id)
        if cam_id:
            for rec in mats.values():
                if rec.get("cam_id") == cam_id:
                    return rec
    except Exception:  # noqa: BLE001
        pass

    # Final: try matching by cam_id directly (e.g. "aluminum" -> alu_6061 row).
    for rec in mats.values():
        if rec.get("cam_id") == mat_id:
            return rec

    return None


def _clampd(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _dia_bracket(dia_mm: float) -> str:
    if dia_mm <= 6:
        return "<=6mm"
    elif dia_mm <= 12:
        return "<=12mm"
    elif dia_mm <= 25:
        return "<=25mm"
    else:
        return ">25mm"


def _midpoint(lo: float, hi: float) -> float:
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# aicad.calc/1 envelope builder
# ---------------------------------------------------------------------------

def _calc_envelope(
    calc: str,
    answer: Any,
    *,
    units: str,
    equation: str,
    inputs: dict,
    source_kind: str = "table",
    citation: str,
    notes: list[str] | None = None,
) -> dict:
    """Build an aicad.calc/1 envelope (contracts/advisor.md §7)."""
    return {
        "schema": _SCHEMA_CALC,
        "calc": calc,
        "answer": answer,
        "units": units,
        "equation": equation,
        "inputs": inputs,
        "source": {"kind": source_kind, "citation": citation},
        "notes": notes or [],
    }


# ---------------------------------------------------------------------------
# feeds_speeds — public API
# ---------------------------------------------------------------------------

def feeds_speeds(
    material: str,
    tool_type: str,
    tool_dia: float,
    operation: str,
    machine_limits: Optional[dict] = None,
    thread: str = "",
    machine: str = "",
) -> dict:
    """Compute rpm / feed / stepdown / stepover envelope for a material + tool + operation.

    Returns an ``aicad.calc/1`` envelope (contracts/advisor.md §7).  ``answer`` carries:

    - ``rpm``: recommended spindle speed (min, mid, max) in RPM, clamped to machine limits
    - ``feed``: cutting feed rate (min, mid, max) mm/min
    - ``stepdown``: axial depth of cut (min, mid, max) mm for given ``tool_dia``
    - ``stepover``: radial width of cut (min, mid, max) mm for given ``tool_dia``
    - ``vc_sfm``: surface speed range (SFM)
    - ``vc_mmin``: surface speed range (m/min)
    - ``chipload``: feed-per-tooth range (mm/tooth) for the diameter bracket
    - ``coolant``: advisory coolant note string
    - ``provenance``: notes on which table row was used
    - ``tap_drill_mm``: present when ``thread`` is non-empty (from the tap-drill table)

    Parameters
    ----------
    material : str
        Registry material id or alias (e.g. ``alu_6061``, ``aluminum``, ``ti_6al4v``).
    tool_type : str
        One of ``endmill_flat``, ``endmill_ball``, ``drill``, ``tap``, ``face_mill``.
    tool_dia : float
        Tool diameter in mm.
    operation : str
        One of ``roughing``, ``finishing``, ``slotting``, ``drilling``.
    machine_limits : dict, optional
        Keys: ``max_rpm``, ``min_rpm``, ``max_feed``, ``min_feed`` (all optional within
        the dict). Unset keys fall back to ``_DEFAULT_MACHINE_LIMITS``.
    thread : str, optional
        When non-empty, look up the standard tap drill and include ``tap_drill_mm``
        on the answer (e.g. ``M10x1.5`` → 8.5).
    machine : str, optional
        Machine profile id from ``machines.json`` (e.g. ``haas_vf2``). When set,
        overrides benchtop defaults for rpm/feed clamps before ``machine_limits``.
    """
    limits = dict(_DEFAULT_MACHINE_LIMITS)
    machine_id = str(machine or "").strip()
    if machine_id:
        prof = machine_profile(machine_id)
        limits.update(machine_limits_from_profile(prof))
        limits["machine_profile"] = machine_id
    if machine_limits:
        limits.update(machine_limits)
    max_rpm  = float(limits.get("max_rpm",  _DEFAULT_MACHINE_LIMITS["max_rpm"]))
    min_rpm  = float(limits.get("min_rpm",  _DEFAULT_MACHINE_LIMITS["min_rpm"]))
    max_feed = float(limits.get("max_feed", _DEFAULT_MACHINE_LIMITS["max_feed"]))
    min_feed = float(limits.get("min_feed", _DEFAULT_MACHINE_LIMITS["min_feed"]))

    mat_row = _material_row(material)
    if mat_row is None:
        return {
            "schema": _SCHEMA_CALC,
            "calc": "feeds_speeds",
            "ok": False,
            "error": f"material '{material}' not found in feeds/speeds table",
            "hint": "Use research_material_lookup() to check the material name, or cam_materials() for supported ids.",
        }

    tool_ops = mat_row.get("tool_types", {}).get(tool_type)
    if tool_ops is None:
        avail = list(mat_row.get("tool_types", {}).keys())
        return {
            "schema": _SCHEMA_CALC,
            "calc": "feeds_speeds",
            "ok": False,
            "error": f"tool_type '{tool_type}' not in table for material '{material}'",
            "hint": f"Available tool types: {avail}",
        }

    op_row = tool_ops.get(operation)
    if op_row is None:
        avail_ops = list(tool_ops.keys())
        return {
            "schema": _SCHEMA_CALC,
            "calc": "feeds_speeds",
            "ok": False,
            "error": f"operation '{operation}' not in table for {material}/{tool_type}",
            "hint": f"Available operations: {avail_ops}",
        }

    dia = max(0.1, float(tool_dia))
    bracket = _dia_bracket(dia)

    # ---- surface speed (Vc) ------------------------------------------------
    vc_sfm_range = op_row.get("vc_sfm", [100, 200])
    vc_sfm_lo, vc_sfm_hi = float(vc_sfm_range[0]), float(vc_sfm_range[1])
    vc_sfm_mid = _midpoint(vc_sfm_lo, vc_sfm_hi)

    # SFM -> RPM: rpm = (vc_sfm * 0.3048 * 1000) / (pi * dia_mm)
    def sfm_to_rpm(sfm: float) -> float:
        return (sfm * 0.3048 * 1000.0) / (math.pi * dia)

    rpm_lo  = _clampd(sfm_to_rpm(vc_sfm_lo),  min_rpm, max_rpm)
    rpm_hi  = _clampd(sfm_to_rpm(vc_sfm_hi),  min_rpm, max_rpm)
    rpm_mid = _clampd(sfm_to_rpm(vc_sfm_mid), min_rpm, max_rpm)

    # ---- chipload / feed ---------------------------------------------------
    notes = []

    if tool_type in ("drill",):
        # Drilling uses feed-per-rev not chipload-per-tooth.
        fpr_map = op_row.get("feed_per_rev_mm_by_dia", {})
        fpr_range = fpr_map.get(bracket) or list(fpr_map.values())[-1]
        fpr_lo, fpr_hi = float(fpr_range[0]), float(fpr_range[1])
        fpr_mid = _midpoint(fpr_lo, fpr_hi)

        feed_lo  = _clampd(rpm_lo  * fpr_lo,  min_feed, max_feed)
        feed_hi  = _clampd(rpm_hi  * fpr_hi,  min_feed, max_feed)
        feed_mid = _clampd(rpm_mid * fpr_mid, min_feed, max_feed)
        chipload_lo  = fpr_lo
        chipload_hi  = fpr_hi
        chipload_mid = fpr_mid
        notes.append(f"drill: feed/rev range {fpr_lo}–{fpr_hi} mm (bracket {bracket})")
    elif tool_type == "tap":
        # Tap feed is synchronized: feed = rpm * pitch (caller supplies pitch or use 1.0).
        # We expose vc range here; the synchronized feed must be computed per pitch.
        fpr_lo, fpr_hi = 0.5, 3.0  # placeholder mm/rev (pitch-dependent)
        feed_lo  = _clampd(rpm_lo  * fpr_lo,  min_feed, max_feed)
        feed_hi  = _clampd(rpm_hi  * fpr_hi,  min_feed, max_feed)
        feed_mid = _clampd(rpm_mid * 1.0,    min_feed, max_feed)
        chipload_lo = chipload_hi = chipload_mid = float("nan")
        notes.append("tap: feed must be synchronized (feed = rpm * pitch); use tap_drill() for drill size")
    else:
        # Milling tools: chipload per tooth.
        cl_map = op_row.get("chipload_by_dia", {})
        cl_range = cl_map.get(bracket) or list(cl_map.values())[-1]
        chipload_lo, chipload_hi = float(cl_range[0]), float(cl_range[1])
        chipload_mid = _midpoint(chipload_lo, chipload_hi)

        # Assume 2 flutes as default; caller can override by post-scaling.
        flutes = 2
        feed_lo  = _clampd(rpm_lo  * chipload_lo  * flutes, min_feed, max_feed)
        feed_hi  = _clampd(rpm_hi  * chipload_hi  * flutes, min_feed, max_feed)
        feed_mid = _clampd(rpm_mid * chipload_mid * flutes, min_feed, max_feed)
        notes.append(f"feed assumes {flutes} flutes; scale feed proportionally for other flute counts")

    # ---- stepdown / stepover -----------------------------------------------
    sd_range = op_row.get("stepdown_frac", [0.1, 0.5])
    so_range = op_row.get("stepover_frac", [0.1, 0.5])
    sd_lo, sd_hi = float(sd_range[0]) * dia, float(sd_range[1]) * dia
    so_lo, so_hi = float(so_range[0]) * dia, float(so_range[1]) * dia
    sd_mid = _midpoint(sd_lo, sd_hi)
    so_mid = _midpoint(so_lo, so_hi)

    # ---- coolant note ------------------------------------------------------
    coolant = op_row.get("coolant", "")

    # ---- m/min surface speed -----------------------------------------------
    vc_mmin_lo  = round(vc_sfm_lo  * 0.3048, 1)
    vc_mmin_hi  = round(vc_sfm_hi  * 0.3048, 1)
    vc_mmin_mid = round(vc_sfm_mid * 0.3048, 1)

    source_citation = op_row.get("source", "services/research/data/feeds_speeds.json")

    answer = {
        "rpm":      {"min": round(rpm_lo, 0),      "mid": round(rpm_mid, 0),      "max": round(rpm_hi, 0)},
        "feed":     {"min": round(feed_lo, 0),     "mid": round(feed_mid, 0),     "max": round(feed_hi, 0)},
        "stepdown": {"min": round(sd_lo, 3),       "mid": round(sd_mid, 3),       "max": round(sd_hi, 3)},
        "stepover": {"min": round(so_lo, 3),       "mid": round(so_mid, 3),       "max": round(so_hi, 3)},
        "vc_sfm":  [vc_sfm_lo, vc_sfm_hi],
        "vc_mmin": [vc_mmin_lo, vc_mmin_hi],
        "chipload": {
            "min": round(chipload_lo, 4) if not math.isnan(chipload_lo) else None,
            "mid": round(chipload_mid, 4) if not math.isnan(chipload_mid) else None,
            "max": round(chipload_hi, 4) if not math.isnan(chipload_hi) else None,
        },
        "coolant":    coolant,
        "provenance": f"{material}/{tool_type}/{operation} @ dia_bracket={bracket}; cam_id={mat_row.get('cam_id', '?')}",
    }

    thread_s = str(thread or "").strip()
    if thread_s:
        td = _lookup_tap_drill(thread_s)
        if td is None:
            notes.append(f"thread '{thread_s}' not found in tap_drill table")
        else:
            answer["tap_drill_mm"] = td["tap_drill_mm"]
            if "pct_thread" in td:
                answer["pct_thread"] = td.get("pct_thread", 75)
            if td.get("series"):
                answer["thread_series"] = td["series"]
            notes.append(f"tap_drill for {thread_s}: {td['tap_drill_mm']} mm")

    if machine_id:
        notes.append(f"machine profile '{machine_id}' applied to rpm/feed clamps")

    return _calc_envelope(
        "feeds_speeds",
        answer,
        units="rpm, mm/min, mm",
        equation=(
            f"rpm = vc_sfm*0.3048*1000/(π·dia); "
            f"feed = rpm · chipload · flutes (milling) or rpm · fpr (drilling)"
        ),
        inputs={
            "material": material,
            "tool_type": tool_type,
            "tool_dia_mm": dia,
            "operation": operation,
            "machine_limits": limits,
            "dia_bracket": bracket,
        },
        source_kind="table",
        citation=source_citation,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# check_envelope — returns aicad.advisory/1 item when params are out of range
# ---------------------------------------------------------------------------

def check_envelope(
    material: str,
    tool_type: str,
    tool_dia: float,
    operation: str,
    machine_limits: Optional[dict] = None,
    *,
    rpm: Optional[float] = None,
    feed: Optional[float] = None,
    stepdown: Optional[float] = None,
    stepover: Optional[float] = None,
) -> Optional[dict]:
    """Check whether supplied cutting params fall within the table envelope.

    Returns an ``aicad.advisory/1`` item (contract §3) when any parameter is
    out-of-range, or ``None`` when everything is within bounds.

    The advisory ``severity`` is ``"hint"``; ``candidate_fixes`` contains clamped
    values; ``confidence`` is ``"table"`` (per §3 rules).
    """
    env = feeds_speeds(material, tool_type, tool_dia, operation, machine_limits)
    if env.get("ok") is False or "answer" not in env:
        # Can't range-check if table lookup failed; return a soft advisory.
        return {
            "schema": _SCHEMA_ADV,
            "items": [{
                "id": "adv_fs_no_data",
                "severity": "hint",
                "check": "feeds_speeds_envelope",
                "finding": f"No feeds/speeds table entry for {material}/{tool_type}/{operation}; cannot range-check params.",
                "refs": {},
                "numbers": {},
                "candidate_fixes": [],
                "helper_tools": ["feeds_speeds", "cam_materials"],
                "source": {"kind": "table", "detail": "feeds_speeds_check_envelope"},
            }],
        }

    ans = env["answer"]
    violations: list[dict] = []
    fixes: list[dict] = []

    def _check(name: str, val: Optional[float], lo: float, hi: float, unit: str) -> None:
        if val is None:
            return
        if val < lo or val > hi:
            clamped = round(_clampd(val, lo, hi), 3)
            violations.append({
                "param": name, "supplied": round(val, 3), "lo": lo, "hi": hi,
                "unit": unit,
            })
            fixes.append({
                "action": "adjust_param",
                "params": {name: clamped},
                "basis": f"{name} table envelope [{lo}…{hi}] {unit} for {material}/{tool_type}/{operation}",
                "predicted_effect": f"Clamped {name} {val:.3g} → {clamped} {unit} (within table envelope)",
                "confidence": _SCHEMA_ADV_ITEM_CONFIDENCE,
            })

    rpm_lo  = ans["rpm"]["min"];      rpm_hi  = ans["rpm"]["max"]
    feed_lo = ans["feed"]["min"];     feed_hi = ans["feed"]["max"]
    sd_lo   = ans["stepdown"]["min"]; sd_hi   = ans["stepdown"]["max"]
    so_lo   = ans["stepover"]["min"]; so_hi   = ans["stepover"]["max"]

    _check("rpm",      rpm,      rpm_lo,  rpm_hi,  "RPM")
    _check("feed",     feed,     feed_lo, feed_hi, "mm/min")
    _check("stepdown", stepdown, sd_lo,   sd_hi,   "mm")
    _check("stepover", stepover, so_lo,   so_hi,   "mm")

    if not violations:
        return None

    finding_parts = []
    for v in violations:
        finding_parts.append(
            f"{v['param']}={v['supplied']} {v['unit']} outside [{v['lo']}…{v['hi']}]"
        )
    finding = (
        f"Cutting params out of table envelope for {material}/{tool_type}/{operation}: "
        + "; ".join(finding_parts)
        + f". Suggested clamped values in candidate_fixes."
    )

    return {
        "schema": _SCHEMA_ADV,
        "items": [{
            "id": "adv_fs_out_of_envelope",
            "severity": "hint",
            "check": "feeds_speeds_envelope",
            "finding": finding,
            "refs": {
                "material": material, "tool_type": tool_type,
                "tool_dia_mm": tool_dia, "operation": operation,
            },
            "numbers": {v["param"]: v["supplied"] for v in violations},
            "candidate_fixes": fixes,
            "helper_tools": ["feeds_speeds"],
            "source": {"kind": "table", "detail": "services/research/data/feeds_speeds.json"},
        }],
    }


# ---------------------------------------------------------------------------
# tap_drill — public API
# ---------------------------------------------------------------------------

_METRIC_RE  = re.compile(r"^m\s*([0-9]+(?:\.[0-9]+)?)\s*(?:[x×]\s*([0-9]+(?:\.[0-9]+)?))?$", re.I)
_INCH_RE    = re.compile(r"^(#?\d+(?:/\d+)?)\s*[-]\s*(\d+)\s*(unc|unf)?$", re.I)


def _tap_drill_table() -> dict:
    db = _load_db()
    return db.get("tap_drill", {})


def _lookup_tap_drill(designation: str) -> Optional[dict]:
    """Look up the tap drill table for a thread designation.

    Returns a dict with at minimum ``tap_drill_mm`` and ``source`` keys, or None.
    """
    td = _tap_drill_table()
    des_stripped = designation.strip()

    # Canonicalise input for table lookup: try exact match across all sub-tables.
    for sub in ("metric_coarse", "metric_fine", "unified_unc", "unified_unf"):
        sub_table = td.get(sub, {})
        if des_stripped in sub_table:
            row = sub_table[des_stripped].copy()
            row["series"] = sub
            return row
        # Try case-insensitive.
        for key, val in sub_table.items():
            if key.lower() == des_stripped.lower():
                row = val.copy()
                row["series"] = sub
                return row

    # Try to construct a canonical key from parsed components.
    m = _METRIC_RE.match(des_stripped)
    if m:
        nom_str = m.group(1)
        pitch_str = m.group(2)
        nom = float(nom_str)
        if pitch_str:
            pitch = float(pitch_str)
            canonical = f"M{int(nom) if nom == int(nom) else nom}x{pitch}"
        else:
            canonical = f"M{int(nom) if nom == int(nom) else nom}"
        # Try coarse then fine.
        for sub in ("metric_coarse", "metric_fine"):
            sub_table = td.get(sub, {})
            for key, val in sub_table.items():
                if key.lower() == canonical.lower():
                    row = val.copy()
                    row["series"] = sub
                    return row
        # Compute fallback: tap drill ≈ major - pitch (ISO 2306 rule, 75% thread).
        try:
            from ..research import thread_spec as _ts  # noqa: PLC0415
            ts = _ts.thread_spec(designation)
            if ts.get("ok"):
                minor_int = ts["answer"].get("minor_diameter_internal_mm") or ts["answer"].get("minor_diameter_mm")
                if minor_int:
                    return {
                        "tap_drill_mm": round(float(minor_int), 3),
                        "pct_thread": 75,
                        "series": "metric_computed",
                        "_note": "Computed from ISO 724 minor diameter (not from table; verify against ISO 2306).",
                    }
        except Exception:  # noqa: BLE001
            pass
        if pitch_str:
            fallback_mm = nom - float(pitch_str)
            return {
                "tap_drill_mm": round(fallback_mm, 3),
                "pct_thread": 75,
                "series": "metric_computed",
                "_note": "Computed as major - pitch (ISO 2306 rule); verify against table.",
            }

    return None


def tap_drill(thread: str) -> dict:
    """Return the standard tap drill diameter for a thread designation.

    Accepts metric coarse (``M6x1.0``, ``M8``) and metric fine, UNC (``1/4-20``,
    ``#10-24 UNC``) and UNF designations.

    Returns an ``aicad.calc/1`` envelope. ``answer.tap_drill_mm`` is the recommended
    drill diameter in mm (for both metric and inch threads). ``answer.pct_thread``
    is the approximate thread percentage (typically 75%).
    """
    td = _tap_drill_table()
    source_citation = td.get("_source", "Machinery's Handbook 31st ed. §Tap Drill Sizes; ISO 2306; ASME B94.9")

    result = _lookup_tap_drill(thread)
    if result is None:
        return {
            "schema": _SCHEMA_CALC,
            "calc": "tap_drill",
            "ok": False,
            "error": f"Thread designation '{thread}' not found in tap_drill table",
            "hint": (
                "Accepted forms: metric 'M6x1.0', 'M8' (coarse pitch inferred); "
                "unified '#10-24', '1/4-20 UNC', '3/8-16 UNF'. "
                "Use research_thread_spec() for full thread geometry."
            ),
        }

    notes = []
    if "_note" in result:
        notes.append(result.pop("_note"))

    answer = {
        "tap_drill_mm": result["tap_drill_mm"],
        "pct_thread": result.get("pct_thread", 75),
        "series": result.get("series", ""),
    }
    if "tap_drill_in" in result:
        answer["tap_drill_in"] = result["tap_drill_in"]

    return _calc_envelope(
        "tap_drill",
        answer,
        units="mm",
        equation="tap_drill_mm = major_diameter_mm - pitch_mm (ISO 2306, 75% thread engagement)",
        inputs={"thread": thread, "prefilled": {}},
        source_kind="table",
        citation=source_citation,
        notes=notes,
    )

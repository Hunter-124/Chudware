"""Groundedness checker — slot-match numerals in model text vs CAD state.

Shared between the verify-battery (T-H4) and the GSL eval harness (T-F10).
Usage:
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness(model_text, state_dict)
    # result: {"score": 0.0..1.0, "matched": [...], "missed": [...], "spurious": [...]}
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Unit normalization table — all factors convert TO the SI base listed in the
# comment. Keys are lowercase.
# ---------------------------------------------------------------------------
UNIT_FACTORS: dict[str, tuple[str, float]] = {
    # length → mm
    "mm":  ("mm",  1.0),
    "cm":  ("mm",  10.0),
    "m":   ("mm",  1000.0),
    "in":  ("mm",  25.4),
    "ft":  ("mm",  304.8),
    # stress → MPa
    "mpa": ("MPa", 1.0),
    "gpa": ("MPa", 1000.0),
    "ksi": ("MPa", 6.894757),
    "psi": ("MPa", 0.006894757),
    # force → N
    "n":   ("N",   1.0),
    "kn":  ("N",   1000.0),
    "lbf": ("N",   4.44822),
    # mass → g
    "g":   ("g",   1.0),
    "kg":  ("g",   1000.0),
    "lb":  ("g",   453.592),
    # angle → deg
    "deg": ("deg", 1.0),
    "°":   ("deg", 1.0),
    "rad": ("deg", 57.2958),
    # dimensionless / area / volume — keep as-is
    "mm2": ("mm2", 1.0),
    "mm3": ("mm3", 1.0),
    "mm²": ("mm2", 1.0),
    "mm³": ("mm3", 1.0),
    "%":   ("%",   1.0),
}

# Regex to match numbers (int/float/scientific) with an optional unit token.
# Captures: (number_str, unit_str_or_empty)
_NUM_RE = re.compile(
    r"""
    [-+]?                       # optional sign
    (?:
        \d+\.?\d*               # integer or decimal
      | \.\d+                   # leading-dot decimal
    )
    (?:[eE][-+]?\d+)?           # optional scientific exponent
    (?:\s*                      # optional whitespace before unit
        (mm²|mm³|mm2|mm3|MPa|GPa|ksi|psi|lbf|kN|kn|kg|lb|cm|ft|in|
         deg|rad|°|mm|[gmn%])   # unit token (order matters: longer first)
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _parse_numerals(text: str) -> list[tuple[float, str]]:
    """Extract (raw_value, raw_unit) pairs from text. Unit may be empty string."""
    results = []
    for m in _NUM_RE.finditer(text):
        try:
            val = float(m.group(0).split()[0] if m.group(1) else m.group(0))
            unit = (m.group(1) or "").strip()
            results.append((val, unit))
        except (ValueError, AttributeError):
            continue
    return results


def _normalize(value: float, unit: str) -> float:
    """Convert value+unit to SI base value. Returns value unchanged if unit unknown."""
    key = unit.lower() if unit else ""
    if key in UNIT_FACTORS:
        _, factor = UNIT_FACTORS[key]
        return value * factor
    return value


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, float]]:
    """Recursively flatten a nested dict/list to (dotted.key, float) pairs."""
    out: list[tuple[str, float]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, float(obj)))
    return out


def _values_match(extracted: float, slot_val: float) -> bool:
    """Return True if two normalized values are within tolerance."""
    # Near-zero: absolute tolerance
    if abs(slot_val) < 1e-9 and abs(extracted) < 1e-9:
        return True
    if abs(slot_val) < 1e-9:
        return abs(extracted) <= 0.01
    # General: ±5% relative
    rel = abs(extracted - slot_val) / abs(slot_val)
    return rel <= 0.05


def check_groundedness(model_text: str, state_dict: dict) -> dict:
    """Slot-match numerals extracted from model_text against state_dict numerics.

    Returns:
        {
            "score": float 0..1,
            "matched": list of {extracted, unit, slot, slot_value},
            "missed":  list of {extracted, unit},  # in text but no matching slot
            "spurious": list of {slot, value},      # in state but not mentioned
        }
    """
    # Extract and normalize numerals from text
    raw_pairs = _parse_numerals(model_text)
    extracted: list[tuple[float, str, float]] = []  # (raw_val, unit, norm_val)
    for raw_val, unit in raw_pairs:
        norm_val = _normalize(raw_val, unit)
        extracted.append((raw_val, unit, norm_val))

    # Flatten state dict to (slot_name, slot_value) pairs (numeric only)
    slots = _flatten(state_dict)
    # Also try normalizing slot values (they are assumed to be in SI already)
    slot_norms = [(name, val) for name, val in slots]

    # Edge case: no numerals in text AND no numerals in state → vacuously grounded
    if not extracted and not slot_norms:
        return {"score": 1.0, "matched": [], "missed": [], "spurious": []}

    matched: list[dict] = []
    missed: list[dict] = []
    used_slots: set[str] = set()

    for raw_val, unit, norm_val in extracted:
        best_slot = None
        # Try direct match with each unit family: the extracted norm_val may be in
        # a different SI family than the slot value (e.g., both mm after conversion).
        # We also try matching the raw value directly against slots.
        for slot_name, slot_val in slot_norms:
            if slot_name in used_slots:
                continue
            if _values_match(norm_val, slot_val) or _values_match(raw_val, slot_val):
                best_slot = (slot_name, slot_val)
                break

        if best_slot:
            used_slots.add(best_slot[0])
            matched.append({
                "extracted": raw_val,
                "unit": unit,
                "slot": best_slot[0],
                "slot_value": best_slot[1],
            })
        else:
            missed.append({"extracted": raw_val, "unit": unit})

    spurious = [
        {"slot": name, "value": val}
        for name, val in slot_norms
        if name not in used_slots
    ]

    total = len(extracted)
    score = len(matched) / max(total, 1) if total > 0 else 1.0

    return {
        "score": score,
        "matched": matched,
        "missed": missed,
        "spurious": spurious,
    }

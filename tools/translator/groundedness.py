#!/usr/bin/env python3
"""Groundedness seam — SHARED with T-H4 (verifier battery). ONE implementation, two callers.

    HOT-SPOT: the production groundedness checker is OWNED BY T-H4
    (task/T-H4-verifier-battery). This file defines ONLY the interface the T-F10 eval
    harness needs, plus a minimal deterministic membership stub so the harness runs
    end-to-end today. When T-H4 lands, its checker implements `GroundednessChecker`
    and `set_checker()` swaps it in — no eval-harness change required.

The contract (text + state -> per-numeral grounded/ungrounded verdicts):

    checker.check(text, state) -> list[Verdict]

where every numeral found in `text` gets a Verdict {value, unit, grounded, matched_path}.
`grounded` == the numeral is supported by some slot of `state` (extrinsic hallucination
== an ungrounded numeral). The T-F10 slot-fidelity eval is INDEPENDENT of this seam for
its primary metric (SER slot accuracy over `enumerate_slots`); it consults the checker
only to attribute EXTRINSIC (invented-feature) hallucination, which is a groundedness
question by definition.

Interface open questions for T-H4 are logged in work/questions/T-F10.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from tools.translator.gsl_ser import Slot, enumerate_slots, extract_numerals


@dataclass(frozen=True)
class Verdict:
    """One numeral's groundedness against a state."""

    value: float
    unit: str
    grounded: bool
    matched_path: Optional[str] = None   # the slot it grounds to, if grounded


@runtime_checkable
class GroundednessChecker(Protocol):
    """The seam T-H4 implements. `check` returns one Verdict per numeral in `text`."""

    def check(self, text: str, state: dict) -> list[Verdict]:
        ...


class _MembershipStub:
    """# SEAM(T-H4): groundedness implementation lands with T-H4.

    Minimal deterministic stand-in: a numeral is grounded iff it unit-normalises to
    some `enumerate_slots(state)` value (the same `Slot.matches` comparison the slot
    harness uses). This is intentionally the FLOOR of groundedness (exact slot
    membership only) — T-H4's checker adds the richer verify-battery semantics
    (unit-sanity ranges, tolerance-stack awareness, the IP-7 inference output gate).
    Do NOT grow this stub into that checker; extend T-H4 instead.
    """

    def check(self, text: str, state: dict) -> list[Verdict]:
        slots: list[Slot] = [s for s in enumerate_slots(state) if s.numeric]
        verdicts: list[Verdict] = []
        for value, unit in extract_numerals(text):
            match = next((s for s in slots if s.matches(value, unit)), None)
            verdicts.append(Verdict(value, unit, match is not None,
                                    match.path if match else None))
        return verdicts


_CHECKER: GroundednessChecker = _MembershipStub()


def set_checker(checker: GroundednessChecker) -> None:
    """Swap the active groundedness checker (T-H4 calls this to install the real one)."""
    global _CHECKER
    if not isinstance(checker, GroundednessChecker):
        raise TypeError("checker must implement the GroundednessChecker protocol")
    _CHECKER = checker


def get_checker() -> GroundednessChecker:
    return _CHECKER


def check(text: str, state: dict) -> list[Verdict]:
    """Module-level convenience: verdicts from the active checker."""
    return _CHECKER.check(text, state)


def is_stub() -> bool:
    """True while the T-H4 implementation has not been installed (report provenance)."""
    return isinstance(_CHECKER, _MembershipStub)


__all__ = ["Verdict", "GroundednessChecker", "H4GroundednessAdapter",
           "set_checker", "get_checker", "check", "is_stub"]


# ===========================================================================
# T-H4 implementation (merged 2026-07-02). check_groundedness() below is the
# production checker used by the verify battery. ALIGNMENT TODO (recorded in
# FOREMAN merge plan + work/questions/T-F10.md ruling): wrap it in a class
# implementing GroundednessChecker above (per-numeral Verdicts) and make
# set_checker() install it as the default, retiring _MembershipStub. Until
# then the two callers use their respective surfaces and both test suites run.
# ===========================================================================


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
# The number and unit are SEPARATE named groups so attached units ("50mm") parse.
# The (?![A-Za-z0-9]) lookahead stops "3 more" reading as "3 m" + "ore".
_NUM_RE = re.compile(
    r"""
    (?P<num>
        [-+]?                   # optional sign
        (?: \d+\.?\d*           # integer or decimal
          | \.\d+ )             # leading-dot decimal
        (?:[eE][-+]?\d+)?       # optional scientific exponent
    )
    (?:\s*                      # optional whitespace before unit
        (?P<unit>
            mm²|mm³|mm2|mm3|MPa|GPa|ksi|psi|lbf|kN|kg|lb|cm|ft|in|
            deg|rad|°|mm|[gmnGMN]|%
        )
        (?![A-Za-z0-9])         # unit must end at a word boundary
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _parse_numerals(text: str) -> list[tuple[float, str]]:
    """Extract (raw_value, raw_unit) pairs from text. Unit may be empty string."""
    results = []
    for m in _NUM_RE.finditer(text):
        try:
            val = float(m.group("num"))
        except ValueError:
            continue
        results.append((val, (m.group("unit") or "").strip()))
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


def _verdicts_from_state(model_text: str, state_dict: dict) -> tuple[
    list[tuple[float, str, float]],  # extracted: (raw_val, unit, norm_val)
    list[tuple[str, float]],          # slot_norms: (name, val)
    list[dict],                        # matched
    list[dict],                        # spurious
    set[str],                          # used_slots
]:
    """Core matching logic shared by check_groundedness() and H4GroundednessAdapter.

    Returns the raw decomposed result so both callers can derive their surface
    (dict for the battery, list[Verdict] for the GroundednessChecker protocol)
    from one canonical implementation.
    """
    raw_pairs = _parse_numerals(model_text)
    extracted: list[tuple[float, str, float]] = []
    for raw_val, unit in raw_pairs:
        norm_val = _normalize(raw_val, unit)
        extracted.append((raw_val, unit, norm_val))

    slot_norms = _flatten(state_dict)

    matched: list[dict] = []
    spurious: list[dict] = []
    used_slots: set[str] = set()

    for raw_val, unit, norm_val in extracted:
        best_slot = None
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
            spurious.append({"extracted": raw_val, "unit": unit})

    return extracted, slot_norms, matched, spurious, used_slots


class H4GroundednessAdapter:
    """GroundednessChecker implementation wrapping the T-H4 check_groundedness() logic.

    Implements the GroundednessChecker Protocol (one Verdict per numeral in `text`)
    by delegating to _verdicts_from_state(), the same core used by check_groundedness().
    Install via set_checker(H4GroundednessAdapter()) to upgrade the module default from
    the _MembershipStub floor to the full T-H4 verify-battery semantics.

    NOTE (alignment TODO): test_groundedness_is_stub_by_default in the T-F10 suite
    asserts is_stub() is True, pinning _MembershipStub as the process-start default.
    Until that test is updated to accept the adapter as default, the module-level
    default remains _MembershipStub. See work/questions/align-post-merge.md.
    """

    def check(self, text: str, state: dict) -> list[Verdict]:
        """Return one Verdict per numeral in text, in extraction order."""
        raw_pairs = _parse_numerals(text)
        slot_norms = _flatten(state)
        used_slots: set[str] = set()
        verdicts: list[Verdict] = []

        for raw_val, unit in raw_pairs:
            norm_val = _normalize(raw_val, unit)
            best_slot: tuple[str, float] | None = None
            for slot_name, slot_val in slot_norms:
                if slot_name in used_slots:
                    continue
                if _values_match(norm_val, slot_val) or _values_match(raw_val, slot_val):
                    best_slot = (slot_name, slot_val)
                    break
            if best_slot:
                used_slots.add(best_slot[0])
                verdicts.append(Verdict(
                    value=raw_val,
                    unit=unit,
                    grounded=True,
                    matched_path=best_slot[0],
                ))
            else:
                verdicts.append(Verdict(value=raw_val, unit=unit, grounded=False))

        return verdicts


def check_groundedness(model_text: str, state_dict: dict) -> dict:
    """Slot-match numerals extracted from model_text against state_dict numerics.

    Returns:
        {
            "score": float 0..1 (= matched / total text numerals),
            "matched":  list of {extracted, unit, slot, slot_value},
            "spurious": list of {extracted, unit},   # in text, NO state match — hallucination risk
            "unmentioned_state": list of {slot, value},  # in state, never cited (informational)
        }
    """
    extracted, slot_norms, matched, spurious, used_slots = _verdicts_from_state(
        model_text, state_dict
    )

    # Edge case: no numerals in the text → vacuously grounded (nothing to hallucinate)
    if not extracted:
        return {"score": 1.0, "matched": [], "spurious": [],
                "unmentioned_state": [{"slot": n, "value": v} for n, v in slot_norms]}

    unmentioned = [
        {"slot": name, "value": val}
        for name, val in slot_norms
        if name not in used_slots
    ]

    return {
        "score": len(matched) / len(extracted),
        "matched": matched,
        "spurious": spurious,
        "unmentioned_state": unmentioned,
    }

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


__all__ = ["Verdict", "GroundednessChecker", "set_checker", "get_checker", "check", "is_stub"]

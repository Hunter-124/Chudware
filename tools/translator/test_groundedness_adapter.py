"""Adapter conformance tests: H4GroundednessAdapter vs _MembershipStub.

Proves that both implementations agree on .grounded for two canonical cases:
  1. A numeral that IS grounded (present in the gsl-ser state within ±5% tolerance).
  2. A numeral that is NOT grounded (not present in state — spurious).

These tests exercise the GroundednessChecker seam (set_checker / get_checker / is_stub)
with the adapter installed. They do NOT require the engine.

Enumerator note: _MembershipStub uses gsl_ser.enumerate_slots (requires a gsl-ser
shaped state); H4GroundednessAdapter uses _flatten (works on plain dicts).  Agreement
tests therefore use a gsl-ser-shaped state (BREP_STATE below) so both enumerators
can find its numeric slots. The adapter-only tests use flat dicts.

Note: matched_path is NOT compared between stub and adapter because the two use
different path encodings (gsl_ser dotted paths vs _flatten dotted paths). The only
semantically-binding agreement property is: whether each numeral is grounded or not.
"""
from __future__ import annotations

import pytest

from tools.translator.groundedness import (
    H4GroundednessAdapter,
    _MembershipStub,
    Verdict,
    GroundednessChecker,
)

# gsl-ser shaped state: enumerate_slots AND _flatten both find radius=4.0 here
# (bores[0].radius via _flatten; bore.f11.radius via enumerate_slots)
_BREP_STATE = {
    "domain": "brep",
    "part": "bolt_plate",
    "bbox_mm": [120.0, 80.0, 10.0],
    "bores": [
        {"sid": "f11", "radius": 4.0},
    ],
}

# Flat state only usable by adapter (enumerate_slots returns nothing for it)
_FLAT_STATE = {"height": 50.0, "mass": 150.0}

_STUB = _MembershipStub()
_ADAPTER = H4GroundednessAdapter()


# ── protocol conformance ────────────────────────────────────────────────────


def test_adapter_implements_protocol():
    assert isinstance(_ADAPTER, GroundednessChecker)


def test_stub_implements_protocol():
    assert isinstance(_STUB, GroundednessChecker)


# ── grounded numeral: stub and adapter both agree it IS grounded ────────────
# Use BREP_STATE so both enumerate the radius=4.0 slot.


def test_grounded_numeral_stub():
    """4mm matches bore radius=4.0 in state → grounded (via stub's enumerate_slots)."""
    verdicts = _STUB.check("The bore radius is 4mm.", _BREP_STATE)
    assert len(verdicts) >= 1
    v = next((v for v in verdicts if abs(v.value - 4.0) < 1e-6), None)
    assert v is not None, "numeral 4 not found in stub verdicts"
    assert v.grounded is True


def test_grounded_numeral_adapter():
    """Same text/state — adapter must also return grounded=True for 4mm."""
    verdicts = _ADAPTER.check("The bore radius is 4mm.", _BREP_STATE)
    assert len(verdicts) >= 1
    v = next((v for v in verdicts if abs(v.value - 4.0) < 1e-6), None)
    assert v is not None, "numeral 4 not found in adapter verdicts"
    assert v.grounded is True


def test_grounded_agreement():
    """Stub and adapter agree on .grounded for the grounded case (gsl-ser state)."""
    text = "The bore radius is 4mm."
    stub_v = _STUB.check(text, _BREP_STATE)
    ada_v = _ADAPTER.check(text, _BREP_STATE)
    stub_map = {round(v.value, 6): v.grounded for v in stub_v}
    ada_map = {round(v.value, 6): v.grounded for v in ada_v}
    assert set(stub_map.keys()) == set(ada_map.keys()), (
        f"stub saw numerals {set(stub_map.keys())} but adapter saw {set(ada_map.keys())}"
    )
    for val, grounded in stub_map.items():
        assert ada_map[val] == grounded, (
            f"numeral {val}: stub says grounded={grounded}, adapter says {ada_map[val]}"
        )


# ── spurious numeral: stub and adapter both agree it is NOT grounded ────────


def test_spurious_numeral_stub():
    """999mm has no state match in BREP_STATE → spurious (grounded=False) for stub."""
    verdicts = _STUB.check("The bore radius is 999mm.", _BREP_STATE)
    assert len(verdicts) >= 1
    v = next((v for v in verdicts if abs(v.value - 999.0) < 1e-6), None)
    assert v is not None, "numeral 999 not found in stub verdicts"
    assert v.grounded is False


def test_spurious_numeral_adapter():
    """Same text/state — adapter must also return grounded=False for 999mm."""
    verdicts = _ADAPTER.check("The bore radius is 999mm.", _BREP_STATE)
    assert len(verdicts) >= 1
    v = next((v for v in verdicts if abs(v.value - 999.0) < 1e-6), None)
    assert v is not None, "numeral 999 not found in adapter verdicts"
    assert v.grounded is False


def test_spurious_agreement():
    """Stub and adapter agree on .grounded for the spurious case (gsl-ser state)."""
    text = "The bore radius is 999mm."
    stub_v = _STUB.check(text, _BREP_STATE)
    ada_v = _ADAPTER.check(text, _BREP_STATE)
    stub_map = {round(v.value, 6): v.grounded for v in stub_v}
    ada_map = {round(v.value, 6): v.grounded for v in ada_v}
    assert set(stub_map.keys()) == set(ada_map.keys())
    for val, grounded in stub_map.items():
        assert ada_map[val] == grounded, (
            f"numeral {val}: stub={grounded}, adapter={ada_map[val]}"
        )


# ── adapter-only tests (flat state dict — adapter handles these directly) ───


def test_adapter_flat_state_grounded():
    """Adapter works on plain flat dicts (not just gsl-ser shaped states)."""
    verdicts = _ADAPTER.check("The part is 50mm tall.", _FLAT_STATE)
    v = next((v for v in verdicts if abs(v.value - 50.0) < 1e-6), None)
    assert v is not None
    assert v.grounded is True


def test_adapter_flat_state_spurious():
    verdicts = _ADAPTER.check("The part is 999mm tall.", _FLAT_STATE)
    v = next((v for v in verdicts if abs(v.value - 999.0) < 1e-6), None)
    assert v is not None
    assert v.grounded is False


# ── adapter Verdict structure ──────────────────────────────────────────────


def test_adapter_verdict_has_matched_path_when_grounded():
    verdicts = _ADAPTER.check("The height is 50mm.", _FLAT_STATE)
    grounded = [v for v in verdicts if v.grounded]
    assert grounded, "expected at least one grounded verdict"
    assert grounded[0].matched_path is not None
    assert grounded[0].matched_path == "height"


def test_adapter_verdict_no_matched_path_when_spurious():
    verdicts = _ADAPTER.check("The height is 999mm.", _FLAT_STATE)
    spurious = [v for v in verdicts if not v.grounded]
    assert spurious, "expected at least one spurious verdict"
    assert spurious[0].matched_path is None


def test_adapter_vacuous_empty_text():
    """No numerals → empty verdicts."""
    verdicts = _ADAPTER.check("This design looks good.", _FLAT_STATE)
    assert verdicts == []


def test_adapter_set_checker_roundtrip():
    """Verify set_checker installs the adapter and is_stub() reflects the change."""
    import tools.translator.groundedness as gm
    original = gm.get_checker()
    adapter = H4GroundednessAdapter()
    try:
        gm.set_checker(adapter)
        assert gm.get_checker() is adapter
        assert gm.is_stub() is False
    finally:
        gm.set_checker(original)
    assert gm.get_checker() is original

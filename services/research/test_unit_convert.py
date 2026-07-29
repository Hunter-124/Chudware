"""Tests for the arbitrary ``from_unit`` -> ``to_unit`` converter in
``services.research.unit_convert``.

Every reference value below is an EXACT (or exactly-defined-to-a-standard) conversion
whose defining document is cited inline. None of them was read off the implementation —
that is the whole point: a converter with a wrong factor is worse than no converter,
because it launders a model's guess into a "deterministic" answer.

Legacy ``unit_convert(value, unit, family=...)`` coverage lives in
``test_source_helpers.py``; the frozen-behaviour test here is a belt-and-braces
duplicate so a regression fails in this file too.
"""
from __future__ import annotations

import math

import pytest

from services import research
from services.research.unit_convert import supported_units

uc = research.unit_convert


def _val(*args, **kwargs) -> float:
    out = uc(*args, **kwargs)
    assert out["ok"] is True, out
    return out["value"]


# ------------------------------------------------------------------ exact reference values


def test_inch_is_exactly_25_4_mm():
    # International yard and pound agreement (1959): 1 in == 25.4 mm EXACTLY.
    assert _val(25.4, from_unit="mm", to_unit="in") == 1.0
    assert _val(1, from_unit="in", to_unit="mm") == 25.4


def test_ksi_to_mpa_is_6_894757():
    # 1 psi == 6894.757293168... Pa, from 1 lbf == 4.4482216152605 N (exact) divided by
    # 1 in^2 == 0.00064516 m^2 (exact). NIST SP 811 Appendix B.9 lists 6.894 757 E+03 Pa.
    # This is the tool's own motivating example: the ksi/MPa 6.9x silent error.
    assert _val(1, from_unit="ksi", to_unit="MPa") == pytest.approx(6.894757, abs=5e-7)
    assert _val(60, from_unit="ksi", to_unit="MPa") == pytest.approx(413.6854376, abs=1e-6)


def test_lbf_to_newton_is_exact():
    # lbf := lb * g_n = 0.45359237 kg * 9.80665 m/s^2 == 4.4482216152605 N EXACTLY.
    assert _val(1, from_unit="lbf", to_unit="N") == 4.4482216152605


def test_pound_to_kilogram_is_exact():
    # International avoirdupois pound (1959): 1 lb == 0.45359237 kg EXACTLY.
    assert _val(1, from_unit="lb", to_unit="kg") == 0.45359237
    # 1 lb == 16 oz exactly, so an ounce is 0.45359237/16 kg.
    assert _val(16, from_unit="oz", to_unit="lb") == pytest.approx(1.0, rel=1e-15)


def test_celsius_to_fahrenheit_is_affine_at_two_points():
    # degF = degC * 9/5 + 32.  Tested at TWO temperatures on purpose: a pure scale factor
    # (no offset) would pass at exactly one point and be silently wrong everywhere else.
    assert _val(0, from_unit="degC", to_unit="degF") == pytest.approx(32.0, abs=1e-9)
    assert _val(100, from_unit="degC", to_unit="degF") == pytest.approx(212.0, abs=1e-9)
    # and the reverse direction, same two points
    assert _val(32, from_unit="degF", to_unit="degC") == pytest.approx(0.0, abs=1e-9)
    assert _val(212, from_unit="degF", to_unit="degC") == pytest.approx(100.0, abs=1e-9)


def test_celsius_to_kelvin_offset():
    # ITS-90 / SI: T/K = t/degC + 273.15 exactly.
    assert _val(0, from_unit="degC", to_unit="K") == pytest.approx(273.15, abs=1e-12)
    assert _val(25, from_unit="degC", to_unit="K") == pytest.approx(298.15, abs=1e-12)


def test_cubic_inch_is_exactly_16387_064_mm3():
    # 25.4^3 == 16387.064 exactly (the inch definition cubed). measure_part reports mm^3,
    # models routinely want in^3 or cm^3.
    assert _val(1, from_unit="in^3", to_unit="mm^3") == 16387.064
    assert _val(1000, from_unit="mm^3", to_unit="cm^3") == 1.0


def test_lbf_ft_to_newton_metre():
    # 4.4482216152605 N * 0.3048 m (1 ft == 304.8 mm exactly).
    assert _val(1, from_unit="lbf*ft", to_unit="N*m") == 1.3558179483314004
    # 1 lbf*in is one twelfth of that.
    assert _val(12, from_unit="lbf*in", to_unit="lbf*ft") == pytest.approx(1.0, rel=1e-15)


def test_square_inch_and_bar_and_atm():
    assert _val(1, from_unit="in^2", to_unit="mm^2") == 645.16      # 25.4^2 exactly
    assert _val(1, from_unit="bar", to_unit="Pa") == 100000.0       # 1 bar := 1e5 Pa
    assert _val(1, from_unit="atm", to_unit="Pa") == 101325.0       # standard atmosphere
    assert _val(1, from_unit="MPa", to_unit="psi") == pytest.approx(145.0377377, abs=1e-6)


def test_degrees_to_radians():
    assert _val(180, from_unit="deg", to_unit="rad") == pytest.approx(math.pi, rel=1e-15)


def test_litre_and_gallon():
    assert _val(1, from_unit="L", to_unit="cm^3") == pytest.approx(1000.0, rel=1e-15)
    # US liquid gallon := 231 in^3 exactly -> 3.785411784 L
    assert _val(1, from_unit="gal", to_unit="L") == pytest.approx(3.785411784, rel=1e-12)


# ----------------------------------------------------------------- cross-family refusal


@pytest.mark.parametrize(
    "frm,to,fam_a,fam_b",
    [("mm", "kg", "length", "mass"),
     ("psi", "degC", "pressure", "temperature"),
     ("N", "degC", "force", "temperature"),
     ("in^3", "mm^2", "volume", "area")],
)
def test_cross_family_refuses_and_names_both_families(frm, to, fam_a, fam_b):
    out = uc(1, from_unit=frm, to_unit=to)
    assert out["ok"] is False
    assert out["reason"] == "bad_unit"
    hint = out["hint"]
    assert fam_a in hint and fam_b in hint, hint
    assert "value" not in out


def test_family_argument_contradicting_the_units_refuses():
    out = uc(1, from_unit="ksi", to_unit="MPa", family="length")
    assert out["ok"] is False
    assert "pressure" in out["hint"] and "length" in out["hint"]


def test_unknown_unit_refuses():
    out = uc(1, from_unit="mm", to_unit="widgets")
    assert out["ok"] is False and out["reason"] == "bad_unit"
    assert "widgets" in out["hint"]


# ------------------------------------------------------------------- round trip, all families


_ROUNDTRIP = [
    ("length", "mm", "in"), ("length", "m", "ft"), ("length", "um", "mil"),
    ("area", "mm^2", "in^2"), ("area", "m^2", "cm^2"),
    ("volume", "mm^3", "in^3"), ("volume", "L", "gal"),
    ("angle", "deg", "rad"), ("angle", "rev", "grad"),
    ("mass", "kg", "lb"), ("mass", "g", "oz"),
    ("force", "N", "lbf"), ("force", "kN", "kip"),
    ("pressure", "MPa", "ksi"), ("pressure", "Pa", "psi"), ("pressure", "bar", "atm"),
    ("torque", "N*m", "lbf*ft"), ("torque", "N*mm", "lbf*in"),
    ("temperature", "degC", "degF"), ("temperature", "K", "degF"),
    ("count", "count", "count"),
]


@pytest.mark.parametrize("family,a,b", _ROUNDTRIP)
def test_round_trip_every_family(family, a, b):
    """value -> other unit -> back must return the original. Catches an INVERTED factor,
    which a single-direction reference test cannot."""
    for v in (1.0, 37.5, -4.25):
        mid = _val(v, from_unit=a, to_unit=b)
        back = _val(mid, from_unit=b, to_unit=a)
        assert back == pytest.approx(v, rel=1e-9, abs=1e-9)
        assert uc(v, from_unit=a, to_unit=b)["family"] == family


def test_every_registered_family_is_round_tripped():
    """Guards the table above against a new family being added with no round-trip."""
    covered = {f for f, _a, _b in _ROUNDTRIP}
    assert covered == set(supported_units())


# ------------------------------------------------------------------------ spellings / case


@pytest.mark.parametrize(
    "spelling,canon",
    [("MPa", "MPa"), ("mpa", "MPa"), ("megapascal", "MPa"), ("N/mm^2", "MPa"),
     ("psi", "psi"), ("ksi", "ksi"), ("KSI", "ksi"),
     ("Nmm", "N*mm"), ("N-mm", "N*mm"), ("N*mm", "N*mm"),
     ("in", "in"), ("inch", "in"), ("inches", "in"), ('"', "in"), ("INCH", "in"),
     ("deg", "deg"), ("degrees", "deg"), ("\u00b0", "deg"),
     ("um", "um"), ("micron", "um"), ("\u00b5m", "um"),
     ("ft-lb", "lbf*ft"), ("in-lb", "lbf*in"), ("cc", "cm^3"), ("in3", "in^3"),
     ("\u00b0C", "degC"), ("celsius", "degC"), ("kelvin", "K")],
)
def test_spellings_models_actually_write(spelling, canon):
    out = uc(1, from_unit=spelling, to_unit=canon)
    assert out["ok"] is True, out
    assert out["from_unit"] == canon
    assert out["value"] == pytest.approx(1.0, rel=1e-15)


@pytest.mark.parametrize("sym,canon", [("mm", "mm"), ("nm", "nm"), ("Nm", "N*m"),
                                       ("m", "m"), ("g", "g"), ("K", "K")])
def test_case_sensitive_symbols_resolve_verbatim(sym, canon):
    out = uc(1, from_unit=sym, to_unit=canon)
    assert out["ok"] is True, out
    assert out["from_unit"] == canon


@pytest.mark.parametrize("sym", ["NM", "nM", "Mm", "MM", "M", "G", "k"])
def test_dangerous_case_variants_refuse_rather_than_guess(sym):
    out = uc(1, from_unit=sym, to_unit="mm")
    assert out["ok"] is False, out
    assert "ambiguous" in out["hint"]
    assert "value" not in out


def test_direction_and_family_are_echoed_not_silently_changed():
    out = uc(2, from_unit="ksi", to_unit="mpa")
    assert out["from_unit"] == "ksi" and out["to_unit"] == "MPa"
    assert out["from_value"] == 2.0
    assert out["family"] == "pressure"
    assert "ksi -> MPa" in out["interpreted"]


# ------------------------------------------------------------------------------- batch


def test_batch_reports_per_entry_and_bad_entry_carries_no_value():
    out = uc(batch=[
        {"value": 1, "from_unit": "in", "to_unit": "mm"},
        {"value": 1, "from_unit": "mm", "to_unit": "kg"},      # cross-family: must fail
        {"value": 1, "from_unit": "ksi", "to_unit": "MPa"},
    ])
    assert out["ok"] is True
    res = out["results"]
    assert len(res) == 3
    assert out["succeeded"] == 2 and out["failed"] == 1
    assert res[0]["ok"] is True and res[0]["value"] == 25.4
    assert res[1]["ok"] is False and "value" not in res[1]
    assert "length" in res[1]["hint"] and "mass" in res[1]["hint"]
    assert res[2]["ok"] is True and res[2]["value"] == pytest.approx(6.894757, abs=5e-7)


def test_batch_rejects_a_non_object_entry_without_killing_the_call():
    out = uc(batch=[{"value": 1, "from_unit": "in", "to_unit": "mm"}, "nonsense"])
    assert out["ok"] is True
    assert out["results"][0]["ok"] is True
    assert out["results"][1]["ok"] is False
    assert "value" not in out["results"][1]


def test_batch_must_be_a_list():
    out = uc(batch={"value": 1})
    assert out["ok"] is False and out["reason"] == "bad_unit"


# ---------------------------------------------------------------- legacy behaviour frozen


def test_legacy_canonical_direction_unchanged():
    out = research.unit_convert(1, "in", "length")
    assert out["ok"] is True
    assert out["value"] == 25.4 and out["unit"] == "mm" and out["units"] == "mm"
    assert out["family"] == "length"
    assert out["answer"] == "1 in = 25.4 mm"
    # legacy envelope carries no from/to keys — additive change must not leak into it
    assert "from_unit" not in out and "to_unit" not in out

    assert research.unit_convert(90, "deg", "angle")["value"] == 90
    assert research.unit_convert(2, "cm")["value"] == 20.0
    bad = research.unit_convert(1, "kg", "length")
    assert bad["ok"] is False and bad["reason"] == "bad_unit"


def test_missing_to_unit_is_a_teaching_error():
    out = uc(1, from_unit="ksi")
    assert out["ok"] is False
    assert "to_unit" in out["hint"]

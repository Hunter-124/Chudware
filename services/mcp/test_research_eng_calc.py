"""Input-dialect regression for ``research_eng_calc``'s parameter names.

Every payload here is a REAL shape from the live pass-2 general-lane run of 2026-07-29
(claude-sonnet-4-5), which called

    research_eng_calc(calc_id="cantilever_end_deflection",
                      inputs={"L_mm":120, "P_N":500, "b_mm":20, "h_mm":8, "E_MPa":200000})

and was refused with ``missing ['force_n','length_mm','I_mm4']``.  Two of those three were
pure dialect (``L``/``P`` are the textbook symbols, units already correct); the third,
``I_mm4``, is NOT derivable from b/h without assuming a section shape, so the tool must
TEACH the substitution rather than invent a deflection.

Every assertion here is a NUMBER, a stored field, or a specific refusal — never `ok:true`.
Dispatch goes through the path that ships (``agent_turn._bind_tool``), so a regression in
either the reconciler or the tool wiring fails these.
"""
from __future__ import annotations

import json

import pytest


def _call(tool: str, **kwargs):
    from services.agent import agent_turn as at

    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve through _bind_tool"
    out = fn(**kwargs)
    return json.loads(out) if isinstance(out, str) else out


def test_belt_route_nested_geometry_reaches_the_registered_calculator():
    import math

    out = _call("research_eng_calc", calc="belt_route", params={"pulleys": [
        {"center_mm": [0, 0], "pitch_radius_mm": 10, "wrap": "ccw"},
        {"center_mm": [100, 0], "pitch_radius_mm": 10, "wrap": "ccw"},
        {"center_mm": [50, 0], "pitch_radius_mm": 5, "wrap": "cw"},
    ]})
    expected = 100 + 2 * math.sqrt(2500 - 225) + 20 * math.pi + 30 * math.asin(.3)
    assert out["answer"]["length_mm"] == pytest.approx(expected, rel=1e-12)


# The expectation, derived here rather than copied from the tool:
#   I = b*h^3/12  for a solid rectangle
#   d = F*L^3 / (3*E*I)
B_MM, H_MM = 20.0, 8.0
F_N, L_MM, E_MPA = 500.0, 120.0, 200_000.0
I_MM4 = B_MM * H_MM ** 3 / 12.0                     # 853.333… mm^4
D_MM = F_N * L_MM ** 3 / (3.0 * E_MPA * I_MM4)      # 1.6875 mm




# --------------------------------------------------------------------------- (a) case

@pytest.mark.parametrize("spelling", ["E_MPa", "E_MPA", "e_mpa", "E_Mpa"])
def test_modulus_case_variants_resolve_and_the_calc_returns_the_number(spelling):
    """`E_mpa` is the canonical param; a model writes the unit as `MPa`.  A difference in
    the case of a unit suffix must never be a missing parameter."""
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"length_mm": L_MM, "force_n": F_N, "I_mm4": I_MM4,
                        spelling: E_MPA})
    assert out["ok"] is True, out.get("hint")
    assert out["value"] == pytest.approx(D_MM, rel=1e-6)
    assert out["unit"] == "mm"
    # the value came from the modulus we passed, not a default
    assert out["inputs"]["E_mpa"] == pytest.approx(E_MPA)


def test_case_only_variant_is_echoed_not_silently_renamed():
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"length_mm": L_MM, "force_n": F_N, "I_mm4": I_MM4,
                        "E_MPa": E_MPA})
    assert any("E_MPa" in n and "E_mpa" in n for n in out["interpreted"])


# --------------------------------------------------------------------------- (b) symbols

def test_L_and_P_symbols_resolve_and_the_deflection_is_right():
    """The live payload, with the one genuinely underdetermined input supplied."""
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"L_mm": L_MM, "P_N": F_N, "I_mm4": I_MM4, "E_MPa": E_MPA})
    assert out["ok"] is True, out.get("hint")
    assert out["value"] == pytest.approx(D_MM, rel=1e-6)
    assert out["inputs"]["length_mm"] == pytest.approx(L_MM)
    assert out["inputs"]["force_n"] == pytest.approx(F_N)
    notes = " | ".join(out["interpreted"])
    assert "'L_mm' as 'length_mm'" in notes and "'P_N' as 'force_n'" in notes


def test_symbols_on_the_other_legacy_formulas_produce_their_numbers():
    axial = _call("research_eng_calc", formula="axial_stress",
                  params={"P_N": 1000, "A_mm2": 10})
    assert axial["ok"] is True, axial.get("hint")
    assert axial["value"] == pytest.approx(1000.0 / 10.0)          # 100 MPa

    bend = _call("research_eng_calc", formula="bending_stress",
                 params={"M_Nmm": 5000, "c_mm": 4, "I_mm4": I_MM4})
    assert bend["ok"] is True, bend.get("hint")
    assert bend["value"] == pytest.approx(5000.0 * 4.0 / I_MM4, rel=1e-6)


def test_bare_symbol_without_a_unit_is_not_guessed():
    """`P` alone is pressure in one formula and a point load in another — the mapping is
    unit-guarded, so a unitless `P` must stay unresolved rather than pick a meaning."""
    out = _call("research_eng_calc", formula="axial_stress",
                params={"P": 1000, "A_mm2": 10})
    assert out["ok"] is False
    assert out["reason"] == "bad_query"
    assert "force_n" in out["hint"]
    assert "value" not in out


def test_a_formula_owning_L_mm_keeps_it():
    """`beam` declares L_mm/b_mm/h_mm itself; the symbol table must not re-point them and
    the registry calc must still return its own numbers."""
    out = _call("research_eng_calc", formula="beam",
                params={"support": "cantilever", "load": "point", "section": "rect",
                        "L_mm": L_MM, "b_mm": B_MM, "h_mm": H_MM,
                        "F_n": F_N, "E_mpa": E_MPA})
    assert out["ok"] is True, out.get("hint")
    answer = out["answer"]
    assert answer["I_mm4"] == pytest.approx(I_MM4, rel=1e-6)
    assert answer["max_deflection_mm"] == pytest.approx(D_MM, rel=1e-4)


# --------------------------------------------------------------------------- (c) I_mm4

def test_missing_I_with_b_and_h_teaches_the_substitution_and_invents_nothing():
    """The verbatim live payload.  b/h describe a rectangle only if you ASSUME the section
    is one, so the tool must refuse and hand back the substitution instead of a number."""
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"L_mm": L_MM, "P_N": F_N, "b_mm": B_MM, "h_mm": H_MM,
                        "E_MPa": E_MPA})
    assert out["ok"] is False
    assert out["reason"] == "bad_query"
    # it did NOT invent a deflection
    assert "value" not in out and "answer" not in out
    assert out["substitution"] == "I_mm4 = b*h**3/12 (solid rectangle)"
    hint = out["hint"]
    assert "b*h**3/12" in hint
    assert "I_mm4" in hint
    assert "853.333" in hint          # the illustrative value for THEIR b/h
    assert "beam" in hint             # the calc that does take a section
    # the two symbols were still resolved, so only I is outstanding
    assert "missing ['I_mm4']" in hint


def test_the_taught_substitution_is_the_value_that_makes_the_calc_run():
    """Closes the loop: feeding the hint's own number back yields the deflection."""
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"L_mm": L_MM, "P_N": F_N,
                        "I_mm4": B_MM * H_MM ** 3 / 12.0, "E_MPa": E_MPA})
    assert out["ok"] is True, out.get("hint")
    assert out["value"] == pytest.approx(1.6875, rel=1e-6)


def test_no_rect_teaching_when_no_b_or_h_was_passed():
    out = _call("research_eng_calc", calc_id="cantilever_end_deflection",
                inputs={"L_mm": L_MM, "P_N": F_N, "E_MPa": E_MPA})
    assert out["ok"] is False
    assert "substitution" not in out
    assert "b*h**3/12" not in out["hint"]


# --------------------------------------------------------------------------- no catch-all

def test_unknown_input_key_is_still_refused():
    """`Q_N` has the right unit but Q is not a force symbol — a unit-suffixed key must not
    be slurped into whichever param happens to be missing."""
    out = _call("research_eng_calc", formula="cantilever_end_deflection",
                params={"L_mm": L_MM, "Q_N": F_N, "I_mm4": I_MM4, "E_MPa": E_MPA})
    assert out["ok"] is False
    assert out["reason"] == "bad_query"
    assert "force_n" in out["hint"]
    assert "value" not in out


def test_unrelated_key_does_not_satisfy_a_required_param():
    out = _call("research_eng_calc", formula="axial_stress",
                params={"force_n": 1000, "flange_wibble_mm": 3})
    assert out["ok"] is False
    assert out["reason"] == "bad_query"
    assert "area_mm2" in out["hint"]
    assert "value" not in out

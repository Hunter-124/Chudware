"""Input-dialect regression for research_tools + intake_tools.fit_report.

Every call in ``test_verified_failing_ledger_calls`` is a REAL argument shape mined from
recorded agent-run ledgers that used to be rejected before
the tool could run.  The rest pin the behaviour those widenings introduced: measured-mode
arithmetic, the closest-ISO classification, the shaft-basis mirror, material-query
splitting, eng_calc near-miss resolution, and fit_report's ad-hoc pair + teaching error.

Dispatch is exercised through the path that ships — ``agent_turn._bind_tool`` resolves the
real MCP registry and calls the underlying Python function — plus one FastMCP round-trip,
because a var-keyword parameter that works via ``_bind_tool`` can still be broken over
FastMCP (a bare ``**kwargs`` becomes a REQUIRED pydantic field there).
"""
from __future__ import annotations

import asyncio
import json

import pytest


# --------------------------------------------------------------------------- harness

def _call(tool: str, **kwargs):
    """Dispatch exactly as the built-in agent does: reconcile, then call the real fn."""
    from services.agent import agent_turn as at

    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve through _bind_tool"
    reconciled, _notes, err = at._reconcile_kwargs(tool, fn, dict(kwargs))
    assert err is None, f"{tool} rejected {kwargs} before running: {err}"
    out = fn(**reconciled)
    return json.loads(out) if isinstance(out, str) else out


@pytest.fixture(scope="module")
def scratch_doc():
    return _call("new_part", name="fit_dialect_scratch")["doc_id"]


# --------------------------------------------------------------------------- baseline

# Captured from HEAD before the dialect work; the canonical call must not drift.
CANONICAL_H7G6 = {
    "schema": "aicad.calc/1",
    "ok": True,
    "calc": "fit_calc",
    "answer": {
        "hole": [0.0, 0.021],
        "shaft": [-0.02, -0.007],
        "clearance": [0.007, 0.041],
        "fit_character": "clearance",
        "hole_absolute": [20.0, 20.021],
        "shaft_absolute": [19.98, 19.993],
    },
    "units": "mm",
    "equation": "ISO 286-2, nominal=20mm, H7/g6",
    "inputs": {"nominal": 20, "fit": "H7/g6"},
    "source": {"kind": "closed_form", "citation": "ISO 286-2:2010"},
    "notes": [],
}


def test_canonical_fit_calc_is_byte_identical():
    """A caller already using the documented shape sees zero change — no `mode`, no
    `interpreted`, no reordered keys."""
    assert _call("fit_calc", nominal=20, fit="H7/g6") == CANONICAL_H7G6


def test_canonical_call_still_works_through_fastmcp():
    """The var-keyword parameter must not break the external MCP surface: FastMCP turns
    a bare ``**kwargs`` into a REQUIRED field, which fails every call including this one.
    """
    from mcp.server.fastmcp import FastMCP
    from services.mcp.research_tools import register_research_tools

    mcp = FastMCP("dialect-test")
    register_research_tools(mcp)

    schema = asyncio.run(mcp.list_tools())
    fit = next(t for t in schema if t.name == "fit_calc")
    assert "kwargs" not in (fit.inputSchema.get("required") or []), (
        "the var-keyword parameter is advertised as REQUIRED — every FastMCP call fails")

    # FastMCP validates `nominal: Optional[float]`, so it hands the body 20.0 —
    # exactly as the old `nominal: float` signature did. Same envelope, float echo.
    expected = json.loads(json.dumps(CANONICAL_H7G6))
    expected["inputs"]["nominal"] = 20.0
    expected["equation"] = "ISO 286-2, nominal=20.0mm, H7/g6"
    _content, structured = asyncio.run(
        mcp.call_tool("fit_calc", {"nominal": 20, "fit": "H7/g6"}))
    assert json.loads(structured["result"]) == expected


# --------------------------------------------------------------------------- ledger calls

# Real calls from the run ledgers, each verified to fail on the pre-fix tree.
# (tool, args, predicate on the parsed response)
LEDGER_CALLS = [
    ("fit_calc", {"shaft_diameter": 20, "hole_diameter": 20.02}, "ok"),
    ("fit_calc", {"shaft": 20, "hole": 20.02}, "ok"),
    ("fit_calc", {"diameter": 20, "fit": "H7/g6"}, "ok"),
    ("fit_calc", {"fit_type": "H7/g6", "diameter": 20}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_type": "h7"}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_type": "sliding_fit"}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_class": "h7"}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_type": "sliding", "shaft_tolerance": "h7"},
     "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_type": "sliding"}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "fit_type": "sliding", "tolerance": "h7",
                  "clearance_limit": 0.042}, "ok"),
    ("fit_calc", {"shaft": "20", "fit": "h7", "tolerance": "0.042"}, "ok"),
    ("fit_calc", {"shd": 20, "fit": "h7", "tolerance": 0.042}, "ok"),
    ("fit_calc", {"diameter": 20, "fit": "h7", "tolerance": 0.042}, "ok"),
    ("fit_calc", {"shaft_diameter": 20, "shaft_tolerance": "S1", "bore_diameter": 20,
                  "bore_tolerance": "h7"}, "ok"),
    ("fit_calc", {"shaft": 20, "shaft_tolerance": "S1", "bore": 20,
                  "bore_tolerance": "h7", "fit_type": "sliding"}, "ok"),
    ("fit_calc", {"shaft": "20", "fit": "h7", "tolerance": "0.042",
                  "output": "bore_size,clearance"}, "ok"),
    ("fit_calc", {"diameter": "20", "fit": "h7", "tolerance": "0.042",
                  "output": "bore_size,clearance"}, "ok"),
    ("fit_calc", {"size": "20", "fit": "h7", "tolerance": "0.042",
                  "output": "bore_size,clearance"}, "ok"),
    ("fit_calc", {"nominal": 20, "fit": "G7/h7"}, "ok"),
    ("fit_calc", {"nominal": 20, "hole": "H7", "shaft": "h7"}, "ok"),
    ("recommend_fit", {"fit_type": "clearance", "diameter": 20}, "ok"),
    ("recommend_fit", {"fit_type": "sliding", "shaft_diameter": 20}, "ok"),
    ("research_material_lookup", {"query": "6061"}, "ok"),
    ("research_eng_calc", {"formula": "circle_area", "params": {"diameter": 20}}, "ok"),
    ("research_eng_calc", {"formula": "circle_area", "params": {"r": 10}}, "ok"),
]


@pytest.mark.parametrize("tool,args,expect", LEDGER_CALLS,
                         ids=[f"{t}-{'-'.join(sorted(a))}" for t, a, _ in LEDGER_CALLS])
def test_verified_failing_ledger_calls(tool, args, expect):
    out = _call(tool, **args)
    assert out.get("ok") is True, f"{tool}({args}) still fails: {out}"


def test_ambiguous_single_diameter_teaches_both_modes():
    """fit_calc(diameter=20) is genuinely under-determined — it must NOT guess."""
    out = _call("fit_calc", diameter=20)
    assert out["ok"] is False
    assert out["reason"] == "ambiguous_fit_input"
    assert set(out["modes"]) == {"design", "measured"}
    hint = out["hint"].lower()
    assert "design" in hint and "measured" in hint
    assert "hole_diameter=20.02" in out["modes"]["measured"]
    assert "h7/g6" in out["modes"]["design"].lower()


# --------------------------------------------------------------------------- measured mode

def test_measured_mode_clearance_arithmetic():
    """Ø20.020 bore on a Ø20.000 shaft: 0.020 mm clearance, and the standard class it
    lands in is H7/h6 (hole inside 20.000–20.021, shaft inside 19.987–20.000)."""
    out = _call("fit_calc", hole_diameter=20.02, shaft_diameter=20.0)
    ans = out["answer"]
    assert out["mode"] == "measured"
    assert ans["clearance"] == [0.02, 0.02]
    assert ans["fit_character"] == "clearance"
    assert ans["nominal_mm"] == 20.0
    assert ans["closest_iso_fits"][0]["fit"] == "H7/h6"
    assert ans["closest_iso_fits"][0]["covers_measured"] is True


def test_measured_mode_interference_arithmetic():
    """Ø20.030 shaft into a Ø20.000 bore: 0.030 mm interference, classed press."""
    out = _call("fit_calc", shaft_diameter=20.03, bore_diameter=20.0)
    ans = out["answer"]
    assert ans["clearance"] == [-0.03, -0.03]
    assert ans["fit_character"] == "press"
    assert ans["closest_iso_fits"][0]["fit_character"] == "press"
    assert ans["closest_iso_fits"][0]["covers_measured"] is True


def test_measured_mode_with_per_side_tolerances_reports_min_and_max():
    """Per-side tolerance classes are a real input mode: 20 H7 bore on a 20 g6 shaft
    reproduces the ISO 286 band exactly (0.007–0.041 mm) from measured inputs."""
    out = _call("fit_calc", bore=20.0, bore_tolerance="H7",
                shaft=20.0, shaft_tolerance="g6")
    ans = out["answer"]
    assert ans["hole_absolute"] == [20.0, 20.021]
    assert ans["shaft_absolute"] == [19.98, 19.993]
    assert ans["clearance"] == [0.007, 0.041]
    assert ans["closest_iso_fits"][0]["fit"] == "H7/g6"
    assert ans["closest_iso_fits"][0]["deviation_mm"] == 0.0


def test_measured_mode_numeric_tolerances_are_symmetric_and_echoed():
    out = _call("fit_calc", hole=20.0, hole_tolerance=0.01,
                shaft=19.99, shaft_tolerance=0.005)
    ans = out["answer"]
    assert ans["hole_absolute"] == [19.99, 20.01]
    assert ans["shaft_absolute"] == [19.985, 19.995]
    # min clearance = 19.990 - 19.995 = -0.005; max = 20.010 - 19.985 = 0.025
    assert ans["clearance"] == [-0.005, 0.025]
    assert ans["fit_character"] == "transition"
    assert any("±0.01" in n for n in out["interpreted"])


def test_unparseable_side_tolerance_is_dropped_loudly_not_silently():
    """`shaft_tolerance="S1"` is a real ledger value and is not a usable ISO grade —
    the call still answers, but says the side was taken as exact and how to spell it."""
    out = _call("fit_calc", shaft_diameter=20, shaft_tolerance="S1",
                bore_diameter=20, bore_tolerance="h7")
    assert out["ok"] is True
    notes = " ".join(out["interpreted"])
    assert "S1" in notes and "exactly 20 mm" in notes
    # the bore side WAS applied, case-corrected to H7
    assert out["answer"]["hole_absolute"] == [20.0, 20.021]
    assert "uppercase" in notes


# --------------------------------------------------------------------------- design mode

def test_shaft_basis_designation_uses_the_iso_mirror_rule():
    """`G7/h7` used to fail outright with `unsupported_hole`. ISO 286-1 §5.3 makes it
    identical to H7/g7, so the G7 band is +0.007/+0.028 at Ø20 (the published value)."""
    out = _call("fit_calc", nominal=20, fit="G7/h7")
    assert out["ok"] is True
    assert out["answer"]["hole"] == [0.007, 0.028]
    assert out["answer"]["clearance"] == [0.007, 0.049]
    assert any("mirror" in n for n in out["interpreted"])


def test_shaft_basis_press_fit_matches_its_hole_basis_twin():
    """P7/h6 and H7/p6 are the SAME fit by construction — that is what Δ is for."""
    shaft_basis = _call("fit_calc", nominal=50, fit="P7/h6")
    hole_basis = _call("fit_calc", nominal=50, fit="H7/p6")
    assert shaft_basis["answer"]["clearance"] == hole_basis["answer"]["clearance"]
    assert shaft_basis["answer"]["clearance"] == [-0.042, -0.001]


def test_units_and_diameter_shorthand_are_converted_and_echoed():
    out = _call("fit_calc", nominal="0.75in", fit="H7/g6")
    assert out["answer"]["hole_absolute"] == [19.05, 19.071]
    assert any("19.05" in n for n in out["interpreted"])


def test_clearance_limit_is_checked_and_output_is_projected():
    out = _call("fit_calc", diameter=20, fit="H7/g6", clearance_limit=0.042,
                output="bore_size,clearance")
    ans = out["answer"]
    assert ans["limit_check"] == {"limit_mm": 0.042, "min_clearance_mm": 0.007,
                                  "max_clearance_mm": 0.041, "within_limit": True}
    assert ans["requested"] == {"bore_size": [20.0, 20.021],
                                "clearance": [0.007, 0.041]}


# --------------------------------------------------------------------------- recommend_fit

def test_recommend_fit_with_a_diameter_returns_the_computed_band():
    out = _call("recommend_fit", fit_type="sliding", shaft_diameter=20)
    top = out["answer"][0]
    assert top["recommended_classes"] == ["H7/g6"]
    assert top["band"]["fit"] == "H7/g6"
    assert top["band"]["nominal_mm"] == 20.0
    assert top["band"]["hole_absolute"] == [20.0, 20.021]
    assert top["band"]["clearance"] == [0.007, 0.041]
    assert any("fit_type" in n for n in out["interpreted"])


def test_recommend_fit_without_a_diameter_is_unchanged():
    out = _call("recommend_fit", function="sliding")
    assert out["ok"] is True
    assert "band" not in out["answer"][0]
    assert "interpreted" not in out


# --------------------------------------------------------------------------- material

def test_material_query_splits_into_material_and_property():
    out = _call("research_material_lookup", query="6061 yield strength")
    assert out["ok"] is True
    assert out["value"] == 276
    assert out["units"] == "MPa"
    assert out["property"] == "yield_mpa"
    assert any("split the query" in n for n in out["interpreted"])


def test_material_query_without_a_property_tail_stays_whole():
    """"6061-T6" must not be sliced up — only an unambiguous trailing property splits."""
    out = _call("research_material_lookup", alloy="6061-T6", prop="density")
    assert out["ok"] is True
    assert out["property"] == "density_g_cm3"
    assert out["value"] == pytest.approx(2.7)


def test_selection_style_call_says_it_is_not_a_selector(monkeypatch):
    """`application=`/`environment=` is a material RECOMMENDATION ask. It is served as a
    composed candidate search AND names the tool that actually does selection."""
    import services.mcp.research_tools as rt

    seen = {}

    def _fake(material, property=None):
        seen["material"] = material
        return {"ok": True, "answer": None, "note": "stub"}

    monkeypatch.setattr(rt.research, "material_lookup", _fake)
    mcp_fn = rt  # keep the module referenced for clarity
    assert mcp_fn is rt

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("mat-test")
    rt.register_research_tools(mcp)
    fn = mcp._tool_manager.get_tool("research_material_lookup").fn

    out = json.loads(fn(part_type="bushing", application="sliding_fit",
                        environment="general"))
    assert "bushing" in seen["material"] and "sliding fit" in seen["material"]
    notes = " ".join(out["interpreted"])
    assert "research_web_search" in notes
    assert "NAMED material" in notes


# --------------------------------------------------------------------------- eng_calc

def test_eng_calc_near_miss_formula_is_resolved_and_echoed():
    out = _call("research_eng_calc", calc="circle-area", inputs={"diameter_mm": 20})
    assert out["ok"] is True
    assert out["value"] == pytest.approx(314.159265, rel=1e-6)
    notes = " ".join(out["interpreted"])
    assert "circle_area" in notes


def test_eng_calc_unknown_formula_teaches_the_catalogue():
    out = _call("research_eng_calc", formula="axial_stres",
                params={"force_n": 1000, "area_mm2": 10})
    # a one-character typo resolves outright rather than erroring
    assert out["ok"] is True
    assert out["value"] == pytest.approx(100.0)


def test_eng_calc_arithmetic_is_refused_with_the_reason():
    """`formula="add"` is 3 recorded failures; the catalogue alone would not teach why."""
    out = _call("research_eng_calc", formula="add", params={"a": 17.0, "b": 2.25})
    assert out["ok"] is False
    assert out["reason"] == "not_found"
    assert "arithmetic" in out["hint"]
    assert "stress" in out["known"] and "axial_stress" in out["known"]["stress"]
    assert isinstance(out["closest"], list)


def test_eng_calc_param_stem_and_radius_are_reconciled():
    out = _call("research_eng_calc", formula="hoop_stress",
                params={"pressure": 5, "radius": 50, "thickness": 2})
    assert out["ok"] is True
    assert out["value"] == pytest.approx(125.0)
    assert len(out["interpreted"]) == 3


# --------------------------------------------------------------------------- fit_report

def test_fit_report_teaching_error_names_the_prerequisite_tools(scratch_doc):
    out = _call("fit_report", doc_id=scratch_doc)
    assert out["ok"] is False
    assert out["error"] == "nothing measured yet"
    assert out["state"]["doc_id"] == scratch_doc
    assert out["state"]["measured_parts"] == 0
    assert out["state"]["ledger_rows"] == 0
    hint = out["hint"]
    assert "submit_part_photos" in hint
    assert "measure_from_photo" in hint
    assert "confirm_dimensions" in hint
    # the order matters and must be stated
    assert hint.index("submit_part_photos") < hint.index("confirm_dimensions")
    assert "submit_part_photos" in out["example"]
    assert "fit_report(bore_diameter=" in out["ad_hoc_example"]


def test_fit_report_ad_hoc_pair_from_explicit_diameters(scratch_doc):
    out = _call("fit_report", doc_id=scratch_doc, bore_diameter=20.02,
                shaft_diameter=20.0)
    assert out["ok"] is True
    assert out["mode"] == "ad_hoc_pair"
    check = out["fit_report"]["checks"][0]
    assert check["clearance_mm"] == [0.02, 0.02]
    assert check["source"] == "argument"
    assert out["fit"]["answer"]["closest_iso_fits"][0]["fit"] == "H7/h6"
    assert "NOT" in out["interpreted"][0]
    assert "does not clear the intake gate" in out["interpreted"][0]


def test_fit_report_ad_hoc_design_pair_from_a_class(scratch_doc):
    """`fit_report(bore="d20", band="sliding-fit")` — Ø shorthand plus a functional
    keyword, both recorded shapes."""
    out = _call("fit_report", doc_id=scratch_doc, bore="d20", band="sliding-fit")
    assert out["ok"] is True
    check = out["fit_report"]["checks"][0]
    assert check["fit"] == "H7/g6"
    assert check["clearance_mm"] == [0.007, 0.041]
    assert out["fit_report"]["overall"] == "pass"


def test_fit_report_bore_with_lowercase_class_is_read_as_the_bore_band(scratch_doc):
    """`fit_report(bore_diameter=20, tolerance="h7")` — the only diameter is a bore, so
    the lowercase (shaft) class is the bore's, miscased."""
    out = _call("fit_report", doc_id=scratch_doc, bore_diameter=20, tolerance="h7")
    assert out["ok"] is True
    assert out["fit"]["answer"]["hole_absolute"] == [20.0, 20.021]
    assert any("BORE" in n for n in out["interpreted"])

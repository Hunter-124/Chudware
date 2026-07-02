"""Tests for the PyNite structural analysis layer (T-D2).

Covers the required DoD axial-post case plus textbook hand-calc validations (cantilever tip
deflection, simply-supported UDL midspan moment/deflection) so the solver output is checked against
closed-form answers, not just itself — mirroring the workstream-E precedent (work/questions/T-E2).

Run: /home/hunter/Repositories/CAD/.venv-mcp/bin/python -m pytest \
        engine_oracle/structural/test_analysis.py -q
"""

from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("Pynite")  # environment SKIP if the solver isn't provisioned (not a failure)

from engine.structural.analysis import (  # noqa: E402
    analyze_doc,
    build_pynite_model,
    material_properties,
    run_analysis,
    section_properties,
)
from engine.structural.elements import StructureDoc, make_element  # noqa: E402
from engine.structural.load_cases import DEAD_LIVE, DEAD_ONLY, LoadCombo  # noqa: E402

UNIT_D = LoadCombo("DEAD_ONLY", dead_factor=1.0)  # service-level (unfactored) dead for hand calcs


# --------------------------------------------------------------------------- section properties


def test_section_properties_rectangle():
    p = section_properties("100x100", "cedar")
    assert p["E"] == 12000.0 and p["G"] == 750.0
    assert p["A"] == pytest.approx(1e4)
    assert p["Iz"] == pytest.approx(100 * 100 ** 3 / 12.0)   # 8.333e6 mm^4


def test_material_prefix_matching():
    assert material_properties("steel_a36")[0] == 200000.0
    assert material_properties("df_no2")[0] == 11000.0
    assert material_properties("cedar")[:2] == (12000.0, 750.0)


# --------------------------------------------------------------------------- required DoD test


def test_axial_post_1kN():
    """2 m cedar post, 1 kN axial (compressive) point load → axial ≈ 1 kN, deflection ≈ 0.

    Contract sign convention is +tension, so a compressed post reports axial_n ≈ -1000 N.
    """
    doc = StructureDoc(name="post")
    doc.add(make_element("post", "100x100", "cedar", 2000.0))   # identity transform ⇒ vertical +Z
    doc.load_cases = [{
        "id": "lc1", "kind": "dead", "label": "D",
        "gravity": [0, 0, -1], "factor": 1.0,
        "point": {"at_mm": 2000.0, "p_n": 1000.0},   # 1 kN down at the top ⇒ pure axial
    }]
    out = analyze_doc(doc, UNIT_D)
    res = out["results"]
    assert len(res) == 1
    r = res[0]
    assert abs(r["axial_n"]) == pytest.approx(1000.0, rel=1e-3)   # magnitude ≈ 1 kN
    assert r["axial_n"] < 0                                       # compression (contract +tension)
    assert abs(r["deflection_mm"]) < 1e-3                         # transverse deflection ≈ 0


# --------------------------------------------------------------------------- hand-calc validations


def test_cantilever_tip_deflection_hand_calc():
    """Horizontal cantilever, transverse tip point load P: δ_tip = P·L³/(3·E·I), M_fix = P·L."""
    L, P = 3000.0, 500.0
    # place the beam along +X: rotate the local +Z axis onto global +X (row-major 4×4).
    x_along = [0, 0, 1, 0,
               0, 1, 0, 0,
               1, 0, 0, 0,
               0, 0, 0, 1]
    doc = StructureDoc(name="cantilever")
    doc.add(make_element("beam", "150x150", "cedar", L, transform=x_along))
    doc.load_cases = [{
        "id": "lc1", "kind": "dead", "gravity": [0, -1, 0], "factor": 1.0,
        "point": {"at_mm": L, "p_n": P},    # transverse tip load in -Y
    }]
    out = analyze_doc(doc, UNIT_D)["results"][0]

    props = section_properties("150x150", "cedar")
    E, I = props["E"], props["Iz"]
    expected_defl = P * L ** 3 / (3.0 * E * I)
    expected_moment = P * L
    assert abs(out["deflection_mm"]) == pytest.approx(expected_defl, rel=0.02)
    assert abs(out["moment_nmm"]) == pytest.approx(expected_moment, rel=0.02)


def test_simply_supported_udl_hand_calc():
    """Pinned-pinned beam under UDL w: M_mid = w·L²/8, δ_mid = 5·w·L⁴/(384·E·I).

    Built directly on PyNite (pinned supports) to validate run_analysis extraction + units on the
    most classic case — the doc-level v1 support model idealises footings as fixed, so this canonical
    simply-supported check exercises run_analysis rather than the doc build path.
    """
    from Pynite import FEModel3D

    L = 4000.0
    w = 2.0  # N/mm
    p = section_properties("200x300", "cedar")
    E, G, I = p["E"], p["G"], p["Iz"]

    m = FEModel3D()
    m.add_material("cedar", E, G, p["nu"], 1e-9)
    m.add_section("s", p["A"], p["Iy"], p["Iz"], p["J"])
    m.add_node("A", 0, 0, 0)
    m.add_node("B", L, 0, 0)
    m.add_member("beam", "A", "B", "cedar", "s")
    m.def_support("A", True, True, True, True, False, False)    # pin
    m.def_support("B", False, True, True, True, False, False)   # roller (release DX)
    m.add_member_dist_load("beam", "FY", -w, -w, case="dead")

    results = run_analysis(m, UNIT_D)
    r = next(x for x in results if x.element_id == "beam")

    expected_moment = w * L ** 2 / 8.0
    expected_defl = 5.0 * w * L ** 4 / (384.0 * E * I)
    assert abs(r.moment_nmm) == pytest.approx(expected_moment, rel=0.02)
    assert abs(r.deflection_mm) == pytest.approx(expected_defl, rel=0.02)


# --------------------------------------------------------------------------- connectivity + shape


def test_two_element_shared_node_connectivity():
    """Two collinear posts stacked end-to-end share the merged mid node → 3 nodes, not 4."""
    lower = make_element("post", "100x100", "cedar", 1000.0)          # (0,0,0)->(0,0,1000)
    stacked = [0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1000, 0, 0, 0, 1]     # translate +1000 in Z
    upper = make_element("post", "100x100", "cedar", 1000.0, transform=stacked)
    doc = StructureDoc(name="stacked")
    doc.add(lower)
    doc.add(upper)
    model = build_pynite_model(doc)
    assert len(model.nodes) == 3            # merged shared mid node (4 endpoints → 3 nodes)
    assert len(model.members) == 2


def test_result_json_serializable_and_shape():
    doc = StructureDoc(name="post")
    doc.add(make_element("post", "100x100", "cedar", 2000.0))
    doc.load_cases = [{"kind": "dead", "gravity": [0, 0, -1], "factor": 1.0,
                       "point": {"at_mm": 2000.0, "p_n": 1000.0}}]
    out = analyze_doc(doc, [DEAD_ONLY, DEAD_LIVE])   # two combos
    blob = json.dumps(out)                            # must not raise (no numpy scalars)
    assert isinstance(blob, str)
    fields = {"element_id", "axial_n", "shear_n", "moment_nmm", "deflection_mm", "dc_ratio", "pass"}
    for r in out["results"]:
        assert fields <= set(r)
        assert r["dc_ratio"] is None and r["pass"] is None   # T-D3 fills these
        for k in ("axial_n", "shear_n", "moment_nmm", "deflection_mm"):
            assert isinstance(r[k], float) and math.isfinite(r[k])
    assert len(out["results"]) == 2   # 1 member × 2 combos

"""Boundary-condition dialect regression tests for services/mcp/cad_sim_tools.py.

Measured live 2026-07-28 (claude-sonnet-4-5, one static-FEA task): four distinct
rejections, each of them a setup the solver fully supports written in a shape the tool
didn't read — the polar magnitude+direction load, the plural "faces" key, a real face
SID handed back from an earlier tool result, and mesh_fineness="coarse".

Every accepted-dialect test proves the fix with a NUMBER: the alternative spelling must
produce the IDENTICAL solver result as the shape that already worked, on the same
geometry. Every rejected-dialect test proves the input fails with an actionable error AND
that no study was run (sim_study_list stays empty on a fresh doc).

Conventions follow services/mcp/test_dialect_guards_gsl_plm_sim.py: real engine bridge via
services.agent.agent_turn._bind_tool, skipped when the bridge is unavailable.
"""
from __future__ import annotations

import json
import math
import threading
import time

import pytest
from services.mcp import cad_sim_tools as sim_tools

from services.mcp.cad_sim_tools import (
    _canon_item_keys,
    _norm_bc_list,
    _norm_direction,
    _norm_fineness,
    _positive_int,
    _validate_thermal_schedule,
)


def _dispatch():
    from services.agent.agent_turn import _bind_tool  # noqa: PLC0415
    return _bind_tool


def _skip_if_no_bridge(exc: Exception):
    msg = str(exc)
    if "engine" in msg.lower() or "bridge" in msg.lower() or "connect" in msg.lower():
        pytest.skip(f"engine bridge unavailable: {exc}")
    raise


_SEQ = [0]


def _block(name: str) -> str:
    """A fresh 100x50x20 steel block doc. The name is made unique per call: new_part
    REUSES a doc of the same name, and a reused doc would both fail the second box add
    and pollute the sim_study_list count these tests assert on."""
    _SEQ[0] += 1
    name = f"{name}_{_SEQ[0]}"
    bind_tool = _dispatch()
    try:
        r = json.loads(bind_tool("new_part")(name=name))
    except Exception as e:  # noqa: BLE001
        _skip_if_no_bridge(e)
    if not r.get("ok"):
        pytest.skip(f"new_part failed: {r}")
    doc_id = r["doc_id"]
    r2 = json.loads(bind_tool("add_features")(doc_id=doc_id, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
                                          "params": {"length": 100, "width": 50,
                                                     "height": 20}}}]))
    if not r2.get("ok"):
        pytest.skip(f"add_features failed: {r2}")
    return doc_id


@pytest.fixture(scope="module")
def bc_doc():
    return _block("bc_dialect_block")


def _static(doc_id: str, **kw) -> dict:
    """run_fea_static, resolving the auto-background handoff. A slow solve is handed off to
    a worker thread (`auto_backgrounded`) to dodge the transport timeout, so the result has
    to be polled off get_job — otherwise a loaded box turns a numeric assertion into a
    flake about a job_id."""
    material = kw.pop("material", "steel")
    out = json.loads(_dispatch()("run_fea_static")(doc_id=doc_id, material=material, **kw))
    if out.get("skipped"):
        pytest.skip(f"FEA backend unavailable: {out.get('reason')}")
    jid = out.get("job_id")
    if jid and out.get("summary") is None:
        get_job = _dispatch()("get_job")
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            job = json.loads(get_job(job_id=jid))
            status = job.get("status")
            if status in ("done", "completed", "error", "failed"):
                out = job.get("result") if isinstance(job.get("result"), dict) else job
                break
            time.sleep(1.0)
        else:
            pytest.skip(f"backgrounded solve {jid} did not finish in 300 s")
        if out.get("skipped"):
            pytest.skip(f"FEA backend unavailable: {out.get('reason')}")
    return out


def _peak(out: dict) -> float:
    assert out.get("ok") is True, out
    vm = (out.get("summary") or {}).get("max_von_mises_mpa")
    assert vm is not None, out
    return float(vm)


def _n_studies(doc_id: str) -> int:
    from services.mcp.cad_common import cb  # noqa: PLC0415
    return len(cb.call("sim_study_list", {"doc_id": doc_id}).get("studies") or [])


# ----------------------------------------------------------- accepted dialects (same number)

def test_polar_magnitude_direction_equals_explicit_force_vector(bc_doc):
    """magnitude + unit direction is the SAME load as force:[x,y,z] — the peak von Mises
    stress must match exactly, not merely come back ok."""
    explicit = _peak(_static(bc_doc, fixtures=["bottom"],
                             loads=[{"face": "top", "force": [200.0, 0.0, 0.0]}]))
    polar = _peak(_static(bc_doc, fixtures=["bottom"],
                          loads=[{"face": "top", "type": "force",
                                  "magnitude": 200, "direction": [1, 0, 0]}]))
    assert polar == explicit, (polar, explicit)


def test_non_unit_direction_is_normalized_not_used_as_a_scale(bc_doc):
    """direction [3,0,0] with magnitude 200 is a 200 N load, NOT a 600 N one — a
    non-unit direction must never silently scale the load."""
    explicit = _peak(_static(bc_doc, fixtures=["bottom"],
                             loads=[{"face": "top", "force": [200.0, 0.0, 0.0]}]))
    scaled = _peak(_static(bc_doc, fixtures=["bottom"],
                           loads=[{"face": "top", "magnitude": 200,
                                   "direction": [3, 0, 0]}]))
    assert scaled == explicit, (scaled, explicit)


def test_plural_faces_one_element_equals_singular_face(bc_doc):
    """faces:[q] is trivially the singular face:q — identical result on both sides,
    for the load AND the fixture wrapper the live model wrote."""
    singular = _peak(_static(bc_doc, fixtures=["bottom"],
                             loads=[{"face": "top", "pressure": 0.5}]))
    plural = _peak(_static(bc_doc, fixtures=[{"faces": ["bottom"], "type": "fixed"}],
                           loads=[{"faces": ["top"], "pressure": 0.5}]))
    assert plural == singular, (plural, singular)


def test_plural_faces_multi_element_fans_out_to_every_face(bc_doc):
    """A multi-element faces list applies the load to EACH face — strictly more loaded
    surface than the single-face case, so the stress must differ (no silent drop of the
    tail of the list)."""
    one = _peak(_static(bc_doc, fixtures=["bottom"],
                        loads=[{"faces": ["top"], "pressure": 0.5}]))
    two = _peak(_static(bc_doc, fixtures=["bottom"],
                        loads=[{"faces": ["top", "+x"], "pressure": 0.5}]))
    assert two != one, (two, one)


def test_face_sid_from_an_earlier_result_resolves_like_the_query_word(bc_doc):
    """A raw face SID (describe_part / query_geometry hands these out) is a legal face
    reference; it must resolve to the same face the side word does."""
    from services.mcp.cad_common import cb  # noqa: PLC0415
    matches = cb.call("doc_query", {"doc_id": bc_doc,
                                    "query": {"of": "face", "side": "top"}})["matches"]
    sid = matches[0]["sid"]
    by_word = _peak(_static(bc_doc, fixtures=["bottom"],
                            loads=[{"face": "top", "force": [0, 0, -2000]}]))
    by_sid = _peak(_static(bc_doc, fixtures=["bottom"],
                           loads=[{"face": sid, "force": [0, 0, -2000]}]))
    assert by_sid == by_word, (sid, by_sid, by_word)


def test_mesh_fineness_keyword_equals_the_equivalent_number(bc_doc):
    """mesh_fineness accepts the run_cfd_accurate preset words as well as a number:
    "coarse" is exactly 0.25."""
    by_word = _peak(_static(bc_doc, fixtures=["bottom"],
                            loads=[{"face": "top", "pressure": 0.5}],
                            mesh_fineness="coarse"))
    by_number = _peak(_static(bc_doc, fixtures=["bottom"],
                              loads=[{"face": "top", "pressure": 0.5}],
                              mesh_fineness=0.25))
    assert by_word == by_number, (by_word, by_number)


# ------------------------------------------- the SAME normalizer on every solver entry point
# The live failure was reported against run_fea_static, but the identical rejection lived in
# every other loads/fixtures-taking solver. One helper, so one test each is enough to prove
# the wiring — each asserts a real solver NUMBER, not ok:true.

def test_modal_accepts_the_fixture_wrapper_and_the_fineness_word(bc_doc):
    out = json.loads(_dispatch()("run_fea_modal")(
        fixtures=[{"faces": ["bottom"], "type": "fixed"}], mesh_fineness="coarse",
        n_modes=3, doc_id=bc_doc))
    assert out.get("ok") is True, out
    freqs = out.get("freqs_hz") or []
    assert len(freqs) == 3 and freqs[0] > 0.5, out       # held part, no rigid-body mode


def test_fatigue_accepts_the_polar_plural_load(bc_doc):
    out = json.loads(_dispatch()("run_fea_fatigue")(
        fixtures=["bottom"],
        loads=[{"faces": ["top"], "magnitude": 2000, "direction": [0, 0, -1]}],
        mesh_fineness="coarse", doc_id=bc_doc))
    if out.get("skipped"):
        pytest.skip(f"fatigue backend unavailable: {out.get('reason')}")
    assert out.get("ok") is True, out
    assert float(out["fos_endurance"]) > 0.0, out


def test_transient_thermal_accepts_plural_hot_and_cold_faces(bc_doc):
    out = json.loads(_dispatch()("run_fea_thermal")(
        hot_faces=[{"faces": ["+x"], "temperature": 100}],
        cold_faces=[{"faces": ["-x"], "temperature": 20}],
        transient={"t_end_s": 5, "dt_s": 1}, mesh_fineness="coarse", doc_id=bc_doc))
    if out.get("skipped"):
        pytest.skip(f"thermal backend unavailable: {out.get('reason')}")
    assert out.get("ok") is True, out
    assert float(out["summary"]["max_temp_c"]) == pytest.approx(100.0, abs=1e-6), out


@pytest.fixture(scope="module")
def buckling_column_doc():
    """Real 10x10x200 mm steel column; no external-solver skip is permitted."""
    bind_tool = _dispatch()
    out = json.loads(bind_tool("new_part")(name="buckling_euler_column", fresh=True))
    assert out.get("ok"), out
    doc_id = out["doc_id"]
    out = json.loads(bind_tool("add_features")(doc_id=doc_id, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Column",
            "params": {"length": 10, "width": 10, "height": 200},
        }},
    ]))
    assert out.get("ok"), out
    return doc_id


def test_buckling_real_column_matches_euler_without_ccx(
        buckling_column_doc, monkeypatch, tmp_path):
    """Tet10 fixed-free column must reproduce Euler within 8%, using real CAD."""
    from services.mcp import fea_advanced
    from xml.etree import ElementTree

    monkeypatch.setattr(fea_advanced, "ccx_path", lambda: None)
    vtu = tmp_path / "buckling.vtu"
    out = json.loads(_dispatch()("run_fea_buckling")(
        fixtures=[{"faces": ["bottom"], "type": "fixed"}],
        load=[{"faces": ["top"], "magnitude": 1000, "direction": [0, 0, -1]}],
        material={"E_mpa": 200000, "nu": 0.3},
        doc_id=buckling_column_doc, vtu_path=str(vtu)))
    assert out.get("ok") is True, out
    expected = math.pi**2 * 200000 * (10 * 10**3 / 12) / (2 * 200)**2
    assert out["critical_load_n"] == pytest.approx(expected, rel=0.08), out
    factors = [mode["load_factor"] for mode in out["modes"]]
    assert len(factors) == 4 and factors == sorted(factors), out
    assert all(math.isfinite(factor) and factor > 0 for factor in factors), out
    assert out["critical_load_factor"] == pytest.approx(factors[0]), out
    assert out["critical_load_n"] == pytest.approx(factors[0] * 1000), out
    for mode in out["modes"]:
        participation = mode["participation"]
        assert set(participation) == {"x", "y", "z"}, out
        assert all(math.isfinite(value) and 0 <= value <= 1
                   for value in participation.values()), out
        assert sum(participation.values()) == pytest.approx(1.0), out
    assert out["provenance"]["solver"] == "engine", out
    point_data = ElementTree.parse(vtu).find(".//PointData")
    assert point_data is not None
    for number in range(1, 5):
        field = point_data.find(f"./DataArray[@Name='mode_{number}']")
        assert field is not None
        assert field.get("NumberOfComponents") == "3"
        values = [float(value) for value in field.text.split()]
        assert len(values) == 3 * out["n_nodes"]
        assert max(abs(value) for value in values) == pytest.approx(1.0)


def test_buckling_refuses_tension(buckling_column_doc, monkeypatch):
    """A tensile prestress must not be reported as a positive buckling capacity."""
    from services.mcp import fea_advanced

    monkeypatch.setattr(fea_advanced, "ccx_path", lambda: None)
    out = json.loads(_dispatch()("run_fea_buckling")(
        fixtures=["bottom"], load=[{"face": "top", "force": [0, 0, 1000]}],
        doc_id=buckling_column_doc))
    assert out["ok"] is False and not out.get("skipped"), out
    assert not out.get("modes") and out.get("critical_load_n") is None, out


def test_buckling_search_ceiling_is_not_reported_as_infinite_strength(buckling_column_doc):
    out = json.loads(_dispatch()("run_fea_buckling")(
        fixtures=["bottom"], load=[{"face": "top", "force": [0, 0, -1000]}],
        material={"E_mpa": 200000, "nu": 0.3},
        doc_id=buckling_column_doc, max_load_factor=1.0))
    assert out["ok"] is False and not out.get("skipped"), out
    assert out["max_load_factor"] == 1.0, out
    assert not out.get("modes") and out.get("critical_load_n") is None, out


# ----------------------------------------------- rejected dialects (clear error, no study run)

@pytest.mark.parametrize("loads,needle", [
    ([{"face": "top", "magnitude": 200, "direction": [0, 0, 0]}], "zero length"),
    ([{"faces": [], "force": [0, 0, -100]}], "empty list"),
    ([{"face": "top", "force": [0, 0, -100], "magnitude": 200}], "disagree"),
])
def test_malformed_load_fails_clearly_and_runs_no_study(loads, needle):
    """A load the normalizer cannot read must FAIL naming the accepted shapes — never
    become a silent ok/no-op, and never reach the solver."""
    doc = _block("bc_reject_block")
    out = _static(doc, fixtures=["bottom"], loads=loads)
    assert out.get("ok") is False, out
    err = out["error"]
    assert needle in err, err
    assert "force" in err or "face" in err, err
    assert _n_studies(doc) == 0, "a rejected load must not persist a study"


def test_zero_length_direction_error_names_the_accepted_shapes():
    doc = _block("bc_zerodir_block")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"face": "top", "magnitude": 200, "direction": [0, 0, 0]}])
    assert out.get("ok") is False
    assert "loads[0]" in out["error"] and "force:[fx,fy,fz]" in out["error"], out["error"]


def test_feature_id_as_a_face_names_the_tool_that_lists_faces():
    """A FEATURE id is not a face and the engine query grammar has no sid/owner predicate,
    so the error must say so and name describe_part rather than 'TOKENS, not prose'."""
    doc = _block("bc_featid_block")
    out = _static(doc, fixtures=["bottom"], loads=[{"face": "f4", "force": [0, 0, -100]}])
    assert out.get("ok") is False
    assert "FEATURE id" in out["error"] and "describe_part" in out["error"], out["error"]
    assert _n_studies(doc) == 0


def test_unknown_face_sid_says_so_instead_of_blaming_the_grammar():
    doc = _block("bc_badsid_block")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"face": "Fdeadbe", "force": [0, 0, -100]}])
    assert out.get("ok") is False
    assert "sid" in out["error"] and "describe_part" in out["error"], out["error"]
    assert "TOKENS" not in out["error"], out["error"]


# ------------------------------------------------------------------ unit-level normalizer edges

def test_already_valid_entries_pass_through_unchanged():
    """The payload handed to the C++ solver must stay byte-identical for inputs that
    already worked."""
    loads = [{"face": "top", "force": [0, 0, -500]}, {"face": "+x", "pressure": 0.5}]
    assert _norm_bc_list(loads, "loads") == loads
    assert _norm_bc_list(["bottom", {"of": "face", "side": "top"}], "fixtures",
                         unwrap_query=True) == ["bottom", {"of": "face", "side": "top"}]


def test_fixture_wrapper_collapses_to_the_bare_query():
    assert _norm_bc_list([{"face": "bottom", "type": "fixed"}], "fixtures",
                         unwrap_query=True) == ["bottom"]


def test_load_siblings_survive_the_plural_fan_out():
    out = _norm_bc_list([{"faces": ["a", "b"], "flux_w_m2": 100,
                          "schedule": {"duty": 0.25}}], "loads")
    assert out == [{"face": "a", "flux_w_m2": 100, "schedule": {"duty": 0.25}},
                   {"face": "b", "flux_w_m2": 100, "schedule": {"duty": 0.25}}]


def test_face_and_faces_together_is_ambiguous():
    with pytest.raises(ValueError, match="ambiguous"):
        _norm_bc_list([{"face": "top", "faces": ["bottom"], "pressure": 1}], "loads")


def test_magnitude_without_direction_is_rejected_not_guessed():
    with pytest.raises(ValueError, match="no 'direction'"):
        _norm_bc_list([{"face": "top", "magnitude": 200}], "loads")


def test_pressure_typed_magnitude_becomes_pressure():
    assert _norm_bc_list([{"face": "top", "type": "pressure", "magnitude": 0.5}],
                         "loads") == [{"face": "top", "pressure": 0.5}]


def test_pressure_with_a_direction_is_rejected():
    with pytest.raises(ValueError, match="takes no direction"):
        _norm_bc_list([{"face": "top", "type": "pressure", "magnitude": 0.5,
                        "direction": [0, 0, -1]}], "loads")


def test_agreeing_force_and_magnitude_is_accepted_and_deduplicated():
    assert _norm_bc_list([{"face": "top", "force": [0, 0, -100], "magnitude": 100}],
                         "loads") == [{"face": "top", "force": [0, 0, -100]}]


def test_signed_axis_direction_word():
    assert _norm_direction("-z", "loads[0]") == [0.0, 0.0, -1.0]
    with pytest.raises(ValueError, match="direction"):
        _norm_direction("downish", "loads[0]")


@pytest.mark.parametrize("word,value", [("coarse", 0.25), ("MEDIUM", 0.5),
                                        ("fine", 0.75), ("very fine", 1.0)])
def test_fineness_words(word, value):
    assert _norm_fineness(word) == value


def test_fineness_number_and_numeric_string_unchanged():
    assert _norm_fineness(0.5) == 0.5
    assert _norm_fineness("0.35") == 0.35


def test_fineness_garbage_names_the_vocabulary():
    with pytest.raises(ValueError, match="coarse"):
        _norm_fineness("as fine as you can")


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_fineness_rejects_out_of_range_and_non_finite_values(value):
    with pytest.raises(ValueError, match="mesh_fineness|finite|0..1"):
        _norm_fineness(value)


@pytest.mark.parametrize("value", [[float("nan"), 0, 1], [float("inf"), 0, 1]])
def test_direction_rejects_non_finite_components(value):
    with pytest.raises(ValueError, match="finite"):
        _norm_direction(value, "loads[0]")


def test_direction_object_refuses_fields_that_would_be_ignored():
    with pytest.raises(ValueError, match="exactly x, y, z"):
        _norm_direction({"x": 1, "y": 0, "z": 0, "units": "mm"}, "loads[0]")


def test_positive_integer_guard_does_not_truncate_or_accept_bool():
    for value in (0, -1, 2.5, True, float("nan")):
        with pytest.raises(ValueError, match="positive integer"):
            _positive_int(value, "n_modes")
    assert _positive_int("3", "n_modes") == 3


def test_fixture_wrapper_refuses_fields_or_constraint_types_the_solver_would_ignore():
    with pytest.raises(ValueError, match="unsupported fixture type"):
        _norm_bc_list([{"face": "bottom", "type": "roller"}], "fixtures",
                      unwrap_query=True)
    with pytest.raises(ValueError, match="unsupported fields"):
        _norm_bc_list([{"face": "bottom", "temperature": 20}], "fixtures",
                      unwrap_query=True)


def test_missing_face_is_reported_before_load_shape_validation():
    with pytest.raises(ValueError, match="each load needs a 'face' query") as exc:
        _norm_bc_list([{"name": "tip", "type": "force", "magnitude_n": 500,
                        "direction": [0, 0, -1]}], "loads")
    assert "unsupported load type" not in str(exc.value)


def test_material_requirements_are_analysis_specific():
    steady = sim_tools._resolve_study_material(
        {"name": "thermal-only", "conductivity_w_mk": 1.5}, "thermal_steady")
    assert steady["conductivity_w_mk"] == 1.5

    transient = sim_tools._resolve_study_material(
        {"name": "thermal-only", "density_g_cm3": 2.4},
        "thermal_transient", conductivity_w_mk=1.5, specific_heat_j_kgk=800)
    assert transient["density_g_cm3"] == 2.4
    assert transient["conductivity_w_mk"] == 1.5
    assert transient["cp_j_kgk"] == 800

    with pytest.raises(ValueError, match=r"material\.conductivity_w_mk"):
        sim_tools._resolve_study_material(
            {"name": "missing-thermal-fields"}, "thermal_steady")
    with pytest.raises(ValueError, match=r"material\.cp_j_kgk"):
        sim_tools._resolve_study_material(
            {"name": "missing-cp", "density_g_cm3": 2.4, "k_w_mk": 1.5},
            "thermal_transient")
    with pytest.raises(ValueError, match="E_mpa"):
        sim_tools._resolve_study_material(
            {"name": "not-structural", "conductivity_w_mk": 1.5}, "static")


def test_default_frequency_sweep_includes_only_finite_in_band_modes():
    base = [10.0, 100.0, 2000.0]
    merged = sim_tools._merge_default_frequency_sweep(
        base, [9.0, 55.0, 2001.0, float("nan"), float("inf"), "250"])
    assert merged == [10.0, 55.0, 100.0, 250.0, 2000.0]


def test_mechanical_load_refuses_mixed_models_and_unused_direction():
    with pytest.raises(ValueError, match="both 'force' and 'pressure'"):
        _norm_bc_list([{"face": "top", "force": [0, 0, -1], "pressure": 1}],
                      "loads")
    with pytest.raises(ValueError, match="only meaningful with 'magnitude'"):
        _norm_bc_list([{"face": "top", "force": [0, 0, -1],
                        "direction": [0, 0, -1]}], "loads")


def test_thermal_schedule_rejects_zero_or_ambiguous_excitation():
    assert _validate_thermal_schedule({"duty": 0.0, "period_s": 1.0},
                                      "loads[0].schedule") is False
    with pytest.raises(ValueError, match="exactly one"):
        _validate_thermal_schedule(
            {"duty": 0.5, "points": [[0, 1]], "period_s": 1.0},
            "loads[0].schedule")
    with pytest.raises(ValueError, match="strictly ascending"):
        _validate_thermal_schedule(
            {"points": [[0, 1], [0, 0]], "period_s": 1.0},
            "loads[0].schedule")


def test_geometry_gate_refuses_a_degenerate_body_before_meshing(monkeypatch):
    monkeypatch.setattr(
        sim_tools.cb, "call",
        lambda method, params: {"bodies": [{
            "bbox": {"min": [0, 0, 0], "max": [10, 10, 0]}}]})
    with pytest.raises(ValueError, match="degenerate"):
        sim_tools._require_sim_geometry("doc")


def test_fast_flow_result_guard_rejects_zero_fields_and_input_unit_mismatch():
    result = {
        "converged": True, "drag_n": 1.0, "lift_n": 0.0, "cd": 0.2,
        "frontal_area_mm2": 100.0, "reynolds": 5000.0, "ref_length_mm": 50.0,
        "velocity_ms": 10.0, "residual": -1.0, "voxel_mm": 1.0, "steps_run": 50,
    }
    assert sim_tools._validate_flow_result(result, 10.0)["drag_n"] == 1.0
    with pytest.raises(ValueError, match="zero-field"):
        sim_tools._validate_flow_result({**result, "drag_n": 0.0}, 10.0)
    with pytest.raises(ValueError, match="does not match"):
        sim_tools._validate_flow_result(result, 20.0)


def test_transient_thermal_result_guard_checks_complete_fields_and_summary():
    result = {
        "n_steps": 2, "n_nodes": 2, "n_elems": 1,
        "frames": [
            {"t_s": 1.0, "temperature": [20.0, 30.0]},
            {"t_s": 2.0, "temperature": [25.0, 40.0]},
        ],
        "envelope_max": [25.0, 40.0],
        "summary": {
            "max_temp_c": 40.0, "max_temp_time_s": 2.0,
            "min_temp_c": 20.0, "final_max_temp_c": 40.0,
        },
    }
    checked = sim_tools._validate_transient_thermal_result(result, 2.0)
    assert checked["counts"] == {"n_steps": 2, "n_nodes": 2, "n_elems": 1}
    free_dof = sim_tools._validate_transient_thermal_result(
        {**result, "n_dof": 1}, 2.0)
    assert free_dof["counts"]["n_dof"] == 1
    prescribed = sim_tools._validate_transient_thermal_result(
        {**result, "n_dof": 0}, 2.0)
    assert prescribed["counts"]["n_dof"] == 0
    with pytest.raises(ValueError, match=r"\[0,n_nodes\]"):
        sim_tools._validate_transient_thermal_result({**result, "n_dof": 3}, 2.0)
    with pytest.raises(ValueError, match="1/2 nodal temperatures"):
        sim_tools._validate_transient_thermal_result(
            {**result, "frames": [{"t_s": 2.0, "temperature": [40.0]}]}, 2.0)
    with pytest.raises(ValueError, match="disagrees"):
        sim_tools._validate_transient_thermal_result(
            {**result, "summary": {**result["summary"], "max_temp_c": 99.0}}, 2.0)


def test_static_collector_rejects_negative_or_inconsistent_solver_envelopes():
    ctx = {"doc": "d", "material": {"name": "steel"}}
    negative = sim_tools._collect_static(ctx, {
        "ok": True,
        "summary": {"max_von_mises_mpa": 1.0, "max_disp_mm": -1.0, "min_fos": 2.0},
        "n_dof": 30, "n_elems": 2, "n_nodes": 10,
    }, [])
    assert negative["ok"] is False and "negative" in negative["error"]
    inconsistent = sim_tools._collect_static(ctx, {
        "ok": True,
        "summary": {"max_von_mises_mpa": 1.0, "max_disp_mm": 1.0, "min_fos": 2.0},
        "n_dof": 29, "n_elems": 2, "n_nodes": 10,
    }, [])
    assert inconsistent["ok"] is False and "inconsistent mesh" in inconsistent["error"]


def test_modal_collector_refuses_out_of_order_modes_before_persistence():
    out = sim_tools._collect_modal(
        {"doc": "d", "material": {"name": "steel"}},
        [12.0, 8.0], 2, [])
    assert out["ok"] is False and "ascending order" in out["error"]
    inconsistent = sim_tools._collect_modal(
        {"doc": "d", "material": {"name": "steel"}},
        [8.0, 12.0], 2, [], diagnostics={"n_nodes": 10, "n_dof": 29})
    assert inconsistent["ok"] is False and "inconsistent mesh" in inconsistent["error"]

# ===================================================== boundary-condition ITEM KEYS (live fix)
# Measured live 2026-07-29 (claude-sonnet-4-5, general lane, sim suite). The VERBATIM load
# entry the model wrote, refused with "each load needs a 'face' query":
#   {"name":"tip_load","face_query":"the opposite end face","type":"force",
#    "magnitude_n":500,"direction":[0,0,-1]}
# The SHAPE (polar magnitude+direction) was already supported; only the KEY SPELLINGS missed.
# Only the keys are reproduced verbatim below — "the opposite end face" is prose about the
# live model's cantilever, so these run against this file's block with a resolvable query.

def _await(out: dict) -> dict:
    """Resolve the auto-background handoff for a non-static solver (same reason as _static)."""
    if out.get("skipped"):
        pytest.skip(f"FEA backend unavailable: {out.get('reason')}")
    jid = out.get("job_id")
    if not jid or out.get("freqs_hz") is not None:
        return out
    get_job = _dispatch()("get_job")
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        job = json.loads(get_job(job_id=jid))
        if job.get("status") in ("done", "completed", "error", "failed"):
            return job.get("result") if isinstance(job.get("result"), dict) else job
        time.sleep(1.0)
    pytest.skip(f"backgrounded solve {jid} did not finish in 300 s")


def _call_reconciled(tool: str, **kwargs) -> dict:
    """The built-in agent's REAL dispatch: _bind_tool -> _reconcile_kwargs -> the tool.

    Needed for argument-NAME dialect (num_modes), which is reconciled one layer above the
    tool: calling the bound function directly with num_modes= is just a TypeError and would
    prove nothing about the live path."""
    from services.agent import agent_turn as at  # noqa: PLC0415
    fn = at._bind_tool(tool)
    assert fn is not None, tool
    kw, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    assert err is None, err
    return json.loads(fn(**kw))


def test_live_verbatim_load_keys_solve_and_match_the_canonical_spelling(bc_doc):
    """THE live payload. face_query/magnitude_n must produce the IDENTICAL stress AND
    deflection as face/magnitude — equality between the two spellings is the proof the
    rename changed nothing but the key."""
    live = _static(bc_doc, fixtures=["bottom"],
                   loads=[{"name": "tip_load", "face_query": "top", "type": "force",
                           "magnitude_n": 500, "direction": [0, 0, -1]}])
    canon = _static(bc_doc, fixtures=["bottom"],
                    loads=[{"face": "top", "magnitude": 500, "direction": [0, 0, -1]}])
    vm, vm_c = _peak(live), _peak(canon)
    assert vm == vm_c, (vm, vm_c)
    disp = float(live["summary"]["max_disp_mm"])
    assert disp == float(canon["summary"]["max_disp_mm"]), live["summary"]
    # Physically sane: 500 N on a 100x50x20 steel block is a small-strain elastic case —
    # tens of MPa at most, sub-millimetre, and finite.
    assert math.isfinite(vm) and math.isfinite(disp), (vm, disp)
    assert 0.0 < vm < 250.0, vm     # steel yield is 250 MPa: this case stays elastic
    assert 0.0 < disp < 1.0, disp   # sub-millimetre tip deflection on a 100 mm steel block
    # The rename must be VISIBLE, not silent.
    interp = " ".join(live.get("interpreted") or [])
    assert "'face_query' as 'face'" in interp, live.get("interpreted")
    assert "'magnitude_n' as 'magnitude'" in interp, live.get("interpreted")
    assert "interpreted" not in canon, canon    # canonical call: payload unchanged


def test_the_other_live_entries_force_n_and_the_fixed_fixture_wrapper(bc_doc):
    """The second pair from the same suite: fixtures=[{"face": ..., "type": "fixed"}] and a
    load written with `force_n` (newtons — the same quantity `magnitude` holds)."""
    live = _static(bc_doc, fixtures=[{"face": "bottom", "type": "fixed"}],
                   loads=[{"face": "top", "force_n": 1200, "direction": [0, 0, -1]}])
    canon = _static(bc_doc, fixtures=["bottom"],
                    loads=[{"face": "top", "force": [0.0, 0.0, -1200.0]}])
    assert _peak(live) == _peak(canon), (live["summary"], canon["summary"])
    assert _peak(live) > 0.0
    assert any("'force_n' as 'magnitude'" in n for n in live["interpreted"]), live


def test_a_load_with_no_face_reference_at_all_is_still_refused(bc_doc):
    """The renames must not have created a load that solves without a face. This entry has
    every OTHER key of the live payload and no face reference of any spelling."""
    doc = _block("bc_nofaceref_block")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"name": "tip_load", "type": "force", "magnitude_n": 500,
                          "direction": [0, 0, -1]}])
    assert out.get("ok") is False, out
    err = out["error"]
    assert "needs a 'face' query" in err, err
    assert 'accepted shapes' in err and '"force": [fx,fy,fz]' in err, err
    assert _n_studies(doc) == 0, "a refused load must not persist a study"


def test_an_unknown_item_key_is_refused_and_names_the_real_vocabulary():
    """No catch-all. `safety_factor` is not a spelling of anything a BC entry carries, and
    silently dropping it would hide a setting the caller believed it had applied."""
    doc = _block("bc_unknownkey_block")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -100], "safety_factor": 2}])
    assert out.get("ok") is False, out
    err = out["error"]
    assert "unknown key 'safety_factor'" in err, err
    assert "'magnitude'" in err and "'pressure'" in err, err   # the real vocabulary
    assert _n_studies(doc) == 0


def test_direction_normal_is_refused_and_demands_an_explicit_vector():
    """DELIBERATE REFUSAL, not a default. "normal" is a WORD: it is unresolvable at the tool
    boundary (no face resolved yet) and ambiguous in SIGN even with the face in hand
    (outward = tension, inward = compression). Guessing an axis would solve a different
    load and report success."""
    doc = _block("bc_normaldir_block")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force_n": 1200, "direction": "normal"}])
    assert out.get("ok") is False, out
    err = out["error"]
    assert "FACE-RELATIVE word, not a vector" in err, err
    assert "[dx,dy,dz]" in err, err                    # it demands a vector
    assert "SIGN" in err and "pressure" in err, err    # and says why, plus the real shape
    assert _n_studies(doc) == 0
    for word in ("inward", "outward", "face normal", "perpendicular"):
        with pytest.raises(ValueError, match="FACE-RELATIVE"):
            _norm_direction(word, "loads[0]")


def test_modal_num_modes_returns_that_many_ascending_positive_frequencies(bc_doc):
    """Live 2026-07-29: run_fea_modal(num_modes=3) was refused as an unknown argument.
    `num_modes` is the English spelling of `n_modes` — the same COUNT — so it now binds,
    and the proof is three real frequencies."""
    out = _await(_call_reconciled("run_fea_modal", fixtures=["bottom"],
                                  num_modes=3, mesh_fineness="coarse", doc_id=bc_doc))
    assert out.get("ok") is True, out
    freqs = out.get("freqs_hz") or []
    assert len(freqs) == 3, out
    assert all(f > 0.5 for f in freqs), freqs          # held part: no rigid-body mode
    assert freqs == sorted(freqs), freqs               # ascending, lowest first
    canon = _await(json.loads(_dispatch()("run_fea_modal")(
        fixtures=["bottom"], n_modes=3, mesh_fineness="coarse", doc_id=bc_doc)))
    assert canon["freqs_hz"] == freqs, (canon["freqs_hz"], freqs)


# ----------------------------------------------------------- unit-level item-key normalizer

def test_item_keys_are_renamed_and_the_rename_is_reported():
    notes: list = []
    out = _norm_bc_list([{"name": "tip_load", "face_query": "top", "type": "force",
                          "magnitude_n": 500, "direction": [0, 0, -1]}], "loads", notes=notes)
    assert out == [{"name": "tip_load", "face": "top", "force": [0.0, 0.0, -500.0]}], out
    assert notes == ["loads[0]: read 'face_query' as 'face', "
                     "'magnitude_n' as 'magnitude'"], notes


def test_kind_alias_on_a_face_wrapper_is_not_misclassified_as_a_raw_query():
    notes: list = []
    out = _norm_bc_list(
        [{"face": "top", "kind": "force", "magnitude": 10, "direction": "-z"}],
        "loads", notes=notes)
    assert out == [{"face": "top", "force": [0.0, 0.0, -10.0]}]
    assert notes == ["loads[0]: read 'kind' as 'type'"]



def test_canonical_item_keys_are_case_and_separator_insensitive():
    notes: list = []
    out = _norm_bc_list(
        [{"Face": "top", "Force": [0, 0, -10], "Name": "case-load"}],
        "loads", notes=notes)
    assert out == [{"face": "top", "force": [0.0, 0.0, -10.0],
                    "name": "case-load"}]
    assert notes

def test_a_raw_geometry_query_dict_is_never_key_checked():
    """A query / query-result match dict goes straight to the engine grammar. It carries
    keys this vocabulary knows nothing about ("of", "surf", "kind", "centroid"), so touching
    it would break every structured fixture query."""
    q = {"of": "face", "side": "top", "pick": "largest"}
    m = {"kind": "face", "surf": "plane", "centroid": [0, 0, 5], "area_or_len": 2400,
         "hint_index": 0}
    assert _norm_bc_list([q, m], "fixtures", unwrap_query=True) == [q, m]
    assert _canon_item_keys(q, "fixtures[0]") is q


def test_two_spellings_of_the_same_slot_together_is_ambiguous():
    with pytest.raises(ValueError, match="another spelling"):
        _norm_bc_list([{"face": "top", "face_query": "bottom", "pressure": 1}], "loads")
    with pytest.raises(ValueError, match="another spelling"):
        _norm_bc_list([{"face": "top", "magnitude": 500, "magnitude_n": 500,
                        "direction": [0, 0, -1]}], "loads")


def test_load_type_is_deliberately_not_aliased_onto_type():
    """run_fea_fatigue's top-level `load_type` names the cyclic SPECTRUM
    ("reversed"/"zero_based"), a different thing from a BC kind. Aliasing it onto the item's
    `type` would silently drop the spectrum the caller asked for, so it stays a refusal."""
    with pytest.raises(ValueError, match="unknown key 'load_type'"):
        _norm_bc_list([{"face": "top", "force": [0, 0, -100], "load_type": "reversed"}],
                      "loads")


def test_descriptive_item_labels_ride_along_instead_of_being_refused():
    """The live payload carried "name":"tip_load". A cosmetic label can never change what
    the solver computes, so refusing the whole entry over it is the worse failure."""
    out = _norm_bc_list([{"face": "top", "pressure": 0.5, "name": "top_pressure",
                          "comment": "from the spec"}], "loads")
    assert out == [{"face": "top", "pressure": 0.5, "name": "top_pressure",
                    "comment": "from the spec"}]


def test_thermal_and_plural_spellings_normalize_too():
    notes: list = []
    assert _norm_bc_list([{"faces_query": ["a", "b"], "temperature_c": 100}], "hot_faces",
                         notes=notes) == [{"face": "a", "temperature": 100},
                                          {"face": "b", "temperature": 100}]
    assert notes and "'faces_query' as 'faces'" in notes[0], notes
    assert _norm_bc_list([{"surface": "top", "heat_flux_w_m2": 5000}], "loads") == [
        {"face": "top", "flux_w_m2": 5000}]
    assert _norm_bc_list([{"face": "left", "htc_w_m2k": 25, "t_amb": 20}], "loads") == [
        {"face": "left", "h_w_m2k": 25, "t_amb_c": 20}]


def test_a_canonical_entry_is_still_returned_byte_identical_with_no_note():
    notes: list = []
    loads = [{"face": "top", "force": [0, 0, -500]}, {"face": "+x", "pressure": 0.5}]
    assert _norm_bc_list(loads, "loads", notes=notes) == loads
    assert notes == []


# ============================================================ asynchronous solve jobs
# services/sim/jobs.py + the sim_run / sim_status / sim_cancel / sim_results tools.
# Every assertion here is a VALUE or an OS fact, never an `ok:true`:
#   * the async answer must equal the synchronous answer to the last digit (proof the
#     physics did not change),
#   * a cancelled job's pid must be gone according to the kernel (proof the solver stopped,
#     not that a flag flipped),
#   * a partial result must carry its partial label and a full result must be REFUSED while
#     the solve is unfinished.

import os  # noqa: E402
import pathlib  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from services.sim import jobs as simjobs  # noqa: E402

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# A medium mesh is slow enough to distinguish a real asynchronous return from a blocking
# solve while staying below the standard 3 GB resguard budget on a busy workstation.
_ASYNC_FINENESS = 0.6


@pytest.fixture(autouse=True, scope="module")
def _isolated_job_registry(tmp_path_factory):
    """Never spool test jobs into the user's real data root."""
    d = tmp_path_factory.mktemp("sim_jobs")
    prev = os.environ.get("CHUDWARE_SIM_JOBS_DIR")
    os.environ["CHUDWARE_SIM_JOBS_DIR"] = str(d)
    yield d
    if prev is None:
        os.environ.pop("CHUDWARE_SIM_JOBS_DIR", None)
    else:
        os.environ["CHUDWARE_SIM_JOBS_DIR"] = prev


def _tool(name: str):
    return _dispatch()(name)


def _await_state(job_id: str, wanted=("done", "failed", "cancelled"), timeout_s: float = 300.0):
    deadline = time.monotonic() + timeout_s
    st: dict = {}
    while time.monotonic() < deadline:
        st = json.loads(_tool("sim_status")(job_id=job_id))
        if st.get("state") in wanted:
            return st
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {wanted}: {st}")


def _proc_gone(pid: int) -> bool:
    """True when the kernel has no LIVE process at `pid` — a zombie (already dead, merely
    unreaped) counts as gone, a running process does not."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat[stat.rindex(")") + 2:].split()[0] == "Z"


def test_sim_run_returns_a_job_id_immediately_and_matches_the_synchronous_answer():
    """The whole point of the async path, proved with two numbers: the START call must
    return in a small fraction of the solve's own runtime, and the RESULT must be the
    identical stress field the blocking tool returns for the identical model."""
    doc = _block("async_static")
    fixtures = ["bottom"]
    loads = [{"face": "top", "force": [0, 0, -500]}]

    t0 = time.monotonic()
    sync = _static(doc, fixtures=fixtures, loads=loads, mesh_fineness=_ASYNC_FINENESS)
    t_sync = time.monotonic() - t0
    peak = _peak(sync)

    t1 = time.monotonic()
    started = json.loads(_tool("sim_run")(analysis="fea_static", doc_id=doc,
                                          fixtures=fixtures, loads=loads, material="steel",
                                          mesh_fineness=_ASYNC_FINENESS))
    t_start = time.monotonic() - t1
    assert started.get("ok") and started.get("job_id"), started
    assert t_start < max(2.0, t_sync / 4.0), (
        f"sim_run blocked for {t_start:.2f}s against a {t_sync:.2f}s solve — it waited")

    st = _await_state(started["job_id"])
    assert st["state"] == "done", st
    res = json.loads(_tool("sim_results")(job_id=started["job_id"]))
    assert res.get("ok") is True and res.get("converged") is True, res
    assert res["summary"]["max_von_mises_mpa"] == pytest.approx(peak, rel=1e-12), (
        res["summary"], sync["summary"])
    assert res["summary"]["max_disp_mm"] == pytest.approx(
        sync["summary"]["max_disp_mm"], rel=1e-12)
    assert res["n_dof"] == sync["n_dof"]


def test_sim_cancel_actually_kills_the_solver_process():
    """A cancel that leaves the solver running is worse than no cancel. Assert TERMINATION
    against the kernel (no live pid, no live process group), not against our own bookkeeping."""
    doc = _block("async_cancel")
    started = json.loads(_tool("sim_run")(analysis="fea_static", doc_id=doc,
                                          fixtures=["bottom"],
                                          loads=[{"face": "top", "force": [0, 0, -500]}],
                                          material="steel", mesh_fineness=1.0))
    assert started.get("ok"), started
    jid, pid = started["job_id"], started["pid"]
    # Let the worker really get into the solve, so we are killing work in flight.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not simjobs.partial(jid):
        time.sleep(0.1)
    assert not _proc_gone(pid), "worker died on its own — nothing to prove about cancel"

    t0 = time.monotonic()
    out = json.loads(_tool("sim_cancel")(job_id=jid))
    t_cancel = time.monotonic() - t0
    assert out.get("terminated") is True, out
    assert out.get("pid_alive_after") is False and out.get("group_alive_after") is False, out
    assert out["state"] == "cancelled"
    assert t_cancel < 5.0, f"cancel took {t_cancel:.2f}s"
    # Independent of the tool's own reply: ask the OS.
    assert _proc_gone(pid), f"pid {pid} is still alive after sim_cancel"
    assert json.loads(_tool("sim_status")(job_id=jid))["state"] == "cancelled"
    # And the refusal still holds afterwards: a cancelled job has no results.
    res = json.loads(_tool("sim_results")(job_id=jid))
    assert res["ok"] is False and res["state"] == "cancelled", res


def test_partial_results_are_labelled_and_full_results_are_refused_until_converged():
    from services.sim.job_worker import _Progress

    # This tests collection semantics, not how quickly a child reaches its first
    # iteration. Publish the two real channel shapes in a controlled RUNNING job.
    jid = "sim-" + uuid.uuid4().hex[:12]
    directory = simjobs.job_dir(jid)
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with simjobs._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, "selftest", "selftest_sleep", simjobs.RUNNING, os.getpid(),
             simjobs._proc_key(os.getpid()), now, now, 0, "{}"))
    progress = _Progress(directory, jid)
    try:
        progress.emit({"phase": "starting"})
        initial = json.loads(_tool("sim_results")(job_id=jid, partial=True))
        assert initial["partial"] is True and initial["converged"] is False, initial
        assert initial["snapshot"]["phase"] == "starting", initial
        assert "pct" not in initial["snapshot"] and "summary" not in initial, initial
        progress.emit({"phase": "iteration", "pct": 50.0, "elements": 128})

        full = json.loads(_tool("sim_results")(job_id=jid))
        assert full["ok"] is False and full["state"] == "running", full
        assert "summary" not in full and "snapshot" not in full, full

        part = json.loads(_tool("sim_results")(job_id=jid, partial=True))
        assert part["ok"] is True and part["partial"] is True, part
        assert part["converged"] is False, part
        snap = part["snapshot"]
        assert snap["partial"] is True and snap["converged"] is False, snap
        assert 0 < snap["pct"] < 100, snap
    finally:
        simjobs._set(jid, state=simjobs.CANCELLED, pid=None, proc_key=None)


def test_a_stage_with_no_partial_channel_says_so_instead_of_faking_one():
    """contract rule 1: a check that cannot run must say so, not answer."""
    job = simjobs.start("selftest", "selftest_sleep", {"seconds": 0.2, "steps": 1})
    jid = job["job_id"]
    _await_state(jid, timeout_s=30.0)
    (simjobs.job_dir(jid) / "partial.json").unlink()  # a stage that published nothing
    out = json.loads(_tool("sim_results")(job_id=jid, partial=True))
    assert out["ok"] is False and out["partial"] is True, out
    assert "no partial results exist" in out["error"], out
    assert "snapshot" not in out, out


def test_an_unknown_job_id_explains_the_registry_instead_of_a_bare_not_found():
    st = json.loads(_tool("sim_status")(job_id="sim-neverissued00"))
    assert st["ok"] is False and st["state"] == "unknown", st
    assert "PERSISTED" in st["error"] and "sim_jobs()" in st["error"], st


def test_a_job_started_by_another_process_is_still_collectable_here():
    """Agent turns fork a fresh services.worker_main, so an in-memory registry would make a
    job started in turn N unreachable in turn N+1. Start the job in a DIFFERENT interpreter
    and collect it here — that is the turn boundary, reproduced."""
    code = ("import sys, json; sys.path.insert(0, '.');"
            "from services.sim import jobs as J;"
            "print(json.dumps(J.start('selftest', 'selftest_sleep',"
            " {'seconds': 1.0, 'steps': 4})))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO_ROOT),
                          capture_output=True, text=True, env=dict(os.environ), timeout=60)
    assert proc.returncode == 0, proc.stderr[-800:]
    jid = json.loads(proc.stdout.strip().splitlines()[-1])["job_id"]

    st = _await_state(jid, timeout_s=60.0)
    assert st["state"] == "done", st
    assert simjobs.raw_result(jid)["steps"] == 4
    listed = json.loads(_tool("sim_jobs")(limit=50))
    assert jid in [j["job_id"] for j in listed["jobs"]], listed


# --------------------------------------------------- the zero-input study (2026-07-30)
# Measured driving the bracket flow ("will this hold 200 N?") end to end: a static study
# with loads=[] or force=[0,0,0] solved happily and reported max_von_mises_mpa 0.0,
# max_disp_mm 0.0 and min_fos 15.0 — the FoS saturation cap, i.e. the most reassuring
# number the surface can produce, from a study that was never asked a question. Fatigue
# reported regime "infinite" the same way, and buckling silently substituted a 1 N load.


def test_a_static_study_with_no_load_is_refused_not_solved_as_zero(bc_doc):
    """The core defect: loads=[] used to come back ok:true / min_fos 15.0. Refuse at setup,
    name the missing input, and say why zero is not an answer."""
    out = json.loads(_dispatch()("run_fea_static")(doc_id=bc_doc, fixtures=["bottom"],
                                                   loads=[]))
    assert out["ok"] is False, out
    assert "no non-zero load" in out["error"], out
    assert "summary" not in out, out


def test_a_static_load_whose_magnitude_is_zero_is_the_same_refusal(bc_doc):
    """force=[0,0,0] is a well-formed load entry that drives nothing — the shape check
    passed it straight through to a solve whose every output was 0."""
    out = json.loads(_dispatch()("run_fea_static")(
        doc_id=bc_doc, fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, 0]}]))
    assert out["ok"] is False and "no non-zero load" in out["error"], out


def test_a_zero_pressure_load_is_refused_too(bc_doc):
    """The pressure branch of the same guard — a 0 MPa pressure is not a load."""
    out = json.loads(_dispatch()("run_fea_static")(
        doc_id=bc_doc, fixtures=["bottom"], loads=[{"face": "top", "pressure": 0.0}]))
    assert out["ok"] is False and "no non-zero load" in out["error"], out


def test_one_real_load_beside_a_zero_one_still_solves(bc_doc):
    """The guard is 'nothing drives this study', NOT 'every entry must be non-zero' — a
    zero entry alongside a real one must not cost the caller its solve."""
    out = _static(bc_doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, 0]},
                         {"face": "top", "force": [0, 0, -500]}])
    assert out["ok"] is True, out
    assert out["summary"]["max_von_mises_mpa"] > 0.0, out


def test_empty_fixtures_is_diagnosed_as_empty_not_as_a_bad_query(bc_doc):
    """fixtures=[] and 'the query matched nothing' are different mistakes; the single old
    message told a caller who supplied NO queries to go check its queries."""
    out = json.loads(_dispatch()("run_fea_static")(
        doc_id=bc_doc, fixtures=[], loads=[{"face": "top", "force": [0, 0, -500]}]))
    assert out["ok"] is False, out
    assert "fixtures=[]" in out["error"] and "rigid-body" in out["error"], out
    assert "query_geometry" not in out["error"], out


def test_the_async_lane_refuses_the_zero_load_study_before_starting_a_job(bc_doc):
    """sim_run shares _build_static_study, so the refusal must land at SETUP — a job_id for
    a study that cannot mean anything is worse than a refusal."""
    out = json.loads(_dispatch()("sim_run")(doc_id=bc_doc, analysis="fea_static",
                                            fixtures=["bottom"], loads=[]))
    assert out["ok"] is False and "no non-zero load" in out["error"], out
    assert "job_id" not in out, out


def test_fatigue_with_no_load_no_longer_reports_infinite_life(bc_doc):
    """The most dangerous instance: ok:true, regime "infinite" == "this part never fails"."""
    out = json.loads(_dispatch()("run_fea_fatigue")(doc_id=bc_doc, fixtures=["bottom"],
                                                    loads=[]))
    assert out["ok"] is False, out
    assert "no non-zero load" in out["error"], out
    assert out.get("regime") is None, out


def test_a_static_reply_carries_the_mesh_it_solved_on(bc_doc):
    """n_elems / n_nodes come back from the engine and used to be dropped, leaving the
    caller no way to judge whether the mesh was fine enough."""
    out = _static(bc_doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -500]}])
    assert out["n_elems"] > 0 and out["n_nodes"] > 0, out
    assert out["n_dof"] >= 3 * out["n_nodes"], out


def test_a_saturated_factor_of_safety_says_it_is_a_cap(bc_doc):
    """min_fos clamps at 15 in the solver, so 15 must not read as a computed margin."""
    out = _static(bc_doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -1.0]}])
    assert out["ok"] is True, out
    if out["summary"]["min_fos"] >= 15.0:
        assert "saturation cap" in out["fos_note"], out


def test_buckling_refuses_a_zero_load_instead_of_substituting_one_newton(bc_doc):
    """`P = max(1.0, |F|)` reported applied_load_n 1.0 and a full critical-load table for a
    load the caller never supplied — the surviving half of the hard-coded-1000-N defect."""
    out = json.loads(_dispatch()("run_fea_buckling")(
        doc_id=bc_doc, fixtures=["bottom"], load=[{"face": "top", "force": [0, 0, 0]}]))
    assert out["ok"] is False, out
    assert not out.get("modes") and out.get("critical_load_n") is None, out


def test_a_transient_thermal_run_with_no_thermal_input_is_refused(bc_doc):
    """No Dirichlet face and no flux/convection load: C dT/dt + K T = 0 from a uniform
    t_init has the constant solution, so the reply was just the input handed back."""
    out = json.loads(_dispatch()("run_fea_thermal")(
        doc_id=bc_doc, hot_faces=[], cold_faces=[],
        transient={"t_end_s": 10, "dt_s": 1}))
    assert out["ok"] is False, out
    assert "no thermal input" in out["error"], out


def test_a_doubled_load_doubles_the_linear_static_answer(bc_doc):
    """A result that does not respond to its input is not a solve. Linear statics is exactly
    proportional, so 2x load must give 2x stress AND 2x deflection."""
    one = _static(bc_doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -200]}])
    two = _static(bc_doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -400]}])
    assert one["summary"]["max_von_mises_mpa"] > 0.0, one
    assert two["summary"]["max_von_mises_mpa"] == pytest.approx(
        2.0 * one["summary"]["max_von_mises_mpa"], rel=1e-3)
    assert two["summary"]["max_disp_mm"] == pytest.approx(
        2.0 * one["summary"]["max_disp_mm"], rel=1e-3)


def test_swapping_steel_for_aluminium_changes_the_stiffness_it_solved_with(bc_doc):
    """Silent material drop is the rib/thread defect class. Same load, same mesh: deflection
    must scale as E_steel/E_alu (~2.9), and stress must NOT move (statically determinate)."""
    steel = _static(bc_doc, fixtures=["bottom"], material="steel",
                    loads=[{"face": "top", "force": [0, 0, -200]}])
    alu = _static(bc_doc, fixtures=["bottom"], material="aluminum",
                  loads=[{"face": "top", "force": [0, 0, -200]}])
    ratio = alu["summary"]["max_disp_mm"] / steel["summary"]["max_disp_mm"]
    assert 2.4 < ratio < 3.4, (ratio, steel["summary"], alu["summary"])
    assert alu["summary"]["max_von_mises_mpa"] == pytest.approx(
        steel["summary"]["max_von_mises_mpa"], rel=0.05)


# ===================================================== durable (out-of-process) solve jobs
# Every heavy run_fea_* / run_cfd_* path now runs its SOLVE in a child process tracked by
# the SQLite registry in services/sim/jobs.py. These tests are engine-free on purpose: what
# they pin is the CONTRACT between the three stages — the payload the child receives, the
# job id the agent is handed, and the fact that collect (which writes a study into the
# document) runs exactly once no matter how many times, or from how many processes, a
# finished job is polled.

import uuid  # noqa: E402

from services.mcp import cad_common  # noqa: E402
from services.mcp import cad_doc_tools  # noqa: E402
from services.sim import jobs as simjobs  # noqa: E402


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """An isolated job spool, so a test can never see (or prune) a real solve."""
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path / "sim_jobs"))
    simjobs._LOCAL_PROCS.clear()
    return tmp_path


@pytest.fixture()
def started(monkeypatch):
    """Capture what would have been spawned instead of spawning it."""
    seen: list = []

    def fake_start(kind, stage, payload, meta=None):
        seen.append({"kind": kind, "stage": stage, "payload": payload, "meta": meta or {}})
        return {"job_id": f"sim-{len(seen):012x}", "state": "running", "pid": 4242,
                "kind": kind, "stage": stage, "started_at": 0.0}

    monkeypatch.setattr(simjobs, "start", fake_start)
    return seen


@pytest.fixture()
def tools():
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    mcp = FastMCP("test-durable-sim")
    sim_tools.register_cad_sim_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


@pytest.fixture()
def get_job_tool():
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    mcp = FastMCP("test-durable-get-job")
    cad_doc_tools.register_cad_doc_tools(mcp)
    return mcp._tool_manager._tools["get_job"].fn


_MATERIAL = {"name": "Steel", "E_mpa": 200000.0, "nu": 0.29,
             "density_g_cm3": 7.85, "yield_mpa": 250.0, "ultimate_mpa": 400.0}


def _static_ctx(tmp_path) -> dict:
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;", encoding="utf-8")
    return {"analysis": "fea_static", "doc": "doc-1", "material": _MATERIAL,
            "material_arg": "steel", "fixtures": ["bottom"],
            "loads": [{"face": "top", "force": [0.0, 0.0, -200.0]}],
            "mesh_fineness": 0.5, "step_path": str(step),
            "study": {"step_path": str(step), "material": _MATERIAL,
                      "fixtures": [{"nodes": [0, 1, 2]}], "mesh_order": 2,
                      "fineness": 0.5,
                      "loads": [{"type": "force", "vector": [0.0, 0.0, -200.0],
                                 "faces": [{"nodes": [3, 4, 5]}]}]}}


def test_background_static_hands_back_a_durable_job_not_a_thread(
        tmp_path, monkeypatch, registry, started, tools):
    """The whole point of the move: the id the agent gets back names a CHILD PROCESS in an
    on-disk registry, so it is still meaningful after this server is gone."""
    ctx = _static_ctx(tmp_path)
    monkeypatch.setattr(sim_tools, "_build_static_study",
                        lambda *a, **kw: ctx)
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)
    out = json.loads(tools["run_fea_static"](fixtures=["bottom"], loads=[], background=True))
    assert out["ok"] and out["status"] == "running" and out["doc_id"] == "doc-1", out
    assert out["job_id"].startswith("sim-"), out
    assert out["cancel_supported"] is True, out

    job = started[0]
    assert (job["kind"], job["stage"]) == ("fea_static", "core_rpc"), job
    assert job["payload"]["method"] == "sim_run_static", job
    # The payload references only files on disk — nothing in it points back at this process.
    assert job["payload"]["params"]["study"]["step_path"] == ctx["step_path"]
    # The COLLECT stage's whole context travels with the job, minus the bulky study.
    assert "study" not in job["meta"] and job["meta"]["doc"] == "doc-1", job["meta"]
    assert job["meta"]["material"] == _MATERIAL and job["meta"]["notes"] == []


def test_adaptive_sync_metadata_overrides_the_static_setup_analysis(
        tmp_path, monkeypatch, registry, tools):
    """Both the immediate wait and a later reconnect select collectors from job metadata."""
    base = _static_ctx(tmp_path)
    monkeypatch.setattr(sim_tools, "_build_static_study",
                        lambda *a, **kw: dict(base))
    ctx = sim_tools._build_adaptive_study(
        ["bottom"], [{"face": "top", "force": [0.0, 0.0, -200.0]}], "steel", 0.5,
        3, 0.05, False, 0, "doc-1", [])
    assert ctx["analysis"] == "fea_adaptive"
    assert ctx["study"]["adapt"] == {"max_passes": 3, "eta_target": 0.05}

    captured: dict = {}

    def fake_sync(kind, stage, payload, meta):
        captured.update(kind=kind, stage=stage, payload=payload, meta=meta)
        return {"ok": True, "collector_probe": True}

    monkeypatch.setattr(sim_tools, "_build_adaptive_study", lambda *a, **kw: ctx)
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)
    monkeypatch.setattr(sim_tools, "solve_durable_or_handoff", fake_sync)
    out = json.loads(tools["run_fea_adaptive"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}]))
    assert out["ok"] and out["collector_probe"], out
    assert (captured["kind"], captured["stage"]) == ("fea_adaptive", "core_rpc")
    assert captured["meta"]["analysis"] == "fea_adaptive"
    assert captured["payload"]["method"] == "sim_run_adaptive"


def test_modal_widening_lives_in_the_stage_so_both_paths_solve_the_same_physics(
        tmp_path, monkeypatch, registry, started, tools):
    """A free-free part's 0 Hz cluster is six-fold degenerate and the tool has always
    widened the subspace and re-solved. run_fea_modal and sim_run must hand the child the
    SAME instruction, or the async answer is a second, weaker physics."""
    step = tmp_path / "modal.step"
    step.write_text("ISO-10303-21;", encoding="utf-8")
    ctx = {"analysis": "fea_modal", "doc": "doc-2", "material": _MATERIAL,
           "material_arg": "steel", "fixtures": [], "fixture_sets": [], "free_free": True,
           "mesh_fineness": 0.5, "step_path": str(step),
           "study": {"step_path": str(step), "material": _MATERIAL, "fixtures": [],
                     "mesh_order": 1, "fineness": 0.5}}
    monkeypatch.setattr(sim_tools, "_build_modal_study", lambda *a, **kw: dict(ctx))
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)

    json.loads(tools["run_fea_modal"](fixtures=[], n_modes=6, background=True))
    json.loads(tools["sim_run"](analysis="fea_modal", fixtures=[], n_modes=6))
    assert [j["stage"] for j in started] == ["modal_rpc", "modal_rpc"], started
    assert started[0]["payload"] == started[1]["payload"], started
    assert started[0]["payload"]["widen_free_free"] is True
    assert started[0]["payload"]["solve_n"] == sim_tools._modal_solve_n(6, True)


def test_cfd_flow_payload_carries_its_geometry_and_a_spool_relative_progress_file(
        tmp_path, monkeypatch, registry, started, tools):
    """sim_run_flow tessellates a DOCUMENT, and the child shares none — so the solid ships
    as a file. Its live progress file has to live in the job's own spool: a caller-minted
    temp path is unreachable to the later process that reconnects to the job."""
    exported: list = []

    def fake_call(method, params=None):
        params = params or {}
        if method == "doc_export_step":
            exported.append(params["path"])
            open(params["path"], "w", encoding="utf-8").write("ISO-10303-21;")
            return {"ok": True}
        raise AssertionError(f"unexpected engine call {method!r}")

    monkeypatch.setattr(sim_tools.cb, "call", fake_call)
    monkeypatch.setattr(sim_tools, "_doc", lambda doc_id: "doc-3")
    monkeypatch.setattr(sim_tools, "_require_sim_geometry", lambda d: [{}])
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)

    out = json.loads(tools["run_cfd_flow"](velocity=12.0, cells=48, n_steps=800,
                                           background=True))
    assert out["job_id"].startswith("sim-"), out
    job = started[0]
    assert (job["kind"], job["stage"]) == ("cfd_flow", "core_rpc"), job
    assert job["payload"]["method"] == "sim_run_flow"
    assert job["payload"]["open_step"] == exported[0], job["payload"]
    assert job["payload"]["progress_file"] == simjobs.PROGRESS_TOKEN
    assert job["payload"]["params"]["progress_path"] == simjobs.PROGRESS_TOKEN
    # doc_id is filled by the stage from the re-opened solid, never carried across.
    assert "doc_id" not in job["payload"]["params"], job["payload"]
    assert job["meta"]["velocity"] == 12.0 and job["meta"]["doc"] == "doc-3"


def test_openfoam_runs_on_its_own_stage_so_cancel_reaches_the_mesher(
        tmp_path, monkeypatch, registry, started, tools):
    """OpenFOAM is minutes of subprocesses. They belong to the JOB's process group, which
    is the only way sim_cancel stops a snappyHexMesh instead of orphaning it."""
    def fake_call(method, params=None):
        params = params or {}
        if method == "export_file":
            open(params["path"], "w", encoding="utf-8").write("solid x\nendsolid x\n")
            return {"ok": True}
        raise AssertionError(f"unexpected engine call {method!r}")

    monkeypatch.setattr(sim_tools.cb, "call", fake_call)
    monkeypatch.setattr(sim_tools, "_doc", lambda doc_id: "doc-4")
    monkeypatch.setattr(sim_tools, "_require_sim_geometry", lambda d: [{}])
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)
    monkeypatch.setattr("services.sim.foam_runner.capabilities",
                        lambda: {"openfoam": True, "openfoam_kind": "native",
                                 "openfoam_version": "v2412"})

    out = json.loads(tools["run_cfd_accurate"](velocity=15.0, preset="coarse"))
    assert out["job_id"].startswith("sim-") and out["preset"] == "coarse", out
    assert out["solver"] == "v2412", out
    job = started[0]
    assert (job["kind"], job["stage"]) == ("cfd_openfoam", "openfoam_tunnel"), job
    assert job["payload"]["speed_mps"] == 15.0 and job["payload"]["preset"] == "coarse"
    assert job["payload"]["fluid"] == {"nu": 1.5e-5, "rho": 1.225}
    assert job["payload"]["stl_path"].endswith(".stl")


def _finished_job(kind: str, meta: dict, raw: dict) -> str:
    """A DONE row plus the result artifact, exactly as a finished child leaves them."""
    job_id = "sim-" + uuid.uuid4().hex[:12]
    directory = simjobs.job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(
        json.dumps({"result": raw, "done_at": time.time()}), encoding="utf-8")
    now = time.time()
    with simjobs._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,created_at,"
            "started_at,done_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, kind, "core_rpc", simjobs.DONE, None, None, None, now, now, now,
             0, json.dumps(meta)))
    return job_id


_STATIC_RAW = {"ok": True, "n_dof": 300, "n_elems": 40, "n_nodes": 100,
               "mesh_type_used": "tet10",
               "summary": {"max_von_mises_mpa": 12.5, "max_disp_mm": 0.04, "min_fos": 20.0}}


def _adaptive_raw(target_met: bool) -> dict:
    eta = 0.03 if target_met else 0.08
    stop = "eta_target_met" if target_met else "pass_budget_exhausted"
    return {
        **_STATIC_RAW,
        "adapt": {
            "passes": [
                {"pass": 1, "global_eta": 0.12, "n_elems": 20},
                {"pass": 2, "global_eta": eta, "n_elems": 40},
            ],
            "passes_run": 2,
            "global_eta": eta,
            "eta_target": 0.05,
            "eta_target_met": target_met,
            "stop_reason": stop,
        },
    }


def test_reconnected_adaptive_job_uses_adaptive_collector_and_keeps_convergence_metadata(
        tmp_path, monkeypatch, registry):
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-adaptive")
    job_id = _finished_job(
        "fea_adaptive",
        {"analysis": "fea_adaptive", "doc": "doc-adaptive", "material": _MATERIAL,
         "material_arg": "steel", "notes": []},
        _adaptive_raw(True))

    out = cad_common.collect_durable(job_id)
    assert out["ok"] is True and out["converged"] is True, out
    assert out["global_eta"] == 0.03 and out["eta_target"] == 0.05
    assert out["passes_run"] == 2 and len(out["passes"]) == 2
    assert out["stop_reason"] == "eta_target_met"
    assert "final refined mesh" in out["resolution_note"]
    assert len(persisted) == 1 and persisted[0]["type"] == "fea_adaptive", persisted
    assert persisted[0]["adapt"] == out["adapt"]


def test_adaptive_collector_refuses_missing_metadata_before_persisting(monkeypatch):
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-wrong")
    st = {"meta": {"analysis": "fea_adaptive", "doc": "doc-missing",
                   "material": _MATERIAL, "material_arg": "steel", "notes": []}}

    out = sim_tools._collect_job_adaptive(st, dict(_STATIC_RAW))
    assert out["ok"] is False and "without convergence metadata" in out["error"], out
    assert persisted == []


def test_unconverged_adaptive_result_is_diagnostic_failure_not_static_success(monkeypatch):
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-diagnostic")
    st = {"meta": {"analysis": "fea_adaptive", "doc": "doc-under-resolved",
                   "material": _MATERIAL, "material_arg": "steel", "notes": []}}

    out = sim_tools._collect_job_adaptive(st, _adaptive_raw(False))
    assert out["ok"] is False and out["converged"] is False, out
    assert out["eta_target_met"] is False
    assert out["global_eta"] == 0.08 and out["eta_target"] == 0.05
    assert out["passes_run"] == 2 and out["stop_reason"] == "pass_budget_exhausted"
    assert out["summary"]["max_von_mises_mpa"] == 12.5
    assert "under-resolved" in out["resolution_note"]
    assert "not a converged result" in out["error"]
    assert len(persisted) == 1 and persisted[0]["type"] == "fea_adaptive", persisted
    assert persisted[0]["converged"] is False


def test_a_finished_job_collects_once_however_often_it_is_polled(
        tmp_path, monkeypatch, registry):
    """Collect appends a study record to the document. Re-running it per poll would grow
    one study per poll in the feature tree for a single solve."""
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-1")
    job_id = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-5",
                                          "material": _MATERIAL, "material_arg": "steel",
                                          "notes": []}, _STATIC_RAW)

    first = cad_common.collect_durable(job_id)
    assert first["ok"] is True and first["summary"]["min_fos"] == 20.0, first
    assert first["saved_to_study"] is True
    for _ in range(4):
        assert cad_common.collect_durable(job_id) == first
    assert len(persisted) == 1, persisted
    assert persisted[0]["type"] == "fea_static"


def test_a_finished_job_survives_the_server_that_started_it(
        tmp_path, monkeypatch, registry):
    """After an MCP restart the collector registry is fresh and the document is long gone,
    but the agent still holds the job id — so the answer has to come back off the spool."""
    monkeypatch.setattr(sim_tools, "_persist_study", lambda doc, record: "study-1")
    job_id = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-6",
                                          "material": _MATERIAL, "material_arg": "steel",
                                          "notes": []}, _STATIC_RAW)
    collected = cad_common.collect_durable(job_id)

    # A process that has not (yet) registered any collector must still replay the envelope
    # the winner wrote, rather than re-deriving it or reporting the job unknown.
    monkeypatch.delitem(cad_common._DURABLE_COLLECTORS, "fea_static")
    assert cad_common.collect_durable(job_id) == collected


def test_running_get_job_ages_real_progress_not_heartbeats(
        monkeypatch, registry, get_job_tool, tools):
    from services.sim.job_worker import _Progress

    job_id = "sim-" + uuid.uuid4().hex[:12]
    directory = simjobs.job_dir(job_id)
    directory.mkdir(parents=True)
    wall_now = time.time()
    with simjobs._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, "selftest", "selftest_sleep", simjobs.RUNNING, os.getpid(),
             simjobs._proc_key(os.getpid()), wall_now, wall_now, 0, "{}"))
    now = [0.0]
    monkeypatch.setattr(simjobs, "_clock", lambda: now[0])
    publisher = _Progress(directory, job_id)
    probe = {"h_mm": 4.0, "cells_done": 8, "cells_total": 64,
             "elements_so_far": 128, "sub_phase": "fill"}
    event = {"phase": "mesh_background_probe", "elements": 128, "probe": probe}
    publisher.emit(event)
    try:
        for instant in (30.0, 60.0, 90.0):
            now[0] = instant
            publisher.heartbeat()
            # A native callback/file watcher may repeat unchanged counters too.
            publisher.emit({**event, "elapsed_s": instant, "note": str(instant)})
            polled = json.loads(get_job_tool(job_id=job_id))
            assert polled["status"] == "running", polled
            assert polled["progress"]["probe"] == probe
            assert polled["progress"]["elements"] == 128
            assert polled["progress"]["last_advance_s"] == instant
            assert polled["progress"]["stalled"] is False

        # No more publications: the public poll must age a silent worker itself.
        for instant, stalled in ((120.0, False), (120.001, True), (180.0, True)):
            now[0] = instant
            polled = json.loads(get_job_tool(job_id=job_id))["progress"]
            assert polled["last_advance_s"] == instant
            assert polled["stalled"] is stalled
            for partial in (False, True):
                result = json.loads(tools["sim_results"](job_id=job_id, partial=partial))
                assert result["progress"] == polled
                if partial:
                    assert result["snapshot"]["probe"] == probe
                    assert result["snapshot"]["last_advance_s"] == instant
                    assert result["converged"] is False

        # Cells can advance without creating any additional elements.
        publisher.emit({**event, "probe": {**probe, "cells_done": 9}})
        advanced = json.loads(get_job_tool(job_id=job_id))["progress"]
        assert advanced["last_advance_s"] == 0.0
        assert advanced["stalled"] is False
        assert advanced["elements"] == 128
        now[0] = 301.0
        assert json.loads(get_job_tool(job_id=job_id))["progress"]["stalled"] is True
        simjobs._set(job_id, state=simjobs.CANCELLED, pid=None, proc_key=None)
        assert json.loads(get_job_tool(job_id=job_id))["progress"]["stalled"] is False
    finally:
        simjobs._set(job_id, state=simjobs.CANCELLED, pid=None, proc_key=None)


def test_get_job_collects_a_reconnected_durable_job_only_after_releasing_thread_lock(
        tmp_path, monkeypatch, registry, get_job_tool):
    """DONE collection may wait and persist; neither operation belongs under _JOB_LOCK."""
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-1")
    job_id = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-7",
                                          "material": _MATERIAL, "material_arg": "steel",
                                          "notes": []}, _STATIC_RAW)
    real_snapshot = cad_doc_tools.durable_job_snapshot
    snapshot_calls: list = []

    def unlocked_snapshot(requested_job_id):
        acquired = cad_common._JOB_LOCK.acquire(blocking=False)
        assert acquired, "durable registry/collection ran while _JOB_LOCK was held"
        cad_common._JOB_LOCK.release()
        snapshot_calls.append(requested_job_id)
        return real_snapshot(requested_job_id)

    monkeypatch.setattr(cad_doc_tools, "durable_job_snapshot", unlocked_snapshot)
    first = json.loads(get_job_tool(job_id=job_id))
    assert first["status"] == "done" and first["kind"] == "fea_static", first
    assert first["result"]["summary"]["min_fos"] == 20.0
    assert snapshot_calls == [job_id]
    assert persisted and len(persisted) == 1
    assert cad_common._JOBS.get(job_id) is None

    second = json.loads(get_job_tool(job_id=job_id))
    assert second == first
    assert snapshot_calls == [job_id, job_id]
    assert len(persisted) == 1


def test_get_job_keeps_short_thread_jobs_on_the_in_process_fast_path(
        monkeypatch, get_job_tool):
    jid = "job-thread-probe"
    monkeypatch.setitem(cad_common._JOBS, jid, {
        "status": "done", "kind": "short_op", "result": {"value": 7}, "error": None,
        "progress": {}, "started_at": 1.0, "done_at": 2.0,
    })
    durable_calls: list = []
    monkeypatch.setattr(
        cad_doc_tools, "durable_job_snapshot",
        lambda job_id: durable_calls.append(job_id) or None)

    out = json.loads(get_job_tool(job_id=jid))
    assert out == {"ok": True, "job_id": jid, "status": "done",
                   "kind": "short_op", "result": {"value": 7}}
    assert durable_calls == []


def test_a_failed_durable_job_is_reported_as_an_error_not_an_empty_result(
        tmp_path, monkeypatch, registry, get_job_tool):
    """A worker that died must never surface as a done job with a blank answer."""
    job_id = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-8",
                                          "notes": []}, _STATIC_RAW)
    simjobs._set(job_id, state=simjobs.FAILED, error="the solver process exited")
    snapshot = json.loads(get_job_tool(job_id=job_id))
    assert snapshot["status"] == "error", snapshot
    assert snapshot["error"] == "the solver process exited", snapshot
    assert "result" not in snapshot


# ------------------------------------- terminal topology jobs through sim_status/sim_results
# Topology registers a terminal hook, so a failed or cancelled run is collected EXACTLY ONCE
# into its study whichever polling API reaches it first. get_job/wait_job did; these two
# pollers did not: sim_status dropped the job meta before anything could collect, and
# sim_results refused failed/cancelled jobs outright — an agent that only polled them left
# the study's last_job "running" forever. A finished but iteration-limited run is ok:true
# and converged:false; sim_results used to overwrite that with converged = ok.

from services.mcp.test_topology_tools import (  # noqa: E402
    _died,
    _finish,
    _native_child,
    _native_result,
    _submitted,
)
from services.mcp.test_topology_tools import h as topology_harness  # noqa: E402,F401
from services.sim import topology as topo  # noqa: E402


def _jsonable(value):
    return json.loads(json.dumps(value))


@pytest.mark.parametrize("state", [simjobs.FAILED, simjobs.CANCELLED])
def test_terminal_topology_job_polled_only_by_sim_tools_reaches_its_study_once(
        topology_harness, tools, state):
    h = topology_harness
    out, meta = _submitted(h)
    jid = out["job_id"]
    _died(meta, jid, cancel_requested=state == simjobs.CANCELLED)
    edits_before = h.doc.edits

    status = json.loads(tools["sim_status"](job_id=jid))
    assert status["state"] == state and status["ok"] is False, status
    assert "meta" not in status, status
    assert status["code"] == f"solve_{state}", status
    detail = status["detail"]
    assert detail["ok"] is False and detail["state"] == state, detail
    assert detail["saved_to_study"] is True and detail["run_id"] == out["run_id"], detail
    assert detail["converged"] is False and detail["feasible"] is None, detail
    assert detail["print_ready"] is False, detail
    assert h.doc.edits == edits_before + 1, "the terminal job persists exactly one update"
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == state and stored["last_job"]["job_id"] == jid
    assert [r["run_id"] for r in stored["runs"]] == ["older", out["run_id"]]
    assert stored["runs"][-1]["result"]["ok"] is False

    full = json.loads(tools["sim_results"](job_id=jid))
    assert full["ok"] is False and full["state"] == state, full
    assert full["converged"] is False and full["saved_to_study"] is True, full
    assert full["run_id"] == out["run_id"] and full["result"] == detail["result"], full
    assert full["result"]["termination"] == (
        "cancelled" if state == simjobs.CANCELLED else "failed")
    for invented in ("summary", "history", "density", "fields"):
        assert invented not in full and invented not in full["result"], invented

    # Every later poll, through any API, replays the one collected envelope.
    for _ in range(2):
        assert json.loads(tools["sim_status"](job_id=jid))["detail"] == detail
        assert json.loads(tools["sim_results"](job_id=jid))["result"] == detail["result"]
    assert _jsonable(cad_common.durable_job_snapshot(jid)["detail"]) == detail
    assert _jsonable(simjobs.collected(jid)) == detail
    assert h.doc.edits == edits_before + 1


def test_native_topology_failure_reaches_sim_results_with_its_evidence_not_as_success(
        topology_harness, tools):
    h = topology_harness
    out, meta = _submitted(h)
    jid = out["job_id"]
    child = _native_child(pathlib.Path(out["artifact_dir"]))
    (child / "checkpoint.json").write_text('{"schema": "x"}')
    raw = {"ok": False, "converged": False, "feasible": True, "termination": "failed",
           "error": "singular stiffness", "error_code": "physics_failed",
           "method": "simp", "physics": "structural", "result_dir": str(child),
           "summary": {"iterations": 3}, "density": [0.5] * 8,
           "manufacturing": {"geometry_valid": True, "print_ready": True},
           "artifacts": {"checkpoint": {
               "path": str(child / "checkpoint.json"), "format": "json",
               "sha256": topo.sha256_file(child / "checkpoint.json")}}}
    _finish(meta, raw, jid)
    edits_before = h.doc.edits

    res = json.loads(tools["sim_results"](job_id=jid))
    assert res["ok"] is False and res["state"] == simjobs.FAILED, res
    assert res["converged"] is False and res["feasible"] is None, res
    assert res["print_ready"] is False and res["saved_to_study"] is True, res
    assert res["error"] == "singular stiffness", res
    # The last iterate's numbers are evidence inside the retained result, never promoted
    # to a top-level answer.
    assert "summary" not in res and "density" not in res, res
    result = res["result"]
    assert result["error_code"] == "physics_failed" and result["termination"] == "failed"
    assert result["summary"] == {"iterations": 3}
    assert result["artifacts"]["checkpoint"]["sha256"] == topo.sha256_file(
        child / "checkpoint.json")
    assert result["manufacturing"]["print_ready"] is False
    assert h.doc.edits == edits_before + 1
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == simjobs.FAILED
    assert stored["runs"][-1]["result"]["error_code"] == "physics_failed"

    status = json.loads(tools["sim_status"](job_id=jid))
    assert status["code"] == "solve_failed", status
    assert status["detail"]["run_id"] == res["run_id"], status
    assert status["detail"]["result"] == res["result"], status
    assert h.doc.edits == edits_before + 1


def test_partial_poll_of_a_cancelled_topology_job_collects_it_without_finalizing(
        topology_harness, tools):
    h = topology_harness
    out, meta = _submitted(h)
    jid = out["job_id"]
    _died(meta, jid, cancel_requested=True)
    (simjobs.job_dir(jid) / "partial.json").write_text(json.dumps(
        {"partial": True, "phase": "optimize", "iteration": 7, "objective": 4.2}))
    edits_before = h.doc.edits

    part = json.loads(tools["sim_results"](job_id=jid, partial=True))
    assert part["partial"] is True and part["converged"] is False, part
    assert part["state"] == simjobs.CANCELLED, part
    assert part["snapshot"]["iteration"] == 7, part
    terminal = part["terminal"]
    assert terminal["ok"] is False and terminal["state"] == simjobs.CANCELLED, terminal
    assert terminal["saved_to_study"] is True and terminal["run_id"] == out["run_id"]
    assert terminal["converged"] is False, terminal
    for finalized in ("result", "summary", "fields", "density"):
        assert finalized not in terminal and finalized not in part, finalized
    assert h.doc.edits == edits_before + 1
    assert h.doc.stored("study-00000001")["last_job"]["state"] == simjobs.CANCELLED

    full = json.loads(tools["sim_results"](job_id=jid))
    assert full["run_id"] == terminal["run_id"] and full["ok"] is False, full
    assert h.doc.edits == edits_before + 1


def test_iteration_limited_topology_stays_ok_but_unconverged_through_sim_results(
        topology_harness, tools):
    h = topology_harness
    out, meta = _submitted(h)
    jid = out["job_id"]
    run_dir = pathlib.Path(out["artifact_dir"])
    _finish(meta, _native_result(run_dir, converged=False, termination="max_iterations"),
            jid)

    assert json.loads(tools["sim_status"](job_id=jid))["state"] == "done"
    res = json.loads(tools["sim_results"](job_id=jid))
    assert res["ok"] is True and res["state"] == "done", res
    assert res["converged"] is False, res
    assert res["termination"] == "max_iterations" and res["outcome"]["converged"] is False
    assert res["result"]["converged"] is False, res["result"]
    assert h.doc.stored("study-00000001")["runs"][-1]["result"]["converged"] is False
    assert json.loads(tools["sim_results"](job_id=jid)) == res


def test_legacy_kinds_keep_their_sim_status_and_sim_results_semantics(
        tmp_path, monkeypatch, registry, tools):
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-1")
    done = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-9",
                                        "material": _MATERIAL, "material_arg": "steel",
                                        "notes": []}, _STATIC_RAW)
    res = json.loads(tools["sim_results"](job_id=done))
    assert res["ok"] is True and res["converged"] is True and res["state"] == "done", res
    assert res["summary"]["min_fos"] == 20.0 and len(persisted) == 1

    refusal = {"ok": False, "error": "mesh rejected", "n_elems": 12,
               "mesh_quality": {"min_jacobian": 0.01}, "summary": {"min_fos": 99.0}}
    failed = _finished_job("fea_static", {"analysis": "fea_static", "doc": "doc-10",
                                          "notes": []}, refusal)
    simjobs._set(failed, state=simjobs.FAILED, error="the solver process exited")
    status = json.loads(tools["sim_status"](job_id=failed))
    assert status["state"] == simjobs.FAILED and "meta" not in status, status
    assert "code" not in status and "detail" not in status, status
    out = json.loads(tools["sim_results"](job_id=failed))
    assert out["ok"] is False and out["state"] == simjobs.FAILED, out
    assert out["solver_error"] == "mesh rejected", out
    assert out["n_elems"] == 12 and out["mesh_quality"] == {"min_jacobian": 0.01}, out
    assert "summary" not in out and "converged" not in out, out
    assert simjobs.collected(failed) is None and len(persisted) == 1


# ------------------------------------------------------- the convergence ladder is SEQUENTIAL
# Measured live: a three-leg ladder started its legs as three simultaneous durable jobs, and
# four ladders launched back-to-back put twelve solves on the machine at once. The memory
# governor (core/util/resource_governor) budgets a fraction of the RAM AVAILABLE at solve
# time, so the legs starved each other and every one of them failed — the same meshes solved
# cleanly one at a time. These tests pin the ordering, not a timing.

_GOVERNOR_PLAN_NOTE = (
    "governor: plan what=fea_solver workers=6/6 reserve=0.10GB budget=9.42GB rss=1.20GB "
    "avail=13.60GB total=31.00GB pressure=0.00 admitted=1 knobs=CHUDWARE_MEM_FRAC")

_GOVERNOR_REFUSAL = (
    "static FEA solve needs an estimated 20.5 GB but this machine's budget is 9.4 GB "
    "(31 GB RAM, 13.6 GB available) — use a coarser mesh / fewer cells / first-order "
    "elements, or raise CHUDWARE_MEM_FRAC / CHUDWARE_MEM_CAP_GB")


class _LegStub:
    """A fake durable-job backend that REFUSES to run two legs at once.

    `start` asserts every previously started leg has already reached a terminal state, so
    a ladder that queues its legs up front fails here instead of in a live study. Each leg
    reports RUNNING for two polls before it settles, so "sequential" cannot be an artifact
    of instant completion.
    """

    def __init__(self, monkeypatch, peaks: dict, fail: dict | None = None,
                 implausible: set | None = None):
        self.peaks = peaks              # leg index -> monitored von Mises value
        self.fail = fail or {}          # leg index -> error string
        # leg indices whose solve left the linear-elastic regime (the engine's
        # plausibility flag), which the ladder must not count as convergence
        self.implausible = implausible or set()
        self.events: list = []          # ("start"|"done", leg index)
        self.jobs: dict = {}            # job id -> {"index", "state", "polls"}
        monkeypatch.setattr(simjobs, "start", self._start)
        monkeypatch.setattr(simjobs, "status", self._status)
        monkeypatch.setattr(sim_tools, "collect_durable", self._collect)

    def _start(self, kind, stage, payload, meta=None):
        live = [j for j in self.jobs.values() if j["state"] not in simjobs.TERMINAL]
        assert not live, f"a leg was started while {live} was still solving"
        index = (meta or {})["convergence_step"]["index"]
        job_id = f"sim-leg{index}"
        self.jobs[job_id] = {"index": index, "state": simjobs.RUNNING, "polls": 0}
        self.events.append(("start", index))
        return {"job_id": job_id, "state": "running", "kind": kind, "stage": stage}

    def _status(self, job_id):
        job = self.jobs[job_id]
        job["polls"] += 1
        if job["polls"] > 2 and job["state"] == simjobs.RUNNING:
            job["state"] = simjobs.DONE
            self.events.append(("done", job["index"]))
        return {"state": job["state"], "meta": {}}

    def _collect(self, job_id, st=None):
        index = self.jobs[job_id]["index"]
        if index in self.fail:
            return {"ok": False, "doc_id": "doc-1", "error": self.fail[index]}
        bad = index in self.implausible
        return {"ok": True, "doc_id": "doc-1", "n_dof": 3000 * (index + 1),
                "n_nodes": 1000 * (index + 1), "n_elems": 900 * (index + 1),
                "mesh": "tet10", "solver_notes": [_GOVERNOR_PLAN_NOTE],
                "plausibility": {"max_disp_over_bbox_diag": 41.2 if bad else 0.0016,
                                 "bbox_diag_mm": 408.9,
                                 "flag": "implausible" if bad else "ok"},
                "summary": {"max_von_mises_mpa": self.peaks[index],
                            "max_disp_mm": 16847.0 if bad else 0.04,
                            "min_fos": 12.0}}

    @property
    def starts(self) -> list:
        return [i for kind, i in self.events if kind == "start"]


@pytest.fixture()
def ladder_setup(tmp_path, monkeypatch):
    """The convergence tool with its setup stage and autosave stubbed out."""
    monkeypatch.setattr(sim_tools, "_build_static_study",
                        lambda *a, **kw: _static_ctx(tmp_path))
    monkeypatch.setattr(sim_tools, "_autosave_all_open", lambda: None)
    return tmp_path


def test_convergence_legs_solve_one_at_a_time_coarse_to_fine(
        ladder_setup, monkeypatch, tools):
    """Leg i+1 must not START until leg i is terminal — the whole point of the fix."""
    stub = _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    assert stub.events == [("start", 0), ("done", 0), ("start", 1), ("done", 1),
                           ("start", 2), ("done", 2)], stub.events
    assert [r["size_mm"] for r in out["table"]] == [4, 2, 1], out["table"]
    assert out["ok"] is True and out["n_meshes"] == 3, out
    assert out["converged"] is True, out            # last delta ~0.96% < 5%
    assert out["job_ids"] == ["sim-leg0", "sim-leg1", "sim-leg2"], out


def test_falling_dof_cannot_certify_convergence_and_names_admission():
    rows = [
        {"n_dof": 1000, "n_elems": 200, "monitored_value": 20.0,
         "delta_pct_vs_prev": None},
        {"n_dof": 800, "n_elems": 160, "monitored_value": 20.0,
         "delta_pct_vs_prev": 0.0,
         "governor": {"reason": "memory_admission"}},
    ]
    out = sim_tools._convergence_envelope("doc", "max_von_mises_mpa", 5, rows, [])
    assert not out["converged"]
    assert out["degenerate_legs"][0]["cause"] == "memory_admission"
    assert out["degenerate_legs"][0]["dof_growth_pct"] == -20.0


def test_refused_ladder_recovery_is_terminal_and_retains_admission(monkeypatch):
    monkeypatch.setattr(simjobs, "status", lambda jid: {
        "state": simjobs.FAILED, "error": "measured solve exceeds memory budget",
        "meta": {"doc": "doc", "convergence_step": {
            "index": int(jid), "target_size_mm": 6 - int(jid), "mesh_order": 2}}})
    monkeypatch.setattr(simjobs, "refusal_result", lambda jid: {
        "ok": False, "provenance": {"governor": {
            "model": "measured_octave", "reason": "memory_admission",
            "estimated_elems": 200000, "estimate_gb": 11.2}}})
    out = sim_tools._convergence_from_jobs(["0", "1"], "max_disp_mm", 5)
    assert not out["ok"] and not out["converged"]
    assert "pending" not in out
    assert out["n_meshes"] == 2
    assert all(row.get("error") for row in out["table"])
    assert all(row["governor"]["reason"] == "memory_admission" for row in out["table"])


def test_each_leg_row_carries_the_governors_own_memory_decision(
        ladder_setup, monkeypatch, tools):
    """The agent that starved this study believed the budget was ~70% of TOTAL RAM. The
    governor's numbers are on the row, and the rule is in the reply."""
    _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    for row in out["table"]:
        assert row["memory_budget"] == {"budget_gb": 9.42, "avail_gb": 13.6,
                                        "total_gb": 31.0, "workers": 6}, row
    assert "budget 9.42 GB" in out["resources"], out["resources"]
    assert "available 13.60 GB" in out["resources"], out["resources"]
    assert "RAM AVAILABLE" in out["resources"] and "ONE LADDER" in out["resources"]


def test_a_failed_leg_stops_the_ladder_and_the_finer_legs_are_marked_skipped(
        ladder_setup, monkeypatch, tools):
    """A finer mesh is both unreadable without its coarser reference and the more
    expensive solve, so a mid-ladder failure must not be followed by another attempt."""
    stub = _LegStub(monkeypatch, {0: 100.0, 2: 105.0}, fail={1: _GOVERNOR_REFUSAL})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    assert stub.starts == [0, 1], stub.events   # the 1 mm leg was never started
    assert out["ok"] is False and out["converged"] is False, out
    assert out["skipped_legs"] == [2], out
    third = out["table"][2]
    assert third["skipped"] is True and third["size_mm"] == 1, third
    assert "stopped the ladder" in third["error"], third
    assert "continue_on_fail=True" in third["error"], third
    assert "rows [1]" in out["error"] and "rows [2]" in out["error"], out["error"]
    # The refused leg's row explains itself with the governor's own arithmetic.
    assert out["table"][1]["memory_budget"] == {
        "budget_gb": 9.4, "total_gb": 31.0, "avail_gb": 13.6, "estimate_gb": 20.5}


def test_continue_on_fail_solves_the_remaining_legs_after_a_failure(
        ladder_setup, monkeypatch, tools):
    stub = _LegStub(monkeypatch, {0: 100.0, 2: 105.0}, fail={1: _GOVERNOR_REFUSAL})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1], continue_on_fail=True))

    assert stub.starts == [0, 1, 2], stub.events
    assert out["ok"] is False and "skipped_legs" not in out, out
    assert [bool(r.get("skipped")) for r in out["table"]] == [False, False, False], out
    assert out["table"][2]["monitored_value"] == 105.0, out["table"][2]


def test_background_ladder_is_one_job_whose_legs_still_run_in_order(
        ladder_setup, monkeypatch, tools):
    """background=True returns immediately, but the ladder is ONE driver job that starts
    each leg only after the previous one finished — not N legs queued at once."""
    stub = _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    reply = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1], background=True))

    jid = reply["job_id"]
    assert jid.startswith("job-") and reply["state"] == "running", reply
    assert reply["n_legs"] == 3 and reply["sequential"] is True, reply
    assert reply["legs_done"] == 0 and reply["leg_job_ids"] == [], reply
    assert "ONE AT A TIME" in reply["note"], reply["note"]
    assert f"get_job('{jid}')" in reply["next_actions"], reply

    snap = _await_ladder_job(jid)
    assert snap["status"] == "done", snap
    assert snap["progress"]["legs_done"] == 3, snap["progress"]
    assert snap["progress"]["leg_job_ids"] == ["sim-leg0", "sim-leg1", "sim-leg2"]
    assert stub.events == [("start", 0), ("done", 0), ("start", 1), ("done", 1),
                           ("start", 2), ("done", 2)], stub.events
    table = snap["result"]["table"]
    assert [r["size_mm"] for r in table] == [4, 2, 1], table
    assert snap["result"]["converged"] is True, snap["result"]


def test_sim_results_resolves_the_background_ladder_job_id(
        ladder_setup, registry, monkeypatch, tools, get_job_tool):
    """Benchmark C3: run_fea_convergence(background=True) handed out a job-* id that
    get_job resolved while sim_results — "from ANY tool that returned a job_id" — called
    it unknown. It must refuse while the ladder runs and hand back get_job's result once
    the ladder is done."""
    stub = _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    release = threading.Event()
    settle = stub._status
    monkeypatch.setattr(simjobs, "status", lambda job_id: (
        settle(job_id) if release.is_set() else {"state": simjobs.RUNNING, "meta": {}}))
    reply = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1], background=True))
    jid = reply["job_id"]
    assert jid.startswith("job-"), reply
    try:
        running = json.loads(tools["sim_results"](job_id=jid))
        assert running["ok"] is False and running["state"] == "running", running
        assert "no converged results" in running["error"], running
        assert "table" not in running and "registry" not in running["error"], running
        assert f"wait_job('{jid}')" in running["hint"], running

        partial = json.loads(tools["sim_results"](job_id=jid, partial=True))
        assert partial["ok"] is True and partial["partial"] is True, partial
        assert partial["converged"] is False, partial
        assert partial["snapshot"]["legs_total"] == 3, partial

        status = json.loads(tools["sim_status"](job_id=jid))
        assert status["state"] == "running" and status["durable"] is False, status
        assert "result" not in status, status

        cancel = json.loads(tools["sim_cancel"](job_id=jid))
        assert cancel["ok"] is False and "nothing was cancelled" in cancel["error"], cancel

        listed = json.loads(tools["sim_jobs"]())
        assert jid in [j["job_id"] for j in listed["process_jobs"]], listed
    finally:
        release.set()

    assert _await_ladder_job(jid)["status"] == "done"
    via_get_job = json.loads(get_job_tool(job_id=jid))
    assert via_get_job["status"] == "done", via_get_job
    done = json.loads(tools["sim_results"](job_id=jid))
    assert done["job_id"] == jid and done["state"] == "done", done
    assert {key: done[key] for key in via_get_job["result"]} == via_get_job["result"]
    assert done["converged"] is True, done
    assert [r["size_mm"] for r in done["table"]] == [4, 2, 1], done["table"]
    assert done["progress"]["leg_job_ids"] == ["sim-leg0", "sim-leg1", "sim-leg2"], done


def test_sim_tools_name_a_lost_driver_job_honestly(registry, tools):
    """A job-* id is an in-process driver, never a sim registry row: its miss says so."""
    out = json.loads(tools["sim_results"](job_id="job-000000000000"))
    assert out["ok"] is False and out["state"] == "unknown", out
    assert "in-process driver" in out["error"], out
    assert "No such simulation job in the registry" not in out["error"], out
    assert "run_fea_convergence(job_ids=[...])" in out["error"], out


def _await_ladder_job(job_id: str, timeout_s: float = 30.0) -> dict:
    """The finished record of a background ladder driver job."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with cad_common._JOB_LOCK:
            snap = dict(cad_common._JOBS[job_id])
        if snap["status"] != "running":
            return snap
        time.sleep(0.05)
    pytest.fail(f"the background ladder {job_id} never finished")


def test_a_ladder_that_outruns_the_call_budget_keeps_solving_its_legs_in_order(
        ladder_setup, monkeypatch, tools):
    """The old handoff queued EVERY remaining leg as its own job the moment the sync
    budget expired — three simultaneous solves from one call. Now the same sequential
    driver finishes the ladder, re-attaching to the leg that was already in flight."""
    monkeypatch.setattr(sim_tools, "_SYNC_SOLVE_BUDGET_S", 0.01)
    stub = _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    reply = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    # Handed off mid-leg: exactly ONE leg exists, and it is not re-queued.
    assert reply["state"] == "running" and reply["legs_done"] == 0, reply
    assert reply["leg_job_ids"] == ["sim-leg0"], reply
    assert "rest continue in ONE background job" in reply["note"] or \
           "runs in ONE background job" in reply["note"], reply["note"]

    snap = _await_ladder_job(reply["job_id"])
    assert snap["status"] == "done", snap
    assert stub.events == [("start", 0), ("done", 0), ("start", 1), ("done", 1),
                           ("start", 2), ("done", 2)], stub.events
    assert [r["size_mm"] for r in snap["result"]["table"]] == [4, 2, 1]
    assert snap["result"]["job_ids"] == ["sim-leg0", "sim-leg1", "sim-leg2"], snap


# ------------------------------------------- a mechanism does not converge, however smooth
# Measured live (a frame's front spine tube, O60 x 5.16 mm wall, tet10 at h = 16 mm): the solve
# returned ok with max displacement 9.6e6 mm at an equilibrium error of 0.00042%. The
# residual is tiny precisely BECAUSE a mechanism carries no load, so equilibrium can never
# catch it — and a ladder built on such a leg reports a tidy delta over nonsense.

def test_an_implausible_leg_carries_no_delta_and_forces_converged_false(
        ladder_setup, monkeypatch, tools):
    """The middle leg left the small-strain regime: it must not be a reference either."""
    _LegStub(monkeypatch, {0: 100.0, 1: 5000.0, 2: 105.0}, implausible={1})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    table = out["table"]
    assert [bool(r.get("implausible")) for r in table] == [False, True, False], table
    # The flagged leg keeps its numbers for diagnosis but has no comparable delta.
    assert table[1]["monitored_value"] == 5000.0, table[1]
    assert table[1]["delta_pct_vs_prev"] is None, table[1]
    assert table[1]["plausibility"]["flag"] == "implausible", table[1]
    # The finest leg is measured against leg 0 (100 -> 105), never against the mechanism.
    assert table[2]["delta_pct_vs_prev"] == 5.0, table[2]
    assert out["implausible_legs"] == [1], out
    assert out["converged"] is False, out
    assert "IMPLAUSIBLE" in out["note"], out["note"]
    assert "thin_wall_ratio" in out["note"], out["note"]


def test_a_ladder_of_plausible_legs_still_converges_on_its_own_delta(
        ladder_setup, monkeypatch, tools):
    """The control: the plausibility block rides on every row and changes nothing."""
    _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    assert [r["plausibility"]["flag"] for r in out["table"]] == ["ok", "ok", "ok"]
    assert all("implausible" not in r for r in out["table"]), out["table"]
    assert "implausible_legs" not in out, out
    assert out["converged"] is True, out


# --------------------------------------------------- requested vs DELIVERED element size
# The ladder that forced this asked for 1.6 / 1.2 / 0.9 mm and was meshed at
# 5.62 / 5.43 / 5.42 mm by the memory governor. Nothing in the row or the .vtu name said
# so, so the delivered size had to be reverse-engineered out of governor prose. These
# drive the pure helpers directly: no engine, synthetic rows, exact ratios.

def test_size_delivery_ratio_and_note_name_the_legs_the_governor_resized():
    """Every solved leg gets an entry, ratio is delivered/requested, and a miss is a
    sentence naming the legs — not a flag the reader has to know to look for."""
    rows = [
        {"size_mm": 1.6, "delivered_size_mm": 5.62},
        {"size_mm": 1.2, "delivered_size_mm": 5.43},
        {"size_mm": 0.9, "delivered_size_mm": 5.42},
    ]
    delivery = sim_tools._size_delivery(rows)

    assert [e["leg"] for e in delivery] == [0, 1, 2], delivery
    assert [e["requested_mm"] for e in delivery] == [1.6, 1.2, 0.9], delivery
    assert [e["delivered_mm"] for e in delivery] == [5.62, 5.43, 5.42], delivery
    assert [e["ratio"] for e in delivery] == [3.5125, 4.525, 6.0222], delivery

    note = sim_tools._size_delivery_note(delivery)
    assert note is not None
    assert all(f"leg {i}" in note for i in (0, 1, 2)), note
    assert "1.6 mm" in note and "5.62 mm" in note, note
    assert "delivered_size_mm" in note, note


def test_size_delivery_is_silent_inside_tolerance_and_ignores_unsolved_legs():
    """A leg meshed close to its request is not news; a leg with no mesh has nothing to
    report; a fineness rung states no size in mm, so its ratio is null rather than a
    number invented from one side."""
    rows = [
        {"size_mm": 4.0, "delivered_size_mm": 4.0},
        {"size_mm": 2.0, "delivered_size_mm": 2.4},          # 1.2x — inside the band
        {"size_mm": None, "fineness": 0.5, "delivered_size_mm": 3.2},
        {"size_mm": 1.0, "error": "the governor refused this leg"},
        {"size_mm": 0.5, "skipped": True},
    ]
    delivery = sim_tools._size_delivery(rows)

    assert [e["leg"] for e in delivery] == [0, 1, 2], delivery
    assert [e["ratio"] for e in delivery] == [1.0, 1.2, None], delivery
    assert sim_tools._size_delivery_note(delivery) is None


def test_size_delivery_also_reports_a_mesh_FINER_than_the_request():
    """The band floor and feature refinement pull the other way, and a leg meshed at
    0.6x the stated size is also not the discretization that was stated."""
    delivery = sim_tools._size_delivery([{"size_mm": 1.0, "delivered_size_mm": 0.6}])

    assert delivery[0]["ratio"] == 0.6, delivery
    note = sim_tools._size_delivery_note(delivery)
    assert note is not None and "leg 0" in note, note


def test_the_ladder_reply_carries_size_delivery_beside_its_verdict(
        ladder_setup, monkeypatch, tools):
    """The table is on the reply of a real ladder run, one entry per solved leg, and a
    ladder meshed at the sizes it asked for says nothing extra in its note."""
    _LegStub(monkeypatch, {0: 100.0, 1: 104.0, 2: 105.0})
    out = json.loads(tools["run_fea_convergence"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -200]}],
        sizes_mm=[4, 2, 1]))

    # The stub reports no mesh_quality, so no leg has a delivered size to compare: the
    # entries exist (one per solved leg) and say so honestly instead of inventing one.
    assert [e["leg"] for e in out["size_delivery"]] == [0, 1, 2], out["size_delivery"]
    assert [e["requested_mm"] for e in out["size_delivery"]] == [4, 2, 1], out
    assert all(e["delivered_mm"] is None and e["ratio"] is None
               for e in out["size_delivery"]), out["size_delivery"]
    assert all(r["delivered_size_mm"] is None for r in out["table"]), out["table"]
    assert "DELIVERED SIZE" not in out["note"], out["note"]


def test_collect_static_forwards_the_engines_mechanism_and_plausibility_evidence(
        monkeypatch):
    """An evidence field the engine sent must reach the caller VERBATIM, reply and study."""
    persisted: list = []
    monkeypatch.setattr(sim_tools, "_persist_study",
                        lambda doc, record: persisted.append(record) or "study-9")
    quality = {"n_nodes": 100, "n_elems": 40, "order": 2, "aspect_max": 3.1,
               "connected_components": 1, "unfixed_components": 0,
               "thin_wall_ratio": 0.32}
    plausibility = {"max_disp_over_bbox_diag": 41.2, "bbox_diag_mm": 408.9,
                    "flag": "implausible"}
    warning = ("displacement_implausible: max displacement 16847.000000 mm is 41.200000x "
               "the model bounding-box diagonal (408.900000 mm)")
    raw = {**_STATIC_RAW, "mesh_quality": quality, "plausibility": plausibility,
           "warnings": [warning]}

    out = sim_tools._collect_static(
        {"doc": "doc-9", "material": _MATERIAL, "material_arg": "steel"}, raw, [])

    # NOT refused: a real large deflection is worth diagnosing. But it is stated.
    assert out["ok"] is True, out
    assert out["plausibility"] == plausibility, out
    assert out["warnings"] == [warning], out
    assert out["mesh_quality"]["unfixed_components"] == 0, out["mesh_quality"]
    assert out["mesh_quality"]["thin_wall_ratio"] == 0.32, out["mesh_quality"]
    # The persisted study is the release artifact, so it carries the same evidence.
    assert len(persisted) == 1
    assert persisted[0]["plausibility"] == plausibility, persisted[0]
    assert persisted[0]["warnings"] == [warning], persisted[0]


def test_collect_static_omits_plausibility_when_the_engine_sent_none(monkeypatch):
    """A key the engine did not send stays ABSENT, not a null the reader must interpret."""
    monkeypatch.setattr(sim_tools, "_persist_study", lambda doc, record: "study-10")
    out = sim_tools._collect_static(
        {"doc": "doc-10", "material": _MATERIAL, "material_arg": "steel"},
        dict(_STATIC_RAW), [])
    assert out["ok"] is True, out
    assert "plausibility" not in out and "warnings" not in out, out


def test_thin_slice_governor_uses_measured_count_at_two_sizes(tmp_path, monkeypatch):
    """A box-envelope overestimate must not discard a feasible thin plate mesh."""
    from services.mcp import core_bridge as cb

    if not cb.available():
        pytest.skip("native mesh bridge unavailable")
    # Keep a bounded solve budget: a thin plate must be judged by its measured
    # mesh, not the much larger surface envelope fitted on a bulky box.
    monkeypatch.setenv("CHUDWARE_MEM_CAP_GB", "5")
    doc = cb.call("doc_new", {"kind": "part", "name": "governor_thin_slice"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [{
            "op": "add_feature", "feature": {
                "type": "box", "name": "ThinSlice",
                "params": {"length": 120, "width": 80, "height": 2},
            },
        }]})
        step = str(tmp_path / "thin_slice.step")
        cb.call("doc_export_step", {"doc_id": doc, "path": step})
        for h in (4.0, 2.84):
            mesh = cb.call("sim_mesh", {
                "step_path": step, "target_size_mm": h, "order": 2,
            })
            assert mesh["ok"], mesh
            evidence = mesh["provenance"]["governor"]
            measured = evidence["measured_probe"]
            assert mesh["char_len_mm"] == pytest.approx(h), evidence
            assert measured["elems"] == mesh["n_elems"]
            assert 0.5 * measured["elems"] <= evidence["estimated_elems"] <= 2 * measured["elems"]
    finally:
        cb.call("doc_close", {"doc_id": doc})

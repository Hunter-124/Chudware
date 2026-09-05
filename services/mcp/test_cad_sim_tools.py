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
    out = json.loads(_dispatch()("run_fea_static")(doc_id=doc_id, material="steel", **kw))
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


def test_buckling_polar_load_sets_the_applied_magnitude(bc_doc):
    """The buckling load factor scales the APPLIED load, so the polar form reaching the
    solver is provable from applied_load_n itself."""
    out = json.loads(_dispatch()("run_fea_buckling")(
        fixtures=["bottom"],
        load=[{"face": "top", "magnitude": 1000, "direction": [0, 0, -1]}],
        doc_id=bc_doc))
    if out.get("skipped"):
        pytest.skip(f"ccx unavailable: {out.get('reason')}")
    assert out.get("ok") is True, out
    assert out["applied_load_n"] == pytest.approx(1000.0), out
    assert out["load_factors"] and out["load_factors"][0] > 0.0, out
    mapping = out["input_mapping"]
    assert mapping["world_axis"] == "z", mapping
    assert mapping["proxy_length_mm"] == pytest.approx(20.0, rel=1e-3), mapping
    assert mapping["proxy_cross_section_mm"] == pytest.approx([100.0, 50.0], rel=1e-3)


def test_buckling_refuses_to_map_shear_into_axial_compression(bc_doc):
    out = json.loads(_dispatch()("run_fea_buckling")(
        fixtures=["bottom"],
        load=[{"face": "top", "force": [10, 0, -1000]}],
        doc_id=bc_doc))
    if out.get("skipped"):
        pytest.skip(f"ccx unavailable: {out.get('reason')}")
    assert out["ok"] is False and "tangential" in out["error"], out


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
    job = simjobs.start("selftest", "selftest_sleep", {"seconds": 6.0, "steps": 12})
    jid = job["job_id"]
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not simjobs.partial(jid):
            time.sleep(0.1)

        full = json.loads(_tool("sim_results")(job_id=jid))
        assert full["ok"] is False and full["state"] == "running", full
        assert "no converged results" in full["error"], full
        assert "summary" not in full and "snapshot" not in full, full

        part = json.loads(_tool("sim_results")(job_id=jid, partial=True))
        assert part["ok"] is True and part["partial"] is True, part
        assert part["converged"] is False, part
        assert "PARTIAL" in part["warning"] and "NOT a" in part["warning"], part
        snap = part["snapshot"]
        assert snap["partial"] is True and snap["converged"] is False, snap
        assert 0 < snap["pct"] < 100, snap
    finally:
        simjobs.cancel(jid)


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
    if out.get("skipped"):
        pytest.skip(f"ccx unavailable: {out.get('reason')}")
    assert out["ok"] is False, out
    assert "buckling load is zero" in out["error"], out
    assert "applied_load_n" not in out, out


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
    bind = _dispatch()("run_fea_static")
    steel = json.loads(bind(doc_id=bc_doc, fixtures=["bottom"], material="steel",
                            loads=[{"face": "top", "force": [0, 0, -200]}]))
    alu = json.loads(bind(doc_id=bc_doc, fixtures=["bottom"], material="aluminum",
                          loads=[{"face": "top", "force": [0, 0, -200]}]))
    for r in (steel, alu):
        if r.get("skipped"):
            pytest.skip(f"FEA backend unavailable: {r.get('reason')}")
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
    assert f"sim_cancel('{out['job_id']}')" in out["next_actions"], out
    assert "survives an MCP restart" in out["note"], out

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

    def __init__(self, monkeypatch, peaks: dict, fail: dict | None = None):
        self.peaks = peaks              # leg index -> monitored von Mises value
        self.fail = fail or {}          # leg index -> error string
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
        return {"ok": True, "doc_id": "doc-1", "n_dof": 3000 * (index + 1),
                "n_nodes": 1000 * (index + 1), "n_elems": 900 * (index + 1),
                "mesh": "tet10", "solver_notes": [_GOVERNOR_PLAN_NOTE],
                "summary": {"max_von_mises_mpa": self.peaks[index],
                            "max_disp_mm": 0.04, "min_fos": 12.0}}

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

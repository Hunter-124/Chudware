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
import time

import pytest

from services.mcp.cad_sim_tools import (
    _norm_bc_list,
    _norm_direction,
    _norm_fineness,
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

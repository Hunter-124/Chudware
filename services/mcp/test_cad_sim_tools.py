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

from services.mcp.cad_sim_tools import (
    _canon_item_keys,
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

# ~7 s of solve on the dev box (0.5 is ~3 s). Long enough that "sim_run returned before the
# solve could have finished" is a real measurement rather than a rounding artefact.
_ASYNC_FINENESS = 0.85


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

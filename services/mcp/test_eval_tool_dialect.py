"""Input-dialect + error-teaching regression tests for services/mcp/cad_eval_tools.py.

The argument shapes exercised here came out of REAL recorded agent runs and were
verified failing by ``tools/flywheel/replay_ledger_failures.py`` before the fix:

    query_geometry(target="bushing_body", query="faces, edges, volumes")
    measure_part(...)  -> "document has no bodies to evaluate"
    query_geometry(...) -> "document has no bodies to query"

The dispatch path under test is the BUILT-IN agent's:
``agent_turn._bind_tool`` -> ``agent_turn._reconcile_kwargs`` -> the tool function.
``_reconcile_kwargs`` refuses to call a tool with a kwarg its signature does not
name, so these tests fail loudly if a tool ever loses its var-keyword parameter.

The FastMCP surface is covered too: a bare ``**kwargs`` becomes a REQUIRED schema
field literally named "kwargs" and breaks every external call while the built-in
path sails through, so the canonical calls are also driven through ``Tool.run``.

Documents are real: the engine (core/build-occt-linux/bin/libchudware.so) loads by
default and every part below is built through the same tools an agent would use.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services.agent import agent_turn as at

WIDENED_TOOLS = (
    "query_geometry", "check_sketch", "measure", "measure_part", "measure_between",
    "check_interference", "check_feature_overlap", "check_symmetry", "inspect_section",
    "draft_analysis",
)

PLATE = [
    {"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": 80, "width": 60, "height": 10}}},
    {"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Bore",
        "params": {"radius": 6, "height": 40}, "mode": "cut"}},
]


def _dispatch(tool: str, /, **kwargs):
    """Exactly what the built-in agent does. Returns (payload, reconciler_error)."""
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve in the MCP registry"
    bound, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    if err is not None:
        return None, str(err.get("error", ""))
    out = fn(**bound)
    return (json.loads(out) if isinstance(out, str) else out), None


def _call(tool: str, /, **kwargs) -> dict:
    payload, err = _dispatch(tool, **kwargs)
    assert err is None, f"{tool} rejected {sorted(kwargs)} before calling: {err}"
    return payload


def _notes(payload: dict) -> str:
    return " ".join(payload.get("interpreted") or [])


def _new_doc(name: str, ops: list | None = None) -> str:
    doc_id = _call("new_part", name=name)["doc_id"]
    if ops:
        built = _call("add_features", doc_id=doc_id, ops=ops)
        assert built.get("ok") is True, built
    return doc_id


@pytest.fixture(scope="module")
def part() -> str:
    """A plate with a through bore — a body, several planar faces, one cylinder."""
    return _new_doc("eval_dialect_part", PLATE)


@pytest.fixture(scope="module")
def empty() -> str:
    """A document that resolves perfectly and holds nothing at all."""
    return _new_doc("eval_dialect_empty")


# --------------------------------------------------------------------------
# query_geometry: the spellings a model actually writes
# --------------------------------------------------------------------------

def test_canonical_query_is_untouched(part):
    """The documented call must behave byte-identically — no stray `interpreted`."""
    out = _call("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    assert out["ok"] is True
    assert out["query"] == {"of": "face", "side": "top"}
    assert out["count"] == 1
    assert list(out) == ["ok", "query", "count", "matches"]


def test_target_spelling_is_accepted(part):
    """The recorded failure: query_geometry(target=..., query=...) -> unexpected kwarg."""
    out = _call("query_geometry", target="bushing_body",
                query="faces, edges, volumes", doc_id=part)
    assert out["ok"] is True
    assert out["query"] == {"of": "edge"}
    assert out["count"] > 0


def test_target_alone_is_read_as_the_query(part):
    out = _call("query_geometry", target={"of": "face", "side": "top"}, doc_id=part)
    assert out["ok"] is True
    assert out["count"] == 1
    assert "read as query=" in _notes(out)


def test_flattened_query_kwargs_are_assembled_and_echoed(part):
    """of=/side= written as top-level kwargs mean the documented dict."""
    canonical = _call("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    flat = _call("query_geometry", of="face", side="top", doc_id=part)
    assert flat["ok"] is True
    assert flat["query"] == {"of": "face", "side": "top"}
    assert flat["matches"] == canonical["matches"]
    assert "assembled into query=" in _notes(flat)
    assert "of='face'" in _notes(flat) and "side='top'" in _notes(flat)


def test_face_alias_pins_the_topology_kind(part):
    faces = _call("query_geometry", face="top", doc_id=part)
    edges = _call("query_geometry", edge="longest", doc_id=part)
    assert faces["query"]["of"] == "face"
    assert edges["query"]["of"] == "edge"


def test_a_subject_that_matches_nothing_is_reported_not_ignored(part):
    out = _call("query_geometry", target="bushing_body", query="top", doc_id=part)
    assert out["unresolved_subject"] == ["bushing_body"]
    assert "did NOT restrict this query" in _notes(out)


def test_a_subject_naming_an_open_part_selects_that_document(part):
    """target= naming an open part is the document the model meant."""
    out = _call("query_geometry", target="eval_dialect_part", query="top")
    assert out["ok"] is True
    assert "used as doc_id=" in _notes(out)
    assert out["count"] == 1


def test_conflicting_query_keys_raise_instead_of_one_winning(part):
    out = _call("query_geometry", query={"of": "face"}, of="edge", doc_id=part)
    assert out["ok"] is False
    assert "arrived twice with different values" in out["error"]


def test_no_query_at_all_teaches_the_accepted_forms(part):
    out = _call("query_geometry", doc_id=part)
    assert out["ok"] is False
    assert "needs a geometry query" in out["error"]
    assert 'of="face", side="top"' in out["error"]


# --------------------------------------------------------------------------
# limit / max / max_results: spellings of the `count` result cap
# (live 2026-07-27, claude-opus flyswatter run: query_geometry(..., limit=30)
# was rejected outright and cost a billed completion)
# --------------------------------------------------------------------------

def test_limit_is_folded_into_count(part):
    """`count` IS a result cap (core/doc/picks.cpp truncates the match set to
    it), so limit= is the same intent under its most common spelling."""
    out = _call("query_geometry", query={"of": "face"}, limit=2, doc_id=part)
    assert out["ok"] is True, out
    assert out["query"]["count"] == 2            # folded into the resolved query
    assert out["count"] == 2                     # and it actually caps the set
    assert len(out["matches"]) == 2
    assert "flat" in _notes(out)


def test_max_and_max_results_are_folded_into_count(part):
    for spelling in ("max", "max_results"):
        out = _call("query_geometry", query={"of": "face"}, doc_id=part,
                    **{spelling: 1})
        assert out["ok"] is True, (spelling, out)
        assert out["query"]["count"] == 1
        assert out["count"] == 1


def test_count_and_limit_together_conflict_instead_of_one_winning(part):
    """Two spellings of the same cap with different values must not collapse
    silently — that is one value, passed once."""
    out = _call("query_geometry", query={"of": "face"}, count=1, limit=2,
                doc_id=part)
    assert out["ok"] is False
    assert "arrived twice" in out["error"]
    assert "limit" in out["error"]


def test_canonical_count_is_untouched(part):
    out = _call("query_geometry", query={"of": "face", "count": 1}, doc_id=part)
    assert out["ok"] is True
    assert out["query"] == {"of": "face", "count": 1}  # echo stays verbatim


# --------------------------------------------------------------------------
# undo_denied: the refusal is correct policy but was a dead end — it named
# alternatives without their call shapes and never mentioned the op the model
# actually wanted (removing/altering a just-added feature).
# --------------------------------------------------------------------------

def test_undo_denied_teaches_the_way_out():
    from services.mcp.cad_common import _err
    from services.mcp.core_bridge import CoreError
    out = json.loads(_err(CoreError(
        "undo is disabled for mcp/agent connections; agents use "
        "doc_checkpoint/doc_revert or PLM rollback", "undo_denied")))
    assert out["ok"] is False and out["code"] == "undo_denied"
    hint = out["hint"]
    # the policy does not change and retrying is pointless — say so
    assert "will fail identically" in hint
    # (a) the checkpoint/revert pair, with its call shapes
    assert "doc_checkpoint(doc_id)" in hint and "doc_revert(doc_id" in hint
    assert "plm_rollback" in hint and "plm_timeline" in hint
    # (b) removing/altering a just-added feature is an OP, named by its real op name
    assert 'ops=[{"op":"delete_feature","id":"fN"}]' in hint
    assert '"op":"edit_feature"' in hint
    # and the same content as a machine-readable block
    alt = out["alternatives"]
    assert "delete_feature" in alt["remove_a_feature"]
    assert "edit_feature" in alt["alter_a_feature"]
    assert "plm_rollback" in alt["rollback_document"]


def test_other_core_errors_are_untouched():
    """Only undo_denied gets the envelope; every other engine error renders
    exactly as before."""
    from services.mcp.cad_common import _err
    from services.mcp.core_bridge import CoreError
    out = json.loads(_err(CoreError("boom", "invalid_op", {"x": 1})))
    assert out == {"ok": False, "error": "boom", "code": "invalid_op",
                   "detail": {"x": 1}}


# --------------------------------------------------------------------------
# The "no bodies" message: which situation, and what to call next
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs", [
    ("query_geometry", {"query": {"of": "face", "side": "top"}}),
    ("measure_part", {}),
])
def test_empty_document_names_the_doc_and_the_next_call(empty, tool, kwargs):
    out = _call(tool, doc_id=empty, **kwargs)
    assert out["ok"] is False
    err = out["error"]
    assert empty in err                      # the RESOLVED doc_id
    assert '"eval_dialect_empty"' in err     # and its name
    assert "0 features" in err               # which of the two situations this is
    assert "add_features(" in err            # the concrete next call
    assert out["detail"]["diagnosis"] == "no_bodies_empty_document"
    assert out["detail"]["doc_name"] == "eval_dialect_empty"
    assert out["detail"]["engine_message"].startswith("document has no bodies")


def test_rebuild_failure_is_told_apart_from_an_empty_part(empty, monkeypatch):
    """A doc whose features all FAILED needs the opposite remedy, so it must not be
    reported as "empty" — the engine flags it, and the message must carry that."""
    import services.mcp.cad_eval_tools as ev

    broken = ev.CoreError(
        "document has no bodies to evaluate — feature 'f2' (fillet) failed to rebuild: "
        "radius too large",
        "invalid_op", {"doc_id": empty, "reason": "feature rebuild failed"})
    taught = ev._teach_no_bodies(broken, empty, "measure_part()")

    assert taught is not broken
    assert str(taught).count("FAILED TO REBUILD") == 1
    assert "f2" in str(taught)
    assert "edit_feature" in str(taught)
    assert "Adding more geometry will not help" in str(taught)
    assert taught.code == "invalid_op"
    assert taught.detail["reason"] == "feature rebuild failed"


def test_undiagnosable_no_bodies_says_so_instead_of_guessing(empty, monkeypatch):
    """When the tree cannot be read, neither remedy may be asserted as fact."""
    import services.mcp.cad_eval_tools as ev

    monkeypatch.setattr(ev, "_feature_rows", lambda _doc_id: None)
    taught = ev._teach_no_bodies(
        ev.CoreError("document has no bodies to query", "invalid_op", {"doc_id": empty}),
        empty, "query_geometry(query=…)")
    assert "cannot tell you whether" in str(taught)
    assert "get_part(" in str(taught)


# --------------------------------------------------------------------------
# The rest of the sweep
# --------------------------------------------------------------------------

def test_datum_plane_axis_pairs_are_accepted(part):
    """"XY"/"xz" died on `plane 'xy' is not a reserved planar datum`."""
    top = _call("check_symmetry", plane="XY", doc_id=part)
    assert top["ok"] is True and top["plane"] == "top"
    assert "reserved datum plane 'top'" in _notes(top)

    sec = _call("inspect_section", section="xz", doc_id=part)
    assert sec["ok"] is True and sec["plane"] == "front"
    assert "read as plane=" in _notes(sec)


def test_an_unreadable_plane_lists_the_three_legal_ones(part):
    out = _call("check_symmetry", plane="diagonal", doc_id=part)
    assert out["ok"] is False
    for name in ('"top"', '"front"', '"right"'):
        assert name in out["error"]


def test_a_malformed_pull_direction_is_no_longer_silently_plus_z(part):
    """draft_analysis(pull_dir=[1,0]) used to answer the +Z question instead."""
    out = _call("draft_analysis", pull_dir=[1, 0], doc_id=part)
    assert out["ok"] is False
    assert "exactly 3 components" in out["error"]


def test_a_signed_axis_word_keeps_its_sign(part):
    out = _call("draft_analysis", pull="-z", angle_deg=5, doc_id=part)
    assert out["ok"] is True
    assert out["pull_dir"] == [0.0, 0.0, -1.0]
    assert out["threshold_deg"] == 5


def test_measure_between_accepts_the_pair_written_apart(part):
    apart = _call("measure_between", a="top", b="bottom", doc_id=part)
    listed = _call("measure_between", targets=["top", "bottom"], doc_id=part)
    assert apart["ok"] is True and listed["ok"] is True
    assert apart["distance"] == pytest.approx(listed["distance"])
    assert "read as a_query=" in _notes(apart)


def test_measure_accepts_a_single_selector_and_echoes_the_wrap(part):
    fn = at._bind_tool("measure")
    bound, _n, err = at._reconcile_kwargs("measure", fn, {"targets": "top", "doc_id": part})
    assert err is None
    _image, caption = fn(**bound)
    payload = json.loads(caption.rsplit("[measure_json=", 1)[1].rstrip("] "))
    assert payload["n_targets"] == 1
    assert "wrapped as" in " ".join(payload["interpreted"])


def test_an_unknown_sketch_comes_back_with_the_roster(part):
    out = _call("check_sketch", sketch_id="s99", doc_id=part)
    assert out["ok"] is False
    assert "NOT ONE of them is a sketch" in out["error"]
    assert "add_features(" in out["error"]


def test_check_feature_overlap_wraps_a_lone_feature_id(part):
    out = _call("check_feature_overlap", feature="Plate", doc_id=part)
    assert out["ok"] is True
    assert "wrapped as a list" in _notes(out)


def test_measure_part_density_aliases(part):
    steel = _call("measure_part", doc_id=part)
    alu = _call("measure_part", density=2.7, doc_id=part)
    assert alu["mass"]["mass_g"] < steel["mass"]["mass_g"]
    assert "read as density_g_cm3=" in _notes(alu)


def test_an_alias_that_disagrees_with_the_canonical_value_is_refused(part):
    out = _call("check_symmetry", plane="top", about="right", doc_id=part)
    assert out["ok"] is False
    assert "spelled two ways and they disagree" in out["error"]


def test_a_genuinely_unknown_kwarg_still_names_what_does_exist(part):
    out = _call("check_symmetry", wobble=3, doc_id=part)
    assert out["ok"] is False
    assert "unknown argument(s) wobble=3" in out["error"]
    assert "it takes plane= and doc_id=." in out["error"]


# --------------------------------------------------------------------------
# Both dispatch surfaces stay intact
# --------------------------------------------------------------------------

def test_every_widened_tool_bypasses_the_reconciler():
    """var_kw=True is the mechanic that lets a tool own its own input dialect."""
    for tool in WIDENED_TOOLS:
        fn = at._bind_tool(tool)
        assert fn is not None, tool
        assert at._signature_spec(fn)["var_kw"] is True, tool


def _fastmcp_tools():
    from mcp.server.fastmcp import FastMCP

    from services.mcp.cad_eval_tools import register_cad_eval_tools

    mcp = FastMCP("eval-dialect-test")
    register_cad_eval_tools(mcp)
    return mcp._tool_manager


def _text(result) -> str:
    """FastMCP Tool.run returns the raw value or a (unstructured, structured) pair."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list) and result:
        return getattr(result[0], "text", str(result[0]))
    return result if isinstance(result, str) else str(result)


def test_var_kw_did_not_become_a_required_fastmcp_field():
    """A bare **kwargs makes FastMCP demand a field literally named "kwargs"."""
    mgr = _fastmcp_tools()
    for tool in WIDENED_TOOLS:
        required = mgr.get_tool(tool).parameters.get("required") or []
        assert "kwargs" not in required, f"{tool} advertises a bogus 'kwargs' arg"


def test_canonical_calls_still_work_through_fastmcp(part):
    """The path _bind_tool bypasses — and therefore the one that breaks silently."""
    mgr = _fastmcp_tools()

    got = asyncio.run(mgr.get_tool("query_geometry").run(
        {"query": {"of": "face", "side": "top"}, "doc_id": part}))
    payload = json.loads(_text(got))
    assert payload["ok"] is True
    assert payload["count"] == 1

    mass = asyncio.run(mgr.get_tool("measure_part").run({"doc_id": part}))
    assert json.loads(_text(mass))["ok"] is True

"""A geometry-query key that does not select must say so.

``core/doc/picks.cpp::resolve_geom_query`` reads the keys it knows and IGNORES the
rest, so a predicate the engine has no concept of widens the query to EVERY
candidate instead of narrowing it. Measured live 2026-08-04 (claude-sonnet-5,
basic-modeling sweep) on a 50 mm cube with one 20 mm bore:

  * ``measure_between(a_query={"of":"face","shape":"cylinder"})`` — ``shape`` is
    the spelling query_geometry's own docstring advertises — came back "matched 7
    candidates", i.e. every face. The flat-kwargs path canonicalised it; the dict
    path never did.
  * ``measure_between(a_query={"sid":"F1b1b80"})`` — the sid query_geometry had
    just handed the model — matched 7 candidates too.
  * ``add_features`` with ``refs.edges=[{"of":"edge","sid":"NOPE_NOT_A_SID"}]``
    SUCCEEDED and filleted every edge on the body. That one is the reason this
    file exists: a wrong key produced wrong geometry, silently.

Every body here is real; nothing is monkeypatched.
"""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn as at
from services.mcp.cad_common import normalize_geom_query


def _call(tool: str, /, **kwargs) -> dict:
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve in the MCP registry"
    bound, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    assert err is None, f"{tool} rejected {sorted(kwargs)}: {err}"
    out = fn(**bound)
    return json.loads(out) if isinstance(out, str) else out


@pytest.fixture(scope="module")
def bored_cube() -> str:
    """50 mm cube with one 20 mm bore: 6 planar faces + 1 cylindrical face."""
    doc = _call("new_part", name="query_guard_cube")["doc_id"]
    built = _call("add_features", doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Cube",
            "params": {"length": {"value": 50}, "width": {"value": 50},
                       "height": {"value": 50}}}},
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "Bore",
            "params": {"diameter": {"value": 20}, "end": "through_all"},
            "refs": {"face": {"of": "face", "side": "top"}}}}])
    assert built.get("ok") is True, built
    return doc


def _bore_sid(doc: str) -> str:
    q = _call("query_geometry", doc_id=doc, query={"of": "face", "surf": "cylinder"})
    matches = q.get("matches") or []
    assert len(matches) == 1, q
    return matches[0]["sid"]


# ── the unit: which keys select, which only look like they do ────────────────

@pytest.mark.parametrize("spelling", ["shape", "surface"])
def test_a_surf_spelling_inside_the_query_dict_is_canonicalised(spelling):
    out, notes = normalize_geom_query({"of": "face", spelling: "cylinder"}, where="q")
    assert out == {"of": "face", "surf": "cylinder"}
    assert notes


@pytest.mark.parametrize("spelling", ["limit", "max", "max_results"])
def test_a_count_spelling_inside_the_query_dict_is_canonicalised(spelling):
    out, _notes = normalize_geom_query({"of": "face", spelling: 2}, where="q")
    assert out == {"of": "face", "count": 2}


def test_a_key_the_engine_never_reads_is_refused_not_ignored():
    with pytest.raises(ValueError) as ei:
        normalize_geom_query({"of": "face", "bogus_key": "zzz"}, where="q")
    assert "bogus_key" in str(ei.value)
    assert "matched EVERY candidate" in str(ei.value)


def test_a_readback_field_alone_cannot_select():
    with pytest.raises(ValueError) as ei:
        normalize_geom_query({"of": "edge", "sid": "E1c4a90"}, where="q")
    assert "READBACK" in str(ei.value)


def test_a_readback_field_rides_along_with_a_real_predicate():
    """Handing the whole match object back is a documented workflow — keep it working."""
    q = {"of": "face", "surf": "cylinder", "sid": "F1b1b80", "centroid": [0, 0, 0],
         "kind": "face", "normal": [0, 0, -1], "radius": 10.0, "concavity": "bore",
         "hint_index": 0, "area_or_len": 3141.6, "axis": [0, 0, -1], "owner_feature": None}
    out, _notes = normalize_geom_query(dict(q), where="q")
    assert out == q


def test_a_bare_domain_query_still_means_every_element():
    """{"of":"edge"} is the documented global-fillet selector; it must not be refused."""
    out, notes = normalize_geom_query({"of": "edge"}, where="q")
    assert out == {"of": "edge"} and not notes


# ── end to end, against real geometry ────────────────────────────────────────

def test_shape_now_narrows_the_query_instead_of_matching_every_face(bored_cube):
    q = _call("query_geometry", doc_id=bored_cube, query={"of": "face", "shape": "cylinder"})
    assert len(q.get("matches") or []) == 1, q


def test_an_inert_key_in_a_query_is_an_error_not_seven_matches(bored_cube):
    q = _call("query_geometry", doc_id=bored_cube, query={"of": "face", "bogus": "zzz"})
    assert q.get("ok") is False and "bogus" in str(q.get("error")), q


def test_a_sid_measures_the_element_it_names(bored_cube):
    sid = _bore_sid(bored_cube)
    for target in ({"sid": sid}, {"kind": "face", "sid": sid}, {"of": "face", "sid": sid}):
        out = _call("measure_between", doc_id=bored_cube, a_query=dict(target))
        assert out.get("ok") is True, (target, out)
        # The bore is a 20 mm through-hole in a 50 mm cube: pi * d * h = pi * 20 * 50.
        assert abs(out["area"] - 3141.592654) < 1e-3, (target, out)


def test_a_stale_sid_is_named_rather_than_widened(bored_cube):
    out = _call("measure_between", doc_id=bored_cube, a_query={"sid": "F000000"})
    assert out.get("ok") is False
    assert "F000000" in str(out.get("error")) and "stable" in str(out.get("error"))


def test_a_bogus_sid_no_longer_fillets_the_whole_body(bored_cube):
    """The live failure: one edge asked for, every edge rounded, no error at all."""
    before = _call("query_geometry", doc_id=bored_cube, query={"of": "face", "surf": "round"})
    out = _call("add_features", doc_id=bored_cube, ops=[
        {"op": "add_feature", "feature": {
            "type": "fillet", "name": "Rogue", "params": {"radius": {"value": 2}},
            "refs": {"edges": [{"of": "edge", "sid": "NOPE_NOT_A_SID"}]}}}])
    assert out.get("ok") is False, out
    assert "READBACK" in str(out.get("error")), out
    after = _call("query_geometry", doc_id=bored_cube, query={"of": "face", "surf": "round"})
    assert len(after.get("matches") or []) == len(before.get("matches") or [])

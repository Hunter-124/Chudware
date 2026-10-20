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
def test_animation_provenance_preserves_grouped_writer_identity():
    """Reading build provenance must not turn a sibling worker into a foreign writer."""
    from services.mcp import cad_animation_tools as anim
    from services.mcp import daemon_bridge

    owner = daemon_bridge.client()
    doc_id = owner.call("doc_new", {"name": "animation_writer_identity"})["doc_id"]
    worker = daemon_bridge.DaemonClient(writer_group=owner.writer_group)
    outsider = daemon_bridge.DaemonClient(writer_group=owner.writer_group + "-outsider")
    try:
        worker.connect(spawn=False)
        outsider.connect(spawn=False)
        assert worker.call("attach_doc", {"doc_id": doc_id, "mode": "rw"})["ok"]
        attached = outsider.call("attach_doc", {"doc_id": doc_id, "mode": "rw"})
        assert attached["ok"]
        assert "another writer" in attached["warning"]
        first = owner.call("doc_apply_ops", {"doc_id": doc_id, "ops": PLATE[:1]})
        assert first["revision"] == 1
        assert "_foreign_edits" not in first

        build_id, _source = anim._engine_build_id()
        assert build_id
        assert build_id == owner.call("daemon_status", {})["engine_build_id"]
        second = worker.call("doc_apply_ops", {"doc_id": doc_id, "ops": PLATE[1:]})
        assert second["revision"] == 2
        assert "_foreign_edits" not in second
        own_state = owner.call("doc_tree", {"doc_id": doc_id})
        assert own_state["revision"] == 2
        assert "_foreign_edits" not in own_state, own_state
        assert len(own_state["tree"]["features"]) == 2

        foreign_state = outsider.call("doc_tree", {"doc_id": doc_id})
        assert foreign_state["_foreign_edits"][0]["revision"] == 2
        edited = outsider.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{
            "op": "add_feature", "feature": {"type": "box", "name": "OtherWriter",
                "params": {"length": 10, "width": 10, "height": 10}}}]})
        assert edited["revision"] == 3
        notified = owner.call("doc_tree", {"doc_id": doc_id})
        assert notified["revision"] == 3
        assert notified["_foreign_edits"][0]["doc_id"] == doc_id
        assert notified["_foreign_edits"][0]["revision"] == 3
        assert notified["_foreign_edits"][0]["method"] == "doc_apply_ops"
    finally:
        outsider.close()
        worker.close()
        owner.call("doc_close", {"doc_id": doc_id})


def test_recovery_refreshes_remap_without_changing_grouped_writer_identity(monkeypatch):
    """A real orphan recovery refreshes stale hello metadata and keeps worker ownership."""
    import tempfile
    import time
    from pathlib import Path
    from services.mcp import cad_animation_tools as anim
    from services.mcp import cad_common
    from services.mcp import daemon_bridge
    from services.mcp import private_hostd

    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
    monkeypatch.setattr(cad_common, "_DOC_ALIASES", {})
    monkeypatch.setattr(cad_common, "_CUR", {"doc_id": None})
    # This daemon has its own short socket path, roots and PID. The package daemon
    # and every document already held by module-scoped fixtures remain untouched.
    with tempfile.TemporaryDirectory(prefix="cw-remap-", dir="/tmp") as directory:
        scratch = Path(directory)
        spawned = private_hostd.spawn_private_daemon(scratch)
        assert spawned is not None, private_hostd.last_failure
        process, socket_path, key = spawned
        monkeypatch.setenv("CHUDWARE_HOSTD_SOCKET", socket_path)
        monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
        owner = daemon_bridge.DaemonClient(writer_group=daemon_bridge._WRITER_GROUP)
        retired = daemon_bridge.DaemonClient(writer_group=key + "-retired")
        worker = daemon_bridge.DaemonClient(writer_group=owner.writer_group)
        monkeypatch.setattr(daemon_bridge, "client", lambda: owner)
        try:
            admission = owner.refresh_server_info()
            assert admission["proto"] == 1
            _early_build_id, early_source = anim._engine_build_id()
            assert early_source.startswith("hostd "), early_source
            assert owner.server_info is admission
            retired.connect(spawn=False)
            old_id = retired.call("doc_new", {"name": "fresh_recovery_remap"})["doc_id"]
            assert retired.call("doc_apply_ops", {"doc_id": old_id, "ops": PLATE[:1]})[
                "revision"] == 1
            retired.close()
            deadline = time.monotonic() + 10
            entries = []
            entry = None
            while time.monotonic() < deadline:
                entries = owner.call("recovery_list", {}).get("entries", [])
                entry = next((row for row in entries if row.get("doc_id") == old_id), None)
                if entry is not None:
                    break
                time.sleep(0.1)
            assert entry is not None, entries
            assert old_id not in owner.server_info.get("id_remap", {})
            opened = owner.call("recovery_open", {"id": entry["id"]})["opened"]
            new_id = next(row["doc_id"] for row in opened if row["original_doc_id"] == old_id)
            assert old_id not in owner.server_info.get("id_remap", {})
            worker.connect(spawn=False)
            assert worker.call("attach_doc", {"doc_id": new_id, "mode": "rw"})["ok"]

            recovered = cad_common._recover_via_daemon()

            assert owner.server_info["id_remap"][old_id] == new_id
            assert cad_common._DOC_ALIASES[old_id] == new_id
            assert any(row["original_doc_id"] == old_id and row["doc_id"] == new_id
                       and row["name"] == "fresh_recovery_remap" for row in recovered)
            ready_build_id, ready_source = anim._engine_build_id()
            assert ready_build_id == owner.call("daemon_status", {})["engine_build_id"]
            assert ready_build_id
            assert ready_source.startswith("hostd ")
            before = owner.call("doc_tree", {"doc_id": new_id})
            edited = worker.call("doc_apply_ops", {"doc_id": new_id, "ops": PLATE[1:]})
            assert edited["revision"] == before["revision"] + 1
            assert "_foreign_edits" not in edited, edited
            after = owner.call("doc_tree", {"doc_id": new_id})
            assert after["revision"] == edited["revision"]
            assert "_foreign_edits" not in after, after
            assert len(after["tree"]["features"]) == 2
            doc = next(row for row in owner.call("doc_list", {})["docs"]
                       if row["doc_id"] == new_id)
            assert doc["origin_kind"] == "mcp"
        finally:
            worker.close()
            retired.close()
            owner.close()
            private_hostd._stop(process, scratch)


# query_geometry: the spellings a model actually writes
# --------------------------------------------------------------------------

def test_canonical_query_carries_current_compact_model_identity(part):
    """Canonical query syntax stays canonical while matches say which model revision they came from."""
    out = _call("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    assert out["ok"] is True
    assert out["doc_id"] == part
    assert out["query"] == {"of": "face", "side": "top"}
    assert out["count"] == 1
    assert "interpreted" not in out
    state = out["model_state"]
    assert isinstance(state["revision"], int)
    assert state["feature_count"] == 2
    assert state["recent_features_returned"] == len(state["recent_features"]) == 2
    assert state["recent_features_limit"] >= state["recent_features_returned"]
    assert state["recent_features_omitted"] == 0
    assert state["recent_features_truncated"] is False
    assert all(set(row) == {"id", "name", "type", "suppressed"}
               for row in state["recent_features"])


def test_query_model_state_roster_is_hard_bounded(monkeypatch):
    """A large feature tree gives query feedback identity, not an unbounded parameter dump."""
    import services.mcp.cad_eval_tools as ev

    features = [{"id": f"f{i}", "name": f"Feature {i}", "type": "box",
                 "suppressed": bool(i % 2)}
                for i in range(10)]
    monkeypatch.setattr(
        ev.cb, "call",
        lambda method, payload: {"revision": 17, "tree": {"features": features}}
        if method == "doc_tree" and payload == {"doc_id": "state-doc"} else None)

    state = ev._query_model_state("state-doc")
    assert state["revision"] == 17 and state["feature_count"] == 10
    assert state["recent_features_limit"] == 8
    assert state["recent_features_returned"] == 8
    assert state["recent_features_omitted"] == 2
    assert state["recent_features_truncated"] is True
    assert [row["id"] for row in state["recent_features"]] == [
        f"f{i}" for i in range(2, 10)]

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


def test_undo_denied_alternatives_actually_resolve():
    """The refusal is exactly as broken as it was before if it names tools that
    cannot be called. Every non-engine-internal tool it recommends must resolve
    through the built-in agent's real dispatch path
    (``services.agent.agent_turn._bind_tool`` — what ``agent_turn.py`` uses, NOT
    FastMCP), and the two engine-internal RPCs it mentions for context must
    genuinely NOT resolve there, proving the "engine-internal" framing is true
    rather than aspirational."""
    from services.mcp.cad_common import _err
    from services.mcp.core_bridge import CoreError
    out = json.loads(_err(CoreError(
        "undo is disabled for mcp/agent connections; agents use "
        "doc_checkpoint/doc_revert or PLM rollback", "undo_denied")))
    hint = out["hint"]
    for name in ("add_features", "plm_timeline", "plm_rollback"):
        assert name in hint
        assert at._bind_tool(name) is not None, (
            f"{name!r} is recommended by the undo_denied hint but does not "
            f"resolve via _bind_tool — the model cannot actually call it")
    # doc_checkpoint/doc_revert are named only as context, explicitly marked
    # engine-internal — they must NOT be presented as callable.
    assert "engine-internal" in hint
    for name in ("doc_checkpoint", "doc_revert"):
        assert at._bind_tool(name) is None, (
            f"{name!r} unexpectedly resolves via _bind_tool — the hint's "
            f"'engine-internal' framing is now wrong, update it")


def test_no_role_card_advertises_undo_or_redo():
    """core/hostd/doc_registry.cpp::preCall UNCONDITIONALLY denies doc_undo/doc_redo
    for kind mcp/agent (CHUDWARE_UNDO_AGENTS=1 kill-switch aside) — a role card that
    advertises undo/redo as a concrete tool guarantees a call that fails identically
    every time."""
    from services.agent.roles import load_all_roles
    for role_id, card in load_all_roles().items():
        named = {t.tool for t in card.tool_allowlist if t.tool}
        denied = named & {"undo", "redo"}
        assert not denied, (
            f"role {role_id!r} advertises {sorted(denied)}, which the daemon "
            f"unconditionally denies for mcp/agent connections")


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
    applied = _call("measure_part", doc_id=part)  # the part's own material (6061, 2.7)
    steel = _call("measure_part", density=7.85, doc_id=part)
    assert steel["mass"]["mass_g"] > applied["mass"]["mass_g"]
    assert "read as density_g_cm3=" in _notes(steel)


@pytest.mark.parametrize(("key", "value"), [
    ("kind", "thickness"),
    ("what", "wall thickness"),
])
def test_measure_part_whole_part_thickness_aliases_share_wall_result(part, key, value):
    """The live `kind="thickness"` payload is the whole-part probe, never a face/edge measure."""
    canonical = _call("wall_thickness", doc_id=part)
    routed = _call("measure_part", doc_id=part, **{key: value})

    # The canonical path retains the pre-alias envelope/order exactly; only the alias gets a
    # rewrite note. AICM field paths are per-call temporary outputs, so compare the real readings.
    assert list(canonical)[:3] == ["ok", "doc_id", "summary"]
    assert "interpreted" not in canonical
    assert set(routed) == set(canonical) | {"interpreted"}
    for field in ("doc_id", "summary", "min_thickness_mm", "mean_thickness_mm",
                  "max_thickness_mm", "n_sampled"):
        assert routed[field] == canonical[field]
    assert f"{key}=" in _notes(routed)
    assert "wall_thickness" in _notes(routed)


def test_measure_part_does_not_guess_a_geometry_target_as_whole_part_thickness(part):
    out = _call("measure_part", what="hole thickness", doc_id=part)
    assert out["ok"] is False
    assert "takes no geometry target" in out["error"]


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


@pytest.fixture(scope="module")
def interference_threshold_doc() -> str:
    """Separate cylinders with overlapping AABBs, plus a remote member."""
    return _new_doc("eval_threshold_cylinders", [
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": name,
            "params": {"radius": 10, "height": 10, "at": at,
                       "centered": False, "merge": False}}}
        for name, at in (("A", [0, 0, 0]), ("B", [15, 15, 0]), ("Far", [100, 100, 0]))
    ])


@pytest.mark.parametrize("surface", ["agent", "fastmcp"])
def test_interference_threshold_and_exact_default_remain_distinct(interference_threshold_doc, surface):
    def check(**params):
        args = {"doc_id": interference_threshold_doc, **params}
        if surface == "agent":
            return _call("check_interference", **args)
        return json.loads(_text(asyncio.run(
            _fastmcp_tools().get_tool("check_interference").run(args))))

    threshold = check(clearance_mode="threshold")
    assert threshold["ok"] and threshold["complete"], threshold
    assert threshold["pairs_checked"] == threshold["pairs_total"] == 3
    assert threshold["interferences"] == threshold["pair_failures"] == []
    assert len(threshold["clearances"]) == 3
    assert all("distance" not in pair and pair["distance_status"] == "lower_bound"
               for pair in threshold["clearances"])
    assert threshold["clearances"][0]["distance_lower_bound_mm"] == 0
    assert "tightest gap" not in threshold["summary"].lower()

    exact = check()
    assert exact["complete"] and exact["pairs_checked"] == 3, exact
    assert len(exact["clearances"]) == 3
    assert exact["clearances"][0]["distance"] == pytest.approx(1.2132034356, abs=1e-6)
    assert all("distance_lower_bound_mm" not in pair for pair in exact["clearances"])
    assert check(clearance_mode="exact")["clearances"] == exact["clearances"]

    scoped = check(clearance_mode="threshold", min_clearance_mm=2, members=["b1", "b2"])
    assert scoped["complete"] and scoped["pairs_checked"] == scoped["pairs_total"] == 1, scoped
    assert scoped["scope"]["total_members"] == 3
    assert len(scoped["clearances"]) == 1
    close = scoped["clearances"][0]
    assert close["distance"] == pytest.approx(1.2132034356, abs=1e-6)
    assert close["warning"] == "below min clearance"
    assert close["distance_status"] == "exact"
    assert "distance_lower_bound_mm" not in close


def test_interference_unknown_clearance_mode_refused(interference_threshold_doc):
    out = _call("check_interference", doc_id=interference_threshold_doc,
                clearance_mode="approximate")
    assert out["ok"] is False
    assert "clearance_mode" in out["error"]
    with pytest.raises(Exception, match="clearance_mode"):
        asyncio.run(_fastmcp_tools().get_tool("check_interference").run(
            {"doc_id": interference_threshold_doc, "clearance_mode": "approximate"}))


def test_interference_narration_does_not_turn_bounds_or_unknowns_into_gaps():
    from services.mcp.fit_narration import _format_fit

    text = _format_fit({"clearances": [
        {"a": "Bound", "b": "B", "distance_lower_bound_mm": 0},
        {"a": "Measured", "b": "B", "distance": 4.25},
        {"a": "Unknown", "b": "B"},
    ]})
    rows = text.splitlines()
    bound = next(row for row in rows if "Bound" in row)
    measured = next(row for row in rows if "Measured" in row)
    unknown = next(row for row in rows if "Unknown" in row)
    assert "lower bound" in bound and "not measured" in bound
    assert "0.00 mm gap" not in bound
    assert "4.25 mm gap" in measured
    assert "not measured" in unknown and "0.00" not in unknown


# --------------------------------------------------------------------------
# The handle round-trip: what these tools TELL the model to do must work
# (live 2026-07-29 — all three of these failed before the fix)
# --------------------------------------------------------------------------

def test_sid_from_query_geometry_measures(part):
    """query_geometry hands back stable sids; measure_between must accept one.

    Both docstrings send the model round this exact loop ("call query_geometry(query)
    first to see the candidates"), and feeding the sid straight back used to raise
    "Can't read 'Fedecf8' as a geometry query". The sid IS the unambiguous handle —
    it is strictly better than the phrase that produced it.
    """
    q, err = _dispatch("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    assert err is None and q["ok"] is True
    sid = q["matches"][0]["sid"]

    m, err = _dispatch("measure_between", a_query=sid, doc_id=part)
    assert err is None, err
    assert m["ok"] is True, m
    assert m["kind"] == "area"
    # The plate is 80x60 with a r=6 bore punched through it.
    assert m["area"] == pytest.approx(80 * 60 - 3.14159265 * 36, rel=2e-3)


def test_sid_measures_the_same_face_the_phrase_did(part):
    """The sid route and the phrase route must agree — otherwise one of them is lying."""
    by_word, err_w = _dispatch("measure_between", a_query="top", doc_id=part)
    assert err_w is None and by_word["ok"] is True
    q, _ = _dispatch("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    by_sid, err_s = _dispatch("measure_between", a_query=q["matches"][0]["sid"], doc_id=part)
    assert err_s is None and by_sid["ok"] is True
    assert by_sid["area"] == pytest.approx(by_word["area"], rel=1e-9)


def test_sid_pair_measures_distance(part):
    """Two sids -> the distance between them. The plate is 10 mm thick."""
    top, _ = _dispatch("query_geometry", query={"of": "face", "side": "top"}, doc_id=part)
    bot, _ = _dispatch("query_geometry", query={"of": "face", "side": "bottom"}, doc_id=part)
    m, err = _dispatch("measure_between", a_query=top["matches"][0]["sid"],
                       b_query=bot["matches"][0]["sid"], doc_id=part)
    assert err is None, err
    assert m["ok"] is True, m
    assert m["distance"] == pytest.approx(10.0, abs=1e-6)


def test_feature_id_names_its_faces_instead_of_teaching_grammar(part):
    """A feature id owns MANY faces, so measuring it is a PICK, not a parse failure.

    Before the fix "f1" hit the token-grammar wall ("the grammar is TOKENS, not prose"),
    which tells a model nothing about what to do next. It must instead name the feature
    and hand over the candidate sids. Deliberately NOT auto-picking one — silently
    measuring one of six faces is the defect class this suite exists to prevent.
    """
    m, err = _dispatch("measure_between", a_query="f1", doc_id=part)
    assert err is None, err
    assert m["ok"] is False
    msg = m["error"]
    assert "Plate" in msg, msg                      # names the feature it resolved
    assert "measuring needs exactly one" in msg, msg
    assert msg.count("F") >= 2, msg                 # hands over real sids to choose from
    assert "TOKENS" not in msg, msg                 # and NOT the old vocabulary wall


def test_feature_name_resolves_like_its_id(part):
    """"Plate" and "f1" are the same handle — both must reach the same teaching."""
    by_id, e1 = _dispatch("measure_between", a_query="f1", doc_id=part)
    by_name, e2 = _dispatch("measure_between", a_query="Plate", doc_id=part)
    assert e1 is None and e2 is None
    assert by_id["ok"] is False and by_name["ok"] is False
    assert "measuring needs exactly one" in by_name["error"]



# --------------------------------------------------------------------------
# A size request is a different TOOL, and the error must say which
# --------------------------------------------------------------------------

@pytest.mark.parametrize("q", ["bbox", "bounding box", "overall size", "extents"])
def test_size_request_names_describe_part_not_the_vocabulary(part, q):
    """query_geometry cannot answer "how big is it" — it must route, not recite grammar.

    measure_part is the WRONG destination (it reports volume/mass/centroid and no bbox);
    describe_part's outline opens with `bbox WxDxH mm`. The message must name the tool
    that actually answers and keep the two apart.
    """
    r, err = _dispatch("query_geometry", query=q, doc_id=part)
    assert err is None, err
    assert r["ok"] is False
    msg = r["error"]
    assert "describe_part" in msg, msg
    assert "does NOT report a bbox" in msg, msg


def test_describe_part_really_carries_the_bbox_we_point_at(part):
    """Guards the redirect: if describe_part stops printing a bbox, this must fail."""
    d, err = _dispatch("describe_part", doc_id=part)
    assert err is None, err
    assert "bbox" in d["outline"]
    assert "80x60x10" in d["outline"].replace(" ", "")


def test_a_real_query_containing_a_size_word_still_resolves(part):
    """The redirect is a FALLBACK. "the top face dimensions" is still a query."""
    r, err = _dispatch("query_geometry", query="the top face dimensions", doc_id=part)
    assert err is None, err
    assert r["ok"] is True, r
    assert r["count"] == 1


def test_measure_part_routes_a_what_argument_to_the_right_tool(part):
    """measure_part has no target — say which tool does, don't just list two parameters.

    Live 2026-07-29: measure_part(what="the hole diameter") was refused with
    "it takes doc_id= and density_g_cm3= only", leaving the model to guess. A refusal
    that names the right tool costs one call; one that doesn't costs several.
    """
    r, err = _dispatch("measure_part", what="the hole diameter", doc_id=part)
    assert err is None, err
    assert r["ok"] is False
    msg = r["error"]
    assert "measure_between" in msg, msg
    assert "describe_part" in msg, msg
    assert "mass properties" in msg, msg


def test_measure_part_canonical_call_is_untouched(part):
    """The routing must not disturb the ordinary call or its density alias."""
    r, err = _dispatch("measure_part", doc_id=part)
    assert err is None and r["ok"] is True
    applied = r["mass"]["mass_g"]
    applied_density = r["mass"]["material"]["density_g_cm3"]
    r2, err2 = _dispatch("measure_part", doc_id=part, density=7.85)
    assert err2 is None and r2["ok"] is True
    # The alias really drives the readout: mass scales with the density it names.
    assert r2["mass"]["material"]["source"] == "explicit"
    assert r2["mass"]["mass_g"] == pytest.approx(applied * 7.85 / applied_density, rel=1e-6)


def test_measure_part_still_refuses_a_genuinely_unknown_argument(part):
    """The router must not become a catch-all that swallows every typo."""
    r, err = _dispatch("measure_part", frobnicate=1, doc_id=part)
    assert err is None, err
    assert r["ok"] is False
    assert "unknown argument" in r["error"]
    assert "density_g_cm3" in r["error"]


def test_return_coords_is_accepted_because_coords_are_always_returned(part):
    """A flag asking for what the tool already does must not fail the call.

    Live 2026-07-29: query_geometry(query="all planar faces", return_coords=True) was
    refused outright, even though every match already carries centroid/normal/axis. The
    flag is dropped WITH a visible note, so nothing is silently ignored.
    """
    r, err = _dispatch("query_geometry", query={"of": "face", "surf": "flat"},
                       return_coords=True, doc_id=part)
    assert err is None, err
    assert r["ok"] is True, r
    assert r["count"] >= 1
    # The thing the flag asked for is genuinely present.
    assert r["matches"][0]["centroid"] is not None
    assert "normal" in r["matches"][0]
    # And the drop was reported, not hidden.
    assert any("return_coords" in n for n in r.get("interpreted") or []), r.get("interpreted")


def test_output_flag_drop_is_a_closed_list_not_a_catch_all(part):
    """An unrelated unknown argument must still be refused."""
    r, err = _dispatch("query_geometry", query={"of": "face", "side": "top"},
                       return_sparkles=True, doc_id=part)
    assert err is None, err
    assert r["ok"] is False
    assert "return_sparkles" in r["error"]


@pytest.mark.parametrize("key", ["what", "measurements", "measures", "properties", "report"])
def test_a_mass_property_ask_is_answered_not_rerouted(part, key):
    """what="mass" is what measure_part RETURNS — routing it away costs a call for nothing.

    Regression from my own first fix, caught by the live sim pass-2b re-run: the routing
    keyed on the ARGUMENT NAME and sent measure_part(what="mass") to measure_between,
    which cannot answer it. The VALUE has to decide.
    """
    r, err = _dispatch("measure_part", **{key: "mass", "doc_id": part})
    assert err is None, err
    assert r["ok"] is True, r
    assert r["mass"]["mass_g"] > 0
    assert any(key in n for n in r.get("interpreted") or []), r.get("interpreted")


def test_a_combined_ask_answers_the_mass_half_and_says_where_bbox_lives(part):
    """"mass and bounding_box": answer the half this tool owns, name the tool for the other.

    Note the UNDERSCORE spelling — a JSON list renders as "['mass', 'bounding_box']", which
    an earlier whitespace-only pattern missed.
    """
    r, err = _dispatch("measure_part", measurements=["mass", "bounding_box"], doc_id=part)
    assert err is None, err
    assert r["ok"] is True, r
    assert r["mass"]["mass_g"] > 0
    notes = " ".join(r.get("interpreted") or [])
    assert "describe_part" in notes, notes
    assert "bbox" in notes, notes


def test_a_geometry_ask_under_a_plural_key_still_routes(part):
    """The plural keys must not become a way to smuggle a geometry target past the router."""
    r, err = _dispatch("measure_part", measurements=["the hole diameter"], doc_id=part)
    assert err is None, err
    assert r["ok"] is False
    assert "measure_between" in r["error"]

# --------------------------------------------------------------------------
# Dispatch-path SYMMETRY: an unknown argument must be refused on the external
# MCP path exactly as it already is on the built-in agent path.
#
# Measured baseline (2026-07-29, before services/mcp/server.py
# _open_varkw_arg_model existed):
#
#   query_geometry(query="top", doc_id=<plate>, return_sparkles=True)
#     agent path -> ok:False "query_geometry() got unknown argument(s)
#                   return_sparkles=True — it takes query=, doc_id=, …"
#     MCP path   -> ok:True, count:1, one matching face, `return_sparkles` GONE
#
# FastMCP folds a var-keyword parameter into ONE ordinary pydantic field and
# leaves the generated arg model at pydantic's default extra="ignore", so
# arg_model.model_validate() dropped every key the tool's own dialect
# normaliser was supposed to judge: a loud refusal became a silently WRONG
# call. These tests fail the moment that drop comes back.
# --------------------------------------------------------------------------

# Otherwise-valid arguments for each widened tool, so the ONLY thing wrong with
# the call under test is the invented key.
VALID_BASE = {
    "query_geometry": {"query": "top"},
    "check_sketch": {"sketch": "s1"},
    "measure": {"targets": ["top"]},
    "measure_part": {},
    "measure_between": {"a_query": "top"},
    "check_interference": {},
    "check_feature_overlap": {},
    "check_symmetry": {"plane": "top"},
    "inspect_section": {"plane": "top"},
    "draft_analysis": {},
}


def _mcp_text(name: str, args: dict) -> str:
    """Drive the EXTERNAL-MCP dispatch path and return everything it said.

    ``server.mcp.call_tool`` -> ``ToolManager.call_tool`` is the seam the
    arg-teaching wrapper owns and the one the wire protocol uses; the fresh
    registry ``_fastmcp_tools()`` builds deliberately does NOT have it.
    ``measure`` raises rather than returning ok:False, so a refusal can arrive
    as a ToolError too — both shapes are loud, and both are read here.
    """
    from services.mcp import server

    try:
        res = asyncio.run(server.mcp.call_tool(name, args))
    except Exception as exc:  # noqa: BLE001 - a raised refusal is still a refusal
        return f"{type(exc).__name__}: {exc}"
    if isinstance(res, tuple):
        content, structured = res
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            return structured["result"]
        res = content
    if isinstance(res, list):
        return " ".join(getattr(b, "text", str(b)) for b in res)
    return res if isinstance(res, str) else str(res)


@pytest.mark.parametrize("tool", WIDENED_TOOLS)
def test_mcp_path_refuses_what_the_agent_path_refuses(part, tool):
    """One invented key, both dispatch paths, same verdict."""
    args = dict(VALID_BASE[tool], doc_id=part, return_sparkles=True)

    # The agent path refuses either in the reconciler, in a returned ok:False
    # payload, or by raising (`measure` has no try/except) — all three are loud.
    try:
        payload, agent_err = _dispatch(tool, **args)
    except ValueError as exc:
        agent_err = str(exc)
    else:
        agent_err = agent_err or json.dumps(payload or {})
    assert "return_sparkles" in agent_err, f"agent path did not refuse: {agent_err}"

    said = _mcp_text(tool, args)
    assert "return_sparkles" in said, (
        f"{tool}: the MCP path DROPPED return_sparkles instead of refusing it — {said[:400]}")
    assert '"ok": true' not in said.lower(), f"{tool} reported success anyway: {said[:400]}"


@pytest.mark.parametrize("tool,args", [
    # Every one of these spellings is a KNOWN synonym the tool bodies adopt. They
    # travel the same extras channel as `return_sparkles`, so a guard built on
    # extra="forbid" would refuse them — which is why the fix forwards the keys
    # to the tool instead of second-guessing them here.
    ("query_geometry", {"of": "face", "side": "top"}),
    ("query_geometry", {"target": "top"}),
    ("query_geometry", {"face": "top"}),
    ("query_geometry", {"query": "all planar faces", "return_coords": True}),
    ("measure_part", {"density": 2.7}),
    ("measure_between", {"a": "top", "b": "bottom"}),
    ("check_interference", {"coincident": True}),
    ("check_interference", {"clearance": 0.5}),
    ("check_symmetry", {"about": "top"}),
    ("inspect_section", {"at": "top"}),
    ("check_feature_overlap", {"feature": "Plate"}),
    ("draft_analysis", {"direction": "+Z"}),
])
def test_valid_synonyms_still_reach_the_tool_through_the_mcp_path(part, tool, args):
    said = _mcp_text(tool, dict(args, doc_id=part))
    assert '"ok": true' in said.lower(), f"{tool}({', '.join(args)}) was refused: {said[:400]}"


def test_measure_still_renders_through_the_mcp_path(part):
    """`measure` returns image content, not JSON — the extras channel must not
    disturb a tool whose result never goes through an output model."""
    said = _mcp_text("measure", {"selectors": ["top"], "doc_id": part})
    assert "unknown argument" not in said, said[:400]
    assert "Error" not in said, said[:400]

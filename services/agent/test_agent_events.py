"""test_agent_events.py — M20 contract/unit tests for `aicad.agent_events/2`.

Proves each net-new `/2` agent-level event kind:
  * serializes with §2.1 parent-linkage (`id` span, `parent_id`, `actor`),
  * carries its kind-specific fields (esp. the human-readable `target` on tool_call),
  * round-trips through the EventJournal to events.jsonl (the replay/drain source),
  * fans out to BOTH a live sink and the journal without double-writing.

Provider-gated items (live token streaming for `agent.thought.delta`) are asserted
as SCHEMA-only here and marked gated — no live provider is exercised.
"""
from __future__ import annotations

import json
import os

import pytest

from services.agent import agent_events as ae
from services.dag.event_journal import EventJournal, run_events_path


# ---------------------------------------------------------------------------
# Builders: linkage + kind-specific fields
# ---------------------------------------------------------------------------

def _has_linkage(payload: dict) -> bool:
    # §2.1: `id` REQUIRED on every agent-level event; parent_id/actor optional.
    return "id" in payload and isinstance(payload["id"], str) and payload["id"]


def test_span_ids_are_unique():
    assert ae.new_span_id() != ae.new_span_id()


def test_tool_call_carries_target_and_linkage():
    kind, p = ae.tool_call_event(
        "n_build", "measure_part", {"part": "Bracket", "tol": 0.1},
        {"ok": True, "value": 42}, ok=True, latency_ms=12.5,
        parent_id="turn_span", actor="cad_designer")
    assert kind == "agent.tool_call"
    assert _has_linkage(p)
    assert p["parent_id"] == "turn_span"
    assert p["actor"] == "cad_designer"
    # target is a plain-language string (NOT a hash), best-effort from args.
    assert p["target"] == "Bracket"
    assert p["tool_name"] == "measure_part"
    assert p["ok"] is True
    assert p["latency_ms"] == 12.5
    # digests remain (bound telemetry vocabulary).
    assert len(p["args_digest"]) == 16 and len(p["result_digest"]) == 16


def test_target_falls_back_to_scalar_join_then_empty():
    # No known target key → compact scalar join.
    _, p = ae.tool_call_event("n", "t", {"count": 3, "flag": True}, {}, ok=True,
                              latency_ms=1.0)
    assert "count=3" in p["target"]
    # Nothing legible → empty string (contract: "" when nothing legible).
    _, p2 = ae.tool_call_event("n", "t", {}, {}, ok=True, latency_ms=1.0)
    assert p2["target"] == ""


def test_target_prefers_face_over_generic():
    _, p = ae.tool_call_event("n", "fillet", {"face": "top face of Part1", "r": 2},
                              {}, ok=True, latency_ms=1.0)
    assert p["target"] == "top face of Part1"


def test_new_part_target_is_part_name_not_doc_id():
    # new_part(name=…) — Doing row must show the human name.
    _, p = ae.tool_call_event(
        "n", "new_part", {"name": "mud_bucket"},
        {"ok": True, "doc_id": "doc-cpp00000002", "name": "mud_bucket"},
        ok=True, latency_ms=1.0)
    assert p["target"] == "mud_bucket"
    assert "doc-cpp" not in p["target"]


def test_add_features_opaque_doc_id_resolved_via_doc_list(monkeypatch):
    # add_features(doc_id=doc-cpp…) with no name in args — resolve via doc_list.
    monkeypatch.setattr(
        ae, "_resolve_doc_name",
        lambda doc_id: "mud_bucket" if doc_id == "doc-cpp00000002" else "")
    _, p = ae.tool_call_event(
        "n", "add_features",
        {"doc_id": "doc-cpp00000002", "ops": [{"op": "add_feature"}]},
        {"ok": True}, ok=True, latency_ms=1.0)
    assert p["target"] == "mud_bucket"


def test_add_features_prefers_ops_feature_summary_over_opaque_id(monkeypatch):
    # When ops carry a feature name, that wins over an unresolvable doc_id.
    monkeypatch.setattr(ae, "_resolve_doc_name", lambda _doc_id: "")
    _, p = ae.tool_call_event(
        "n", "add_features",
        {"doc_id": "doc-cpp00000009",
         "ops": [{"op": "add_feature",
                  "feature": {"type": "extrude", "name": "BucketWall",
                              "params": {"depth": 40}}}]},
        {"ok": True}, ok=True, latency_ms=1.0)
    assert p["target"] == "BucketWall (extrude)"
    assert "doc-cpp" not in p["target"]


def test_opaque_doc_id_alone_yields_empty_without_resolver(monkeypatch):
    # Never surface doc-cpp… as the human target when resolve fails.
    monkeypatch.setattr(ae, "_resolve_doc_name", lambda _doc_id: "")
    assert ae.derive_target({"doc_id": "doc-cpp00000001"}) == ""


def test_tool_call_prefers_result_name_over_opaque_args_target(monkeypatch):
    monkeypatch.setattr(ae, "_resolve_doc_name", lambda _doc_id: "")
    _, p = ae.tool_call_event(
        "n", "add_features", {"doc_id": "doc-cpp00000003"},
        {"ok": True, "name": "mud_bucket", "doc_id": "doc-cpp00000003"},
        ok=True, latency_ms=1.0)
    assert p["target"] == "mud_bucket"


def test_thought_block_and_delta_kinds():
    k, p = ae.thought_event("n", "considering the load path", model="opus",
                            parent_id="turn", actor="cad")
    assert k == "agent.thought"
    assert _has_linkage(p) and p["parent_id"] == "turn"
    assert p["text"] == "considering the load path"
    assert p["model"] == "opus"
    # /2 forward-compat delta variant (producer-gated; schema only).
    kd, pd = ae.thought_event("n", "tok", delta=True)
    assert kd == "agent.thought.delta"
    assert _has_linkage(pd)


def test_thought_truncates_at_500():
    _, p = ae.thought_event("n", "x" * 900)
    assert len(p["text"]) == 500 and p["text"].endswith("…")


def test_context_injected_event():
    k, p = ae.context_injected_event("n", "ledger", "prior render + build plan",
                                     token_est=1200, parent_id="turn", actor="cad")
    assert k == "agent.context_injected"
    assert _has_linkage(p)
    assert p["source"] == "ledger"
    assert p["label"] == "prior render + build plan"
    assert p["token_est"] == 1200


def test_spawn_event_links_parent_and_child():
    k, p = ae.spawn_event("root_span", "child_span", "reviewer", kind="worker",
                          brief="review the bracket", budget_tokens=5000,
                          span_id="child_span", parent_id="root_span",
                          actor="orchestrator")
    assert k == "agent.spawn"
    assert _has_linkage(p)
    assert p["parent_id"] == "root_span"      # links to spawner
    assert p["subagent_id"] == "child_span"   # the child's execution root
    assert p["id"] == "child_span"            # child span id == its node id
    assert p["role"] == "reviewer" and p["spawn_kind"] == "worker"
    assert p["budget_tokens"] == 5000
    assert "kind" not in p                     # reserved envelope key not shadowed


def test_advisor_event():
    k, p = ae.advisor_event("doc1", "council", "approved: build order sound",
                            actor="design_thinker")
    assert k == "agent.advisor"
    assert _has_linkage(p)
    assert p["advisor_kind"] == "council"
    assert "kind" not in p                     # reserved envelope key not shadowed
    assert p["verdict_summary"] == "approved: build order sound"


# ---------------------------------------------------------------------------
# Journal round-trip: every /2 kind persists to events.jsonl and reads back.
# ---------------------------------------------------------------------------

def _read_journal(run_id, runs_dir):
    path = run_events_path(run_id, runs_dir)
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return lines


def test_all_new_kinds_round_trip_through_journal(tmp_path):
    run_id = "m20_rt"
    journal = EventJournal(run_id, runs_dir=tmp_path)
    events = [
        ae.tool_call_event("n", "measure_part", {"part": "P"}, {}, ok=True,
                           latency_ms=1.0, parent_id="t", actor="a"),
        ae.thought_event("n", "hmm", parent_id="t", actor="a"),
        ae.context_injected_event("n", "memory", "recent facts", parent_id="t"),
        ae.spawn_event("t", "c", "reviewer", span_id="c", parent_id="t"),
        ae.advisor_event("n", "advise", "ok", parent_id="t"),
    ]
    for kind, payload in events:
        journal.emit(kind, payload)

    lines = _read_journal(run_id, tmp_path)
    assert len(lines) == 5
    kinds = [rec["kind"] for rec in lines]
    assert kinds == [
        "agent.tool_call", "agent.thought", "agent.context_injected",
        "agent.spawn", "agent.advisor",
    ]
    # Envelope (§2) + linkage (§2.1) present on every persisted record.
    for rec in lines:
        assert rec["run_id"] == run_id          # envelope
        assert "ts" in rec and "kind" in rec    # envelope
        assert "id" in rec                       # §2.1 span id
        assert rec.get("node_id")                # kind field


def test_spawn_advisor_kind_field_does_not_clobber_discriminator(tmp_path):
    # Regression: a payload sub-field named `kind` would overwrite the envelope
    # discriminator when EventJournal spreads the payload (**payload). spawn/advisor
    # therefore use spawn_kind/advisor_kind. Prove the persisted `kind` is the
    # event kind, not the sub-value.
    run_id = "m20_kind"
    journal = EventJournal(run_id, runs_dir=tmp_path)
    journal.emit(*ae.spawn_event("t", "c", "reviewer", kind="ponder", span_id="c"))
    journal.emit(*ae.advisor_event("n", "design_review", "looks good"))
    lines = _read_journal(run_id, tmp_path)
    assert [r["kind"] for r in lines] == ["agent.spawn", "agent.advisor"]
    assert lines[0]["spawn_kind"] == "ponder"
    assert lines[1]["advisor_kind"] == "design_review"


# ---------------------------------------------------------------------------
# emit_event fan-out: both sink conventions + no double-write.
# ---------------------------------------------------------------------------

def test_emit_event_dispatches_two_arg_live_sink():
    seen = []
    ae.emit_event("agent.thought", {"node_id": "n", "id": "s", "text": "x"},
                  live=lambda k, p: seen.append((k, p)))
    assert seen and seen[0][0] == "agent.thought"


def test_emit_event_dispatches_single_dict_live_sink():
    seen = []

    def loop_style_sink(data):        # loop.py convention: kind is INSIDE the dict
        seen.append(data)

    ae.emit_event("agent.tool_call", {"node_id": "n", "id": "s", "tool_name": "t"},
                  live=loop_style_sink)
    assert seen and seen[0]["kind"] == "agent.tool_call"
    assert seen[0]["tool_name"] == "t"


def test_emit_event_fans_out_to_both_sinks_once(tmp_path):
    run_id = "m20_fan"
    journal = EventJournal(run_id, runs_dir=tmp_path)
    live_seen = []
    kind, payload = ae.tool_call_event("n", "t", {"part": "P"}, {}, ok=True,
                                       latency_ms=1.0)
    ae.emit_event(kind, payload, live=lambda k, p: live_seen.append(k),
                  journal=journal)
    # Live sink hit exactly once.
    assert live_seen == ["agent.tool_call"]
    # Journal hit exactly once (no double-write).
    lines = _read_journal(run_id, tmp_path)
    assert len(lines) == 1 and lines[0]["kind"] == "agent.tool_call"


def test_agent_turn_wrapped_sink_plus_run_id_persists_exactly_once(tmp_path, monkeypatch):
    # Production DAG shape: the executor threads a `journal.wrap(sink)` emit AS on_event
    # AND a run_id is in scope. agent_turn must NOT create a SECOND journal (that was
    # the double-write risk). Prove events.jsonl gets exactly one tool_call line.
    import services.dag.event_journal as ej
    monkeypatch.setattr(ej, "_default_cache_runs_dir", lambda: tmp_path)
    from services.dag.event_journal import EventJournal as _EJ
    from services.agent import agent_turn

    run_id = "m20_wrapped"
    live_seen = []
    wrapped = _EJ(run_id).wrap(lambda k, p: live_seen.append(k))  # persist + live, like executor

    res = agent_turn.run_turn(
        "measure", _ScriptedProvider(), allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        on_event=wrapped, run_id=run_id, node_id="n1")
    assert res.success
    # Live sink saw the tool_call.
    assert "agent.tool_call" in live_seen
    # events.jsonl has EXACTLY one tool_call (no double-write from a 2nd journal).
    lines = _read_journal(run_id, tmp_path)
    tool_calls = [r for r in lines if r["kind"] == "agent.tool_call"]
    assert len(tool_calls) == 1, tool_calls


def test_flaky_sink_never_raises(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("ui exploded")
    # Must swallow — a flaky UI sink cannot break the agent loop.
    ae.emit_event("agent.thought", {"node_id": "n", "id": "s"}, live=boom)


# ---------------------------------------------------------------------------
# Integration: agent_turn emits agent.tool_call through the drain (mocked provider).
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """Emits one tool call then finishes — no live cloud."""
    name = "mock"

    def __init__(self):
        self._step = 0

    def complete(self, system, messages, max_tokens=2048):
        self._step += 1
        if self._step == 1:
            return '```json\n{"tool": "measure_part", "args": {"part": "Bracket"}}\n```'
        return '```json\n{"done": true, "handoff": {"text": "measured"}}\n```'


def test_agent_turn_streams_tool_call_to_live_sink():
    from services.agent import agent_turn

    seen = []
    res = agent_turn.run_turn(
        "measure the bracket", _ScriptedProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True, "mm": 42},
        on_event=lambda k, p: seen.append((k, p)),
        node_id="n_measure")
    assert res.success

    tool_calls = [(k, p) for (k, p) in seen if k == "agent.tool_call"]
    assert len(tool_calls) == 1
    _, p = tool_calls[0]
    assert p["tool_name"] == "measure_part"
    assert p["target"] == "Bracket"           # human target, from args
    assert p["node_id"] == "n_measure"
    assert "id" in p and "parent_id" in p      # linkage: child of the turn span
    assert p["ok"] is True
    assert isinstance(p["latency_ms"], (int, float))


def test_agent_turn_no_sink_is_silent_and_still_works():
    from services.agent import agent_turn
    # No on_event / run_id → no emission, turn still runs (back-compat).
    res = agent_turn.run_turn(
        "go", _ScriptedProvider(), allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True})
    assert res.success


def test_agent_turn_run_id_without_sink_persists_to_journal(tmp_path, monkeypatch):
    # Headless replay path: run_id given, no live sink → standalone journal writes.
    import services.dag.event_journal as ej
    monkeypatch.setattr(ej, "_default_cache_runs_dir", lambda: tmp_path)
    from services.agent import agent_turn

    res = agent_turn.run_turn(
        "measure", _ScriptedProvider(), allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        run_id="m20_headless", node_id="n1")
    assert res.success
    lines = _read_journal("m20_headless", tmp_path)
    tool_calls = [r for r in lines if r["kind"] == "agent.tool_call"]
    assert len(tool_calls) == 1
    # target derives from the tool-call args ({"part": "Bracket"}), NOT the request.
    assert tool_calls[0]["target"] == "Bracket"
    assert tool_calls[0]["run_id"] == "m20_headless"


def test_derive_artifacts_suppresses_internal_scratch(tmp_path):
    """cad_common._tmp() scratch files (chudware_mcp_*) must never surface in the
    GUI Artifacts panel, while real export deliverables survive."""
    import tempfile
    scratch = os.path.join(tempfile.gettempdir(), "chudware_mcp_klyzyi3m.step+")
    real_export = str(tmp_path / "CD-900_Flyswatter.step")
    arts = ae.derive_artifacts({"step_path": scratch, "stl_path": real_export})
    paths = [a["path"] for a in arts]
    assert scratch not in paths
    assert real_export in paths


def test_derive_artifacts_keeps_user_file_under_temp_dir(tmp_path):
    """Location alone is never a suppression signal: a user-requested export that
    merely lives under the system temp dir (no scratch prefix) is a deliverable."""
    import tempfile
    user_file = os.path.join(tempfile.gettempdir(), "my_part_export.step")
    arts = ae.derive_artifacts({"step_path": user_file})
    assert user_file in [a["path"] for a in arts]


def test_derive_artifacts_rejects_render_caption_prose():
    """A render caption is a SENTENCE, not a path. _scene_caption starts with
    '/'.join(view_names), so a multi-view render's caption contains a slash — the
    old "any string with a '/'" rule listed it under 'Produced This Session'
    (live 2026-07-28)."""
    caption = ("top/front/right view of a 58x26x4 mm part. front: f12(top), "
               "f3(side) +2 more. Computed from B-rep normals; occlusion not "
               "modeled.")
    assert ae.derive_artifacts([caption]) == []
    # …and the PNG beside it still survives.
    arts = ae.derive_artifacts(["/tmp/render_out.png", caption])
    assert [a["path"] for a in arts] == ["/tmp/render_out.png"]


def test_derive_artifacts_reads_pathlike_image_objects(tmp_path):
    """fastmcp Image.path is a PosixPath, not a str — the old isinstance(str)
    guard dropped every render artifact on the floor."""
    from pathlib import Path

    class FakeImage:            # same duck type as mcp.server.fastmcp.Image
        def __init__(self, p):
            self.path = Path(p)

    png = str(tmp_path / "view_iso.png")
    arts = ae.derive_artifacts([FakeImage(png), "a caption"])
    assert [a["path"] for a in arts] == [png]
    assert arts[0]["kind"] == "image"



def test_tool_call_read_tool_yields_no_artifacts(tmp_path):
    """Read tools (fs_read, fs_list, read-like describe_*) must NOT contribute
    artifacts — their results are full of paths they merely touched, which was
    the '93 Produced This Session rows' pollution."""
    touched = str(tmp_path / "guide.md")
    _, p = ae.tool_call_event(
        "n", "fs_read", {"path": touched},
        {"ok": True, "path": touched, "content": "..."},
        ok=True, latency_ms=1.0)
    assert "artifacts" not in p
    _, p2 = ae.tool_call_event(
        "n", "fs_list", {"path": str(tmp_path)},
        {"ok": True, "paths": [touched, str(tmp_path / "other.step")]},
        ok=True, latency_ms=1.0)
    assert "artifacts" not in p2


def test_tool_call_producer_tool_yields_export_path(tmp_path):
    """A producing tool still surfaces its real export deliverable."""
    export = str(tmp_path / "CD-900_Flyswatter.step")
    _, p = ae.tool_call_event(
        "n", "export_part", {"fmt": "step", "path": export},
        {"ok": True, "path": export},
        ok=True, latency_ms=1.0)
    paths = [a["path"] for a in p.get("artifacts", [])]
    assert export in paths


def test_tool_call_explicit_artifacts_honoured_for_any_tool(tmp_path):
    """An explicit artifacts= argument is a caller asserting a real deliverable —
    it must pass through regardless of the tool name."""
    deliverable = str(tmp_path / "summary.png")
    explicit = [{"path": deliverable, "kind": "image"}]
    _, p = ae.tool_call_event(
        "n", "measure_part", {"part": "P"}, {"ok": True},
        ok=True, latency_ms=1.0, artifacts=explicit)
    assert p["artifacts"] == explicit

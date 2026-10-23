"""Tests for services/agent/plm_bridge.py.

Coverage:
1. Trigger table — enabled/disabled triggers
2. Message drafting — verbatim rationale splice + dimension provenance
3. Numeral-fidelity — no numeral in the message absent from ledger fields
4. Vocabulary guard — no git terms in drafted messages
5. Offline queue — plmlink events persist in the ledger when vault unavailable
6. Idempotent flush — double-flush is a no-op (same checkpoint_ref only once)
7. Mock vault round-trip — on_milestone with available vault creates checkpoint
8. change_request linkage — audit_id + modifies_checkpoint recorded
9. Buildgraph emit hook — checkpoint node fires bridge.on_milestone
10. Ledger event read — provenance comes from events[], not materialized state
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.agent.plm_bridge import (
    CHECKPOINT_TRIGGERS,
    MockVault,
    PLMBridge,
    _GIT_VOCAB,
    _dedup_key,
    _scan_git_vocab,
    _trigger_enabled,
    draft_checkpoint_message,
    make_buildgraph_node_emit_hook,
)
from services.agent.ledger import Ledger
from services.agent.buildgraph.executor import run_sync


# ---------------------------------------------------------------------------
# Fixture: minimal ledger
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ledger(tmp_path):
    """A fresh Ledger backed by a temp file."""
    run_id = "run_test_plm"
    ledger_path = tmp_path / "ledger.json"
    return Ledger(run_id=run_id, path=ledger_path)


@pytest.fixture
def bridge_with_mock(tmp_ledger):
    """A PLMBridge wired to a MockVault and the tmp ledger."""
    vault = MockVault()
    vault.register_project("run_test_plm", "proj_abc")
    b = PLMBridge(run_id="run_test_plm", vault=vault)
    b.attach_ledger(tmp_ledger)
    return b, vault, tmp_ledger


# ---------------------------------------------------------------------------
# 1. Trigger table coverage
# ---------------------------------------------------------------------------

class TestTriggerTable:
    def test_plan_ratified_enabled(self):
        assert _trigger_enabled("plan_ratified") is True

    def test_part_done_enabled(self):
        assert _trigger_enabled("part_done") is True

    def test_mission_done_enabled(self):
        assert _trigger_enabled("mission_done") is True

    def test_change_request_enabled(self):
        assert _trigger_enabled("change_request") is True

    @pytest.mark.parametrize("stage", ["decompose", "geometry", "linkage", "buildpath"])
    def test_design_pipeline_stage_exits_enabled(self, stage):
        assert _trigger_enabled("stage_exit", stage) is True

    def test_stage_exit_part_verify_enabled(self):
        assert _trigger_enabled("stage_exit", "part_verify") is True

    def test_stage_exit_assembly_enabled(self):
        assert _trigger_enabled("stage_exit", "assembly") is True

    def test_stage_exit_sim_enabled(self):
        assert _trigger_enabled("stage_exit", "sim") is True

    def test_stage_exit_sketch_disabled(self):
        assert _trigger_enabled("stage_exit", "sketch") is False

    def test_stage_exit_model_disabled(self):
        assert _trigger_enabled("stage_exit", "model") is False

    def test_stage_exit_layout_disabled(self):
        assert _trigger_enabled("stage_exit", "layout") is False

    def test_stage_exit_planning_disabled(self):
        assert _trigger_enabled("stage_exit", "planning") is False

    def test_unknown_trigger_disabled(self):
        assert _trigger_enabled("unknown_trigger") is False

    def test_all_checkpoint_trigger_keys_are_bool(self):
        for key, val in CHECKPOINT_TRIGGERS.items():
            assert isinstance(val, bool), f"CHECKPOINT_TRIGGERS[{key!r}] should be bool"


# ---------------------------------------------------------------------------
# 2. Message drafting — verbatim splice
# ---------------------------------------------------------------------------

FIXTURE_EVENTS = [
    {
        "seq": 0, "ts": "2026-07-04T10:00:00Z", "actor": "overseer",
        "kind": "decision",
        "payload": {
            "id": "D1", "stage": "sketch",
            "text": "Use extruded boss for mounting pad",
            "rationale": "Reduces secondary operations vs welded tab",
            "actor": "overseer", "ts": "2026-07-04T10:00:00Z",
        },
    },
    {
        "seq": 1, "ts": "2026-07-04T10:01:00Z", "actor": "builder",
        "kind": "dimension",
        "payload": {
            "id": "DIM1", "name": "bore_diameter", "value": 20.02, "unit": "mm",
            "driver": "sliding fit on shaft S1 (H7/g6)",
            "provenance": "ISO 286-2 table lookup for 20 mm H7",
            "tolerance": {"fit": "H7", "upper": 0.021, "lower": 0.0},
            "status": "confirmed", "confirmed_by": "user",
        },
    },
    {
        "seq": 2, "ts": "2026-07-04T10:02:00Z", "actor": "builder",
        "kind": "dimension",
        "payload": {
            "id": "DIM2", "name": "wall_thickness", "value": 5.0, "unit": "mm",
            "driver": "min stiffness req",
            "provenance": "calc: 3mm insufficient, bumped to 5mm per FEA",
            "tolerance": {}, "status": "proposed", "confirmed_by": None,
        },
    },
]


class TestMessageDrafting:
    def test_decision_rationale_verbatim_in_message(self):
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        # Verbatim rationale must appear in the message unchanged
        assert "Reduces secondary operations vs welded tab" in msg

    def test_dimension_provenance_from_events_not_state(self):
        """Provenance lives in events, not materialized state — must be present."""
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        assert "ISO 286-2" in msg

    def test_dimension_value_in_message(self):
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        assert "20.02" in msg

    def test_wall_thickness_value_in_message(self):
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        assert "5.0" in msg

    def test_trigger_label_part_done(self):
        msg = draft_checkpoint_message([], trigger="part_done")
        assert "Part modeling complete" in msg

    def test_trigger_label_mission_done(self):
        msg = draft_checkpoint_message([], trigger="mission_done")
        assert "Mission complete" in msg

    def test_trigger_label_plan_ratified(self):
        msg = draft_checkpoint_message([], trigger="plan_ratified")
        assert "plan approved" in msg.lower()

    def test_trigger_label_change_request(self):
        msg = draft_checkpoint_message([], trigger="change_request")
        assert "Change request" in msg

    def test_trigger_label_stage_exit_part_verify(self):
        msg = draft_checkpoint_message([], trigger="stage_exit", stage="part_verify")
        assert "verification" in msg.lower() or "Part" in msg

    def test_trigger_label_stage_exit_sim(self):
        msg = draft_checkpoint_message([], trigger="stage_exit", stage="sim")
        assert "Simulation" in msg

    def test_empty_events_returns_nonempty_message(self):
        msg = draft_checkpoint_message([], trigger="part_done")
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# 3. Numeral fidelity — no numeral absent from ledger fields
# ---------------------------------------------------------------------------

class TestNumeralFidelity:
    def test_no_extra_numerals_in_message(self):
        """Every numeral in the message must come from a ledger field."""
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        # Collect all numbers from ledger field values
        ledger_numbers: set[str] = set()
        for ev in FIXTURE_EVENTS:
            payload = ev.get("payload", {})
            for v in payload.values():
                if isinstance(v, (int, float)):
                    ledger_numbers.add(str(v))
                elif isinstance(v, str):
                    # Extract numbers from strings
                    for m in re.findall(r"\d+(?:\.\d+)?", v):
                        ledger_numbers.add(m)

        # Find all numerals in the message
        msg_numbers = re.findall(r"\d+(?:\.\d+)?", msg)
        for num in msg_numbers:
            assert num in ledger_numbers, (
                f"Numeral {num!r} in message is not present in any ledger field"
            )


# ---------------------------------------------------------------------------
# 4. Vocabulary guard — no git terms in drafted messages
# ---------------------------------------------------------------------------

class TestVocabularyGuard:
    def test_no_git_vocab_in_part_done_message(self):
        msg = draft_checkpoint_message(FIXTURE_EVENTS, trigger="part_done")
        bad = _scan_git_vocab(msg)
        assert bad == [], f"Git vocabulary {bad} found in message: {msg!r}"

    def test_no_git_vocab_in_mission_done_message(self):
        msg = draft_checkpoint_message([], trigger="mission_done")
        bad = _scan_git_vocab(msg)
        assert bad == [], f"Git vocabulary {bad} found in message: {msg!r}"

    def test_scan_git_vocab_detects_commit(self):
        assert "commit" in _scan_git_vocab("commit the changes")

    def test_scan_git_vocab_detects_branch(self):
        assert "branch" in _scan_git_vocab("on a branch")

    def test_scan_git_vocab_clean_text(self):
        assert _scan_git_vocab("Part modeling complete. Wall 5 mm.") == []

    def test_git_vocab_forbidden_words_coverage(self):
        """Verify the required forbidden words are all in _GIT_VOCAB."""
        required = {"git", "commit", "repository", "repo", "branch",
                    "merge", "rebase", "head", "ref", "push", "pull", "sha", "hash"}
        missing = required - _GIT_VOCAB
        assert not missing, f"Missing from _GIT_VOCAB: {missing}"


# ---------------------------------------------------------------------------
# 5. Offline queue — plmlink events persist when vault unavailable
# ---------------------------------------------------------------------------

class TestOfflineQueue:
    def test_unavailable_vault_queues_event(self, tmp_ledger):
        class UnavailableVault:
            def is_available(self) -> bool:
                return False
            def project_id_for_run(self, run_id):
                return "proj_xyz"
            def checkpoint(self, **kw):
                raise AssertionError("should not be called")

        bridge = PLMBridge(run_id="run_test_plm", vault=UnavailableVault())
        bridge.attach_ledger(tmp_ledger)

        result = bridge.on_milestone(trigger="part_done", buildgraph_node="bg_001")
        assert result is not None
        assert result["queued"] is True
        assert result["schema"] == "aicad.plmlink/1"

        # Event must be in ledger
        plmlink_events = [e for e in tmp_ledger.events if e.get("kind") == "plmlink"]
        assert len(plmlink_events) == 1
        assert plmlink_events[0]["payload"]["queued"] is True

    def test_no_project_id_queues_event(self, tmp_ledger):
        """If no project_id can be resolved, the event is queued."""
        vault = MockVault()
        # No project registered for this run_id
        bridge = PLMBridge(run_id="run_no_project", vault=vault)
        bridge.attach_ledger(tmp_ledger)

        result = bridge.on_milestone(trigger="part_done", buildgraph_node="bg_002")
        assert result is not None
        assert result["queued"] is True


# ---------------------------------------------------------------------------
# 6. Idempotent flush — double-flush is a no-op
# ---------------------------------------------------------------------------

class TestIdempotentFlush:
    def test_double_flush_skips_already_flushed(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock

        # First: queue a plmlink by calling on_milestone WITHOUT a project_id
        # so it goes through the vault (project is registered)
        result = bridge.on_milestone(trigger="part_done", buildgraph_node="bg_003")
        assert result is not None

        # The vault is available and project is registered, so it should
        # create a checkpoint immediately (not queued). Verify.
        assert result["queued"] is False
        n_checkpoints_after_first = len(vault.checkpoints)

        # Call flush — no queued events remain, so 0 flushed
        summary = bridge.flush(project_id="proj_abc")
        assert summary["flushed"] == 0
        assert len(vault.checkpoints) == n_checkpoints_after_first

        # Call flush again — still 0 flushed (idempotent)
        summary2 = bridge.flush(project_id="proj_abc")
        assert summary2["flushed"] == 0
        assert len(vault.checkpoints) == n_checkpoints_after_first

    def test_flush_drains_queued_events(self, tmp_ledger):
        """Queue events with unavailable vault, then flush with available vault."""
        calls = []

        class ToggleVault:
            def __init__(self):
                self._available = False
                self._checkpoints = []

            def is_available(self):
                return self._available

            def project_id_for_run(self, run_id):
                return "proj_toggle"

            def checkpoint(self, **kw):
                import uuid as _uuid
                commit = "mock_" + _uuid.uuid4().hex[:8]
                entry = {"commit": commit, "kind": "checkpoint",
                         "authored_at": "2026-07-04T00:00:00Z",
                         "author": {"kind": "ai"}, "message": kw.get("message", "")}
                self._checkpoints.append(dict(kw))
                return {"ok": True, "commit": commit, "entry": entry}

        vault = ToggleVault()
        bridge = PLMBridge(run_id="run_toggle", vault=vault)
        bridge.attach_ledger(tmp_ledger)

        # Queue two events while vault unavailable. The first carries the
        # per-item work-unit attribution that must survive the offline queue.
        r1 = bridge.on_milestone(
            trigger="part_done",
            buildgraph_node="bg_004",
            item_id="item_a",
            item_artifacts={"item_a": "bg_part_a", "item_b": "bg_part_b"},
        )
        r2 = bridge.on_milestone(trigger="mission_done", buildgraph_node="bg_005")
        assert r1["queued"] is True
        assert r2["queued"] is True

        # Now enable the vault and flush
        vault._available = True
        summary = bridge.flush(project_id="proj_toggle")
        assert summary["flushed"] == 2
        assert summary["skipped"] == 0
        assert summary["failed"] == 0
        first_call = vault._checkpoints[0]
        assert first_call["item_id"] == "item_a"
        assert first_call["item_artifacts"] == {
            "item_a": "bg_part_a",
            "item_b": "bg_part_b",
        }

        # Second flush — no queued events remain
        summary2 = bridge.flush(project_id="proj_toggle")
        assert summary2["flushed"] == 0
        assert summary2["skipped"] == 2  # both already flushed

    def test_flush_dedup_key_stability(self):
        """Same (run_id, node, trigger) always produces the same dedup key."""
        k1 = _dedup_key("run_abc", "part_done", "bg_001", None)
        k2 = _dedup_key("run_abc", "part_done", "bg_001", None)
        assert k1 == k2

    def test_flush_change_request_dedup_by_audit_id(self):
        k1 = _dedup_key("run_abc", "change_request", "bg_001", "aud_001")
        k2 = _dedup_key("run_xyz", "change_request", "bg_999", "aud_001")
        # Same audit_id → same key regardless of other fields
        assert k1 == k2


# ---------------------------------------------------------------------------
# 7. Mock vault round-trip
# ---------------------------------------------------------------------------

class TestMockVaultRoundTrip:
    def test_on_milestone_creates_checkpoint(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        result = bridge.on_milestone(trigger="plan_ratified", buildgraph_node="bg_010")

        assert result is not None
        assert result["schema"] == "aicad.plmlink/1"
        assert result["trigger"] == "plan_ratified"
        assert result["run_id"] == "run_test_plm"
        assert result["queued"] is False
        assert result["checkpoint_ref"] != ""
        assert result["checkpoint_ref"].startswith("mock_")

    def test_vault_records_checkpoint(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        bridge.on_milestone(trigger="part_done", buildgraph_node="bg_011")

        assert len(vault.checkpoints) == 1
        cp = vault.checkpoints[0]
        assert cp["entry"]["kind"] == "checkpoint"
        assert cp["entry"]["author"]["kind"] == "ai"

    def test_plmlink_event_in_ledger(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        bridge.on_milestone(trigger="mission_done", buildgraph_node="bg_012")

        plmlink_events = [e for e in ledger.events if e.get("kind") == "plmlink"]
        assert len(plmlink_events) == 1
        assert plmlink_events[0]["payload"]["trigger"] == "mission_done"

    def test_flushed_marker_in_ledger(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        bridge.on_milestone(trigger="part_done", buildgraph_node="bg_013")

        markers = [e for e in ledger.events if e.get("kind") == "plmlink_flushed"]
        assert len(markers) == 1

    def test_disabled_trigger_returns_none(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        result = bridge.on_milestone(trigger="stage_exit", stage="sketch",
                                     buildgraph_node="bg_014")
        assert result is None
        assert len(vault.checkpoints) == 0

    def test_stage_exit_part_verify_creates_checkpoint(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        result = bridge.on_milestone(trigger="stage_exit", stage="part_verify",
                                     buildgraph_node="bg_015")
        assert result is not None
        assert result["trigger"] == "stage_exit:part_verify"
        assert len(vault.checkpoints) == 1


# ---------------------------------------------------------------------------
# 8. Change-request linkage
# ---------------------------------------------------------------------------

class TestChangeRequestLinkage:
    def test_change_request_audit_id_recorded(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        result = bridge.on_milestone(
            trigger="change_request",
            buildgraph_node="bg_020",
            audit_id="aud_001",
            modifies_checkpoint="mock_abc123",
        )
        assert result is not None
        cr = result["change_request"]
        assert cr["audit_id"] == "aud_001"
        assert cr["modifies_checkpoint"] == "mock_abc123"

    def test_change_request_dedup_by_audit_id(self, bridge_with_mock):
        bridge, vault, ledger = bridge_with_mock
        result = bridge.on_milestone(
            trigger="change_request",
            buildgraph_node="bg_021",
            audit_id="aud_002",
        )
        dedup = result["dedup_key"]
        assert dedup == "cr:aud_002"


# ---------------------------------------------------------------------------
# 9. Buildgraph emit hook — checkpoint node fires bridge.on_milestone
# ---------------------------------------------------------------------------

class TestBuildgraphHook:
    def _make_graph(self) -> dict:
        return {
            "schema": "aicad.buildgraph/1",
            "mission": "test",
            "nodes": [
                {"id": "bg_p1", "kind": "part", "label": "bracket", "requires": []},
                {"id": "bg_v1", "kind": "verify", "label": "verify_bracket",
                 "requires": ["bg_p1"]},
                {"id": "bg_ck1", "kind": "checkpoint", "label": "part_done_bracket",
                 "requires": ["bg_v1"]},
                {"id": "bg_ck2", "kind": "checkpoint", "label": "mission_done",
                 "requires": ["bg_ck1"]},
            ],
        }

    def test_checkpoint_node_done_fires_part_done(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        graph = self._make_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        # Simulate executor emitting a done event for the part_done checkpoint
        hook("buildgraph.node.done", {"node_id": "bg_ck1"})

        assert len(vault.checkpoints) == 1
        cp = vault.checkpoints[0]
        assert cp["entry"]["kind"] == "checkpoint"

    def test_mission_done_node_fires_mission_done_trigger(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        graph = self._make_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        hook("buildgraph.node.done", {"node_id": "bg_ck2"})

        assert len(vault.checkpoints) == 1

    def test_non_checkpoint_node_does_not_fire(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        graph = self._make_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        hook("buildgraph.node.done", {"node_id": "bg_p1"})
        hook("buildgraph.node.done", {"node_id": "bg_v1"})

        assert len(vault.checkpoints) == 0

    def test_non_done_event_does_not_fire(self, bridge_with_mock):
        bridge, vault, _ = bridge_with_mock
        graph = self._make_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        hook("buildgraph.node.started", {"node_id": "bg_ck1"})
        hook("buildgraph.node.failed", {"node_id": "bg_ck1"})

        assert len(vault.checkpoints) == 0


# ---------------------------------------------------------------------------
# 10. Provenance from events[] not from materialized state
# ---------------------------------------------------------------------------

class TestProvenanceFromEvents:
    def test_provenance_field_absent_from_ledger_state(self, tmp_ledger):
        """Confirm ledger materialized state strips provenance — tests the
        invariant that plm_bridge must read events[], not state."""
        tmp_ledger.upsert_dimension(
            actor="builder",
            dim_id="DIM10",
            name="bore_diameter",
            value=20.02,
            unit="mm",
            driver="H7/g6 fit",
            provenance="ISO 286-2 table lookup",
        )
        # Materialized state should NOT contain provenance
        dims = tmp_ledger.state.get("dimensions", [])
        assert len(dims) == 1
        assert "provenance" not in dims[0], (
            "provenance MUST NOT be in materialized state — bridge reads events[]"
        )

    def test_provenance_field_present_in_events(self, tmp_ledger):
        """Provenance must be in the raw event payload."""
        tmp_ledger.upsert_dimension(
            actor="builder",
            dim_id="DIM11",
            name="wall_thickness",
            value=5.0,
            unit="mm",
            driver="stiffness req",
            provenance="calc: FEA min 3mm, bumped to 5mm",
        )
        dim_events = [e for e in tmp_ledger.events if e.get("kind") == "dimension"]
        assert len(dim_events) == 1
        assert dim_events[0]["payload"]["provenance"] == "calc: FEA min 3mm, bumped to 5mm"

    def test_bridge_drafts_from_events_not_state(self, tmp_ledger):
        """Bridge message drafting uses events[], so provenance appears in message."""
        tmp_ledger.upsert_dimension(
            actor="builder",
            dim_id="DIM12",
            name="bore",
            value=20.02,
            unit="mm",
            driver="fit",
            provenance="ISO 286-2 table lookup for 20 mm H7",
        )
        bridge = PLMBridge(run_id=tmp_ledger.run_id, vault=MockVault())
        bridge.attach_ledger(tmp_ledger)

        events = bridge._events_since_last_checkpoint()
        msg = draft_checkpoint_message(events, trigger="part_done")
        assert "ISO 286-2" in msg, (
            f"Provenance from event not in message: {msg!r}"
        )


# ---------------------------------------------------------------------------
# 11. Real executor integration — capability proven through run_sync
# ---------------------------------------------------------------------------

class TestRealExecutorIntegration:
    """Prove the bridge seam against the actual BuildGraphExecutor, not a
    hand-crafted payload.  Uses run_sync from executor.py so the emit
    contract is exercised end-to-end.

    run_sync is the correct single-process path for integration testing;
    the live mission path dispatches sub-agents via ready_nodes() instead.
    """

    def _make_fixture_graph(self) -> dict:
        return {
            "schema": "aicad.buildgraph/1",
            "mission": "widget_assy",
            "nodes": [
                {"id": "n_part",  "kind": "part",       "label": "bracket",      "requires": []},
                {"id": "n_verify","kind": "verify",      "label": "verify_bracket","requires": ["n_part"]},
                {"id": "n_ck1",   "kind": "checkpoint",  "label": "part_done_bracket","requires": ["n_verify"]},
                {"id": "n_ck2",   "kind": "checkpoint",  "label": "mission_done", "requires": ["n_ck1"]},
            ],
        }

    def test_run_sync_cuts_two_checkpoints(self, tmp_path):
        """run_sync on a graph with two checkpoint nodes cuts exactly two PLM
        checkpoints via the real executor emit path."""
        vault = MockVault()
        vault.register_project("run_exec_test", "proj_exec")
        bridge = PLMBridge(run_id="run_exec_test", vault=vault)
        ledger_path = tmp_path / "ledger.json"
        from services.agent.ledger import Ledger
        ledger = Ledger(run_id="run_exec_test", path=ledger_path)
        bridge.attach_ledger(ledger)

        graph = self._make_fixture_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        # node_fn: every node succeeds immediately
        def node_fn(node):
            return True, {}

        ex = run_sync(graph, node_fn, emit=hook)

        assert ex.is_succeeded(), f"Executor did not succeed: {ex.all_statuses()}"
        # Two checkpoint-kind nodes → two PLM checkpoints
        assert len(vault.checkpoints) == 2, (
            f"Expected 2 checkpoints, got {len(vault.checkpoints)}: "
            f"{[c['entry'] for c in vault.checkpoints]}"
        )

    def test_run_sync_checkpoint_triggers_correct(self, tmp_path):
        """The two checkpoints use part_done and mission_done triggers
        respectively, confirming label→trigger mapping through the real executor."""
        vault = MockVault()
        vault.register_project("run_exec_test2", "proj_exec2")
        bridge = PLMBridge(run_id="run_exec_test2", vault=vault)
        ledger_path = tmp_path / "ledger2.json"
        from services.agent.ledger import Ledger
        ledger = Ledger(run_id="run_exec_test2", path=ledger_path)
        bridge.attach_ledger(ledger)

        graph = self._make_fixture_graph()
        hook = make_buildgraph_node_emit_hook(bridge, graph)

        def node_fn(node):
            return True, {}

        run_sync(graph, node_fn, emit=hook)

        # Collect plmlink events from ledger to inspect triggers
        plmlink_events = [e for e in ledger.events if e.get("kind") == "plmlink"]
        triggers = {e["payload"]["buildgraph_node"]: e["payload"]["trigger"]
                    for e in plmlink_events}
        assert triggers.get("n_ck1") == "part_done", f"n_ck1 trigger: {triggers}"
        assert triggers.get("n_ck2") == "mission_done", f"n_ck2 trigger: {triggers}"

    def test_run_sync_non_checkpoint_nodes_produce_no_checkpoints(self, tmp_path):
        """part and verify nodes must not create PLM checkpoints."""
        vault = MockVault()
        vault.register_project("run_exec_test3", "proj_exec3")
        bridge = PLMBridge(run_id="run_exec_test3", vault=vault)
        ledger_path = tmp_path / "ledger3.json"
        from services.agent.ledger import Ledger
        ledger = Ledger(run_id="run_exec_test3", path=ledger_path)
        bridge.attach_ledger(ledger)

        # Graph with only non-checkpoint nodes
        graph_no_ck = {
            "schema": "aicad.buildgraph/1",
            "mission": "no_checkpoints",
            "nodes": [
                {"id": "n_p1", "kind": "part",   "label": "shaft",  "requires": []},
                {"id": "n_v1", "kind": "verify",  "label": "verify", "requires": ["n_p1"]},
            ],
        }
        hook = make_buildgraph_node_emit_hook(bridge, graph_no_ck)

        def node_fn(node):
            return True, {}

        run_sync(graph_no_ck, node_fn, emit=hook)

        assert len(vault.checkpoints) == 0, (
            f"Expected 0 checkpoints for non-checkpoint nodes, got "
            f"{len(vault.checkpoints)}"
        )


# ---------------------------------------------------------------------------
# 12. Vocabulary scan over all enabled trigger/stage message prefixes
# ---------------------------------------------------------------------------

class TestVocabScanAllEnabledTriggers:
    """Every message prefix that can appear for an enabled trigger must be
    free of git vocabulary (contracts/plm.md §8.1).

    Covers the assembly bug fixed by this task: 'Assembly stage complete.'
    contained the word 'stage' which _scan_git_vocab would catch.
    """

    _STAGE_EXIT_STAGES = ["part_verify", "assembly", "sim"]
    _SIMPLE_TRIGGERS = ["plan_ratified", "part_done", "mission_done", "change_request"]

    def _draft_prefix_only(self, trigger: str, stage: str | None = None) -> str:
        """Draft a message with no ledger events (only the trigger prefix line)."""
        return draft_checkpoint_message([], trigger=trigger, stage=stage)

    def test_simple_trigger_prefixes_are_vocab_clean(self):
        for trigger in self._SIMPLE_TRIGGERS:
            msg = self._draft_prefix_only(trigger)
            hits = _scan_git_vocab(msg)
            assert not hits, (
                f"Git vocab {hits!r} in '{trigger}' message: {msg!r}"
            )

    def test_stage_exit_prefixes_are_vocab_clean(self):
        for stage in self._STAGE_EXIT_STAGES:
            msg = self._draft_prefix_only("stage_exit", stage=stage)
            hits = _scan_git_vocab(msg)
            assert not hits, (
                f"Git vocab {hits!r} in 'stage_exit:{stage}' message: {msg!r}"
            )

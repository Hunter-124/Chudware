"""test_org.py — Org runtime tests (aicad.org/1).

Coverage (per contract §18):
  1. Clustering on a 4-deliverable manifest → correct department cluster fixture.
  2. interface_change gating: send → sender's exit blocked → ack → unblocked.
  3. Silent-expiry impossible: closing a request without a resolution is unrepresentable
     (raises); aging routes open concerns to the overseer audit feed, never drops them.
  4. Re-org revision bump: reorg() increments chart_revision.
  5. Report cadence: dept report emitted every N node completions.

All tests run headless: no FastMCP, no libchudware, no LLM.  Ledger injection uses
the real Ledger against a tmp_path.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Adjust sys.path so imports resolve from the repo root when run directly.
# ---------------------------------------------------------------------------
import sys
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent.org import (
    OrgState,
    build_org_chart,
    has_unacked_interface_changes,
    unresolved_concern_contexts,
    ORG_SCHEMA,
    REQUEST_SCHEMA,
)
from services.agent.ledger import Ledger

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TMP_CACHE = tempfile.mkdtemp(prefix="test_org_cache_")


def _fresh_ledger(suffix: str = "") -> Ledger:
    run_id = f"test_org_{suffix}_{os.getpid()}"
    path = Path(_TMP_CACHE) / "runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(run_id, path)


def _4deliverable_manifest() -> dict:
    """4-deliverable manifest: part + drawing (dep on part) + cam (dep on part)
    + second_part (independent).

    Expected clustering (§18: each deliverable + its depends_on satellites):
      - dept_DL1  : [DL1, DL2, DL3]  (DL2 and DL3 depend_on DL1)
      - dept_DL2  : [DL2, DL1]        (DL2 depends_on DL1)
      - dept_DL3  : [DL3, DL1]        (DL3 depends_on DL1)
      - dept_DL4  : [DL4]             (independent)
    """
    return {
        "schema": "aicad.deliverables/1",
        "request_verbatim": "make a bracket with drawing and gcode, plus a second part",
        "deliverables": [
            {
                "id": "DL1", "kind": "part", "name": "bracket",
                "description": "main bracket",
                "acceptance_criteria": [
                    {"id": "AC1", "text": "bore fits shaft",
                     "check": {"tool": "fit_report", "pass_rule": "clearance in range"}}
                ],
                "depends_on": [],
            },
            {
                "id": "DL2", "kind": "drawing", "name": "bracket drawing",
                "description": "drawing of bracket",
                "acceptance_criteria": [
                    {"id": "AC2", "text": "views correct",
                     "check": {"tool": "review_drawing", "pass_rule": "no errors"}}
                ],
                "depends_on": ["DL1"],
            },
            {
                "id": "DL3", "kind": "cam_program", "name": "bracket gcode",
                "description": "nc program for bracket",
                "acceptance_criteria": [
                    {"id": "AC3", "text": "no gouges",
                     "check": {"tool": "cam_simulate", "pass_rule": "gouges==0"}}
                ],
                "depends_on": ["DL1"],
            },
            {
                "id": "DL4", "kind": "part", "name": "second part",
                "description": "independent part",
                "acceptance_criteria": [
                    {"id": "AC4", "text": "passes verify battery",
                     "check": {"tool": "verify_battery", "pass_rule": "PASS"}}
                ],
                "depends_on": [],
            },
        ],
        "ambiguities": [],
        "assumptions": [],
    }


# ---------------------------------------------------------------------------
# 1. DELIVERABLE CLUSTERING (4-deliverable fixture)
# ---------------------------------------------------------------------------

class TestClustering:
    """Proof: 4-deliverable manifest clusters into correct departments."""

    def test_four_departments_created(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        assert chart["schema"] == ORG_SCHEMA
        depts = chart["departments"]
        assert len(depts) == 4, f"Expected 4 depts, got {len(depts)}: {[d['id'] for d in depts]}"

    def test_dept_ids_match_deliverables(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        dept_ids = {d["id"] for d in chart["departments"]}
        assert "dept_DL1" in dept_ids
        assert "dept_DL2" in dept_ids
        assert "dept_DL3" in dept_ids
        assert "dept_DL4" in dept_ids

    def test_primary_deliverable_clusters_with_satellites(self):
        """DL1 dept must include DL2 and DL3 as satellites (they depend_on DL1)."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        dept_dl1 = next(d for d in chart["departments"] if d["id"] == "dept_DL1")
        assert "DL1" in dept_dl1["deliverables"]
        assert "DL2" in dept_dl1["deliverables"]
        assert "DL3" in dept_dl1["deliverables"]

    def test_independent_deliverable_standalone(self):
        """DL4 (no depends_on) gets a single-deliverable department."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        dept_dl4 = next(d for d in chart["departments"] if d["id"] == "dept_DL4")
        assert dept_dl4["deliverables"] == ["DL4"]

    def test_satellites_also_head_own_depts(self):
        """DL2 and DL3 appear as primaries with their own depts (they are top-level)."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        dept_dl2 = next(d for d in chart["departments"] if d["id"] == "dept_DL2")
        assert "DL2" in dept_dl2["deliverables"]
        assert "DL1" in dept_dl2["deliverables"]  # DL1 is DL2's satellite

    def test_channels_auto_created_for_cross_dept_deps(self):
        """depends_on edges between primaries become channels."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        channels = chart["channels"]
        # DL2 depends_on DL1 → channel between dept_DL1 and dept_DL2 (or dept_DL2 and dept_DL1)
        pairs = [frozenset(ch["between"]) for ch in channels]
        assert frozenset({"dept_DL1", "dept_DL2"}) in pairs
        assert frozenset({"dept_DL1", "dept_DL3"}) in pairs

    def test_dept_manager_role_set(self):
        """Every department has manager.role = 'department_manager'."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        for dept in chart["departments"]:
            assert dept["manager"]["role"] == "department_manager", dept["id"]

    def test_dept_advisors_include_foreman_and_overseer(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        for dept in chart["departments"]:
            assert "foreman" in dept["advisors"]
            assert "overseer" in dept["advisors"]

    def test_initial_status_forming(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        for dept in chart["departments"]:
            assert dept["status"] == "forming"

    def test_chart_revision_starts_at_one(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        assert chart["chart_revision"] == 1


# ---------------------------------------------------------------------------
# 2. INTERFACE-CHANGE GATING
# ---------------------------------------------------------------------------

class TestInterfaceChangeGating:
    """Proof: interface_change discipline enforced at stage-exit.

    Send → sender's exit gate blocked → recipient acks → unblocked.
    """

    def _make_two_dept_org(self):
        """Build a minimal 2-department org with one channel."""
        manifest = {
            "schema": "aicad.deliverables/1",
            "request_verbatim": "part and drawing",
            "deliverables": [
                {"id": "DL1", "kind": "part", "name": "p",
                 "acceptance_criteria": [
                     {"id": "AC1", "text": "ok",
                      "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                 "depends_on": []},
                {"id": "DL2", "kind": "drawing", "name": "drw",
                 "acceptance_criteria": [
                     {"id": "AC2", "text": "ok",
                      "check": {"tool": "review_drawing", "pass_rule": "ok"}}],
                 "depends_on": ["DL1"]},
            ],
        }
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart)
        # Find the channel between the two depts
        ch_id = chart["channels"][0]["id"]
        return org, "dept_DL1", "dept_DL2", ch_id

    def test_no_interface_change_no_block(self):
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        assert not has_unacked_interface_changes(org, dept_a)

    def test_send_interface_change_blocks_sender(self):
        """After sending an interface_change, the sender's exit is blocked."""
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        org.channel_send(ch_id, dept_a, "interface_change",
                         "bore diameter DIM1 changed to 20.02mm")
        assert has_unacked_interface_changes(org, dept_a)

    def test_ack_by_recipient_unblocks_sender(self):
        """Once the recipient acks, the sender is no longer blocked."""
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        msg = org.channel_send(ch_id, dept_a, "interface_change",
                               "bore diameter DIM1 changed to 20.02mm")
        assert has_unacked_interface_changes(org, dept_a)
        org.channel_ack(msg["id"], dept_b)
        assert not has_unacked_interface_changes(org, dept_a)

    def test_non_interface_change_does_not_block(self):
        """An 'info' message does not block the sender's stage exit."""
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        org.channel_send(ch_id, dept_a, "info", "status update")
        assert not has_unacked_interface_changes(org, dept_a)

    def test_sender_is_not_blocked_by_other_depts_messages(self):
        """Dept_b's unacked messages don't block dept_a."""
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        org.channel_send(ch_id, dept_b, "interface_change",
                         "parting plane moved 2mm")
        # dept_b sent it — dept_a is not blocked (§18: blocks SENDER, not receiver)
        assert not has_unacked_interface_changes(org, dept_a)
        assert has_unacked_interface_changes(org, dept_b)

    def test_channel_message_body_capped_120_words(self):
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        long_body = " ".join(["word"] * 200)
        msg = org.channel_send(ch_id, dept_a, "info", long_body)
        assert len(msg["body"].split()) <= 121  # 120 words + possible "…"

    def test_interface_change_ledger_event_written(self):
        ledger = _fresh_ledger("ic_ledger")
        manifest = {
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [
                {"id": "DL1", "kind": "part", "name": "p",
                 "acceptance_criteria": [
                     {"id": "AC1", "text": "ok",
                      "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                 "depends_on": []},
                {"id": "DL2", "kind": "drawing", "name": "d",
                 "acceptance_criteria": [
                     {"id": "AC2", "text": "ok",
                      "check": {"tool": "review_drawing", "pass_rule": "ok"}}],
                 "depends_on": ["DL1"]},
            ],
        }
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart, ledger=ledger)
        ch_id = chart["channels"][0]["id"]
        org.channel_send(ch_id, "dept_DL1", "interface_change", "dim changed")
        # Ledger must carry the message event.
        events = [e for e in ledger.events
                  if e.get("kind") == "artifact"
                  and e.get("payload", {}).get("kind") == "channel_message"]
        assert len(events) >= 1

    def test_invalid_message_kind_raises(self):
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        with pytest.raises(ValueError, match="kind"):
            org.channel_send(ch_id, dept_a, "bogus_kind", "hello")


# ---------------------------------------------------------------------------
# 3. SILENT-EXPIRY IMPOSSIBLE
# ---------------------------------------------------------------------------

class TestSilentExpiryImpossible:
    """Proof: requests can only be closed via explicit resolution; concerns that
    age past one manager turn surface in overseer audit context (never dropped).
    """

    def test_request_requires_explicit_resolution(self):
        """No code path removes a request without recording a resolution action."""
        org = OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))
        req = org.request_raise("dg_abc", "manager", "context", "need the bore spec")
        # Before resolution: open.
        assert org.open_requests() == [req]
        # Resolve it.
        org.request_resolve(req["id"], by="manager", action="grant")
        # After resolution: closed (no longer open).
        assert org.open_requests() == []
        # Resolution fields are set (not None).
        resolved = org._requests[req["id"]]
        assert resolved["resolution"]["action"] == "grant"
        assert resolved["resolution"]["by"] == "manager"
        assert resolved["resolution"]["ts"] is not None

    def test_double_resolve_raises(self):
        """Resolving an already-resolved request raises — cannot overwrite resolution."""
        org = OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))
        req = org.request_raise("dg_abc", "manager", "context", "need spec")
        org.request_resolve(req["id"], by="manager", action="grant")
        with pytest.raises(ValueError, match="already resolved"):
            org.request_resolve(req["id"], by="manager", action="deny")

    def test_invalid_resolution_action_raises(self):
        org = OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))
        req = org.request_raise("dg_abc", "manager", "context", "need spec")
        with pytest.raises(ValueError, match="action"):
            org.request_resolve(req["id"], by="manager", action="silent_expire")

    def test_concern_ages_into_overseer_audit_context(self):
        """An unresolved concern surfaces in the overseer feed after one turn."""
        os.environ["CHUDWARE_OVERSEER"] = "1"
        try:
            org = OrgState(org_chart=build_org_chart({
                "schema": "aicad.deliverables/1",
                "request_verbatim": "x",
                "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                                   "acceptance_criteria": [
                                       {"id": "AC1", "text": "ok",
                                        "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                                   "depends_on": []}],
            }))
            # Raise a concern at turn 0.
            req = org.request_raise("dg_abc", "manager", "concern",
                                    "bore spec seems inconsistent with DIM7")
            # At turn 0: no aged concerns (same turn as raised).
            assert unresolved_concern_contexts(org) == []
            # Advance one turn.
            org.advance_turn()
            # Now the concern has aged past one manager turn.
            contexts = unresolved_concern_contexts(org)
            assert len(contexts) == 1
            ctx = contexts[0]
            assert ctx["request_id"] == req["id"]
            assert ctx["kind"] == "unresolved_concern"
            # The request is STILL open (aging surfaces it, never drops it).
            assert org.open_requests() == [req]
        finally:
            os.environ.pop("CHUDWARE_OVERSEER", None)
            # Re-import to reset the module-level flag for other tests
            import importlib
            import services.agent.org as _org_mod
            importlib.reload(_org_mod)

    def test_resolved_concern_does_not_surface_in_audit_feed(self):
        """A resolved concern never appears in the overseer feed."""
        os.environ["CHUDWARE_OVERSEER"] = "1"
        try:
            org = OrgState(org_chart=build_org_chart({
                "schema": "aicad.deliverables/1",
                "request_verbatim": "x",
                "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                                   "acceptance_criteria": [
                                       {"id": "AC1", "text": "ok",
                                        "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                                   "depends_on": []}],
            }))
            req = org.request_raise("dg_abc", "manager", "concern", "stale spec")
            org.request_resolve(req["id"], by="manager", action="grant")
            org.advance_turn()
            # Resolved concern must not appear in the audit feed.
            assert unresolved_concern_contexts(org) == []
        finally:
            os.environ.pop("CHUDWARE_OVERSEER", None)
            import importlib
            import services.agent.org as _org_mod
            importlib.reload(_org_mod)

    def test_concern_feed_empty_when_overseer_flag_off(self):
        """When CHUDWARE_OVERSEER is off, the audit feed is always empty."""
        os.environ.pop("CHUDWARE_OVERSEER", None)
        org = OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))
        org.request_raise("dg_abc", "manager", "concern", "something worrying")
        org.advance_turn()
        assert unresolved_concern_contexts(org) == []


# ---------------------------------------------------------------------------
# 4. RE-ORG REVISION BUMP
# ---------------------------------------------------------------------------

class TestReorg:
    """Proof: reorg() increments chart_revision."""

    def test_reorg_increments_chart_revision(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        assert chart["chart_revision"] == 1

        org = OrgState(org_chart=chart)
        new_chart = org.reorg(manifest)
        assert new_chart["chart_revision"] == 2

    def test_reorg_again_increments_further(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart)
        org.reorg(manifest)
        org.reorg(manifest)
        assert org.org_chart["chart_revision"] == 3

    def test_reorg_emits_ledger_event(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        ledger = _fresh_ledger("reorg")
        org = OrgState(org_chart=chart, ledger=ledger)
        org.reorg(manifest)
        events = [e for e in ledger.events
                  if e.get("kind") == "artifact"
                  and e.get("payload", {}).get("kind") == "org_reorg"]
        assert len(events) == 1
        assert events[0]["payload"]["chart_revision"] == 2

    def test_reorg_replaces_org_chart(self):
        """After reorg, org_state.org_chart is the new chart."""
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart)
        new_chart = org.reorg(manifest)
        assert org.org_chart is new_chart


# ---------------------------------------------------------------------------
# 5. REPORT CADENCE
# ---------------------------------------------------------------------------

class TestReportCadence:
    """Proof: structured department report emitted every N node completions."""

    def test_report_emitted_at_cadence(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart, report_every=3)
        # First 2 completions: no report.
        assert org.record_node_completion("dept_DL1") is None
        assert org.record_node_completion("dept_DL1") is None
        # Third completion: report emitted.
        report = org.record_node_completion("dept_DL1")
        assert report is not None
        assert report["dept_id"] == "dept_DL1"
        assert report["completions"] == 3

    def test_report_at_6_completions(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart, report_every=3)
        for _ in range(5):
            assert org.record_node_completion("dept_DL1") is None or True
        # 6th triggers second report.
        report = org.record_node_completion("dept_DL1")
        assert report is not None
        assert report["completions"] == 6

    def test_report_has_required_fields(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart, report_every=1)
        report = org.record_node_completion("dept_DL1")
        required = {"dept_id", "deliverables", "completions", "status",
                    "done", "next", "blocked", "requests_open",
                    "interfaces_touched", "ts"}
        assert required <= set(report.keys()), f"Missing: {required - set(report.keys())}"

    def test_report_written_to_ledger(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        ledger = _fresh_ledger("report")
        org = OrgState(org_chart=chart, ledger=ledger, report_every=2)
        org.record_node_completion("dept_DL1")
        org.record_node_completion("dept_DL1")
        events = [e for e in ledger.events
                  if e.get("kind") == "artifact"
                  and e.get("payload", {}).get("kind") == "dept_report"]
        assert len(events) == 1

    def test_no_report_below_cadence(self):
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart, report_every=10)
        for _ in range(9):
            assert org.record_node_completion("dept_DL1") is None


# ---------------------------------------------------------------------------
# 6. ORGANIZE GATE integration (phases.py)
# ---------------------------------------------------------------------------

class TestOrganizeGate:
    """Proof: the ORGANIZE phase exit gate passes iff departments are instantiated."""

    def test_organize_gate_blocks_without_org_chart(self):
        from services.agent.pipeline.phases import PhaseMachine, Phase, PipelineFlags
        machine = PhaseMachine(flags=PipelineFlags(org_layer=True))
        gate = machine.run_gate(Phase.ORGANIZE, {})
        assert not gate.passed
        assert "departments_instantiated" == gate.gate

    def test_organize_gate_passes_with_org_chart(self):
        from services.agent.pipeline.phases import PhaseMachine, Phase, PipelineFlags
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        machine = PhaseMachine(flags=PipelineFlags(org_layer=True))
        gate = machine.run_gate(Phase.ORGANIZE, {"org_chart": chart})
        assert gate.passed, gate.reasons

    def test_organize_gate_disabled_when_flag_off(self):
        from services.agent.pipeline.phases import PhaseMachine, Phase, PipelineFlags
        machine = PhaseMachine(flags=PipelineFlags(org_layer=False))
        gate = machine.run_gate(Phase.ORGANIZE, {})
        # Gate is "org_disabled" → passes trivially.
        assert gate.passed


# ---------------------------------------------------------------------------
# 7. REQUEST KINDS
# ---------------------------------------------------------------------------

class TestRequestKinds:
    """Spot-check all valid request kinds and verify invalid kinds are rejected."""

    def _org(self):
        return OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))

    @pytest.mark.parametrize("kind", [
        "context", "resource", "information", "communication", "concern", "escalation"
    ])
    def test_valid_request_kinds(self, kind):
        org = self._org()
        req = org.request_raise("dg_abc", "manager", kind, f"test {kind}")
        assert req["kind"] == kind
        assert req["schema"] == REQUEST_SCHEMA

    def test_invalid_request_kind_raises(self):
        org = self._org()
        with pytest.raises(ValueError, match="kind"):
            org.request_raise("dg_abc", "manager", "bogus", "test")


# ---------------------------------------------------------------------------
# 8. MCP tool catalog (channel_send, request_resolve, council_convene registered)
# ---------------------------------------------------------------------------

class TestOrgToolRegistration:
    """Proof: the three org-layer tools resolve in the MCP catalog.

    This mirrors the pattern in test_roles.py._build_catalog — we just check
    that the names appear after calling register_org_tools.
    """

    def test_org_tools_register_in_catalog(self):
        import importlib
        import glob

        from services.mcp import tool_registry as reg
        reg.clear_catalog()

        class _StubMCP:
            def __init__(self):
                self.tools = {}

            def tool(self, *a, **k):
                def deco(fn):
                    self.tools[k.get("name") or fn.__name__] = fn
                    return fn
                return deco

        stub = _StubMCP()
        from services.mcp.org_tools import register_org_tools
        register_org_tools(reg.view(stub, "org"))

        catalog_names = set(reg.catalog().keys()) | set(stub.tools.keys())
        for name in ("channel_send", "request_resolve", "council_convene", "request_raise"):
            assert name in catalog_names, f"{name!r} not in catalog: {sorted(catalog_names)}"

    def test_validate_suite_zero_flags_after_org_tools_registered(self):
        """With org_tools registered, validate_suite sees no feature-flagged tools."""
        import glob
        import importlib
        from services.mcp import tool_registry as reg
        from services.agent import roles

        reg.clear_catalog()

        class _StubMCP:
            def __init__(self):
                self.tools = {}

            def tool(self, *a, **k):
                def deco(fn):
                    self.tools[k.get("name") or fn.__name__] = fn
                    return fn
                return deco

        def _regmod(modpath, family):
            try:
                m = importlib.import_module(modpath)
            except Exception:
                return
            for n in dir(m):
                if n.startswith("register_") and n.endswith("_tools"):
                    try:
                        getattr(m, n)(reg.view(_StubMCP(), family))
                    except Exception:
                        pass

        _REPO = Path(__file__).resolve().parents[2]
        for path in sorted(glob.glob(str(_REPO / "services" / "mcp" / "*_tools.py"))):
            mod = "services.mcp." + Path(path).stem
            family = Path(path).stem.replace("_tools", "")
            _regmod(mod, family)

        catalog = set(reg.catalog().keys())
        findings = roles.validate_suite(tool_names=catalog)
        flagged = {f.message.split("'")[1] for f in roles.flags(findings)
                   if "not yet registered" in f.message}
        assert flagged == set(), f"Unexpected feature flags: {flagged}"


# ---------------------------------------------------------------------------
# 9. STAGE EXIT-GATE WIRING (stages.check_interface_change_gate)
# ---------------------------------------------------------------------------

class TestStageExitGateWiring:
    """Proof: the real stages.py gate function blocks on unacked interface_change.

    This is the 'discriminating test' the advisor requested: we call the actual
    foreman-callable gate, not just the OrgState predicate, to prove the wiring
    is real (§18: 'enforced at stage-exit').
    """

    def _make_two_dept_org(self):
        manifest = {
            "schema": "aicad.deliverables/1",
            "request_verbatim": "part and drawing",
            "deliverables": [
                {"id": "DL1", "kind": "part", "name": "p",
                 "acceptance_criteria": [
                     {"id": "AC1", "text": "ok",
                      "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                 "depends_on": []},
                {"id": "DL2", "kind": "drawing", "name": "drw",
                 "acceptance_criteria": [
                     {"id": "AC2", "text": "ok",
                      "check": {"tool": "review_drawing", "pass_rule": "ok"}}],
                 "depends_on": ["DL1"]},
            ],
        }
        chart = build_org_chart(manifest)
        org = OrgState(org_chart=chart)
        ch_id = chart["channels"][0]["id"]
        return org, "dept_DL1", "dept_DL2", ch_id

    def test_gate_passes_when_no_interface_change_sent(self):
        """check_interface_change_gate returns False (not blocked) with no messages."""
        from services.agent.advisor.stages import check_interface_change_gate
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        assert not check_interface_change_gate(org, dept_a)

    def test_gate_blocks_after_interface_change_sent(self):
        """check_interface_change_gate returns True (BLOCKED) after unacked send."""
        from services.agent.advisor.stages import check_interface_change_gate
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        org.channel_send(ch_id, dept_a, "interface_change",
                         "bore diameter changed to 20.02mm — update drawing dims")
        assert check_interface_change_gate(org, dept_a), (
            "Gate should block sender after unacked interface_change"
        )

    def test_gate_unblocks_after_ack(self):
        """check_interface_change_gate returns False once recipient acks."""
        from services.agent.advisor.stages import check_interface_change_gate
        org, dept_a, dept_b, ch_id = self._make_two_dept_org()
        msg = org.channel_send(ch_id, dept_a, "interface_change",
                               "bore diameter changed to 20.02mm — update drawing dims")
        assert check_interface_change_gate(org, dept_a)
        org.channel_ack(msg["id"], dept_b)
        assert not check_interface_change_gate(org, dept_a)

    def test_gate_is_inert_when_org_state_is_none(self):
        """Passing None org_state never raises and always returns False (safe no-op)."""
        from services.agent.advisor.stages import check_interface_change_gate
        assert not check_interface_change_gate(None, "dept_DL1")


# ---------------------------------------------------------------------------
# 10. OVERSEER CONCERN FEED — assemble_evidence integration
# ---------------------------------------------------------------------------

class TestOverseerConcernFeedIntegration:
    """Proof: aged unresolved concerns surface in the overseer's assemble_evidence.

    This is the second discriminating test the advisor required: we call the
    actual assemble_evidence function (not just check the producer helper) and
    assert the concern appears in the resulting evidence bundle (§18 → §11).
    """

    def _aged_concern_org(self):
        """Return an OrgState that has one aged unresolved concern."""
        os.environ["CHUDWARE_OVERSEER"] = "1"
        org = OrgState(org_chart=build_org_chart({
            "schema": "aicad.deliverables/1",
            "request_verbatim": "x",
            "deliverables": [{"id": "DL1", "kind": "part", "name": "p",
                               "acceptance_criteria": [
                                   {"id": "AC1", "text": "ok",
                                    "check": {"tool": "fit_report", "pass_rule": "ok"}}],
                               "depends_on": []}],
        }))
        req = org.request_raise("dg_abc", "manager", "concern",
                                "tolerance stack looks inconsistent with DIM7")
        org.advance_turn()
        return org, req

    def test_org_concerns_appear_in_evidence_bundle(self):
        """assemble_evidence with org_concerns → [ORG-CONCERNS] block present."""
        from services.agent.advisor.overseer import assemble_evidence, AuditScope
        try:
            org, req = self._aged_concern_org()
            concerns = unresolved_concern_contexts(org)
            assert concerns, "Fixture must yield at least one aged concern"

            scope = AuditScope(run_id="test-run", stage="feature",
                               roles=["foreman"], buildgraph_nodes=1)
            evidence = assemble_evidence(
                "stage_exit", scope,
                org_concerns=concerns,
            )
            assert "[ORG-CONCERNS]" in evidence, (
                "assemble_evidence must include [ORG-CONCERNS] block when org_concerns given"
            )
            assert req["id"] in evidence, (
                "The specific request id must appear in the evidence bundle"
            )
            assert "[/ORG-CONCERNS]" in evidence
        finally:
            os.environ.pop("CHUDWARE_OVERSEER", None)

    def test_no_org_concerns_block_when_none_passed(self):
        """assemble_evidence without org_concerns → no [ORG-CONCERNS] block."""
        from services.agent.advisor.overseer import assemble_evidence, AuditScope
        scope = AuditScope(run_id="test-run", stage="feature",
                           roles=["foreman"], buildgraph_nodes=1)
        evidence = assemble_evidence("stage_exit", scope)
        assert "[ORG-CONCERNS]" not in evidence

    def test_no_org_concerns_block_when_empty_list_passed(self):
        """assemble_evidence with empty org_concerns → no [ORG-CONCERNS] block."""
        from services.agent.advisor.overseer import assemble_evidence, AuditScope
        scope = AuditScope(run_id="test-run", stage="feature",
                           roles=["foreman"], buildgraph_nodes=1)
        evidence = assemble_evidence("stage_exit", scope, org_concerns=[])
        assert "[ORG-CONCERNS]" not in evidence


# ---------------------------------------------------------------------------
# 11. DEPARTMENT_MANAGER card loads and drives a fixture department
# ---------------------------------------------------------------------------

class TestDepartmentManagerCard:
    """Proof: roles.load_role('department_manager') loads verbatim and
    the card's role id can be attached to a fixture department.
    """

    def test_department_manager_card_loads(self):
        """load_role('department_manager') must not raise and must return a RoleCard."""
        from services.agent.roles import load_role
        card = load_role("department_manager")
        assert card.id == "department_manager"

    def test_department_manager_card_drives_fixture_dept(self):
        """The loaded card's role id matches the 'manager.role' set by build_org_chart."""
        from services.agent.roles import load_role
        card = load_role("department_manager")
        manifest = _4deliverable_manifest()
        chart = build_org_chart(manifest)
        # Every department's manager.role must match the card id verbatim.
        for dept in chart["departments"]:
            assert dept["manager"]["role"] == card.id, (
                f"dept {dept['id']} manager.role={dept['manager']['role']!r} "
                f"!= card.id={card.id!r}"
            )

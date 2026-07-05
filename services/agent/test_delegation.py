"""Tests for services/agent/delegation.py + services/agent/routing.py.

Contract: contracts/advisor.md §12 (aicad.delegation/1) + §14 (aicad.routing/1).

Test coverage:
  * Approval matrix: auto (depth-1), overseer (deeper), ponder (any depth).
  * Budget accrual + revocation at exact exhaustion.
  * Routing fallthrough when a source is offline.
  * Resolution ledger events emitted on every resolve.
  * Depth cap enforcement.
  * Single-source degenerate case (all roles collapse onto one model).
  * resolve_assignments() returns the per-role map (T-W17 GUI API).
  * delegate() tool returns correct dict on success + error dict on failure.
  * Sub-agent identities: each node gets a unique dg_ id (no shared mutable default).

Offline discipline: mock providers, no network, no LLM, no all-skip.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from services.agent.delegation import (
    DEPTH_CAP,
    DELEGATION_SCHEMA,
    DelegationError,
    DelegationTree,
    delegate,
)
from services.agent.routing import (
    RoutingError,
    RoutingTable,
    Resolution,
    Source,
    ModelEntry,
    load_routing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_routing(
    sources: list[dict] | None = None,
    assignments: dict | None = None,
    approval_policy: dict | None = None,
) -> RoutingTable:
    """Build a RoutingTable from raw dicts (test helper)."""
    cfg: dict = {
        "schema": "aicad.routing/1",
        "sources": sources or [],
        "assignments": assignments or {
            "overseer": ["strongest"],
            "orchestrator": ["strongest"],
            "sketcher": ["cheap_local", "strong"],
            "default": ["cheap_local", "strong"],
        },
        "approval_policy": approval_policy or {
            "depth_1": "auto",
            "deeper": "overseer",
        },
    }
    return load_routing(cfg)


def _two_source_routing() -> RoutingTable:
    return _make_routing(sources=[
        {"id": "cloud_api", "kind": "anthropic",
         "models": [{"id": "claude-sonnet-4-6", "tier": "frontier"}]},
        {"id": "local_lms", "kind": "lmstudio",
         "models": [{"id": "qwen3-4b", "tier": "cheap_local"}]},
    ])


def _single_source_routing() -> RoutingTable:
    return _make_routing(sources=[
        {"id": "cloud_api", "kind": "anthropic",
         "models": [{"id": "claude-sonnet-4-6", "tier": "frontier"}]},
    ])


class _MockLedger:
    """In-memory ledger-like for testing: captures events and artifacts."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.artifacts: list[dict] = []

    def append(self, actor: str, kind: str, payload: dict) -> dict:
        ev = {"actor": actor, "kind": kind, "payload": payload}
        self.events.append(ev)
        return ev

    def add_artifact(self, actor: str, kind: str, artifact_id: str, data: dict) -> dict:
        entry = {"actor": actor, "kind": kind, "id": artifact_id, "data": data}
        # Update in place if same id (artifact upsert).
        for i, a in enumerate(self.artifacts):
            if a["id"] == artifact_id:
                self.artifacts[i] = entry
                return entry
        self.artifacts.append(entry)
        return entry

    def events_of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------

class TestRoutingParse(unittest.TestCase):
    """Parsing + validation of routing.yaml shapes."""

    def test_empty_sources_valid(self):
        rt = _make_routing(sources=[])
        self.assertEqual(rt.available_sources(), [])

    def test_wrong_schema_raises(self):
        with self.assertRaises(RoutingError):
            load_routing({"schema": "wrong/1"})

    def test_unknown_kind_raises(self):
        with self.assertRaises(RoutingError):
            _make_routing(sources=[{"id": "x", "kind": "unknown_provider",
                                    "models": []}])

    def test_unknown_tier_raises(self):
        with self.assertRaises(RoutingError):
            _make_routing(sources=[{"id": "x", "kind": "anthropic",
                                    "models": [{"id": "m", "tier": "ultra"}]}])

    def test_unknown_preference_raises(self):
        with self.assertRaises(RoutingError):
            _make_routing(assignments={"overseer": ["super_strongest"]})

    def test_explicit_pref_accepted(self):
        rt = _make_routing(
            sources=[{"id": "cloud_api", "kind": "anthropic",
                      "models": [{"id": "m1", "tier": "frontier"}]}],
            assignments={"overseer": ["explicit:cloud_api/m1"]},
        )
        res = rt.resolve("overseer")
        self.assertIsNotNone(res)
        self.assertEqual(res.model_id, "m1")

    def test_explicit_pref_offline_returns_none(self):
        rt = _make_routing(
            sources=[{"id": "cloud_api", "kind": "anthropic",
                      "models": [{"id": "m1", "tier": "frontier"}]}],
            assignments={"overseer": ["explicit:cloud_api/m1"]},
        )
        res = rt.resolve("overseer", availability={"cloud_api": False})
        # No fallback — explicit pref, source offline, collapse kicks in.
        # The collapse path picks any model from any available source.
        # With all sources offline, result is None.
        self.assertIsNone(res)


class TestRoutingResolution(unittest.TestCase):
    """resolve() correctness."""

    def test_strongest_picks_frontier(self):
        rt = _two_source_routing()
        res = rt.resolve("overseer")
        self.assertIsNotNone(res)
        self.assertEqual(res.model_tier, "frontier")
        self.assertEqual(res.model_id, "claude-sonnet-4-6")

    def test_cheap_local_picks_local(self):
        rt = _two_source_routing()
        res = rt.resolve("sketcher")
        self.assertIsNotNone(res)
        self.assertEqual(res.model_tier, "cheap_local")

    def test_fallthrough_when_local_offline(self):
        """sketcher prefers cheap_local then strong; local offline → picks frontier."""
        rt = _two_source_routing()
        res = rt.resolve("sketcher", availability={"cloud_api": True, "local_lms": False})
        self.assertIsNotNone(res)
        # Falls through to cloud_api (frontier tier, the strong fallback).
        self.assertEqual(res.source_id, "cloud_api")
        self.assertTrue(res.fallthrough)

    def test_fallthrough_event_flagged(self):
        rt = _two_source_routing()
        res = rt.resolve("sketcher", availability={"cloud_api": True, "local_lms": False})
        self.assertTrue(res.fallthrough)

    def test_no_sources_returns_none(self):
        rt = _make_routing(sources=[])
        res = rt.resolve("overseer")
        self.assertIsNone(res)

    def test_single_source_collapse_any_role(self):
        """Single source → all roles collapse onto it."""
        rt = _single_source_routing()
        for role in ("overseer", "orchestrator", "sketcher", "cam_programmer"):
            res = rt.resolve(role)
            self.assertIsNotNone(res, f"role {role!r} should resolve with single source")
            self.assertEqual(res.source_id, "cloud_api")

    def test_unknown_role_falls_to_default(self):
        rt = _two_source_routing()
        res = rt.resolve("totally_unknown_role")
        # Falls to 'default' assignment = [cheap_local, strong].
        self.assertIsNotNone(res)

    def test_resolve_emits_ledger_event(self):
        rt = _two_source_routing()
        ledger = _MockLedger()
        rt.resolve("overseer", ledger=ledger)
        routing_events = ledger.events_of_kind("routing")
        self.assertEqual(len(routing_events), 1)
        ev = routing_events[0]["payload"]
        self.assertEqual(ev["schema"], "aicad.routing/1")
        self.assertEqual(ev["role"], "overseer")
        self.assertIn("model_id", ev)

    def test_all_offline_returns_none(self):
        rt = _two_source_routing()
        res = rt.resolve("overseer",
                         availability={"cloud_api": False, "local_lms": False})
        self.assertIsNone(res)


class TestResolveAssignments(unittest.TestCase):
    """resolve_assignments() — T-W17 settings GUI API."""

    def test_all_roles_returned(self):
        rt = _two_source_routing()
        assignments = rt.resolve_assignments()
        # All roles in the routing config are returned.
        self.assertIn("overseer", assignments)
        self.assertIn("sketcher", assignments)

    def test_returns_resolution_or_none(self):
        rt = _two_source_routing()
        assignments = rt.resolve_assignments()
        for role, res in assignments.items():
            self.assertTrue(
                res is None or isinstance(res, Resolution),
                f"role {role!r}: expected Resolution or None, got {type(res)}"
            )

    def test_no_sources_all_none(self):
        rt = _make_routing(sources=[])
        assignments = rt.resolve_assignments()
        for role, res in assignments.items():
            self.assertIsNone(res, f"role {role!r} should be None with no sources")


class TestApprovalPolicy(unittest.TestCase):
    """approval_for_depth() — ponder special case + depth rules."""

    def test_ponder_auto_at_any_depth(self):
        rt = _make_routing()
        for depth in range(1, DEPTH_CAP + 1):
            self.assertEqual(rt.approval_for_depth(depth, kind="ponder"), "auto",
                             f"ponder at depth {depth} should be auto")

    def test_depth1_worker_auto(self):
        rt = _make_routing()
        self.assertEqual(rt.approval_for_depth(1, kind="worker"), "auto")

    def test_depth2_worker_overseer(self):
        rt = _make_routing()
        self.assertEqual(rt.approval_for_depth(2, kind="worker"), "overseer")

    def test_depth3_worker_overseer(self):
        rt = _make_routing()
        self.assertEqual(rt.approval_for_depth(3, kind="worker"), "overseer")


# ---------------------------------------------------------------------------
# Delegation tree tests
# ---------------------------------------------------------------------------

class TestDelegationTreeBasics(unittest.TestCase):
    """DelegationTree initialisation + node structure."""

    def setUp(self):
        self.routing = _two_source_routing()
        self.ledger = _MockLedger()
        self.tree = DelegationTree(
            run_id="r_test01",
            orchestrator_budget=10_000,
            ledger=self.ledger,
            routing=self.routing,
        )

    def test_root_node_exists_and_approved(self):
        root = self.tree.get_node(self.tree.root_id)
        self.assertIsNotNone(root)
        self.assertEqual(root.status, "approved")
        self.assertEqual(root.depth, 0)
        self.assertEqual(root.role, "orchestrator")

    def test_root_id_is_dg_prefixed(self):
        self.assertTrue(self.tree.root_id.startswith("dg_"))

    def test_artifact_written_on_init(self):
        arts = [a for a in self.ledger.artifacts if a["kind"] == "delegation"]
        self.assertGreater(len(arts), 0)
        data = arts[0]["data"]
        self.assertEqual(data["schema"], DELEGATION_SCHEMA)

    def test_transition_event_on_init(self):
        events = self.ledger.events_of_kind("delegation")
        self.assertGreater(len(events), 0)
        ev = events[0]["payload"]
        self.assertEqual(ev["status"], "approved")


class TestDelegationApprovalMatrix(unittest.TestCase):
    """Approval chain: auto (depth-1), overseer (deeper), ponder (any depth)."""

    def setUp(self):
        self.routing = _two_source_routing()
        self.ledger = _MockLedger()
        self.tree = DelegationTree(
            run_id="r_approval",
            orchestrator_budget=100_000,
            ledger=self.ledger,
            routing=self.routing,
        )

    def test_depth1_worker_auto_approved(self):
        node = self.tree.request_delegation(
            "sketcher", "Draw the base sketch", 2000,
            kind="worker",
        )
        self.assertEqual(node.status, "approved")
        self.assertEqual(node.approval.approved_by, "auto")
        self.assertEqual(node.depth, 1)

    def test_depth2_worker_pending_overseer(self):
        # First create a depth-1 node.
        d1 = self.tree.request_delegation("orchestrator", "Plan phase", 5000, kind="worker")
        self.assertEqual(d1.status, "approved")
        # Now depth-2.
        d2 = self.tree.request_delegation(
            "sketcher", "Sketch sub-task", 1000,
            parent_id=d1.id, kind="worker",
        )
        self.assertEqual(d2.status, "pending")
        self.assertEqual(d2.approval.policy, "overseer")

    def test_depth2_ponder_auto_approved(self):
        d1 = self.tree.request_delegation("orchestrator", "Plan phase", 5000, kind="worker")
        d2 = self.tree.request_delegation(
            "sketcher", "Which datum scheme is best?", 800,
            parent_id=d1.id, kind="ponder",
        )
        self.assertEqual(d2.status, "approved",
                         "ponder must be auto-approved at any depth")
        self.assertEqual(d2.approval.approved_by, "auto")

    def test_depth3_ponder_auto_approved(self):
        d1 = self.tree.request_delegation("orchestrator", "Phase", 10000, kind="worker")
        # d2 is pending (depth=2 worker) — approve it externally.
        d2 = self.tree.request_delegation("sketcher", "Sub", 2000,
                                          parent_id=d1.id, kind="worker")
        self.tree.approve(d2.id, approved_by="overseer")
        # Start d2 so it can delegate.
        self.tree.start(d2.id)
        # Now depth-3 ponder.
        d3 = self.tree.request_delegation(
            "feature_modeler", "Deepest ponder", 400,
            parent_id=d2.id, kind="ponder",
        )
        self.assertEqual(d3.status, "approved")

    def test_overseer_approve_transitions_to_approved(self):
        d1 = self.tree.request_delegation("orchestrator", "Plan", 5000, kind="worker")
        d2 = self.tree.request_delegation("sketcher", "Sub", 1000,
                                          parent_id=d1.id, kind="worker")
        self.assertEqual(d2.status, "pending")
        self.tree.approve(d2.id, approved_by="overseer")
        self.assertEqual(d2.status, "approved")

    def test_approving_non_pending_raises(self):
        d1 = self.tree.request_delegation("sketcher", "Auto-approved", 500, kind="worker")
        self.assertEqual(d1.status, "approved")
        with self.assertRaises(DelegationError):
            self.tree.approve(d1.id)

    def test_approval_request_event_emitted(self):
        d1 = self.tree.request_delegation("orchestrator", "Plan", 5000, kind="worker")
        self.tree.request_delegation("sketcher", "Sub-task needing approval", 500,
                                     parent_id=d1.id, kind="worker")
        approval_events = self.ledger.events_of_kind("delegation_approval_request")
        self.assertEqual(len(approval_events), 1)

    def test_invalid_kind_raises(self):
        with self.assertRaises(DelegationError):
            self.tree.request_delegation("sketcher", "Bad kind", 500, kind="supervisor")

    def test_depth_cap_enforced(self):
        """Cannot exceed DEPTH_CAP levels."""
        parent_id = self.tree.root_id
        for depth in range(1, DEPTH_CAP + 1):
            node = self.tree.request_delegation(
                "sketcher", f"Depth {depth}", 1000,
                parent_id=parent_id, kind="ponder",
            )
            parent_id = node.id
        with self.assertRaises(DelegationError):
            self.tree.request_delegation(
                "sketcher", f"Depth {DEPTH_CAP + 1} — too deep", 100,
                parent_id=parent_id, kind="ponder",
            )


class TestBudgetInheritanceRevocation(unittest.TestCase):
    """Budget accrual + revocation at exact exhaustion."""

    def setUp(self):
        self.routing = _two_source_routing()
        self.ledger = _MockLedger()
        self.tree = DelegationTree(
            run_id="r_budget",
            orchestrator_budget=1000,
            ledger=self.ledger,
            routing=self.routing,
        )

    def _start_d1(self, budget: int = 600) -> Any:
        node = self.tree.request_delegation("sketcher", "Task", budget, kind="worker")
        self.tree.start(node.id)
        return node

    def test_accrue_to_node_and_ancestor(self):
        d1 = self._start_d1(600)
        self.tree.accrue(d1.id, 100)
        root = self.tree.get_node(self.tree.root_id)
        # Both the child and root should have 100 spent.
        self.assertEqual(d1.budget.tokens_spent, 100)
        self.assertEqual(root.budget.tokens_spent, 100)

    def test_revoke_at_exact_exhaustion(self):
        """Revocation fires when remaining hits exactly 0."""
        d1 = self._start_d1(budget=400)
        root = self.tree.get_node(self.tree.root_id)
        # Exhaust root (1000 granted − 1000 spent = 0 remaining).
        self.tree.accrue(d1.id, 1000)
        # Root should be revoked.
        self.assertEqual(root.status, "revoked",
                         "root should be revoked when budget hits 0")
        # Child should also be revoked.
        self.assertEqual(d1.status, "revoked",
                         "child should cascade-revoke when root is revoked")

    def test_revocation_event_emitted(self):
        d1 = self._start_d1(budget=400)
        self.tree.accrue(d1.id, 1000)
        revoke_events = self.ledger.events_of_kind("delegation_revoked")
        self.assertGreater(len(revoke_events), 0)

    def test_no_revoke_below_threshold(self):
        """Accruing less than the budget must NOT revoke."""
        d1 = self._start_d1(600)
        self.tree.accrue(d1.id, 500)  # root has 500 remaining
        root = self.tree.get_node(self.tree.root_id)
        self.assertNotEqual(root.status, "revoked")
        self.assertNotEqual(d1.status, "revoked")

    def test_child_budget_exceeds_ancestor_grant_raises(self):
        """Cannot grant a child a budget larger than the ancestor's total grant."""
        # root has 1000 granted. Requesting a child with 1001 should fail.
        with self.assertRaises(DelegationError):
            self.tree.request_delegation("sketcher", "Too big", 1001, kind="worker")

    def test_child_spend_revokes_at_ancestor_exhaustion(self):
        """Two children can both be granted if total grant <= ancestor.
        But actual spend that exhausts root revokes them."""
        d1 = self._start_d1(600)
        # Second child with 400 — total grants = 1000 = root's budget.
        d2 = self.tree.request_delegation("sketcher", "Second task", 400, kind="worker")
        self.assertEqual(d2.status, "approved")
        # Now accrue 1000 to d1 — exhausts root → both revoked.
        self.tree.accrue(d1.id, 1000)
        root = self.tree.get_node(self.tree.root_id)
        self.assertEqual(root.status, "revoked")

    def test_explicit_revoke(self):
        d1 = self._start_d1(600)
        self.tree.revoke(d1.id, reason="test explicit revoke")
        self.assertEqual(d1.status, "revoked")

    def test_cascade_revoke(self):
        """Revoking a node also revokes all its active descendants."""
        d1 = self.tree.request_delegation("orchestrator", "Phase", 800, kind="worker")
        d2 = self.tree.request_delegation("sketcher", "Ponder", 200,
                                          parent_id=d1.id, kind="ponder")
        self.assertEqual(d2.status, "approved")
        self.tree.revoke(d1.id, reason="cascade test")
        self.assertEqual(d1.status, "revoked")
        self.assertEqual(d2.status, "revoked")

    def test_complete_node(self):
        d1 = self._start_d1(600)
        self.tree.complete(d1.id, tokens_used=50)
        self.assertEqual(d1.status, "done")
        root = self.tree.get_node(self.tree.root_id)
        self.assertEqual(root.budget.tokens_spent, 50)

    def test_fail_node(self):
        d1 = self._start_d1(600)
        self.tree.fail(d1.id, reason="provider error")
        self.assertEqual(d1.status, "failed")


class TestNodeIdentities(unittest.TestCase):
    """Each node gets a unique dg_ id — no shared mutable default."""

    def test_each_request_unique_id(self):
        tree = DelegationTree(run_id="r_ids", orchestrator_budget=50_000,
                              routing=_two_source_routing())
        ids = set()
        for i in range(10):
            node = tree.request_delegation(
                "sketcher", f"Task {i}", 200, kind="ponder",
            )
            self.assertTrue(node.id.startswith("dg_"),
                            f"node id must start with dg_, got {node.id!r}")
            ids.add(node.id)
        # All 10 must be distinct.
        self.assertEqual(len(ids), 10, "node ids must be unique across requests")

    def test_root_id_unique_across_trees(self):
        t1 = DelegationTree(run_id="r1", orchestrator_budget=1000)
        t2 = DelegationTree(run_id="r2", orchestrator_budget=1000)
        self.assertNotEqual(t1.root_id, t2.root_id)


class TestDelegateTool(unittest.TestCase):
    """delegate() callable — the tool exposed to role cards."""

    def setUp(self):
        self.tree = DelegationTree(
            run_id="r_tool",
            orchestrator_budget=10_000,
            routing=_two_source_routing(),
        )

    def test_success_returns_node_dict(self):
        result = delegate("sketcher", "Draw the profile", 500, tree=self.tree)
        self.assertIn("id", result)
        self.assertIn("status", result)
        self.assertTrue(result["id"].startswith("dg_"))
        self.assertEqual(result["status"], "approved")

    def test_depth1_auto_approved_in_result(self):
        result = delegate("sketcher", "Brief text", 200, tree=self.tree)
        self.assertEqual(result["status"], "approved")

    def test_error_returns_error_dict(self):
        result = delegate("sketcher", "Bad budget", -1, tree=self.tree)
        self.assertIn("error", result)
        self.assertEqual(result["status"], "failed")

    def test_depth_cap_via_tool(self):
        """delegate() respects DEPTH_CAP and returns error dict (never raises)."""
        parent_id = self.tree.root_id
        for depth in range(1, DEPTH_CAP + 1):
            result = delegate(
                "sketcher", f"Depth {depth}", 100,
                tree=self.tree, parent_id=parent_id, kind="ponder",
            )
            parent_id = result["id"]
        # Next request exceeds cap.
        result = delegate("sketcher", "Too deep", 50,
                          tree=self.tree, parent_id=parent_id, kind="ponder")
        self.assertIn("error", result)
        self.assertEqual(result["status"], "failed")


class TestArtifactShape(unittest.TestCase):
    """to_artifact() matches the aicad.delegation/1 schema."""

    def test_artifact_schema_tag(self):
        tree = DelegationTree(run_id="r_art", orchestrator_budget=1000)
        art = tree.to_artifact()
        self.assertEqual(art["schema"], DELEGATION_SCHEMA)
        self.assertIn("nodes", art)
        self.assertIn("depth_cap", art)
        self.assertEqual(art["depth_cap"], DEPTH_CAP)

    def test_node_dict_has_required_fields(self):
        tree = DelegationTree(run_id="r_fields", orchestrator_budget=5000,
                              routing=_two_source_routing())
        node = tree.request_delegation("sketcher", "Brief", 500, kind="ponder")
        d = node.to_dict()
        for key in ("id", "parent", "role", "brief", "requested_by", "kind",
                    "depth", "approval", "model", "budget", "docs", "status"):
            self.assertIn(key, d, f"missing required field {key!r} in node dict")


class TestDocLeaseShape(unittest.TestCase):
    """Docs field carries lease requests (T-W16 request shape only)."""

    def test_doc_leases_stored(self):
        tree = DelegationTree(run_id="r_docs", orchestrator_budget=5000)
        node = tree.request_delegation(
            "sketcher", "Need doc access", 500,
            docs=[{"doc_id": "doc_abc", "lease": "read"},
                  {"doc_id": "doc_xyz", "lease": "write"}],
        )
        self.assertEqual(len(node.docs), 2)
        self.assertEqual(node.docs[0].doc_id, "doc_abc")
        self.assertEqual(node.docs[0].lease, "read")
        self.assertEqual(node.docs[1].lease, "write")

    def test_doc_leases_in_artifact(self):
        tree = DelegationTree(run_id="r_docs2", orchestrator_budget=5000)
        node = tree.request_delegation(
            "sketcher", "Doc access", 200,
            docs=[{"doc_id": "doc_123", "lease": "read"}],
        )
        art = tree.to_artifact()
        node_data = next(n for n in art["nodes"] if n["id"] == node.id)
        self.assertEqual(len(node_data["docs"]), 1)
        self.assertEqual(node_data["docs"][0]["doc_id"], "doc_123")


class TestOverseerProviderResolution(unittest.TestCase):
    """overseer.resolve_overseer_provider with routing seam (T-W14)."""

    def test_routing_preferred_over_fallback(self):
        from services.agent.advisor.overseer import resolve_overseer_provider
        # Build a routing table with only a 'cloud_api' source (anthropic tier).
        routing = _two_source_routing()
        available = {
            "cloud_api": MagicMock(name="cloud_provider"),
            "local_lms": MagicMock(name="local_provider"),
        }
        prov = resolve_overseer_provider(available, routing=routing)
        # Overseer = strongest = frontier = cloud_api.
        self.assertEqual(prov, available["cloud_api"])

    def test_fallback_when_no_routing(self):
        from services.agent.advisor.overseer import resolve_overseer_provider
        available = {"anthropic": MagicMock(name="anthropic_prov")}
        prov = resolve_overseer_provider(available, routing=None)
        self.assertIs(prov, available["anthropic"])

    def test_none_when_empty_available(self):
        from services.agent.advisor.overseer import resolve_overseer_provider
        prov = resolve_overseer_provider({}, routing=None)
        self.assertIsNone(prov)

    def test_single_source_collapse(self):
        from services.agent.advisor.overseer import resolve_overseer_provider
        # No routing, only one provider — collapse onto it.
        mock_prov = MagicMock(name="only_prov")
        prov = resolve_overseer_provider({"only": mock_prov}, routing=None)
        self.assertIs(prov, mock_prov)


if __name__ == "__main__":
    unittest.main()

"""Tests for services/agent/delegation.py + services/agent/routing.py.

Contract: contracts/advisor.md §12 (aicad.delegation/1) + §14 (aicad.routing/1).

Test coverage:
  * Every valid delegation auto-approves at every depth and for every kind.
  * Budget accrual + revocation at exact exhaustion.
  * Routing fallthrough when a source is offline.
  * Resolution ledger events emitted on every resolve.
  * Soft depth limit: advisory warning event emitted; spawn still succeeds.
  * Single-source degenerate case (all roles collapse onto one model).
  * resolve_assignments() returns the per-role map (settings GUI API).
  * delegate() tool returns correct dict on success + error dict on failure.
  * Sub-agent identities: each node gets a unique dg_ id (no shared mutable default).
  * Budget meter: tree-wide token+agent tracking, always-current.
  * Kill-switch: revokes all active nodes, sets killed=True, rejects further spawns.
  * Soft-limit advisory events: depth_warning + agent_limit_warning.
  * delegation: block in routing.yaml drives soft_depth_limit + max_agents on RoutingTable.

Offline discipline: mock providers, no network, no LLM, no all-skip.
"""

from __future__ import annotations

from contextlib import nullcontext
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from services.agent.delegation import (
    DEPTH_CAP,
    DEFAULT_MAX_AGENTS,
    DEFAULT_MAX_SUBAGENT_DEPTH,
    DELEGATION_SCHEMA,
    BudgetMeter,
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
# Hermeticity: DelegationTree falls back to agent.max_subagent_depth from the
# developer's live config.json when the kwarg is omitted. These are unit tests
# of tree mechanics, so pin the getter to the documented default — otherwise a
# local Settings toggle (e.g. max_subagent_depth 0 or 1) turns the file red.
# Tests that assert on the cap itself pass max_subagent_depth explicitly.
# ---------------------------------------------------------------------------

_depth_patch = None


def setUpModule() -> None:
    global _depth_patch
    _depth_patch = patch(
        "services.agent.config.get_max_subagent_depth",
        return_value=DEFAULT_MAX_SUBAGENT_DEPTH,
    )
    _depth_patch.start()


def tearDownModule() -> None:
    if _depth_patch is not None:
        _depth_patch.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_routing(
    sources: list[dict] | None = None,
    assignments: dict | None = None,
) -> RoutingTable:
    """Build a RoutingTable from raw dicts (test helper)."""
    cfg: dict = {
        "schema": "aicad.routing/1",
        "sources": sources or [],
        "assignments": assignments
        or {
            "overseer": ["strongest"],
            "orchestrator": ["strongest"],
            "sketcher": ["cheap_local", "strong"],
            "default": ["cheap_local", "strong"],
        },
    }
    return load_routing(cfg)


def _two_source_routing() -> RoutingTable:
    return _make_routing(
        sources=[
            {
                "id": "cloud_api",
                "kind": "anthropic",
                "models": [{"id": "claude-sonnet-4-6", "tier": "frontier"}],
            },
            {
                "id": "local_lms",
                "kind": "lmstudio",
                "models": [{"id": "qwen3-4b", "tier": "cheap_local"}],
            },
        ]
    )


def _single_source_routing() -> RoutingTable:
    return _make_routing(
        sources=[
            {
                "id": "cloud_api",
                "kind": "anthropic",
                "models": [{"id": "claude-sonnet-4-6", "tier": "frontier"}],
            },
        ]
    )


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
            _make_routing(
                sources=[{"id": "x", "kind": "unknown_provider", "models": []}]
            )

    def test_unknown_tier_raises(self):
        with self.assertRaises(RoutingError):
            _make_routing(
                sources=[
                    {
                        "id": "x",
                        "kind": "anthropic",
                        "models": [{"id": "m", "tier": "ultra"}],
                    }
                ]
            )

    def test_unknown_preference_raises(self):
        with self.assertRaises(RoutingError):
            _make_routing(assignments={"overseer": ["super_strongest"]})

    def test_explicit_pref_accepted(self):
        rt = _make_routing(
            sources=[
                {
                    "id": "cloud_api",
                    "kind": "anthropic",
                    "models": [{"id": "m1", "tier": "frontier"}],
                }
            ],
            assignments={"overseer": ["explicit:cloud_api/m1"]},
        )
        res = rt.resolve("overseer")
        self.assertIsNotNone(res)
        self.assertEqual(res.model_id, "m1")

    def test_explicit_pref_offline_returns_none(self):
        rt = _make_routing(
            sources=[
                {
                    "id": "cloud_api",
                    "kind": "anthropic",
                    "models": [{"id": "m1", "tier": "frontier"}],
                }
            ],
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
        res = rt.resolve(
            "sketcher", availability={"cloud_api": True, "local_lms": False}
        )
        self.assertIsNotNone(res)
        # Falls through to cloud_api (frontier tier, the strong fallback).
        self.assertEqual(res.source_id, "cloud_api")
        self.assertTrue(res.fallthrough)

    def test_fallthrough_event_flagged(self):
        rt = _two_source_routing()
        res = rt.resolve(
            "sketcher", availability={"cloud_api": True, "local_lms": False}
        )
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
            self.assertIsNotNone(
                res, f"role {role!r} should resolve with single source"
            )
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
        res = rt.resolve(
            "overseer", availability={"cloud_api": False, "local_lms": False}
        )
        self.assertIsNone(res)


class TestResolveAssignments(unittest.TestCase):
    """resolve_assignments() — settings GUI API."""

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
                f"role {role!r}: expected Resolution or None, got {type(res)}",
            )

    def test_no_sources_all_none(self):
        rt = _make_routing(sources=[])
        assignments = rt.resolve_assignments()
        for role, res in assignments.items():
            self.assertIsNone(res, f"role {role!r} should be None with no sources")


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


class TestDelegationAutoApproval(unittest.TestCase):
    """Every valid delegation is approved immediately, regardless of depth or kind."""

    def setUp(self):
        self.routing = _two_source_routing()
        self.ledger = _MockLedger()
        self.tree = DelegationTree(
            run_id="r_approval",
            orchestrator_budget=100_000,
            ledger=self.ledger,
            routing=self.routing,
            # Approval-matrix tests exercise depth-3 ponder; hard nesting
            # cap is covered separately in TestMaxSubagentDepth.
            max_subagent_depth=8,
        )

    def test_depth1_worker_auto_approved(self):
        node = self.tree.request_delegation(
            "sketcher",
            "Draw the base sketch",
            2000,
            kind="worker",
        )
        self.assertEqual(node.status, "approved")
        self.assertTrue(node.approved_ts)
        self.assertEqual(node.depth, 1)

    def test_deep_worker_auto_approved_without_human_involvement(self):
        d1 = self.tree.request_delegation(
            "orchestrator",
            "Plan phase",
            5000,
            kind="worker",
        )
        d2 = self.tree.request_delegation(
            "sketcher",
            "Sketch sub-task",
            1000,
            parent_id=d1.id,
            kind="worker",
        )
        self.assertEqual(d2.depth, 2)
        self.assertEqual(d2.status, "approved")
        self.assertTrue(d2.approved_ts)

    def test_depth2_ponder_auto_approved(self):
        d1 = self.tree.request_delegation(
            "orchestrator", "Plan phase", 5000, kind="worker"
        )
        d2 = self.tree.request_delegation(
            "sketcher",
            "Which datum scheme is best?",
            800,
            parent_id=d1.id,
            kind="ponder",
        )
        self.assertEqual(
            d2.status, "approved", "ponder must be auto-approved at any depth"
        )
        self.assertTrue(d2.approved_ts)

    def test_invalid_kind_raises(self):
        with self.assertRaises(DelegationError):
            self.tree.request_delegation("sketcher", "Bad kind", 500, kind="supervisor")

    def test_soft_depth_limit_warning_not_error(self):
        """Soft depth advisory still fires; hard nesting cap is separate.

        With max_subagent_depth high enough, spawning past soft_depth_limit
        emits delegation_depth_warning but the spawn SUCCEEDS.
        """
        # Use a tree with soft_depth_limit=3 to hit the boundary quickly.
        # Raise max_subagent_depth so the hard cap does not refuse first.
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_softcap",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            soft_depth_limit=3,
            max_subagent_depth=8,
        )
        parent_id = tree.root_id
        # Spawn through the soft limit — all should succeed (ponder = auto-approved).
        for depth in range(1, 6):
            node = tree.request_delegation(
                "sketcher",
                f"Depth {depth}",
                500,
                parent_id=parent_id,
                kind="ponder",
            )
            self.assertIsNotNone(node, f"spawn at depth {depth} must succeed")
            self.assertEqual(
                node.status, "approved", f"ponder at depth {depth} must be approved"
            )
            parent_id = node.id
        # A depth_warning event must have been emitted when depth >= soft_depth_limit.
        warn_events = ledger.events_of_kind("delegation_depth_warning")
        self.assertGreater(
            len(warn_events),
            0,
            "delegation_depth_warning event must fire at soft limit",
        )
        # No DelegationError must have been raised (test would have failed above).

    def test_depth_cap_constant_still_exported(self):
        """DEPTH_CAP is kept as a module constant for backward-compat with artifact readers."""
        self.assertEqual(DEPTH_CAP, 3)  # value must not change


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
            # Pin the hard nesting cap: this class exercises budget accrual and
            # cascade revocation, not agent.max_subagent_depth. Left unset it
            # would be read from the developer's config.json and a local
            # setting of 1 would refuse the depth-2 spawn in test_cascade_revoke.
            max_subagent_depth=8,
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
        self.assertEqual(
            root.status, "revoked", "root should be revoked when budget hits 0"
        )
        # Child should also be revoked.
        self.assertEqual(
            d1.status, "revoked", "child should cascade-revoke when root is revoked"
        )

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
        d2 = self.tree.request_delegation(
            "sketcher", "Ponder", 200, parent_id=d1.id, kind="ponder"
        )
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
        tree = DelegationTree(
            run_id="r_ids", orchestrator_budget=50_000, routing=_two_source_routing()
        )
        ids = set()
        for i in range(10):
            node = tree.request_delegation(
                "sketcher",
                f"Task {i}",
                200,
                kind="ponder",
            )
            self.assertTrue(
                node.id.startswith("dg_"),
                f"node id must start with dg_, got {node.id!r}",
            )
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
        self.assertIs(result["ok"], True)

    def test_depth1_auto_approved_in_result(self):
        result = delegate("sketcher", "Brief text", 200, tree=self.tree)
        self.assertEqual(result["status"], "approved")

    def test_refusal_returns_named_actionable_reason(self):
        result = delegate("sketcher", "Bad budget", -1, tree=self.tree)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["reason"], "invalid_budget")
        self.assertEqual(result["status"], "failed")
        self.assertIn("budget_tokens must be positive", result["error"])
        self.assertNotIn("brief", result)

    def test_depth_past_old_cap_via_tool_succeeds(self):
        """Soft-limit advisory events fire; spawn succeeds when under hard cap.

        The call never raises for soft limits — only max_subagent_depth refuses.
        """
        # Use a tree with soft_depth_limit=3 and a high hard nesting cap so we
        # can prove soft-limit warnings do not produce an error dict.
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_tool_depth",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            soft_depth_limit=3,
            max_subagent_depth=8,
        )
        parent_id = tree.root_id
        for depth in range(1, DEPTH_CAP + 2):  # one past the old hard cap
            result = delegate(
                "sketcher",
                f"Depth {depth}",
                100,
                tree=tree,
                parent_id=parent_id,
                kind="ponder",
            )
            self.assertNotIn(
                "error", result, f"spawn at depth {depth} must not return error dict"
            )
            self.assertEqual(
                result["status"],
                "approved",
                f"ponder at depth {depth} must be approved",
            )
            parent_id = result["id"]
        # Warning events must exist for depths >= soft_depth_limit.
        warn_events = ledger.events_of_kind("delegation_depth_warning")
        self.assertGreater(len(warn_events), 0)

    def test_dispatched_success_is_reported_ok_true(self):
        from services.agent.agent_turn import _dispatch_tool
        from services.agent.mission_dispatch import MissionDispatch

        mission = MissionDispatch(
            run_id="r_delegate_success",
            delegation_tree=self.tree,
            provider=object(),
        )
        mission._run_subagent = MagicMock(
            return_value={
                "ok": True,
                "subagent_status": "done",
                "handoff": {"status": "done", "summary": "cube built"},
                "result": {"calls": [{"tool": "new_part", "ok": True}]},
                "tokens_used": 100,
            }
        )
        result = _dispatch_tool(
            "delegate",
            {
                "role": "feature_modeler",
                "brief": "Build a small test cube from scratch.",
                "budget_tokens": 500,
            },
            _inner=lambda name, args: mission.resolve(name)(**args),
        )

        self.assertIs(result["ok"], True)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["subagent_status"], "done")
        self.assertTrue(result["id"].startswith("dg_"))

    def test_child_failure_surfaces_cause_before_spawn_record(self):
        """Reproduce the live holster path through the normal dispatch wrapper."""
        from services.agent.agent_events import tool_call_event
        from services.agent.agent_turn import _dispatch_tool
        from services.agent.mission_dispatch import MissionDispatch

        mission = MissionDispatch(
            run_id="r_delegate_provider_failure",
            delegation_tree=self.tree,
            provider=object(),
        )
        mission._run_subagent = MagicMock(
            return_value={
                "ok": False,
                "subagent_status": "failed",
                "stop_reason": "provider_error",
                "result": {
                    "error": "provider error: endpoint returned no completion",
                    "error_detail": (
                        "kimi-code: RuntimeError: endpoint returned no completion"
                    ),
                    "stop_reason": "provider_error",
                },
            }
        )
        result = _dispatch_tool(
            "delegate",
            {
                "role": "feature_modeler",
                "brief": "Build a small test cube from scratch.",
                "budget_tokens": 500,
            },
            _inner=lambda name, args: mission.resolve(name)(**args),
        )

        self.assertIs(result["ok"], False)
        self.assertEqual(result["reason"], "subagent_provider_error")
        self.assertIn("stop_reason='provider_error'", result["error"])
        self.assertIn("endpoint returned no completion", result["error"])
        self.assertEqual(list(result)[:3], ["ok", "reason", "error"])
        self.assertIn(result["error"], str(result)[:999])

        _, event = tool_call_event(
            "parent",
            "delegate",
            {},
            result,
            ok=False,
            latency_ms=1.0,
        )
        self.assertEqual(event["error_preview"], result["error"])


class TestDelegateTerminalTransitions(unittest.TestCase):
    """A delegate run always leaves its coordination node terminal."""

    def setUp(self):
        from services.agent.mission_dispatch import MissionDispatch

        self.ledger = _MockLedger()
        self.tree = DelegationTree(
            run_id="r_delegate_terminal",
            orchestrator_budget=10_000,
            ledger=self.ledger,
            routing=_two_source_routing(),
        )
        self.node = self.tree.request_delegation(
            "sketcher",
            "Build the delegated profile",
            500,
        )
        self.mission = MissionDispatch(
            run_id="r_delegate_terminal",
            delegation_tree=self.tree,
            provider=object(),
        )

    def _run_with(self, *, outcome=None, error: Exception | None = None):
        runner = MagicMock(return_value=outcome, side_effect=error)
        with (
            patch("services.mcp.cad_common.doc_scope", return_value=nullcontext()),
            patch.object(self.mission, "_run_subagent_turn", runner),
            patch("services.agent.subagent_registry.set_status"),
        ):
            return self.mission._run_subagent(self.node.to_dict(), "delegate brief")

    def test_delegate_exception_still_marks_child_terminal(self):
        result = self._run_with(error=RuntimeError("provider exploded"))

        self.assertIs(result["ok"], False)
        self.assertIn("provider exploded", result["error"])
        self.assertEqual(self.tree._nodes[self.node.id].status, "failed")
        terminal = [
            event
            for event in self.ledger.events_of_kind("delegation")
            if event["payload"].get("node_id") == self.node.id
            and event["payload"].get("status") in ("done", "failed")
        ]
        self.assertEqual(len(terminal), 1)

    def test_raising_terminal_write_emits_observability_event(self):
        with (
            patch("services.mcp.cad_common.doc_scope", return_value=nullcontext()),
            patch.object(
                self.mission,
                "_run_subagent_turn",
                side_effect=RuntimeError("provider exploded"),
            ),
            patch.object(
                self.tree,
                "fail",
                side_effect=RuntimeError("terminal store unavailable"),
            ),
            self.assertLogs("services.agent.delegation", level="ERROR"),
        ):
            result = self.mission._run_subagent(
                self.node.to_dict(),
                "delegate brief",
            )

        self.assertIs(result["ok"], False)
        events = self.ledger.events_of_kind(
            "delegation_terminal_transition_error",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["node_id"], self.node.id)
        self.assertEqual(events[0]["payload"]["target_status"], "failed")
        self.assertIn(
            "terminal store unavailable",
            events[0]["payload"]["error"],
        )

    def test_delegate_success_path_still_completes_once(self):
        outcome = {
            "ok": True,
            "subagent_status": "done",
            "result": {"summary": "profile built"},
            "tokens_used": 125,
        }
        result = self._run_with(outcome=outcome)

        self.assertIs(result, outcome)
        node = self.tree._nodes[self.node.id]
        self.assertEqual(node.status, "done")
        self.assertEqual(node.budget.tokens_spent, 125)
        terminal = [
            event
            for event in self.ledger.events_of_kind("delegation")
            if event["payload"].get("node_id") == self.node.id
            and event["payload"].get("status") in ("done", "failed")
        ]
        self.assertEqual(len(terminal), 1)

    def test_stop_path_marks_child_failed_and_registry_terminated(self):
        outcome = {
            "ok": False,
            "subagent_status": "terminated",
            "stop_reason": "terminated_by_user",
            "tokens_used": 25,
        }
        with (
            patch("services.mcp.cad_common.doc_scope", return_value=nullcontext()),
            patch.object(
                self.mission,
                "_run_subagent_turn",
                return_value=outcome,
            ),
            patch("services.agent.subagent_registry.set_status") as set_status,
        ):
            result = self.mission._run_subagent(
                self.node.to_dict(),
                "delegate brief",
            )

        self.assertIs(result, outcome)
        node = self.tree._nodes[self.node.id]
        self.assertEqual(node.status, "failed")
        self.assertEqual(node.budget.tokens_spent, 25)
        set_status.assert_any_call(
            self.node.id,
            "terminated",
            note="terminated by user at turn boundary",
        )


class TestArtifactShape(unittest.TestCase):
    """to_artifact() matches the aicad.delegation/1 schema."""

    def test_artifact_schema_tag(self):
        tree = DelegationTree(run_id="r_art", orchestrator_budget=1000)
        art = tree.to_artifact()
        self.assertEqual(art["schema"], DELEGATION_SCHEMA)
        self.assertIn("nodes", art)
        # depth_cap retained for backward compat with artifact readers.
        self.assertIn("depth_cap", art)
        self.assertEqual(art["depth_cap"], DEPTH_CAP)
        # Additive budget-meter / soft-limit fields.
        self.assertIn("soft_depth_limit", art)
        self.assertIn("budget_meter", art)
        self.assertIn("killed", art)
        self.assertFalse(art["killed"])
        meter = art["budget_meter"]
        for key in (
            "tokens_granted",
            "tokens_spent",
            "tokens_remaining",
            "agents_spawned",
            "max_agents",
            "soft_depth_limit",
            "max_subagent_depth",
            "killed",
        ):
            self.assertIn(key, meter, f"budget_meter missing field {key!r}")

    def test_node_dict_has_required_fields(self):
        tree = DelegationTree(
            run_id="r_fields", orchestrator_budget=5000, routing=_two_source_routing()
        )
        node = tree.request_delegation("sketcher", "Brief", 500, kind="ponder")
        d = node.to_dict()
        for key in (
            "id",
            "parent",
            "role",
            "brief",
            "requested_by",
            "kind",
            "depth",
            "approved_ts",
            "model",
            "budget",
            "docs",
            "status",
        ):
            self.assertIn(key, d, f"missing required field {key!r} in node dict")


class TestDocLeaseShape(unittest.TestCase):
    """Docs field carries lease requests (request shape only)."""

    def test_doc_leases_stored(self):
        tree = DelegationTree(run_id="r_docs", orchestrator_budget=5000)
        node = tree.request_delegation(
            "sketcher",
            "Need doc access",
            500,
            docs=[
                {"doc_id": "doc_abc", "lease": "read"},
                {"doc_id": "doc_xyz", "lease": "write"},
            ],
        )
        self.assertEqual(len(node.docs), 2)
        self.assertEqual(node.docs[0].doc_id, "doc_abc")
        self.assertEqual(node.docs[0].lease, "read")
        self.assertEqual(node.docs[1].lease, "write")

    def test_doc_leases_in_artifact(self):
        tree = DelegationTree(run_id="r_docs2", orchestrator_budget=5000)
        node = tree.request_delegation(
            "sketcher",
            "Doc access",
            200,
            docs=[{"doc_id": "doc_123", "lease": "read"}],
        )
        art = tree.to_artifact()
        node_data = next(n for n in art["nodes"] if n["id"] == node.id)
        self.assertEqual(len(node_data["docs"]), 1)
        self.assertEqual(node_data["docs"][0]["doc_id"], "doc_123")


class TestOverseerProviderResolution(unittest.TestCase):
    """overseer.resolve_overseer_provider with routing seam."""

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


# ---------------------------------------------------------------------------
# Budget meter tests
# ---------------------------------------------------------------------------


class TestBudgetMeter(unittest.TestCase):
    """BudgetMeter: tree-wide token+agent tracking."""

    def test_initial_meter_state(self):
        tree = DelegationTree(
            run_id="r_meter1",
            orchestrator_budget=10_000,
            soft_depth_limit=8,
            max_agents=64,
        )
        m = tree.budget_meter
        self.assertEqual(m.tokens_granted, 10_000)
        self.assertEqual(m.tokens_spent, 0)
        self.assertEqual(m.agents_spawned, 0)
        self.assertEqual(m.soft_depth_limit, 8)
        self.assertEqual(m.max_agents, 64)
        self.assertFalse(m.killed)

    def test_meter_tokens_remaining(self):
        tree = DelegationTree(run_id="r_meter2", orchestrator_budget=5_000)
        self.assertEqual(tree.budget_meter.tokens_remaining, 5_000)

    def test_meter_syncs_on_accrue(self):
        """budget_meter.tokens_spent mirrors root's spent counter after accrue."""
        tree = DelegationTree(
            run_id="r_meter3", orchestrator_budget=10_000, routing=_two_source_routing()
        )
        node = tree.request_delegation("sketcher", "task", 2000, kind="worker")
        tree.start(node.id)
        tree.accrue(node.id, 300)
        self.assertEqual(tree.budget_meter.tokens_spent, 300)
        self.assertEqual(tree.budget_meter.tokens_remaining, 9_700)

    def test_meter_agent_count_increments(self):
        """agents_spawned increments with every new non-root delegation."""
        tree = DelegationTree(
            run_id="r_meter4", orchestrator_budget=50_000, routing=_two_source_routing()
        )
        self.assertEqual(tree.budget_meter.agents_spawned, 0)
        tree.request_delegation("sketcher", "task 1", 500, kind="ponder")
        self.assertEqual(tree.budget_meter.agents_spawned, 1)
        tree.request_delegation("sketcher", "task 2", 500, kind="ponder")
        self.assertEqual(tree.budget_meter.agents_spawned, 2)

    def test_meter_in_artifact(self):
        """budget_meter is present in every artifact flush."""
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_meter5", orchestrator_budget=1000, ledger=ledger
        )
        tree.request_delegation("sketcher", "brief", 200, kind="ponder")
        arts = [a for a in ledger.artifacts if a["kind"] == "delegation"]
        self.assertGreater(len(arts), 0)
        art = arts[-1]["data"]
        self.assertIn("budget_meter", art)
        self.assertEqual(art["budget_meter"]["agents_spawned"], 1)

    def test_budget_meter_to_dict_fields(self):
        m = BudgetMeter(
            tokens_granted=1000,
            tokens_spent=200,
            agents_spawned=3,
            max_agents=10,
            soft_depth_limit=5,
        )
        d = m.to_dict()
        self.assertEqual(d["tokens_granted"], 1000)
        self.assertEqual(d["tokens_spent"], 200)
        self.assertEqual(d["tokens_remaining"], 800)
        self.assertAlmostEqual(d["token_pct"], 0.2)
        self.assertEqual(d["agents_spawned"], 3)
        self.assertEqual(d["agents_remaining"], 7)
        self.assertEqual(d["max_agents"], 10)
        self.assertAlmostEqual(d["agent_pct"], 0.3)
        self.assertEqual(d["soft_depth_limit"], 5)
        self.assertFalse(d["killed"])

    def test_meter_agent_pct(self):
        m = BudgetMeter(tokens_granted=1000, agents_spawned=32, max_agents=64)
        self.assertAlmostEqual(m.agent_pct, 0.5)

    def test_meter_token_pct(self):
        m = BudgetMeter(tokens_granted=1000, tokens_spent=500)
        self.assertAlmostEqual(m.token_pct, 0.5)

    def test_meter_token_pct_zero_grant(self):
        """Zero-division guard: token_pct returns 0.0 when grant is 0."""
        m = BudgetMeter(tokens_granted=0)
        self.assertEqual(m.token_pct, 0.0)


# ---------------------------------------------------------------------------
# Kill-switch tests
# ---------------------------------------------------------------------------


class TestKillSwitch(unittest.TestCase):
    """kill(): halt the whole tree, set killed=True, reject subsequent spawns."""

    def _tree_with_active_nodes(self) -> tuple[DelegationTree, Any, Any]:
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_kill1",
            orchestrator_budget=50_000,
            ledger=ledger,
            routing=_two_source_routing(),
        )
        d1 = tree.request_delegation("sketcher", "task a", 1000, kind="ponder")
        d2 = tree.request_delegation("sketcher", "task b", 500, kind="ponder")
        return tree, d1, d2

    def test_kill_revokes_all_active_nodes(self):
        tree, d1, d2 = self._tree_with_active_nodes()
        tree.kill()
        root = tree.get_node(tree.root_id)
        self.assertEqual(root.status, "revoked", "root must be revoked after kill()")
        self.assertEqual(d1.status, "revoked", "d1 must be revoked after kill()")
        self.assertEqual(d2.status, "revoked", "d2 must be revoked after kill()")

    def test_kill_sets_killed_flag(self):
        tree, _, _ = self._tree_with_active_nodes()
        self.assertFalse(tree.budget_meter.killed)
        tree.kill()
        self.assertTrue(tree.budget_meter.killed)

    def test_kill_rejects_subsequent_spawns(self):
        """After kill(), request_delegation raises DelegationError."""
        tree, _, _ = self._tree_with_active_nodes()
        tree.kill()
        with self.assertRaises(DelegationError) as ctx:
            tree.request_delegation("sketcher", "this must fail", 100, kind="ponder")
        self.assertIn("killed", str(ctx.exception).lower())

    def test_kill_emits_delegation_killed_event(self):
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_kill2",
            orchestrator_budget=5000,
            ledger=ledger,
            routing=_two_source_routing(),
        )
        tree.request_delegation("sketcher", "task", 500, kind="ponder")
        tree.kill()
        killed_events = ledger.events_of_kind("delegation_killed")
        self.assertEqual(len(killed_events), 1)
        ev = killed_events[0]["payload"]
        self.assertEqual(ev["schema"], DELEGATION_SCHEMA)
        self.assertIn("budget_meter", ev)
        self.assertTrue(ev["budget_meter"]["killed"])

    def test_kill_is_idempotent(self):
        """Calling kill() twice must not raise or double-emit events."""
        ledger = _MockLedger()
        tree = DelegationTree(run_id="r_kill3", orchestrator_budget=5000, ledger=ledger)
        tree.kill()
        tree.kill()  # second call must be silent
        self.assertEqual(len(ledger.events_of_kind("delegation_killed")), 1)

    def test_kill_artifact_shows_killed_true(self):
        tree, _, _ = self._tree_with_active_nodes()
        tree.kill()
        art = tree.to_artifact()
        self.assertTrue(art["killed"])
        self.assertTrue(art["budget_meter"]["killed"])

    def test_kill_via_delegate_tool_returns_error(self):
        """After kill(), the delegate() tool returns error dict (never raises)."""
        tree, _, _ = self._tree_with_active_nodes()
        tree.kill()
        result = delegate("sketcher", "should fail", 100, tree=tree, kind="ponder")
        self.assertIn("error", result)
        self.assertEqual(result["status"], "failed")

    def test_kill_revokes_deep_approved_nodes(self):
        """Approved nodes at multiple depths are revoked by kill()."""
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_kill4",
            orchestrator_budget=50_000,
            ledger=ledger,
            routing=_two_source_routing(),
            max_subagent_depth=8,
        )
        d1 = tree.request_delegation("orchestrator", "phase", 5000, kind="worker")
        d2 = tree.request_delegation(
            "sketcher", "sub-task", 1000, parent_id=d1.id, kind="worker"
        )
        self.assertEqual(d1.status, "approved")
        self.assertEqual(d2.status, "approved")
        tree.kill()
        self.assertEqual(d1.status, "revoked")
        self.assertEqual(d2.status, "revoked")


# ---------------------------------------------------------------------------
# Soft-limit advisory event tests
# ---------------------------------------------------------------------------


class TestSoftLimitAdvisory(unittest.TestCase):
    """Soft depth + agent count limits emit advisory events, not errors."""

    def test_depth_warning_event_at_soft_limit(self):
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_soft1",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            soft_depth_limit=3,
            max_subagent_depth=8,
        )
        parent_id = tree.root_id
        for depth in range(1, 5):  # spawn to depth 4, past limit of 3
            node = tree.request_delegation(
                "sketcher",
                f"Depth {depth}",
                200,
                parent_id=parent_id,
                kind="ponder",
            )
            parent_id = node.id
        warn_events = ledger.events_of_kind("delegation_depth_warning")
        self.assertGreater(len(warn_events), 0)
        # Each warning must include schema, depth, and soft_depth_limit.
        for ev in warn_events:
            p = ev["payload"]
            self.assertEqual(p["schema"], DELEGATION_SCHEMA)
            self.assertIn("depth", p)
            self.assertIn("soft_depth_limit", p)
            self.assertGreaterEqual(p["depth"], 3)

    def test_no_depth_warning_below_soft_limit(self):
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_soft2",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            soft_depth_limit=5,
            max_subagent_depth=8,
        )
        # Spawn only 2 deep — no warning expected.
        d1 = tree.request_delegation("sketcher", "depth 1", 500, kind="ponder")
        tree.request_delegation(
            "sketcher", "depth 2", 200, parent_id=d1.id, kind="ponder"
        )
        warn_events = ledger.events_of_kind("delegation_depth_warning")
        self.assertEqual(len(warn_events), 0)

    def test_agent_limit_warning_at_max_agents(self):
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_soft3",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            max_agents=3,
        )
        # Spawn exactly max_agents nodes.
        for i in range(3):
            tree.request_delegation("sketcher", f"agent {i}", 200, kind="ponder")
        warn_events = ledger.events_of_kind("delegation_agent_limit_warning")
        self.assertGreater(len(warn_events), 0)
        ev = warn_events[0]["payload"]
        self.assertEqual(ev["schema"], DELEGATION_SCHEMA)
        self.assertIn("agents_spawned", ev)
        self.assertIn("max_agents", ev)

    def test_no_agent_warning_below_max(self):
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_soft4",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            max_agents=10,
        )
        for i in range(5):
            tree.request_delegation("sketcher", f"agent {i}", 200, kind="ponder")
        warn_events = ledger.events_of_kind("delegation_agent_limit_warning")
        self.assertEqual(len(warn_events), 0)

    def test_advisory_events_do_not_prevent_spawn(self):
        """Soft-limit warnings must not stop the spawn from completing."""
        ledger = _MockLedger()
        tree = DelegationTree(
            run_id="r_soft5",
            orchestrator_budget=100_000,
            ledger=ledger,
            routing=_two_source_routing(),
            soft_depth_limit=1,
            max_agents=1,
            max_subagent_depth=8,
        )
        # Both limits hit immediately — spawn must still succeed.
        node = tree.request_delegation("sketcher", "first task", 500, kind="ponder")
        self.assertEqual(node.status, "approved")
        # Second spawn also succeeds (past both limits).
        node2 = tree.request_delegation("sketcher", "second task", 200, kind="ponder")
        self.assertEqual(node2.status, "approved")


# ---------------------------------------------------------------------------
# Routing delegation config tests
# ---------------------------------------------------------------------------


class TestRoutingDelegationConfig(unittest.TestCase):
    """delegation: block in routing.yaml / load_routing dict drives RoutingTable."""

    def test_delegation_config_parsed_from_dict(self):
        cfg = {
            "schema": "aicad.routing/1",
            "sources": [],
            "assignments": {"default": ["strongest"]},
            "delegation": {"soft_depth_limit": 12, "max_agents": 128},
        }
        rt = load_routing(cfg)
        self.assertEqual(rt.soft_depth_limit, 12)
        self.assertEqual(rt.max_agents, 128)

    def test_delegation_config_defaults_when_missing(self):
        cfg = {
            "schema": "aicad.routing/1",
            "sources": [],
            "assignments": {"default": ["strongest"]},
        }
        rt = load_routing(cfg)
        self.assertEqual(rt.soft_depth_limit, 8)  # DEFAULT_SOFT_DEPTH_LIMIT
        self.assertEqual(rt.max_agents, 64)  # DEFAULT_MAX_AGENTS

    def test_routing_yaml_has_delegation_block(self):
        """The canonical routing.yaml on disk must have a delegation: block."""
        rt = load_routing()  # reads the real routing.yaml next to the module
        self.assertIsInstance(rt.soft_depth_limit, int)
        self.assertGreater(rt.soft_depth_limit, 0)
        self.assertIsInstance(rt.max_agents, int)
        self.assertGreater(rt.max_agents, 0)


# ---------------------------------------------------------------------------
# agent.max_subagent_depth hard nesting cap
# ---------------------------------------------------------------------------


class TestMaxSubagentDepth(unittest.TestCase):
    """Hard nesting depth (agent.max_subagent_depth) refuses past the limit."""

    def test_depth_refusal_at_limit(self):
        """With max_subagent_depth=2, depth-1 and depth-2 succeed; depth-3 refuses."""
        tree = DelegationTree(
            run_id="r_hardcap",
            orchestrator_budget=100_000,
            routing=_two_source_routing(),
            max_subagent_depth=2,
            soft_depth_limit=99,
        )
        d1 = tree.request_delegation("sketcher", "depth 1", 500, kind="ponder")
        self.assertEqual(d1.depth, 1)
        d2 = tree.request_delegation(
            "sketcher",
            "depth 2",
            200,
            parent_id=d1.id,
            kind="ponder",
        )
        self.assertEqual(d2.depth, 2)
        with self.assertRaises(DelegationError) as ctx:
            tree.request_delegation(
                "sketcher",
                "depth 3",
                100,
                parent_id=d2.id,
                kind="ponder",
            )
        msg = str(ctx.exception)
        self.assertIn("max subagent nesting depth (2) reached", msg)

    def test_depth_zero_blocks_all_spawns(self):
        """max_subagent_depth=0: root may not spawn any subagent (depth 1 refused)."""
        tree = DelegationTree(
            run_id="r_zero",
            orchestrator_budget=10_000,
            routing=_two_source_routing(),
            max_subagent_depth=0,
        )
        with self.assertRaises(DelegationError) as ctx:
            tree.request_delegation("sketcher", "nope", 100, kind="ponder")
        self.assertIn("max subagent nesting depth (0) reached", str(ctx.exception))

    def test_delegate_tool_returns_error_dict_at_limit(self):
        tree = DelegationTree(
            run_id="r_tool_hard",
            orchestrator_budget=50_000,
            routing=_two_source_routing(),
            max_subagent_depth=1,
        )
        r1 = delegate("sketcher", "ok", 200, tree=tree, kind="ponder")
        self.assertNotIn("error", r1)
        r2 = delegate(
            "sketcher",
            "too deep",
            100,
            tree=tree,
            parent_id=r1["id"],
            kind="ponder",
        )
        self.assertIn("error", r2)
        self.assertEqual(r2["status"], "failed")
        self.assertIn("max subagent nesting depth (1) reached", r2["error"])
        self.assertIs(r2["ok"], False)
        self.assertEqual(r2["reason"], "max_subagent_depth")

    def test_artifact_includes_max_subagent_depth(self):
        tree = DelegationTree(
            run_id="r_art",
            orchestrator_budget=1000,
            max_subagent_depth=3,
        )
        art = tree.to_artifact()
        self.assertEqual(art["max_subagent_depth"], 3)
        self.assertEqual(art["budget_meter"]["max_subagent_depth"], 3)

    def test_default_constant(self):
        self.assertEqual(DEFAULT_MAX_SUBAGENT_DEPTH, 2)


# ---------------------------------------------------------------------------
# Coordination board
# ---------------------------------------------------------------------------


class TestCoordinationBoard(unittest.TestCase):
    """Claim conflict detection, release frees, progress notes."""

    def setUp(self):
        from services.agent.coordination import (
            CoordinationBoard,
            clear_boards,
        )

        clear_boards()
        self.board = CoordinationBoard(
            "r_coord_test",
            persist=False,
        )

    def tearDown(self):
        from services.agent.coordination import clear_boards

        clear_boards()

    def test_claim_success(self):
        out = self.board.register_claim(
            "agent_a",
            ["doc_1", "feature_x"],
            "editing boss",
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "claimed")
        self.assertEqual(len(out["claims"]), 2)

    def test_claim_conflict_detection(self):
        self.board.register_claim("agent_a", ["doc_1"], "first claim")
        out = self.board.register_claim(
            "agent_b",
            ["doc_1", "doc_2"],
            "second claim",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "CONFLICT")
        self.assertEqual(len(out["conflicts"]), 1)
        self.assertEqual(out["conflicts"][0]["resource"], "doc_1")
        self.assertEqual(out["conflicts"][0]["holder"], "agent_a")
        self.assertEqual(out["conflicts"][0]["intent"], "first claim")
        # Conflicting claim must NOT partially grant remaining resources.
        snap = self.board.board_snapshot()
        resources = {c["resource"] for c in snap["claims"]}
        self.assertIn("doc_1", resources)
        self.assertNotIn("doc_2", resources)

    def test_release_frees_for_other_agent(self):
        self.board.register_claim("agent_a", ["doc_1"], "hold")
        rel = self.board.release_claim("agent_a", ["doc_1"])
        self.assertTrue(rel["ok"])
        self.assertEqual(rel["released"], ["doc_1"])
        out = self.board.register_claim("agent_b", ["doc_1"], "now free")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "claimed")

    def test_same_agent_reclaim_ok(self):
        self.board.register_claim("agent_a", ["doc_1"], "v1")
        out = self.board.register_claim("agent_a", ["doc_1"], "v2 refresh")
        self.assertTrue(out["ok"])
        snap = self.board.board_snapshot()
        self.assertEqual(snap["claims"][0]["intent"], "v2 refresh")

    def test_post_progress_and_snapshot(self):
        self.board.register_claim("agent_a", ["f1"], "work")
        prog = self.board.post_progress("agent_a", "sketch closed")
        self.assertTrue(prog["ok"])
        snap = self.board.board_snapshot()
        self.assertEqual(snap["claim_count"], 1)
        self.assertEqual(snap["progress_count"], 1)
        self.assertEqual(snap["progress"][0]["note"], "sketch closed")

    def test_release_all_for_agent(self):
        self.board.register_claim("agent_a", ["a", "b", "c"], "batch")
        rel = self.board.release_claim("agent_a")
        self.assertEqual(rel["count"], 3)
        self.assertEqual(self.board.board_snapshot()["claim_count"], 0)

    def test_get_board_registry(self):
        from services.agent.coordination import get_board, drop_board

        b1 = get_board("r_shared", persist=False)
        b2 = get_board("r_shared", persist=False)
        self.assertIs(b1, b2)
        b1.register_claim("x", ["r"], "i")
        self.assertEqual(b2.board_snapshot()["claim_count"], 1)
        drop_board("r_shared")

    def test_preamble_text_mentions_claims(self):
        self.board.register_claim("agent_a", ["doc_z"], "editing")
        text = self.board.preamble_text()
        self.assertIn("doc_z", text)
        self.assertIn("agent_a", text)
        self.assertIn("coordinate_claim", text)


if __name__ == "__main__":
    unittest.main()

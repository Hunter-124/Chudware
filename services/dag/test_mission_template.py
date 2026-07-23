"""Tests for services/dag/mission_template.py's ``tier`` plumbing.

Regression coverage for the node_runner provider-leak fix: a mission compiled
with ``tier=`` must record that tier on EVERY node's ``inputs`` (not just the
entry node) so ``services.dag.node_runner`` can resolve the matching provider
for internal nodes via ``services.agent.tiers.provider_for_tier`` instead of
silently falling through to the ambient config.json / ``CHUDWARE_PROVIDER``
cloud default.

Run: pytest services/dag/test_mission_template.py -q
"""
from __future__ import annotations

from dag.mission_template import build_mission_graph


def _minimal_manifest() -> dict:
    return {
        "request_verbatim": "Design a mounting bracket.",
        "ambiguities": [],
        "deliverables": [
            {
                "id": "d1",
                "name": "Bracket",
                "kind": "part",
                "description": "A simple L-bracket.",
                "acceptance_criteria": [],
            },
        ],
    }


class TestTierPlumbing:
    def test_no_tier_leaves_inputs_unchanged(self):
        """Omitting tier (the default) must not add an inputs.tier key anywhere —
        no behaviour change for callers that don't pass it."""
        graph = build_mission_graph(_minimal_manifest(), graph_id="m1")
        assert graph["nodes"], "expected at least one node"
        for node in graph["nodes"]:
            assert "tier" not in node["inputs"], node["id"]

    def test_tier_recorded_on_every_node_including_internal(self):
        """A mission compiled with tier='local-24k' must carry that tier on EVERY
        node's inputs — not just the entry node (n_intake). The whole point of
        this plumbing is that internal nodes (n_plan, n_build_*, n_verify_*,
        n_deliver_*, ...) resolve the SAME tier, so node_runner can keep them off
        the ambient cloud default."""
        graph = build_mission_graph(_minimal_manifest(), graph_id="m1",
                                    tier="local-24k")
        node_ids = {n["id"] for n in graph["nodes"]}
        # Sanity: the graph has more than just the entry node (PLAN/BUILD/VERIFY/
        # DELIVER are all "internal" relative to n_intake).
        assert node_ids >= {"n_intake", "n_plan", "n_build_d1",
                            "n_verify_d1", "n_deliver_d1"}
        for node in graph["nodes"]:
            assert node["inputs"].get("tier") == "local-24k", node["id"]

    def test_tier_does_not_clobber_other_inputs(self):
        """Adding inputs.tier must not disturb any other input already set (e.g.
        the per-deliverable request / role_prompt fields)."""
        graph = build_mission_graph(_minimal_manifest(), graph_id="m1",
                                    tier="local-24k")
        build_node = next(n for n in graph["nodes"] if n["id"] == "n_build_d1")
        assert build_node["inputs"].get("deliverable") == "d1"
        assert build_node["inputs"].get("role") == "feature_modeler"
        assert build_node["inputs"].get("tier") == "local-24k"

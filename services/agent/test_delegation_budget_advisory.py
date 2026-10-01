"""Delegation budget advisory stays actionable even for tiny positive grants."""

from __future__ import annotations

def _feature_modeler_budget_requirements():
    from services.agent.delegation import delegation_budget_requirements

    brief = "Build the ratified feature chain and verify the finished part."
    req = delegation_budget_requirements("feature_modeler", brief, tier="local-24k")
    return brief, req


def _feature_modeler_tree(run_id):
    from services.agent.delegation import DelegationTree

    return DelegationTree(
        run_id=run_id,
        orchestrator_budget=2_000_000,
        tier="local-24k",
    )


def test_absurd_positive_grant_spawns_with_budget_advisory():
    from services.agent.delegation import delegate

    brief, req = _feature_modeler_budget_requirements()
    tree = _feature_modeler_tree("budget-advisory")
    result = delegate("feature_modeler", brief, 50, tree=tree)

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert len(tree.all_nodes()) == 2
    assert result["available_tokens"] == 50
    assert result["recommended_tokens"] == req["recommended_tokens"]
    assert result["mandated_calls"] == 18
    assert "proceeding" in result["remedy"].lower()

    warnings = [
        event
        for event in tree._pending_events
        if event.get("kind") == "delegation_budget_warning"
    ]
    assert len(warnings) == 1
    payload = warnings[0]["payload"]
    assert payload["available_tokens"] == 50
    assert payload["recommended_tokens"] == req["recommended_tokens"]
    assert payload["mandated_calls"] == 18
    assert "proceeding" in payload["message"].lower()



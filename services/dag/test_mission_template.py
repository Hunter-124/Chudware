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

from services.dag.mission_template import build_mission_graph


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


def _manifest_for(kind: str, name: str) -> dict:
    return {
        "request_verbatim": f"Create {name}.",
        "ambiguities": [],
        "deliverables": [{
            "id": "d1", "name": name, "kind": kind,
            "description": name, "acceptance_criteria": [],
        }],
    }


def test_drawing_gets_specialist_build_and_reviewer_delivery():
    graph = build_mission_graph(_manifest_for("drawing", "Dimensioned drawing"))
    roles = {node["id"]: node["inputs"].get("role") for node in graph["nodes"]}
    assert roles["n_build_d1"] == "drawing_detailer"
    assert roles["n_verify_d1"] == "reviewer"
    assert roles["n_deliver_d1"] == "reviewer"


def test_ecad_analysis_does_not_fall_back_to_sim_engineer_for_delivery():
    graph = build_mission_graph(_manifest_for("analysis", "PCB schematic and board"))
    roles = {node["id"]: node["inputs"].get("role") for node in graph["nodes"]}
    assert roles["n_build_d1"] == "ecad_engineer"
    assert roles["n_deliver_d1"] == "reviewer"



def test_report_is_built_and_delivered_by_report_capable_role():
    graph = build_mission_graph(_manifest_for("report", "Inspection report"))
    by_id = {node["id"]: node for node in graph["nodes"]}

    assert by_id["n_build_d1"]["inputs"]["role"] == "general_agent"
    assert "make_report" in by_id["n_build_d1"]["inputs"]["request"]
    assert by_id["n_deliver_d1"]["inputs"]["role"] == "general_agent"


def test_multi_part_manufacturing_package_expands_artifacts_and_complexity():
    manifest = {
        "request_verbatim": (
            "Create a complete manufacturing package for this multi-part welded press."
        ),
        "ambiguities": [],
        "deliverables": [
            {
                "id": "frame",
                "name": "Welded frame",
                "kind": "part",
                "description": "Fabricated weldment frame.",
                "acceptance_criteria": [],
            },
            {
                "id": "ram",
                "name": "Ram",
                "kind": "part",
                "description": "Machined ram.",
                "acceptance_criteria": [],
            },
            {
                "id": "press",
                "name": "Press assembly",
                "kind": "assembly",
                "description": "Assembly of the welded frame and ram.",
                "acceptance_criteria": [],
                "depends_on": ["frame", "ram"],
            },
            {
                "id": "package",
                "name": "Manufacturing package",
                "kind": "report",
                "description": "All controlled shop documentation.",
                "acceptance_criteria": [],
                "depends_on": ["press"],
            },
        ],
    }

    graph = build_mission_graph(manifest)
    by_id = {node["id"]: node for node in graph["nodes"]}

    expected = {
        "n_build_frame_drawing",
        "n_build_ram_drawing",
        "n_build_manufacturing_assembly_drawing",
        "n_build_manufacturing_bom",
        "n_build_manufacturing_work_instructions",
        "n_build_manufacturing_weldment_traveler",
    }
    assert expected <= set(by_id)
    assert "n_deliver_package" not in by_id
    assert (
        by_id["n_build_manufacturing_assembly_drawing"]["inputs"]["task_class"]
        != "simple_part"
    )
    assert by_id["n_build_manufacturing_bom"]["inputs"]["role"] == "general_agent"
    assert (
        by_id["n_build_manufacturing_work_instructions"]["inputs"]["role"]
        == "general_agent"
    )
    edges = {(edge["from"], edge["to"]) for edge in graph["edges"]}
    assert ("n_build_frame", "n_build_frame_drawing") in edges
    assert (
        "n_build_manufacturing_assembly_drawing",
        "n_build_manufacturing_work_instructions",
    ) in edges
    assert (
        "n_build_manufacturing_bom",
        "n_build_manufacturing_work_instructions",
    ) in edges

"""planner.py — Build-order DAG planner for aicad.buildgraph/1.

Contract: contracts/advisor.md §13 (FROZEN), §3.
T-W15.

This planner is LLM-FREE.  Ambiguity in a plan becomes an aicad.advisory/1
finding, never a guess.

Two input shapes are accepted:

  1. loop.py BUILD PLAN JSON (from PLAN_PROMPT) — keys: name, parts[], fit
     Each part has: name, purpose, dimensions, features[]
     Each feature is a plain-text string.

  2. dag_plan mission spec — keys: schema="aicad.dag/1", id, name, nodes[], edges[]
     Each node may have type="design" with inputs.request carrying a build description.

Public API
----------
  build_graph(plan, mission="") -> (graph_dict, advisory_list)

  graph_dict conforms to aicad.buildgraph/1.
  advisory_list is a list of aicad.advisory/1 items (possibly empty).
"""
from __future__ import annotations

import uuid
from typing import Any

from .rules import RULES, _has_any
from .schema import validate_graph, BuildGraphCycleError, _advisory


# ---------------------------------------------------------------------------
# Node id generation
# ---------------------------------------------------------------------------

def _bgid(prefix: str = "bg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Input shape detection
# ---------------------------------------------------------------------------

def _is_build_plan(plan: dict) -> bool:
    """True if plan looks like a loop.py BUILD PLAN (has 'parts' key)."""
    return isinstance(plan.get("parts"), list)


def _is_dag_plan(plan: dict) -> bool:
    """True if plan looks like an aicad.dag/1 mission graph."""
    return plan.get("schema") == "aicad.dag/1"


# ---------------------------------------------------------------------------
# Build-plan → buildgraph
# ---------------------------------------------------------------------------

def _nodes_from_build_plan(
    plan: dict,
    advisories: list[dict],
) -> list[dict]:
    """Convert loop.py BUILD PLAN JSON into aicad.buildgraph/1 nodes.

    Each part becomes a chain:
      part_start (kind=part) → features (kind=feature) → part_done (kind=checkpoint)

    Feature ordering is determined by prerequisite rules.  Ambiguous orderings
    emit advisory findings rather than guessing.
    """
    nodes: list[dict] = []
    part_done_ids: list[str] = []

    for part in plan.get("parts", []):
        part_name: str = part.get("name", "unknown_part")
        part_id = _bgid("bg")

        # Part root node
        part_node: dict = {
            "id": part_id,
            "kind": "part",
            "label": part_name,
            "doc_id": "",
            "refs": {"features": [], "parts": []},
            "requires": [],
            "why": f"part root for {part_name!r}",
            "status": "pending",
            "assigned": None,
        }
        nodes.append(part_node)

        # Feature nodes
        features: list[str] = part.get("features", [])
        feature_nodes: list[dict] = []
        feat_label_to_id: dict[str, str] = {}

        for feat_text in features:
            feat_id = _bgid("bg")
            feat_node: dict = {
                "id": feat_id,
                "kind": "feature",
                "label": feat_text if len(feat_text) <= 80 else feat_text[:77] + "...",
                "doc_id": "",
                "refs": {"features": [], "parts": []},
                "requires": [part_id],   # every feature requires the part root
                "why": f"part {part_name!r} must be initialised before this feature",
                "status": "pending",
                "assigned": None,
            }
            feature_nodes.append(feat_node)
            feat_label_to_id[feat_text] = feat_id

        # Apply prerequisite rules to infer additional edges
        why_parts: dict[str, list[str]] = {}  # feat_id -> list of why strings
        for feat_text, feat_id in feat_label_to_id.items():
            feat_node = next(fn for fn in feature_nodes if fn["id"] == feat_id)
            why_parts[feat_id] = list(feat_node.get("why", "").split("; "))

            for rule in RULES:
                try:
                    fires = rule["matcher"](feat_text, plan)
                except Exception:
                    fires = False
                if not fires:
                    continue

                try:
                    required_labels = rule["requires"](feat_text, plan)
                except Exception:
                    required_labels = []

                if not required_labels:
                    # Rule fired but no concrete requires found — ambiguity advisory
                    advisories.append(_advisory(
                        "prerequisite_ambiguous",
                        (
                            f"Rule {rule['id']!r} matched feature {feat_text!r} in part "
                            f"{part_name!r} but could not resolve a concrete prerequisite. "
                            f"Rationale: {rule['why']}. State the prerequisite explicitly."
                        ),
                        severity="hint",
                        refs={"rule_id": rule["id"], "feature": feat_text, "part": part_name},
                    ))
                    continue

                for req_label in required_labels:
                    # Detect sentinel labels (returned when a rule fires but the
                    # referenced item is absent from the plan).  Sentinel labels
                    # contain "(referenced but not found in plan)" — do NOT fuzzy-
                    # match them, always emit a dangling blocker.
                    is_sentinel = "referenced but not found" in req_label

                    req_id = None if is_sentinel else feat_label_to_id.get(req_label)
                    if req_id is None and not is_sentinel:
                        # Could be a partial match — try substring
                        req_id = _fuzzy_match(req_label, feat_label_to_id)
                    if req_id is None:
                        advisories.append(_advisory(
                            "dangling_requires",
                            (
                                f"Feature {feat_text!r} in part {part_name!r} requires "
                                f"{req_label!r} (rule {rule['id']!r}: {rule['why']}) but no "
                                "matching node was found in the graph. Add the missing "
                                "prerequisite feature or remove the reference."
                            ),
                            severity="blocker",
                            refs={"feature": feat_text, "missing": req_label,
                                  "rule_id": rule["id"]},
                        ))
                        continue

                    if req_id not in feat_node["requires"] and req_id != feat_id:
                        feat_node["requires"].append(req_id)
                        why_parts[feat_id].append(rule["why"])

            # Deduplicate why
            seen_why: set[str] = set()
            unique_why: list[str] = []
            for w in why_parts.get(feat_id, []):
                if w and w not in seen_why:
                    seen_why.add(w)
                    unique_why.append(w)
            feat_node["why"] = "; ".join(unique_why) if unique_why else "part root must exist first"

        nodes.extend(feature_nodes)

        # Shell ambiguity advisory: shell + cuts with no ordering
        _check_shell_ambiguity_in_features(
            part_name, feature_nodes, feat_label_to_id, advisories
        )

        # Checkpoint node for this part (part_done)
        part_done_id = _bgid("bg")
        all_feat_ids = [fn["id"] for fn in feature_nodes]
        checkpoint_node: dict = {
            "id": part_done_id,
            "kind": "checkpoint",
            "label": f"{part_name}_done",
            "doc_id": "",
            "refs": {"features": [], "parts": [part_name]},
            "requires": all_feat_ids or [part_id],
            "why": f"PLM checkpoint: all features of {part_name!r} complete",
            "status": "pending",
            "assigned": None,
        }
        nodes.append(checkpoint_node)
        part_done_ids.append(part_done_id)

    # Mission-done checkpoint joins all part_done nodes
    if len(part_done_ids) > 1:
        mission_done_id = _bgid("bg")
        nodes.append({
            "id": mission_done_id,
            "kind": "checkpoint",
            "label": "mission_done",
            "doc_id": "",
            "refs": {"features": [], "parts": []},
            "requires": part_done_ids,
            "why": "PLM milestone: all parts complete — assembly can proceed",
            "status": "pending",
            "assigned": None,
        })

    return nodes


def _check_shell_ambiguity_in_features(
    part_name: str,
    feature_nodes: list[dict],
    feat_label_to_id: dict[str, str],
    advisories: list[dict],
) -> None:
    shell_nodes = [fn for fn in feature_nodes
                   if _has_any(fn.get("label", ""), "shell")]
    cut_nodes = [fn for fn in feature_nodes
                 if _has_any(fn.get("label", ""), "hole", "bore", "cut", "pocket")]
    if not shell_nodes or not cut_nodes:
        return
    for sn in shell_nodes:
        shell_reqs = set(sn.get("requires", []))
        cut_ids = {cn["id"] for cn in cut_nodes}
        cut_reqs_shell = any(sn["id"] in cn.get("requires", []) for cn in cut_nodes)
        if not (shell_reqs & cut_ids) and not cut_reqs_shell:
            advisories.append(_advisory(
                "shell_order_ambiguous",
                (
                    f"Part {part_name!r}: shell feature {sn.get('label', '')!r} and "
                    f"cut/hole features {[cn.get('label', '') for cn in cut_nodes]!r} "
                    "have no explicit ordering. State whether shell runs before or after "
                    "cuts/holes (usually after, but may vary if cuts are through-shell)."
                ),
                severity="action",
                refs={"part": part_name, "shell_node": sn["id"],
                      "cut_nodes": [cn["id"] for cn in cut_nodes]},
            ))


# ---------------------------------------------------------------------------
# DAG plan mission → buildgraph
# ---------------------------------------------------------------------------

def _nodes_from_dag_plan(
    plan: dict,
    advisories: list[dict],
) -> list[dict]:
    """Convert an aicad.dag/1 mission graph into aicad.buildgraph/1 nodes.

    Each aicad.dag/1 node becomes one buildgraph node.  Edges from the dag/1
    graph are converted to requires[] entries on the successor node.
    """
    dag_nodes: list[dict] = plan.get("nodes", [])
    dag_edges: list[dict] = plan.get("edges", [])

    # Build successor map from edges
    requires_map: dict[str, list[str]] = {}
    for edge in dag_edges:
        to_id = edge.get("to", "")
        from_id = edge.get("from", "")
        if to_id and from_id:
            requires_map.setdefault(to_id, []).append(from_id)

    nodes: list[dict] = []
    for dag_node in dag_nodes:
        dag_id = dag_node.get("id", _bgid("bg"))
        node_type = dag_node.get("type", "design")
        bg_kind = _dag_type_to_bg_kind(node_type)
        bg_node: dict = {
            "id": f"bg_{dag_id}",
            "kind": bg_kind,
            "label": dag_node.get("name", dag_id),
            "doc_id": dag_node.get("inputs", {}).get("doc_id", ""),
            "refs": {"features": [], "parts": []},
            "requires": [f"bg_{r}" for r in requires_map.get(dag_id, [])],
            "why": f"dag/1 edge: {dag_id!r} depends on predecessors per mission graph",
            "status": "pending",
            "assigned": None,
        }
        nodes.append(bg_node)

    # Emit advisory for verify nodes without requires
    for node in nodes:
        if node["kind"] in ("verify", "checkpoint") and not node["requires"]:
            advisories.append(_advisory(
                "missing_join",
                (
                    f"Node {node['id']!r} has kind={node['kind']!r} but no predecessors "
                    "in the dag/1 graph. A verify/checkpoint node should join upstream work."
                ),
                severity="action",
                refs={"node_id": node["id"]},
            ))

    return nodes


def _dag_type_to_bg_kind(dag_type: str) -> str:
    mapping = {
        "plan": "op",
        "design": "feature",
        "ecad": "feature",
        "firmware": "feature",
        "structural": "op",
        "simulate": "op",
        "source": "op",
        "verify": "verify",
        "gate": "verify",
        "report": "checkpoint",
    }
    return mapping.get(dag_type, "op")


# ---------------------------------------------------------------------------
# Fuzzy label matching
# ---------------------------------------------------------------------------

def _fuzzy_match(label: str, label_to_id: dict[str, str]) -> str | None:
    """Case-insensitive substring match of label against known feature labels."""
    label_lower = label.lower().strip()
    for feat_label, feat_id in label_to_id.items():
        if label_lower in feat_label.lower() or feat_label.lower() in label_lower:
            return feat_id
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_graph(
    plan: dict,
    mission: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a BUILD PLAN or dag/1 mission spec into an aicad.buildgraph/1 graph.

    Args:
        plan:    Either a loop.py BUILD PLAN dict (has 'parts' key) or an
                 aicad.dag/1 graph dict (schema: 'aicad.dag/1').
        mission: Optional human label for the graph.

    Returns:
        (graph_dict, advisory_list)
        graph_dict conforms to aicad.buildgraph/1.
        advisory_list is a list of aicad.advisory/1 items; may be empty.
        Raises BuildGraphCycleError if the resulting graph has a cycle.
    """
    advisories: list[dict[str, Any]] = []

    if _is_build_plan(plan):
        nodes = _nodes_from_build_plan(plan, advisories)
        name = plan.get("name", mission or "build_plan")
    elif _is_dag_plan(plan):
        nodes = _nodes_from_dag_plan(plan, advisories)
        name = plan.get("name", mission or "dag_mission")
    else:
        advisories.append(_advisory(
            "unknown_plan_shape",
            "Plan dict has neither 'parts' (BUILD PLAN) nor schema='aicad.dag/1'. "
            "Cannot infer structure — returning empty graph.",
            severity="blocker",
        ))
        nodes = []
        name = mission or "unknown"

    graph: dict[str, Any] = {
        "schema": "aicad.buildgraph/1",
        "mission": mission,
        "nodes": nodes,
    }

    # Validator findings are merged into advisories (they are advisory, not hard-stop,
    # except cycles which raise).
    try:
        validator_findings = validate_graph(graph)
    except BuildGraphCycleError:
        raise  # always a structural blocker — propagate
    advisories.extend(validator_findings)

    return graph, advisories

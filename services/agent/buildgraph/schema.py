"""schema.py — Validator for aicad.buildgraph/1 graphs.

Contract: contracts/advisor.md §13 (FROZEN — graph shape), §3 (advisory items).

validate_graph(graph) returns a list of aicad.advisory/1 items.
An empty list means validation passed cleanly.
The caller decides whether to proceed (findings are advisory, not hard-stop) —
except for CYCLES, which are always structural blockers.
"""
from __future__ import annotations

import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Exported exception (structurally invalid graph — cycles)
# ---------------------------------------------------------------------------

class BuildGraphCycleError(Exception):
    """Raised when the graph contains a dependency cycle."""


# ---------------------------------------------------------------------------
# Advisory item builders (aicad.advisory/1 shape per §3)
# ---------------------------------------------------------------------------

_SEQ_COUNTER = 0


def _next_seq() -> int:
    global _SEQ_COUNTER
    _SEQ_COUNTER += 1
    return _SEQ_COUNTER


def _advisory(
    check: str,
    finding: str,
    severity: str = "action",
    refs: dict | None = None,
) -> dict[str, Any]:
    """Build a minimal aicad.advisory/1 item."""
    return {
        "schema": "aicad.advisory/1",
        "seq": _next_seq(),
        "stage": "build_plan",
        "items": [{
            "id": f"adv_{uuid.uuid4().hex[:8]}",
            "severity": severity,
            "check": check,
            "finding": finding,
            "refs": refs or {},
            "numbers": {},
            "candidate_fixes": [],
            "helper_tools": [],
            "source": {"kind": "calc", "detail": "buildgraph_validator"},
        }],
    }


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------

def validate_graph(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate an aicad.buildgraph/1 graph deterministically.

    Checks performed:
      1. Schema string matches "aicad.buildgraph/1".
      2. All node ids are unique.
      3. Every requires[] entry references a known node id.
      4. No cycles (Kahn topological sort).
      5. No orphan nodes (nodes unreachable from any root or all reachable).
      6. Missing join: verify/checkpoint nodes must have >= 1 requires.
      7. Shell ambiguity: shell nodes in the same part as cut/hole nodes
         without an explicit requires relationship to them.

    Returns:
        List of aicad.advisory/1 item dicts.  Empty → clean.
        Raises BuildGraphCycleError if cycles are detected (structural blocker).
    """
    findings: list[dict[str, Any]] = []

    # 1. Schema check
    if graph.get("schema") != "aicad.buildgraph/1":
        findings.append(_advisory(
            "schema_mismatch",
            f"Graph schema is {graph.get('schema')!r}; expected 'aicad.buildgraph/1'",
            severity="blocker",
        ))

    nodes: list[dict] = graph.get("nodes", [])
    if not nodes:
        findings.append(_advisory(
            "empty_graph",
            "Build graph has no nodes; nothing to validate.",
            severity="hint",
        ))
        return findings

    # 2. Unique node ids
    node_ids: set[str] = set()
    duplicate_ids: list[str] = []
    for node in nodes:
        nid = node.get("id", "")
        if nid in node_ids:
            duplicate_ids.append(nid)
        node_ids.add(nid)
    for dup in duplicate_ids:
        findings.append(_advisory(
            "duplicate_node_id",
            f"Node id {dup!r} appears more than once in the graph.",
            severity="blocker",
            refs={"node_id": dup},
        ))

    # 3. Dangling requires (references to unknown node ids)
    for node in nodes:
        nid = node.get("id", "")
        for req in node.get("requires", []):
            if req not in node_ids:
                findings.append(_advisory(
                    "dangling_requires",
                    (
                        f"Node {nid!r} requires {req!r} but that node is not "
                        "in the graph. Add the missing node or remove the requires."
                    ),
                    severity="blocker",
                    refs={"node_id": nid, "missing_requires": req},
                ))

    # 4. Cycle detection (Kahn's algorithm on requires edges)
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    prereq_map: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for node in nodes:
        nid = node.get("id", "")
        for req in node.get("requires", []):
            if req in node_ids:  # skip dangling (already reported)
                prereq_map[req].append(nid)
                in_degree[nid] += 1

    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        curr = queue.pop(0)
        visited += 1
        for successor in prereq_map.get(curr, []):
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if visited < len(node_ids):
        # Find the nodes in the cycle
        cycle_nodes = [nid for nid, deg in in_degree.items() if deg > 0]
        raise BuildGraphCycleError(
            f"Dependency cycle detected among nodes: {cycle_nodes}. "
            "Break the cycle by removing a requires edge."
        )

    # 5. Orphan detection: nodes that have no successors AND no predecessors
    #    (isolated nodes in a non-trivial graph).
    has_requires: set[str] = set()
    is_required_by: set[str] = set()
    for node in nodes:
        nid = node.get("id", "")
        for req in node.get("requires", []):
            if req in node_ids:
                has_requires.add(nid)
                is_required_by.add(req)

    if len(nodes) > 1:
        for node in nodes:
            nid = node.get("id", "")
            if nid not in has_requires and nid not in is_required_by:
                findings.append(_advisory(
                    "orphan_node",
                    (
                        f"Node {nid!r} ({node.get('label', '')!r}) has no requires "
                        "and nothing requires it. It will run in isolation."
                    ),
                    severity="hint",
                    refs={"node_id": nid},
                ))

    # 6. verify/checkpoint must have at least one requires
    for node in nodes:
        nid = node.get("id", "")
        if node.get("kind") in ("verify", "checkpoint"):
            if not node.get("requires"):
                findings.append(_advisory(
                    "missing_join",
                    (
                        f"Node {nid!r} has kind={node.get('kind')!r} but no requires[]. "
                        "A verify/checkpoint node must join at least one upstream node."
                    ),
                    severity="action",
                    refs={"node_id": nid},
                ))

    # 7. Shell ambiguity — shell node in a part that also has hole/cut nodes
    #    with no explicit ordering between them
    _check_shell_ambiguity(nodes, node_ids, findings)

    return findings


def _check_shell_ambiguity(
    nodes: list[dict],
    node_ids: set[str],
    findings: list[dict],
) -> None:
    """Emit advisory when shell and cut/hole nodes coexist without explicit ordering."""
    # Group nodes by doc_id (part)
    by_part: dict[str, list[dict]] = {}
    for node in nodes:
        doc = node.get("doc_id") or "__global__"
        by_part.setdefault(doc, []).append(node)

    for doc_id, part_nodes in by_part.items():
        shell_nodes = [n for n in part_nodes if n.get("kind") == "feature"
                       and _label_has(n, "shell")]
        cut_nodes = [n for n in part_nodes if n.get("kind") == "feature"
                     and _label_has_any(n, "hole", "bore", "cut", "pocket")]
        if not shell_nodes or not cut_nodes:
            continue
        for sn in shell_nodes:
            # Check if shell requires any cut or vice versa
            shell_reqs = set(sn.get("requires", []))
            cut_ids = {cn["id"] for cn in cut_nodes}
            # Check if any cut requires the shell
            shell_id = sn["id"]
            cut_reqs_shell = any(
                shell_id in cn.get("requires", []) for cn in cut_nodes
            )
            if not (shell_reqs & cut_ids) and not cut_reqs_shell:
                findings.append(_advisory(
                    "shell_order_ambiguous",
                    (
                        f"Node {sn['id']!r} ({sn.get('label', '')!r}) is a shell and "
                        f"coexists with cut/hole nodes {[cn['id'] for cn in cut_nodes]!r} "
                        "in part {doc_id!r} with no explicit ordering. "
                        "State whether shell runs before or after cuts/holes."
                    ),
                    severity="action",
                    refs={"node_id": sn["id"], "doc_id": doc_id,
                          "cut_nodes": [cn["id"] for cn in cut_nodes]},
                ))


def _label_has(node: dict, keyword: str) -> bool:
    label = (node.get("label") or "").lower()
    return keyword.lower() in label


def _label_has_any(node: dict, *keywords: str) -> bool:
    return any(_label_has(node, kw) for kw in keywords)

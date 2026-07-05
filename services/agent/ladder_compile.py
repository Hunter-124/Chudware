"""ladder_compile.py — compile a rung-D build_pathway into aicad.buildgraph/1 (T-W25).

Contract: contracts/advisor.md §22 rung D ("compiles losslessly into
aicad.buildgraph/1 (steps → nodes, why → edge whys)") + §13 (buildgraph shape,
FROZEN — T-W15). The compile is LLM-free and deterministic.

LOSSLESS means the FULL step is reconstructable from the graph, not just op+why.
A build_pathway step is::

    {n, op, elements: [G ids], sketch_on, orientation, why, risks: []}

and the buildgraph node is (T-W15 planner shape)::

    {id, kind, label, doc_id, refs:{features,parts}, requires, why, status, assigned}

Not every step field has a native node home, so the compiler stashes the
step's structural fields under ``node["ladder"]`` (an additive namespace that the
T-W15 validator ignores — it only reads id/kind/requires/label/why/doc_id). The
``why`` ALSO rides on ``node["why"]`` so §13 consumers (buildgraph_status,
executor) and the "why → edge whys" requirement both see it: an edge's rationale
lives on the dependent (successor) node's ``why`` string, exactly as the T-W15
planner encodes edge whys.

``decompile_graph`` inverts the mapping and reconstructs the full build_pathway,
proving losslessness in the round-trip test.
"""
from __future__ import annotations

import uuid
from typing import Any

BUILDGRAPH_SCHEMA = "aicad.buildgraph/1"


def _node_id(n: int) -> str:
    """Deterministic-per-run node id (mirrors planner's bg_<hex> style but stable
    within one compile so requires-edges resolve)."""
    return f"bg_ladder_{n}"


def compile_pathway(build_pathway: dict[str, Any], *, mission: str = "",
                    doc_id: str = "") -> dict[str, Any]:
    """Compile a build_pathway artifact into an aicad.buildgraph/1 graph.

    steps → nodes (in n order); each node ``requires`` the immediately-preceding
    step's node (the sequence the pathway committed to); each step's ``why`` becomes
    the node's ``why`` (edge-why on the successor). The full step is preserved under
    ``node["ladder"]`` so the graph round-trips losslessly.
    """
    steps = sorted(build_pathway.get("steps") or [], key=lambda s: s.get("n", 0))
    nodes: list[dict[str, Any]] = []
    prev_id: str | None = None
    for s in steps:
        nid = _node_id(int(s.get("n", len(nodes) + 1)))
        elements = list(s.get("elements", []) or [])
        op = str(s.get("op", ""))
        label = f"{op}: {', '.join(elements)}" if elements else op
        requires = [prev_id] if prev_id else []
        node = {
            "id": nid,
            "kind": "feature",
            "label": label[:80],
            "doc_id": doc_id,
            "refs": {"features": elements, "parts": []},
            "requires": requires,
            "why": str(s.get("why", "")),
            "status": "pending",
            "assigned": None,
            # additive, lossless preservation of the full step (§13 validator ignores it)
            "ladder": {
                "n": s.get("n"),
                "op": op,
                "elements": elements,
                "sketch_on": s.get("sketch_on", ""),
                "orientation": s.get("orientation", ""),
                "risks": list(s.get("risks", []) or []),
            },
        }
        nodes.append(node)
        prev_id = nid

    graph: dict[str, Any] = {
        "schema": BUILDGRAPH_SCHEMA,
        "mission": mission,
        "nodes": nodes,
        # additive: pathway-level context the steps don't carry (round-trips)
        "ladder_meta": {
            "orientation_rationale": build_pathway.get("orientation_rationale", ""),
            "alternates_considered": build_pathway.get("alternates_considered", []),
            "defects": build_pathway.get("defects", []),
        },
    }
    return graph


def decompile_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the full build_pathway from a compiled graph — the lossless inverse.

    Reads each node's ``ladder`` namespace (or falls back to the native fields when a
    node was produced by the generic T-W15 planner rather than this compiler)."""
    steps: list[dict[str, Any]] = []
    for node in graph.get("nodes", []):
        lad = node.get("ladder")
        if lad is not None:
            steps.append({
                "n": lad.get("n"),
                "op": lad.get("op", ""),
                "elements": list(lad.get("elements", []) or []),
                "sketch_on": lad.get("sketch_on", ""),
                "orientation": lad.get("orientation", ""),
                "why": node.get("why", ""),
                "risks": list(lad.get("risks", []) or []),
            })
        else:  # generic node — best-effort reconstruction
            steps.append({
                "n": len(steps) + 1,
                "op": (node.get("label") or "").split(":")[0].strip(),
                "elements": list(node.get("refs", {}).get("features", []) or []),
                "sketch_on": "",
                "orientation": "",
                "why": node.get("why", ""),
                "risks": [],
            })
    steps.sort(key=lambda s: s.get("n") or 0)
    meta = graph.get("ladder_meta", {})
    return {
        "steps": steps,
        "orientation_rationale": meta.get("orientation_rationale", ""),
        "alternates_considered": meta.get("alternates_considered", []),
        "defects": meta.get("defects", []),
    }

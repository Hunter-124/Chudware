"""MCP tools: cad_graph / cad_graph_link / assembly_plan_review (1D).

Lets an external model TRAVERSE the deterministic CAD project graph
(services.plm.cad_graph) and PLAN assemblies with best practices — deep products
built as subassemblies (each its own .step+) mated into the parent, not one
monolithic file. Also exposes AI-authored link overlay (origin="ai").

Every tool returns the flat envelope: ``{ok:true, …}`` / ``{ok:false, reason, hint}``.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# token guard — cap the node/edge payload so a huge project can't blow the context.
_MAX_NODES = 400
_MAX_EDGES = 400


def _err(reason: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "reason": reason, "hint": hint})


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, default=str)


def register_graph_tools(mcp: FastMCP) -> None:
    """Register the CAD-graph traversal + planning tools onto *mcp*."""

    @mcp.tool()
    def cad_graph(query_kind: str = "get", node_id: str = "",
                  src: str = "", dst: str = "", project_dir: str = "") -> str:
        """Traverse the CAD project graph — dimensional links + assembly structure.

        query_kind:
          - "get"   → the whole graph (nodes + edges), token-bounded.
          - "blast" → transitive dependents of `node_id` (what breaks if it changes).
          - "path"  → shortest link chain between `src` and `dst`.
        `project_dir` is the project folder (the shared working directory). Edges
        carry meta (dim/value/expr/via) explaining WHAT links two parts.
        """
        try:
            from services.plm import cad_graph as cg
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        try:
            qk = (query_kind or "get").strip().lower()
            if qk == "get":
                g = cg.get_graph(project_dir or None)
                nodes = g.get("nodes") or []
                edges = g.get("edges") or []
                truncated = len(nodes) > _MAX_NODES or len(edges) > _MAX_EDGES
                return _dumps({
                    "ok": True,
                    "nodes": nodes[:_MAX_NODES],
                    "edges": edges[:_MAX_EDGES],
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "truncated": truncated,
                    "generated_at": g.get("generated_at"),
                })
            if qk == "blast":
                if not node_id:
                    return _err("node_id required", "pass the graph node id to blast from")
                return _dumps(cg.blast_radius(node_id, project_dir=project_dir or None))
            if qk == "path":
                if not src or not dst:
                    return _err("src_dst_required", "pass both graph node ids")
                return _dumps(cg.find_path(src, dst, project_dir=project_dir or None))
            return _err("invalid_query_kind", "query_kind ∈ get | blast | path")
        except Exception as exc:  # noqa: BLE001
            return _err("cad_graph_failed", str(exc))

    @mcp.tool()
    def cad_graph_link(src: str, dst: str, kind: str, note: str = "",
                       project_dir: str = "") -> str:
        """Author an AI link between two graph nodes (origin="ai").

        Use to record a design relationship the deterministic extractor can't see
        (e.g. two parts that must share a bore diameter). `kind` is a graph edge
        kind (REFERENCES, DERIVES_FROM, MATES, …). Persisted in the project overlay.
        """
        try:
            from services.plm import graph_links as gl
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"graph_links module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        try:
            return _dumps(gl.add_link(project_dir, src, dst, kind,
                                      note=note, origin="ai"))
        except Exception as exc:  # noqa: BLE001
            return _err("cad_graph_link_failed", str(exc))

    @mcp.tool()
    def assembly_plan_review(project_dir: str = "", doc_id: str = "") -> str:
        """Best-practice review of an assembly's structure BEFORE a large build.

        Returns suggestions: extract tightly-mated component clusters into
        subassemblies, group over-flat assemblies, split multi-body parts. Build
        deep products as subassemblies (each its own .step+) mated into the parent
        — never one giant file.
        """
        try:
            from services.plm import cad_graph as cg
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        try:
            g = cg.build_graph(project_dir or None)
            out = cg.assembly_advice(g)
            return _dumps(out)
        except Exception as exc:  # noqa: BLE001
            return _err("assembly_plan_review_failed", str(exc))

"""Worker-RPC bridge for the CAD project graph (contracts/cad_graph.md v1).

Exposes ``cad_graph_get`` / ``cad_graph_blast`` / ``cad_graph_path`` over the
engine-worker seam. Thin wrappers over :mod:`services.plm.cad_graph`.
GUI degrades to hidden when an RPC reports ``unknown_method``.
"""
from __future__ import annotations

from typing import Any, Callable


def _cad_graph_get(params: dict, _emit: Callable) -> dict:
    """``cad_graph_get {project_dir?|doc_id?} -> {ok, nodes, edges, generated_at}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or None
        doc_id = p.get("doc_id") or None
        force = bool(p.get("force", False))
        open_docs = p.get("open_docs") if isinstance(p.get("open_docs"), dict) else None
        return cg.get_graph(
            project_dir, doc_id=doc_id, open_docs=open_docs, force=force,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_get failed: {exc}", "hint": ""}


def _cad_graph_blast(params: dict, _emit: Callable) -> dict:
    """``cad_graph_blast {node_id, project_dir?} -> {ok, affected, edges}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        node_id = str(p.get("node_id") or "").strip()
        if not node_id:
            return {"ok": False, "reason": "node_id required",
                    "hint": "pass the graph node id to blast from",
                    "affected": [], "edges": []}
        project_dir = p.get("project_dir") or p.get("path") or None
        force = bool(p.get("force", False))
        graph = None
        if project_dir:
            graph = cg.build_graph(project_dir, force=force)
        return cg.blast_radius(node_id, graph, project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_blast failed: {exc}",
                "hint": "", "affected": [], "edges": []}


def _cad_graph_path(params: dict, _emit: Callable) -> dict:
    """``cad_graph_path {src, dst, project_dir?} -> {ok, path}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        src = str(p.get("src") or "").strip()
        dst = str(p.get("dst") or "").strip()
        if not src or not dst:
            return {"ok": False, "reason": "src and dst required",
                    "hint": "pass graph node ids for both ends", "path": []}
        project_dir = p.get("project_dir") or p.get("path") or None
        force = bool(p.get("force", False))
        graph = None
        if project_dir:
            graph = cg.build_graph(project_dir, force=force)
        return cg.find_path(src, dst, graph, project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_path failed: {exc}",
                "hint": "", "path": []}


CAD_GRAPH_METHODS: dict[str, Callable[..., Any]] = {
    "cad_graph_get": _cad_graph_get,
    "cad_graph_blast": _cad_graph_blast,
    "cad_graph_path": _cad_graph_path,
}

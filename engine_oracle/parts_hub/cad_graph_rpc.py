"""Worker-RPC bridge for the CAD project graph (contracts/cad_graph.md v1).

Exposes ``cad_graph_get`` / ``cad_graph_blast`` / ``cad_graph_path`` over the
engine-worker seam. Thin wrappers over :mod:`services.plm.cad_graph`.
GUI degrades to hidden when an RPC reports ``unknown_method``.
"""
from __future__ import annotations

from typing import Any, Callable


def _cad_graph_get(params: dict, _emit: Callable) -> dict:
    """``cad_graph_get {project_dir?|doc_id?} -> {ok, nodes, edges, generated_at, …}``.

    Additive optional params (contracts/cad_graph.md addenda):
      force, include_archive, max_nodes, max_edges, open_docs.
    Response always includes node_count/edge_count/truncated/filter.
    """
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or None
        doc_id = p.get("doc_id") or None
        force = bool(p.get("force", False))
        include_archive = bool(p.get("include_archive", False))
        open_docs = p.get("open_docs") if isinstance(p.get("open_docs"), dict) else None
        max_nodes = p.get("max_nodes", None)
        max_edges = p.get("max_edges", None)
        if max_nodes is not None:
            try:
                max_nodes = int(max_nodes)
            except (TypeError, ValueError):
                max_nodes = None
        if max_edges is not None:
            try:
                max_edges = int(max_edges)
            except (TypeError, ValueError):
                max_edges = None
        return cg.get_graph(
            project_dir, doc_id=doc_id, open_docs=open_docs, force=force,
            include_archive=include_archive,
            max_nodes=max_nodes, max_edges=max_edges,
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


def _cad_graph_link_add(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_add {project_dir, src, dst, kind, note?, origin?}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return gl.add_link(
            project_dir,
            str(p.get("src") or ""), str(p.get("dst") or ""),
            str(p.get("kind") or ""),
            note=str(p.get("note") or ""),
            origin=str(p.get("origin") or "manual"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_add failed: {exc}", "hint": ""}


def _cad_graph_link_remove(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_remove {project_dir, link_id}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return gl.remove_link(project_dir, str(p.get("link_id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_remove failed: {exc}", "hint": ""}


def _cad_graph_link_list(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_list {project_dir} -> {ok, links}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": "", "links": []}
        return {"ok": True, "links": gl.list_links(project_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_list failed: {exc}",
                "hint": "", "links": []}


CAD_GRAPH_METHODS: dict[str, Callable[..., Any]] = {
    "cad_graph_get": _cad_graph_get,
    "cad_graph_blast": _cad_graph_blast,
    "cad_graph_path": _cad_graph_path,
    "cad_graph_link_add": _cad_graph_link_add,
    "cad_graph_link_remove": _cad_graph_link_remove,
    "cad_graph_link_list": _cad_graph_link_list,
}

"""MCP tools: cad_graph / cad_graph_link / assembly_plan_review (1D) plus the
PLM project-graph surface (plm_graph_*).

Lets an external model TRAVERSE the deterministic CAD project graph
(services.plm.cad_graph) and PLAN assemblies with best practices — deep products
built as subassemblies (each its own .step+) mated into the parent, not one
monolithic file. Also exposes the AI-authored link overlay (origin="ai"),
keyword tags, watch entries, graph configuration (linked projects), change
diffs, heuristic link suggestions and one-node briefs.

Every tool returns the flat envelope: ``{ok:true, …}`` / ``{ok:false, reason, hint}``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from services.mcp._envelopes import compact_err as _err, stringly_dumps as _dumps

# token guard — cap the node/edge payload so a huge project can't blow the context.
_MAX_NODES = 400
_MAX_EDGES = 400
# token guard for the change-diff lists (nodes/edges added/removed).
_MAX_DIFF = 200


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
          - "tree"  → assembly decomposition: nested assembly → subassembly/part
            tree with instance counts. `node_id` roots at one assembly;
            omit it for every top-level assembly in the project.
        Node kinds include the CAD structure (part/assembly/feature/…) plus
        `file` (non-CAD project files — office docs, images, exchange formats)
        and `keyword` (shared ``kw:<keyword>`` tag nodes, linked to their
        targets by TAGGED edges). For day-to-day navigation prefer
        `plm_graph_search` (find nodes by substring/kind/keyword) and
        `plm_graph_neighbors` (ego graph around one node) over paging the
        whole graph with "get".
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
            if qk == "tree":
                return _dumps(cg.assembly_tree(project_dir or None,
                                               doc_id=node_id or None))
            return _err("invalid_query_kind", "query_kind ∈ get | blast | path | tree")
        except Exception as exc:  # noqa: BLE001
            return _err("cad_graph_failed", str(exc))

    @mcp.tool()
    def cad_graph_link(src: str, dst: str, kind: str, note: str = "",
                       project_dir: str = "") -> str:
        """Author an AI link between two graph nodes (origin="ai").

        Use to record a design relationship the deterministic extractor can't see
        (e.g. two parts that must share a bore diameter). `kind` is a graph edge
        kind (REFERENCES, DERIVES_FROM, MATES, …). Persisted in the project overlay.

        `src`/`dst` may be cross-project qualified as ``<project_key>/<node_id>``
        — the link is stored qualified and resolves into the merged graph when
        that project is linked (see `plm_graph_configure` linked projects).
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

    # ------------------------------------------------------------------
    # PLM project-graph surface (plm_graph_*) — keywords, search, ego
    # graphs, overlay links, watch, change diffs, config, suggestions.
    # ------------------------------------------------------------------

    @mcp.tool()
    def plm_graph_keyword(path: str, target: str = "", keyword: str = "",
                          entry_id: str = "", op: str = "add") -> str:
        """Manage keyword tags on graph targets (nodes or project-relative paths).

        op:
          - "add"    → tag `target` with `keyword` (origin="ai"). On the next
            graph build tagged nodes carry ``meta.keywords`` and a TAGGED edge
            from the shared ``kw:<keyword>`` node.
          - "remove" → by `entry_id`, or by (`target`, `keyword`) pair.
          - "list"   → every keyword entry; pass `target` to filter to one node.
        """
        try:
            from services.plm import keywords as kw
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"keywords module: {exc}")
        if not path:
            return _err("project_dir required", "pass the project folder as `path`")
        try:
            o = (op or "add").strip().lower()
            if o == "add":
                if not target or not keyword:
                    return _err("target_keyword_required",
                                "pass both the target node id/path and the keyword")
                return _dumps(kw.add_keyword(path, target, keyword, origin="ai"))
            if o == "remove":
                if entry_id:
                    return _dumps(kw.remove_keyword(path, entry_id))
                if target and keyword:
                    return _dumps(kw.remove_pair(path, target, keyword))
                return _err("entry_id_required",
                            "pass entry_id, or target+keyword to remove a pair")
            if o == "list":
                entries = kw.list_entries(path)
                if target:
                    entries = [e for e in entries if e.get("target") == target]
                return _dumps({"ok": True, "entries": entries})
            return _err("invalid_op", "op ∈ add | remove | list")
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_keyword_failed", str(exc))

    @mcp.tool()
    def plm_graph_search(project_dir: str = "", q: str = "", kinds: str = "",
                         keyword: str = "", linked: bool = False) -> str:
        """Search the project graph by substring, node kind and/or keyword tag.

        `q` matches case-insensitively against node id/label/path/keywords/
        project; `kinds` is a comma-separated node-kind filter (part, assembly,
        doc, file, keyword, …); `keyword` matches tagged nodes plus the shared
        ``kw:<keyword>`` node. `linked` merges the configured linked projects.
        Token-bounded: nodes/edges capped, honest counts + `truncated`.
        """
        try:
            from services.plm import cad_graph as cg
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        try:
            kind_list = [k.strip() for k in (kinds or "").split(",") if k.strip()]
            g = cg.get_graph(project_dir, linked=bool(linked),
                             max_nodes=0, max_edges=0)
            res = cg.search_graph(g, q=q, kinds=kind_list or None,
                                  keyword=keyword)
            nodes = res.get("nodes") or []
            edges = res.get("edges") or []
            out: dict[str, Any] = {
                "ok": True,
                "nodes": nodes[:_MAX_NODES],
                "edges": edges[:_MAX_EDGES],
                "node_count": len(nodes),
                "edge_count": len(edges),
                "truncated": len(nodes) > _MAX_NODES or len(edges) > _MAX_EDGES,
            }
            if g.get("linked_projects"):
                out["linked_projects"] = g["linked_projects"]
            return _dumps(out)
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_search_failed", str(exc))

    @mcp.tool()
    def plm_graph_neighbors(project_dir: str = "", node_id: str = "",
                            depth: int = 1, linked: bool = False) -> str:
        """Ego graph: `node_id` plus everything within `depth` hops (undirected).

        The navigation companion to `plm_graph_search`: search to find a node,
        then read its neighborhood. Token-bounded like the other graph tools.
        """
        try:
            from services.plm import cad_graph as cg
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        if not node_id:
            return _err("node_id required", "pass the graph node id")
        try:
            g = cg.get_graph(project_dir, linked=bool(linked),
                             max_nodes=0, max_edges=0)
            res = cg.neighbors(g, node_id, depth)
            if not res.get("ok"):
                return _dumps(res)
            nodes = res.get("nodes") or []
            edges = res.get("edges") or []
            return _dumps({
                "ok": True,
                "nodes": nodes[:_MAX_NODES],
                "edges": edges[:_MAX_EDGES],
                "node_count": len(nodes),
                "edge_count": len(edges),
                "depth": res.get("depth"),
                "truncated": len(nodes) > _MAX_NODES or len(edges) > _MAX_EDGES,
            })
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_neighbors_failed", str(exc))

    @mcp.tool()
    def plm_graph_links(project_dir: str = "", op: str = "list",
                        link_id: str = "") -> str:
        """List or remove hand-/AI-authored overlay links (the add half lives in
        `cad_graph_link`).

        op:
          - "list"   → every overlay link in the project.
          - "remove" → drop one link by its `link_id`.
        """
        try:
            from services.plm import graph_links as gl
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"graph_links module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        try:
            o = (op or "list").strip().lower()
            if o == "list":
                return _dumps({"ok": True, "links": gl.list_links(project_dir)})
            if o == "remove":
                if not link_id:
                    return _err("link_id required",
                                "pass the overlay link id to remove")
                return _dumps(gl.remove_link(project_dir, link_id))
            return _err("invalid_op", "op ∈ list | remove")
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_links_failed", str(exc))

    @mcp.tool()
    def plm_graph_watch(path: str, op: str = "list", keyword: str = "",
                        node_id: str = "", note: str = "",
                        entry_id: str = "") -> str:
        """Manage the watch overlay: keywords / node ids of interest.

        `plm_graph_changes` flags added/removed nodes matching a watch entry.
        op:
          - "add"    → watch a `keyword` OR a `node_id` (exactly one;
            origin="ai"), with an optional `note`.
          - "remove" → drop one watch entry by `entry_id`.
          - "list"   → every watch entry in the project.
        """
        try:
            from services.plm import watch as w
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"watch module: {exc}")
        if not path:
            return _err("project_dir required", "pass the project folder as `path`")
        try:
            o = (op or "list").strip().lower()
            if o == "add":
                return _dumps(w.watch_add(path, keyword=keyword,
                                          node_id=node_id, note=note,
                                          origin="ai"))
            if o == "remove":
                if not entry_id:
                    return _err("entry_id required",
                                "pass the watch entry id to remove")
                return _dumps(w.watch_remove(path, entry_id))
            if o == "list":
                return _dumps({"ok": True, "watch": w.list_watch(path)})
            return _err("invalid_op", "op ∈ add | remove | list")
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_watch_failed", str(exc))

    @mcp.tool()
    def plm_graph_changes(project_dir: str = "", linked: bool = False) -> str:
        """Diff the project graph against its last cached build.

        The on-disk cached graph is the baseline (absent → empty graph); a
        fresh forced rebuild is the new state. Added/removed nodes that match a
        watch-overlay entry (`plm_graph_watch`) are flagged ``watched: true``
        and collected into the sorted `watched` id list. Each diff list is
        capped at 200 entries with a ``<list>_truncated`` flag.
        """
        try:
            from services.plm import cad_graph as cg
            from services.plm import watch as w
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        try:
            old = cg.load_cached_graph(project_dir) or {"nodes": [], "edges": []}
            new = cg.get_graph(project_dir, force=True, max_nodes=0,
                               max_edges=0, linked=bool(linked))
            diff = cg.graph_diff(old, new)
            watch_entries = w.list_watch(project_dir)
            matched = (w.watched_matches(new, watch_entries)
                       if watch_entries else set())
            matched_old = (w.watched_matches(old, watch_entries)
                           if watch_entries else set())
            for n in diff["nodes_added"]:
                if n.get("id") in matched:
                    n["watched"] = True
            for n in diff["nodes_removed"]:
                if n.get("id") in matched_old:
                    n["watched"] = True
            watched_changed = sorted({
                n["id"] for n in diff["nodes_added"] + diff["nodes_removed"]
                if n.get("watched")})
            out: dict[str, Any] = {
                "ok": True,
                "watched": watched_changed,
                "generated_at": new.get("generated_at"),
            }
            for key in ("nodes_added", "nodes_removed",
                        "edges_added", "edges_removed"):
                lst = diff.get(key) or []
                out[key] = lst[:_MAX_DIFF]
                out[f"{key}_truncated"] = len(lst) > _MAX_DIFF
            return _dumps(out)
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_changes_failed", str(exc))

    @mcp.tool()
    def plm_graph_configure(path: str, op: str = "get",
                            office_content: Optional[bool] = None,
                            add_linked_project: str = "",
                            remove_linked_project: str = "",
                            project_key: str = "") -> str:
        """Read or update the per-project graph configuration.

        op:
          - "get" → ``{ok, config, project_key}``.
          - "set" → apply whichever fields are provided, in order:
            `office_content` (bool — extract text from office documents),
            `add_linked_project` / `remove_linked_project` (project paths for
            cross-project graph merges), `project_key` (short alphanumeric key
            used in ``<project_key>/<node_id>`` cross-project refs). Stops at
            the first failing field and returns that error envelope.
        """
        try:
            from services.plm import graph_config as gcfg
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"graph_config module: {exc}")
        if not path:
            return _err("project_dir required", "pass the project folder as `path`")
        try:
            o = (op or "get").strip().lower()
            if o == "get":
                return _dumps({"ok": True, "config": gcfg.get_config(path),
                               "project_key": gcfg.project_key(path)})
            if o == "set":
                if office_content is not None:
                    r = gcfg.set_config(path, office_content=bool(office_content))
                    if not r.get("ok"):
                        return _dumps(r)
                if add_linked_project:
                    r = gcfg.add_linked_project(path, add_linked_project)
                    if not r.get("ok"):
                        return _dumps(r)
                if remove_linked_project:
                    r = gcfg.remove_linked_project(path, remove_linked_project)
                    if not r.get("ok"):
                        return _dumps(r)
                if project_key:
                    r = gcfg.set_config(path, project_key=project_key)
                    if not r.get("ok"):
                        return _dumps(r)
                return _dumps({"ok": True, "config": gcfg.get_config(path),
                               "project_key": gcfg.project_key(path)})
            return _err("invalid_op", "op ∈ get | set")
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_configure_failed", str(exc))

    @mcp.tool()
    def plm_graph_suggest(project_dir: str = "", node_id: str = "",
                          limit: int = 10) -> str:
        """Heuristic overlay-link suggestions over the project graph.

        Returns ``{ok, suggestions:[{src, dst, kind, reason, confidence}]}`` —
        candidate REFERENCES edges the deterministic extractor missed:
        same-folder file↔doc pairing ("same_folder", 0.4), a file's path stem
        appearing in another node's label/id ("name_match", 0.7), two nodes
        sharing a keyword tag ("shared_keyword", 0.6), and part numbers / stems
        mentioned in extracted office text ("content_mention", 0.9). Pairs
        already connected by a graph edge or overlay link are skipped. Pass
        `node_id` to keep only suggestions touching that node.
        """
        try:
            from services.plm import cad_graph as cg
            from services.plm import graph_links as gl
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        try:
            g = cg.get_graph(project_dir, max_nodes=0, max_edges=0)
            nodes = g.get("nodes") or []
            by_id = {n.get("id"): n for n in nodes if n.get("id")}
            if node_id and node_id not in by_id:
                return _err("not_found", f"no node {node_id!r}")
            connected: set[frozenset] = set()
            for e in g.get("edges") or []:
                connected.add(frozenset(
                    (str(e.get("src")), str(e.get("dst")))))
            for lk in gl.list_links(project_dir):
                connected.add(frozenset(
                    (str(lk.get("src")), str(lk.get("dst")))))

            confidence = {"content_mention": 0.9, "name_match": 0.7,
                          "shared_keyword": 0.6, "same_folder": 0.4}
            best: dict[frozenset, dict[str, Any]] = {}

            def _offer(a: str, b: str, reason: str) -> None:
                if a == b or a not in by_id or b not in by_id:
                    return
                pair = frozenset((a, b))
                if pair in connected:
                    return
                conf = confidence[reason]
                cur = best.get(pair)
                if cur is not None and cur["confidence"] >= conf:
                    return
                best[pair] = {"src": a, "dst": b, "kind": "REFERENCES",
                              "reason": reason, "confidence": conf}

            def _dir_of(n: dict[str, Any]) -> Optional[str]:
                meta = n.get("meta") or {}
                if n.get("kind") == "file":
                    return str(meta.get("folder") or "")
                p = n.get("path")
                if not p:
                    return None
                parent = str(Path(str(p)).parent).replace("\\", "/")
                return "" if parent == "." else parent

            file_nodes = [n for n in nodes if n.get("kind") == "file"]
            path_nodes = [n for n in nodes
                          if n.get("kind") not in ("file", "keyword")
                          and n.get("path")]

            # same_folder: file node + path-backed doc node in one folder
            for f in file_nodes:
                fdir = _dir_of(f)
                if fdir is None:
                    continue
                for o in path_nodes:
                    if _dir_of(o) == fdir:
                        _offer(str(f["id"]), str(o["id"]), "same_folder")

            # name_match: file path stem inside another node's label/id
            for f in file_nodes:
                if not f.get("path"):
                    continue
                stem = Path(str(f["path"])).stem.lower()
                if not stem:
                    continue
                for o in nodes:
                    if o["id"] == f["id"]:
                        continue
                    hay = (str(o.get("label") or "") + "\n"
                           + str(o.get("id") or "")).lower()
                    if stem in hay:
                        _offer(str(f["id"]), str(o["id"]), "name_match")

            # shared_keyword: two nodes tagged with the same keyword
            kw_index: dict[str, list[str]] = {}
            for n in nodes:
                for k in (n.get("meta") or {}).get("keywords") or []:
                    kw_index.setdefault(str(k), []).append(str(n["id"]))
            for ids in kw_index.values():
                ids = sorted(set(ids))
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        _offer(ids[i], ids[j], "shared_keyword")

            # content_mention: office text mentions a part number / path stem
            for f in file_nodes:
                preview = str((f.get("meta") or {}).get("text_preview")
                              or "").lower()
                if not preview:
                    continue
                for o in nodes:
                    if o["id"] == f["id"]:
                        continue
                    term = str((o.get("meta") or {}).get("part_number")
                               or "").lower()
                    if not term and o.get("path"):
                        term = Path(str(o["path"])).stem.lower()
                    if term and term in preview:
                        _offer(str(f["id"]), str(o["id"]), "content_mention")

            suggestions = sorted(
                best.values(),
                key=lambda s: (-s["confidence"], s["src"], s["dst"]))
            if node_id:
                suggestions = [s for s in suggestions
                               if s["src"] == node_id or s["dst"] == node_id]
            lim = max(1, int(limit or 10))
            return _dumps({"ok": True, "suggestions": suggestions[:lim]})
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_suggest_failed", str(exc))

    @mcp.tool()
    def plm_graph_brief(project_dir: str = "", node_id: str = "") -> str:
        """One-node dossier: identity, degree, edge-kind histogram, neighbors.

        Combines the ego graph at depth 1 with the overlay links touching
        `node_id` and the watch overlay (`watched: true` when the node matches
        a watch entry). Use after `plm_graph_search` to orient on a hit.
        """
        try:
            from services.plm import cad_graph as cg
            from services.plm import graph_links as gl
            from services.plm import watch as w
        except Exception as exc:  # noqa: BLE001
            return _err("unavailable", f"cad_graph module: {exc}")
        if not project_dir:
            return _err("project_dir required", "pass the project folder")
        if not node_id:
            return _err("node_id required", "pass the graph node id")
        try:
            g = cg.get_graph(project_dir, max_nodes=0, max_edges=0)
            ego = cg.neighbors(g, node_id, 1)
            if not ego.get("ok"):
                return _dumps(ego)
            node = next(n for n in ego["nodes"] if n.get("id") == node_id)
            meta = node.get("meta") or {}
            touching = [e for e in ego["edges"]
                        if e.get("src") == node_id or e.get("dst") == node_id]
            edge_kinds: dict[str, int] = {}
            for e in touching:
                k = str(e.get("kind"))
                edge_kinds[k] = edge_kinds.get(k, 0) + 1
            by_id = {n.get("id"): n for n in ego["nodes"]}
            neighbors: list[dict[str, Any]] = []
            seen: set[str] = set()
            for e in touching:
                other = e.get("dst") if e.get("src") == node_id else e.get("src")
                if other in seen or other == node_id:
                    continue
                seen.add(other)
                on = by_id.get(other) or {}
                neighbors.append({"id": other, "kind": on.get("kind"),
                                  "label": on.get("label")})
            neighbors.sort(key=lambda d: str(d["id"]))
            overlay = [lk for lk in gl.list_links(project_dir)
                       if lk.get("src") == node_id or lk.get("dst") == node_id]
            watched = node_id in w.watched_matches(g, w.list_watch(project_dir))
            return _dumps({
                "ok": True,
                "node": {
                    "id": node.get("id"),
                    "kind": node.get("kind"),
                    "label": node.get("label"),
                    "path": node.get("path"),
                    "project": meta.get("project"),
                    "keywords": meta.get("keywords") or [],
                },
                "degree": len(touching),
                "edge_kinds": edge_kinds,
                "neighbors": neighbors[:25],
                "overlay_links": overlay,
                "watched": watched,
            })
        except Exception as exc:  # noqa: BLE001
            return _err("plm_graph_brief_failed", str(exc))

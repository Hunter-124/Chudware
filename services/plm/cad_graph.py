"""services.plm.cad_graph — deterministic CAD project graph (contracts/cad_graph.md v1).

Graphify/Sourcegraph analogue over CAD projects: parts, features, sketches,
bodies, mates, BOM lines, drawings, sim studies, boards, firmware.

Nodes: ``{id, kind, label, path?, doc_id?, meta{}, origin}``
Edges: ``{src, dst, kind, meta{}, origin}``

Kinds (nodes): project|doc|part|assembly|body|feature|sketch|mate|bom_item|
               drawing|sim_study|board|firmware
Kinds (edges): CONTAINS|DEPENDS_ON|MATES|INSTANCE_OF|REFERENCES|DERIVES_FROM|
               DOCUMENTS|SIMULATES|MANUFACTURES

``origin`` is always ``extracted`` (deterministic from doc data) or
``inferred`` (heuristic). Cache lives at ``<project>/.plm/graph.json`` and is
keyed by source-file mtimes; recompute is always allowed.

AI-VC seam: :func:`on_ai_vc_checkpoint` is the rebuild hook CheckpointsCore
calls after a successful checkpoint (best-effort; never raises into AI-VC).
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

ORIGIN_EXTRACTED = "extracted"
ORIGIN_INFERRED = "inferred"

NODE_KINDS = frozenset({
    "project", "doc", "part", "assembly", "body", "feature", "sketch",
    "mate", "bom_item", "drawing", "sim_study", "board", "firmware",
})
EDGE_KINDS = frozenset({
    "CONTAINS", "DEPENDS_ON", "MATES", "INSTANCE_OF", "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS", "SIMULATES", "MANUFACTURES",
})

_CONTAINER_SUFFIXES = (".step+", ".ecad+")
_SCHEMA_KIND = {
    "aicad.part": "part",
    "aicad.asm": "assembly",
    "aicad.drawing": "drawing",
    "aicad.pcb": "board",
    "aicad.sch": "board",
    "aicad.cam": "doc",
    "aicad.req": "doc",
}

# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def build_graph(
    project_dir: str | os.PathLike | None = None,
    *,
    open_docs: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build (or load-cached) the CAD project graph.

    Parameters
    ----------
    project_dir
        Project root. When None, only *open_docs* contribute.
    open_docs
        Optional ``{doc_id: document_dict}`` of in-memory open documents
        (engine sessions). Merged on top of on-disk containers.
    force
        Skip the mtime cache and rebuild.
    """
    root = Path(project_dir).resolve() if project_dir else None
    sources = _collect_sources(root, open_docs or {})
    if not force and root is not None:
        cached = _load_cache_if_valid(root, sources)
        if cached is not None:
            return cached

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    def add_node(n: dict[str, Any]) -> str:
        nid = n["id"]
        if nid in node_ids:
            return nid
        if "origin" not in n:
            n["origin"] = ORIGIN_EXTRACTED
        nodes.append(n)
        node_ids.add(nid)
        return nid

    def add_edge(src: str, dst: str, kind: str, *,
                 meta: dict | None = None,
                 origin: str = ORIGIN_EXTRACTED) -> None:
        if src not in node_ids or dst not in node_ids:
            return
        edges.append({
            "src": src, "dst": dst, "kind": kind,
            "meta": meta or {}, "origin": origin,
        })

    # project root
    proj_id = "project:root"
    proj_label = root.name if root is not None else "open-docs"
    proj_path = str(root) if root is not None else None
    add_node({
        "id": proj_id, "kind": "project", "label": proj_label,
        "path": proj_path, "doc_id": None, "meta": {},
        "origin": ORIGIN_EXTRACTED,
    })

    # path → part/assembly node id (for INSTANCE_OF / BOM resolution)
    path_to_doc_node: dict[str, str] = {}
    uuid_to_doc_node: dict[str, str] = {}
    # shared equation index: name → list of (node_id, value, unit)
    eq_index: dict[str, list[tuple[str, Any, str | None]]] = {}
    # feature dim index for REFERENCES blast: dim_key → feature node ids
    dim_owners: dict[str, list[str]] = {}

    # --- walk on-disk containers + open docs --------------------------------
    for src in sources:
        doc = src["doc"]
        if not isinstance(doc, dict):
            continue
        rel = src.get("path") or ""
        doc_uuid = str(doc.get("id") or "")
        schema = str(doc.get("schema") or "")
        kind = _kind_from_schema(schema) or src.get("kind_hint") or "doc"
        name = str(doc.get("name") or (Path(rel).stem if rel else doc_uuid) or "doc")
        doc_id = src.get("doc_id") or doc_uuid or rel

        # container doc node (part|assembly|drawing|board|doc)
        if kind == "part":
            node_kind = "part"
        elif kind == "assembly":
            node_kind = "assembly"
        elif kind == "drawing":
            node_kind = "drawing"
        elif kind == "board":
            node_kind = "board"
        else:
            node_kind = "doc"

        dnode_id = f"doc:{doc_id}" if not rel else f"doc:{rel}"
        add_node({
            "id": dnode_id, "kind": node_kind, "label": name,
            "path": rel or None, "doc_id": doc_id,
            "meta": {"schema": schema, "uuid": doc_uuid},
            "origin": ORIGIN_EXTRACTED,
        })
        add_edge(proj_id, dnode_id, "CONTAINS")
        if rel:
            path_to_doc_node[_norm_path(rel)] = dnode_id
            path_to_doc_node[_stem(rel)] = dnode_id
        if doc_uuid:
            uuid_to_doc_node[doc_uuid] = dnode_id

        # features + sketches
        feat_nodes: dict[str, str] = {}
        for feat in doc.get("features") or []:
            if not isinstance(feat, dict):
                continue
            fid = str(feat.get("id") or "")
            if not fid:
                continue
            ftype = str(feat.get("type") or feat.get("name") or "feature")
            is_sketch = ftype in ("sketch", "sketch3d") or (
                isinstance(feat.get("params"), dict)
                and "entities" in (feat.get("params") or {})
            )
            fkind = "sketch" if is_sketch else "feature"
            flabel = str(feat.get("name") or ftype or fid)
            fnode = f"feature:{dnode_id}:{fid}"
            add_node({
                "id": fnode, "kind": fkind, "label": flabel,
                "path": rel or None, "doc_id": doc_id,
                "meta": {
                    "feature_id": fid, "type": ftype,
                    "suppressed": bool(feat.get("suppressed")),
                    "params": _shallow_params(feat.get("params")),
                },
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, fnode, "CONTAINS")
            feat_nodes[fid] = fnode

            # DEPENDS_ON previous features via refs / derived_from
            for dep in _feature_deps(feat):
                if dep in feat_nodes:
                    add_edge(fnode, feat_nodes[dep], "DEPENDS_ON",
                             meta={"via": "ref"})
            df = feat.get("derived_from")
            if isinstance(df, dict) and df.get("feature"):
                # cross-doc derived — resolve later if possible
                src_doc = df.get("doc_id") or df.get("doc_name")
                src_feat = df.get("feature") or df.get("source_feature")
                if src_doc and src_feat:
                    # store as DERIVES_FROM to a synthetic key; resolve after
                    pass  # resolved in second pass via meta
            # dim ownership for shared-dim REFERENCES
            for dkey, dval, dunit in _feature_dims(feat):
                dim_owners.setdefault(dkey, []).append(fnode)
                # also index under equation-style name
                eq_index.setdefault(dkey, []).append((fnode, dval, dunit))

        # history order DEPENDS_ON (feature N depends on N-1) — inferred
        feat_list = [f for f in (doc.get("features") or []) if isinstance(f, dict) and f.get("id")]
        for i in range(1, len(feat_list)):
            a = feat_nodes.get(str(feat_list[i]["id"]))
            b = feat_nodes.get(str(feat_list[i - 1]["id"]))
            if a and b:
                # only add if not already DEPENDS_ON via explicit ref
                if not any(e["src"] == a and e["dst"] == b and e["kind"] == "DEPENDS_ON"
                           for e in edges):
                    add_edge(a, b, "DEPENDS_ON",
                             meta={"via": "history"}, origin=ORIGIN_INFERRED)

        # bodies
        for body in doc.get("bodies") or []:
            if not isinstance(body, dict):
                continue
            bid = str(body.get("id") or body.get("name") or "")
            if not bid:
                continue
            bnode = f"body:{dnode_id}:{bid}"
            add_node({
                "id": bnode, "kind": "body",
                "label": str(body.get("name") or bid),
                "path": rel or None, "doc_id": doc_id,
                "meta": {"body_id": bid},
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, bnode, "CONTAINS")
            owner = body.get("owner_feature") or body.get("feature")
            if owner and str(owner) in feat_nodes:
                add_edge(feat_nodes[str(owner)], bnode, "CONTAINS",
                         meta={"via": "owns_topo"})

        # equations → dim index for cross-doc REFERENCES
        globals_ = (doc.get("equations") or {}).get("globals") or []
        for eq in globals_:
            if not isinstance(eq, dict):
                continue
            ename = str(eq.get("name") or "")
            if not ename:
                continue
            eval_ = eq.get("value", eq.get("expr"))
            unit = eq.get("unit")
            eq_index.setdefault(ename, []).append((dnode_id, eval_, unit))

        # mates (assemblies)
        for mate in doc.get("mates") or []:
            if not isinstance(mate, dict):
                continue
            mid = str(mate.get("id") or "")
            if not mid:
                continue
            mtype = str(mate.get("type") or mate.get("kind") or "mate")
            mnode = f"mate:{dnode_id}:{mid}"
            add_node({
                "id": mnode, "kind": "mate", "label": mtype,
                "path": rel or None, "doc_id": doc_id,
                "meta": {
                    "mate_id": mid, "type": mtype,
                    "suppressed": bool(mate.get("suppressed")),
                    "refs": mate.get("refs") or [],
                },
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, mnode, "CONTAINS")
            # MATES edges to component instances
            for ref in _mate_component_ids(mate):
                cnode = f"instance:{dnode_id}:{ref}"
                # instance node may not exist yet — create placeholder
                if cnode not in node_ids:
                    add_node({
                        "id": cnode, "kind": "doc",
                        "label": ref, "path": rel or None,
                        "doc_id": doc_id,
                        "meta": {"component_id": ref, "placeholder": True},
                        "origin": ORIGIN_INFERRED,
                    })
                    add_edge(dnode_id, cnode, "CONTAINS", origin=ORIGIN_INFERRED)
                add_edge(mnode, cnode, "MATES", meta={"type": mtype})

        # components → INSTANCE_OF + BOM
        bom_counts: dict[str, int] = {}
        for comp in doc.get("components") or []:
            if not isinstance(comp, dict):
                continue
            cid = str(comp.get("id") or "")
            cref = _component_ref(comp)
            clabel = str(comp.get("name") or cref or cid or "component")
            cnode = f"instance:{dnode_id}:{cid or clabel}"
            add_node({
                "id": cnode, "kind": "doc",
                "label": clabel, "path": rel or None, "doc_id": doc_id,
                "meta": {
                    "component_id": cid, "ref": cref,
                    "suppressed": bool(comp.get("suppressed")),
                    "fixed": bool(comp.get("fixed")),
                },
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, cnode, "CONTAINS")
            if cref:
                bom_counts[cref] = bom_counts.get(cref, 0) + 1
                # INSTANCE_OF resolved after all docs registered
                # stash target on meta for second pass
                edges.append({  # provisional; filtered if target missing later
                    "src": cnode, "dst": f"__resolve__:{cref}",
                    "kind": "INSTANCE_OF",
                    "meta": {"ref": cref}, "origin": ORIGIN_EXTRACTED,
                })

        # explicit BOM field
        for row in doc.get("bom") or doc.get("bom_items") or []:
            if not isinstance(row, dict):
                continue
            rref = str(row.get("part") or row.get("ref") or row.get("path")
                       or row.get("part_ref") or "")
            qty = int(row.get("quantity") or row.get("qty") or 1)
            if rref:
                bom_counts[rref] = bom_counts.get(rref, 0) + max(qty, 1)

        for rref, qty in bom_counts.items():
            bitem = f"bom:{dnode_id}:{_safe(rref)}"
            add_node({
                "id": bitem, "kind": "bom_item",
                "label": f"{_stem(rref)} ×{qty}",
                "path": rel or None, "doc_id": doc_id,
                "meta": {"ref": rref, "quantity": qty},
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, bitem, "CONTAINS")
            edges.append({
                "src": bitem, "dst": f"__resolve__:{rref}",
                "kind": "INSTANCE_OF",
                "meta": {"ref": rref, "quantity": qty},
                "origin": ORIGIN_EXTRACTED,
            })

        # drawing → DOCUMENTS source
        for key in ("source", "source_doc", "part"):
            sref = doc.get(key)
            if isinstance(sref, dict):
                sref = sref.get("path") or sref.get("doc_id")
            if isinstance(sref, str) and sref:
                edges.append({
                    "src": dnode_id, "dst": f"__resolve__:{sref}",
                    "kind": "DOCUMENTS",
                    "meta": {"via": key}, "origin": ORIGIN_EXTRACTED,
                })
                break

        # studies inside document.json
        for study in doc.get("studies") or []:
            if not isinstance(study, dict):
                continue
            sid = str(study.get("id") or study.get("name") or "")
            if not sid:
                continue
            snode = f"sim:{dnode_id}:{sid}"
            add_node({
                "id": snode, "kind": "sim_study",
                "label": str(study.get("name") or sid),
                "path": rel or None, "doc_id": doc_id,
                "meta": {"study_id": sid, "type": study.get("type")},
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, snode, "CONTAINS")
            add_edge(snode, dnode_id, "SIMULATES")

        # studies/ zip members listed in source
        for study_id in src.get("study_ids") or []:
            snode = f"sim:{dnode_id}:{study_id}"
            if snode not in node_ids:
                add_node({
                    "id": snode, "kind": "sim_study",
                    "label": study_id, "path": rel or None, "doc_id": doc_id,
                    "meta": {"study_id": study_id, "via": "studies/"},
                    "origin": ORIGIN_EXTRACTED,
                })
                add_edge(dnode_id, snode, "CONTAINS")
                add_edge(snode, dnode_id, "SIMULATES")

    # --- project-level folders: sim studies, boards, firmware --------------
    if root is not None:
        _walk_project_folders(root, proj_id, add_node, add_edge, path_to_doc_node)

    # --- resolve __resolve__: targets --------------------------------------
    resolved_edges: list[dict[str, Any]] = []
    for e in edges:
        dst = e["dst"]
        if isinstance(dst, str) and dst.startswith("__resolve__:"):
            ref = dst[len("__resolve__:"):]
            target = _resolve_doc_node(ref, path_to_doc_node, uuid_to_doc_node)
            if target is None:
                continue  # drop unresolved
            e = dict(e, dst=target)
        if e["src"] in node_ids and e["dst"] in node_ids:
            resolved_edges.append(e)
    edges = resolved_edges

    # --- shared dims / layouts → REFERENCES --------------------------------
    for ename, owners in eq_index.items():
        # group by (value, unit) — same name+value across docs = shared
        by_val: dict[tuple[str, str | None], list[str]] = {}
        for nid, val, unit in owners:
            key = (json.dumps(val, sort_keys=True, default=str), unit)
            by_val.setdefault(key, []).append(nid)
        for (val_s, unit), nids in by_val.items():
            uniq = list(dict.fromkeys(nids))  # stable unique
            if len(uniq) < 2:
                continue
            try:
                val = json.loads(val_s)
            except Exception:  # noqa: BLE001
                val = val_s
            # fully connect (undirected as bidirectional REFERENCES)
            for i, a in enumerate(uniq):
                for b in uniq[i + 1:]:
                    meta = {"dim": ename, "value": val, "unit": unit}
                    add_edge(a, b, "REFERENCES", meta=meta)
                    add_edge(b, a, "REFERENCES", meta=meta)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    graph = {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "generated_at": generated_at,
        "project_dir": str(root) if root else None,
        "mtimes": {s["key"]: s["mtime"] for s in sources if s.get("key")},
    }
    if root is not None:
        _save_cache(root, graph)
    return graph


def get_graph(
    project_dir: str | os.PathLike | None = None,
    *,
    doc_id: str | None = None,
    open_docs: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """``cad_graph_get`` implementation."""
    g = build_graph(project_dir, open_docs=open_docs, force=force)
    if doc_id:
        # filter to the subgraph of that doc + its neighbors
        keep = {n["id"] for n in g["nodes"]
                if n.get("doc_id") == doc_id or n["kind"] == "project"
                or (n.get("path") and doc_id in (n.get("path") or ""))}
        # expand one hop
        changed = True
        while changed:
            changed = False
            for e in g["edges"]:
                if e["src"] in keep and e["dst"] not in keep:
                    keep.add(e["dst"]); changed = True
                if e["dst"] in keep and e["src"] not in keep:
                    keep.add(e["src"]); changed = True
        g = {
            **g,
            "nodes": [n for n in g["nodes"] if n["id"] in keep],
            "edges": [e for e in g["edges"]
                      if e["src"] in keep and e["dst"] in keep],
        }
    return {
        "ok": True,
        "nodes": g["nodes"],
        "edges": g["edges"],
        "generated_at": g.get("generated_at"),
        "project_dir": g.get("project_dir"),
    }


def blast_radius(node_id: str, graph: dict[str, Any] | None = None,
                 *, project_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """``cad_graph_blast`` — transitive dependents of *node_id*.

    Walks reverse DEPENDS_ON / REFERENCES / INSTANCE_OF / DOCUMENTS / MATES /
    CONTAINS (child→parent not followed; parent→child via reverse of CONTAINS
    is NOT taken — dependents are nodes that point *at* us or that we reach
    following reverse DEPENDS_ON / REFERENCES).
    """
    g = graph or build_graph(project_dir)
    # reverse adjacency for "who depends on me"
    rev: dict[str, list[tuple[str, dict]]] = {}
    fwd_dep: dict[str, list[tuple[str, dict]]] = {}
    for e in g.get("edges") or []:
        kind = e.get("kind")
        # dependents = nodes that DEPENDS_ON / REFERENCES / DOCUMENTS / INSTANCE_OF us
        if kind in ("DEPENDS_ON", "REFERENCES", "DOCUMENTS", "INSTANCE_OF",
                    "DERIVES_FROM", "SIMULATES", "MATES"):
            rev.setdefault(e["dst"], []).append((e["src"], e))
        # also follow CONTAINS upward? no — blast is "what breaks if I change X"
        # so: features that DEPENDS_ON me, docs that DOCUMENT me, instances of me
        if kind in ("DEPENDS_ON", "REFERENCES"):
            fwd_dep.setdefault(e["src"], []).append((e["dst"], e))

    if not any(n["id"] == node_id for n in g.get("nodes") or []):
        return {"ok": False, "reason": "not_found",
                "hint": f"no node {node_id!r}", "affected": [], "edges": []}

    affected: list[str] = []
    used_edges: list[dict[str, Any]] = []
    seen: set[str] = {node_id}
    frontier = [node_id]
    while frontier:
        cur = frontier.pop()
        for nbr, edge in rev.get(cur, []):
            if nbr in seen:
                continue
            seen.add(nbr)
            affected.append(nbr)
            used_edges.append(edge)
            frontier.append(nbr)
        # also: if cur is a shared dim node, REFERENCES neighbors are co-affected
        for e in g.get("edges") or []:
            if e.get("kind") != "REFERENCES":
                continue
            if e["src"] == cur and e["dst"] not in seen:
                seen.add(e["dst"])
                affected.append(e["dst"])
                used_edges.append(e)
                frontier.append(e["dst"])
            elif e["dst"] == cur and e["src"] not in seen:
                seen.add(e["src"])
                affected.append(e["src"])
                used_edges.append(e)
                frontier.append(e["src"])

    return {"ok": True, "affected": affected, "edges": used_edges,
            "node_id": node_id}


def find_path(src: str, dst: str, graph: dict[str, Any] | None = None,
              *, project_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """``cad_graph_path`` — shortest undirected path between two nodes."""
    g = graph or build_graph(project_dir)
    ids = {n["id"] for n in g.get("nodes") or []}
    if src not in ids:
        return {"ok": False, "reason": "not_found",
                "hint": f"no node {src!r}", "path": []}
    if dst not in ids:
        return {"ok": False, "reason": "not_found",
                "hint": f"no node {dst!r}", "path": []}
    if src == dst:
        return {"ok": True, "path": [src]}

    adj: dict[str, list[str]] = {}
    for e in g.get("edges") or []:
        adj.setdefault(e["src"], []).append(e["dst"])
        adj.setdefault(e["dst"], []).append(e["src"])

    # BFS
    prev: dict[str, str | None] = {src: None}
    queue = [src]
    found = False
    while queue and not found:
        cur = queue.pop(0)
        for nbr in adj.get(cur, []):
            if nbr in prev:
                continue
            prev[nbr] = cur
            if nbr == dst:
                found = True
                break
            queue.append(nbr)
    if not found:
        return {"ok": True, "path": []}  # no path
    # reconstruct
    path = [dst]
    while path[-1] != src:
        p = prev[path[-1]]
        if p is None:
            break
        path.append(p)
    path.reverse()
    return {"ok": True, "path": path}


def on_ai_vc_checkpoint(
    project_dir: str | os.PathLike | None = None,
    doc_id: str | None = None,
    **_: Any,
) -> None:
    """AI-VC rebuild hook — best-effort; never raises into the checkpoint path.

    CheckpointsCore (services/plm/ai_vc.py) should call this after a successful
    checkpoint. Forces a graph rebuild when *project_dir* is known.
    """
    try:
        if project_dir:
            build_graph(project_dir, force=True)
        # doc_id alone without project_dir: nothing durable to cache
    except Exception:  # noqa: BLE001
        pass


def invalidate_cache(project_dir: str | os.PathLike) -> None:
    """Drop the on-disk cache so the next get rebuilds."""
    p = _cache_path(Path(project_dir))
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# source collection
# ---------------------------------------------------------------------------


def _collect_sources(
    root: Path | None,
    open_docs: dict[str, Any],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    if root is not None and root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            # skip vault / cache / vcs
            dirnames[:] = [d for d in dirnames
                           if d not in {".aicadvault", ".plm", ".git",
                                        "__pycache__", "node_modules",
                                        ".venv", "venv"}]
            for fn in filenames:
                lower = fn.lower()
                if not (lower.endswith(".step+") or lower.endswith(".ecad+")):
                    continue
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(root)).replace("\\", "/")
                    mtime = fp.stat().st_mtime
                    doc, study_ids = _read_container(fp)
                except Exception:  # noqa: BLE001
                    continue
                if doc is None:
                    continue
                key = f"file:{rel}"
                sources.append({
                    "key": key, "path": rel, "mtime": mtime,
                    "doc": doc, "doc_id": doc.get("id"),
                    "study_ids": study_ids,
                    "kind_hint": "board" if lower.endswith(".ecad+") else None,
                })
                seen_keys.add(key)

    for did, doc in (open_docs or {}).items():
        if not isinstance(doc, dict):
            continue
        key = f"open:{did}"
        if key in seen_keys:
            continue
        sources.append({
            "key": key, "path": None, "mtime": time.time(),
            "doc": doc, "doc_id": did, "study_ids": [],
        })
    return sources


def _read_container(path: Path) -> tuple[dict | None, list[str]]:
    """Return (document.json dict, study ids from studies/)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None, []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if "document.json" not in names:
                return None, []
            doc = json.loads(zf.read("document.json").decode("utf-8"))
            study_ids = []
            for n in names:
                if n.startswith("studies/") and n.endswith(".json"):
                    # studies/<id>.json
                    parts = n.split("/")
                    if len(parts) == 2:
                        study_ids.append(parts[1][:-5])  # strip .json
            return doc, study_ids
    except Exception:  # noqa: BLE001
        return None, []


def _walk_project_folders(
    root: Path,
    proj_id: str,
    add_node,
    add_edge,
    path_to_doc_node: dict[str, str],
) -> None:
    """Emit sim_study / board / firmware nodes from well-known project dirs."""
    for child in sorted(root.iterdir() if root.is_dir() else []):
        if not child.is_dir():
            continue
        name = child.name.lower()
        if name in {".aicadvault", ".plm", ".git", "__pycache__",
                    ".venv", "venv", "node_modules"}:
            continue
        try:
            entries = set(os.listdir(child))
        except OSError:
            continue
        rel = child.name

        # firmware markers
        fw_markers = {"platformio.ini", "CMakeLists.txt", "Makefile",
                      "src", "main.c", "main.cpp", "main.py"}
        # board / ecad loose files
        board_markers = {e for e in entries
                         if e.endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro"))}
        # sim study markers
        sim_markers = {"study.json", "system", "constant", "polyMesh", "results", "mesh"}

        if name in ("firmware", "fw", "mcu", "embedded") or (entries & fw_markers and name in (
                "firmware", "fw", "software", "src", "mcu")):
            nid = f"firmware:{rel}"
            add_node({
                "id": nid, "kind": "firmware", "label": child.name,
                "path": rel, "doc_id": None,
                "meta": {"dir": rel}, "origin": ORIGIN_EXTRACTED,
            })
            add_edge(proj_id, nid, "CONTAINS")
            # MANUFACTURES edges to boards if any
            continue

        if name in ("boards", "ecad", "pcb") or board_markers:
            if board_markers:
                for bf in sorted(board_markers):
                    nid = f"board:{rel}/{bf}"
                    add_node({
                        "id": nid, "kind": "board", "label": bf,
                        "path": f"{rel}/{bf}", "doc_id": None,
                        "meta": {}, "origin": ORIGIN_EXTRACTED,
                    })
                    add_edge(proj_id, nid, "CONTAINS")
            else:
                nid = f"board:{rel}"
                add_node({
                    "id": nid, "kind": "board", "label": child.name,
                    "path": rel, "doc_id": None,
                    "meta": {"dir": rel}, "origin": ORIGIN_EXTRACTED,
                })
                add_edge(proj_id, nid, "CONTAINS")
            continue

        if name in ("sim", "sims", "simulation", "fea", "cfd", "studies") or (
                entries & sim_markers):
            # each subdir or study.json → sim_study
            if "study.json" in entries:
                nid = f"sim:dir:{rel}"
                add_node({
                    "id": nid, "kind": "sim_study", "label": child.name,
                    "path": rel, "doc_id": None,
                    "meta": {"dir": rel}, "origin": ORIGIN_EXTRACTED,
                })
                add_edge(proj_id, nid, "CONTAINS")
            else:
                for sub in sorted(child.iterdir()):
                    if sub.is_dir() and not sub.name.startswith("."):
                        nid = f"sim:dir:{rel}/{sub.name}"
                        add_node({
                            "id": nid, "kind": "sim_study", "label": sub.name,
                            "path": f"{rel}/{sub.name}", "doc_id": None,
                            "meta": {"dir": f"{rel}/{sub.name}"},
                            "origin": ORIGIN_EXTRACTED,
                        })
                        add_edge(proj_id, nid, "CONTAINS")


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _cache_path(root: Path) -> Path:
    return root / ".plm" / "graph.json"


def _load_cache_if_valid(root: Path, sources: list[dict[str, Any]]) -> dict | None:
    cp = _cache_path(root)
    if not cp.is_file():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    cached_mtimes = data.get("mtimes") or {}
    current = {s["key"]: s["mtime"] for s in sources if s.get("key")}
    # any new/missing/changed mtime → invalidate
    if set(cached_mtimes) != set(current):
        return None
    for k, mt in current.items():
        # float compare with small epsilon for FS granularity
        if abs(float(cached_mtimes.get(k, -1)) - float(mt)) > 1e-6:
            return None
    data["ok"] = True
    return data


def _save_cache(root: Path, graph: dict[str, Any]) -> None:
    cp = _cache_path(root)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        # write atomically
        tmp = cp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(graph, sort_keys=True, indent=2,
                                  default=str), encoding="utf-8")
        tmp.replace(cp)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _kind_from_schema(schema: str) -> str | None:
    base = str(schema).split("/", 1)[0]
    return _SCHEMA_KIND.get(base)


def _norm_path(p: str) -> str:
    return str(p).replace("\\", "/").lstrip("./")


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    for suffix in _CONTAINER_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def _shallow_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in ("entities", "constraints", "external_refs"):
            continue  # bulky sketch data
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict) and "value" in v:
            out[k] = v.get("value")
        elif isinstance(v, (list, tuple)) and len(v) <= 4:
            out[k] = list(v)
    return out


def _feature_deps(feat: dict[str, Any]) -> list[str]:
    deps: list[str] = []
    refs = feat.get("refs")
    if isinstance(refs, dict):
        for v in refs.values():
            deps.extend(_collect_feature_ids(v))
    elif isinstance(refs, list):
        for v in refs:
            deps.extend(_collect_feature_ids(v))
    # common param keys that point at prior features
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    for key in ("sketch", "profile", "path", "seed", "features", "body",
                "face", "plane", "axis"):
        if key in params:
            deps.extend(_collect_feature_ids(params[key]))
        if isinstance(refs, dict) and key in refs:
            deps.extend(_collect_feature_ids(refs[key]))
    # de-dupe preserving order, skip self
    self_id = str(feat.get("id") or "")
    seen: set[str] = set()
    out: list[str] = []
    for d in deps:
        if d and d != self_id and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _collect_feature_ids(v: Any) -> list[str]:
    if isinstance(v, str) and v and not v.startswith(("{", "/")):
        # bare feature id (f1, sketch1, …) — not a selector object
        if v[0].isalpha() or v[0] == "_":
            return [v]
        return []
    if isinstance(v, dict):
        out: list[str] = []
        if "feature" in v and isinstance(v["feature"], str):
            out.append(v["feature"])
        for vv in v.values():
            out.extend(_collect_feature_ids(vv))
        return out
    if isinstance(v, list):
        out = []
        for item in v:
            out.extend(_collect_feature_ids(item))
        return out
    return []


def _feature_dims(feat: dict[str, Any]) -> list[tuple[str, Any, str | None]]:
    """Yield (dim_name, value, unit) from feature params."""
    out: list[tuple[str, Any, str | None]] = []
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    fid = str(feat.get("id") or "")
    for k, v in params.items():
        if k in ("entities", "constraints", "external_refs", "code", "text"):
            continue
        if isinstance(v, (int, float)):
            out.append((f"{fid}.{k}", v, None))
            out.append((k, v, None))  # bare name for cross-doc match
        elif isinstance(v, dict) and "value" in v:
            out.append((f"{fid}.{k}", v.get("value"), v.get("unit")))
            out.append((k, v.get("value"), v.get("unit")))
    # sketch dimension constraints
    if isinstance(params.get("constraints"), list):
        for c in params["constraints"]:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("distance", "length", "angle", "radius", "diameter"):
                val = c.get("value")
                if isinstance(val, dict):
                    out.append((f"{fid}.{c.get('id', 'dim')}", val.get("value"),
                                val.get("unit")))
                elif isinstance(val, (int, float)):
                    out.append((f"{fid}.{c.get('id', 'dim')}", val, None))
    return out


def _component_ref(comp: dict[str, Any]) -> str:
    src = comp.get("source")
    if isinstance(src, dict):
        return str(src.get("path") or src.get("embedded") or src.get("doc_id") or "")
    if isinstance(src, str):
        return src
    return str(comp.get("ref") or "")


def _mate_component_ids(mate: dict[str, Any]) -> list[str]:
    out: list[str] = []
    refs = mate.get("refs")
    if isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict) and r.get("component"):
                out.append(str(r["component"]))
            elif isinstance(r, str):
                out.append(r)
    for key in ("a", "b", "component_a", "component_b", "entity_a", "entity_b"):
        v = mate.get(key)
        if isinstance(v, str) and v:
            out.append(v)
        elif isinstance(v, dict) and v.get("component"):
            out.append(str(v["component"]))
    # de-dupe
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _resolve_doc_node(
    ref: str,
    path_to_doc_node: dict[str, str],
    uuid_to_doc_node: dict[str, str],
) -> str | None:
    if not ref:
        return None
    n = _norm_path(ref)
    if n in path_to_doc_node:
        return path_to_doc_node[n]
    if ref in uuid_to_doc_node:
        return uuid_to_doc_node[ref]
    st = _stem(ref)
    if st in path_to_doc_node:
        return path_to_doc_node[st]
    # try with .step+ suffix
    for suf in _CONTAINER_SUFFIXES:
        cand = st + suf
        if cand in path_to_doc_node:
            return path_to_doc_node[cand]
        if _norm_path(cand) in path_to_doc_node:
            return path_to_doc_node[_norm_path(cand)]
    return None

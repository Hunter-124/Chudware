"""services.plm.cad_graph — deterministic CAD project graph (contracts/cad_graph.md v1).

Sourcegraph-style code-graph analogue over CAD projects: parts, features, sketches,
bodies, mates, BOM lines, drawings, sim studies, boards, firmware.

Nodes: ``{id, kind, label, path?, doc_id?, meta{}, origin}``
Edges: ``{src, dst, kind, meta{}, origin}``

Kinds (nodes): project|doc|part|assembly|body|feature|sketch|mate|bom_item|
               drawing|sim_study|board|firmware
Kinds (edges): CONTAINS|DEPENDS_ON|MATES|INSTANCE_OF|REFERENCES|DERIVES_FROM|
               DOCUMENTS|SIMULATES|MANUFACTURES

``origin`` is always ``extracted`` (deterministic from doc data) or
``inferred`` (heuristic). Cache lives at ``<project>/.plm/graph.json.gz`` and is
keyed by source-file mtimes; recompute is always allowed.

Walk policy
-----------
* Always skip VCS/cache dirs (``.aicadvault``, ``.plm``, ``.git``, …). Nested
  ``.aicadvault`` shadow trees are skipped so a nested vault is never
  double-counted; nested vault *working trees* (the real CAD files beside the
  shadow dir) are still walked — they are live design content.
* Archive/stale dirs (``_archive_stale``, ``_archive``, ``archive``, ``.trash``
  by default; override via ``exclude_dirs`` / ``CHUDWARE_GRAPH_EXCLUDE_DIRS``) are
  pruned from the walk so large project trees don't bloat the graph. Pass
  ``include_archive=True`` to keep them (nodes then carry ``meta.archive``).

Size guards
-----------
``get_graph`` caps the returned payload (``max_nodes`` / ``max_edges``, defaults
below) and reports ``truncated`` + full ``node_count``/``edge_count`` so the UI
can show "showing N of M". The on-disk cache stores the full (archive-filtered)
graph; caps apply only on the response path. Cache writes are atomic
(tmp + rename). A fast path serves the cache when its mtime is newer than every
source ``.step+``/``.ecad+`` and the overlay, without re-reading containers.

AI-VC seam: :func:`on_ai_vc_checkpoint` is the rebuild hook CheckpointsCore
calls after a successful checkpoint (best-effort; never raises into AI-VC).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import zlib
import json
import os
import re
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from services.plm import snapshot

ORIGIN_EXTRACTED = "extracted"
ORIGIN_INFERRED = "inferred"

# assembly best-practice advisor thresholds (assembly_advice)
_SUBASM_MIN_CLUSTER = 3
_FLAT_ASM_MAX = 12
_MULTIBODY_MAX = 2

# Response payload caps for cad_graph_get / UI (full graph still cached).
DEFAULT_MAX_NODES = 5000
DEFAULT_MAX_EDGES = 10000

# Bound both stored and expanded cache bytes before JSON parsing. Compression
# keeps legitimate large graphs cacheable without dropping any nodes or edges.
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_EXPANDED_BYTES = 1024 * 1024 * 1024
# Cap how many co-owners of the same bare dim+value form a complete
# REFERENCES clique. Without this, a common bool/flag (merge=true across
# hundreds of extrudes) emits O(n²) edges and bloats the on-disk cache.
MAX_SHARED_VALUE_GROUP = 24
# Cap doc-node meta.mat (distinct material names). _sanitize_meta already
# hard-bounds any list to 8 entries, so capping here at the same number
# avoids a silently-truncated (and therefore unsorted-looking) list.
MAX_DOC_MATERIALS = 8

# Non-CAD files surfaced as `file` graph nodes (contracts/cad_graph.md 2026-07
# addendum): office documents, text, images, 3D-exchange and fab formats.
# Metadata only — content extraction is config-gated (office_content).
_FILE_EXTS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
    ".csv", ".md", ".txt", ".rst",
    ".png", ".jpg", ".jpeg", ".svg", ".bmp", ".webp",
    ".step", ".stp", ".iges", ".igs", ".stl", ".obj", ".3mf",
    ".gbr", ".drl",
})
# Hard cap on file nodes per project (token/cache guard); overflow is reported
# as `files_truncated` on the graph dict.
MAX_FILE_NODES = 500
# Office text extraction caps (used only when graph_config office_content=on).
MAX_OFFICE_MENTIONS = 20     # REFERENCES edges per office file
MAX_TEXT_PREVIEW = 300       # meta.text_preview chars
# part-number-shaped tokens (PRJ-0042) detected in office text
_OFFICE_PN_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,5}\b")



# Dir basenames always pruned from the project walk (shadow/cache/VCS).
# Nested .aicadvault is intentionally here — working trees beside it stay.
_SYSTEM_SKIP_DIRS = frozenset({
    ".aicadvault", ".plm", ".git", "__pycache__", "node_modules",
    ".venv", "venv",
})

# Archive/stale dir basenames excluded by default (case-sensitive match on the
# directory name as it appears on disk). Overridable via exclude_dirs kwarg or
# CHUDWARE_GRAPH_EXCLUDE_DIRS (os.pathsep-separated).
DEFAULT_ARCHIVE_DIR_NAMES = frozenset({
    "_archive_stale", "_archive", "archive", ".trash",
})

NODE_KINDS = frozenset({
    "project", "doc", "part", "assembly", "body", "feature", "sketch",
    "mate", "bom_item", "drawing", "sim_study", "board", "firmware",
    "equation", "file", "keyword",
})
EDGE_KINDS = frozenset({
    "CONTAINS", "DEPENDS_ON", "MATES", "INSTANCE_OF", "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS", "SIMULATES", "MANUFACTURES", "TAGGED",
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


def _archive_dir_names(
    exclude_dirs: Iterable[str] | None = None,
) -> frozenset[str]:
    """Resolve the archive/stale dir-name set (defaults + env override)."""
    if exclude_dirs is not None:
        return frozenset(str(d) for d in exclude_dirs if d)
    env = os.environ.get("CHUDWARE_GRAPH_EXCLUDE_DIRS", "").strip()
    if env:
        parts = [p for p in env.split(os.pathsep) if p]
        if parts:
            return frozenset(parts)
    return DEFAULT_ARCHIVE_DIR_NAMES


def _skip_dir_names(
    *,
    include_archive: bool = False,
    exclude_dirs: Iterable[str] | None = None,
) -> frozenset[str]:
    """Dir basenames to prune from os.walk (system + optional archive set)."""
    names = set(_SYSTEM_SKIP_DIRS)
    if not include_archive:
        names |= set(_archive_dir_names(exclude_dirs))
    return frozenset(names)


def path_is_archive(
    rel_path: str | None,
    archive_names: Iterable[str] | None = None,
) -> bool:
    """True when any path segment is an archive/stale dir basename."""
    if not rel_path:
        return False
    names = frozenset(archive_names) if archive_names is not None \
        else DEFAULT_ARCHIVE_DIR_NAMES
    for part in str(rel_path).replace("\\", "/").split("/"):
        if part in names:
            return True
    return False

# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def build_graph(
    project_dir: str | os.PathLike | None = None,
    *,
    open_docs: dict[str, Any] | None = None,
    force: bool = False,
    include_archive: bool = False,
    exclude_dirs: Iterable[str] | None = None,
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
    include_archive
        When False (default), prune archive/stale directories from the walk.
        When True, keep them; nodes under those dirs get ``meta.archive=True``.
    exclude_dirs
        Optional override for archive dir basenames (defaults:
        :data:`DEFAULT_ARCHIVE_DIR_NAMES` / ``CHUDWARE_GRAPH_EXCLUDE_DIRS``).
    """
    root = Path(project_dir).resolve() if project_dir else None
    archive_names = _archive_dir_names(exclude_dirs)
    skip_names = _skip_dir_names(
        include_archive=include_archive, exclude_dirs=exclude_dirs)

    # Fast path: serve cache when fresher than every source mtime — avoids
    # re-reading every .step+ container on large project trees. Skipped when
    # open_docs are merged (those are ephemeral and not on disk).
    if (not force and root is not None
            and not (open_docs or {})):
        cached = _load_cache_if_valid(
            root,
            include_archive=include_archive,
            archive_names=sorted(archive_names),
        )
        if cached is not None:
            return cached

    # Freshness tracks every candidate, even files excluded by the existing
    # display cap or currently unreadable containers. Otherwise a large project
    # writes a keyset its own fast scan can never validate. Capture BEFORE reads
    # so a concurrent edit invalidates the next request rather than going stale.
    source_states: dict[str, list[int]] = {}
    source_mtimes = (_source_mtimes_fast(root, skip_names=skip_names,
                                        source_states=source_states) if root else {})

    sources = _collect_sources(
        root, open_docs or {}, skip_names=skip_names,
        archive_names=archive_names, include_archive=include_archive)
    # Non-CAD file-node records participate in cache validation exactly like
    # container sources (same file:<rel> key namespace, disjoint by suffix).
    file_recs: list[dict[str, Any]] = []
    files_truncated = 0
    if root is not None:
        file_recs = _collect_files(
            root, skip_names=skip_names, include_archive=include_archive)
        if len(file_recs) > MAX_FILE_NODES:
            files_truncated = len(file_recs) - MAX_FILE_NODES
            file_recs = file_recs[:MAX_FILE_NODES]

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    # (src, dst) pairs of DEPENDS_ON edges — O(1) dedup for the per-feature
    # history check below (was an O(len(edges)) scan per feature).
    dep_pairs: set[tuple[str, str]] = set()

    def add_node(n: dict[str, Any]) -> str:
        nid = n["id"]
        if nid in node_ids:
            return nid
        if "origin" not in n:
            n["origin"] = ORIGIN_EXTRACTED
        n["meta"] = _sanitize_meta(n.get("meta") or {})
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
        if kind == "DEPENDS_ON":
            dep_pairs.add((src, dst))

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
        is_arch = bool(src.get("archive"))
        doc_meta: dict[str, Any] = {"schema": schema, "uuid": doc_uuid}
        if src.get("mtime") is not None:
            doc_meta["mt"] = src["mtime"]  # source mtime (cheap staleness)
        if src.get("doc_hash"):
            doc_meta["dh"] = src["doc_hash"]  # content hash (true freshness)
        if is_arch:
            doc_meta["archive"] = True
        un = _doc_units(doc)
        if un:
            doc_meta["un"] = un  # display unit system ("ips" | "mmgs")
        mats = _doc_materials(doc)
        if mats:
            doc_meta["mat"] = mats  # distinct material names (doc_schema.md §1)
        add_node({
            "id": dnode_id, "kind": node_kind, "label": name,
            "path": rel or None, "doc_id": doc_id,
            "meta": doc_meta,
            "origin": ORIGIN_EXTRACTED,
        })
        add_edge(proj_id, dnode_id, "CONTAINS")
        if rel:
            path_to_doc_node[_norm_path(rel)] = dnode_id
            path_to_doc_node[_stem(rel)] = dnode_id
        if doc_uuid:
            uuid_to_doc_node[doc_uuid] = dnode_id

        # equations → equation nodes + shared-dim index (must precede the
        # feature loop so expression-driven DERIVES_FROM edges can resolve).
        eq_nodes: dict[str, str] = {}
        globals_ = (doc.get("equations") or {}).get("globals") or []
        for eq in globals_:
            if not isinstance(eq, dict):
                continue
            ename = str(eq.get("name") or "")
            if not ename:
                continue
            eexpr = eq.get("expr")
            eval_ = eq.get("value", eexpr)
            unit = eq.get("unit")
            enode = f"eq:{dnode_id}:{ename}"
            add_node({
                "id": enode, "kind": "equation", "label": ename,
                "path": rel or None, "doc_id": doc_id,
                "meta": {"expr": eexpr, "value": eq.get("value"), "unit": unit},
                "origin": ORIGIN_EXTRACTED,
            })
            add_edge(dnode_id, enode, "CONTAINS")
            eq_nodes[ename] = enode
            # keep the cross-doc shared-value index keyed by bare name
            eq_index.setdefault(ename, []).append((dnode_id, eval_, unit))
        # eq → eq DERIVES_FROM for globals that reference other globals
        for eq in globals_:
            if not isinstance(eq, dict):
                continue
            ename = str(eq.get("name") or "")
            src_node = eq_nodes.get(ename)
            if not src_node:
                continue
            for ref in _expr_names(eq.get("expr")):
                tgt = eq_nodes.get(ref)
                if tgt and tgt != src_node:
                    add_edge(src_node, tgt, "DERIVES_FROM",
                             meta={"expr": eq.get("expr"), "via": "equation"})

        # features + sketches
        feat_nodes: dict[str, str] = {}
        pending_ext: list[tuple[str, Any, Any, bool]] = []
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
                # cross-doc derived — resolved by the __resolve__ second pass
                src_doc = df.get("doc_id") or df.get("doc_name")
                src_feat = df.get("feature") or df.get("source_feature")
                if src_doc and src_feat:
                    edges.append({
                        "src": fnode, "dst": f"__resolve__:{src_doc}",
                        "kind": "DERIVES_FROM",
                        "meta": {"feature": src_feat, "via": "derived_from"},
                        "origin": ORIGIN_EXTRACTED,
                    })
            # dimensional links: shared-value index + expression-driven edges
            for dkey, dval, dunit, dexpr in _feature_dims(feat):
                dim_owners.setdefault(dkey, []).append(fnode)
                eq_index.setdefault(dkey, []).append((fnode, dval, dunit))
                if dexpr:
                    for ref in _expr_names(dexpr):
                        tgt = eq_nodes.get(ref)
                        if tgt:
                            add_edge(fnode, tgt, "DERIVES_FROM",
                                     meta={"dim": dkey, "expr": dexpr,
                                           "value": dval, "unit": dunit})
            # external refs (associative convert-entities) → REFERENCES
            fparams = feat.get("params") if isinstance(feat.get("params"), dict) else {}
            ext = fparams.get("external_refs")
            if not isinstance(ext, list):
                ext = feat.get("external_refs")
            if isinstance(ext, list):
                for xr in ext:
                    if not isinstance(xr, dict):
                        continue
                    xsrc = xr.get("source")
                    if not isinstance(xsrc, dict):
                        continue
                    sel = xsrc.get("selector")
                    owner = sel.get("owner_feature") if isinstance(sel, dict) else None
                    owner = owner or xsrc.get("feature") or xsrc.get("owner_feature")
                    cross = xsrc.get("doc") or xsrc.get("doc_id") or xsrc.get("path")
                    pending_ext.append(
                        (fnode, owner, cross, bool(xr.get("stale"))))

        # history order DEPENDS_ON (feature N depends on N-1) — inferred
        feat_list = [f for f in (doc.get("features") or []) if isinstance(f, dict) and f.get("id")]
        for i in range(1, len(feat_list)):
            a = feat_nodes.get(str(feat_list[i]["id"]))
            b = feat_nodes.get(str(feat_list[i - 1]["id"]))
            if a and b:
                # only add if not already DEPENDS_ON via explicit ref
                if (a, b) not in dep_pairs:
                    add_edge(a, b, "DEPENDS_ON",
                             meta={"via": "history"}, origin=ORIGIN_INFERRED)

        # resolve external refs now that this doc's feat_nodes is complete
        for xfnode, xowner, xcross, xstale in pending_ext:
            if xowner and str(xowner) in feat_nodes:
                add_edge(xfnode, feat_nodes[str(xowner)], "REFERENCES",
                         meta={"via": "external_ref", "stale": xstale})
            elif xcross:
                edges.append({
                    "src": xfnode, "dst": f"__resolve__:{xcross}",
                    "kind": "REFERENCES",
                    "meta": {"via": "external_ref", "stale": xstale},
                    "origin": ORIGIN_EXTRACTED,
                })

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

        # mates (assemblies)
        for mate in doc.get("mates") or []:
            if not isinstance(mate, dict):
                continue
            mid = str(mate.get("id") or "")
            if not mid:
                continue
            mtype = str(mate.get("type") or mate.get("kind") or "mate")
            mval_raw = mate.get("value")
            if isinstance(mval_raw, dict):
                mval = mval_raw.get("value")
                mexpr = mval_raw.get("expr")
                munit = mval_raw.get("unit") or mate.get("unit")
            elif isinstance(mval_raw, (int, float)):
                mval, mexpr, munit = mval_raw, None, mate.get("unit")
            else:
                mval, mexpr, munit = None, None, mate.get("unit")
            mnode = f"mate:{dnode_id}:{mid}"
            add_node({
                "id": mnode, "kind": "mate", "label": mtype,
                "path": rel or None, "doc_id": doc_id,
                "meta": {
                    "mate_id": mid, "type": mtype,
                    "suppressed": bool(mate.get("suppressed")),
                    "refs": mate.get("refs") or [],
                    "value": mval, "unit": munit, "expr": mexpr,
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
                add_edge(mnode, cnode, "MATES",
                         meta={"type": mtype, "value": mval,
                               "unit": munit, "expr": mexpr})
            # mate expr referencing a global → DERIVES_FROM
            if mexpr:
                for ref in _expr_names(mexpr):
                    tgt = eq_nodes.get(ref)
                    if tgt:
                        add_edge(mnode, tgt, "DERIVES_FROM",
                                 meta={"expr": mexpr, "value": mval,
                                       "unit": munit})

        # components → INSTANCE_OF + BOM
        bom_counts: dict[str, int] = {}
        for comp in doc.get("components") or []:
            if not isinstance(comp, dict):
                continue
            cid = str(comp.get("id") or "")
            cref = _component_ref(comp)
            clabel = str(comp.get("name") or cref or cid or "component")
            cnode = f"instance:{dnode_id}:{cid or clabel}"
            cmeta: dict[str, Any] = {
                "component_id": cid, "ref": cref,
                "suppressed": bool(comp.get("suppressed")),
                "fixed": bool(comp.get("fixed")),
            }
            # World placement (row-major 4x4; translation at 3/7/11) — lets
            # the graph 3D mode lay instances out like the actual model.
            ctr = comp.get("transform")
            if (isinstance(ctr, list) and len(ctr) == 16
                    and all(isinstance(v, (int, float)) for v in ctr)):
                cmeta["wp"] = [float(ctr[3]), float(ctr[7]), float(ctr[11])]
            add_node({
                "id": cnode, "kind": "doc",
                "label": clabel, "path": rel or None, "doc_id": doc_id,
                "meta": cmeta,
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

        # drawing → DOCUMENTS source / cam → MANUFACTURES source
        if str(schema).split("/", 1)[0] == "aicad.cam":
            for key in ("source", "source_doc", "part", "stock"):
                sref = doc.get(key)
                if isinstance(sref, dict):
                    sref = (sref.get("from_solid") if key == "stock"
                            else (sref.get("path") or sref.get("doc_id")))
                if isinstance(sref, str) and sref:
                    edges.append({
                        "src": dnode_id, "dst": f"__resolve__:{sref}",
                        "kind": "MANUFACTURES",
                        "meta": {"via": key}, "origin": ORIGIN_EXTRACTED,
                    })
                    break
        else:
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
        _walk_project_folders(
            root, proj_id, add_node, add_edge, path_to_doc_node,
            skip_names=skip_names)

    # --- non-CAD files → `file` nodes (+ config-gated office content) ------
    if root is not None:
        items_by_path = _manifest_items_by_path(root)
        # enrich path-backed doc nodes with PLM item identity (part number
        # mentions in office text resolve against these)
        if items_by_path:
            for n in nodes:
                it = items_by_path.get(str(n.get("path") or ""))
                if not it:
                    continue
                meta = n.setdefault("meta", {})
                if it.get("item_id"):
                    meta.setdefault("item_id", it["item_id"])
                if it.get("part_number"):
                    meta.setdefault("part_number", it["part_number"])
                if it.get("lifecycle"):
                    meta.setdefault("lifecycle", it["lifecycle"])
    if root is not None and file_recs:
        try:
            from services.plm import graph_config as _gcfg
            cfg = _gcfg.get_config(root)
        except Exception:  # noqa: BLE001
            cfg = {}
        office_on = bool(cfg.get("office_content"))
        office_kinds = set(cfg.get("office_kinds") or [])
        office_max = int(cfg.get("office_max_chars") or 4000)
        # mention index: path stem / part number → node ids
        mention_index: dict[str, list[str]] = {}
        for n in nodes:
            p = n.get("path")
            if p:
                mention_index.setdefault(
                    Path(str(p)).stem.lower(), []).append(n["id"])
            pn = (n.get("meta") or {}).get("part_number")
            if pn:
                mention_index.setdefault(str(pn).lower(), []).append(n["id"])
        pending_mentions: list[tuple[str, str]] = []
        for rec in file_recs:
            rel = rec["path"]
            nid = f"file:{rel}"
            fmeta: dict[str, Any] = {
                "ext": rec["ext"], "size_bytes": rec["size"],
                "mt": rec["mtime"],
                "folder": str(Path(rel).parent).replace("\\", "/")
                if "/" in rel else "",
            }
            item = items_by_path.get(rel)
            if item:
                fmeta["item_id"] = item.get("item_id")
                if item.get("part_number"):
                    fmeta["part_number"] = item["part_number"]
                if item.get("lifecycle"):
                    fmeta["lifecycle"] = item["lifecycle"]
            # office content extraction (config-gated, never fatal)
            mention_terms: list[str] = []
            if office_on and rec["ext"] in office_kinds:
                ext_res = _extract_office_text(root / rel, office_max)
                if ext_res is None:
                    fmeta["extract_error"] = True
                else:
                    text = ext_res.get("text") or ""
                    if text:
                        fmeta["text_preview"] = re.sub(
                            r"\s+", " ", text).strip()[:MAX_TEXT_PREVIEW]
                        mention_terms = sorted(set(
                            t.lower() for t in _OFFICE_PN_RE.findall(text)))
                    fmeta["word_count"] = len(text.split())
            add_node({
                "id": nid, "kind": "file",
                "label": Path(rel).name, "path": rel, "doc_id": None,
                "meta": fmeta, "origin": ORIGIN_EXTRACTED,
            })
            add_edge(proj_id, nid, "CONTAINS")
            mention_index.setdefault(Path(rel).stem.lower(), []).append(nid)
            if fmeta.get("part_number"):
                mention_index.setdefault(
                    str(fmeta["part_number"]).lower(), []).append(nid)
            for term in mention_terms[:MAX_OFFICE_MENTIONS]:
                pending_mentions.append((nid, term))
        for src_nid, term in pending_mentions:
            tgts = [t for t in mention_index.get(term, []) if t != src_nid]
            if tgts:
                add_edge(src_nid, tgts[0], "REFERENCES",
                         meta={"via": "office_mention", "term": term},
                         origin=ORIGIN_INFERRED)

    # --- keyword overlay → `keyword` nodes + TAGGED edges ------------------
    if root is not None:
        try:
            from services.plm import keywords as _kwmod
            kw_entries = _kwmod.list_entries(root)
        except Exception:  # noqa: BLE001
            kw_entries = []
        if kw_entries:
            node_by_id = {n["id"]: n for n in nodes}
            tagged_keywords: dict[str, list[str]] = {}
            for ent in kw_entries:
                target = str(ent.get("target") or "")
                keyword = str(ent.get("keyword") or "")
                if not target or not keyword:
                    continue
                # resolve: node id, else project-relative file/doc path
                tnid = target if target in node_ids else None
                if tnid is None and f"file:{target}" in node_ids:
                    tnid = f"file:{target}"
                if tnid is None and f"doc:{target}" in node_ids:
                    tnid = f"doc:{target}"
                if tnid is None:
                    continue  # unknown endpoint — dropped like overlay links
                kw_nid = f"kw:{keyword}"
                if kw_nid not in node_ids:
                    add_node({
                        "id": kw_nid, "kind": "keyword", "label": keyword,
                        "path": None, "doc_id": None, "meta": {},
                        "origin": str(ent.get("origin") or "manual"),
                    })
                add_edge(kw_nid, tnid, "TAGGED",
                         meta={"entry_id": ent.get("id")},
                         origin=str(ent.get("origin") or "manual"))
                tagged_keywords.setdefault(tnid, []).append(keyword)
            for tnid, kws in tagged_keywords.items():
                tgt_node = node_by_id.get(tnid)
                if tgt_node is not None:
                    tgt_node.setdefault("meta", {})["keywords"] = \
                        sorted(set(kws))[:8]

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

    # Propagate instance world positions (meta.wp) onto the docs they
    # instance — first instance wins (edges are in deterministic load
    # order). Lets the 3D graph view place PART/ASM docs at their real
    # model coordinates, not just instance nodes.
    node_by_id = {n["id"]: n for n in nodes}
    for e in edges:
        if e["kind"] != "INSTANCE_OF":
            continue
        src_n = node_by_id.get(e["src"])
        dst_n = node_by_id.get(e["dst"])
        if not src_n or not dst_n:
            continue
        wp = (src_n.get("meta") or {}).get("wp")
        if wp and "wp" not in (dst_n.get("meta") or {}):
            dst_n.setdefault("meta", {})["wp"] = list(wp)

    # --- shared dims / layouts → REFERENCES --------------------------------
    for ename, owners in eq_index.items():
        # group by (value, unit) — same name+value across docs = shared
        by_val: dict[tuple[str, str | None], list[str]] = {}
        for nid, val, unit in owners:
            # bool is a subclass of int — never treat flags as shared dims
            if isinstance(val, bool):
                continue
            key = (json.dumps(val, sort_keys=True, default=str), unit)
            by_val.setdefault(key, []).append(nid)
        for (val_s, unit), nids in by_val.items():
            uniq = list(dict.fromkeys(nids))  # stable unique
            if len(uniq) < 2:
                continue
            # Pathological cliques (e.g. merge=true on every extrude) must not
            # emit a complete graph — keep a bounded sample so blast/path still
            # see the shared-value signal without O(n²) cache blow-up.
            if len(uniq) > MAX_SHARED_VALUE_GROUP:
                uniq = uniq[:MAX_SHARED_VALUE_GROUP]
            try:
                val = json.loads(val_s)
            except Exception:  # noqa: BLE001
                val = val_s
            # heuristic name+value coincidence — inferred, not extracted truth
            for i, a in enumerate(uniq):
                for b in uniq[i + 1:]:
                    meta = {"dim": ename, "value": val, "unit": unit,
                            "via": "shared_value"}
                    add_edge(a, b, "REFERENCES", meta=meta, origin=ORIGIN_INFERRED)
                    add_edge(b, a, "REFERENCES", meta=meta, origin=ORIGIN_INFERRED)

    # --- manual / AI-authored overlay links --------------------------------
    if root is not None:
        try:
            from services.plm import graph_links as _gl
            for lk in _gl.list_links(root):
                lsrc = lk.get("src")
                ldst = lk.get("dst")
                if lsrc in node_ids and ldst in node_ids:
                    add_edge(lsrc, ldst, str(lk.get("kind")),
                             meta={"note": lk.get("note") or "",
                                   "link_id": lk.get("id")},
                             origin=str(lk.get("origin") or "manual"))
        except Exception:  # noqa: BLE001
            pass

    for _e in edges:
        _e["meta"] = _sanitize_meta(_e.get("meta") or {})

    # every node carries its owning project root (cross-project merge key)
    if root is not None:
        for _n in nodes:
            _n.setdefault("meta", {}).setdefault("project", str(root))

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    graph = {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "generated_at": generated_at,
        "project_dir": str(root) if root else None,
        "mtimes": {**source_mtimes,
                   **{s["key"]: s["mtime"] for s in sources
                      if str(s.get("key", "")).startswith("open:")}},
        "source_states": source_states,
        "filter": {
            "include_archive": bool(include_archive),
            "exclude_dirs": sorted(archive_names),
            "skipped_system_dirs": sorted(_SYSTEM_SKIP_DIRS),
        },
    }
    if files_truncated:
        graph["files_truncated"] = files_truncated
    if root is not None:
        _save_cache(root, graph)
    return graph


def get_graph(
    project_dir: str | os.PathLike | None = None,
    *,
    doc_id: str | None = None,
    open_docs: dict[str, Any] | None = None,
    force: bool = False,
    include_archive: bool = False,
    exclude_dirs: Iterable[str] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    if_mtimes_max: float | None = None,
    if_sources_count: int | None = None,
    linked: bool = False,
) -> dict[str, Any]:
    """``cad_graph_get`` implementation.

    Caps the returned node/edge lists (defaults: :data:`DEFAULT_MAX_NODES` /
    :data:`DEFAULT_MAX_EDGES`) and always reports honest counts via
    ``node_count`` / ``edge_count`` + ``truncated``. Pass ``max_nodes=0`` or
    ``max_edges=0`` to disable a cap. Archive dirs are excluded by default
    (see :func:`build_graph`).

    When ``linked`` is true the primary project's graph is merged with every
    project in its ``linked_projects`` config list (:func:`merge_linked_graphs`);
    the response carries ``linked_projects: [root paths]``.

    Change probe: when ``if_mtimes_max`` is given (the ``sources_mtime_max``
    from a previous full response, optionally with ``if_sources_count``) and
    the on-disk cache is still fresh with an identical newest-source mtime
    and source count, returns ``{"ok": True, "unchanged": True}`` without
    reading the cache payload — cheap enough for polling UIs. The count
    catches deletions / old-mtime additions that max(mtime) alone misses.
    NOTE: unlike _load_cache_if_valid this probe does NOT re-check the
    recorded filter policy (_filter_matches); it is only valid for callers
    that hold include_archive/exclude_dirs fixed between the full response
    and the probe (the PLM UI toggles policy only via force-refresh).
    Any doubt (cache stale/missing, fingerprint mismatch) falls through to
    the full path.
    """
    if (if_mtimes_max is not None and not force and not doc_id
            and not open_docs and not linked and project_dir is not None):
        root = Path(project_dir)
        cp = _cache_path(root)
        try:
            cache_stat = cp.stat()
            cache_mtime = cache_stat.st_mtime
            cache_mtime_ns = cache_stat.st_mtime_ns
        except OSError:
            cache_mtime = 0.0
            cache_mtime_ns = 0
        if cache_mtime > 0.0:
            skip = _skip_dir_names(include_archive=include_archive)
            source_states: dict[str, list[int]] = {}
            fast = _source_mtimes_fast(root, skip_names=skip, source_states=source_states)
            newest = max(fast.values()) if fast else 0.0
            n_files = sum(1 for k in fast
                          if not str(k).startswith("overlay:")
                          and not str(k).startswith("open:"))
            count_ok = (if_sources_count is None
                        or n_files == int(if_sources_count))
            if (cache_mtime + 1e-6 >= newest
                    and all(state[1] <= cache_mtime_ns for state in source_states.values())
                    and abs(newest - float(if_mtimes_max)) <= 1e-6
                    and count_ok):
                return {"ok": True, "unchanged": True,
                        "sources_mtime_max": newest,
                        "sources_count": n_files}
    g = build_graph(
        project_dir, open_docs=open_docs, force=force,
        include_archive=include_archive, exclude_dirs=exclude_dirs,
    )
    # build_graph's fast scan (fresh, or the cache's mtimes after it validated
    # them against an identical scan). Captured before the linked merge.
    primary_mtimes = g.get("mtimes")
    linked_dirs: list[str] = []
    if linked and project_dir is not None:
        g, linked_dirs = _merge_with_linked(Path(project_dir), g)
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

    nodes = list(g.get("nodes") or [])
    edges = list(g.get("edges") or [])
    node_count = len(nodes)
    edge_count = len(edges)

    n_cap = DEFAULT_MAX_NODES if max_nodes is None else int(max_nodes)
    e_cap = DEFAULT_MAX_EDGES if max_edges is None else int(max_edges)
    # 0 / negative = uncapped
    if n_cap > 0 and len(nodes) > n_cap:
        nodes = nodes[:n_cap]
    if e_cap > 0 and len(edges) > e_cap:
        # drop edges whose endpoints fell off the node cap, then slice
        keep_ids = {n["id"] for n in nodes}
        edges = [e for e in edges
                 if e.get("src") in keep_ids and e.get("dst") in keep_ids]
        if len(edges) > e_cap:
            edges = edges[:e_cap]

    truncated = len(nodes) < node_count or len(edges) < edge_count
    # Change-probe fingerprint: newest source mtime via the SAME code path
    # the probe uses (_source_mtimes_fast — every .step+/.ecad+ under the
    # request's skip policy, plus the overlay), so equality can actually
    # hold. With the default skip policy build_graph already ran exactly that
    # scan (its "mtimes" minus open: keys), so reuse it instead of walking the
    # tree a second time.
    sources_mtime_max = 0.0
    sources_count = 0
    if project_dir is not None:
        if exclude_dirs is None and isinstance(primary_mtimes, dict):
            fast = {k: v for k, v in primary_mtimes.items()
                    if not str(k).startswith("open:")}
        else:
            skip = _skip_dir_names(include_archive=include_archive)
            fast = _source_mtimes_fast(Path(project_dir), skip_names=skip)
        sources_mtime_max = max(fast.values()) if fast else 0.0
        sources_count = sum(1 for k in fast
                            if not str(k).startswith("overlay:")
                            and not str(k).startswith("open:"))
    else:
        src_mtimes = [v for k, v in (g.get("mtimes") or {}).items()
                      if not str(k).startswith("open:") and isinstance(v, (int, float))]
        sources_mtime_max = max(src_mtimes) if src_mtimes else 0.0
        sources_count = len(src_mtimes)
    out = {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "generated_at": g.get("generated_at"),
        "project_dir": g.get("project_dir"),
        "node_count": node_count,
        "edge_count": edge_count,
        "truncated": truncated,
        "sources_mtime_max": sources_mtime_max,
        "sources_count": sources_count,
        "filter": g.get("filter") or {
            "include_archive": bool(include_archive),
            "exclude_dirs": sorted(_archive_dir_names(exclude_dirs)),
        },
    }
    if linked_dirs:
        out["linked_projects"] = linked_dirs
    if g.get("files_truncated"):
        out["files_truncated"] = g["files_truncated"]
    return out


def assembly_tree(
    project_dir: str | os.PathLike | None = None,
    *,
    doc_id: str | None = None,
    include_archive: bool = False,
    max_depth: int = 12,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``cad_graph_assembly_tree`` — nested assembly decomposition.

    Walks ``doc CONTAINS bom_item INSTANCE_OF doc`` chains into a tree:
    ``{ok, trees:[{id, label, kind, count, path?, children:[…]}]}``.
    ``count`` = instance quantity of that child within its parent (1 for the
    root). Roots: *doc_id* when given, else every assembly that no other
    assembly instances (all assemblies as a cyclic fallback). Children of a
    non-assembly doc are not expanded (parts are leaves). Cycle-guarded and
    depth-capped so malformed graphs still terminate.
    """
    g = graph or build_graph(project_dir, include_archive=include_archive)
    nodes = {n["id"]: n for n in g.get("nodes") or [] if n.get("id")}

    contains_kids: dict[str, list[str]] = {}
    instance_of: dict[str, list[str]] = {}
    instanced_targets: set[str] = set()
    for e in g.get("edges") or []:
        k = e.get("kind")
        if k == "CONTAINS":
            contains_kids.setdefault(e["src"], []).append(e["dst"])
        elif k == "INSTANCE_OF":
            instance_of.setdefault(e["src"], []).append(e["dst"])
            if (nodes.get(e["src"]) or {}).get("kind") == "bom_item":
                instanced_targets.add(e["dst"])

    def children_of(doc: str, seen: frozenset[str], depth: int) -> list[dict]:
        if depth >= max_depth:
            return []
        # Aggregate bom_item INSTANCE_OF targets (count = instances).
        counts: dict[str, int] = {}
        order: list[str] = []
        for kid in contains_kids.get(doc, []):
            kn = nodes.get(kid)
            if kn is None:
                continue
            if kn["kind"] == "bom_item":
                for tgt in instance_of.get(kid, []):
                    if tgt not in nodes or tgt == doc:
                        continue
                    if tgt not in counts:
                        order.append(tgt)
                    counts[tgt] = counts.get(tgt, 0) + 1
            elif kn["kind"] in ("part", "assembly", "doc"):
                if kid not in counts:
                    order.append(kid)
                counts[kid] = counts.get(kid, 0) + 1
        out = []
        for tgt in order:
            tn = nodes[tgt]
            entry = {
                "id": tgt,
                "label": tn.get("label") or tgt,
                "kind": tn.get("kind"),
                "count": counts[tgt],
            }
            if tn.get("path"):
                entry["path"] = tn["path"]
            if tn.get("kind") == "assembly" and tgt not in seen:
                entry["children"] = children_of(
                    tgt, seen | {tgt}, depth + 1)
            out.append(entry)
        return out

    def tree_for(doc: str) -> dict | None:
        n = nodes.get(doc)
        if n is None:
            return None
        t = {"id": doc, "label": n.get("label") or doc,
             "kind": n.get("kind"), "count": 1,
             "children": children_of(doc, frozenset({doc}), 0)}
        if n.get("path"):
            t["path"] = n["path"]
        return t

    if doc_id:
        t = tree_for(doc_id)
        if t is None:
            return {"ok": False, "reason": "doc_id not in graph",
                    "hint": "pass an assembly/part node id", "trees": []}
        return {"ok": True, "trees": [t]}

    # Roots: assemblies nothing else instances; fallback = every assembly.
    assemblies = [n["id"] for n in nodes.values() if n.get("kind") == "assembly"]
    roots = [a for a in assemblies if a not in instanced_targets] or assemblies
    trees = [t for a in sorted(roots) if (t := tree_for(a)) is not None]
    return {"ok": True, "trees": trees}


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


# ---------------------------------------------------------------------------
# cross-project merge (contracts/cad_graph.md 2026-07 cross-project addendum)
# ---------------------------------------------------------------------------


def resolve_node_ref(
    ref: str,
    project_dir: str | os.PathLike,
    linked_dirs: Iterable[str | os.PathLike] | None = None,
) -> tuple[str, str] | None:
    """Resolve a possibly-qualified node reference to ``(project_dir, local_id)``.

    ``<project_key>/<local_node_id>`` addresses a node in the primary project
    or any of *linked_dirs* (matched via :func:`graph_config.project_key`);
    anything else is a local id in *project_dir*. Returns None when the key
    matches no known project.
    """
    from services.plm import graph_config as gcfg
    ref = str(ref or "").strip()
    if not ref:
        return None
    root = Path(project_dir)
    candidates = [root] + [Path(d) for d in (linked_dirs or [])]
    if "/" in ref:
        key, _, local = ref.partition("/")
        if local:
            for d in candidates:
                try:
                    if gcfg.project_key(d) == key:
                        return (str(d), local)
                except Exception:  # noqa: BLE001
                    continue
            # fall through — unknown key is not a local id either
            return None
    return (str(root), ref)


def merge_linked_graphs(
    primary: dict[str, Any],
    linked: list[tuple[str, dict[str, Any]]],
    *,
    primary_dir: str | None = None,
) -> dict[str, Any]:
    """Merge *linked* ``(project_key, graph)`` pairs into *primary*'s graph.

    Node ids stay local per project. ``kw:`` keyword nodes with the same id
    intentionally merge (a shared keyword is one web); any other id collision
    gets the non-primary copy prefixed ``<project_key>/``. Qualified overlay
    links stored in the primary project (``<project_key>/<node_id>``) are
    resolved against the merged id space and appended as edges.
    """
    nodes = [dict(n) for n in primary.get("nodes") or []]
    edges = [dict(e) for e in primary.get("edges") or []]
    node_ids = {n["id"] for n in nodes}
    key_by_dir: dict[str, str] = {}
    for key, g in linked:
        proj_root = str(g.get("project_dir") or "")
        if proj_root:
            key_by_dir[proj_root] = key
        id_map: dict[str, str] = {}
        for n in g.get("nodes") or []:
            nid = str(n.get("id") or "")
            if not nid:
                continue
            if nid in node_ids and not nid.startswith("kw:"):
                mapped = f"{key}/{nid}"
            else:
                mapped = nid  # new id, or kw: merge into the shared node
            id_map[nid] = mapped
            if mapped in node_ids:
                continue  # kw: merge — keep the existing shared node
            nn = dict(n)
            nn["id"] = mapped
            nn.setdefault("meta", {})
            if proj_root:
                nn["meta"]["project"] = proj_root
                nn["meta"]["project_key"] = key
            nodes.append(nn)
            node_ids.add(mapped)
        for e in g.get("edges") or []:
            src = id_map.get(str(e.get("src") or ""))
            dst = id_map.get(str(e.get("dst") or ""))
            if src and dst:
                ee = dict(e)
                ee["src"], ee["dst"] = src, dst
                edges.append(ee)
    # qualified overlay links authored in the primary project
    if primary_dir:
        try:
            from services.plm import graph_config as _gcfg
            from services.plm import graph_links as _gl
            primary_key = _gcfg.project_key(primary_dir)
        except Exception:  # noqa: BLE001
            primary_key = ""
        try:
            for lk in _gl.list_links(primary_dir):
                lsrc, ldst = str(lk.get("src") or ""), str(lk.get("dst") or "")
                if "/" not in lsrc and "/" not in ldst:
                    continue  # local links already merged by build_graph
                rsrc = _qualified_to_merged(
                    lsrc, key_by_dir, node_ids, primary_key)
                rdst = _qualified_to_merged(
                    ldst, key_by_dir, node_ids, primary_key)
                if rsrc and rdst:
                    edges.append({
                        "src": rsrc, "dst": rdst,
                        "kind": str(lk.get("kind") or "REFERENCES"),
                        "meta": {"note": lk.get("note") or "",
                                 "link_id": lk.get("id")},
                        "origin": str(lk.get("origin") or "manual"),
                    })
        except Exception:  # noqa: BLE001
            pass
    out = dict(primary)
    out["nodes"] = nodes
    out["edges"] = edges
    return out


def _qualified_to_merged(
    ref: str,
    key_by_dir: dict[str, str],
    node_ids: set[str],
    primary_key: str = "",
) -> str | None:
    """Map ``<project_key>/<local_id>`` to its merged-graph id (or None).

    Linked-project copies live either unprefixed (no collision) or
    ``<key>/<id>``-prefixed; the primary project's own ids are unprefixed.
    An unqualified ref resolves only as a primary/local id.
    """
    if "/" not in ref:
        return ref if ref in node_ids else None
    key, _, local = ref.partition("/")
    if not local:
        return None
    if primary_key and key == primary_key:
        return local if local in node_ids else None
    if key not in set(key_by_dir.values()):
        return None
    if local in node_ids:
        return local
    prefixed = f"{key}/{local}"
    if prefixed in node_ids:
        return prefixed
    return None


def _merge_with_linked(
    root: Path, primary_graph: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Build (or load) each linked project's graph and merge into *primary*."""
    from services.plm import graph_config as gcfg
    try:
        cfg = gcfg.get_config(root)
    except Exception:  # noqa: BLE001
        return primary_graph, []
    pairs: list[tuple[str, dict[str, Any]]] = []
    dirs: list[str] = []
    for d in cfg.get("linked_projects") or []:
        try:
            droot = Path(d)
            if not droot.is_dir():
                continue
            pairs.append((gcfg.project_key(droot), build_graph(droot)))
            dirs.append(str(droot))
        except Exception:  # noqa: BLE001
            continue
    if not pairs:
        return primary_graph, []
    return merge_linked_graphs(
        primary_graph, pairs, primary_dir=str(root)), dirs


def graph_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Node/edge set difference between two graph dicts (added/removed).

    Nodes compare by ``id``; edges by ``(src, dst, kind)``. Values are the
    full node dicts / edge dicts so callers can annotate them (watch flags).
    """
    old_nodes = {str(n.get("id")): n for n in old.get("nodes") or []}
    new_nodes = {str(n.get("id")): n for n in new.get("nodes") or []}
    nodes_added = [n for nid, n in new_nodes.items() if nid not in old_nodes]
    nodes_removed = [n for nid, n in old_nodes.items() if nid not in new_nodes]

    def _ekey(e: dict[str, Any]) -> tuple[str, str, str]:
        return (str(e.get("src")), str(e.get("dst")), str(e.get("kind")))

    old_edges = {_ekey(e): e for e in old.get("edges") or []}
    new_edges = {_ekey(e): e for e in new.get("edges") or []}
    return {
        "ok": True,
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "edges_added": [e for k, e in new_edges.items() if k not in old_edges],
        "edges_removed": [e for k, e in old_edges.items() if k not in new_edges],
    }


def search_graph(
    graph: dict[str, Any],
    q: str = "",
    kinds: Iterable[str] | None = None,
    keyword: str = "",
) -> dict[str, Any]:
    """Substring/kind/keyword search over a graph dict.

    ``q`` matches case-insensitively against node id, label, path,
    ``meta.keywords`` entries and ``meta.project``. ``keyword`` matches nodes
    tagged with it (``meta.keywords``) plus the ``kw:<keyword>`` node itself.
    Returns matching nodes + the edges with both endpoints in the match set.
    """
    q = (q or "").strip().lower()
    keyword = (keyword or "").strip().lower()
    kind_set = {str(k) for k in kinds} if kinds else None
    hits: list[dict[str, Any]] = []
    for n in graph.get("nodes") or []:
        if kind_set is not None and n.get("kind") not in kind_set:
            continue
        meta = n.get("meta") or {}
        kws = [str(k).lower() for k in (meta.get("keywords") or [])]
        if keyword:
            is_kw_node = str(n.get("id")) == f"kw:{keyword}"
            if keyword not in kws and not is_kw_node:
                continue
        if q:
            hay = [str(n.get("id") or ""), str(n.get("label") or ""),
                   str(n.get("path") or ""), str(meta.get("project") or "")]
            hay.extend(kws)
            if not any(q in h.lower() for h in hay):
                continue
        hits.append(n)
    keep = {n["id"] for n in hits}
    edges = [e for e in graph.get("edges") or []
             if e.get("src") in keep and e.get("dst") in keep]
    return {"ok": True, "nodes": hits, "edges": edges,
            "node_count": len(hits), "edge_count": len(edges)}


def neighbors(
    graph: dict[str, Any], node_id: str, depth: int = 1,
) -> dict[str, Any]:
    """Ego-graph: *node_id* plus everything within *depth* hops (undirected)."""
    ids = {n["id"] for n in graph.get("nodes") or []}
    if node_id not in ids:
        return {"ok": False, "reason": "not_found",
                "hint": f"no node {node_id!r}"}
    depth = max(0, min(int(depth), 6))
    adj: dict[str, list[str]] = {}
    for e in graph.get("edges") or []:
        adj.setdefault(e["src"], []).append(e["dst"])
        adj.setdefault(e["dst"], []).append(e["src"])
    keep = {node_id}
    frontier = [node_id]
    for _ in range(depth):
        nxt = [m for c in frontier for m in adj.get(c, []) if m not in keep]
        keep.update(nxt)
        frontier = nxt
        if not frontier:
            break
    nodes = [n for n in graph.get("nodes") or [] if n["id"] in keep]
    edges = [e for e in graph.get("edges") or []
             if e.get("src") in keep and e.get("dst") in keep]
    return {"ok": True, "nodes": nodes, "edges": edges,
            "node_count": len(nodes), "edge_count": len(edges),
            "depth": depth}


def assembly_advice(graph: dict[str, Any]) -> dict[str, Any]:
    """Deterministic assembly best-practice advisor over a built graph.

    Rules:
      * a cluster of >= _SUBASM_MIN_CLUSTER components tied by >= 2 internal
        mates (and not the whole assembly) -> extract a subassembly;
      * a flat assembly with > _FLAT_ASM_MAX direct components -> group them;
      * a part with > _MULTIBODY_MAX bodies -> split into parts + assembly.
    Returns ``{ok, suggestions, narrative}``.
    """
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    edges = graph.get("edges") or []
    children: dict[str, list[str]] = {}
    mate_insts: dict[str, list[str]] = {}
    for e in edges:
        if e.get("kind") == "CONTAINS":
            children.setdefault(e["src"], []).append(e["dst"])
        elif e.get("kind") == "MATES":
            mate_insts.setdefault(e["src"], []).append(e["dst"])

    suggestions: list[dict[str, Any]] = []

    def _label(nid: str) -> str:
        return str(nodes.get(nid, {}).get("label") or nid)

    for nid, n in nodes.items():
        if n.get("kind") != "assembly":
            continue
        kids = children.get(nid, [])
        comp_ids = [c for c in kids
                    if str(c).startswith("instance:")
                    and nodes.get(c, {}).get("kind") == "doc"]
        mate_ids = [c for c in kids if nodes.get(c, {}).get("kind") == "mate"]

        if len(comp_ids) > _FLAT_ASM_MAX:
            suggestions.append({
                "kind": "group_into_subassemblies",
                "components": [_label(c) for c in comp_ids],
                "reason": (f"{len(comp_ids)} components sit directly in one "
                           f"assembly (> {_FLAT_ASM_MAX}); group related parts "
                           "into subassemblies for clearer structure and BOM."),
            })

        # union-find over component instances via shared mates
        parent = {c: c for c in comp_ids}

        def _find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m in mate_ids:
            insts = [i for i in mate_insts.get(m, []) if i in parent]
            for other in insts[1:]:
                ra, rb = _find(insts[0]), _find(other)
                if ra != rb:
                    parent[ra] = rb

        clusters: dict[str, list[str]] = {}
        for c in comp_ids:
            clusters.setdefault(_find(c), []).append(c)
        internal_mates: dict[str, int] = {}
        for m in mate_ids:
            insts = [i for i in mate_insts.get(m, []) if i in parent]
            roots = {_find(i) for i in insts}
            if insts and len(roots) == 1:
                r = next(iter(roots))
                internal_mates[r] = internal_mates.get(r, 0) + 1

        for root, members in clusters.items():
            if (len(members) >= _SUBASM_MIN_CLUSTER
                    and internal_mates.get(root, 0) >= 2
                    and len(members) < len(comp_ids)):
                suggestions.append({
                    "kind": "extract_subassembly",
                    "components": [_label(c) for c in members],
                    "reason": (f"{len(members)} components with "
                               f"{internal_mates[root]} internal mates form a "
                               "tightly-coupled group — extract them into their "
                               "own subassembly (.step+) mated into the parent."),
                })

    for nid, n in nodes.items():
        if n.get("kind") != "part":
            continue
        bodies = [c for c in children.get(nid, [])
                  if nodes.get(c, {}).get("kind") == "body"]
        mated = {e["dst"] for e in edges
                 if e.get("kind") == "MATES" and e["dst"] in set(bodies)}
        ref = mated or set(bodies)
        if len(ref) > _MULTIBODY_MAX:
            suggestions.append({
                "kind": "split_multibody",
                "components": [_label(b) for b in ref],
                "reason": (f"{len(ref)} bodies live in one part — split into "
                           "separate parts + an assembly for clearer mates, "
                           "interference checks, and BOM lines."),
            })

    if suggestions:
        parts = [f"{s['kind']} ({len(s['components'])} items)"
                 for s in suggestions]
        narrative = ("Assembly review found "
                     f"{len(suggestions)} suggestion(s): " + "; ".join(parts)
                     + ". Prefer deep products built as subassemblies over one "
                     "monolithic file.")
    else:
        narrative = ("No structural issues found — the assembly hierarchy "
                     "looks reasonable.")
    return {"ok": True, "suggestions": suggestions, "narrative": narrative}


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
    """Drop the on-disk cache so the next get rebuilds.

    The old cache is kept as ``graph.prev.json.gz`` (renamed, not deleted) so
    :func:`load_cached_graph` still has a "before" snapshot for change-diffs
    after an overlay mutation.
    """
    p = _cache_path(Path(project_dir))
    try:
        if p.is_file():
            prev = p.with_name("graph.prev.json.gz")
            try:
                p.replace(prev)
            except OSError:
                p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# source collection
# ---------------------------------------------------------------------------


def _collect_sources(
    root: Path | None,
    open_docs: dict[str, Any],
    *,
    skip_names: frozenset[str] | None = None,
    archive_names: frozenset[str] | None = None,
    include_archive: bool = False,
) -> list[dict[str, Any]]:
    """Walk project for .step+/.ecad+ containers.

    *skip_names* basenames are pruned from os.walk (system + archive by
    default). Nested ``.aicadvault`` is always in the system set so shadow
    trees are never double-counted; nested vault *working* trees remain.
    When *include_archive* is True, archive dirs are walked and each source
    is tagged ``archive=True`` for node meta.
    """
    sources: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    skip = skip_names if skip_names is not None else _skip_dir_names(
        include_archive=include_archive)
    arch = archive_names if archive_names is not None else DEFAULT_ARCHIVE_DIR_NAMES

    if root is not None and root.is_dir():
        # Collect candidate files in deterministic walk order first…
        pending: list[tuple[Path, str, float]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # prune system + (optional) archive dirs in-place
            dirnames[:] = [d for d in dirnames if d not in skip]
            for fn in filenames:
                lower = fn.lower()
                if not (lower.endswith(".step+") or lower.endswith(".ecad+")):
                    continue
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(root)).replace("\\", "/")
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                pending.append((fp, rel, mtime))
        # …then read containers in parallel. zlib decompression releases the
        # GIL, so threads give a real speedup on large project trees; map()
        # preserves walk order so the source list stays deterministic.
        if pending:
            workers = min(8, (os.cpu_count() or 4), len(pending))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = pool.map(lambda t: _read_container(t[0]), pending)
            for (fp, rel, mtime), (doc, study_ids, doc_hash) in zip(pending, results):
                if doc is None:
                    continue
                key = f"file:{rel}"
                sources.append({
                    "key": key, "path": rel, "mtime": mtime,
                    "doc": doc, "doc_id": doc.get("id"),
                    "doc_hash": doc_hash,
                    "study_ids": study_ids,
                    "kind_hint": "board" if rel.lower().endswith(".ecad+") else None,
                    "archive": path_is_archive(rel, arch),
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
            "archive": False,
        })
    return sources


def _read_container(path: Path) -> tuple[dict | None, list[str], str | None]:
    """Return (document.json dict, study ids from studies/, content hash).

    The hash is blake2b-8 over the raw document.json bytes (already read
    for parsing, so it costs nothing extra). It changes iff doc content
    changes — the freshness signal preview/thumbnail caches key on
    (surfaced as meta.dh on the doc node)."""
    try:
        # ZIP seeks directly to document.json; never copy cached geometry or
        # simulation payloads just to inspect the design recipe.
        with zipfile.ZipFile(path) as zf:
            members = zf.infolist()
            if len(members) > snapshot._MAX_ZIP_ENTRIES:
                return None, [], None
            folded_names: set[str] = set()
            document = None
            for info in members:
                folded = info.filename.casefold()
                if folded in folded_names:
                    return None, [], None
                folded_names.add(folded)
                if info.filename == "document.json":
                    document = info
            if document is None or document.is_dir():
                return None, [], None
            if (document.file_size > snapshot._MAX_MEMBER_BYTES
                    or document.file_size > max(1, document.compress_size)
                    * snapshot._MAX_COMPRESSION_RATIO):
                return None, [], None
            doc_bytes = bytearray()
            with zf.open(document) as member:
                while True:
                    chunk = member.read(min(
                        snapshot._READ_CHUNK_BYTES,
                        snapshot._MAX_MEMBER_BYTES - len(doc_bytes) + 1,
                    ))
                    if not chunk:
                        break
                    if len(doc_bytes) + len(chunk) > snapshot._MAX_MEMBER_BYTES:
                        return None, [], None
                    doc_bytes.extend(chunk)
            if len(doc_bytes) != document.file_size:
                return None, [], None
            doc_hash = hashlib.blake2b(doc_bytes, digest_size=8).hexdigest()
            doc = json.loads(doc_bytes.decode("utf-8"))
            study_ids = []
            for info in members:
                n = info.filename
                if n.startswith("studies/") and n.endswith(".json"):
                    # studies/<id>.json
                    parts = n.split("/")
                    if len(parts) == 2:
                        study_ids.append(parts[1][:-5])  # strip .json
            return doc, study_ids, doc_hash
    except Exception:  # noqa: BLE001
        return None, [], None


def _collect_files(
    root: Path | None,
    *,
    skip_names: frozenset[str] | None = None,
    include_archive: bool = False,
) -> list[dict[str, Any]]:
    """Walk project for non-CAD files surfaced as `file` graph nodes.

    Metadata only (path/mtime/size/ext) — office content extraction is
    config-gated and happens in build_graph. Same pruning policy as
    :func:`_collect_sources`; ``.step+``/``.ecad+`` containers are excluded
    (they are doc sources, not file nodes). Sorted by rel path so the
    MAX_FILE_NODES cap is deterministic.
    """
    out: list[dict[str, Any]] = []
    if root is None or not root.is_dir():
        return out
    skip = skip_names if skip_names is not None else _skip_dir_names(
        include_archive=include_archive)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            lower = fn.lower()
            if lower.endswith((".step+", ".ecad+")):
                continue
            ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
            if not ext or f".{ext}" not in _FILE_EXTS:
                continue
            fp = Path(dirpath) / fn
            try:
                st = fp.stat()
                rel = str(fp.relative_to(root)).replace("\\", "/")
            except OSError:
                continue
            out.append({
                "key": f"file:{rel}", "path": rel, "mtime": st.st_mtime,
                "size": st.st_size, "ext": ext,
            })
    out.sort(key=lambda r: r["path"])
    return out


def _manifest_items_by_path(root: Path) -> dict[str, dict[str, Any]]:
    """Best-effort {path: manifest item} for generic-item meta on file nodes.

    Reads the vault head manifest when a vault exists; any failure (no vault,
    unreadable head) degrades to an empty map — file nodes still emit.
    """
    try:
        from services.plm.vault import Vault
        if not Vault.exists(root):
            return {}
        v = Vault.open(root)
        try:
            m = v.manifest()
        finally:
            v.close()
        return {str(it.get("path") or ""): it
                for it in (m.get("items") or []) if it.get("path")}
    except Exception:  # noqa: BLE001
        return {}


def _extract_office_text(path: Path, max_chars: int) -> dict[str, Any] | None:
    """Run the shared office extractor (contracts/office.md §2); None on any
    failure (missing library, unreadable file) — never fatal to the build."""
    try:
        from services.mcp.office_tools import extract_office
        res = extract_office(str(path))
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        text = str(res.get("text") or "")[:max(0, int(max_chars))]
        if not text.strip():
            return {"text": "", "kind": res.get("kind")}
        return {"text": text, "kind": res.get("kind")}
    except Exception:  # noqa: BLE001
        return None


def _walk_project_folders(
    root: Path,
    proj_id: str,
    add_node,
    add_edge,
    path_to_doc_node: dict[str, str],
    *,
    skip_names: frozenset[str] | None = None,
) -> None:
    """Emit sim_study / board / firmware nodes from well-known project dirs."""
    skip = skip_names if skip_names is not None else _skip_dir_names()
    for child in sorted(root.iterdir() if root.is_dir() else []):
        if not child.is_dir():
            continue
        # basename match — system + archive dirs never contribute folder nodes
        if child.name in skip or child.name.lower() in {
            ".aicadvault", ".plm", ".git", "__pycache__",
            ".venv", "venv", "node_modules",
        }:
            continue
        try:
            entries = set(os.listdir(child))
        except OSError:
            continue
        name = child.name.lower()
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
    return root / ".plm" / "graph.json.gz"


def _read_cache(path: Path) -> dict[str, Any] | None:
    """Decode a bounded compressed cache; legacy JSON caches rebuild once."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            expanded = stream.read(MAX_CACHE_EXPANDED_BYTES + 1)
        if len(expanded) > MAX_CACHE_EXPANDED_BYTES:
            return None
        data = json.loads(expanded)
        return data if isinstance(data, dict) else None
    except (OSError, EOFError, ValueError, zlib.error):
        return None


def _source_state(st: os.stat_result) -> list[int]:
    """JSON-stable file identity, including permission and metadata changes."""
    return [st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_mode]


def _overlay_mtime(root: Path, source_states: dict[str, list[int]] | None = None) -> float:
    """Newest mtime across the graph overlays (0 when all absent).

    graph_links / keywords / graph_config all change the built graph, so all
    three participate in cache invalidation. graph_watch.json is intentionally
    excluded — watch entries never change the graph.
    """
    newest = 0.0
    for name in ("graph_links.json", "keywords.json", "graph_config.json"):
        p = root / ".plm" / name
        try:
            if p.is_file():
                st = p.stat()
                newest = max(newest, st.st_mtime)
                if source_states is not None:
                    source_states[f"overlay:{name}"] = _source_state(st)
        except OSError:
            continue
    return newest


_PATH_SEPS = tuple(s for s in (os.sep, os.altsep) if s)


def _source_mtimes_fast(
    root: Path,
    *,
    skip_names: frozenset[str],
    source_states: dict[str, list[int]] | None = None,
) -> dict[str, float]:
    """Scan source container + file-node mtimes without reading zip contents.

    Used by the cache fast-path so a warm large project tree can validate the
    on-disk graph without re-parsing every .step+. File-node sources
    (contracts/cad_graph.md file-node addendum) share the ``file:<rel>`` key
    namespace — containers always end .step+/.ecad+, file nodes never do.
    When requested, source_states records stronger stat identity from the same
    scan, so restoring access to an unreadable file invalidates its cached
    omission even when its content mtime has not changed.
    """
    out: dict[str, float] = {}
    if not root.is_dir():
        return out
    # Relative keys come from string slicing once per directory: pathlib's
    # relative_to walks every ancestor per call, which made this scan cost
    # ~5 s on a 145k-file tree (twice per cad_graph_get).
    prefix = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_names]
        reldir = dirpath[len(prefix):]
        if reldir[:1] in _PATH_SEPS:
            reldir = reldir[1:]
        for fn in filenames:
            lower = fn.lower()
            if lower.endswith((".step+", ".ecad+")):
                pass  # container — always a source
            else:
                ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
                if not ext or f".{ext}" not in _FILE_EXTS:
                    continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            rel = (f"{reldir}{os.sep}{fn}" if reldir else fn).replace("\\", "/")
            key = f"file:{rel}"
            out[key] = st.st_mtime
            if source_states is not None:
                source_states[key] = _source_state(st)
    # overlays participate in invalidation (mutations also invalidate_cache)
    om = _overlay_mtime(root, source_states)
    if om > 0:
        out["overlay:graph_links"] = om
    return out


def _filter_matches(
    data: dict[str, Any],
    *,
    include_archive: bool,
    archive_names: list[str] | None,
) -> bool:
    """True when cached filter policy matches this request.

    Caches written before the archive-exclusion addendum lack a ``filter``
    block — treat those as a miss so large project trees rebuild without
    ``_archive_stale`` bloat rather than serving a stale 500MB+ payload.
    """
    filt = data.get("filter")
    if not isinstance(filt, dict):
        return False
    if bool(filt.get("include_archive", False)) != bool(include_archive):
        return False
    wanted = sorted(str(x) for x in (
        archive_names if archive_names is not None
        else sorted(DEFAULT_ARCHIVE_DIR_NAMES)
    ))
    cached = sorted(str(x) for x in (filt.get("exclude_dirs") or []))
    return cached == wanted


def _load_cache_if_valid(
    root: Path,
    *,
    include_archive: bool = False,
    archive_names: list[str] | None = None,
) -> dict | None:
    """Validate the complete source fingerprint before serving a cached graph."""
    cp = _cache_path(root)
    try:
        st = cp.stat()
    except OSError:
        return None
    if st.st_size > MAX_CACHE_BYTES:
        return None
    skip = _skip_dir_names(
        include_archive=include_archive, exclude_dirs=archive_names)
    source_states: dict[str, list[int]] = {}
    current = _source_mtimes_fast(root, skip_names=skip, source_states=source_states)
    if st.st_mtime + 1e-6 < max(current.values(), default=0.0):
        return None
    data = _read_cache(cp)
    if data is None or not _filter_matches(
            data, include_archive=include_archive, archive_names=archive_names):
        return None
    if data.get("source_states") != source_states:
        return None
    cached = {k: v for k, v in (data.get("mtimes") or {}).items()
              if not str(k).startswith("open:")}
    if cached.keys() != current.keys():
        return None
    if any(abs(float(cached[k]) - float(mt)) > 1e-6 for k, mt in current.items()):
        return None
    data["ok"] = True
    return data


def load_cached_graph(project_dir: str | os.PathLike) -> dict[str, Any] | None:
    """Read the on-disk cached graph as-is (no validity check), else None.

    Used by the change-diff RPC: the cached graph is the "before" snapshot a
    fresh rebuild is compared against. Falls back to ``graph.prev.json.gz`` —
    :func:`invalidate_cache` renames the cache there so a mutation doesn't
    destroy the baseline. Size-guarded like the cache loader.
    """
    root = Path(project_dir)
    for cp in (_cache_path(root), root / ".plm" / "graph.prev.json.gz"):
        try:
            if not cp.is_file() or cp.stat().st_size > MAX_CACHE_BYTES:
                continue
            data = _read_cache(cp)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return None


def _save_cache(root: Path, graph: dict[str, Any]) -> None:
    """Atomic cache write (tmp + os.replace). Never leaves a partial file."""
    cp = _cache_path(root)
    tmp: Path | None = None
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(graph, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(payload) > MAX_CACHE_EXPANDED_BYTES:
            return
        # write + fsync so a crash mid-write can't leave a half file that
        # rename would then promote.
        with tempfile.NamedTemporaryFile(mode="wb", dir=cp.parent,
                                         prefix=cp.name + ".", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=1, mtime=0) as compressed:
                compressed.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if tmp.stat().st_size > MAX_CACHE_BYTES:
            tmp.unlink()
            return
        os.replace(tmp, cp)
    except OSError:
        try:
            if tmp is not None and tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sanitize_meta(value: Any, *, _depth: int = 0) -> Any:
    """Bound arbitrary node/edge meta so one pathological doc can't bloat the
    cached graph. Caps nesting depth, string length, list length, dict keys."""
    _MAX_DEPTH, _MAX_STR, _MAX_LIST, _MAX_KEYS = 4, 256, 8, 16
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _depth >= _MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_KEYS:
                break
            out[str(k)[:_MAX_STR]] = _sanitize_meta(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_meta(v, _depth=_depth + 1) for v in list(value)[:_MAX_LIST]]
    return str(value)[:_MAX_STR]


def _kind_from_schema(schema: str) -> str | None:
    base = str(schema).split("/", 1)[0]
    return _SCHEMA_KIND.get(base)


# doc_schema.md §0/§1/§6: display unit system + top-level material key. Both
# fields are optional in the doc and thus optional on the doc node's meta.
_UNITS_IPS = frozenset({"ips", "inch", "inches", "in", "imperial", "english"})
_UNITS_MMGS = frozenset({"mmgs", "mm", "mgs", "metric", "millimeter", "millimeters"})


def _doc_units(doc: dict) -> str | None:
    """Normalize doc.units.system ("MMGS"|"IPS", doc_schema.md §0) to
    "ips" / "mmgs". Absent/unknown → None (field omitted)."""
    units = doc.get("units")
    if not isinstance(units, dict):
        return None
    system = units.get("system")
    if not isinstance(system, str) or not system.strip():
        return None
    s = system.strip().lower()
    if s in _UNITS_IPS:
        return "ips"
    if s in _UNITS_MMGS:
        return "mmgs"
    return None


def _doc_materials(doc: dict) -> list[str]:
    """Distinct material names referenced by the document.

    doc_schema.md §1 defines exactly one material field: the part-doc
    top-level `material.key` (indexes engine/sim/materials.json; may be
    null). Assemblies/drawings carry no material field of their own. Dedup,
    sort, cap at MAX_DOC_MATERIALS (matches _sanitize_meta's list cap so the
    emitted list is never silently re-truncated)."""
    names: set[str] = set()
    mat = doc.get("material")
    if isinstance(mat, dict):
        key = mat.get("key")
        if isinstance(key, str) and key.strip():
            names.add(key.strip())
    if not names:
        return []
    return sorted(names)[:MAX_DOC_MATERIALS]


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


# equation grammar built-ins (contracts/doc_schema.md §4 whitelist) — never link
# these tokens as global refs.
_EQ_FUNCS = frozenset({
    "sin", "cos", "tan", "asin", "acos", "atan",
    "sqrt", "abs", "min", "max", "floor", "ceil", "pi",
})
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _expr_names(expr: Any) -> list[str]:
    """Identifiers referenced by an equation/dimension expression, minus the
    §4 built-in function/const names. Order-preserving, de-duplicated."""
    if not isinstance(expr, str) or not expr:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _IDENT_RE.findall(expr):
        if tok in _EQ_FUNCS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


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


def _feature_dims(
    feat: dict[str, Any],
) -> list[tuple[str, Any, str | None, str | None]]:
    """Yield (dim_name, value, unit, expr) from feature params.

    The qualified ``fid.k`` entry carries the raw ``expr`` (so callers can build
    expression-driven DERIVES_FROM edges); the bare-name duplicate used for
    cross-doc shared-value matching carries ``expr=None`` to avoid double edges.
    """
    out: list[tuple[str, Any, str | None, str | None]] = []
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    fid = str(feat.get("id") or "")
    for k, v in params.items():
        if k in ("entities", "constraints", "external_refs", "code", "text"):
            continue
        # bool subclasses int — exclude flags (merge/centered/copy/…) so they
        # never enter the shared-value dim index.
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append((f"{fid}.{k}", v, None, None))
            out.append((k, v, None, None))  # bare name for cross-doc match
        elif isinstance(v, dict) and "value" in v:
            raw = v.get("value")
            if isinstance(raw, bool):
                continue
            expr = v.get("expr")
            out.append((f"{fid}.{k}", raw, v.get("unit"), expr))
            out.append((k, raw, v.get("unit"), None))
    # sketch dimension constraints
    if isinstance(params.get("constraints"), list):
        for c in params["constraints"]:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("distance", "length", "angle", "radius", "diameter"):
                val = c.get("value")
                cid = c.get("id", "dim")
                if isinstance(val, dict):
                    out.append((f"{fid}.{cid}", val.get("value"),
                                val.get("unit"), val.get("expr")))
                elif isinstance(val, (int, float)):
                    out.append((f"{fid}.{cid}", val, None, None))
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

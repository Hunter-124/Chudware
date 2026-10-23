"""services.plm.whereused — the derived where-used index.

Where-used is **derived, never stored** (contract §4): given the manifest and
the project's canonical documents, walk every consumer edge —

* **assemblies** consume parts/sub-assemblies via ``components[].source.path``
  (doc_schema §6; the ``{path}``/``{doc_id}`` object form is handled too),
* **drawings** consume their projected source via the top-level ``source``
  string (doc_schema §8 — a doc id or a path),
* **CAM jobs / other doc kinds** consume via the same ``source`` convention
  or an explicit ``part``/``source_doc`` reference when present.

Matching is by item **path** (exact or basename-sans-suffix) or **doc uuid**,
so live-session ids and at-rest paths both resolve.

Powers the stale-ref flags (``assist.stale_refs``) and the Project Hub
explorer. The dedicated MCP tool surface ships thin here and
grows into the Hub.
"""
from __future__ import annotations

import json
from typing import Any

from services.plm import manifest as manifest_mod
from services.plm import snapshot


def build_index(m: dict[str, Any],
                docs_by_item: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build ``{item_id: [consumer records]}`` for every item in the project.

    A consumer record::

        {"item_id": <consumer>, "kind": "assembly"|"drawing"|"cam"|…,
         "path": <consumer path>, "via": "component"|"source", "count": N}

    ``count`` is the number of references (component instances for
    assemblies; 1 for source-style references).
    """
    index: dict[str, list[dict[str, Any]]] = {it["item_id"]: [] for it in m["items"]}

    for consumer in m["items"]:
        doc = docs_by_item.get(consumer["item_id"])
        if not isinstance(doc, dict):
            continue
        # component edges (assemblies)
        counts: dict[str, int] = {}
        for comp in doc.get("components", []) or []:
            target = _resolve_ref(m, _component_ref(comp))
            if target is not None and target["item_id"] != consumer["item_id"]:
                counts[target["item_id"]] = counts.get(target["item_id"], 0) + 1
        for tid, n in counts.items():
            index.setdefault(tid, []).append(_record(consumer, "component", n))
        # source edges (drawings, CAM jobs, any doc pointing at one source)
        for key in ("source", "source_doc", "part"):
            ref = doc.get(key)
            if isinstance(ref, dict):
                ref = ref.get("path") or ref.get("doc_id")
            if isinstance(ref, str) and ref:
                target = _resolve_ref(m, ref)
                if target is not None and target["item_id"] != consumer["item_id"]:
                    index.setdefault(target["item_id"], []).append(
                        _record(consumer, "source", 1))
                break  # one source reference per document
    return index


def where_used(m: dict[str, Any], docs_by_item: dict[str, dict[str, Any]],
               item_id: str, *, transitive: bool = False) -> list[dict[str, Any]]:
    """Answer "who consumes this item?" — direct consumers, or the whole
    upward closure with ``transitive=True`` (each record then carries
    ``"level"``: 1 = direct, 2 = consumer-of-consumer, …)."""
    if manifest_mod.find_item(m, item_id) is None:
        raise KeyError(item_id)
    index = build_index(m, docs_by_item)
    direct = [dict(r, level=1) for r in index.get(item_id, [])]
    if not transitive:
        return direct
    out: list[dict[str, Any]] = []
    seen: set[str] = {item_id}
    frontier = direct
    level = 1
    while frontier:
        nxt: list[dict[str, Any]] = []
        for rec in frontier:
            if rec["item_id"] in seen:
                continue
            seen.add(rec["item_id"])
            out.append(rec)
            for up in index.get(rec["item_id"], []):
                nxt.append(dict(up, level=level + 1))
        frontier = nxt
        level += 1
    return out


def docs_at_head(vault: Any) -> dict[str, dict[str, Any]]:
    """Load every item's canonical document from the newest history entry."""
    head = vault._bind.head_commit()
    if head is None:
        return {}
    tree = vault._bind.read_tree(head)
    out: dict[str, dict[str, Any]] = {}
    for it in vault.manifest()["items"]:
        raw = tree.get(snapshot.item_prefix(it["item_id"]) + "document.json")
        if raw is not None:
            try:
                out[it["item_id"]] = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
    return out


# ---------------------------------------------------------------------------
# reference resolution
# ---------------------------------------------------------------------------

def _component_ref(comp: dict[str, Any]) -> str:
    src = comp.get("source")
    if isinstance(src, dict):
        return str(src.get("path") or src.get("embedded") or "")
    if isinstance(src, str):
        return src
    return str(comp.get("ref") or "")  # tolerate legacy/test fixtures


def _resolve_ref(m: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Resolve a path / doc-uuid / bare-name reference to a manifest item."""
    if not ref:
        return None
    for it in m["items"]:
        if ref == it["path"] or ref == it.get("doc_uuid"):
            return it
        stem = _stem(it["path"])
        if ref == stem or _stem(ref) == stem:
            return it
    return None


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    for suffix in (".step+", ".ecad+"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name.rsplit(".", 1)[0] if "." in name else name


def _record(consumer: dict[str, Any], via: str, count: int) -> dict[str, Any]:
    return {"item_id": consumer["item_id"], "kind": consumer["kind"],
            "path": consumer["path"], "part_number": consumer.get("part_number"),
            "via": via, "count": count}

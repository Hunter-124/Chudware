"""services.plm.compare — two-revision compare views (T-R3, contract §7 plm_compare).

Orchestrates — never re-specifies — the two existing engines (contract §9):

* **Semantic**: ``doc_diff`` (services/mcp/doc_diff_tools.py) on the two stored
  canonical ``document.json`` blobs, plus the deterministic plain-language
  change list from :mod:`summarize`.
* **Visual**: the C++ core's ``eval_compare`` RPC (match percentage) and
  side-by-side renders of the two revisions (``doc_render_png``), composed
  into one image when Pillow is available. Drawings additionally get a 2D
  overlay blend of the two sheets.

The visual half is BEST-EFFORT: when the engine library is not available
(e.g. a test box without ``libaicad``), it degrades to a structured
``{degraded: true, reason: …}`` block and the semantic half still answers —
compare never hard-fails on a machine without the core (ease-of-use §8).

Engine doc-load contract (docs/… engine-doc-load-verify-contract):
``doc_new`` (kind+name only — NEVER the tree), then one ``doc_apply_ops``
with ``eq_set`` for every equation global FIRST, then ``add_feature`` per
feature in tree order.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.plm import snapshot


# ---------------------------------------------------------------------------
# semantic compare
# ---------------------------------------------------------------------------

def semantic_compare(doc_a: dict[str, Any], doc_b: dict[str, Any]) -> dict[str, Any]:
    """Semantic change list between two parsed documents: raw ``doc_diff``
    plus the deterministic plain-language summary (numerals copied from the
    diff — Pillar 13)."""
    from services.mcp.doc_diff_tools import doc_diff
    from services.plm import summarize

    diff = doc_diff(doc_a, doc_b)
    return {"doc_diff": diff,
            "summary": summarize.summarize_diff(diff, doc_a, doc_b)}


def is_generic_item(vault: Any, item_id: str) -> bool:
    """True when *item_id* is a non-container (generic) item — a doc/code/link
    item that has no ``document.json`` to semantic-diff."""
    from services.plm import doctypes

    m = vault.manifest()
    from services.plm import manifest as manifest_mod

    it = manifest_mod.find_item(m, item_id)
    dt = (it or {}).get("doc_type")
    return bool(dt and dt in doctypes.GENERIC_DOC_TYPES)


def generic_compare(vault: Any, commit_a: str, commit_b: str,
                    item_id: str) -> dict[str, Any]:
    """Compare a generic item between two history points (contract §2 sibling
    content). Returns ``{mode:"generic", summary, text_diff?}`` — a plain-language
    line always, plus a unified text diff when both versions are text.
    """
    import difflib

    from services.plm import generic_snapshot
    from services.plm import manifest as manifest_mod
    from services.plm.vault import VaultError

    try:
        tree_a = vault._bind.read_tree(commit_a)
        tree_b = vault._bind.read_tree(commit_b)
    except KeyError:
        raise VaultError("not_found", "one of those points in history could not be found")
    old = generic_snapshot.head_content(item_id, tree_a)
    new = generic_snapshot.head_content(item_id, tree_b)
    if not old and not new:
        raise VaultError("invalid_item", "that item is not present at one of those points")
    item = manifest_mod.find_item(vault.manifest(), item_id) or {"path": item_id,
                                                                 "item_id": item_id}
    out: dict[str, Any] = {"mode": "generic",
                           "summary": generic_snapshot.summarize(item, old, new)}
    # a single-file text item gets a readable unified diff
    if len(old) <= 1 and len(new) <= 1:
        a_bytes = next(iter(old.values()), b"")
        b_bytes = next(iter(new.values()), b"")
        a_txt = _as_text(a_bytes)
        b_txt = _as_text(b_bytes)
        if a_txt is not None and b_txt is not None:
            diff = difflib.unified_diff(
                a_txt.splitlines(), b_txt.splitlines(),
                fromfile="before", tofile="after", lineterm="")
            out["text_diff"] = "\n".join(diff)
    return out


def _as_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    return text


def docs_at(vault: Any, commit_a: str, commit_b: str,
            item_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one item's parsed document at two history points.

    Raises ``VaultError`` with contract reason codes on unknown points/items.
    """
    from services.plm.vault import VaultError

    try:
        tree_a = vault._bind.read_tree(commit_a)
        tree_b = vault._bind.read_tree(commit_b)
    except KeyError:
        raise VaultError("not_found", "one of those points in history could not be found")
    path = snapshot.item_prefix(item_id) + "document.json"
    raw_a, raw_b = tree_a.get(path), tree_b.get(path)
    if raw_a is None or raw_b is None:
        raise VaultError("invalid_item", "that item is not present at one of those points")
    return json.loads(raw_a.decode("utf-8")), json.loads(raw_b.decode("utf-8"))


# ---------------------------------------------------------------------------
# visual compare (engine best-effort)
# ---------------------------------------------------------------------------

_DEGRADED_NO_ENGINE = {
    "degraded": True, "reason": "engine_unavailable",
    "hint": "the 3D engine is not available on this machine; "
            "the change list above still applies",
}


def visual_compare(doc_a: dict[str, Any], doc_b: dict[str, Any],
                   out_dir: str | Path) -> dict[str, Any]:
    """Visual two-revision compare via the C++ core (best-effort).

    Returns::

        {degraded: false, match_pct, image_a, image_b,
         side_by_side?,          # composed panel (parts; needs Pillow)
         overlay?}               # 2D blend (drawings; needs Pillow)

    or a ``{degraded: true, reason, hint}`` block when the engine/doc kind
    cannot be compared visually. Never raises for engine absence.
    """
    try:
        from mcp_server import core_bridge as cb
    except Exception:
        return dict(_DEGRADED_NO_ENGINE)
    if not cb.available():
        return dict(_DEGRADED_NO_ENGINE)

    schema = str(doc_a.get("schema", ""))
    kind = schema.split("/", 1)[0].rsplit(".", 1)[-1]  # part | asm | drawing | …
    if kind not in ("part", "asm", "drawing"):
        return {"degraded": True, "reason": "unsupported_kind",
                "hint": "visual compare is not available for this document type yet"}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    ida = _load_doc(cb, doc_a, kind)
    idb = _load_doc(cb, doc_b, kind)
    if ida is None or idb is None:
        return {"degraded": True, "reason": "rebuild_failed",
                "hint": "one of the two versions could not be rebuilt for viewing"}

    result: dict[str, Any] = {"degraded": False, "match_pct": None}

    # geometric match percentage (parts/assemblies; 100 == identical)
    if kind in ("part", "asm"):
        try:
            r = cb.call("eval_compare", {"doc_a": ida, "doc_b": idb})
            if not r.get("degraded"):
                result["match_pct"] = r.get("match_pct")
        except Exception:
            pass

    png_a, png_b = str(out / "revision_a.png"), str(out / "revision_b.png")
    ok_a = _render(cb, ida, png_a, kind)
    ok_b = _render(cb, idb, png_b, kind)
    if ok_a:
        result["image_a"] = png_a
    if ok_b:
        result["image_b"] = png_b

    if ok_a and ok_b:
        composed = _compose(png_a, png_b, out, overlay=(kind == "drawing"))
        result.update(composed)

    return result


def _load_doc(cb: Any, doc: dict[str, Any], kind: str) -> str | None:
    """Load a parsed document into the engine per the doc-load contract."""
    tree = doc.get("tree", doc)
    new_kind = {"asm": "assembly"}.get(kind, kind)
    try:
        doc_id = cb.call("doc_new", {"kind": new_kind,
                                     "name": str(tree.get("name", "compare"))})["doc_id"]
        ops = [{"op": "eq_set", "name": g["name"], "expr": g["expr"]}
               for g in (tree.get("equations", {}) or {}).get("globals", [])]
        ops += [{"op": "add_feature", "feature": f} for f in tree.get("features", []) or []]
        if ops:
            cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        return doc_id
    except Exception:
        return None


def _render(cb: Any, doc_id: str, out_path: str, kind: str) -> bool:
    try:
        if kind == "drawing":
            r = cb.call("drawing_render_png", {"doc_id": doc_id, "out_path": out_path})
        else:
            r = cb.call("doc_render_png", {"doc_id": doc_id, "out_path": out_path})
        return bool(r is None or r.get("ok", True)) and Path(out_path).is_file()
    except Exception:
        return False


def _compose(png_a: str, png_b: str, out: Path, *, overlay: bool) -> dict[str, Any]:
    """Compose the two renders side-by-side (and a 2D overlay for drawings).

    Pillow is optional; without it the two individual panels still stand.
    """
    result: dict[str, Any] = {}
    try:
        from PIL import Image
    except Exception:
        return result
    try:
        a = Image.open(png_a).convert("RGBA")
        b = Image.open(png_b).convert("RGBA")
        h = max(a.height, b.height)
        canvas = Image.new("RGBA", (a.width + b.width, h), (255, 255, 255, 255))
        canvas.paste(a, (0, (h - a.height) // 2))
        canvas.paste(b, (a.width, (h - b.height) // 2))
        side = out / "side_by_side.png"
        canvas.convert("RGB").save(side)
        result["side_by_side"] = str(side)
        if overlay:
            b2 = b.resize(a.size) if b.size != a.size else b
            blend = Image.blend(a, b2, 0.5)
            ov = out / "overlay.png"
            blend.convert("RGB").save(ov)
            result["overlay"] = str(ov)
    except Exception:
        pass
    return result

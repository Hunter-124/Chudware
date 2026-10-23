"""services.plm.compare — two-revision compare views (contract §7 plm_compare).

Orchestrates — never re-specifies — the two existing engines (contract §9):

* **Semantic**: ``doc_diff`` (services/mcp/doc_diff_tools.py) on the two stored
  canonical ``document.json`` blobs, plus the deterministic plain-language
  change list from :mod:`summarize`.
* **Visual**: rendered images of the geometry DIFFERENCE between the two
  revisions — a git-diff for a 3D model. Parts/assemblies go through the core's
  ``doc_render_diff`` RPC (added green / removed red / unchanged grey, one shared
  camera framing across ISO/FRONT/TOP so the frames register), drawings and ECAD
  layouts through the pure-Python vector differ in :mod:`services.plm.visual_diff`,
  and any pair of plain renders through its registered pixel diff as the
  universal fallback. The two plain renders (``doc_render_png`` /
  ``drawing_render_png``) and their side-by-side panel are kept alongside.

The visual half is BEST-EFFORT: when the engine library is not available
(e.g. a test box without ``libchudware``), it degrades to a structured
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
    diff — Pillar 13). ECAD documents (aicad.pcb/1, aicad.sch/1, aicad.ecad/1)
    get the id-based nets/components/footprints/tracks delta instead
    (services.plm.ecad_diff), with a matching plain-language summary."""
    from services.mcp.doc_diff_tools import doc_diff
    from services.plm import ecad_diff as ecad_diff_mod
    from services.plm import summarize

    diff = doc_diff(doc_a, doc_b)
    if ecad_diff_mod.is_ecad_schema(doc_a) or ecad_diff_mod.is_ecad_schema(doc_b):
        return {"doc_diff": diff, "summary": ecad_diff_mod.summarize_ecad_diff(diff)}
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
    line always, plus a unified text diff for a single-file item: verbatim text,
    or (docx/xlsx/xlsm/pptx/pdf/doc) the frozen office_extract text — capped and
    truncation-noted per the context-safe rule. A binary item with no extractor
    (or whose extraction fails/is unavailable) keeps the plain-language summary
    only — this never raises.
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
    # a single-file item gets a readable unified diff
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
        else:
            name = next(iter(old), "") or next(iter(new), "")
            office_diff = _office_text_diff(name, a_bytes, b_bytes)
            if office_diff is not None:
                out["text_diff"] = office_diff
    return out


def _as_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    return text


# .docx/.xlsx/.xlsm/.pptx/.pdf/.doc items have no plain-text form but do have a
# frozen extractor (services/mcp/office_tools.py extract_office); every other
# binary item keeps the one-line summary above.
_OFFICE_EXT_KINDS = frozenset({"docx", "xlsx", "xlsm", "pptx", "pdf", "doc"})
_MAX_DIFF_LINES = 400


def _cap_diff_lines(lines: list[str]) -> str:
    """Join unified-diff *lines*, capped at ``_MAX_DIFF_LINES`` with a
    truncation note (context-safe rule — mirrors fs_tools.py fs_read's
    offset/limit cap)."""
    if len(lines) > _MAX_DIFF_LINES:
        omitted = len(lines) - _MAX_DIFF_LINES
        lines = lines[:_MAX_DIFF_LINES] + [
            f"... (truncated — {omitted} more diff lines omitted)"]
    return "\n".join(lines)


def _office_text_diff(name: str, a_bytes: bytes, b_bytes: bytes) -> str | None:
    """Unified diff of two revisions' ``office_extract`` text, or ``None``
    when *name*'s extension has no extractor, or extraction fails/is
    unavailable — the caller then falls back to the plain-language summary.
    Never raises.
    """
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in _OFFICE_EXT_KINDS:
        return None
    try:
        import difflib
        import tempfile

        from services.mcp.office_tools import extract_office

        with tempfile.TemporaryDirectory(prefix="chudware_plm_office_diff_") as td:
            path_a = Path(td) / f"before.{ext}"
            path_b = Path(td) / f"after.{ext}"
            path_a.write_bytes(a_bytes)
            path_b.write_bytes(b_bytes)
            res_a = extract_office(str(path_a))
            res_b = extract_office(str(path_b))
        if not res_a.get("ok") or not res_b.get("ok"):
            return None
        text_a = str(res_a.get("text", ""))
        text_b = str(res_b.get("text", ""))
        diff = list(difflib.unified_diff(
            text_a.splitlines(), text_b.splitlines(),
            fromfile="before", tofile="after", lineterm=""))
    except Exception:  # noqa: BLE001 — extraction is best-effort, never fatal
        return None
    return _cap_diff_lines(diff)


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
# visual compare — rendered geometry DIFFS (contract §7 eval_compare block)
# ---------------------------------------------------------------------------

_DEGRADED_NO_ENGINE = {
    "degraded": True, "reason": "engine_unavailable",
    "hint": "the 3D engine is not available on this machine; "
            "the change list above still applies",
}

# ISO is the headline view; FRONT/TOP catch changes the ISO silhouette hides.
_DIFF_VIEWS = ("ISO", "FRONT", "TOP")


def _blank_visual() -> dict[str, Any]:
    """Every key of the visual block, all absent. Callers fill what they get, so
    a consumer never has to guess whether a key exists."""
    return {"degraded": False, "match_pct": None, "identical": False,
            "added_volume_mm3": None, "removed_volume_mm3": None,
            "image_a": None, "image_b": None, "side_by_side": None,
            "diff_3d": None, "diff_3d_changes_only": None, "diff_3d_views": {},
            "diff_2d": None, "diff_pixel": None, "counts": None}


def _visual_diff_mod() -> Any:
    """The pure-Python 2D diff module, or None when it is unavailable."""
    try:
        from services.plm import visual_diff
    except Exception:
        return None
    return visual_diff


def _is_unknown_method(exc: BaseException) -> bool:
    """True when an engine call failed because the loaded core does not have the
    method (an older ``libchudware`` predates ``doc_render_diff``)."""
    text = f"{exc}".lower()
    return any(t in text for t in ("unknown_method", "unknown method",
                                   "no such method", "unsupported method",
                                   "method not found", "unknown rpc"))


def _kind_of(doc: dict[str, Any]) -> str:
    schema = str(doc.get("schema", ""))
    return schema.split("/", 1)[0].rsplit(".", 1)[-1]  # part | asm | drawing | pcb | …


def visual_compare(doc_a: dict[str, Any], doc_b: dict[str, Any],
                   out_dir: str | Path,
                   views: list[str] | tuple[str, ...] | None = None,
                   annotate: bool = True) -> dict[str, Any]:
    """Visual two-revision compare: rendered images of the DIFFERENCE between
    two revisions (contract §7 ``eval_compare``).

    *A* is always the older revision, *B* the newer. Returns the block frozen in
    the PLM graphical-diff contract::

        {degraded, match_pct, identical, added_volume_mm3, removed_volume_mm3,
         image_a, image_b, side_by_side,
         diff_3d, diff_3d_changes_only, diff_3d_views,
         diff_2d, diff_pixel, counts}

    or ``{degraded: true, reason, hint}`` when nothing visual can be produced.
    Never raises for engine or Pillow absence, and never returns a path for a
    render that failed — every path in the result exists on disk.
    """
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"degraded": True, "reason": "out_dir_unwritable", "hint": f"{e}"}

    # ECAD boards/schematics diff with no engine at all — the stored canonical
    # documents carry the whole layout, so this path works on any machine.
    from services.plm import ecad_diff

    if ecad_diff.is_ecad_schema(doc_a) or ecad_diff.is_ecad_schema(doc_b):
        return _ecad_visual(doc_a, doc_b, out)

    kind = _kind_of(doc_a)
    if kind not in ("part", "asm", "drawing"):
        return {"degraded": True, "reason": "unsupported_kind",
                "hint": "visual compare is not available for this document type yet"}

    try:
        from services.mcp import core_bridge as cb
    except Exception:
        return dict(_DEGRADED_NO_ENGINE)
    try:
        if not cb.available():
            return dict(_DEGRADED_NO_ENGINE)
    except Exception:
        return dict(_DEGRADED_NO_ENGINE)

    ida = _load_doc(cb, doc_a, kind)
    idb = _load_doc(cb, doc_b, kind)
    if ida is None or idb is None:
        return {"degraded": True, "reason": "rebuild_failed",
                "hint": "one of the two versions could not be rebuilt for viewing"}

    result = _blank_visual()

    png_a, png_b = str(out / "revision_a.png"), str(out / "revision_b.png")
    ok_a = _render(cb, ida, png_a, kind)
    ok_b = _render(cb, idb, png_b, kind)
    if ok_a:
        result["image_a"] = png_a
    if ok_b:
        result["image_b"] = png_b
    if ok_a and ok_b:
        result.update(_compose(png_a, png_b, out))

    if kind == "drawing":
        result.update(_drawing_visual(cb, ida, idb, out))
    else:
        result.update(_geometry_visual(cb, ida, idb, out, views, annotate))

    # universal fallback: a registered pixel diff of the two plain renders. One
    # cheap PIL pass, and it is the only diff image available against an engine
    # too old for doc_render_diff.
    if ok_a and ok_b:
        vd = _visual_diff_mod()
        if vd is not None:
            try:
                r = vd.pixel_diff_image(png_a, png_b, str(out / "diff_pixel.png"))
            except Exception:
                r = None
            if isinstance(r, dict) and _existing(r.get("path")):
                result["diff_pixel"] = str(r["path"])

    return result


def _existing(path: Any) -> bool:
    """True when *path* is a usable on-disk file (never return a broken path)."""
    try:
        return bool(path) and Path(str(path)).is_file()
    except OSError:
        return False


def _ecad_visual(doc_a: dict[str, Any], doc_b: dict[str, Any],
                 out: Path) -> dict[str, Any]:
    """Board-layout diff of two canonical ECAD documents (no engine needed)."""
    vd = _visual_diff_mod()
    if vd is None:
        return {"degraded": True, "reason": "visual_diff_unavailable",
                "hint": "layout diff images are not available on this machine; "
                        "the change list above still applies"}
    try:
        r = vd.ecad_diff_image(doc_a, doc_b, str(out / "diff_2d.png"))
    except Exception:
        r = None
    if not isinstance(r, dict) or not _existing(r.get("path")):
        return {"degraded": True, "reason": "pillow_unavailable",
                "hint": "image rendering is not available on this machine; "
                        "the change list above still applies"}
    result = _blank_visual()
    result["diff_2d"] = str(r["path"])
    result["counts"] = {"added": int(r.get("added") or 0),
                        "removed": int(r.get("removed") or 0),
                        "unchanged": int(r.get("unchanged") or 0)}
    result["identical"] = not (result["counts"]["added"]
                               or result["counts"]["removed"])
    return result


def _drawing_visual(cb: Any, ida: str, idb: str, out: Path) -> dict[str, Any]:
    """Vector-level 2D diff of two drawing revisions from their ``drawing_get``
    payloads (the RPC the engine exposes for sheet/view geometry)."""
    vd = _visual_diff_mod()
    if vd is None:
        return {}
    try:
        get_a = cb.call("drawing_get", {"doc_id": ida})
        get_b = cb.call("drawing_get", {"doc_id": idb})
    except Exception:
        return {}
    if not isinstance(get_a, dict) or not isinstance(get_b, dict):
        return {}
    try:
        r = vd.drawing_diff_image(get_a, get_b, str(out / "diff_2d.png"))
    except Exception:
        r = None
    if not isinstance(r, dict) or not _existing(r.get("path")):
        return {}
    counts = {"added": int(r.get("added") or 0),
              "removed": int(r.get("removed") or 0),
              "unchanged": int(r.get("unchanged") or 0)}
    return {"diff_2d": str(r["path"]), "counts": counts,
            "identical": not (counts["added"] or counts["removed"])}


def _geometry_visual(cb: Any, ida: str, idb: str, out: Path,
                     views: list[str] | tuple[str, ...] | None,
                     annotate: bool = True) -> dict[str, Any]:
    """Rendered 3D geometry diffs via ``doc_render_diff`` (added green / removed
    red / unchanged grey, one shared camera framing).

    ``annotate`` bakes the view name + legend + bbox dims into the pixels — what
    an AI vision review needs from a bare PNG. The GUI draws those as real
    widgets and passes False, so its images stay clean.

    An engine without the method degrades to ``eval_compare`` for the match
    percentage plus the pixel-diff fallback the caller adds.
    """
    want = [str(v).upper() for v in (views or _DIFF_VIEWS)] or list(_DIFF_VIEWS)
    result: dict[str, Any] = {}
    rendered: dict[str, str] = {}
    have_metrics = False
    unavailable = False

    for view in want:
        path = str(out / f"diff_3d_{view.lower()}.png")
        try:
            r = cb.call("doc_render_diff", {"doc_a": ida, "doc_b": idb,
                                            "out_path": path, "view": view,
                                            "mode": "context", "annotate": annotate})
        except Exception as e:  # noqa: BLE001 — an older engine has no such method
            if _is_unknown_method(e):
                unavailable = True
                break
            continue
        if not isinstance(r, dict):
            continue
        if str(r.get("code") or "") == "unknown_method" or _is_unknown_method(
                Exception(str(r.get("error") or r.get("reason") or ""))):
            unavailable = True
            break
        if r.get("ok") is False or r.get("degraded"):
            continue
        if not _existing(r.get("path") or path):
            continue
        rendered[view] = str(r.get("path") or path)
        if not have_metrics:
            have_metrics = True
            result["match_pct"] = r.get("match_pct")
            result["added_volume_mm3"] = r.get("added_volume_mm3")
            result["removed_volume_mm3"] = r.get("removed_volume_mm3")
            result["identical"] = bool(r.get("identical"))

    if rendered:
        result["diff_3d_views"] = rendered
        result["diff_3d"] = rendered.get("ISO") or next(iter(rendered.values()))

    if not unavailable:
        co = str(out / "diff_3d_changes_only.png")
        try:
            r = cb.call("doc_render_diff", {"doc_a": ida, "doc_b": idb,
                                            "out_path": co, "view": "ISO",
                                            "mode": "changes_only",
                                            "annotate": annotate})
            if (isinstance(r, dict) and r.get("ok") is not False
                    and not r.get("degraded") and _existing(r.get("path") or co)):
                result["diff_3d_changes_only"] = str(r.get("path") or co)
        except Exception:  # noqa: BLE001
            pass

    if not have_metrics:
        # fallback source of the geometric match percentage
        try:
            r = cb.call("eval_compare", {"doc_a": ida, "doc_b": idb})
            if isinstance(r, dict) and not r.get("degraded"):
                result["match_pct"] = r.get("match_pct")
                if r.get("identical") is not None:
                    result["identical"] = bool(r.get("identical"))
        except Exception:  # noqa: BLE001
            pass
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


def _compose(png_a: str, png_b: str, out: Path) -> dict[str, Any]:
    """The two plain renders composed side-by-side. Pillow is optional; without
    it the two individual panels still stand."""
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
    except Exception:
        pass
    return result

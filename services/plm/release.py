"""services.plm.release — release orchestration (contract §5).

A release is atomic and does all of (contract §5): pre-flight → rev stamp →
lifecycle transition → geometry freeze → BOM freeze/ref pinning. The vault
engine already cuts the atomic release entry; this module adds the
full release semantics around it:

* **Pre-flight** (§5.1) — unsaved working changes, stale references
  (superseded component pins, via :mod:`assist`), never-released components,
  and an optional verify hook (e.g. ``verify_battery``). Ease-of-use: the
  problems are WARNINGS a user can override with explicit confirmation
  ("warn, don't wall" — DoD), except that a release note is always required.
* **Ref pinning + BOM freeze** (§5.5) — releasing an assembly pins its
  component references to exact revs in ``manifest.bom_freezes`` and freezes
  the BOM rows so ``bom_rollup`` (services/deliverables/joins.py) reproduces
  the same rollup forever, even after components change.
* **Title-block stamp** — the released rev letter is stamped on drawings that
  detail the released item, through the existing ``drawing_set_titleblock``
  engine path. The vault has no live doc sessions, so this emits STAMP ORDERS
  that the app/MCP layer applies (``apply_stamp_orders`` with a doc-id
  resolver + RPC callable); superseded old revs get a watermark field.
"""
from __future__ import annotations

from typing import Any, Callable

from services.plm import assist, items as items_mod
from services.plm import manifest as manifest_mod
from services.plm import whereused


# ---------------------------------------------------------------------------
# pre-flight (contract §5.1 — overridable with confirm, DoD)
# ---------------------------------------------------------------------------

def preflight(vault: Any, item_id: str,
              verify_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
              ) -> dict[str, Any]:
    """Run the release pre-flight battery. Returns ``{ok, problems}`` where
    each problem is ``{kind, hint}`` in plain language (no git vocabulary).

    Problem kinds: ``unsaved_changes`` | ``stale_ref`` |
    ``unreleased_component`` | ``verify_failed``.
    """
    m = vault.manifest()
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        return {"ok": False,
                "problems": [{"kind": "invalid_item",
                              "hint": f"no such item: {item_id}"}]}
    problems: list[dict[str, Any]] = []

    # 1. unsaved working changes anywhere in the project (§5.1)
    dirty = vault._dirty_items(m)
    if dirty:
        names = ", ".join(sorted(
            (manifest_mod.find_item(m, d) or {}).get("path", d) for d in dirty))
        problems.append({"kind": "unsaved_changes",
                         "hint": f"there are unsaved changes in: {names}"})

    # 2. stale references — an assembly consuming a superseded component rev
    for flag in assist.stale_refs(m):
        problems.append({"kind": "stale_ref", "hint": flag["hint"]})

    # 3. never-released components of the assembly being released
    docs = whereused.docs_at_head(vault)
    doc = docs.get(item_id)
    if isinstance(doc, dict):
        for pin in component_pins(m, doc):
            if pin["rev"] is None:
                problems.append({
                    "kind": "unreleased_component",
                    "hint": (f"{pin['path']} has never been released; its current "
                             "working version will be pinned as-is")})

    # 4. optional verify battery hook
    if verify_hook is not None:
        try:
            verdict = verify_hook(item) or {}
        except Exception:
            verdict = {"ok": False, "hint": "the design checks could not be run"}
        if not verdict.get("ok", False):
            problems.append({"kind": "verify_failed",
                             "hint": verdict.get("hint", "the design checks did not pass")})

    return {"ok": not problems, "problems": problems}


# ---------------------------------------------------------------------------
# component pinning + BOM freeze (contract §5.5, plan §3.4)
# ---------------------------------------------------------------------------

def component_pins(m: dict[str, Any], assembly_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The exact-rev pin set for an assembly's component references.

    Each pin: ``{item_id, rev, qty, part_number, path}`` (the §4 pin shape
    plus additive fields so the freeze is self-describing). ``rev`` is the
    component's current released rev (None when never released — pre-flight
    warns; the release commit still captures its exact content).
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for comp in assembly_doc.get("components", []) or []:
        target = whereused._resolve_ref(m, whereused._component_ref(comp))
        if target is None:
            continue
        if target["item_id"] not in counts:
            order.append(target["item_id"])
        counts[target["item_id"]] = counts.get(target["item_id"], 0) + 1
    pins = []
    for tid in order:
        it = manifest_mod.find_item(m, tid)
        pins.append({"item_id": tid, "rev": it.get("rev"), "qty": counts[tid],
                     "part_number": it.get("part_number"), "path": it["path"]})
    return pins


def freeze_rows(freeze: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a ``manifest.bom_freezes`` entry into the row shape the existing
    ``bom_rollup`` (services/deliverables/joins.py) consumes:
    ``{item, qty, source}`` per line, deterministic order.

    Frozen means frozen: the rows come ONLY from the freeze pins, so later
    component edits/releases never change the output (DoD round-trip).
    """
    rows = []
    for pin in freeze.get("pins", []) or []:
        label = pin.get("part_number") or pin.get("path") or pin["item_id"]
        rev = pin.get("rev")
        rows.append({"item": f"{label} rev {rev}" if rev else f"{label} (unreleased)",
                     "qty": pin.get("qty", 1),
                     "source": "made"})
    return rows


def latest_freeze(m: dict[str, Any], assembly_item_id: str) -> dict[str, Any] | None:
    """The newest BOM freeze recorded for an assembly (None when unreleased)."""
    freezes = [fz for fz in m.get("bom_freezes", []) or []
               if fz.get("assembly_item") == assembly_item_id]
    return freezes[-1] if freezes else None


def frozen_bom(vault: Any, assembly_item_id: str) -> dict[str, Any]:
    """The frozen BOM rollup for a released assembly: the frozen rows plus the
    ``bom_rollup`` sheet block built from them (aicad.sourcing/1-compatible
    row shape). Raises ``KeyError`` when the assembly has no freeze yet."""
    m = vault.manifest()
    fz = latest_freeze(m, assembly_item_id)
    if fz is None:
        raise KeyError(assembly_item_id)
    rows = freeze_rows(fz)
    out: dict[str, Any] = {"rev": fz.get("rev"), "pins": fz.get("pins", []),
                           "rows": rows}
    try:
        from services.deliverables.joins import bom_rollup

        asm = manifest_mod.find_item(m, assembly_item_id) or {}
        out["rollup"] = bom_rollup(rows, level=asm.get("part_number") or "Top")
    except ImportError:
        pass  # rows are the contract; the sheet block is garnish
    return out


# ---------------------------------------------------------------------------
# geometry freeze (contract §5 step 4 — a released rev is byte-exact forever)
# ---------------------------------------------------------------------------

def freeze_geometry_step(doc: dict[str, Any]) -> bytes | None:
    """The frozen B-rep (AP214 STEP bytes) for a document's recipe, or ``None``.

    Rebuilds *doc*'s recipe into a throwaway engine document (the same doc-load
    contract :func:`services.plm.compare._load_doc` uses) and exports it with
    ``doc_export_step``, which works for parts and assemblies alike.

    STRICTLY best-effort: no engine, an unbuildable recipe, an export failure or
    any exception yields ``None``. A release must never fail because the frozen
    interchange geometry could not be produced.
    """
    if not isinstance(doc, dict):
        return None
    schema = str(doc.get("schema", "") or "")
    kind = schema.split("/", 1)[0].rsplit(".", 1)[-1]
    if kind not in ("part", "asm", "assembly"):
        return None  # only parts/assemblies have a B-rep to freeze
    try:
        from services.mcp import core_bridge as cb
    except Exception:
        return None
    try:
        if not cb.available():
            return None
    except Exception:
        return None

    import tempfile
    from pathlib import Path as _Path

    from services.plm import compare as compare_mod

    doc_id = compare_mod._load_doc(cb, doc, kind)  # noqa: SLF001 — same package
    if doc_id is None:
        return None
    tmp = _Path(tempfile.mkdtemp(prefix="chudware_plm_freeze_")) / "geometry.step"
    try:
        r = cb.call("doc_export_step", {"doc_id": doc_id, "path": str(tmp)})
        if isinstance(r, dict) and (r.get("ok") is False or r.get("degraded")):
            return None
        if not tmp.is_file():
            return None
        data = tmp.read_bytes()
        return data or None
    except Exception:
        return None
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc_id})
        except Exception:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        try:
            tmp.parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the full release flow
# ---------------------------------------------------------------------------

def release(vault: Any, item_id: str, message: str, *,
            confirm: bool = False,
            verify_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
            geometry_step: bytes | None = None) -> dict[str, Any]:
    """Release an item with full release semantics (pre-flight → pin → freeze →
    rev stamp → lifecycle → stamp orders).

    Pre-flight problems block unless ``confirm=True`` (explicit override —
    "warn, don't wall"). Returns the vault release result plus
    ``{preflight, bom_freeze?, stamp_orders}``.
    """
    from services.plm.vault import VaultError

    m = vault.manifest()
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        raise VaultError("invalid_item", f"no such item: {item_id}")
    try:
        items_mod.require_in_work_for_release(item)
    except items_mod.ItemRuleError as e:
        raise VaultError(e.reason, e.hint)

    pf = preflight(vault, item_id, verify_hook=verify_hook)
    if not pf["ok"] and not confirm:
        err = VaultError("preflight_failed",
                         "; ".join(p["hint"] for p in pf["problems"]))
        err.problems = pf["problems"]  # structured detail for the envelope
        raise err

    # component pins for assemblies (from the item's current working doc)
    docs = whereused.docs_at_head(vault)
    pins = None
    doc = docs.get(item_id)
    if isinstance(doc, dict) and (doc.get("components") or item["kind"] == "assembly"):
        pins = component_pins(m, doc)

    # geometry freeze (§5 step 4). Best-effort: a caller may hand us the bytes;
    # otherwise we rebuild the recipe through the engine. No engine / no
    # buildable recipe simply means this rev carries no frozen interchange
    # geometry — the release itself must never fail for it.
    if geometry_step is None and isinstance(doc, dict):
        geometry_step = freeze_geometry_step(doc)

    res = vault.release(item_id, message, geometry_step=geometry_step,
                        bom_pins=pins)
    res["geometry_frozen"] = geometry_step is not None
    res["preflight"] = pf
    if pins is not None:
        fz = latest_freeze(vault.manifest(), item_id)
        res["bom_freeze"] = {"rev": res["rev"], "pins": pins,
                             "rows": freeze_rows(fz or {"pins": pins})}
    res["stamp_orders"] = stamp_orders(vault, item_id, res["rev"])
    return res


# ---------------------------------------------------------------------------
# drawing title-block stamping (rev letter + superseded watermark)
# ---------------------------------------------------------------------------

def stamp_orders(vault: Any, item_id: str, rev: str) -> list[dict[str, Any]]:
    """Stamp orders for every drawing that details the released item.

    Each order:: ``{drawing_item, drawing_path, fields: {rev, part_number}}``
    — ready for ``drawing_set_titleblock`` once resolved to a live doc id
    (the vault holds documents at rest, not sessions).
    """
    m = vault.manifest()
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        return []
    docs = whereused.docs_at_head(vault)
    orders = []
    for rec in whereused.build_index(m, docs).get(item_id, []):
        if rec["via"] != "source":
            continue
        consumer = manifest_mod.find_item(m, rec["item_id"])
        if consumer is None or consumer["kind"] not in ("drawing", "cam"):
            continue
        if consumer["kind"] == "drawing":
            orders.append({
                "drawing_item": consumer["item_id"],
                "drawing_path": consumer["path"],
                "fields": {"rev": rev,
                           "part_number": item.get("part_number") or ""},
            })
    return orders


def watermark_for(item: dict[str, Any], rev: str) -> str | None:
    """The print watermark for a given rev of an item: ``"SUPERSEDED"`` when
    that rev is no longer the current released one (old-rev prints), else
    None. Deterministic — drive the print/export path with it."""
    for rr in item.get("released_revs", []) or []:
        if rr.get("rev") == rev:
            if rr.get("lifecycle") == manifest_mod.LIFECYCLE_SUPERSEDED:
                return "SUPERSEDED"
            return None
    return None


def apply_stamp_orders(orders: list[dict[str, Any]],
                       resolve_doc_id: Callable[[str], str | None],
                       call: Callable[[str, dict[str, Any]], Any],
                       sheet_id: str = "s1") -> list[dict[str, Any]]:
    """Apply stamp orders through the existing engine path
    (``drawing_set_titleblock``). ``resolve_doc_id`` maps a drawing's project
    path to its live doc id (None → skipped, reported). Returns per-order
    results ``{drawing_path, applied, error?}`` — best-effort, never raises.
    """
    results = []
    for order in orders:
        doc_id = resolve_doc_id(order["drawing_path"])
        if not doc_id:
            results.append({"drawing_path": order["drawing_path"], "applied": False,
                            "error": "not open"})
            continue
        try:
            call("drawing_set_titleblock",
                 {"doc_id": doc_id, "sheet_id": sheet_id, "fields": order["fields"]})
            results.append({"drawing_path": order["drawing_path"], "applied": True})
        except Exception as e:  # noqa: BLE001 — best-effort by design
            results.append({"drawing_path": order["drawing_path"], "applied": False,
                            "error": str(e)})
    return results

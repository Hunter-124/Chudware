"""Spec validation + provenance slot resolution (contract §2, §4).

The validator turns a model-authored ``spec`` into a *resolved* structure the template engines
write verbatim, enforcing the one rule that makes a deliverable trustworthy: **a load-bearing
slot MUST reference a tool result, never a literal** (§4.3). It fails BEFORE any file is written
(§4.4):

- literal (bare value) in a load-bearing slot → ``provenance_violation``
- a ref naming a missing/ambiguous result id or a key absent from the payload → ``slot_unresolved``
- malformed spec (kind mismatch, unknown template, bad block shape) → ``invalid_spec``

Load-bearing-ness is declared by the **template** (§4.3, §8): the template says which slot names
(and which workbook columns) are load-bearing per block type, so the model cannot silently
downgrade a value slot to prose.
"""
from __future__ import annotations

from typing import Any

from .errors import DeliverableError


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and "from" in value


def _dig(payload: Any, key: str) -> Any:
    """Read ``payload[key]`` where ``key`` may be a dotted path. Raises KeyError if absent."""
    cur = payload
    for part in str(key).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(key)
    return cur


def _fmt(raw: Any, unit: str | None, rnd: Any) -> str:
    if isinstance(raw, bool):
        s = str(raw)
    elif isinstance(raw, (int, float)) and rnd is not None:
        s = f"{float(raw):.{int(rnd)}f}"
    else:
        s = str(raw)
    return f"{s} {unit}" if unit else s


def _resolve_ref(name: str, value: dict, attached: dict) -> dict:
    """Resolve a provenance ref (§4.2). Returns a resolved-value dict; raises on failure."""
    frm = value.get("from") or {}
    if not isinstance(frm, dict):
        raise DeliverableError("invalid_spec", f"slot {name!r} 'from' must be an object")
    tool = frm.get("tool")
    key = frm.get("key")
    result_id = frm.get("result")
    if not key:
        raise DeliverableError("invalid_spec", f"slot {name!r} ref missing 'key'")

    if result_id is not None:
        entry = attached.get(result_id)
        if entry is None:
            raise DeliverableError(
                "slot_unresolved", f"slot {name!r}: result {result_id!r} not in attached_results")
    else:
        cands = [rid for rid, e in attached.items() if isinstance(e, dict) and e.get("tool") == tool]
        if not cands:
            raise DeliverableError(
                "slot_unresolved", f"slot {name!r}: no attached result for tool {tool!r}")
        if len(cands) > 1:
            raise DeliverableError(
                "slot_unresolved",
                f"slot {name!r}: {len(cands)} results match tool {tool!r}; add a 'result' id")
        result_id = cands[0]
        entry = attached[result_id]

    payload = entry.get("payload", {}) if isinstance(entry, dict) else {}
    try:
        raw = _dig(payload, key)
    except KeyError:
        raise DeliverableError(
            "slot_unresolved", f"slot {name!r}: result {result_id!r} has no key {key!r}")

    unit = value.get("unit")
    rnd = value.get("round")
    return {
        "raw": raw,
        "unit": unit,
        "text": _fmt(raw, unit, rnd),
        "ref": {"tool": tool or (entry.get("tool") if isinstance(entry, dict) else None),
                "key": key, "result": result_id},
    }


def _resolve_slot(name: str, value: Any, load_bearing: bool, attached: dict,
                  source_refs: list) -> dict:
    if _is_ref(value):
        rv = _resolve_ref(name, value, attached)
        source_refs.append(dict(rv["ref"]))
        return rv
    # Not a ref. A dict-without-'from' is malformed; a scalar is a literal.
    if isinstance(value, dict):
        raise DeliverableError(
            "invalid_spec", f"slot {name!r} is neither a literal nor a 'from' ref")
    if load_bearing:
        raise DeliverableError(
            "provenance_violation",
            f"slot {name!r} must reference a tool result, not a literal")
    return {"raw": value, "unit": None, "text": str(value), "ref": None}


def _resolve_cell(col: int, cell: Any, load_bearing_cols: set, attached: dict,
                  source_refs: list) -> dict:
    """Resolve one workbook/table cell: a live formula, a resolved value ref, or a label.

    A cell in a load-bearing column is enforced only when it is a bare **numeral** (§4.3: a
    label cell is free prose even in a value column) — so a header/label string is allowed, but a
    typed-in number where a tool result belongs fails with ``provenance_violation``.
    """
    if isinstance(cell, str) and cell.startswith("="):
        return {"formula": cell}
    numeric_literal = isinstance(cell, (int, float)) and not isinstance(cell, bool)
    load_bearing = col in load_bearing_cols and (_is_ref(cell) or numeric_literal)
    return _resolve_slot(f"col{col}", cell, load_bearing, attached, source_refs)


def validate_and_resolve(spec: Any, expected_kind: str, template, source_refs: list) -> dict:
    """Validate ``spec`` against ``expected_kind`` + ``template`` and resolve every slot.

    Appends each consumed provenance triple to ``source_refs``. Raises
    :class:`DeliverableError` on the first failure (nothing is written by the caller after a
    raise). Returns a resolved spec dict the template builders consume.
    """
    if not isinstance(spec, dict):
        raise DeliverableError("invalid_spec", "spec must be a JSON object")

    kind = spec.get("kind")
    if kind != expected_kind:
        raise DeliverableError(
            "invalid_spec",
            f"this tool builds {expected_kind!r}; spec kind is {kind!r}")

    attached = spec.get("attached_results") or {}
    if not isinstance(attached, dict):
        raise DeliverableError("invalid_spec", "'attached_results' must be an object")

    outline = spec.get("outline")
    if not isinstance(outline, list):
        raise DeliverableError("invalid_spec", "'outline' must be a list of blocks")

    blocks: list[dict] = []
    for i, blk in enumerate(outline):
        if not isinstance(blk, dict):
            raise DeliverableError("invalid_spec", f"outline[{i}] must be an object")
        btype = blk.get("type")
        if not isinstance(btype, str) or not btype:
            raise DeliverableError("invalid_spec", f"outline[{i}] missing 'type'")

        lb_slots = set(template.load_bearing_slots(btype))
        slots_out: dict = {}
        raw_slots = blk.get("slots") or {}
        if not isinstance(raw_slots, dict):
            raise DeliverableError("invalid_spec", f"outline[{i}] 'slots' must be an object")
        for sname, sval in raw_slots.items():
            slots_out[sname] = _resolve_slot(sname, sval, sname in lb_slots, attached, source_refs)

        rows_out: list = []
        raw_rows = blk.get("rows")
        if raw_rows is not None:
            if not isinstance(raw_rows, list):
                raise DeliverableError("invalid_spec", f"outline[{i}] 'rows' must be a list")
            lb_cols = set(template.load_bearing_columns(btype))
            for r, row in enumerate(raw_rows):
                if not isinstance(row, list):
                    raise DeliverableError(
                        "invalid_spec", f"outline[{i}].rows[{r}] must be a list of cells")
                rows_out.append([
                    _resolve_cell(c, cell, lb_cols, attached, source_refs)
                    for c, cell in enumerate(row)
                ])

        body = blk.get("body") or []
        if isinstance(body, str):
            body = [body]
        blocks.append({
            "type": btype,
            "heading": blk.get("heading", ""),
            "body": list(body),
            "slots": slots_out,
            "rows": rows_out,
        })

    return {
        "kind": kind,
        "title": spec.get("title", ""),
        "run_id": spec.get("run_id"),
        "template": spec.get("template"),
        "blocks": blocks,
    }

"""Provenance joins — the layer between raw engine tool results and a deliverable spec.

A deliverable is only trustworthy if every load-bearing number traces to a tool result
(contract §4). This module is the *join*: it takes the results an agent already has — from
``describe_part`` / ``measure_part``, ``run_fea_static``, a material-data answer (Pillar 8),
BOM + sourced-part cost data, CAM setup sheets, drawing exports — and produces

  1. **attached_results** entries in the frozen ``{tool, ts, payload}`` shape (contract §4.1),
     the model's evidence bag; and
  2. **slot / cell refs** (``{"from": {tool, key, result}, ...}``, contract §4.2) that a spec
     drops into a load-bearing slot instead of a number.

So a caller (or a golden test) builds a fully-provenanced spec deterministically from live
project state, hand-typing nothing. Everything here is pure data-shaping — no engine imports, no
I/O — so it is import-cheap and unit-testable without the office libraries. Missing / partial
inputs degrade gracefully (a source-less BOM line still yields a row; an absent optional key is
simply skipped), never raising.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- building blocks

def attach(result_id: str, tool: str, payload: dict, ts: str | None = None) -> dict:
    """One ``attached_results`` entry (contract §4.1): ``{tool, ts, payload}`` keyed by id.

    Returns ``{result_id: entry}`` so calls compose with :func:`results_map`.
    """
    return {result_id: {"tool": tool, "ts": ts or _now(), "payload": dict(payload or {})}}


def results_map(*entries: dict) -> dict:
    """Merge attach() results (and plain dicts) into one ``attached_results`` map."""
    out: dict = {}
    for e in entries:
        if e:
            out.update(e)
    return out


def ref(result_id: str, key: str, *, tool: str | None = None,
        unit: str | None = None, round: int | None = None) -> dict:  # noqa: A002 - contract name
    """A provenance slot/cell ref (contract §4.2) — what a load-bearing slot holds instead of a
    number. ``key`` may be a dotted path into the payload."""
    frm: dict = {"key": key, "result": result_id}
    if tool is not None:
        frm["tool"] = tool
    out: dict = {"from": frm}
    if unit is not None:
        out["unit"] = unit
    if round is not None:
        out["round"] = round
    return out


# --------------------------------------------------------------------------- result normalizers
# Each takes a raw engine result and returns an attach() entry with a stable payload shape, so
# refs against it use predictable keys regardless of small differences in the source tool output.


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def mass_props(result: dict, result_id: str = "mass1", ts: str | None = None) -> dict:
    """Normalize a describe_part / measure_part mass-properties result.

    Payload keys: ``mass_g``, ``volume_mm3``, ``surface_area_mm2``, ``cg_x_mm``/``cg_y_mm``/
    ``cg_z_mm``, ``material``. Absent inputs are omitted (a ref to a missing key is caught by the
    validator as ``slot_unresolved`` — never a silent blank).
    """
    r = result or {}
    cg = r.get("cg") or r.get("center_of_mass") or {}
    payload = {
        "mass_g": _first(r, "mass_g", "mass"),
        "volume_mm3": _first(r, "volume_mm3", "volume"),
        "surface_area_mm2": _first(r, "surface_area_mm2", "surface_area", "area"),
        "cg_x_mm": _first(r, "cg_x_mm", "cg_x") or _first(cg, "x"),
        "cg_y_mm": _first(r, "cg_y_mm", "cg_y") or _first(cg, "y"),
        "cg_z_mm": _first(r, "cg_z_mm", "cg_z") or _first(cg, "z"),
        "material": _first(r, "material", "material_name"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return attach(result_id, "describe_part", payload, ts)


def fea_static(result: dict, result_id: str = "fea1", ts: str | None = None) -> dict:
    """Normalize a run_fea_static result: ``max_von_mises_mpa``, ``min_fos``, ``max_disp_mm``,
    optional ``yield_strength_mpa`` / ``margin``."""
    r = result or {}
    payload = {
        "max_von_mises_mpa": _first(r, "max_von_mises_mpa", "max_stress_mpa", "max_stress"),
        "min_fos": _first(r, "min_fos", "min_factor_of_safety", "fos"),
        "max_disp_mm": _first(r, "max_disp_mm", "max_displacement_mm", "max_disp"),
        "yield_strength_mpa": _first(r, "yield_strength_mpa", "yield_mpa"),
        "margin": _first(r, "margin", "margin_of_safety"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return attach(result_id, "run_fea_static", payload, ts)


def material_data(result: dict, result_id: str = "mat1", ts: str | None = None) -> dict:
    """Normalize a Pillar 8 material-data answer — properties **plus its citation**.

    Payload keys: ``density_kg_m3``, ``youngs_modulus_gpa``, ``yield_strength_mpa``,
    ``ultimate_strength_mpa``, ``poisson``, and the answer-contract fields ``source`` +
    ``fetched_ts`` (shown in the calc package, contract step 2).
    """
    r = result or {}
    payload = {
        "density_kg_m3": _first(r, "density_kg_m3", "density"),
        "youngs_modulus_gpa": _first(r, "youngs_modulus_gpa", "youngs_modulus", "E_gpa"),
        "yield_strength_mpa": _first(r, "yield_strength_mpa", "yield_mpa", "yield_strength"),
        "ultimate_strength_mpa": _first(r, "ultimate_strength_mpa", "uts_mpa", "ultimate_strength"),
        "poisson": _first(r, "poisson", "poisson_ratio", "nu"),
        "source": _first(r, "source", "citation"),
        "fetched_ts": _first(r, "fetched_ts", "fetched", "retrieved_ts"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return attach(result_id, "material_data", payload, ts)


def cam_setup_sheet(result: dict, result_id: str = "cam1", ts: str | None = None) -> dict:
    """Normalize a CAM setup-sheet result: ``tool_count``, ``op_count``, ``cycle_time_min``,
    ``stock_removed_cm3``."""
    r = result or {}
    payload = {
        "tool_count": _first(r, "tool_count", "num_tools"),
        "op_count": _first(r, "op_count", "num_ops", "operation_count"),
        "cycle_time_min": _first(r, "cycle_time_min", "cycle_time", "est_cycle_time_min"),
        "stock_removed_cm3": _first(r, "stock_removed_cm3", "material_removed_cm3"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return attach(result_id, "cam_setup_sheet", payload, ts)


def drawing_export(result: dict, result_id: str = "dwg1", ts: str | None = None) -> dict:
    """Normalize a drawing/export result: ``path``, ``sheet_count``, ``view_count``."""
    r = result or {}
    payload = {
        "path": _first(r, "path", "file", "export_path"),
        "sheet_count": _first(r, "sheet_count", "sheets", "num_sheets"),
        "view_count": _first(r, "view_count", "views", "num_views"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return attach(result_id, "make_drawing", payload, ts)


# --------------------------------------------------------------------------- BOM / cost rollup


def bom_rollup(items: list[dict], *, level: str = "Top", result_prefix: str = "bom") -> dict:
    """Join a BOM parts list (+ optional sourced-part cost) into a workbook sheet block.

    Each ``item`` is ``{item, qty, unit_cost?, source?}``. ``qty`` and ``unit_cost`` are
    engineering values, so they become **attached results** and the sheet cells reference them
    (never typed). ``unit_cost`` / ``source`` are optional — a BOM without sourcing data still
    produces a valid sheet (cost cells blank), degrading gracefully (contract step 3).

    Returns ``{"block": <sheet block for the spec outline>, "attached": <attached_results map>}``
    with a live ``=Qty*UnitCost`` extended-cost column and a ``=SUM(...)`` cost rollup row. Column
    order matches the ``bom_workbook`` template: Item | Qty | Unit Cost | Ext Cost | Source.
    """
    attached: dict = {}
    header = ["Item", "Qty", "Unit Cost", "Ext Cost", "Source"]
    rows: list[list[Any]] = [header]
    has_cost = False
    for i, it in enumerate(items or [], start=1):
        rid = f"{result_prefix}{i}"
        name = str(it.get("item") or it.get("name") or f"Part {i}")
        qty = it.get("qty", it.get("quantity"))
        unit_cost = it.get("unit_cost", it.get("cost"))
        source = it.get("source") or it.get("supplier") or ""

        payload = {"qty": qty}
        qty_cell: Any = ref(rid, "qty")
        if unit_cost is not None:
            payload["unit_cost"] = unit_cost
            has_cost = True
        attached.update(attach(rid, "bom", payload))

        excel_row = i + 1  # header is row 1
        unit_cell: Any = ref(rid, "unit_cost", unit=None) if unit_cost is not None else ""
        ext_cell = f"=B{excel_row}*C{excel_row}" if unit_cost is not None else ""
        rows.append([name, qty_cell, unit_cell, ext_cell, source])

    if has_cost and len(rows) > 1:
        last = len(rows)               # excel row index of the final item
        rows.append(["Total", "", "", f"=SUM(D2:D{last})", ""])

    block = {"type": "sheet", "heading": level, "rows": rows}
    return {"block": block, "attached": attached}

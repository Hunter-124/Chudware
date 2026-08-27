"""Typed manufacturing-plan tools backed by the mission design ledger.

The plan is design data, not a physical-machine command.  It is therefore
registered in the ``cad`` family by :mod:`services.mcp.server`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_AUDIT_TYPES = {
    "dimension_tolerance", "thread", "finish", "note", "gdt", "datum",
    "weld", "title_field",
}
_MACHINING_PROCESSES = {"cnc_mill", "lathe"}
_VALID_VIEWS = {"full", "audit_spec", "work_instructions"}


def _objects(value: Any, field: str, *, allow_strings: bool = False) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    out: list[dict] = []
    for index, row in enumerate(value):
        if isinstance(row, dict):
            out.append(dict(row))
        elif allow_strings and isinstance(row, str) and row.strip():
            out.append({"id": f"{field}_{index + 1}", "text": row.strip()})
        else:
            raise ValueError(f"{field}[{index}] must be an object")
    return out


def _required_text(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"mfg_plan requires {field}")
    return text


def _profile_snapshot(process_id: str, supplied: Any) -> dict:
    from services.agent.geometry_helpers import (  # noqa: PLC0415
        _KNOWN_PROFILE_IDS,
        _profile,
        canonical_process_strict,
    )
    canonical = canonical_process_strict(process_id)
    if canonical not in _KNOWN_PROFILE_IDS():
        raise ValueError(
            f"unknown manufacturing process {process_id!r}; a real DfM profile is required"
        )
    if isinstance(supplied, dict) and supplied:
        snapshot = dict(supplied)
        stated = snapshot.get("process")
        if stated and canonical_process_strict(str(stated)) != canonical:
            raise ValueError(
                f"process profile {stated!r} does not match process id {canonical!r}"
            )
        snapshot.setdefault("process", canonical)
        return snapshot
    return dict(_profile(str(canonical)))


def _normalize_source(plan: dict) -> dict:
    raw = plan.get("source_doc")
    if isinstance(raw, str):
        source = {"doc_id": raw}
    elif isinstance(raw, dict):
        source = dict(raw)
    else:
        source = {}
    source.setdefault("doc_id", plan.get("source_doc_id") or plan.get("doc_id"))
    if plan.get("source_revision") is not None:
        source.setdefault("revision", plan["source_revision"])
    source["doc_id"] = _required_text(source.get("doc_id"), "source_doc.doc_id")
    if source.get("revision") is not None:
        source["revision"] = str(source["revision"])
    return source


def _normalize_process(plan: dict) -> dict:
    raw = plan.get("process")
    if isinstance(raw, str):
        process = {"id": raw}
    elif isinstance(raw, dict):
        process = dict(raw)
    else:
        process = {}
    process_id = _required_text(
        process.get("id") or process.get("process_id"), "process.id"
    )
    supplied = process.get("profile_snapshot") or process.get("profile")
    snapshot = _profile_snapshot(process_id, supplied)
    canonical = str(snapshot.get("process") or process_id)
    return {"id": canonical, "profile_snapshot": snapshot}


def _normalize_material(plan: dict) -> dict:
    raw = plan.get("material")
    if isinstance(raw, str):
        material = {"key": raw}
    elif isinstance(raw, dict):
        material = dict(raw)
    else:
        material = {}
    material["key"] = _required_text(
        material.get("key") or material.get("id") or material.get("name"),
        "material.key",
    )
    material["condition"] = _required_text(
        material.get("condition") or plan.get("material_condition"),
        "material.condition",
    )
    return material


def _normalize_resource(value: Any, field: str) -> dict:
    if isinstance(value, str) and value.strip():
        return {"id" if field == "machine" else "description": value.strip()}
    if not isinstance(value, dict) or not value:
        raise ValueError(f"mfg_plan requires non-empty {field}")
    return dict(value)


def _geometry_refs(row: dict) -> list[str]:
    raw = row.get("geometry_spec_ids", row.get("geometry_specs"))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw if str(value).strip()]


def _normalize_ordered(rows: Any, field: str) -> list[dict]:
    out = _objects(rows, field)
    if not out:
        raise ValueError(f"mfg_plan requires at least one {field[:-1]}")
    seen_ids: set[str] = set()
    seen_order: set[float] = set()
    for index, row in enumerate(out):
        row_id = _required_text(row.get("id"), f"{field}[{index}].id")
        if row_id in seen_ids:
            raise ValueError(f"{field} ids must be unique; duplicate {row_id!r}")
        seen_ids.add(row_id)
        order = row.get("order", row.get("sequence", (index + 1) * 10))
        if isinstance(order, bool) or not isinstance(order, (int, float)):
            raise ValueError(f"{field}[{index}].order must be numeric")
        if float(order) in seen_order:
            raise ValueError(f"{field} order values must be unique")
        seen_order.add(float(order))
        refs = _geometry_refs(row)
        if not refs:
            raise ValueError(f"{field}[{index}] must reference geometry_spec_ids")
        row["id"] = row_id
        row["order"] = order
        row["geometry_spec_ids"] = refs
    return sorted(out, key=lambda row: (float(row["order"]), row["id"]))


def _normalize_audit_rows(value: Any, field: str) -> list[dict]:
    rows = _objects(value, field)
    seen: set[str] = set()
    for index, row in enumerate(rows):
        req_id = _required_text(
            row.get("requirement_id") or row.get("id"),
            f"{field}[{index}].requirement_id",
        )
        if req_id in seen:
            raise ValueError(f"audit requirement ids must be unique; duplicate {req_id!r}")
        seen.add(req_id)
        row["requirement_id"] = req_id
        row_type = str(row.get("type") or row.get("kind") or "").strip().lower()
        aliases = {"dimension": "dimension_tolerance", "surface_finish": "finish"}
        row_type = aliases.get(row_type, row_type)
        if row_type not in _AUDIT_TYPES:
            raise ValueError(
                f"{field}[{index}] has unsupported audit type {row_type!r}; "
                f"valid types: {', '.join(sorted(_AUDIT_TYPES))}"
            )
        row["type"] = row_type
        row["critical"] = bool(row.get("critical", True))
        if row_type == "dimension_tolerance":
            value = row.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field}[{index}] dimension requires numeric value")
            tolerance = row.get("tolerance")
            if tolerance is None and any(
                key in row for key in ("tol_type", "tol_upper", "tol_lower")
            ):
                tolerance = {
                    "type": row.get("tol_type", "deviation"),
                    "upper": row.get("tol_upper"),
                    "lower": row.get("tol_lower"),
                }
            if not isinstance(tolerance, dict):
                raise ValueError(f"{field}[{index}] dimension requires tolerance object")
            for bound in ("upper", "lower"):
                candidate = tolerance.get(bound)
                if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
                    raise ValueError(
                        f"{field}[{index}].tolerance.{bound} must be numeric"
                    )
            row["tolerance"] = {
                "type": str(tolerance.get("type") or "deviation"),
                "upper": tolerance["upper"],
                "lower": tolerance["lower"],
            }
        derived = row.get("derived_from")
        if derived is not None and not isinstance(derived, list):
            raise ValueError(f"{field}[{index}].derived_from must be a list")
        row["derived_from"] = list(derived or [])
    return rows


def _normalize_plan(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"plan must be a JSON object: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("plan must be an object")
    plan = dict(raw)
    normalized: dict[str, Any] = {
        "schema": "aicad.mfg-plan/1",
        "id": str(plan.get("id") or plan.get("plan_id") or "main").strip(),
        "revision": _required_text(plan.get("revision"), "revision"),
        "source_doc": _normalize_source(plan),
        "process": _normalize_process(plan),
        "material": _normalize_material(plan),
        "stock": _normalize_resource(plan.get("stock"), "stock"),
        "machine": _normalize_resource(plan.get("machine"), "machine"),
    }
    if not normalized["id"]:
        normalized["id"] = "main"
    quantity = plan.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("mfg_plan quantity must be a positive integer")
    normalized["quantity"] = quantity
    normalized["setups"] = _normalize_ordered(plan.get("setups"), "setups")
    normalized["operations"] = _normalize_ordered(plan.get("operations"), "operations")
    setup_ids = {row["id"] for row in normalized["setups"]}
    for index, operation in enumerate(normalized["operations"]):
        setup_id = _required_text(
            operation.get("setup_id"), f"operations[{index}].setup_id"
        )
        if setup_id not in setup_ids:
            raise ValueError(
                f"operations[{index}].setup_id {setup_id!r} does not name a setup"
            )
        operation["setup_id"] = setup_id
        operation["parameters"] = _objects(
            operation.get("parameters"), f"operations[{index}].parameters"
        )
        operation["inspection"] = _objects(
            operation.get("inspection"), f"operations[{index}].inspection"
        )
        if operation.get("machined", normalized["process"]["id"] in _MACHINING_PROCESSES):
            operation["machined"] = True
            for required in ("machine", "tool"):
                _required_text(operation.get(required), f"operations[{index}].{required}")
            if not operation["parameters"] or not operation["inspection"]:
                raise ValueError(
                    f"machined operations[{index}] requires parameters and inspection rows"
                )
    normalized["critical_dimensions"] = _normalize_audit_rows(
        plan.get("critical_dimensions"), "critical_dimensions"
    )
    normalized["other_callouts"] = _normalize_audit_rows(
        plan.get("other_callouts", plan.get("callouts")), "other_callouts"
    )
    if not normalized["critical_dimensions"] and not normalized["other_callouts"]:
        raise ValueError("mfg_plan requires at least one audit-native critical row or callout")
    audit_ids = [
        row["requirement_id"]
        for row in normalized["critical_dimensions"] + normalized["other_callouts"]
    ]
    if len(audit_ids) != len(set(audit_ids)):
        raise ValueError("audit requirement ids must be unique across all plan rows")
    normalized["inspection_points"] = _objects(
        plan.get("inspection_points", plan.get("inspection")), "inspection_points"
    )
    normalized["hold_points"] = _objects(plan.get("hold_points"), "hold_points")
    normalized["work_steps"] = _objects(
        plan.get("work_steps"), "work_steps", allow_strings=True
    )
    normalized["references"] = _objects(
        plan.get("references"), "references", allow_strings=True
    )
    normalized["assumptions"] = _objects(
        plan.get("assumptions"), "assumptions", allow_strings=True
    )
    normalized["open_questions"] = _objects(
        plan.get("open_questions"), "open_questions", allow_strings=True
    )
    operation_ids = {row["id"] for row in normalized["operations"]}
    requirement_ids = {
        row["requirement_id"]
        for row in normalized["critical_dimensions"] + normalized["other_callouts"]
    }
    for field in ("inspection_points", "work_steps"):
        for index, row in enumerate(normalized[field]):
            if row.get("operation_id") and row["operation_id"] not in operation_ids:
                raise ValueError(
                    f"{field}[{index}].operation_id {row['operation_id']!r} "
                    "does not name an operation"
                )
            if row.get("requirement_id") and row["requirement_id"] not in requirement_ids:
                raise ValueError(
                    f"{field}[{index}].requirement_id {row['requirement_id']!r} "
                    "does not name an audit requirement"
                )
    for index, row in enumerate(normalized["hold_points"]):
        after = row.get("after_operation_id")
        if after and after not in operation_ids:
            raise ValueError(
                f"hold_points[{index}].after_operation_id {after!r} "
                "does not name an operation"
            )
    from services.agent.ledger import validate_ledger_payload  # noqa: PLC0415
    validate_ledger_payload("mfg_plan", normalized)
    return normalized


def _find_plan(ledger, plan_id: str) -> dict | None:
    return next((row for row in ledger.mfg_plans() if row.get("id") == plan_id), None)


def _audit_spec(plan: dict) -> dict:
    rows = [dict(row) for row in plan.get("critical_dimensions", [])]
    rows.extend(dict(row) for row in plan.get("other_callouts", []))
    return {"requirements": rows}


def _display(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _requirement_text(row: dict) -> str:
    value = row.get("value", row.get("ra", row.get("thread", row.get("note", ""))))
    unit = str(row.get("unit") or "").strip()
    text = f"{value} {unit}".strip()
    tolerance = row.get("tolerance")
    if isinstance(tolerance, dict):
        text += (
            f" ({tolerance.get('type', 'deviation')}: "
            f"upper={tolerance.get('upper')}, lower={tolerance.get('lower')})"
        )
    return text


def _work_instruction_spec(plan: dict) -> dict:
    result_id = f"mfg_plan:{plan['id']}:{plan['revision']}"
    payload: dict[str, Any] = {
        "material": f"{plan['material']['key']} {plan['material']['condition']}",
        "stock_prep": _display(plan["stock"]),
        "plan_revision": plan["revision"],
    }

    def ref(key: str, unit: str | None = None) -> dict:
        value: dict[str, Any] = {
            "from": {"tool": "mfg_plan_get", "key": key, "result": result_id}
        }
        if unit:
            value["unit"] = unit
        return value

    setup_by_id = {row["id"]: row for row in plan["setups"]}
    requirements = {
        row["requirement_id"]: row
        for row in plan.get("critical_dimensions", []) + plan.get("other_callouts", [])
    }
    outline: list[dict] = [{
        "type": "part",
        "part_id": plan["source_doc"]["doc_id"],
        "revision": plan["revision"],
        "drawing": plan["source_doc"].get("drawing_id", ""),
        "slots": {"material": ref("material"), "stock_prep": ref("stock_prep")},
    }]
    plan_inspection = plan.get("inspection_points", [])
    for op_index, operation in enumerate(plan["operations"]):
        setup = setup_by_id[operation["setup_id"]]
        slots: dict[str, dict] = {}
        parameters: list[dict] = []
        for param_index, parameter in enumerate(operation.get("parameters", [])):
            name = str(parameter.get("name") or parameter.get("id") or f"parameter_{param_index + 1}")
            key = f"op_{op_index + 1}_{name}"
            payload[key] = parameter.get("value", parameter.get("text"))
            slots[name] = ref(key, str(parameter.get("unit") or "") or None)
            parameters.append({"label": parameter.get("label") or name, "slot": name})
            if parameter.get("provenance"):
                provenance_name = f"{name}_provenance"
                provenance_key = f"{key}_provenance"
                payload[provenance_key] = parameter["provenance"]
                slots[provenance_name] = ref(provenance_key)
                parameters.append({"label": f"{name} provenance", "slot": provenance_name})
        inspections = list(operation.get("inspection", []))
        inspections.extend(
            row for row in plan_inspection if row.get("operation_id") == operation["id"]
        )
        projected_inspection: list[dict] = []
        seen_inspection: set[tuple[str, str]] = set()
        for inspect_index, inspection in enumerate(inspections):
            req_id = inspection.get("requirement_id")
            req = requirements.get(req_id, {})
            feature = str(inspection.get("feature") or req.get("name") or req_id or "").strip()
            gauge = str(inspection.get("gauge") or inspection.get("method") or "").strip()
            if not feature or not gauge:
                raise ValueError(
                    f"operation {operation['id']!r} inspection requires feature and gauge"
                )
            identity = (feature, gauge)
            if identity in seen_inspection:
                continue
            seen_inspection.add(identity)
            limit_key = f"op_{op_index + 1}_inspection_{inspect_index + 1}_limits"
            if req:
                payload[limit_key] = _requirement_text(req)
            elif inspection.get("limits") is not None:
                payload[limit_key] = _display(inspection["limits"])
            else:
                raise ValueError(
                    f"operation {operation['id']!r} inspection requires requirement_id or limits"
                )
            slots[f"inspection_{inspect_index + 1}_limits"] = ref(limit_key)
            projected_inspection.append({
                "feature": feature,
                "gauge": gauge,
                "limits_slot": f"inspection_{inspect_index + 1}_limits",
                "frequency": inspection.get("frequency") or inspection.get("record") or "",
                "requirement_id": req_id,
                "provenance": inspection.get("provenance") or req.get("provenance"),
            })
        outline.append({
            "type": "operation",
            "op_no": operation["order"],
            "heading": operation.get("name") or operation["id"],
            "machined": bool(operation.get("machined")),
            "setup": {
                "datum": setup.get("datum", ""),
                "wcs": setup.get("wcs", ""),
                "fixturing": setup.get("fixturing") or setup.get("fixture") or "",
            },
            "machine": operation.get("machine") or plan["machine"].get("id", ""),
            "tool": operation.get("tool", ""),
            "body": [
                row.get("text", "") for row in plan.get("work_steps", [])
                if row.get("operation_id") == operation["id"] and row.get("text")
            ],
            "parameters": parameters,
            "inspection": projected_inspection,
            "slots": slots,
            "geometry_spec_ids": list(operation["geometry_spec_ids"]),
            "provenance": operation.get("provenance", ""),
        })
    outline.append({
        "type": "finish", "heading": "Deburr, clean, and finish",
        "body": ["Deburr and clean in accordance with the released drawing and plan callouts."],
    })
    for hold in plan.get("hold_points", []):
        outline.append({
            "type": "hold_point", "heading": hold.get("name") or hold.get("id") or "Hold point",
            "body": [hold.get("text") or "Release requires the named authority."],
            "release_authority": hold.get("release_authority", ""),
            "record": hold.get("record", ""),
            "after_operation_id": hold.get("after_operation_id"),
        })
    if not plan.get("hold_points"):
        outline.append({"type": "hold_point", "body": ["Quality release required."]})
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    outline.extend([
        {"type": "safety", "ppe": safety.get("ppe") or ["Use shop-approved PPE"],
         "body": safety.get("body") or ["Follow machine-specific safety procedures."]},
        {"type": "approval", "prepared_by": "Manufacturing Engineering",
         "approved_by": "Quality", "signoff": "Name / signature / date"},
        {"type": "revision", "revision": plan["revision"],
         "description": "Manufacturing plan projection"},
    ])
    return {
        "kind": "work_instruction",
        "template": "work_instruction",
        "title": f"Manufacturing Work Instruction — {plan['source_doc']['doc_id']}",
        "outline": outline,
        "attached_results": {
            result_id: {"tool": "mfg_plan_get", "ts": "", "payload": payload}
        },
        "plan_id": plan["id"],
        "plan_revision": plan["revision"],
    }


def _structural_errors(plan: dict, doc_id: str | None = None) -> list[str]:
    errors: list[str] = []
    for field in (
        "id", "revision", "source_doc", "process", "material", "quantity",
        "setups", "operations",
    ):
        if plan.get(field) in (None, "", [], {}):
            errors.append(f"missing {field}")
    setup_rows = [row for row in plan.get("setups", []) if isinstance(row, dict)]
    operation_rows = [row for row in plan.get("operations", []) if isinstance(row, dict)]
    setup_ids = {row.get("id") for row in setup_rows}
    operation_ids = {row.get("id") for row in operation_rows}
    if len(setup_ids) != len(setup_rows):
        errors.append("setup ids must be present and unique")
    if len(operation_ids) != len(operation_rows):
        errors.append("operation ids must be present and unique")
    for section, rows in (("setup", setup_rows), ("operation", operation_rows)):
        orders = [row.get("order") for row in rows]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in orders):
            errors.append(f"{section} order values must be numeric")
        elif len(set(orders)) != len(orders):
            errors.append(f"{section} order values must be unique")
        for row in rows:
            if not _geometry_refs(row):
                errors.append(f"{section} {row.get('id')!r} has no geometry_spec_ids")
    for operation in operation_rows:
        if operation.get("setup_id") not in setup_ids:
            errors.append(
                f"operation {operation.get('id')!r} references missing setup "
                f"{operation.get('setup_id')!r}"
            )
    for field in ("inspection_points", "work_steps"):
        for row in plan.get(field, []):
            if isinstance(row, dict) and row.get("operation_id") not in (None, *operation_ids):
                errors.append(
                    f"{field} row references missing operation {row.get('operation_id')!r}"
                )
    for row in plan.get("hold_points", []):
        if isinstance(row, dict) and row.get("after_operation_id") not in (None, *operation_ids):
            errors.append(
                f"hold point references missing operation {row.get('after_operation_id')!r}"
            )
    try:
        from services.agent.ledger import validate_ledger_payload  # noqa: PLC0415
        validate_ledger_payload("mfg_plan", plan)
    except ValueError as exc:
        errors.append(str(exc))
    source_doc = plan.get("source_doc") if isinstance(plan.get("source_doc"), dict) else {}
    if not source_doc.get("doc_id"):
        errors.append("missing source_doc.doc_id")
    if doc_id and source_doc.get("doc_id") != doc_id:
        errors.append(
            f"plan source doc {source_doc.get('doc_id')!r} != requested {doc_id!r}"
        )
    return errors


def _geometry_ref_check(ledger, plan: dict) -> dict:
    available = {row.get("id") for row in ledger.geometry_specs()}
    referenced = {
        ref for section in ("setups", "operations") for row in plan.get(section, [])
        for ref in _geometry_refs(row)
    }
    missing = sorted(referenced - available)
    return {
        "check": "geometry_spec_refs",
        "status": "fail" if missing else "pass",
        "detail": "missing geometry specs: " + ", ".join(missing) if missing else "all geometry references resolve",
        "referenced": sorted(referenced),
        "missing": missing,
    }


def _dfm_checks(ledger, plan: dict) -> list[dict]:
    from services.agent import geometry_helpers  # noqa: PLC0415
    specs = {row.get("id"): row for row in ledger.geometry_specs()}
    checks: list[dict] = []
    for operation in plan.get("operations", []):
        kinds: list[str] = []
        if operation.get("geometry_kind"):
            kinds.append(str(operation["geometry_kind"]))
        for ref in _geometry_refs(operation):
            for shape in specs.get(ref, {}).get("shapes", []):
                if isinstance(shape, dict) and shape.get("kind"):
                    kinds.append(str(shape["kind"]))
        kind = next((candidate for candidate in kinds if geometry_helpers.binding_for(candidate)), None)
        name = f"dfm:{operation.get('id', '?')}"
        if not kind:
            checks.append({
                "check": name, "status": "not_evaluated",
                "detail": "no geometry helper binding is available for this operation",
            })
            continue
        try:
            result = geometry_helpers.resolve_helpers(
                kind,
                material=plan.get("material", {}).get("key", ""),
                process=plan.get("process", {}).get("id", ""),
                **(operation.get("helper_inputs") or {}),
            )
        except Exception as exc:  # engine-less and optional calculators are supported
            checks.append({
                "check": name, "status": "not_evaluated",
                "detail": f"DfM helper unavailable: {exc}", "geometry_kind": kind,
            })
            continue
        if result is None or not result.ok:
            checks.append({
                "check": name, "status": "not_evaluated",
                "detail": (result.note if result is not None else "no DfM helper result"),
                "geometry_kind": kind,
            })
            continue
        inputs = operation.get("helper_inputs") or {}
        failures: list[str] = []
        limits = result.numbers
        comparisons = (
            ("wall_mm", "min_wall_mm"),
            ("feature_mm", "min_feature_mm"),
            ("draft_deg", "process_min_draft_deg"),
        )
        for actual_key, limit_key in comparisons:
            actual = inputs.get(actual_key)
            limit = limits.get(limit_key)
            if isinstance(actual, (int, float)) and isinstance(limit, (int, float)) and actual < limit:
                failures.append(f"{actual_key}={actual} below {limit_key}={limit}")
        checks.append({
            "check": name,
            "status": "fail" if failures else "pass",
            "detail": "; ".join(failures) if failures else "DfM facts resolved",
            "geometry_kind": kind,
            "facts": result.as_dict(),
        })
    return checks


def _check_plan(ledger, plan: dict, doc_id: str | None) -> dict:
    structural = _structural_errors(plan, doc_id)
    checks: list[dict] = [{
        "check": "structure",
        "status": "fail" if structural else "pass",
        "detail": "; ".join(structural) if structural else "structural invariants hold",
    }]
    profile = plan.get("process", {}).get("profile_snapshot")
    checks.append({
        "check": "process_profile",
        "status": "pass" if isinstance(profile, dict) and profile else "not_evaluated",
        "detail": (
            "process profile snapshot is present"
            if isinstance(profile, dict) and profile
            else "process profile is unavailable; process capability was not evaluated"
        ),
    })
    checks.append(_geometry_ref_check(ledger, plan))
    checks.extend(_dfm_checks(ledger, plan))
    blocking_questions = [
        row for row in plan.get("open_questions", [])
        if row.get("blocking", True) and row.get("status", "open") != "answered"
    ]
    checks.append({
        "check": "open_questions",
        "status": "fail" if blocking_questions else "pass",
        "detail": (
            f"{len(blocking_questions)} blocking question(s) remain"
            if blocking_questions else "no blocking questions"
        ),
        "question_ids": [row.get("id") for row in blocking_questions],
    })
    statuses = {row["status"] for row in checks}
    overall = "fail" if "fail" in statuses else (
        "not_evaluated" if "not_evaluated" in statuses else "pass"
    )
    return {
        "ok": overall != "fail",
        "run_id": ledger.run_id,
        "plan_id": plan["id"],
        "revision": plan.get("revision"),
        "status": overall,
        "blocking": bool(blocking_questions) or overall == "fail",
        "checks": checks,
    }


def _markdown(plan: dict) -> str:
    lines = [
        f"# Manufacturing Plan — {plan['id']}", "",
        f"Revision: {plan['revision']}",
        f"Source document: {plan['source_doc']['doc_id']}",
        f"Process: {plan['process']['id']}",
        f"Material: {plan['material']['key']} ({plan['material']['condition']})",
        f"Quantity: {plan['quantity']}", "", "## Operations", "",
    ]
    for operation in plan["operations"]:
        lines.append(
            f"- {operation['order']}: {operation.get('name') or operation['id']} "
            f"(setup {operation['setup_id']}; geometry {', '.join(operation['geometry_spec_ids'])})"
        )
    lines.extend(["", "## Critical requirements", ""])
    for row in plan["critical_dimensions"] + plan["other_callouts"]:
        lines.append(
            f"- {row['requirement_id']} [{row['type']}]: {_requirement_text(row)} "
            f"— {row.get('provenance', 'non-numeric callout')}"
        )
    lines.extend(["", "## Open questions", ""])
    questions = plan.get("open_questions", [])
    lines.extend(f"- {row.get('text') or row.get('id')}" for row in questions)
    if not questions:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def register_mfg_plan_tools(mcp) -> None:
    """Register the four run-ledger manufacturing planning tools."""

    @mcp.tool()
    def mfg_plan_upsert(
        run_id: str,
        plan: dict,
        actor: str = "manufacturing_planner",
    ) -> dict:
        """Normalize, validate, and upsert one typed manufacturing plan."""
        try:
            from services.agent.ledger import Ledger  # noqa: PLC0415
            normalized = _normalize_plan(plan)
            ledger = Ledger.load(run_id)
            event = ledger.upsert_mfg_plan(actor, normalized)
            return {
                "ok": True, "run_id": run_id, "plan_id": normalized["id"],
                "revision": normalized["revision"], "seq": event["seq"],
                "plan": normalized,
            }
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "run_id": run_id}
        except Exception as exc:  # pragma: no cover - persistence/runtime boundary
            return {"ok": False, "error": f"manufacturing plan write failed: {exc}",
                    "run_id": run_id}

    @mcp.tool()
    def mfg_plan_get(
        run_id: str,
        plan_id: str = "main",
        view: str = "full",
    ) -> dict:
        """Read a plan or its deterministic drawing/work-instruction projection."""
        try:
            from services.agent.ledger import Ledger  # noqa: PLC0415
            selected = str(view or "full").strip().lower()
            if selected not in _VALID_VIEWS:
                return {"ok": False, "error": f"unknown view {view!r}",
                        "valid_views": sorted(_VALID_VIEWS)}
            plan = _find_plan(Ledger.load(run_id), plan_id)
            if plan is None:
                return {"ok": False, "error": f"manufacturing plan {plan_id!r} not found",
                        "run_id": run_id, "plan_id": plan_id}
            if selected == "audit_spec":
                return _audit_spec(plan)
            if selected == "work_instructions":
                return _work_instruction_spec(plan)
            return dict(plan)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "run_id": run_id,
                    "plan_id": plan_id}

    @mcp.tool()
    def mfg_plan_check(
        run_id: str,
        plan_id: str = "main",
        doc_id: str | None = None,
    ) -> dict:
        """Check structure, references, DfM facts, and blocking unknowns."""
        from services.agent.ledger import Ledger  # noqa: PLC0415
        ledger = Ledger.load(run_id)
        plan = _find_plan(ledger, plan_id)
        if plan is None:
            return {"ok": False, "status": "fail", "blocking": True,
                    "error": f"manufacturing plan {plan_id!r} not found",
                    "run_id": run_id, "plan_id": plan_id,
                    "checks": [{"check": "plan_exists", "status": "fail",
                                "detail": "plan is absent"}]}
        return _check_plan(ledger, plan, doc_id)

    @mcp.tool()
    def mfg_plan_render(
        run_id: str,
        plan_id: str,
        format: str = "json",  # noqa: A002 - fixed MCP contract
    ) -> dict:
        """Render JSON or Markdown and record a manufacturing_plan artifact."""
        from services.agent.ledger import Ledger  # noqa: PLC0415
        ledger = Ledger.load(run_id)
        plan = _find_plan(ledger, plan_id)
        if plan is None:
            return {"ok": False, "error": f"manufacturing plan {plan_id!r} not found",
                    "run_id": run_id, "plan_id": plan_id}
        fmt = str(format or "json").strip().lower()
        if fmt not in {"json", "md"}:
            return {"ok": False, "error": f"unknown format {format!r}",
                    "valid_formats": ["json", "md"]}
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan_id).strip("._") or "main"
        path = Path(ledger.path).parent / f"manufacturing_plan_{safe_id}.{fmt}"
        content = (
            json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if fmt == "json" else _markdown(plan)
        )
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"manufacturing plan render failed: {exc}",
                    "run_id": run_id, "plan_id": plan_id}
        artifact_id = f"mfg_plan:{plan_id}:{plan.get('revision')}:{fmt}"
        event = ledger.add_artifact(
            "manufacturing_planner", "manufacturing_plan", artifact_id,
            str(path), "manufacturing_plan", plan_id=plan_id,
            revision=plan.get("revision"), format=fmt,
        )
        return {
            "ok": True, "run_id": run_id, "plan_id": plan_id,
            "revision": plan.get("revision"), "format": fmt,
            "path": str(path), "artifact_id": artifact_id, "seq": event["seq"],
        }

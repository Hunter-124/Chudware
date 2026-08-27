"""Structured machining work instructions rendered as DOCX or PDF.

Engineering values are carried in provenance-resolved ``slots``.  The template never derives
feeds, speeds, stock sizes, inspection limits, or process values: it prints only values supplied
by attached tool results.  A machined operation is not renderable without a complete setup and
an in-process inspection plan.
"""
from __future__ import annotations

from pathlib import Path

from ..errors import DeliverableError
from . import Template, register_template
from ._doc import Doc, render


def _label(name: str) -> str:
    return str(name).replace("_", " ").strip().title()


def _slot_text(block: dict, name: str) -> str:
    value = block.get("slots", {}).get(name)
    if not isinstance(value, dict) or "text" not in value:
        raise DeliverableError(
            "invalid_spec", f"operation references missing provenance slot {name!r}")
    return str(value["text"])


def _validate_operation(block: dict) -> None:
    if block.get("machined") is not True:
        return
    setup = block.get("setup")
    if not isinstance(setup, dict) or not setup:
        raise DeliverableError(
            "invalid_spec", "a machined operation requires a structured 'setup' object")
    if not (setup.get("datum") or setup.get("wcs")):
        raise DeliverableError(
            "invalid_spec", "a machined operation setup requires 'datum' and/or 'wcs'")
    if not (setup.get("fixturing") or setup.get("fixture") or setup.get("fixture_notes")):
        raise DeliverableError(
            "invalid_spec", "a machined operation setup requires fixturing instructions")
    if not block.get("machine") or not block.get("tool"):
        raise DeliverableError(
            "invalid_spec", "a machined operation requires both 'machine' and 'tool'")
    parameters = block.get("parameters")
    if not isinstance(parameters, list) or not parameters:
        raise DeliverableError(
            "invalid_spec",
            "a machined operation requires provenance-backed process 'parameters'",
        )
    inspection = block.get("inspection")
    if not isinstance(inspection, list) or not inspection:
        raise DeliverableError(
            "invalid_spec", "a machined operation requires in-process 'inspection' rows")
    for index, row in enumerate(inspection):
        if not isinstance(row, dict):
            raise DeliverableError(
                "invalid_spec", f"inspection[{index}] must be an object")
        if not row.get("feature") or not row.get("gauge"):
            raise DeliverableError(
                "invalid_spec", f"inspection[{index}] requires feature and gauge")
        limit_slot = row.get("limits_slot")
        if not limit_slot and isinstance(row.get("limits"), str):
            candidate = row["limits"]
            if candidate in block.get("slots", {}):
                limit_slot = candidate
        if not limit_slot:
            raise DeliverableError(
                "provenance_violation",
                f"inspection[{index}] limits must name a provenance-backed limits_slot",
            )
        _slot_text(block, str(limit_slot))


def _validate_document(blocks: list[dict]) -> None:
    operations = [block for block in blocks if block.get("type") == "operation"]
    for block in operations:
        if block.get("op_no") in (None, ""):
            raise DeliverableError("invalid_spec", "every operation requires an 'op_no'")
        _validate_operation(block)
    part_blocks = [
        block for block in blocks if block.get("type") in {"part", "document_control"}
    ]
    if not part_blocks:
        raise DeliverableError("invalid_spec", "work_instruction requires a part-control block")
    part = part_blocks[0]
    if not part.get("part_id") or not part.get("revision"):
        raise DeliverableError(
            "invalid_spec", "part control requires part_id and revision")
    for slot_name in ("material", "stock_prep"):
        if slot_name not in part.get("slots", {}):
            raise DeliverableError(
                "invalid_spec",
                f"part control requires provenance-backed {slot_name!r}",
            )
    if not operations:
        raise DeliverableError("invalid_spec", "work_instruction requires ordered operations")
    operation_numbers = [str(block["op_no"]) for block in operations]
    if len(operation_numbers) != len(set(operation_numbers)):
        raise DeliverableError("invalid_spec", "operation op_no values must be unique")
    required_groups = (
        ({"finish", "deburr_finish"}, "deburr/finish"),
        ({"hold_point"}, "hold point"),
        ({"safety", "ppe_safety"}, "PPE/safety"),
        ({"approval", "signoff"}, "approval/signoff"),
        ({"revision"}, "revision row"),
    )
    present = {block.get("type") for block in blocks}
    for alternatives, label in required_groups:
        if alternatives.isdisjoint(present):
            raise DeliverableError(
                "invalid_spec", f"work_instruction requires a {label} block")


def _operation_parameters(block: dict) -> list[list[str]]:
    rows = [["Parameter", "Approved value"]]
    used: set[str] = set()
    parameters = block.get("parameters") or []
    if not isinstance(parameters, list):
        raise DeliverableError("invalid_spec", "operation 'parameters' must be a list")
    for index, item in enumerate(parameters):
        if not isinstance(item, dict) or not item.get("slot"):
            raise DeliverableError(
                "invalid_spec", f"parameters[{index}] requires a provenance slot name")
        name = str(item["slot"])
        rows.append([str(item.get("label") or _label(name)), _slot_text(block, name)])
        used.add(name)
    inspection_slots = {
        str(row.get("limits_slot") or row.get("limits"))
        for row in (block.get("inspection") or []) if isinstance(row, dict)
    }
    for name, value in block.get("slots", {}).items():
        if name not in used and name not in inspection_slots:
            rows.append([_label(name), str(value["text"])])
    return rows


def _render_operation(doc: Doc, block: dict, fallback_no: int) -> None:
    number = block.get("op_no", fallback_no)
    heading = str(block.get("heading") or block.get("name") or "Operation")
    doc.heading(f"Operation {number} — {heading}", level=2)
    setup = block.get("setup") or {}
    setup_rows = [
        ("Datum", setup.get("datum", "")),
        ("WCS", setup.get("wcs", "")),
        ("Fixturing", setup.get("fixturing") or setup.get("fixture")
         or setup.get("fixture_notes", "")),
        ("Machine", block.get("machine", "")),
        ("Tool", block.get("tool", "")),
    ]
    doc.kv([(k, v) for k, v in setup_rows if v])
    for line in block.get("body", []):
        doc.para(line)
    parameter_rows = _operation_parameters(block)
    if len(parameter_rows) > 1:
        doc.heading("Process parameters", level=3)
        doc.table(parameter_rows)
    inspection = block.get("inspection") or []
    if inspection:
        rows = [["Feature", "Gauge / method", "Limits", "Frequency / record"]]
        for row in inspection:
            limit_slot = str(row.get("limits_slot") or row.get("limits"))
            rows.append([
                row.get("feature", ""), row.get("gauge", ""),
                _slot_text(block, limit_slot),
                row.get("frequency") or row.get("record") or "",
            ])
        doc.heading("In-process inspection", level=3)
        doc.table(rows)
    doc.spacer()


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    _validate_document(resolved["blocks"])
    doc = Doc(resolved.get("title") or "Work Instruction")
    operation_no = 0
    for block in resolved["blocks"]:
        block_type = block.get("type")
        if block_type in {"part", "document_control"}:
            doc.heading("Document and part control", level=1)
            fields = [
                ("Part ID", block.get("part_id", "")),
                ("Revision", block.get("revision", "")),
                ("Drawing", block.get("drawing", "")),
            ]
            fields.extend((_label(name), value["text"])
                          for name, value in block.get("slots", {}).items())
            doc.kv([(key, value) for key, value in fields if value])
            for line in block.get("body", []):
                doc.para(line)
        elif block_type == "operation":
            operation_no += 1
            _render_operation(doc, block, operation_no)
        elif block_type in {"finish", "deburr_finish"}:
            doc.heading(block.get("heading") or "Deburr / clean / finish", level=1)
            for line in block.get("body", []):
                doc.para(line)
            doc.kv([(_label(name), value["text"])
                    for name, value in block.get("slots", {}).items()])
        elif block_type == "hold_point":
            doc.heading(block.get("heading") or "Hold point", level=1)
            for line in block.get("body", []):
                doc.para(line)
            doc.kv([("Release authority", block.get("release_authority", "")),
                    ("Record", block.get("record", ""))])
        elif block_type in {"safety", "ppe_safety"}:
            doc.heading(block.get("heading") or "PPE and safety", level=1)
            doc.bullets(block.get("ppe") or [])
            for line in block.get("body", []):
                doc.para(line)
        elif block_type in {"approval", "signoff"}:
            doc.heading(block.get("heading") or "Approval and signoff", level=1)
            doc.table([
                ["Role", "Name / signature", "Date"],
                ["Prepared by", block.get("prepared_by", ""), ""],
                ["Approved by", block.get("approved_by", ""), ""],
                ["Shop completion", block.get("signoff", ""), ""],
            ])
        elif block_type == "revision":
            doc.heading(block.get("heading") or "Revision record", level=1)
            doc.table([
                ["Revision", "Date", "Description", "Approved by"],
                [block.get("revision", ""), block.get("date", ""),
                 block.get("description", ""), block.get("approved_by", "")],
            ])
        else:
            doc.heading(block.get("heading") or _label(block_type), level=1)
            for line in block.get("body", []):
                doc.para(line)
    render(doc, fmt, out_path)


register_template(Template(
    "work_instruction",
    "work_instruction",
    _build,
    lb_all={"part", "document_control", "operation", "finish", "deburr_finish"},
))

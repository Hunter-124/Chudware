"""Controlled weldment traveler with cut, joint, WPS, inspection, and signoff records."""
from __future__ import annotations

from pathlib import Path

from ..errors import DeliverableError
from . import Template, register_template
from ._doc import Doc, render


def _label(name: str) -> str:
    return str(name).replace("_", " ").strip().title()


def _cell_text(cell: dict) -> str:
    if not isinstance(cell, dict):
        return str(cell)
    if "text" in cell:
        return str(cell["text"])
    if "formula" in cell:
        return str(cell["formula"])
    return ""


def _rows(block: dict) -> list[list[str]]:
    return [[_cell_text(cell) for cell in row] for row in block.get("rows", [])]


def _require_rows(block: dict, minimum_columns: int, name: str) -> list[list[str]]:
    rows = _rows(block)
    if len(rows) < 2:
        raise DeliverableError(
            "invalid_spec", f"{name} requires a header and at least one data row")
    if any(len(row) < minimum_columns for row in rows):
        raise DeliverableError(
            "invalid_spec", f"{name} rows require at least {minimum_columns} columns")
    return rows


def _validate_document(blocks: list[dict]) -> None:
    present = {block.get("type") for block in blocks}
    control = next(
        (block for block in blocks
         if block.get("type") in {"document_control", "weldment"}),
        None,
    )
    if control is None:
        raise DeliverableError(
            "invalid_spec", "weldment_traveler requires a document-control block")
    if not (control.get("part_id") or control.get("weldment_id")) or not control.get("revision"):
        raise DeliverableError(
            "invalid_spec", "document control requires part/weldment id and revision")
    if not (control.get("material") or control.get("slots", {}).get("material")):
        raise DeliverableError("invalid_spec", "document control requires material")
    required_groups = (
        ({"cut_list"}, "cut list"),
        ({"joint_list"}, "joint list"),
        ({"wps"}, "WPS reference"),
        ({"fit_up"}, "fit-up checks"),
        ({"distortion_control"}, "distortion-control notes"),
        ({"inspection"}, "visual/NDT inspection"),
        ({"signoff", "approval"}, "signoff"),
    )
    for alternatives, label in required_groups:
        if alternatives.isdisjoint(present):
            raise DeliverableError(
                "invalid_spec", f"weldment_traveler requires {label}")
    cut_list = next(block for block in blocks if block.get("type") == "cut_list")
    joint_list = next(block for block in blocks if block.get("type") == "joint_list")
    _require_rows(cut_list, 4, "cut_list")
    _require_rows(joint_list, 5, "joint_list")
    wps = next(block for block in blocks if block.get("type") == "wps")
    if not wps.get("slots"):
        raise DeliverableError(
            "invalid_spec", "wps requires provenance-backed reference fields in slots")
    fit_up = next(block for block in blocks if block.get("type") == "fit_up")
    if not (fit_up.get("checks") or fit_up.get("body")):
        raise DeliverableError("invalid_spec", "fit_up requires at least one check")
    distortion = next(
        block for block in blocks if block.get("type") == "distortion_control")
    if not (distortion.get("notes") or distortion.get("body")):
        raise DeliverableError(
            "invalid_spec", "distortion_control requires at least one note")
    inspection = next(block for block in blocks if block.get("type") == "inspection")
    if not (inspection.get("rows") or inspection.get("checks") or inspection.get("body")):
        raise DeliverableError(
            "invalid_spec", "inspection requires visual/NDT rows or checks")


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    _validate_document(resolved["blocks"])
    doc = Doc(resolved.get("title") or "Weldment Traveler")
    for block in resolved["blocks"]:
        block_type = block.get("type")
        if block_type in {"document_control", "weldment"}:
            doc.heading("Document and weldment control", level=1)
            fields = [
                ("Part / weldment ID", block.get("part_id") or block.get("weldment_id") or ""),
                ("Revision", block.get("revision", "")),
                ("Drawing", block.get("drawing", "")),
                ("Material", block.get("material", "")),
            ]
            fields.extend((_label(name), value["text"])
                          for name, value in block.get("slots", {}).items())
            doc.kv([(key, value) for key, value in fields if value])
        elif block_type == "cut_list":
            doc.heading(block.get("heading") or "Cut list", level=1)
            doc.table(_require_rows(block, 4, "cut_list"))
        elif block_type == "joint_list":
            doc.heading(block.get("heading") or "Joint list and weld sequence", level=1)
            doc.table(_require_rows(block, 5, "joint_list"))
        elif block_type == "wps":
            doc.heading(block.get("heading") or "Welding procedure specification", level=1)
            doc.kv([(_label(name), value["text"])
                    for name, value in block.get("slots", {}).items()])
            for line in block.get("body", []):
                doc.para(line)
        elif block_type == "fit_up":
            doc.heading(block.get("heading") or "Fit-up checks", level=1)
            doc.bullets(block.get("checks") or block.get("body") or [])
        elif block_type == "distortion_control":
            doc.heading(block.get("heading") or "Distortion control", level=1)
            doc.bullets(block.get("notes") or block.get("body") or [])
        elif block_type == "inspection":
            doc.heading(block.get("heading") or "Visual / NDT inspection", level=1)
            rows = _rows(block)
            if rows:
                doc.table(rows)
            else:
                doc.bullets(block.get("checks") or block.get("body") or [])
            doc.kv([(_label(name), value["text"])
                    for name, value in block.get("slots", {}).items()])
        elif block_type in {"signoff", "approval"}:
            doc.heading(block.get("heading") or "Traveler signoff", level=1)
            doc.table([
                ["Role", "Name / ID / signature", "Date"],
                ["Welder", block.get("welder", ""), ""],
                ["Fit-up inspection", block.get("fit_up_inspector", ""), ""],
                ["Weld inspection / NDT", block.get("inspector", ""), ""],
                ["Quality release", block.get("quality_release", ""), ""],
            ])
        elif block_type == "revision":
            doc.heading(block.get("heading") or "Revision record", level=1)
            doc.table([
                ["Revision", "Date", "Description", "Approved by"],
                [block.get("revision", ""), block.get("date", ""),
                 block.get("description", ""), block.get("approved_by", "")],
            ])
        elif block_type in {"safety", "ppe_safety"}:
            doc.heading(block.get("heading") or "PPE and safety", level=1)
            doc.bullets((block.get("ppe") or []) + (block.get("body") or []))
        else:
            doc.heading(block.get("heading") or _label(block_type), level=1)
            for line in block.get("body", []):
                doc.para(line)
    render(doc, fmt, out_path)


register_template(Template(
    "weldment_traveler",
    "weldment_traveler",
    _build,
    lb_all={"document_control", "weldment", "wps", "inspection"},
    lb_cols={
        "cut_list": {2, 3},
        "joint_list": {2, 4},
        "inspection": {2},
    },
))

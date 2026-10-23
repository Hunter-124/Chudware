"""``assembly_manual`` — assembly instructions + user manual (docx/pdf), an engineering template.

The build-and-operate document: numbered assembly steps with exploded / step renders and
torque-and-fastener callouts, plus user-manual sections. Fastener torque, counts and gaps are
load-bearing — pulled from the assembly / fastening data (contract §4), never typed. Block types:

- ``title``   — cover heading + body prose.
- ``section`` — a user-manual section (free-prose heading + body; optional bullet list).
- ``step``    — one assembly step. Free prose: ``step_no`` (label), ``image`` (a step/exploded
                render path — embedded if the file exists, else a caption), ``body`` instruction
                text. Load-bearing slots: ``torque``, ``fastener_count``, ``gap``, ``preload``.

Ships as a ``make_report`` template (docx/pdf) rather than the reserved ``make_manual`` tool:
manuals need no new tool seam, so ``make_manual`` stays reserved
in ``deliverables/1`` and no contract change is required. Rendered from the shared :mod:`._doc`
model.
"""
from __future__ import annotations

from pathlib import Path

from . import Template, register_template, resolve_image
from ._doc import Doc, render

_CALLOUTS = {"torque", "fastener_count", "gap", "preload"}


def _callout_kv(block: dict) -> list[tuple]:
    return [(n.replace("_", " ").capitalize(), block["slots"][n]["text"])
            for n in ("torque", "fastener_count", "gap", "preload")
            if n in block.get("slots", {})]


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    doc = Doc(resolved.get("title", "Assembly & User Manual"))
    step_n = 0
    for blk in resolved["blocks"]:
        btype = blk.get("type")
        if btype == "title":
            for line in blk.get("body", []):
                doc.para(line)
        elif btype == "section":
            doc.heading(blk.get("heading", "Section"), level=1)
            for line in blk.get("body", []):
                doc.para(line)
        elif btype == "step":
            step_n += 1
            label = blk.get("step_no")
            head = f"Step {label}" if label is not None else f"Step {step_n}"
            if blk.get("heading"):
                head += f" — {blk['heading']}"
            doc.heading(head, level=2)
            for line in blk.get("body", []):
                doc.para(line)
            img = resolve_image(blk.get("image"))
            if img is not None or blk.get("image"):
                doc.image(img, caption=str(blk.get("image") or "") if img is None else "")
            kv = _callout_kv(blk)
            if kv:
                doc.kv(kv)
            doc.spacer()

    render(doc, fmt, out_path)


register_template(Template(
    "assembly_manual", "report", _build,
    lb_slots={"step": set(_CALLOUTS)}))

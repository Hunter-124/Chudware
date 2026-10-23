"""``verification_report`` — the test / verification report (docx/pdf), an engineering template.

The requirements-to-evidence document: each requirement joined to the measured evidence that
verifies it, the measured value pulled from a tool result (contract §4), never asserted by the
model. Until the requirements schema lands the criterion is a free-prose field; the join
mechanics (measured value → provenance ref) are already enforced. Block types:

- ``title``       — cover heading + body prose.
- ``requirement`` — one requirement row. Free prose: ``req_id``, ``criterion``, ``verdict``
                    (PASS/FAIL) block fields + ``body`` requirement text. Load-bearing slots:
                    ``measured``, ``limit``, ``margin`` (the evidence numbers).
- ``summary``     — optional closing prose / counts (free prose).

Rendered as a per-requirement verification table plus each requirement's evidence. Registered
``kind='report'`` → the existing ``make_report`` tool (docx/pdf), no contract change.
"""
from __future__ import annotations

from pathlib import Path

from . import Template, register_template
from ._doc import Doc, render

_EVIDENCE = {"measured", "limit", "margin"}


def _slot(block: dict, name: str) -> str:
    rv = block.get("slots", {}).get(name)
    return rv["text"] if rv else ""


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    doc = Doc(resolved.get("title", "Verification Report"))

    reqs = [b for b in resolved["blocks"] if b.get("type") == "requirement"]
    if reqs:
        doc.heading("Requirements verification matrix", level=1)
        table = [["Req", "Criterion", "Measured", "Limit", "Verdict"]]
        for b in reqs:
            table.append([
                str(b.get("req_id") or b.get("heading") or ""),
                str(b.get("criterion") or ""),
                _slot(b, "measured"),
                _slot(b, "limit"),
                str(b.get("verdict") or ""),
            ])
        doc.table(table, header=True)
        doc.spacer()

    for blk in resolved["blocks"]:
        btype = blk.get("type")
        if btype == "title":
            for line in blk.get("body", []):
                doc.para(line)
        elif btype == "requirement":
            head = str(blk.get("req_id") or blk.get("heading") or "Requirement")
            doc.heading(head, level=2)
            for line in blk.get("body", []):
                doc.para(line)
            if blk.get("criterion"):
                doc.para(f"Criterion: {blk['criterion']}")
            kv = [(n.capitalize(), _slot(blk, n)) for n in ("measured", "limit", "margin")
                  if _slot(blk, n)]
            doc.kv(kv)
            if blk.get("verdict"):
                doc.para(f"Verdict: {blk['verdict']}")
            doc.spacer()
        elif btype == "summary":
            doc.heading(blk.get("heading", "Summary"), level=1)
            for line in blk.get("body", []):
                doc.para(line)

    render(doc, fmt, out_path)


register_template(Template(
    "verification_report", "report", _build,
    lb_slots={"requirement": set(_EVIDENCE)}))

"""``calc_package`` — the engineering calculation package (docx/pdf), an engineering template.

The signed calc set an engineer files: the equations used, the assumptions behind them, the
material data they rely on **with its citation** (Pillar 8 answer-contract fields: ``source`` +
``fetched_ts``), and the results — every computed number resolved from a tool result (contract
§4), never typed. Block types:

- ``title``       — cover heading + body prose.
- ``assumptions`` — free-prose bullets (the stated assumptions; no load-bearing numbers).
- ``material``    — material property block. Load-bearing slots ``density``, ``youngs_modulus``,
                    ``yield_strength``, ``ultimate_strength``, ``poisson``; the free-prose slots
                    ``source`` and ``fetched_ts`` (or same-named block fields) carry the citation.
- ``equation``    — a computed relation. ``expr`` (block field / body) is the prose formula;
                    **every slot is load-bearing** (inputs and the result are all engine values).
- ``results_table`` — a results grid; the value column (index 1) is load-bearing.

Registered ``kind='report'`` → driven by the existing ``make_report`` tool (docx/pdf), no
contract change. Both formats come from the shared :mod:`._doc` model.
"""
from __future__ import annotations

from pathlib import Path

from . import Template, register_template
from ._doc import Doc, render

_MATERIAL = {"density", "youngs_modulus", "yield_strength", "ultimate_strength", "poisson"}
_CITATION_SLOTS = ("source", "fetched_ts")


def _slot_kv(block: dict, skip=()) -> list[tuple]:
    return [(name.replace("_", " ").capitalize(), rv["text"])
            for name, rv in block.get("slots", {}).items() if name not in skip]


def _citation(block: dict) -> str:
    src = block.get("slots", {}).get("source", {}).get("text") or block.get("source")
    ts = block.get("slots", {}).get("fetched_ts", {}).get("text") or block.get("fetched_ts")
    if src and ts:
        return f"Source: {src} (fetched {ts})"
    if src:
        return f"Source: {src}"
    return ""


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    doc = Doc(resolved.get("title", "Calculation Package"))
    for blk in resolved["blocks"]:
        btype = blk.get("type")
        if btype == "title":
            for line in blk.get("body", []):
                doc.para(line)
            continue

        doc.heading(blk.get("heading") or btype.replace("_", " ").title(), level=1)
        for line in blk.get("body", []):
            doc.para(line)

        if btype == "assumptions":
            doc.bullets(blk.get("body", []) or [])
        elif btype == "material":
            doc.kv(_slot_kv(blk, skip=_CITATION_SLOTS))
            cite = _citation(blk)
            if cite:
                doc.para(cite)
        elif btype == "equation":
            expr = blk.get("expr")
            if expr:
                doc.para(str(expr))
            doc.kv(_slot_kv(blk))
        elif btype == "results_table":
            rows = [[c.get("text", c.get("formula", "")) for c in row]
                    for row in blk.get("rows", [])]
            if rows:
                doc.table(rows, header=True)
        else:  # generic section — show any resolved slots
            doc.kv(_slot_kv(blk))
        doc.spacer()

    render(doc, fmt, out_path)


register_template(Template(
    "calc_package", "report", _build,
    lb_slots={"material": set(_MATERIAL)},
    lb_cols={"results_table": {1}},
    lb_all={"equation"}))

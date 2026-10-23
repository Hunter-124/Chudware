"""``user_manual`` — the auto-generated end-user manual (docx/pdf), a deliverable template.

Ships the manual :mod:`tools.manual_gen` composes from the ribbon inventory, the ``cad_guide``
topic prose, and a contracts summary — ``title`` + ``section`` blocks only, ALL free prose (no
load-bearing slots: a user manual states no computed engineering values, so ``lb_slots={}``).
Block types, mirroring :mod:`.assembly_manual`:

- ``title``   — cover heading + body prose (rendered as the doc title + intro paragraphs).
- ``section`` — one manual section: a heading plus body paragraphs (already flattened by the
                generator — one paragraph per markdown line/heading, no markdown syntax passed
                through since the shared :mod:`._doc` model renders plain text, not markdown).

Rendered from the shared :mod:`._doc` model, same as every other report template (§8 of
contracts/deliverables.md — a new template needs no contract change).
"""
from __future__ import annotations

from pathlib import Path

from . import Template, register_template
from ._doc import Doc, render


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    doc = Doc(resolved.get("title", "Chudware User Manual"))
    for blk in resolved["blocks"]:
        btype = blk.get("type")
        if btype == "title":
            for line in blk.get("body", []):
                doc.para(line)
        elif btype == "section":
            doc.heading(blk.get("heading", "Section"), level=blk.get("level", 1))
            for line in blk.get("body", []):
                doc.para(line)
            doc.spacer()

    render(doc, fmt, out_path)


register_template(Template("user_manual", "report", _build, lb_slots={}))

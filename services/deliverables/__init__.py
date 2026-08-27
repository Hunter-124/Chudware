"""Chudware deliverables — spec-driven office artifacts (contract ``deliverables/1``).

The engineering premise (contract §4): **the model never types a load-bearing number.**
The model authors structure and prose (an ``outline`` of blocks); every load-bearing value
is a *slot* that this module resolves from the tool results the model attached to the spec.
A bare numeral in a load-bearing slot is a validation ERROR, not a value.

Seven tools return the flat MCP envelope (``{ok:true, ...}`` / ``{ok:false, reason, hint}``):

- :func:`make_presentation` — ``.pptx`` deck (python-pptx)
- :func:`make_report` — ``.docx``/``.pdf`` report (python-docx / reportlab)
- :func:`make_workbook` — ``.xlsx`` workbook with live formulas (openpyxl)
- :func:`make_work_instruction` — controlled machining routing (``.docx``/``.pdf``)
- :func:`make_weldment_traveler` — controlled weld traveler (``.docx``/``.pdf``)
- :func:`render_document` — downscaled preview PNGs + a ``resource_link`` (LibreOffice→PDF
  subprocess for office formats; pypdfium2 for PDF; native rasterize for svg/png/csv/md)
- :func:`list_artifacts` — read the artifact registry (§3 records), optional ``run_id`` filter

Open-source-first (contract §7): we wrap permissive in-process engines and never write a
format parser. pandoc/LibreOffice are subprocess-only, discovered on PATH; CI passes without
them installed (any path that needs one degrades to ``{ok:false, reason:"converter_missing"}``).
Never PyMuPDF (AGPL).
"""
from __future__ import annotations

from .api import (
    list_artifacts,
    make_presentation,
    make_report,
    make_weldment_traveler,
    make_work_instruction,
    make_workbook,
    render_document,
)
from .guide import DELIVERABLES_GUIDE
from .mcp_tools import register_deliverables_tools

__all__ = [
    "make_presentation",
    "make_report",
    "make_workbook",
    "make_work_instruction",
    "make_weldment_traveler",
    "render_document",
    "list_artifacts",
    "DELIVERABLES_GUIDE",
    "register_deliverables_tools",
]

"""tool_guidance.office — curated options for the office document tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "office_extract": {
        "prompt_note": (
            "Cheapest first look at any office file — dispatch by extension. "
            "Switch to the per-format office_read_* reader when you need "
            "structure (pptx notes, pdf page_range, single xlsx sheet)."
        ),
    },
    "office_read_csv": {
        "prompt_note": (
            "First row is always returned as header, never as data; default "
            "limit=0 reads ALL rows — pass a limit to peek at a large file."
        ),
        # limit=0 is genuinely unbounded (rows cap only applies when limit>0):
        # a large BOM CSV otherwise dumps every row into context.
        "param_defaults": {"limit": 500},
    },
    "office_edit_csv": {
        "prompt_note": (
            "Full rewrite — you must send the ENTIRE dataset every call; rows "
            "is data rows only, pass header separately. office_read_csv first "
            "to preserve what you don't change."
        ),
    },
    "office_read_docx": {
        "prompt_note": (
            "Body-level paragraphs + tables only (no headers, footers or text "
            "boxes). paragraph order here is exactly what "
            "office_edit_docx(paragraph_index) addresses."
        ),
    },
    "office_edit_docx": {
        "prompt_note": (
            "paragraph_index is 0-based over body paragraphs only (no table "
            "cells); text goes into the first run and the rest are blanked — "
            "per-run formatting in the paragraph is lost."
        ),
    },
    "office_read_doc": {
        "prompt_note": (
            "Needs soffice/libreoffice on PATH (reason:converter_missing "
            "otherwise) — headless-converts to .docx in a tmp dir, then reads "
            "exactly like office_read_docx."
        ),
    },
    "office_edit_doc": {
        "prompt_note": (
            "Lossy soffice round-trip (.doc→.docx→.doc) with office_edit_docx "
            "ops in the middle — legacy .doc features may not survive. Prefer "
            "office_edit_docx whenever a .docx exists."
        ),
    },
    "office_read_pdf": {
        "prompt_note": (
            "page_range input is 1-based (\"1-3,5\") but the returned "
            "page.index is 0-based — mind the shift when feeding indices to "
            "office_edit_pdf."
        ),
    },
    "office_edit_pdf": {
        "prompt_note": (
            "Page indices in ops are 0-based and ops apply sequentially, so "
            "deletes shift later pages — delete in descending order. For text "
            "changes: extract, edit the docx, re-export to PDF."
        ),
    },
    "office_read_pptx": {
        "prompt_note": (
            "text_runs skips empty text frames, so run position is NOT the "
            "shape_index office_edit_pptx expects — count all shapes on the "
            "slide when targeting an edit."
        ),
    },
    "office_edit_pptx": {
        "prompt_note": (
            "slide and shape_index are 0-based and shape_index counts ALL "
            "shapes on the slide, not just text frames; one invalid op aborts "
            "the whole batch before anything is saved."
        ),
    },
    "office_read_xlsx": {
        "prompt_note": (
            "Returns cached values (data_only=True) — a formula cell shows its "
            "last calculated value or None if never calculated; the formula "
            "text itself is not visible here."
        ),
    },
    "office_edit_xlsx": {
        "prompt_note": (
            "Preserves existing formulas and .xlsm macros, but the openpyxl "
            "round-trip drops charts/images, and a newly written formula has "
            "no cached value until Excel recalculates it."
        ),
    },
}

"""``cad_guide('deliverables')`` topic text (contract §8) — how an agent drives this tool family."""
from __future__ import annotations

DELIVERABLES_GUIDE = """\
DELIVERABLES — ship professional engineering documents inside the app.

Five tools, all spec-driven (structured in, structured out). YOU author the structure and
prose; the tools fill every load-bearing number from tool results you attach — you never type a
number into a deliverable.

  make_presentation(spec) -> record   # .pptx deck        (template: smoke_deck)
  make_report(spec)       -> record   # .docx or .pdf     (template: smoke_report)
  make_workbook(spec)     -> record   # .xlsx, live formulas (template: smoke_workbook)
  render_document(path)   -> preview  # downscaled PNGs + a resource_link to the full file
  list_artifacts(run_id?) -> records  # what you've produced (optionally one run)

THE SPEC (one object per make_*):
  {
    "kind": "report",                 # must match the tool
    "title": "Bracket — Strength Report",
    "template": "smoke_report",       # one smoke template per format ships now
    "format": "docx",                 # optional; report: docx|pdf, deck: pptx, workbook: xlsx
    "run_id": "run_...",              # optional; groups a run's artifacts
    "outline": [ blocks ],            # ordered; block types are template-defined
    "attached_results": { "<id>": { "tool": "...", "ts": "...", "payload": {...} } }
  }

A block: { "type": "section", "heading": "...", "body": ["prose..."], "slots": {...}, "rows": [...] }

PROVENANCE — the rule that makes a deliverable trustworthy:
  Headings, body, captions are free prose — literal strings are fine.
  A LOAD-BEARING slot (a stress, mass, margin, cost, dimension, temperature, ...) MUST be a
  reference to an attached tool result, NOT a literal:
      "slots": { "max_stress": { "from": {"tool":"run_fea_static","key":"max_von_mises_mpa",
                                           "result":"fea1"}, "unit":"MPa", "round":1 } }
  A bare numeral in a load-bearing slot is a VALIDATION ERROR (provenance_violation) — the file
  is never written. A ref to a missing result/key is slot_unresolved. So: run the analysis tool,
  attach its result under attached_results, and reference it — the code writes the number.

WORKBOOKS: a sheet block carries "rows" (lists of cells). A cell is a free-prose label, a
provenance ref (load-bearing columns), or a live formula string like "=SUM(B2:B5)" written as a
real formula so the sheet recomputes when opened.

RENDER/PREVIEW: render_document(path) returns preview PNGs (≤1024 px, <1 MB) plus a resource_link
to the real file. Office formats need LibreOffice on PATH; if it is absent the artifact is still
produced and only the preview degrades ({ok:false, reason:"converter_missing"}). PDF/PNG/CSV/MD
preview in-process with no subprocess.

Every make_* returns a record: {artifact_id, kind, path, created_ts, run_id, source_refs[],
spec_ref}. list_artifacts reads them back, newest first.
"""

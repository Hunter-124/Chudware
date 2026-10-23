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

═══════════════════════════════════════════════════════════════════════════════════════════════
ENGINEERING TEMPLATES v1 — the standard documents a professional engineer ships. Each
registers against the same generic registry and is driven by one existing make_* tool (no new
tool). Select one via spec.template. The `deliverables.joins` helpers turn raw engine results
into the attached_results bag + the slot refs, so you never type a number.

1) design_review  — make_presentation, format pptx. Slides for a design review.
   Blocks: title | views (images:[render paths], captions) | massprops | fea_margins | risks.
   LOAD-BEARING slots — massprops: mass, volume, surface_area, cg_x/cg_y/cg_z;
   fea_margins: max_stress, min_fos, max_disp, yield_strength, margin.
   Example block:
     {"type":"fea_margins","heading":"FEA margins","slots":{
        "max_stress":{"from":{"tool":"run_fea_static","key":"max_von_mises_mpa","result":"fea1"},
                      "unit":"MPa","round":1},
        "min_fos":{"from":{"tool":"run_fea_static","key":"min_fos","result":"fea1"},"round":2}}}

2) calc_package  — make_report, format docx|pdf. Equations, assumptions, cited material data.
   Blocks: title | assumptions (prose bullets) | material | equation | results_table.
   LOAD-BEARING — material: density, youngs_modulus, yield_strength, ultimate_strength, poisson
   (the free-prose slots `source` + `fetched_ts` carry the Pillar-8 citation); equation: EVERY
   slot is load-bearing (its `expr` field is the prose formula); results_table: value column.
   Example:
     {"type":"material","heading":"AL 6061-T6","slots":{
        "yield_strength":{"from":{"tool":"material_data","key":"yield_strength_mpa","result":"mat1"},
                          "unit":"MPa","round":0},
        "source":{"from":{"tool":"material_data","key":"source","result":"mat1"}},
        "fetched_ts":{"from":{"tool":"material_data","key":"fetched_ts","result":"mat1"}}}}

3) bom_workbook  — make_workbook, format xlsx. One sheet per assembly level, LIVE cost formulas.
   Sheet columns are FIXED: Item | Qty | Unit Cost | Ext Cost | Source (indices 0..4).
   LOAD-BEARING columns: Qty (1), Unit Cost (2). Ext Cost is a live "=B2*C2" formula; a rollup
   row uses "=SUM(...)". Source is free prose (a sourced-part link, blank if unsourced).
   Easiest: `deliverables.joins.bom_rollup([{item,qty,unit_cost?,source?}, ...])` returns the
   sheet block + attached_results ready to drop into the spec.

4) verification_report  — make_report, format docx|pdf. Requirements → evidence matrix.
   Blocks: title | requirement (req_id, criterion, verdict = prose; body = requirement text) |
   summary. LOAD-BEARING slots on `requirement`: measured, limit, margin (the evidence numbers).
   Example:
     {"type":"requirement","req_id":"REQ-001","criterion":"peak stress < yield","verdict":"PASS",
      "slots":{"measured":{"from":{"tool":"run_fea_static","key":"max_von_mises_mpa",
               "result":"fea1"},"unit":"MPa","round":1}}}

5) assembly_manual  — make_report, format docx|pdf. Assembly steps + user manual. (make_manual
   stays reserved; manuals ship as a make_report template — no new tool.)
   Blocks: title | section (manual prose) | step (step_no, image = a render path, body =
   instructions). LOAD-BEARING slots on `step`: torque, fastener_count, gap, preload.
   Example:
     {"type":"step","step_no":2,"heading":"Torque bolts","image":"artifacts/step2.png",
      "body":["Tighten in a cross pattern."],
      "slots":{"torque":{"from":{"tool":"assembly","key":"bolt_torque_nm","result":"asm1"},
               "unit":"Nm","round":1}}}
"""

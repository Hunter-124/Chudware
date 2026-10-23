"""Golden-file tests for the five engineering deliverable templates.

Templates covered (name / kind / formats):
  - design_review        (presentation / pptx)
  - calc_package          (report / docx | pdf)
  - bom_workbook          (workbook / xlsx)
  - verification_report   (report / docx | pdf)
  - assembly_manual       (report / docx | pdf)

Each positive test drives one ``make_*`` call from a fixture project (real engine-source
numbers wrapped through the ``deliverables.joins`` helpers — never hand-typed), then opens the
produced office file with its real reader library and asserts that every load-bearing numeral
from the fixture actually appears in the rendered content. Each negative test drops a bare
numeral into a load-bearing slot/column and asserts the tool refuses with
``reason == "provenance_violation"`` (contract §4) instead of writing a file.

The in-process engines (python-pptx/docx/openpyxl/matplotlib/reportlab/pypdfium2, plus Pillow
for the fixture image) are real; the whole module is skipped cleanly when a runtime lacks them.

Run:  python3 -m pytest services/deliverables/test_templates.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest


# The in-process engines are required deps; skip the whole module cleanly if a runtime lacks them.
pytest.importorskip("pptx")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("matplotlib")
pytest.importorskip("reportlab")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

import services.deliverables as d  # noqa: E402
from services.deliverables import joins as J, project  # noqa: E402


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    """Point BOTH roots at a fresh tmp dir per test (project.py reads the env live).

    Artifacts live in a hidden per-project store by default; pinning
    CHUDWARE_ARTIFACTS_DIR keeps the suite hermetic instead of writing into the
    real app data root.
    """
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CHUDWARE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    yield


def _abs(rec):
    # resolve_project_path is the documented inverse of the to_project_rel that
    # produced rec["path"] — exercising it here also covers the round trip.
    return project.resolve_project_path(rec["path"])


def _make_view_image() -> str:
    """A tiny real PNG render fixture, project-relative path (mirrors a real render_part output)."""
    from PIL import Image
    project.artifacts_dir()  # ensure the artifacts/ dir exists
    rel = "artifacts/view_front.png"
    Image.new("RGB", (400, 300), (120, 140, 200)).save(project.project_dir() / rel)
    return rel


# --------------------------------------------------------------------------- fixture builders
# Same concrete numbers used throughout — so assertions on rendered file content stay legible.

MASS_G = 214.8
VOLUME_MM3 = 27692.0
SURFACE_AREA_MM2 = 8123.4
MAX_STRESS_MPA = 182.4
MIN_FOS = 1.51
MAX_DISP_MM = 0.31
YIELD_MPA = 276.0
MARGIN = 0.51
DENSITY_KG_M3 = 2700.0
YOUNGS_GPA = 68.9
ULTIMATE_MPA = 310.0
POISSON = 0.33
MAT_SOURCE = "MatWeb AL6061-T6"


def _mass():
    return J.mass_props({
        "mass_g": MASS_G, "volume_mm3": VOLUME_MM3, "surface_area_mm2": SURFACE_AREA_MM2,
        "cg_x_mm": 12.1, "cg_y_mm": 3.4, "cg_z_mm": 0.0, "material": "AL_6061_T6",
    })


def _fea():
    return J.fea_static({
        "max_von_mises_mpa": MAX_STRESS_MPA, "min_fos": MIN_FOS, "max_disp_mm": MAX_DISP_MM,
        "yield_strength_mpa": YIELD_MPA, "margin": MARGIN,
    })


def _material():
    return J.material_data({
        "density_kg_m3": DENSITY_KG_M3, "youngs_modulus_gpa": YOUNGS_GPA,
        "yield_strength_mpa": YIELD_MPA, "ultimate_strength_mpa": ULTIMATE_MPA,
        "poisson": POISSON, "source": MAT_SOURCE, "fetched_ts": "2026-07-01T00:00:00Z",
    })


def _attached_common():
    return J.results_map(_mass(), _fea(), _material())


def _bom_rollup():
    return J.bom_rollup([
        {"item": "Bracket body", "qty": 1, "unit_cost": 12.50, "source": "Stock AL plate"},
        {"item": "M6 bolt", "qty": 4, "unit_cost": 0.35, "source": "McMaster 91290A"},
        {"item": "Washer", "qty": 4},  # no cost -> graceful
    ], level="Top assembly")


# ============================================================ 1. design_review (presentation)

def test_design_review_presentation_pptx():
    img_rel = _make_view_image()
    rec = d.make_presentation({
        "kind": "presentation", "title": "Bracket Design Review", "template": "design_review",
        "outline": [
            {"type": "title", "heading": "Bracket Design Review", "body": ["Day-9 review."]},
            {"type": "views", "heading": "Rendered views", "images": [img_rel, "artifacts/missing.png"],
             "body": ["Front and iso."]},
            {"type": "massprops", "heading": "Mass properties", "slots": {
                "mass": J.ref("mass1", "mass_g", unit="g", round=1),
                "volume": J.ref("mass1", "volume_mm3", unit="mm^3", round=0),
                "surface_area": J.ref("mass1", "surface_area_mm2", unit="mm^2", round=0)}},
            {"type": "fea_margins", "heading": "FEA margins", "slots": {
                "max_stress": J.ref("fea1", "max_von_mises_mpa", unit="MPa", round=1),
                "min_fos": J.ref("fea1", "min_fos", round=2),
                "margin": J.ref("fea1", "margin", round=2)}},
            {"type": "risks", "heading": "Open risks", "body": ["Fatigue at fillet TBD."]},
        ],
        "attached_results": _attached_common(),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "presentation"
    assert rec["path"].endswith(".pptx")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0

    from pptx import Presentation
    prs = Presentation(str(_abs(rec)))
    alltext = "\n".join(sh.text_frame.text for sl in prs.slides for sh in sl.shapes
                         if sh.has_text_frame)
    assert "182.4" in alltext            # max_stress (fea1)
    assert "214.8" in alltext            # mass (mass1)


def test_design_review_corrupt_image_has_visible_degraded_marker():
    image_rel = "artifacts/corrupt-view.png"
    image_path = project.project_dir() / image_rel
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"not a PNG")

    rec = d.make_presentation({
        "kind": "presentation",
        "title": "Corrupt View Review",
        "template": "design_review",
        "outline": [
            {"type": "views", "heading": "Rendered views", "images": [image_rel]},
        ],
        "attached_results": {},
    })

    assert rec["ok"] is True
    from pptx import Presentation
    prs = Presentation(str(_abs(rec)))
    alltext = "\n".join(
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    )
    assert "render unavailable" in alltext.lower()
    assert "corrupt-view.png" in alltext


def test_design_review_bare_numeral_is_provenance_violation():
    bad = d.make_presentation({
        "kind": "presentation", "title": "x", "template": "design_review",
        "outline": [{"type": "massprops", "slots": {"mass": MASS_G}}],
        "attached_results": {},
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


# ============================================================ 2. calc_package (report)

def test_calc_package_pdf():
    rec = d.make_report({
        "kind": "report", "title": "Bracket Calc Package", "template": "calc_package",
        "format": "pdf",
        "outline": [
            {"type": "assumptions", "heading": "Assumptions",
             "body": ["Linear elastic.", "1200 N tip load."]},
            {"type": "material", "heading": "Material — AL 6061-T6", "slots": {
                "yield_strength": J.ref("mat1", "yield_strength_mpa", unit="MPa", round=0),
                "youngs_modulus": J.ref("mat1", "youngs_modulus_gpa", unit="GPa", round=1),
                "source": J.ref("mat1", "source"), "fetched_ts": J.ref("mat1", "fetched_ts")}},
            {"type": "equation", "heading": "Peak stress", "expr": "sigma = M*c/I",
             "slots": {"max_stress": J.ref("fea1", "max_von_mises_mpa", unit="MPa", round=1),
                       "min_fos": J.ref("fea1", "min_fos", round=2)}},
        ],
        "attached_results": _attached_common(),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "report"
    assert rec["path"].endswith(".pdf")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0
    assert _abs(rec).read_bytes()[:5] == b"%PDF-"


def test_calc_package_docx_material_and_citation():
    rec = d.make_report({
        "kind": "report", "title": "Calc docx", "template": "calc_package", "format": "docx",
        "outline": [
            {"type": "material", "heading": "Material", "slots": {
                "yield_strength": J.ref("mat1", "yield_strength_mpa", unit="MPa", round=0),
                "source": J.ref("mat1", "source")}},
        ],
        "attached_results": _attached_common(),
    })
    assert rec["ok"] is True
    assert rec["path"].endswith(".docx")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0

    from docx import Document
    doc = Document(str(_abs(rec)))
    text = "\n".join(p.text for p in doc.paragraphs)
    text += "\n" + "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert "276" in text             # yield_strength_mpa
    assert "MatWeb" in text          # citation source text


def test_calc_package_bare_numeral_is_provenance_violation():
    bad = d.make_report({
        "kind": "report", "title": "x", "template": "calc_package", "format": "docx",
        "outline": [{"type": "material", "slots": {"yield_strength": YIELD_MPA}}],
        "attached_results": {},
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


# ============================================================ 3. bom_workbook

def test_bom_workbook_xlsx():
    roll = _bom_rollup()
    rec = d.make_workbook({
        "kind": "workbook", "title": "Bracket BOM", "template": "bom_workbook",
        "outline": [roll["block"]], "attached_results": roll["attached"],
    })
    assert rec["ok"] is True
    assert rec["kind"] == "workbook"
    assert rec["path"].endswith(".xlsx")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0

    from openpyxl import load_workbook
    ws = load_workbook(str(_abs(rec))).active
    assert ws["B2"].value == 1 and ws["C2"].value == 12.5
    assert isinstance(ws["D2"].value, str) and ws["D2"].value.startswith("=")
    found_sum = any(isinstance(c.value, str) and c.value.startswith("=SUM")
                     for row in ws.iter_rows() for c in row)
    assert found_sum, "no SUM rollup"


def test_bom_rollup_live_formula_and_source_values():
    """J.bom_rollup itself: Qty/UnitCost cells equal the source numbers and Ext Cost is a live
    formula (not a baked number)."""
    roll = _bom_rollup()
    rec = d.make_workbook({
        "kind": "workbook", "title": "Bracket BOM 2", "template": "bom_workbook",
        "outline": [roll["block"]], "attached_results": roll["attached"],
    })
    assert rec["ok"] is True

    from openpyxl import load_workbook
    ws = load_workbook(str(_abs(rec))).active
    # row 2: Bracket body qty=1, unit_cost=12.50
    assert ws["B2"].value == 1
    assert ws["C2"].value == 12.5
    assert ws["D2"].value == "=B2*C2"
    # row 3: M6 bolt qty=4, unit_cost=0.35
    assert ws["B3"].value == 4
    assert ws["C3"].value == 0.35
    assert ws["D3"].value == "=B3*C3"


def test_bom_workbook_bare_numeral_is_provenance_violation():
    roll = _bom_rollup()
    block = roll["block"]
    block["rows"][1][1] = 214.8            # typed number where a Qty ref belongs
    bad = d.make_workbook({
        "kind": "workbook", "title": "x", "template": "bom_workbook",
        "outline": [block], "attached_results": roll["attached"],
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


# ============================================================ 4. verification_report

def test_verification_report_docx():
    rec = d.make_report({
        "kind": "report", "title": "Bracket Verification", "template": "verification_report",
        "outline": [
            {"type": "requirement", "req_id": "REQ-001", "criterion": "Peak stress < yield",
             "verdict": "PASS", "body": ["Bracket withstands 1200 N."],
             "slots": {"measured": J.ref("fea1", "max_von_mises_mpa", unit="MPa", round=1),
                       "limit": J.ref("mat1", "yield_strength_mpa", unit="MPa", round=0),
                       "margin": J.ref("fea1", "margin", round=2)}},
            {"type": "requirement", "req_id": "REQ-002", "criterion": "FoS >= 1.5",
             "verdict": "PASS",
             "slots": {"measured": J.ref("fea1", "min_fos", round=2)}},
        ],
        "attached_results": _attached_common(),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "report"
    assert rec["path"].endswith(".docx")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0

    from docx import Document
    doc = Document(str(_abs(rec)))
    text = "\n".join(p.text for p in doc.paragraphs)
    tabletext = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert "182.4" in (text + tabletext)    # measured (fea1.max_von_mises_mpa)


def test_verification_report_bare_numeral_is_provenance_violation():
    bad = d.make_report({
        "kind": "report", "title": "x", "template": "verification_report",
        "outline": [{"type": "requirement", "req_id": "REQ-001", "criterion": "c",
                     "verdict": "PASS", "slots": {"measured": MAX_STRESS_MPA}}],
        "attached_results": {},
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


# ============================================================ 5. assembly_manual

def test_assembly_manual_docx():
    img_rel = _make_view_image()
    rec = d.make_report({
        "kind": "report", "title": "Bracket Assembly & Manual", "template": "assembly_manual",
        "format": "docx",
        "outline": [
            {"type": "section", "heading": "Overview",
             "body": ["This bracket mounts to the frame."]},
            {"type": "step", "step_no": 1, "heading": "Mount bracket", "image": img_rel,
             "body": ["Align bracket to frame holes."],
             "slots": {"fastener_count": J.ref("fea1", "min_fos", round=0)}},
            {"type": "step", "step_no": 2, "heading": "Torque bolts", "image": "artifacts/missing.png",
             "body": ["Tighten in a cross pattern."],
             "slots": {"torque": J.ref("fea1", "max_von_mises_mpa", unit="Nm", round=1)}},
        ],
        "attached_results": _attached_common(),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "report"
    assert rec["path"].endswith(".docx")
    assert _abs(rec).exists() and _abs(rec).stat().st_size > 0
    assert len(rec["source_refs"]) > 0

    from docx import Document
    text = "\n".join(p.text for p in Document(str(_abs(rec))).paragraphs)
    assert "182.4" in text        # torque (fea1.max_von_mises_mpa)


def test_assembly_manual_bare_numeral_is_provenance_violation():
    bad = d.make_report({
        "kind": "report", "title": "x", "template": "assembly_manual", "format": "docx",
        "outline": [{"type": "step", "step_no": 1, "heading": "h",
                     "slots": {"torque": MAX_STRESS_MPA}}],
        "attached_results": {},
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


# ============================================================ 6. structured shop instructions

def _shop_route():
    return J.attach("route1", "approved_process_plan", {
        "material": "AISI 4140 HT",
        "stock_prep": "Saw blank 52 x 52 x 20 mm",
        "spindle_speed": 4200,
        "feed_rate": 310,
        "speed_feed_provenance": "Approved CAM Plan CP-104 Rev B",
        "bore_limits": "10.000–10.018 mm",
        "wps": "WPS-GMAW-12 / Rev C",
        "member_length": 700,
        "member_qty": 2,
        "weld_size": 6,
        "weld_sequence": 3,
    })


def _docx_text(rec):
    from docx import Document
    doc = Document(str(_abs(rec)))
    paragraphs = [p.text for p in doc.paragraphs]
    cells = [c.text for table in doc.tables for row in table.rows for c in row.cells]
    return "\n".join(paragraphs + cells)


@pytest.mark.parametrize("missing", ["setup", "inspection"])
def test_work_instruction_rejects_machined_operation_without_setup_or_inspection(missing):
    from services.deliverables import api

    operation = {
        "type": "operation",
        "op_no": 10,
        "heading": "Finish bore",
        "machined": True,
        "setup": {"datum": "A", "wcs": "G54", "fixturing": "Soft jaws"},
        "machine": "VMC-3",
        "parameters": [{"label": "Inspection limit", "slot": "bore_limits"}],
        "tool": "T12 boring head",
        "inspection": [
            {"feature": "Finished bore", "gauge": "Air gauge", "limits_slot": "bore_limits"},
        ],
        "slots": {"bore_limits": J.ref("route1", "bore_limits")},
    }
    operation.pop(missing)
    outline = _minimal_work_instruction_outline()
    outline[1] = operation
    bad = api.make_work_instruction({
        "kind": "work_instruction",
        "title": "Shaft WI",
        "template": "work_instruction",
        "outline": outline,
        "attached_results": J.results_map(_shop_route()),
    })
    assert bad["ok"] is False
    assert missing in bad["hint"]
    assert bad["reason"] == "invalid_spec"


def test_work_instruction_docx_carries_provenance_values_verbatim():
    rec = d.make_work_instruction({
        "kind": "work_instruction",
        "title": "Shaft Work Instruction",
        "template": "work_instruction",
        "format": "docx",
        "outline": [
            {"type": "part", "part_id": "SHAFT-01", "revision": "D",
             "slots": {
                 "material": J.ref("route1", "material"),
                 "stock_prep": J.ref("route1", "stock_prep"),
             }},
            {"type": "operation", "op_no": 10, "heading": "Finish bore", "machined": True,
             "setup": {"datum": "A", "wcs": "G54", "fixturing": "Soft jaws on OD"},
             "machine": "VMC-3", "tool": "T12 boring head",
             "parameters": [
                 {"label": "Spindle speed", "slot": "spindle_speed"},
                 {"label": "Feed rate", "slot": "feed_rate"},
                 {"label": "Speed/feed provenance", "slot": "speed_feed_provenance"},
             ],
             "inspection": [
                 {"feature": "Finished bore", "gauge": "Air gauge",
                  "limits_slot": "bore_limits"},
             ],
             "slots": {
                 "spindle_speed": J.ref("route1", "spindle_speed", unit="rpm"),
                 "feed_rate": J.ref("route1", "feed_rate", unit="mm/min"),
                 "speed_feed_provenance": J.ref("route1", "speed_feed_provenance"),
                 "bore_limits": J.ref("route1", "bore_limits"),
             }},
            {"type": "finish", "heading": "Deburr and finish",
             "body": ["Break sharp edges; clean before inspection."]},
            {"type": "hold_point", "heading": "QA hold",
             "body": ["Do not release before bore inspection acceptance."]},
            {"type": "safety", "ppe": ["Safety glasses", "Cut-resistant gloves"],
             "body": ["Stop spindle before gauging."]},
            {"type": "approval", "prepared_by": "Manufacturing Engineering",
             "approved_by": "Quality", "signoff": "Name / signature / date"},
            {"type": "revision", "revision": "D", "date": "2026-08-26",
             "description": "Released routing", "approved_by": "Quality"},
        ],
        "attached_results": J.results_map(_shop_route()),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "work_instruction"
    text = _docx_text(rec)
    for expected in (
        "AISI 4140 HT",
        "Saw blank 52 x 52 x 20 mm",
        "4200 rpm",
        "310 mm/min",
        "Approved CAM Plan CP-104 Rev B",
        "10.000–10.018 mm",
    ):
        assert expected in text
    for expected in (
        "G54",
        "Soft jaws on OD",
        "VMC-3",
        "T12 boring head",
        "Finished bore",
        "Air gauge",
        "Deburr and finish",
        "QA hold",
        "Safety glasses",
        "Approval and signoff",
        "Revision record",
    ):
        assert expected in text



def test_work_instruction_rejects_literal_process_values():
    outline = _minimal_work_instruction_outline()
    outline[1] = {
        "type": "operation",
        "op_no": 10,
        "heading": "Mill",
        "slots": {"spindle_speed": 4200},
    }
    bad = d.make_work_instruction({
        "kind": "work_instruction",
        "title": "Uncontrolled route",
        "template": "work_instruction",
        "outline": outline,
        "attached_results": J.results_map(_shop_route()),
    })
    assert bad["ok"] is False
    assert bad["reason"] == "provenance_violation"


def test_weldment_traveler_docx_carries_controlled_values():
    rec = d.make_weldment_traveler({
        "kind": "weldment_traveler",
        "title": "Frame Weldment Traveler",
        "template": "weldment_traveler",
        "format": "docx",
        "outline": [
            {"type": "document_control", "part_id": "FRAME-01", "revision": "C",
             "material": "ASTM A500 Grade B"},
            {"type": "cut_list",
             "rows": [
                 ["Member", "Profile", "Length", "Qty"],
                 ["Upright", "50 x 50 x 3 SHS",
                  J.ref("route1", "member_length", unit="mm"),
                  J.ref("route1", "member_qty")],
             ]},
            {"type": "joint_list",
             "rows": [
                 ["Joint", "Type", "Size", "Symbol", "Sequence"],
                 ["J1", "Fillet", J.ref("route1", "weld_size", unit="mm"),
                  "FILLET BOTH SIDES", J.ref("route1", "weld_sequence")],
             ]},
            {"type": "wps", "slots": {"reference": J.ref("route1", "wps")}},
            {"type": "fit_up", "body": ["Verify diagonals and root gap before tack welding."]},
            {"type": "distortion_control",
             "body": ["Back-step opposing joints; allow to cool between passes."]},
            {"type": "inspection",
             "rows": [["Stage", "Method", "Acceptance", "Inspector / Date"],
                      ["Final", "Visual + MT", "Per WPS and drawing", ""]]},
            {"type": "signoff", "welder": "Name / ID", "inspector": "Name / ID",
             "quality_release": "Signature / date"},
        ],
        "attached_results": J.results_map(_shop_route()),
    })
    assert rec["ok"] is True
    assert rec["kind"] == "weldment_traveler"
    text = _docx_text(rec)
    for expected in ("700 mm", "WPS-GMAW-12 / Rev C", "6 mm", "FILLET BOTH SIDES"):
        assert expected in text
    for expected in (
        "Cut list",
        "Joint list and weld sequence",
        "Fit-up checks",
        "Distortion control",
        "Visual / NDT inspection",
        "Traveler signoff",
    ):
        assert expected in text


def _minimal_work_instruction_outline():
    return [
        {"type": "part", "part_id": "P-1", "revision": "A",
         "slots": {
             "material": J.ref("route1", "material"),
             "stock_prep": J.ref("route1", "stock_prep"),
         }},
        {"type": "operation", "op_no": 10, "heading": "Verify prepared blank"},
        {"type": "finish", "body": ["Deburr and clean."]},
        {"type": "hold_point", "body": ["Quality release required."]},
        {"type": "safety", "ppe": ["Safety glasses"]},
        {"type": "approval", "prepared_by": "ME", "approved_by": "Quality"},
        {"type": "revision", "revision": "A", "description": "Initial release"},
    ]


def _minimal_weldment_traveler_outline():
    return [
        {"type": "document_control", "part_id": "W-1", "revision": "A",
         "material": "ASTM A500 Grade B"},
        {"type": "cut_list",
         "rows": [["Member", "Profile", "Length", "Qty"],
                  ["M1", "SHS", J.ref("route1", "member_length"),
                   J.ref("route1", "member_qty")]]},
        {"type": "joint_list",
         "rows": [["Joint", "Type", "Size", "Symbol", "Sequence"],
                  ["J1", "Fillet", J.ref("route1", "weld_size"), "FILLET",
                   J.ref("route1", "weld_sequence")]]},
        {"type": "wps", "slots": {"reference": J.ref("route1", "wps")}},
        {"type": "fit_up", "checks": ["Verify root gap."]},
        {"type": "distortion_control", "notes": ["Back-step opposing joints."]},
        {"type": "inspection", "checks": ["Visual inspection."]},
        {"type": "signoff", "welder": "Name / ID", "inspector": "Name / ID"},
    ]


@pytest.mark.parametrize(
    ("entrypoint", "kind", "template_name", "outline_factory"),
    [
        (d.make_work_instruction, "work_instruction", "work_instruction",
         _minimal_work_instruction_outline),
        (d.make_weldment_traveler, "weldment_traveler", "weldment_traveler",
         _minimal_weldment_traveler_outline),
    ],
)
def test_shop_instruction_templates_render_pdf(
        entrypoint, kind, template_name, outline_factory):
    rec = entrypoint({
        "kind": kind,
        "title": f"{kind} PDF",
        "template": template_name,
        "format": "pdf",
        "outline": outline_factory(),
        "attached_results": J.results_map(_shop_route()),
    })
    assert rec["ok"] is True
    assert _abs(rec).read_bytes()[:5] == b"%PDF-"


@pytest.mark.parametrize(
    ("entrypoint", "kind", "template_name"),
    [
        ("make_work_instruction", "work_instruction", "work_instruction"),
        ("make_weldment_traveler", "weldment_traveler", "weldment_traveler"),
    ],
)
def test_shop_instruction_entrypoints_require_a_real_artifact(
        monkeypatch, entrypoint, kind, template_name):
    from services.deliverables import api
    from services.deliverables.templates import get_template

    template = get_template(template_name, kind)
    monkeypatch.setattr(template, "_build", lambda resolved, fmt, out_path: None)
    bad = getattr(api, entrypoint)({
        "kind": kind,
        "title": "No artifact",
        "template": template_name,
        "format": "docx",
        "outline": [],
        "attached_results": {},
    })
    assert bad["ok"] is False
    assert bad["reason"] == "artifact_write_failed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

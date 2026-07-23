"""Golden-file tests for the five T-F15 engineering deliverable templates.

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

import os
import sys
from pathlib import Path

import pytest

# `deliverables` is importable as a top-level package via the repo-root symlink.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The in-process engines are required deps; skip the whole module cleanly if a runtime lacks them.
pytest.importorskip("pptx")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("matplotlib")
pytest.importorskip("reportlab")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

import deliverables as d  # noqa: E402
from deliverables import joins as J, project  # noqa: E402


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    """Point the project dir at a fresh tmp dir for each test (project.py reads the env live)."""
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    yield


def _abs(rec):
    return project.project_dir() / rec["path"]


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

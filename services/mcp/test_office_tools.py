"""Regression tests for services/mcp/office_tools.py (op-vocabulary sweep).

Pure-Python — no libchudware DLL required. Registers the tools on a throwaway
FastMCP and drives them against a tmp PDF.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

pypdf = pytest.importorskip("pypdf")

from services.mcp.office_tools import extract_office, register_office_tools  # noqa: E402


def _mcp() -> FastMCP:
    m = FastMCP("test-office")
    register_office_tools(m)
    return m


def _call(mcp: FastMCP, name: str, args: dict):
    _content, structured = asyncio.run(mcp.call_tool(name, args))
    out = structured.get("result") if isinstance(structured, dict) else structured
    return json.loads(out) if isinstance(out, str) else out


def _tiny_pdf(tmp_path: Path) -> str:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    path = tmp_path / "in.pdf"
    with open(path, "wb") as f:
        writer.write(f)
    return str(path)


def _soffice() -> str:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        pytest.skip("LibreOffice is not installed")
    return binary


def _convert_fixture(mcp: FastMCP, path: Path, target: str, out: Path | None = None) -> Path:
    result = _call(
        mcp,
        "office_convert",
        {"path": str(path), "to": target, **({"out": str(out)} if out else {})},
    )
    assert result["ok"], result
    return Path(result["path"])


def _legacy_fixture(path: Path, target: str) -> Path:
    proc = subprocess.run(
        [
            _soffice(),
            "--headless",
            "--convert-to",
            target,
            "--outdir",
            str(path.parent),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    converted = path.with_suffix(f".{target}")
    assert proc.returncode == 0 and converted.is_file(), proc.stderr
    return converted


def test_office_edit_pdf_unknown_op_names_the_closed_vocabulary(tmp_path):
    """Class-3/5: the old message was ``unknown op '<x>'`` and nothing else — it
    named neither the accepted vocabulary nor a hint to recover. The op space
    here is closed and tiny (rotate/delete/merge/set_metadata), so the error
    must say so."""
    path = _tiny_pdf(tmp_path)
    out = _call(_mcp(), "office_edit_pdf",
                {"path": path, "ops": [{"op": "resize"}]})
    assert out["ok"] is False
    assert out["reason"] == "invalid_op"
    assert "resize" in out["hint"]
    for word in ("rotate", "delete", "merge", "set_metadata"):
        assert word in out["hint"]


def test_office_edit_pdf_still_rejects_reflow_as_unsupported(tmp_path):
    """Recognized-but-unsupported ops (edit_text/replace_text/reflow) must keep
    their distinct ``unsupported_op`` reason, not fall into the generic
    unknown-op branch."""
    path = _tiny_pdf(tmp_path)
    out = _call(_mcp(), "office_edit_pdf",
                {"path": path, "ops": [{"op": "edit_text"}]})
    assert out["ok"] is False
    assert out["reason"] == "unsupported_op"


def test_office_extract_odf_formats_via_libreoffice(tmp_path):
    _soffice()
    docx = pytest.importorskip("docx")
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")
    mcp = _mcp()

    word = docx.Document()
    word.add_paragraph("ODT fixture text")
    docx_path = tmp_path / "fixture.docx"
    word.save(docx_path)
    odt = _convert_fixture(mcp, docx_path, "odt")

    slides = pptx.Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[5])
    slide.shapes.add_textbox(0, 0, 1000000, 1000000).text = "ODP fixture text"
    pptx_path = tmp_path / "fixture.pptx"
    slides.save(pptx_path)
    odp = _convert_fixture(mcp, pptx_path, "odp")

    book = openpyxl.Workbook()
    book.active["A1"] = "ODS fixture text"
    xlsx_path = tmp_path / "fixture.xlsx"
    book.save(xlsx_path)
    ods = _convert_fixture(mcp, xlsx_path, "ods")

    for path, kind, text in (
        (odt, "odt", "ODT fixture text"),
        (odp, "odp", "ODP fixture text"),
        (ods, "ods", "ODS fixture text"),
    ):
        result = extract_office(str(path))
        assert result["ok"], result
        assert result["kind"] == kind
        assert text in result["text"]


def test_office_extract_legacy_ppt_and_xls_via_libreoffice(tmp_path):
    _soffice()
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")
    mcp = _mcp()

    slides = pptx.Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[5])
    slide.shapes.add_textbox(0, 0, 1000000, 1000000).text = "legacy PPT text"
    pptx_path = tmp_path / "legacy.pptx"
    slides.save(pptx_path)
    ppt = _legacy_fixture(pptx_path, "ppt")

    book = openpyxl.Workbook()
    book.active["A1"] = "legacy XLS text"
    xlsx_path = tmp_path / "legacy.xlsx"
    book.save(xlsx_path)
    xls = _legacy_fixture(xlsx_path, "xls")

    ppt_result = extract_office(str(ppt))
    assert ppt_result["ok"], ppt_result
    assert ppt_result["kind"] == "ppt"
    assert "legacy PPT text" in ppt_result["text"]

    xls_result = extract_office(str(xls))
    assert xls_result["ok"], xls_result
    assert xls_result["kind"] == "xls"
    assert "legacy XLS text" in xls_result["text"]
    assert xls_result["sheets"][0]["rows"][0][0] == "legacy XLS text"


def test_office_convert_docx_odt_docx_round_trip(tmp_path):
    _soffice()
    docx = pytest.importorskip("docx")
    mcp = _mcp()
    source = tmp_path / "source.docx"
    doc = docx.Document()
    doc.add_paragraph("round-trip office text")
    doc.save(source)

    odt = _convert_fixture(mcp, source, "odt")
    restored = _convert_fixture(mcp, odt, "docx", tmp_path / "restored.docx")

    result = extract_office(str(restored))
    assert result["ok"], result
    assert "round-trip office text" in result["text"]


def test_soffice_paths_degrade_to_typed_converter_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("services.deliverables.engines.soffice_bin", lambda: None)
    missing = {}
    for suffix in ("odt", "odp", "ods", "ppt", "xls"):
        path = tmp_path / f"missing.{suffix}"
        path.write_bytes(b"not reached")
        missing[suffix] = extract_office(str(path))
    expected = {
        "ok": False,
        "reason": "converter_missing",
        "hint": "install LibreOffice",
    }
    assert missing == {suffix: expected for suffix in missing}
    odt = tmp_path / "missing.odt"

    converted = _call(
        _mcp(),
        "office_convert",
        {"path": str(odt), "to": "docx"},
    )
    assert converted == {
        "ok": False,
        "reason": "converter_missing",
        "hint": "install LibreOffice",
    }


def test_office_edit_docx_run_and_structural_edits_preserve_other_content(tmp_path):
    docx = pytest.importorskip("docx")
    source = tmp_path / "editable.docx"
    doc = docx.Document()
    first = doc.add_paragraph()
    first.add_run("prefix ")
    emphasized = first.add_run("replace me")
    emphasized.bold = True
    doc.add_paragraph("untouched paragraph")
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "old cell"
    doc.save(source)

    result = _call(
        _mcp(),
        "office_edit_docx",
        {
            "path": str(source),
            "ops": [
                {
                    "op": "replace_run_text",
                    "paragraph_index": 0,
                    "run_index": 1,
                    "text": "replacement",
                },
                {
                    "op": "insert_paragraph_after",
                    "paragraph_index": 0,
                    "text": "inserted paragraph",
                },
                {
                    "op": "set_table_cell",
                    "table_index": 0,
                    "row": 0,
                    "column": 0,
                    "text": "new cell",
                },
                {
                    "op": "insert_heading",
                    "after_paragraph_index": 2,
                    "level": 2,
                    "text": "Inserted heading",
                },
            ],
        },
    )
    assert result["ok"], result

    edited = docx.Document(source)
    assert edited.paragraphs[0].text == "prefix replacement"
    assert edited.paragraphs[0].runs[0].text == "prefix "
    assert edited.paragraphs[0].runs[1].bold is True
    assert edited.paragraphs[1].text == "inserted paragraph"
    assert edited.paragraphs[2].text == "untouched paragraph"
    assert edited.paragraphs[3].text == "Inserted heading"
    assert edited.paragraphs[3].style.name == "Heading 2"
    assert edited.tables[0].cell(0, 0).text == "new cell"


def test_office_edit_pptx_edits_table_cell_without_touching_text_shape(tmp_path):
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    source = tmp_path / "table.pptx"
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    text_shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    text_shape.text = "untouched"
    table_shape = slide.shapes.add_table(
        1, 1, Inches(1), Inches(2), Inches(2), Inches(1)
    )
    table_shape.table.cell(0, 0).text = "old"
    prs.save(source)

    table_index = list(slide.shapes).index(table_shape)
    result = _call(
        _mcp(),
        "office_edit_pptx",
        {
            "path": str(source),
            "ops": [
                {
                    "slide": 0,
                    "shape_index": table_index,
                    "row": 0,
                    "column": 0,
                    "text": "new",
                }
            ],
        },
    )
    assert result["ok"], result

    edited = pptx.Presentation(source)
    assert edited.slides[0].shapes[table_index].table.cell(0, 0).text == "new"
    assert edited.slides[0].shapes[1].text == "untouched"


def test_preview_engine_classifies_convertible_html():
    from services.deliverables.engines import classify_ext

    assert classify_ext("html") == "text"

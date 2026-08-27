"""Machining reference lookup and project-local PDF index coverage."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

from services.research import machining_reference as ref


def _tiny_pdf(text: str) -> bytes:
    """Build one text-only PDF without adding a PDF-writing test dependency."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 16 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def test_search_returns_cited_reaming_capability():
    result = ref.machining_ref_search("reaming tolerance")
    assert result["ok"] is True
    hit = next(row for row in result["results"] if row["id"] == "capability-reaming-it")
    assert hit["source"]["standard"] == "ISO 286-1:2010"
    assert hit["source"]["section"]


def test_fit_query_delegates_to_canonical_calculator():
    result = ref.machining_ref_search("H7 g6")
    assert result["ok"] is True
    assert result["results"][0]["delegate_tool"] == "fit_calc"
    assert all(row.get("id") != "iso-fit-table" for row in result["results"])
    press = ref.machining_ref_search("press shrink fit interference by diameter")
    assert press["results"][0]["delegate_tool"] == "fit_calc"


def test_only_canonical_thread_facts_delegate():
    basic = ref.machining_ref_search("M8 thread pitch")
    assert basic["results"][0]["delegate_tool"] == "research_thread_spec"
    engagement = ref.machining_ref_search("thread engagement material class")
    assert engagement["results"][0]["id"] == "thread-engagement-material-class"


def test_tap_drill_delegates_to_existing_dataset():
    result = ref.machining_ref_search("M8 tap drill")
    assert result["results"][0]["delegate_tool"] == "tap_drill"


def test_exact_get_and_unknown_id():
    found = ref.machining_ref_get("general-tolerance-linear-m")
    assert found["ok"] is True
    assert found["record"]["source"]["standard"] == "ISO 2768-1:1989"
    missing = ref.machining_ref_get("does-not-exist")
    assert missing["ok"] is False
    assert missing["reason"] == "not_found"


def test_every_curated_row_has_value_and_full_citation():
    for row in ref._records():
        assert {"id", "topic", "unit", "source", "notes"} <= row.keys()
        assert len({"value", "range", "table"} & row.keys()) == 1
        assert row["source"]["standard"]
        assert row["source"]["section"]


def test_required_handbook_topics_are_present():
    ids = {row["id"] for row in ref._records()}
    assert {
        "general-tolerance-linear-f",
        "general-tolerance-linear-m",
        "general-tolerance-linear-c",
        "general-tolerance-linear-v",
        "general-tolerance-angular",
        "general-geometrical-tolerance-classes",
        "capability-drilling-it",
        "capability-reaming-it",
        "capability-boring-it",
        "capability-milling-it",
        "capability-turning-it",
        "capability-grinding-it",
        "capability-honing-it",
        "capability-lapping-it",
        "drilled-hole-depth-capability",
        "reaming-stock-allowance",
        "boring-stock-allowance",
        "surface-finish-by-operation",
        "internal-corner-radius-cutter",
        "metric-preferred-drill-series",
        "letter-drill-series-common",
        "number-drill-series-common",
        "metric-shcs-counterbore",
        "inch-shcs-counterbore",
        "flat-head-countersink",
        "thread-engagement-material-class",
        "metric-keyway-keyseat",
        "inch-keyway-keyseat",
        "center-drill-sizes",
        "edge-break-chamfer-norm",
        "heat-treat-grind-stock",
    } <= ids


def test_pdf_index_returns_page_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pdf = tmp_path / "owned-reference.pdf"
    pdf.write_bytes(_tiny_pdf("Precision reaming tolerance requires controlled stock allowance."))

    indexed = ref.machining_ref_index_pdf(str(pdf), project_root=str(tmp_path))
    assert indexed["ok"] is True
    assert indexed["indexed_pages"] == 1
    assert indexed["scanned_pages"] == []
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    assert indexed["extractor"]["name"] == "pypdfium2"
    assert indexed["extractor"]["version"]
    assert indexed["indexed_at"] > 0
    ref._INDEX_PATHS.pop(indexed["source_id"])

    result = ref.machining_ref_search_pdf(
        "controlled stock allowance", source_id=indexed["source_id"], limit=3
    )
    assert result["ok"] is True
    assert result["results"]
    citation = result["results"][0]["citation"]
    assert citation == {
        "source_id": indexed["source_id"],
        "sha256": indexed["sha256"],
        "pdf_page": 1,
        "page_index": 0,
    }


def test_no_text_pages_are_reported_without_claiming_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(_tiny_pdf(""))
    monkeypatch.setattr(
        ref,
        "_extract_pdf_pages",
        lambda _path: {
            "ok": True,
            "pages": [{"index": 0, "text": ""}],
            "page_count": 1,
            "page_errors": [],
        },
    )
    result = ref.machining_ref_index_pdf(str(pdf), project_root=str(tmp_path))
    assert result["ok"] is True
    assert result["indexed_pages"] == 0
    assert result["scanned_pages"] == [1]
    assert result["ocr_used"] is False


def test_missing_pdfium_degrades_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "owned-reference.pdf"
    pdf.write_bytes(_tiny_pdf("text"))
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    result = ref.machining_ref_index_pdf(str(pdf), project_root=str(tmp_path))
    assert result["ok"] is False
    assert result["reason"] == "engine_missing"


def test_missing_fts5_degrades_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "owned-reference.pdf"
    pdf.write_bytes(_tiny_pdf("text"))
    monkeypatch.setattr(
        ref,
        "_extract_pdf_pages",
        lambda _path: {"ok": True, "pages": [{"index": 0, "text": "text"}], "page_count": 1},
    )

    def no_fts(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(ref.sqlite3, "connect", no_fts)
    result = ref.machining_ref_index_pdf(str(pdf), project_root=str(tmp_path))
    assert result["ok"] is False
    assert result["reason"] == "fts5_unavailable"


def test_mcp_registration_and_dialect_normalization(tmp_path: Path):
    from mcp.server.fastmcp import FastMCP
    from services.mcp.research_tools import register_research_tools

    mcp = FastMCP("machining-ref-test")
    register_research_tools(mcp)
    manager = mcp._tool_manager
    for name in (
        "machining_ref_search",
        "machining_ref_get",
        "machining_ref_index_pdf",
        "machining_ref_search_pdf",
    ):
        assert manager.get_tool(name) is not None

    search = manager.get_tool("machining_ref_search").fn
    result = json.loads(search(query="reaming tolerance"))
    assert result["ok"] is True
    get = manager.get_tool("machining_ref_get").fn
    result = json.loads(get(id="capability-reaming-it"))
    assert result["record"]["id"] == "capability-reaming-it"

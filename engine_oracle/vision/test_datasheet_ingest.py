"""Tests for engine_oracle/vision/datasheet_ingest.py.

Two scenarios:
1. **Fixture PDF** — a tiny bearing-spec PDF generated at test time with reportlab
   (if present) or a pre-committed one in corpora/reveng/.  Asserts that OD/ID/width
   values appear in an extracted dimension table.

2. **No-backend envelope** — monkeypatches both ``docling`` and ``pdfplumber`` out of
   sys.modules so the graceful envelope path is exercised.  Test must PASS whether or
   not the real backends are installed.
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Fixture: tiny bearing-spec PDF
# ---------------------------------------------------------------------------

FIXTURE_PDF = Path(__file__).parent.parent.parent / "corpora" / "reveng" / "bearing_spec.pdf"

# Table values baked into the generated/committed fixture
BEARING_OD_MM = 52.0
BEARING_ID_MM = 25.0
BEARING_WIDTH_MM = 15.0
BEARING_PN = "6205-2RS"


def _make_bearing_pdf(dest: Path) -> None:
    """Generate a minimal bearing-spec PDF with reportlab."""
    from reportlab.lib.pagesizes import A4  # type: ignore[import]
    from reportlab.platypus import SimpleDocTemplate, Table, Paragraph  # type: ignore[import]
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import]

    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(dest), pagesize=A4)
    styles = getSampleStyleSheet()

    table_data = [
        ["Part No.", "OD (mm)", "ID (mm)", "Width (mm)", "Mass (g)"],
        [BEARING_PN, str(BEARING_OD_MM), str(BEARING_ID_MM), str(BEARING_WIDTH_MM), "91"],
        ["6206-2RS", "62.0", "30.0", "16.0", "143"],
    ]
    elements = [
        Paragraph("Deep Groove Ball Bearings — Dimensional Data", styles["Heading1"]),
        Paragraph(
            f"Part number {BEARING_PN} is a standard 6205 series bearing. "
            "Manufactured per ISO 15:2017. SKF6205-2RS NSK6205.",
            styles["Normal"],
        ),
        Table(table_data, hAlign="LEFT"),
    ]
    doc.build(elements)


def _ensure_fixture() -> Path:
    """Return the fixture PDF path, generating it if missing."""
    if not FIXTURE_PDF.exists():
        try:
            _make_bearing_pdf(FIXTURE_PDF)
        except ImportError:
            pytest.skip("reportlab not installed and no committed fixture PDF")
    return FIXTURE_PDF


# ---------------------------------------------------------------------------
# Helper: run ingest and reload module to pick up fresh imports
# ---------------------------------------------------------------------------

def _ingest(pdf: Path, session_root: Path) -> dict:
    # Reload so monkeypatching takes effect across test cases cleanly
    from ..vision import datasheet_ingest as mod
    importlib.reload(mod)
    return mod.ingest_datasheet(pdf, session_root)


# ---------------------------------------------------------------------------
# Test 1: real extraction with available backend
# ---------------------------------------------------------------------------

class TestBearingExtraction:
    def test_fixture_pdf_exists_or_generated(self) -> None:
        pdf = _ensure_fixture()
        assert pdf.exists(), "Bearing fixture PDF should exist after _ensure_fixture()"

    def test_extraction_ok(self) -> None:
        pdf = _ensure_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ingest(pdf, Path(tmpdir))

        assert result.get("ok") is True, (
            f"Expected ok=true, got: {result.get('reason')}"
        )

    def test_dimension_table_found(self) -> None:
        """At least one table must be tagged is_dimension_table=true."""
        pdf = _ensure_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ingest(pdf, Path(tmpdir))

        if not result.get("ok"):
            pytest.skip(f"Extraction failed ({result.get('reason')}), skipping dim-table check")

        dim_tables = [t for t in result.get("tables", []) if t.get("is_dimension_table")]
        assert dim_tables, "Expected at least one dimension table tagged is_dimension_table=true"

    def test_od_id_width_values_present(self) -> None:
        """The extracted dimension table must contain OD/ID/width values."""
        pdf = _ensure_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ingest(pdf, Path(tmpdir))

        if not result.get("ok"):
            pytest.skip(f"Extraction failed ({result.get('reason')}), skipping value check")

        # Flatten all cell text from all tables
        all_cells: list[str] = []
        for table in result.get("tables", []):
            all_cells.extend(table.get("headers", []))
            for row in table.get("rows", []):
                all_cells.extend(row)

        flat = " ".join(all_cells)

        assert str(BEARING_OD_MM) in flat or str(int(BEARING_OD_MM)) in flat, (
            f"OD={BEARING_OD_MM} not found in extracted cells: {flat[:200]}"
        )
        assert str(BEARING_ID_MM) in flat or str(int(BEARING_ID_MM)) in flat, (
            f"ID={BEARING_ID_MM} not found in extracted cells: {flat[:200]}"
        )
        assert str(BEARING_WIDTH_MM) in flat or str(int(BEARING_WIDTH_MM)) in flat, (
            f"Width={BEARING_WIDTH_MM} not found in extracted cells: {flat[:200]}"
        )

    def test_mpn_candidate_extracted(self) -> None:
        """The bearing PN should appear as an MPN candidate."""
        pdf = _ensure_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ingest(pdf, Path(tmpdir))

        if not result.get("ok"):
            pytest.skip(f"Extraction failed ({result.get('reason')}), skipping MPN check")

        candidates = result.get("mpn_candidates", [])
        # normalize_mpn strips -2RS suffix on some patterns, so check both
        stripped = BEARING_PN.split("-")[0]
        found = any(BEARING_PN in c or stripped in c for c in candidates)
        assert found, f"Expected {BEARING_PN} (or {stripped}) in mpn_candidates: {candidates}"

    def test_datasheet_json_written(self) -> None:
        """datasheet/datasheet.json must be written to the session root."""
        pdf = _ensure_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _ingest(pdf, Path(tmpdir))
            json_path = Path(tmpdir) / "datasheet" / "datasheet.json"
            assert json_path.exists(), "datasheet/datasheet.json not written"
            loaded = json.loads(json_path.read_text())
        assert loaded.get("ok") == result.get("ok")


# ---------------------------------------------------------------------------
# Test 2: graceful degradation when no backend is available
# ---------------------------------------------------------------------------

class TestNoBackendEnvelope:
    def _hide_backends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remove docling and pdfplumber from sys.modules and import machinery."""
        blocked = {"docling", "pdfplumber"}
        for name in list(sys.modules.keys()):
            for b in blocked:
                if name == b or name.startswith(b + ".") or name.startswith(b + "_"):
                    monkeypatch.delitem(sys.modules, name, raising=False)

        # Make future imports raise ImportError
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

        def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
            top = name.split(".")[0]
            if top in blocked:
                raise ImportError(f"Blocked for test: {name}")
            return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

        import builtins
        monkeypatch.setattr(builtins, "__import__", _blocking_import)

    def test_envelope_when_no_backend(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._hide_backends(monkeypatch)

        pdf = _ensure_fixture()

        from ..vision import datasheet_ingest as mod
        importlib.reload(mod)
        result = mod.ingest_datasheet(pdf, tmp_path)

        assert result.get("ok") is False
        assert result.get("reason") == "backend_not_provisioned"
        assert "hint" in result
        assert "docling" in result["hint"].lower()

    def test_envelope_json_written_when_no_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._hide_backends(monkeypatch)

        pdf = _ensure_fixture()

        from ..vision import datasheet_ingest as mod
        importlib.reload(mod)
        mod.ingest_datasheet(pdf, tmp_path)

        json_path = tmp_path / "datasheet" / "datasheet.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data.get("ok") is False
        assert data.get("reason") == "backend_not_provisioned"

    def test_missing_pdf_envelope(self, tmp_path: Path) -> None:
        """Non-existent PDF should return ok=false with reason=pdf_not_found."""
        from ..vision import datasheet_ingest as mod
        importlib.reload(mod)
        result = mod.ingest_datasheet(tmp_path / "nonexistent.pdf", tmp_path)
        assert result.get("ok") is False
        assert result.get("reason") == "pdf_not_found"

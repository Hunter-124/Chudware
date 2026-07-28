"""MCP tools: office_* — read + edit .pptx/.xlsx/.xlsm/.csv/.pdf/.doc/.docx (T-office).

Reuses the already-pinned deliverables-pack libraries (python-pptx, openpyxl, python-docx,
pypdfium2 — see requirements.txt "deliverables pack" block) against arbitrary EXISTING files,
not just fresh templates, plus pypdf (BSD-3, new dep) for PDF page-level edits pypdfium2 can't
do, and the LibreOffice-headless subprocess bridge already wired for
``services/deliverables/engines.py::office_to_pdf`` for legacy .doc round-trips.

Read path prefers ``office_oxide`` (https://github.com/yfedoseev/office_oxide — MIT/Apache-2.0,
pip ``office-oxide``) for fast native text extraction when installed; falls back to the Python
libs above. No new in-process parser is written for any format.

Envelope: ``{ok:true, …}`` | ``{ok:false, reason, hint}`` — never a traceback (mirrors
plm_tools.py's ``_err``/``_ok``). Every tool lazily imports its backing library per call so the
MCP server boots even when a library is momentarily absent; missing libraries degrade to
``reason:"engine_missing"``, a missing ``soffice``/``libreoffice`` binary degrades to
``reason:"converter_missing"`` (same code deliverables/engines.py uses), and PDF ops that would
require real text-content reflow degrade to ``reason:"unsupported_op"``.

FROZEN contract (contracts/office.md) — ``office_extract`` exists BOTH as an MCP tool below
AND as the ``office_extract`` worker RPC (``services/agent/worker_rpc.py``) the host's AI-tab
``docs_panel`` calls via ``st.rpc.call``. Both call the SAME module-level :func:`extract_office`
so the read logic lives exactly once.

Registration: ``register_office_tools(mcp)`` → adds office_extract, office_read_pptx,
office_edit_pptx, office_read_xlsx, office_edit_xlsx, office_read_csv, office_edit_csv,
office_read_pdf, office_edit_pdf, office_read_docx, office_edit_docx, office_read_doc,
office_edit_doc.

Write limits (see contracts/office.md for the full list): .doc edits round-trip lossily through
LibreOffice; .pptx edits are shape/text/table content only (no layout/theme/animation); .pdf
edits are page-level only (rotate/delete/merge/metadata) — no text reflow; .xlsm preserves the
VBA project as an opaque blob (macro *code* is never read or edited, only cell/sheet data).
"""
from __future__ import annotations

import csv
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _err(reason: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "reason": reason, "hint": hint})


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, indent=2, sort_keys=True)


def _to_envelope(result: dict) -> str:
    """Turn a plain {ok,...} dict (as returned by the shared extraction/helper functions)
    into the standard §0 JSON envelope string."""
    if result.get("ok"):
        return _ok(**{k: v for k, v in result.items() if k != "ok"})
    return _err(result.get("reason", "error"), result.get("hint", ""))


def _kind_for(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


# --------------------------------------------------------------------------- pptx

def _pptx_module():
    from pptx import Presentation  # noqa: PLC0415

    return Presentation


def _extract_pptx(path: str) -> dict:
    try:
        Presentation = _pptx_module()
    except ImportError:
        return {"ok": False, "reason": "engine_missing", "hint": "python-pptx is not installed"}
    try:
        prs = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "open_failed", "hint": str(exc)}
    lines: list[str] = []
    for i, slide in enumerate(prs.slides):
        lines.append(f"# Slide {i + 1}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"(notes: {notes})")
    return {"ok": True, "kind": "pptx", "text": "\n".join(lines)}


# --------------------------------------------------------------------------- xlsx / xlsm

def _openpyxl_module():
    import openpyxl  # noqa: PLC0415

    return openpyxl


def _jsonable_cell(value: Any) -> Any:
    """openpyxl hands back datetime/Decimal/etc for typed cells — JSON only accepts the
    primitives, so anything else is stringified."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _extract_xlsx(path: str, keep_vba: bool) -> dict:
    try:
        openpyxl = _openpyxl_module()
    except ImportError:
        return {"ok": False, "reason": "engine_missing", "hint": "openpyxl is not installed"}
    try:
        wb = openpyxl.load_workbook(path, data_only=True, keep_vba=keep_vba)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "open_failed", "hint": str(exc)}
    sheets = []
    lines: list[str] = []
    for ws in wb.worksheets:
        rows = [[_jsonable_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        sheets.append({"name": ws.title, "rows": rows})
        lines.append(f"# {ws.title}")
        for row in rows:
            lines.append(", ".join("" if v is None else str(v) for v in row))
    return {"ok": True, "kind": "xlsm" if keep_vba else "xlsx", "text": "\n".join(lines),
            "sheets": sheets}


# --------------------------------------------------------------------------- csv

def _extract_csv(path: str) -> dict:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "open_failed", "hint": str(exc)}
    return {"ok": True, "kind": "csv", "text": "\n".join(", ".join(row) for row in rows)}


# --------------------------------------------------------------------------- pdf

def _pdf_error(exc: Exception) -> dict:
    """Classify a PDF open failure: password-protected files get their own reason with an
    actionable hint instead of a generic open_failed."""
    if "password" in str(exc).lower():
        return {"ok": False, "reason": "password_protected",
                 "hint": "PDF is password-protected — decrypt it first "
                         "(e.g. qpdf --decrypt in.pdf out.pdf), then read the output"}
    return {"ok": False, "reason": "open_failed", "hint": str(exc)}


def _parse_page_range(spec: str, count: int) -> list[int]:
    """Parse a 1-based page-range spec ("1-3,5") into a 0-based page-index list.
    Empty spec = every page."""
    if not spec:
        return list(range(count))
    idxs: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            idxs.extend(range(int(a) - 1, int(b)))
        else:
            idxs.append(int(part) - 1)
    return [i for i in idxs if 0 <= i < count]


def _extract_pdf(path: str) -> dict:
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError:
        return {"ok": False, "reason": "engine_missing", "hint": "pypdfium2 is not installed"}
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        return _pdf_error(exc)
    try:
        lines: list[str] = []
        for i in range(len(doc)):
            lines.append(f"# Page {i + 1}")
            lines.append(doc[i].get_textpage().get_text_range())
    finally:
        doc.close()
    return {"ok": True, "kind": "pdf", "text": "\n".join(lines)}


# --------------------------------------------------------------------------- docx

def _docx_module():
    import docx  # noqa: PLC0415

    return docx


def _read_docx_structured(path: str) -> dict:
    try:
        docx = _docx_module()
    except ImportError:
        return {"ok": False, "reason": "engine_missing", "hint": "python-docx is not installed"}
    try:
        doc = docx.Document(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "open_failed", "hint": str(exc)}
    paragraphs = [p.text for p in doc.paragraphs]
    tables = [[[cell.text for cell in row.cells] for row in t.rows] for t in doc.tables]
    return {"ok": True, "paragraphs": paragraphs, "tables": tables}


def _extract_docx(path: str) -> dict:
    structured = _read_docx_structured(path)
    if not structured.get("ok"):
        return structured
    lines = list(structured["paragraphs"])
    for table in structured["tables"]:
        for row in table:
            lines.append(" | ".join(row))
    return {"ok": True, "kind": "docx", "text": "\n".join(lines)}


def _apply_docx_ops(doc, ops: list) -> int | dict:
    """Apply office_edit_docx-style ops to an open python-docx Document in place.
    Returns the number of paragraphs changed, or an {ok:false,...} error dict."""
    changed = 0
    for op in ops or []:
        op = op or {}
        if op.get("op") == "append_paragraph":
            doc.add_paragraph(op.get("text", ""))
            changed += 1
            continue
        idx = op.get("paragraph_index")
        text = op.get("text")
        if idx is None or text is None:
            return {"ok": False, "reason": "invalid_op",
                     "hint": "each op needs paragraph_index + text, or op:\"append_paragraph\""}
        if idx < 0 or idx >= len(doc.paragraphs):
            return {"ok": False, "reason": "invalid_op", "hint": f"paragraph_index {idx} out of range"}
        para = doc.paragraphs[idx]
        if para.runs:
            para.runs[0].text = text
            for run in para.runs[1:]:
                run.text = ""
        else:
            para.add_run(text)
        changed += 1
    return changed


# --------------------------------------------------------------------------- doc (legacy, soffice bridge)

def _soffice_bin_or_err():
    """(binary, None) or (None, error-dict) — the same soffice/libreoffice discovery
    deliverables/engines.py uses (subprocess-only, PATH-discovered, never bundled)."""
    try:
        from services.deliverables.engines import soffice_bin  # noqa: PLC0415
    except ImportError:
        return None, {"ok": False, "reason": "engine_missing",
                       "hint": "services.deliverables engine module is unavailable"}
    binary = soffice_bin()
    if not binary:
        return None, {"ok": False, "reason": "converter_missing",
                       "hint": "install LibreOffice (soffice) — legacy .doc needs headless conversion"}
    return binary, None


def _soffice_convert(binary: str, src: Path, outdir: Path, target: str) -> Path | None:
    """Convert *src* to *target* (a bare extension: "docx"/"doc") via LibreOffice headless.
    Mirrors deliverables/engines.py::office_to_pdf's subprocess shape exactly."""
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [binary, "--headless", "--convert-to", target, "--outdir", str(outdir), str(src)],
        capture_output=True, text=True, timeout=120,
    )
    out = outdir / (src.stem + "." + target)
    if proc.returncode != 0 or not out.exists():
        return None
    return out


def _extract_doc(path: str) -> dict:
    binary, err = _soffice_bin_or_err()
    if err:
        return err
    with tempfile.TemporaryDirectory() as tmp:
        converted = _soffice_convert(binary, Path(path), Path(tmp), "docx")
        if converted is None:
            return {"ok": False, "reason": "converter_missing",
                     "hint": f"libreoffice could not convert {Path(path).name} to docx"}
        result = _extract_docx(str(converted))
    if result.get("ok"):
        result = dict(result)
        result["kind"] = "doc"
    return result


# --------------------------------------------------------------------------- frozen shared extraction

_OXIDE_KINDS = frozenset({"docx", "doc", "pptx", "ppt", "xlsx", "xls", "xlsm"})


def _extract_via_office_oxide(path: str, kind: str) -> dict | None:
    """Prefer office_oxide (https://github.com/yfedoseev/office_oxide) for text extract.

    Returns a contract-shaped dict on success, or None to fall through to the
    python-docx/openpyxl/python-pptx path (missing wheel, unsupported kind, or
    extract failure). Spreadsheet ``sheets`` structured payload is left to the
    openpyxl path — oxide only supplies flattened ``text`` / markdown here.
    """
    if kind not in _OXIDE_KINDS:
        return None
    try:
        from office_oxide import Document, extract_text, to_markdown  # noqa: PLC0415
    except ImportError:
        return None
    try:
        text = ""
        # Prefer markdown when available (tables read better for the agent).
        try:
            text = to_markdown(path) or ""
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            try:
                text = extract_text(path) or ""
            except Exception:  # noqa: BLE001
                text = ""
        if not text.strip():
            with Document.open(path) as doc:
                text = (doc.to_markdown() if hasattr(doc, "to_markdown") else "") or ""
                if not text.strip():
                    text = (doc.plain_text() if hasattr(doc, "plain_text") else "") or ""
        if not text.strip():
            return None
        # Map legacy ppt/xls kinds onto the contract vocabulary the host expects.
        out_kind = kind
        if kind == "ppt":
            out_kind = "pptx"
        elif kind == "xls":
            out_kind = "xlsx"
        return {"ok": True, "kind": out_kind, "text": text, "engine": "office_oxide"}
    except Exception:  # noqa: BLE001 — never raise out of extract_office
        return None


def extract_office(path: str) -> dict:
    """Read-only text/structure extraction, dispatched by file extension. FROZEN contract
    (contracts/office.md): both the ``office_extract`` MCP tool and the ``office_extract``
    worker RPC (services/agent/worker_rpc.py) call this exact function, so the read logic
    lives exactly once.

    Prefers ``office_oxide`` (native) for Office formats when installed; falls back to
    python-pptx / openpyxl / python-docx / LibreOffice. Returns
    ``{ok, kind, text, sheets?}`` — ``kind`` in {pptx,xlsx,xlsm,csv,pdf,doc,docx};
    ``sheets`` present only for xlsx/xlsm (``[{name, rows:[[...]]}]``). Never raises —
    every failure is an ``{ok:false, reason, hint}`` dict.
    """
    kind = _kind_for(path)
    oxide = _extract_via_office_oxide(path, kind)
    # Spreadsheets still need the structured sheets payload for the host docs panel —
    # oxide text is a fine preview, but keep openpyxl for xlsx/xlsm when available.
    if oxide is not None and kind not in ("xlsx", "xlsm"):
        return oxide
    if kind == "pptx":
        return _extract_pptx(path)
    if kind in ("xlsx", "xlsm"):
        result = _extract_xlsx(path, keep_vba=(kind == "xlsm"))
        # Attach oxide text as a richer `text` when openpyxl succeeded but text is thin.
        if result.get("ok") and oxide is not None and oxide.get("text"):
            result = dict(result)
            result["text"] = oxide["text"]
            result["engine"] = "office_oxide+openpyxl"
        return result
    if kind == "csv":
        return _extract_csv(path)
    if kind == "pdf":
        return _extract_pdf(path)
    if kind == "doc":
        return _extract_doc(path)
    if kind == "docx":
        return _extract_docx(path)
    return {"ok": False, "reason": "unsupported_kind",
             "hint": f"office_extract does not support '.{kind}' — supported: "
                     "pptx, xlsx, xlsm, csv, pdf, doc, docx"}


# --------------------------------------------------------------------------- registration

def register_office_tools(mcp) -> None:
    """Register the office_* tool family onto *mcp* (contracts/office.md)."""

    @mcp.tool()
    def office_extract(path: str) -> str:
        """Read-only text/structure extraction, dispatched by file extension. ``kind`` in
        {pptx,xlsx,xlsm,csv,pdf,doc,docx}. Returns ``{ok, kind, text, sheets?}`` (``sheets``
        present only for xlsx/xlsm: ``[{name, rows:[[...]]}]``). This is the FROZEN contract
        also exposed as the ``office_extract`` worker RPC for the host docs_panel."""
        return _to_envelope(extract_office(path))

    @mcp.tool()
    def office_read_pptx(path: str) -> str:
        """Structured slide read. Returns ``{ok, slides:[{index, title, text_runs, notes}]}``."""
        try:
            Presentation = _pptx_module()
        except ImportError:
            return _err("engine_missing", "python-pptx is not installed")
        try:
            prs = Presentation(path)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        slides = []
        for i, slide in enumerate(prs.slides):
            title = slide.shapes.title.text if slide.shapes.title is not None else ""
            text_runs = [shape.text_frame.text for shape in slide.shapes
                         if shape.has_text_frame and shape.text_frame.text.strip()]
            notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
            slides.append({"index": i, "title": title, "text_runs": text_runs, "notes": notes})
        return _ok(slides=slides)

    @mcp.tool()
    def office_edit_pptx(path: str, ops: list[dict], out_path: str = "") -> str:
        """``ops``: ``[{slide, shape_index, text}]`` — in-place text-frame replacements
        (content only; no layout/theme/animation edits). Saves over *path*, or *out_path*
        if given. Returns ``{ok, path, slides_changed}``."""
        try:
            Presentation = _pptx_module()
        except ImportError:
            return _err("engine_missing", "python-pptx is not installed")
        try:
            prs = Presentation(path)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        changed = 0
        for op in ops or []:
            op = op or {}
            si, shi, text = op.get("slide"), op.get("shape_index"), op.get("text")
            if si is None or shi is None or text is None:
                return _err("invalid_op", "each op needs slide, shape_index, text")
            if si < 0 or si >= len(prs.slides):
                return _err("invalid_op", f"slide {si} out of range")
            shapes = list(prs.slides[si].shapes)
            if shi < 0 or shi >= len(shapes):
                return _err("invalid_op", f"shape_index {shi} out of range on slide {si}")
            shape = shapes[shi]
            if not shape.has_text_frame:
                return _err("invalid_op", f"shape {shi} on slide {si} has no text frame")
            shape.text_frame.text = text
            changed += 1
        dest = out_path or path
        try:
            prs.save(dest)
        except Exception as exc:  # noqa: BLE001
            return _err("save_failed", str(exc))
        return _ok(path=dest, slides_changed=changed)

    @mcp.tool()
    def office_read_xlsx(path: str, sheet: str = "") -> str:
        """Returns ``{ok, sheets:[{name, rows}]}``, or a single sheet if *sheet* is given.
        ``keep_vba`` is auto-detected from a ``.xlsm`` extension."""
        try:
            openpyxl = _openpyxl_module()
        except ImportError:
            return _err("engine_missing", "openpyxl is not installed")
        keep_vba = Path(path).suffix.lower() == ".xlsm"
        try:
            wb = openpyxl.load_workbook(path, data_only=True, keep_vba=keep_vba)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        if sheet:
            if sheet not in wb.sheetnames:
                return _err("invalid_sheet", f"no sheet named '{sheet}'")
            ws = wb[sheet]
            rows = [[_jsonable_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
            return _ok(sheets=[{"name": ws.title, "rows": rows}])
        sheets = [{"name": ws.title,
                   "rows": [[_jsonable_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]}
                  for ws in wb.worksheets]
        return _ok(sheets=sheets)

    @mcp.tool()
    def office_edit_xlsx(path: str, sheet: str, cells: dict[str, Any], out_path: str = "") -> str:
        """``cells``: ``{"B2": 214.8, "C3": "=SUM(B2:B5)"}`` — preserves other formulas
        (opened with ``data_only=False``). ``.xlsm`` auto keep_vba=True; the macro *code*
        stays an opaque blob (only cell/sheet data is editable). Returns
        ``{ok, path, cells_changed}``."""
        try:
            openpyxl = _openpyxl_module()
        except ImportError:
            return _err("engine_missing", "openpyxl is not installed")
        keep_vba = Path(path).suffix.lower() == ".xlsm"
        try:
            wb = openpyxl.load_workbook(path, data_only=False, keep_vba=keep_vba)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        if sheet not in wb.sheetnames:
            return _err("invalid_sheet", f"no sheet named '{sheet}'")
        ws = wb[sheet]
        for addr, value in (cells or {}).items():
            try:
                ws[addr] = value
            except Exception as exc:  # noqa: BLE001
                return _err("invalid_cell", f"{addr}: {exc}")
        dest = out_path or path
        try:
            wb.save(dest)
        except Exception as exc:  # noqa: BLE001
            return _err("save_failed", str(exc))
        fields = {"path": dest, "cells_changed": len(cells or {})}
        if keep_vba:
            fields["hint"] = "macro code in .xlsm is preserved as an opaque blob — only cell/sheet data was edited"
        return _ok(**fields)

    @mcp.tool()
    def office_read_csv(path: str, limit: int = 0) -> str:
        """Returns ``{ok, header, rows}`` (``rows`` capped to *limit* when > 0)."""
        try:
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        header = rows[0] if rows else []
        data_rows = rows[1:] if rows else []
        if limit and limit > 0:
            data_rows = data_rows[:limit]
        return _ok(header=header, rows=data_rows)

    @mcp.tool()
    def office_edit_csv(path: str, rows: list[list], header: list[str] | None = None,
                        out_path: str = "") -> str:
        """Full-file rewrite (csv has no partial-edit primitive). Returns
        ``{ok, path, row_count}``."""
        dest = out_path or path
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if header:
                    writer.writerow(header)
                for row in rows or []:
                    writer.writerow(row)
        except Exception as exc:  # noqa: BLE001
            return _err("write_failed", str(exc))
        return _ok(path=dest, row_count=len(rows or []))

    @mcp.tool()
    def office_read_pdf(path: str, page_range: str = "") -> str:
        """pypdfium2 text extraction. ``page_range`` is 1-based ("1-3,5"; empty = all pages).
        Returns ``{ok, pages:[{index, text}], page_count}`` (``index`` is 0-based)."""
        try:
            import pypdfium2 as pdfium  # noqa: PLC0415
        except ImportError:
            return _err("engine_missing", "pypdfium2 is not installed")
        try:
            doc = pdfium.PdfDocument(str(path))
        except Exception as exc:  # noqa: BLE001
            return _to_envelope(_pdf_error(exc))
        try:
            count = len(doc)
            idxs = _parse_page_range(page_range, count)
            pages = [{"index": i, "text": doc[i].get_textpage().get_text_range()} for i in idxs]
        finally:
            doc.close()
        return _ok(pages=pages, page_count=count)

    @mcp.tool()
    def office_edit_pdf(path: str, ops: list[dict], out_path: str = "") -> str:
        """pypdf PAGE-LEVEL ops only — ``{"op":"rotate","page":0,"degrees":90}``,
        ``{"op":"delete","page":2}``, ``{"op":"merge","with_path":"other.pdf"}``,
        ``{"op":"set_metadata","title":...}``. NOT text/content editing — anything requiring
        reflow returns ``reason:"unsupported_op"``. Returns ``{ok, path, pages}``."""
        try:
            import pypdf  # noqa: PLC0415
        except ImportError:
            return _err("engine_missing", "pypdf is not installed")
        try:
            reader = pypdf.PdfReader(path)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        writer = pypdf.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        for op in ops or []:
            op = op or {}
            kind = op.get("op")
            if kind == "rotate":
                idx = op.get("page")
                if idx is None or idx < 0 or idx >= len(writer.pages):
                    return _err("invalid_op", f"rotate: page {idx} out of range")
                writer.pages[idx].rotate(op.get("degrees", 90))
            elif kind == "delete":
                idx = op.get("page")
                if idx is None or idx < 0 or idx >= len(writer.pages):
                    return _err("invalid_op", f"delete: page {idx} out of range")
                writer.remove_page(idx)
            elif kind == "merge":
                with_path = op.get("with_path")
                if not with_path:
                    return _err("invalid_op", "merge: with_path required")
                try:
                    other = pypdf.PdfReader(with_path)
                except Exception as exc:  # noqa: BLE001
                    return _err("open_failed", str(exc))
                for page in other.pages:
                    writer.add_page(page)
            elif kind == "set_metadata":
                writer.add_metadata({f"/{k.capitalize()}": v for k, v in op.items() if k != "op"})
            elif kind in ("edit_text", "replace_text", "reflow"):
                return _err("unsupported_op",
                            "PDF text content cannot be edited directly — extract to docx, "
                            "edit, then re-export to PDF via render_document")
            else:
                # class-3 CONTENTLESS ERROR: this used to name neither the offending
                # value nor the accepted vocabulary — "unknown op 'x'" and nothing
                # else. The vocabulary here is closed and small; say it.
                return _err("invalid_op",
                            f"unknown op '{kind}'. PDF ops: rotate, delete, merge, "
                            f"set_metadata (page-level only — edit_text/replace_text/"
                            f"reflow are recognized but unsupported, see reason "
                            f"'unsupported_op' above)")
        dest = out_path or path
        try:
            with open(dest, "wb") as f:
                writer.write(f)
        except Exception as exc:  # noqa: BLE001
            return _err("save_failed", str(exc))
        return _ok(path=dest, pages=len(writer.pages))

    @mcp.tool()
    def office_read_docx(path: str) -> str:
        """Read a .docx file at *path*; returns ``{ok, paragraphs, tables}``."""
        return _to_envelope(_read_docx_structured(path))

    @mcp.tool()
    def office_edit_docx(path: str, ops: list[dict], out_path: str = "") -> str:
        """``ops``: ``[{paragraph_index, text}]`` or ``[{op:"append_paragraph", text}]``.
        Returns ``{ok, path, paragraphs_changed}``."""
        try:
            docx = _docx_module()
        except ImportError:
            return _err("engine_missing", "python-docx is not installed")
        try:
            doc = docx.Document(path)
        except Exception as exc:  # noqa: BLE001
            return _err("open_failed", str(exc))
        changed = _apply_docx_ops(doc, ops)
        if isinstance(changed, dict):
            return _to_envelope(changed)
        dest = out_path or path
        try:
            doc.save(dest)
        except Exception as exc:  # noqa: BLE001
            return _err("save_failed", str(exc))
        return _ok(path=dest, paragraphs_changed=changed)

    @mcp.tool()
    def office_read_doc(path: str) -> str:
        """Legacy .doc: ``soffice --headless --convert-to docx`` to a tmp dir, then read the
        result the same way ``office_read_docx`` does. ``reason:"converter_missing"`` if
        soffice/libreoffice is absent from PATH. Returns ``{ok, paragraphs, tables}``."""
        binary, err = _soffice_bin_or_err()
        if err:
            return _to_envelope(err)
        with tempfile.TemporaryDirectory() as tmp:
            converted = _soffice_convert(binary, Path(path), Path(tmp), "docx")
            if converted is None:
                return _err("converter_missing", f"libreoffice could not convert {Path(path).name} to docx")
            return _to_envelope(_read_docx_structured(str(converted)))

    @mcp.tool()
    def office_edit_doc(path: str, ops: list[dict], out_path: str = "") -> str:
        """Lossy round-trip: soffice .doc→.docx, apply ``office_edit_docx``-style *ops*,
        soffice .docx→.doc back over *out_path* (default: the original *path*). Legacy
        .doc-only features may not survive an OOXML round-trip — this is inherent to the
        format. ``reason:"converter_missing"`` if soffice/libreoffice is absent from PATH.
        Returns ``{ok, path, paragraphs_changed, hint}``."""
        binary, err = _soffice_bin_or_err()
        if err:
            return _to_envelope(err)
        try:
            docx = _docx_module()
        except ImportError:
            return _err("engine_missing", "python-docx is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            converted = _soffice_convert(binary, Path(path), outdir, "docx")
            if converted is None:
                return _err("converter_missing", f"libreoffice could not convert {Path(path).name} to docx")
            try:
                doc = docx.Document(str(converted))
            except Exception as exc:  # noqa: BLE001
                return _err("open_failed", str(exc))
            changed = _apply_docx_ops(doc, ops)
            if isinstance(changed, dict):
                return _to_envelope(changed)
            try:
                doc.save(str(converted))
            except Exception as exc:  # noqa: BLE001
                return _err("save_failed", str(exc))
            back = _soffice_convert(binary, converted, outdir, "doc")
            if back is None:
                return _err("converter_missing", "libreoffice could not convert back to .doc")
            dest = Path(out_path or path)
            dest.write_bytes(back.read_bytes())
        return _ok(path=str(dest), paragraphs_changed=changed,
                   hint="lossy round-trip through LibreOffice — legacy .doc-only features may not survive")

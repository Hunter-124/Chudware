"""The five tool implementations (contract §1, §5) — plain functions returning MCP envelopes.

``services/mcp/server.py`` wraps each of these as a FastMCP ``@mcp.tool()``. Every function
returns the flat envelope: the §3 record (or a preview / list payload) on success, and a
``{ok:false, reason, hint}`` shape on failure — never a traceback, never a partial file with a
hole (a :class:`DeliverableError` raised anywhere below is caught and turned into the envelope).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import engines, project, registry
from .errors import DeliverableError, fail
from .spec import validate_and_resolve
from .templates import get_template

_PRESENTATION_FORMATS = ("pptx",)
_REPORT_FORMATS = ("docx", "pdf")
_WORKBOOK_FORMATS = ("xlsx",)


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w]+", "-", str(text).strip().lower()).strip("-")
    return s or "artifact"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 1000):
        cand = path.with_name(f"{stem}-{n}{suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}-{Path(path).name}{suffix}")


def _make(spec, expected_kind: str, default_format: str, allowed: tuple, toolname: str) -> dict:
    try:
        if not isinstance(spec, dict):
            raise DeliverableError("invalid_spec", "spec must be a JSON object")
        template_name = spec.get("template")
        if not isinstance(template_name, str) or not template_name:
            raise DeliverableError("invalid_spec", "spec missing 'template'")

        fmt = spec.get("format") or default_format
        if fmt not in allowed:
            raise DeliverableError(
                "unsupported_format", f"{toolname} supports {', '.join(allowed)}")

        template = get_template(template_name, expected_kind)

        source_refs: list = []
        resolved = validate_and_resolve(spec, expected_kind, template, source_refs)

        run_id = resolved.get("run_id") or registry.new_run_id()
        artifact_id = registry.new_artifact_id(expected_kind)
        adir = project.artifacts_dir()
        out_abs = _unique_path(adir / f"{_slugify(resolved.get('title') or expected_kind)}.{fmt}")

        # Build the artifact (validation already passed — no hole can reach the file).
        template.build(resolved, fmt, out_abs)

        # Persist the verbatim spec for reproducibility (§3 spec_ref).
        spec_abs = adir / f"{artifact_id}.spec.json"
        spec_abs.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")

        record = registry.build_record(
            artifact_id, expected_kind, out_abs, run_id, source_refs, spec_abs)
        registry.append_record(record)
        return record
    except DeliverableError as e:
        return e.envelope()
    except Exception as e:  # noqa: BLE001 - engine faults must still be contract-shaped
        return fail("invalid_spec", f"{toolname} failed: {type(e).__name__}: {e}")


def make_presentation(spec) -> dict:
    """Build a ``.pptx`` deck from a spec (§2.1). Returns the §3 artifact record."""
    return _make(spec, "presentation", "pptx", _PRESENTATION_FORMATS, "make_presentation")


def make_report(spec) -> dict:
    """Build a ``.docx``/``.pdf`` report from a spec (§2.2). Returns the §3 artifact record."""
    return _make(spec, "report", "docx", _REPORT_FORMATS, "make_report")


def make_workbook(spec) -> dict:
    """Build a ``.xlsx`` workbook with live formulas from a spec (§2.3). Returns the §3 record."""
    return _make(spec, "workbook", "xlsx", _WORKBOOK_FORMATS, "make_workbook")


def render_document(path) -> dict:
    """Render a supported file to downscaled preview PNGs + a ``resource_link`` (contract §5.4).

    Office formats → LibreOffice-headless PDF (subprocess) → per-page PNG via pypdfium2; native
    in-process paths for pdf/image/csv/md; best-effort svg. Missing LibreOffice → the artifact is
    untouched, only the preview degrades to ``converter_missing``.
    """
    try:
        if not isinstance(path, str) or not path:
            return fail("artifact_not_found", "no path given")
        abs_path = project.resolve_project_path(path)
        if not abs_path.exists() or not abs_path.is_file():
            return fail("artifact_not_found", f"no file at {path}")

        ext = abs_path.suffix.lower().lstrip(".")
        cls = engines.classify_ext(ext)
        if cls == "unsupported":
            return fail("unsupported_format", f"cannot preview .{ext}")

        dest = project.previews_dir()
        stem = f"{abs_path.stem}_{abs(hash(str(abs_path.resolve()))) & 0xFFFFFF:06x}"

        if cls == "pdf":
            pages = engines.pdf_to_pngs(abs_path, dest, stem)
        elif cls == "image":
            pages = engines.image_to_png(abs_path, dest, stem)
        elif cls == "text":
            pages = engines.text_to_png(abs_path, dest, stem)
        elif cls == "svg":
            pages = engines.svg_to_pngs(abs_path, dest, stem)
        else:  # office
            pdf = engines.office_to_pdf(abs_path, dest)
            pages = engines.pdf_to_pngs(pdf, dest, stem)

        out_pages = [
            {"index": p["index"],
             "image": {"path": project.to_project_rel(p["_abs"]), **p["image"]}}
            for p in pages
        ]
        return {
            "ok": True,
            "kind": ext,
            "page_count": len(out_pages),
            "pages": out_pages,
            "resource_link": abs_path.resolve().as_uri(),
        }
    except DeliverableError as e:
        return e.envelope()
    except Exception as e:  # noqa: BLE001
        return fail("artifact_not_found", f"render failed: {type(e).__name__}: {e}")


def list_artifacts(run_id=None) -> dict:
    """Read the artifact registry (§5.5), newest-first; optional ``run_id`` filter. Never errors."""
    records = registry.read_records(run_id)
    return {"ok": True, "count": len(records), "artifacts": records}

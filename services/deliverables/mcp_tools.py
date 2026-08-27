"""FastMCP registration for the deliverables tool family (contract §1).

``services/mcp/server.py`` calls :func:`register_deliverables_tools`.  The tools are thin
wrappers over :mod:`deliverables.api`; each returns the flat MCP envelope (``{ok:...}``)
already, so there is no per-call try/except here.
"""
from __future__ import annotations

from typing import Any

from . import api


def register_deliverables_tools(mcp) -> None:
    """Attach office artifact creation, preview, and registry tools."""

    @mcp.tool()
    def make_presentation(spec: dict) -> dict:
        """Build a slide deck (.pptx) from a JSON spec and return its artifact record.

        The spec (see cad_guide('deliverables')) carries the model-authored outline + prose and an
        `attached_results` evidence bag; every load-bearing number is a slot that references a tool
        result — a bare numeral in a load-bearing slot is a validation error, not a value. Returns
        {artifact_id, kind, path, created_ts, run_id, source_refs[], spec_ref} on success, or
        {ok:false, reason, hint} on failure.
        """
        return api.make_presentation(spec)

    @mcp.tool()
    def make_report(spec: dict) -> dict:
        """Build a paginated report (.docx or .pdf) from a JSON spec; returns its artifact record.

        Same spec envelope as make_presentation. format ∈ {docx, pdf} (default docx). Load-bearing
        slots must reference attached tool results; provenance is enforced before any file is
        written. See cad_guide('deliverables').
        """
        return api.make_report(spec)

    @mcp.tool()
    def make_work_instruction(spec: dict) -> dict:
        """Build a controlled machining work instruction (.docx or .pdf).

        The spec kind and template are both `work_instruction`. Ordered operation blocks carry
        setup datum/WCS, fixturing, machine/tool, provenance-backed process parameters, and
        in-process inspection. A machined operation missing setup or inspection is rejected before
        any artifact is written. Returns the same typed artifact/failure envelope as make_report.
        """
        return api.make_work_instruction(spec)

    @mcp.tool()
    def make_weldment_traveler(spec: dict) -> dict:
        """Build a controlled weldment traveler (.docx or .pdf).

        The spec kind and template are both `weldment_traveler`. The outline carries cut and joint
        lists, provenance-backed WPS fields, fit-up/distortion controls, visual/NDT inspection, and
        signoff. Returns the same typed artifact/failure envelope as make_report.
        """
        return api.make_weldment_traveler(spec)

    @mcp.tool()
    def make_workbook(spec: dict) -> dict:
        """Build a spreadsheet (.xlsx) with live formulas from a JSON spec; returns its record.

        Sheet blocks carry `rows` of cells: free-prose labels, provenance-resolved value cells, or
        live formula strings like "=SUM(B2:B5)" written as real formulas that recompute on open.
        See cad_guide('deliverables').
        """
        return api.make_workbook(spec)

    @mcp.tool()
    def render_document(path: str) -> dict:
        """Render a supported file to downscaled preview PNGs (≤1024 px, <1 MB) + a resource_link.

        Office formats (pptx/docx/xlsx/…) preview via LibreOffice headless (subprocess) → pypdfium2;
        pdf/png/csv/md preview in-process with no subprocess. If LibreOffice is not installed the
        artifact itself is untouched and only the preview degrades: {ok:false,
        reason:"converter_missing", hint:"install libreoffice"}.
        """
        return api.render_document(path)

    @mcp.tool()
    def list_artifacts(run_id: str | None = None) -> dict:
        """Read the artifact registry (newest first); optional run_id filters to one agent run.

        Returns {ok:true, count, artifacts:[<record>, …]}. An unknown run_id returns count:0 with an
        empty list — not an error.
        """
        return api.list_artifacts(run_id)

    # Silence "assigned but never used" — the decorator registers them by side effect.
    _ = (
        make_presentation,
        make_report,
        make_work_instruction,
        make_weldment_traveler,
        make_workbook,
        render_document,
        list_artifacts,
    )

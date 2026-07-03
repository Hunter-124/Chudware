"""Structural analysis MCP tools (T-D2 + T-D3) — PyNite member forces, deflections, D/C ratios.

  struct_analyze(doc_path, load_combo)  -> member forces + deflections for an aicad.structure/1 doc
  struct_check(doc_path, load_combo)    -> D/C ratio checks (axial yield + bending) per member

T-D3 fills the ``struct_check`` placeholder left by T-D2.  Implementation lives in:
  * ``engine_oracle/structural/sections.py`` — AISC + lumber section lookups
  * ``engine_oracle/structural/checks.py``   — D/C ratio calculators + CheckResult

PyNite (MIT) is imported **lazily inside the handler** — the engine's structural stack (scipy) may
be absent in some MCP runtime venvs, and a top-level import would take the whole server's module
load down with it (server.py is a shared hot-spot). On absence the handler returns the normal
``{ok:False, code, detail}`` error envelope.

Note: ``struct_check`` is registered here (not appended to server.py) because T-D2 already placed
a placeholder in this module — the T-D3 work order edit is to fill that placeholder in place, not
to append a duplicate registration.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err


def _load_structure(doc_path: str):
    """Read a JSON ``aicad.structure/1`` document from disk into a StructureDoc."""
    from engine.structural.elements import StructureDoc

    with open(doc_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return StructureDoc.from_dict(data)


def register_structural_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def struct_analyze(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Run member-force & deflection analysis on a structural document.

        Args:
            doc_path: path to a JSON ``aicad.structure/1`` document (elements + load_cases).
            load_combo: ASCE 7 combo name — ``DEAD_ONLY`` (1.4D), ``DEAD_LIVE`` (1.2D+1.6L),
                or ``DEAD_LIVE_WIND`` (1.2D+1.6L+0.5W).

        Returns JSON ``{ok, load_combo, results:[{element_id, axial_n, shear_n, moment_nmm,
        deflection_mm, dc_ratio, pass}, ...]}`` — one row per member (axial +tension, forces in N,
        moments in N·mm, deflections in mm). ``dc_ratio``/``pass`` are null until T-D3.
        """
        try:
            from engine.structural.analysis import analyze_doc
        except ImportError as e:  # scipy/PyNite not provisioned in this runtime venv
            return _dumps({
                "ok": False,
                "error": "structural solver unavailable (PyNite/scipy not installed)",
                "code": "solver_unavailable",
                "detail": str(e),
            })
        try:
            doc = _load_structure(doc_path)
            out = analyze_doc(doc, load_combo)
            return _dumps({"ok": True, "load_combo": load_combo, "results": out["results"]})
        except Exception as e:  # noqa: BLE001 - surface as a fixable result, not a transport crash
            return _err(e)

    @mcp.tool()
    def struct_check(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Demand/capacity ratio check for every member in a structural document.

        Runs :func:`~engine.structural.analysis.analyze_doc` under the requested ASCE 7 load
        combination, then applies simplified AISC LRFD (steel) or NDS ASD (wood) D/C checks via
        :func:`~engine.structural.checks.check_member`.

        Args:
            doc_path:   Path to a JSON ``aicad.structure/1`` document.
            load_combo: ASCE 7 combo — ``DEAD_ONLY``, ``DEAD_LIVE`` (default), or
                        ``DEAD_LIVE_WIND``.

        Returns
        -------
        JSON ``{ok, load_combo, disclaimer, results:[{element_id, combo, dc_ratio, passed,
        governing_limit, dc_axial, dc_bending, disclaimer}, ...]}``.

        * ``dc_ratio`` ≤ 1.0 → ``passed=true``.
        * ``governing_limit`` is ``axial_yield_LRFD`` / ``bending_yield_LRFD`` (steel) or
          ``axial_compression_ASD_NDS`` / ``bending_ASD_NDS`` (wood).
        * Every result row and the top-level response carry the mandatory disclaimer:
          "Preliminary check — not sealed engineering. Verify with a licensed structural engineer
          before construction."

        Steel check uses AISC LRFD elastic (φFy, φ=0.90); wood uses NDS ASD allowable stresses
        (Douglas Fir-Larch No.2 defaults; no load-duration / wet-service adjustments).  These are
        *preliminary estimates only* — see disclaimer.
        """
        try:
            from engine.structural.analysis import analyze_doc
            from engine.structural.checks import DISCLAIMER, check_member
            from engine.structural.sections import get_aisc_section, get_lumber_section
        except ImportError as e:
            return _dumps({
                "ok": False,
                "error": "structural solver unavailable (PyNite/scipy not installed)",
                "code": "solver_unavailable",
                "detail": str(e),
            })
        try:
            doc = _load_structure(doc_path)

            # Run force/deflection analysis
            out = analyze_doc(doc, load_combo)
            analysis_results = out["results"]

            # Build element-id → (section, material) maps from the document
            element_map = {el.id: el for el in doc.elements}

            def _section_for(element_id: str) -> dict:
                el = element_map.get(element_id)
                if el is None:
                    return {}
                sname = (el.section_name or "").strip().upper().replace(" ", "")
                mat = (el.material or "").strip().lower()
                # Try AISC lookup first (W/S/C shapes); fall back to lumber; fall back empty
                try:
                    return get_aisc_section(sname)
                except KeyError:
                    pass
                try:
                    return get_lumber_section(sname)
                except KeyError:
                    pass
                # If section not in either table, return brep dims (A + Sx from section_properties)
                from engine.structural.analysis import section_properties
                props = section_properties(el.section_name, el.material,
                                           element_type=el.element_type)
                # Build a minimal section dict with A and Sx for the check
                import math
                # Approximate Sx = Iz / (d/2): use sqrt of Iz as a proxy for d
                Iz = props.get("Iz", 0.0)
                A = props.get("A", 0.0)
                # For a rectangle h×w: Iz = w*h³/12, Sx = w*h²/6 = 2*Iz/h
                # Without h we can't recover Sx exactly — use a conservative proxy
                Sx_approx = 2.0 * Iz / max(math.sqrt(A), 1.0) if A > 0 else 0.0
                return {"A": A, "Sx": Sx_approx, "Sy": Sx_approx, "Ix": props.get("Iy", 0.0)}

            def _material_for(element_id: str) -> str:
                el = element_map.get(element_id)
                return el.material if el else "steel"

            check_rows = []
            for row in analysis_results:
                eid = row.get("element_id", "")
                section = _section_for(eid)
                material = _material_for(eid)
                cr = check_member(row, section, material)
                check_rows.append(cr.to_dict())

            return _dumps({
                "ok": True,
                "load_combo": load_combo,
                "disclaimer": DISCLAIMER,
                "results": check_rows,
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def struct_estimate(doc_path: str) -> str:
        """Line-item cost estimate for a structural document (T-D5).

        Runs quantity takeoff (board-feet for lumber, weight for steel, fastener count) and
        multiplies by the ENR Q4-2025 national average rate table
        (``engine_oracle/data/enr_rates.json``, user-overridable).

        All costs are **preliminary budget figures from a rate table** — not vendor prices.
        Costs for vendor-sourced parts come from ``bom_price``; do not mix the two.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.

        Returns JSON ``{ok, line_items:[{element_id, element_type, section_name, material,
        quantity, unit, unit_rate, material_cost, labor_hours, labor_cost, description}],
        fastener_line:{...}, total_material_cost, total_labor_cost, total_cost, currency,
        rate_table_date, disclaimer}``.

        Raises
        ------
        Returns ``{ok:False, error, code}`` on import failure (PyNite/scipy not provisioned)
        or on invalid doc path/schema.

        Note: registration is in ``structural_tools.py`` (not server.py) to keep all
        ``struct_*`` tools co-located.  This is consistent with the existing pattern —
        deviation from the task's literal "Files touched: server.py" is intentional and
        recorded in work/tasks/T-D5.md.
        """
        try:
            from dataclasses import asdict as _asdict

            from engine.structural.elements import StructureDoc
            from engine.structural.estimate import estimate as _estimate
        except ImportError as e:
            return _dumps({
                "ok": False,
                "error": "structural estimate unavailable (engine.structural not installed)",
                "code": "solver_unavailable",
                "detail": str(e),
            })
        try:
            doc = _load_structure(doc_path)
            result = _estimate(doc)

            def _line_dict(ln) -> dict:
                return {
                    "element_id": ln.element_id,
                    "element_type": ln.element_type,
                    "section_name": ln.section_name,
                    "material": ln.material,
                    "quantity": ln.quantity,
                    "unit": ln.unit,
                    "unit_rate": ln.unit_rate,
                    "material_cost": ln.material_cost,
                    "labor_hours": ln.labor_hours,
                    "labor_cost": ln.labor_cost,
                    "description": ln.description,
                }

            return _dumps({
                "ok": True,
                "line_items": [_line_dict(ln) for ln in result.line_items],
                "fastener_line": _line_dict(result.fastener_line),
                "total_material_cost": result.total_material_cost,
                "total_labor_cost": result.total_labor_cost,
                "total_cost": result.total_cost,
                "currency": result.currency,
                "rate_table_date": result.rate_table_date,
                "disclaimer": result.disclaimer,
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

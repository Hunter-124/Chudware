"""layout_plan_tools.py — MCP tool: layout_plan capture before modeling.

Goal: "ensure proper layout plans the first time; not miss critical dims."

Design: layout_plan rides ON aicad.req/1 (contracts/requirements.md — frozen, no schema
change).  We use existing fields:
  - requirements with kind="dimensional"  → critical dimensions (value+tol+why-critical
    carried in quantity + a verify_by hint whose args="critical_dim")
  - requirements with kind="interface"    → component interface requirements (what mates
    what, which features) with args="interface" in verify_by

A lightweight envelope record is stored in doc-meta key "layout_plan":
  {
    "schema": "aicad.layout/1",
    "req_id": "<uuid of the aicad.req/1 doc>",
    "envelope": {"x_mm": ..., "y_mm": ..., "z_mm": ...},   # overall bounding box
    "critical_req_ids": ["R1", "R2", ...],    # req IDs tagged critical_dim
    "interface_req_ids": ["R3", ...],          # req IDs tagged interface
    "created": "<iso8601>",
    "note": "human-readable summary"
  }

This envelope record is what the coverage gate (battery.py _gap_layout_coverage) reads.
The full aicad.req/1 doc (requirements, evidence, status) is stored separately in
doc-meta key "requirements" (as normal by req_tools.py).

Registered by: services/mcp/server.py (append-only).
Coordination: keep battery/req seam edits additive. The interface_check tool's output
  is consumed as evidence; join point documented at bottom of this file.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# Doc-meta keys
_K_LAYOUT_PLAN = "layout_plan"
_K_REQUIREMENTS = "requirements"

# Evidence verifier names accepted as proof for interface requirements.
_INTERFACE_VERIFIERS = frozenset({
    "check_interference",
    "solve_mates",
    "measure_between",
    "fit_report",
    "interface_check",  # fitment/alignment checker
})

# Evidence verifier names accepted as proof for critical-dim requirements
_DIM_VERIFIERS = frozenset({
    "measure_between",
    "measure_part",
    "query_geometry",
    "confirm_dimensions",
    "run_fea_static",
    "run_fea_modal",
    "fit_report",
})


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _core_bridge():
    try:
        from services.mcp import core_bridge as cb
        return cb
    except Exception:
        return None


def _load_layout_plan(doc_id: str) -> Optional[dict]:
    cb = _core_bridge()
    if cb is None:
        return None
    try:
        raw = cb.call("doc_get_meta", {"doc_id": doc_id, "key": _K_LAYOUT_PLAN}).get("value")
        if isinstance(raw, dict) and raw.get("schema") == "aicad.layout/1":
            return raw
    except Exception:
        pass
    return None


def _save_layout_plan(doc_id: str, plan: dict) -> None:
    cb = _core_bridge()
    if cb is not None:
        try:
            cb.call("doc_set_meta", {"doc_id": doc_id, "key": _K_LAYOUT_PLAN, "value": plan})
        except Exception:
            pass


def _load_req_doc(doc_id: str):
    """Load the aicad.req/1 doc associated with doc_id."""
    try:
        from services.requirements import RequirementsDoc
    except ImportError:
        return None
    cb = _core_bridge()
    if cb is None:
        return None
    try:
        raw = cb.call("doc_get_meta", {"doc_id": doc_id, "key": _K_REQUIREMENTS}).get("value")
        if isinstance(raw, dict):
            return RequirementsDoc.from_dict(raw)
    except Exception:
        pass
    return None


def _save_req_doc(doc_id: str, doc) -> None:
    cb = _core_bridge()
    if cb is not None:
        try:
            cb.call("doc_set_meta", {
                "doc_id": doc_id,
                "key": _K_REQUIREMENTS,
                "value": doc.to_dict(),
            })
        except Exception:
            pass


def register_layout_plan_tools(mcp: "FastMCP") -> None:
    """Register layout_plan tools on the given FastMCP instance."""

    @mcp.tool()
    def layout_plan(
        doc_id: str,
        envelope_x_mm: Optional[float] = None,
        envelope_y_mm: Optional[float] = None,
        envelope_z_mm: Optional[float] = None,
        critical_dims: Optional[list] = None,
        interfaces: Optional[list] = None,
        note: Optional[str] = None,
    ) -> str:
        """Capture the layout plan for a design BEFORE modeling starts.

        Call this at intake/planning time — before add_features — to record:
          - Overall envelope (bounding box in mm)
          - Critical dimensions: value + tolerance + why-critical
          - Component interfaces: what mates to what, via which features

        Each critical_dim and interface is stored as a requirement in the doc's
        aicad.req/1 requirements doc (kind="dimensional" or "interface") so that
        req_report shows them and the coverage gate (verify_battery layout_coverage)
        can check that each was verified before sign-off.

        Args:
            doc_id:         Target document id (must already exist via new_part or similar).
            envelope_x_mm:  Overall bounding box X dimension in mm.
            envelope_y_mm:  Overall bounding box Y dimension in mm.
            envelope_z_mm:  Overall bounding box Z dimension in mm.
            critical_dims:  List of dicts, each:
                {
                  "param":     "bore_dia",          # short parameter name
                  "value":     22.0,                 # nominal value in mm
                  "unit":      "mm",                 # unit string per doc_schema.md
                  "tol_plus":  0.0,                  # upper tolerance (non-negative)
                  "tol_minus": 0.013,                # lower tolerance (non-negative)
                  "why":       "mates to shaft bearing — fit-critical",
                  "verify_by": "measure_between"     # verifier name (optional, default measure_between)
                }
            interfaces:     List of dicts, each:
                {
                  "id":        "IF1",                    # short id (IF1, IF2, ...)
                  "desc":      "Bracket to plate via 4-bolt M5 pattern",
                  "verify_by": "check_interference"      # verifier name (optional)
                }
            note:           Human-readable summary of the layout intent.

        Returns JSON with req_id, critical_req_ids, interface_req_ids, and layout_plan summary.
        """
        # Load or create the req doc
        try:
            from services.requirements import RequirementsDoc
        except ImportError:
            return json.dumps({"ok": False, "error": "services.requirements not available"})

        req_doc = _load_req_doc(doc_id)
        created_new = req_doc is None
        if req_doc is None:
            req_doc = RequirementsDoc.new(f"Layout plan")

        # Load or create the layout_plan envelope
        plan = _load_layout_plan(doc_id)
        if plan is None:
            plan = {
                "schema": "aicad.layout/1",
                "req_id": req_doc.id,
                "envelope": {},
                "critical_req_ids": [],
                "interface_req_ids": [],
                "created": _iso_now(),
                "note": note or "",
            }

        # Update envelope
        envelope = plan.get("envelope") or {}
        if envelope_x_mm is not None:
            envelope["x_mm"] = envelope_x_mm
        if envelope_y_mm is not None:
            envelope["y_mm"] = envelope_y_mm
        if envelope_z_mm is not None:
            envelope["z_mm"] = envelope_z_mm
        plan["envelope"] = envelope

        if note:
            plan["note"] = note

        # Ensure layout need exists
        layout_need = None
        for n in req_doc.stakeholder_needs:
            if getattr(n, "source", None) == "layout_plan":
                layout_need = n
                break
        if layout_need is None:
            layout_need = req_doc.add_need(
                "Layout plan: envelope, interfaces, and critical dimensions captured before modeling",
                source="layout_plan",
            )

        critical_req_ids: list[str] = list(plan.get("critical_req_ids") or [])
        interface_req_ids: list[str] = list(plan.get("interface_req_ids") or [])

        # Add critical_dim requirements
        if critical_dims:
            for dim in critical_dims:
                param = dim.get("param", "dim")
                value = dim.get("value")
                unit = dim.get("unit", "mm")
                tol_plus = dim.get("tol_plus", 0.0)
                tol_minus = dim.get("tol_minus", 0.0)
                why = dim.get("why", "critical dimension")
                verifier = dim.get("verify_by", "measure_between")

                # Build quantity (numbers go in quantity only — no numbers in text)
                quantity = {
                    "param": param,
                    "op": "==",
                    "value": value,
                    "unit": unit,
                    "tol": {"plus": float(tol_plus), "minus": float(tol_minus)},
                } if value is not None else None

                # text must be prose only (no numerals) per aicad.req/1 §0.
                # Numerals in why belong in quantity; strip them from the prose text.
                import re as _re
                why_prose = _re.sub(r'\b\d+(\.\d+)?\b', '', why).strip(" —-.,")
                why_prose = why_prose or "critical dimension"
                # args="critical_dim" flags this as a layout-critical dim for the gate
                req = req_doc.add_requirement(
                    need_id=layout_need.id,
                    kind="dimensional",
                    text=f"Critical dimension — {why_prose}",
                    verify_by=[{"verifier": verifier, "args": "critical_dim"}],
                    quantity=quantity,
                )
                critical_req_ids.append(req.id)

        # Add interface requirements
        if interfaces:
            import re as _re
            for iface in interfaces:
                desc = iface.get("desc", "component interface")
                verifier = iface.get("verify_by", "check_interference")
                # text must be prose only — strip numerals from desc
                desc_prose = _re.sub(r'\b\d+(\.\d+)?\b', '', desc).strip(" —-.,")
                desc_prose = desc_prose or "component interface"
                req = req_doc.add_requirement(
                    need_id=layout_need.id,
                    kind="interface",
                    text=f"Interface — {desc_prose}",
                    verify_by=[{"verifier": verifier, "args": "interface"}],
                    quantity=None,
                )
                interface_req_ids.append(req.id)

        plan["critical_req_ids"] = critical_req_ids
        plan["interface_req_ids"] = interface_req_ids
        plan["req_id"] = req_doc.id

        # Persist both
        _save_req_doc(doc_id, req_doc)
        _save_layout_plan(doc_id, plan)

        return json.dumps({
            "ok": True,
            "req_id": req_doc.id,
            "critical_req_ids": critical_req_ids,
            "interface_req_ids": interface_req_ids,
            "envelope": envelope,
            "note": plan.get("note", ""),
            "created_req_doc": created_new,
            "message": (
                f"Layout plan captured: envelope={envelope}, "
                f"{len(critical_req_ids)} critical dim(s), "
                f"{len(interface_req_ids)} interface(s). "
                "Run req_report(doc_id=...) to see coverage. "
                "Run verify_battery(doc_id, ['layout_coverage']) before declaring done."
            ),
        }, indent=2)

    @mcp.tool()
    def layout_plan_report(
        doc_id: str,
    ) -> str:
        """Return the layout plan summary for a document: envelope, critical dims, interfaces,
        and coverage state (how many have been verified vs. outstanding).

        Args:
            doc_id: The document whose layout plan to report.

        Returns a human-readable text report.
        """
        plan = _load_layout_plan(doc_id)
        if plan is None:
            return (
                "No layout plan found for this document.\n"
                "Call layout_plan(doc_id=..., envelope_x_mm=..., critical_dims=[...]) "
                "BEFORE modeling to capture critical dimensions and interfaces."
            )

        req_doc = _load_req_doc(doc_id)

        lines = ["Layout Plan Report", "=" * 18, ""]

        envelope = plan.get("envelope") or {}
        if envelope:
            parts = []
            if "x_mm" in envelope:
                parts.append(f"X={envelope['x_mm']}mm")
            if "y_mm" in envelope:
                parts.append(f"Y={envelope['y_mm']}mm")
            if "z_mm" in envelope:
                parts.append(f"Z={envelope['z_mm']}mm")
            lines.append(f"Envelope: {', '.join(parts)}")
        else:
            lines.append("Envelope: not specified")

        note = plan.get("note", "")
        if note:
            lines.append(f"Note: {note}")
        lines.append("")

        critical_ids = plan.get("critical_req_ids") or []
        interface_ids = plan.get("interface_req_ids") or []

        req_map: dict[str, dict] = {}
        if req_doc:
            for r in req_doc.requirements:
                req_map[r.id] = r.to_dict()

        def _status_icon(req_id: str) -> str:
            r = req_map.get(req_id)
            if r is None:
                return "?"
            s = r.get("status", "proposed")
            return {"verified": "✓", "failed": "✗", "waived": "~"}.get(s, "○")

        lines.append(f"Critical Dimensions ({len(critical_ids)}):")
        for rid in critical_ids:
            r = req_map.get(rid)
            icon = _status_icon(rid)
            if r:
                qty = r.get("quantity") or {}
                param = qty.get("param", "?")
                val = qty.get("value", "?")
                unit = qty.get("unit", "mm")
                tol = qty.get("tol") or {}
                tol_str = f" +{tol['plus']}/-{tol['minus']}" if tol else ""
                lines.append(f"  {icon} {rid}: {param} = {val}{unit}{tol_str}  [{r.get('status','proposed')}]")
                lines.append(f"       {r.get('text','')}")
            else:
                lines.append(f"  {icon} {rid}: (requirement not found in req doc)")

        lines.append("")
        lines.append(f"Interfaces ({len(interface_ids)}):")
        for rid in interface_ids:
            r = req_map.get(rid)
            icon = _status_icon(rid)
            if r:
                lines.append(f"  {icon} {rid}: {r.get('text','')}  [{r.get('status','proposed')}]")
            else:
                lines.append(f"  {icon} {rid}: (requirement not found in req doc)")

        # Coverage summary
        n_verified = sum(
            1 for rid in (critical_ids + interface_ids)
            if req_map.get(rid, {}).get("status") == "verified"
        )
        n_total = len(critical_ids) + len(interface_ids)
        lines.append("")
        lines.append(f"Coverage: {n_verified}/{n_total} verified")
        if n_verified < n_total:
            lines.append("  WARN: unverified critical dims/interfaces — run verify_battery(['layout_coverage'])")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# interface_check join point note
# ---------------------------------------------------------------------------
# Wiring interface_check in:
#   1. Add "interface_check" to _INTERFACE_VERIFIERS above.
#   2. In _gap_layout_coverage (battery.py), the evidence check already accepts any
#      verifier — the join is automatic.
#   3. The dag_planner.md and cad_guide layout_plan section already mention
#      interface_check as the preferred interface verifier (conditional on landing).

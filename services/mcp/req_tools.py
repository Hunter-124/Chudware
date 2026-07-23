"""Requirements CRUD MCP tools — ``aicad.req/1``.

Four tools exposed to the model:
- ``req_create``   — create a new requirements doc (returns doc id)
- ``req_update``   — add/update requirements, needs, or status transitions
- ``req_status``   — record a verifier result (verified/failed) with evidence
- ``req_report``   — return the markdown projection of a requirements doc

Persistence:
- When ``doc_id`` is given: stored as doc-meta key ``"requirements"`` inside the .step+
  (same mechanism as ``studies`` / ``intake``).
- When ``doc_id`` is omitted: stored as a standalone ``.req.json`` file at ``req_path``.
  If neither is given, the doc is returned in memory only (use ``req_create`` return value
  to persist it yourself).

The pure brain is ``services.requirements``.  This module is the thin MCP adapter.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from services.requirements import RequirementsDoc

# Meta key used inside .step+ doc (same convention as intake/_K_INTAKE etc.)
_K_REQUIREMENTS = "requirements"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _core_bridge():
    """Lazy import core_bridge so req_tools works even when the C++ engine is absent."""
    try:
        from mcp_server import core_bridge as cb  # type: ignore
        return cb
    except Exception:  # noqa: BLE001
        return None


def _load_doc(doc_id: Optional[str] = None, req_path: Optional[str] = None) -> Optional[RequirementsDoc]:
    """Load a requirements doc from doc-meta or a .req.json file."""
    if doc_id:
        cb = _core_bridge()
        if cb is None:
            return None
        try:
            raw = cb.call("doc_get_meta", {"doc_id": doc_id, "key": _K_REQUIREMENTS}).get("value")
            if isinstance(raw, dict):
                return RequirementsDoc.from_dict(raw)
        except Exception:  # noqa: BLE001
            pass
        return None

    if req_path:
        p = Path(req_path)
        if p.exists():
            return RequirementsDoc.from_dict(json.loads(p.read_text(encoding="utf-8")))
        return None

    return None


def _save_doc(
    doc: RequirementsDoc,
    doc_id: Optional[str] = None,
    req_path: Optional[str] = None,
) -> str:
    """Persist a requirements doc; returns a human-readable location string."""
    if doc_id:
        cb = _core_bridge()
        if cb is not None:
            try:
                cb.call("doc_set_meta", {
                    "doc_id": doc_id,
                    "key": _K_REQUIREMENTS,
                    "value": doc.to_dict(),
                })
                return f"doc:{doc_id}/requirements"
            except Exception:  # noqa: BLE001
                pass

    if req_path:
        p = Path(req_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return str(p)

    return "(in-memory only)"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_req_tools(mcp: "FastMCP") -> None:
    """Register all requirements CRUD tools on the given FastMCP instance."""

    @mcp.tool()
    def req_create(
        title: str,
        doc_id: Optional[str] = None,
        req_path: Optional[str] = None,
    ) -> str:
        """Create a new ``aicad.req/1`` requirements document.

        Args:
            title:    Human-readable title for the brief.
            doc_id:   If given, persists the doc inside the named .step+ document
                      as a doc-meta entry (same posture as studies/intake).
            req_path: If given (and doc_id is absent), saves to this .req.json file path.

        Returns JSON with the new document id and saved location.
        """
        doc = RequirementsDoc.new(title)
        location = _save_doc(doc, doc_id=doc_id, req_path=req_path)
        return json.dumps({
            "ok": True,
            "req_id": doc.id,
            "location": location,
            "message": f"Created requirements doc '{title}' (id={doc.id}).",
        }, indent=2)

    @mcp.tool()
    def req_update(
        req_id: Optional[str] = None,
        doc_id: Optional[str] = None,
        req_path: Optional[str] = None,
        action: str = "add_requirement",
        need_text: Optional[str] = None,
        need_source: str = "chat",
        need_id: Optional[str] = None,
        kind: Optional[str] = None,
        text: Optional[str] = None,
        quantity: Optional[dict] = None,
        verify_by: Optional[list] = None,
        requirement_id: Optional[str] = None,
    ) -> str:
        """Add or update content in a requirements document.

        Actions:
        - ``add_need``:        Add a stakeholder need.  Requires ``need_text``.
        - ``add_requirement``: Add a new requirement.  Requires ``need_id``, ``kind``,
                               ``text``, ``verify_by`` (list of ``{verifier, args?}``).
                               ``quantity`` is optional for qualitative requirements.
        - ``update_requirement``: Update fields of an existing requirement.  Requires
                               ``requirement_id``; supply any of ``text``, ``kind``,
                               ``quantity``, ``verify_by`` to change.

        For ``doc_id``/``req_path``: same persistence rules as ``req_create``.
        ``req_id`` (the requirements doc UUID) is used to validate the loaded document id.
        """
        doc = _load_doc(doc_id=doc_id, req_path=req_path)
        if doc is None:
            return json.dumps({"ok": False, "error": "Requirements doc not found. Call req_create first."})

        if req_id and doc.id != req_id:
            return json.dumps({"ok": False, "error": f"req_id mismatch: loaded doc is {doc.id!r}, expected {req_id!r}."})

        try:
            if action == "add_need":
                if not need_text:
                    return json.dumps({"ok": False, "error": "need_text is required for add_need."})
                n = doc.add_need(need_text, source=need_source)
                msg = f"Added need {n.id}."

            elif action == "add_requirement":
                if not (need_id and kind and text and verify_by):
                    return json.dumps({
                        "ok": False,
                        "error": "need_id, kind, text, and verify_by are required for add_requirement.",
                    })
                r = doc.add_requirement(
                    need_id=need_id,
                    kind=kind,
                    text=text,
                    verify_by=verify_by,
                    quantity=quantity,
                )
                msg = f"Added requirement {r.id}."

            elif action == "update_requirement":
                if not requirement_id:
                    return json.dumps({"ok": False, "error": "requirement_id is required for update_requirement."})
                r = doc.update_requirement(
                    requirement_id,
                    who="agent",
                    text=text,
                    kind=kind,
                    quantity=quantity,
                    verify_by=verify_by,
                )
                msg = f"Updated requirement {r.id}."

            else:
                return json.dumps({"ok": False, "error": f"Unknown action {action!r}. Use: add_need, add_requirement, update_requirement."})

        except (ValueError, KeyError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        location = _save_doc(doc, doc_id=doc_id, req_path=req_path)
        return json.dumps({"ok": True, "message": msg, "location": location}, indent=2)

    @mcp.tool()
    def req_status(
        requirement_id: str,
        new_status: str,
        who: str = "agent",
        evidence: Optional[list] = None,
        doc_id: Optional[str] = None,
        req_path: Optional[str] = None,
    ) -> str:
        """Record a status transition on a requirement (e.g. verified/failed after a verifier runs).

        Args:
            requirement_id: The requirement ID to update (e.g. "R1").
            new_status:     Target status: ``proposed``, ``user_confirmed``, ``verified``,
                            ``failed``, or ``waived``.
            who:            ``"agent"`` (automated verifier) or ``"user"`` (explicit action).
                            Agents may NOT set ``user_confirmed`` or ``waived``.
            evidence:       List of evidence dicts to append to ``trace.evidence``.
                            Each dict: ``{verifier, result, ts}``.
            doc_id:         Persist inside this .step+ doc.
            req_path:       Persist to this .req.json file.

        Status ladder: proposed → user_confirmed (user only) → verified/failed (agent ok) → waived (user only).
        """
        doc = _load_doc(doc_id=doc_id, req_path=req_path)
        if doc is None:
            return json.dumps({"ok": False, "error": "Requirements doc not found."})

        try:
            req = doc.set_status(requirement_id, new_status, who=who, evidence=evidence)
        except (ValueError, KeyError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        location = _save_doc(doc, doc_id=doc_id, req_path=req_path)
        return json.dumps({
            "ok": True,
            "requirement_id": req.id,
            "status": req.status,
            "evidence_count": len(req.trace.evidence),
            "location": location,
        }, indent=2)

    @mcp.tool()
    def req_report(
        doc_id: Optional[str] = None,
        req_path: Optional[str] = None,
        format: str = "markdown",
    ) -> str:
        """Return the requirements document as a markdown or JSON report.

        Args:
            doc_id:   Load from this .step+ doc.
            req_path: Load from this .req.json file.
            format:   ``"markdown"`` (default) or ``"json"``.

        The markdown projection is a read-only, deterministic, diff-stable view.
        Use ``format="json"`` to get the full ``aicad.req/1`` object.
        """
        doc = _load_doc(doc_id=doc_id, req_path=req_path)
        if doc is None:
            return json.dumps({"ok": False, "error": "Requirements doc not found. Use req_create to start one."})

        if format == "json":
            return json.dumps(doc.to_dict(), indent=2)

        return _markdown_with_battery_evidence(doc)

    @mcp.tool()
    def req_verify_by_id(
        requirement_id: str,
        doc_id: Optional[str] = None,
        req_path: Optional[str] = None,
        req_doc_id: Optional[str] = None,
    ) -> str:
        """Auto-verify a single requirement by running its named ``verify_by`` verifiers.

        Runs the battery verifiers listed in the requirement's ``verify_by`` field,
        writes the result (status + evidence) back into the requirements doc, and
        returns a JSON summary.

        Verifiers not supported by the battery in v1 (``confirm_dimensions``,
        ``dfm_check``, ``fit_report``) are skipped and noted in the response.

        Args:
            requirement_id: The requirement ID to verify (e.g. "R1").
            doc_id:         Geometry document the verifiers run against.
            req_path:       Path to the ``.req.json`` file holding the requirements doc.
            req_doc_id:     Doc-meta doc_id holding the requirements doc (alternative
                            to req_path; if omitted, falls back to ``doc_id``).

        Returns JSON: ``{ok, requirement_id, status, evidence_count, verifiers_run, unsupported}``.
        """
        from services.dag.verify import _run_requirement_verify, VERDICT_DONE
        import time as _time
        import json as _json

        # Only fall back to the geometry doc_id as the req-doc holder when no
        # req_path is given: _load_doc prioritizes doc_id and never falls
        # through to req_path, so a stray fallback would shadow the file.
        _req_doc_id = req_doc_id or (doc_id if not req_path else None)
        node_inputs: dict = {
            "req_id": requirement_id,
            "req_path": req_path,
            "req_doc_id": _req_doc_id,
            "doc_id": doc_id,
            "check_type": "requirement",
        }
        verdict, output_state = _run_requirement_verify(
            node_id="mcp-req-verify",
            node_name=f"req_verify_by_id({requirement_id})",
            node_inputs=node_inputs,
            retry_remaining=1,
            emit=None,
            tool_fn_override=None,
            t0=_time.monotonic(),
        )
        result = output_state.get("data", {}).get("tool_result") or {}
        reason = output_state.get("data", {}).get("reason")
        if verdict == VERDICT_DONE:
            status = "verified"
        elif reason == "check_failed":
            status = "failed"
        else:
            status = "error"  # doc/req not found, no supported verifiers, writeback error…
        return _json.dumps({
            "ok": verdict == VERDICT_DONE,
            "requirement_id": requirement_id,
            "status": status,
            "evidence_count": result.get("evidence_count", 0),
            "verifiers_run": result.get("verifiers_run", []),
            "unsupported": result.get("unsupported", []),
            "summary": output_state.get("summary", ""),
        }, indent=2)


def _markdown_with_battery_evidence(doc) -> str:
    """Extend the standard markdown projection with a battery-evidence section.

    Battery evidence entries carry ``source == "battery"`` in their evidence list.
    Hand-entered evidence lacks that marker.
    This is additive — the base ``to_markdown()`` output is unchanged.
    """
    base = doc.to_markdown()

    from services.requirements.requirements import _sort_key

    # Check if any requirements have battery evidence
    battery_lines: list[str] = []
    for req in sorted(doc.requirements, key=lambda r: _sort_key(r.id)):
        b_evs = [
            ev for ev in req.trace.evidence
            if isinstance(ev, dict) and ev.get("source") == "battery"
        ]
        h_evs = [
            ev for ev in req.trace.evidence
            if isinstance(ev, dict) and ev.get("source") != "battery"
        ]
        if b_evs or h_evs:
            battery_lines.append(f"### {req.id} — {req.status}")
            if b_evs:
                battery_lines.append(f"**Battery-populated evidence** ({len(b_evs)} entry/entries):")
                for ev in b_evs:
                    verifier = ev.get("verifier", "?")
                    ts = ev.get("ts", "?")
                    res = ev.get("result", {})
                    ok_str = "PASS" if res.get("ok", False) else "FAIL"
                    battery_lines.append(f"  - `{verifier}` at {ts}: {ok_str}")
            if h_evs:
                battery_lines.append(f"**Hand-entered evidence** ({len(h_evs)} entry/entries):")
                for ev in h_evs:
                    verifier = ev.get("verifier", "?")
                    ts = ev.get("ts", "?")
                    battery_lines.append(f"  - `{verifier}` at {ts}")

    if not battery_lines:
        return base

    evidence_section = "\n## Evidence\n\n" + "\n".join(battery_lines) + "\n"
    return base + evidence_section
